"""Billing webhook ingestion and processing (Bundle 7 spec §12.1-§12.3).

Ingest verifies the signature, then stores the event once per provider event
id (a bad signature is stored unverified, truncated, for ops visibility, and
never processed). Processing re-fetches the subscription snapshot and runs the
pure state machine, so duplicate and out-of-order events converge on the
provider's state. An event that cannot be attributed to an account yet raises
``RetryLater`` and stays pending with its attempt counted.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Mapping

from product.subscription_state import TERMINAL, transition
from webapp.billing.port import BillingProvider, WebhookSignatureInvalid
from webapp.persistence import billing as rows
from webapp.persistence import dbapi
from webapp.persistence.audit import audit
from webapp.services import notifications

log = logging.getLogger("webapp.billing")

MAX_UNVERIFIED_PAYLOAD_BYTES = 4096


class RetryLater(Exception):
    """The event is valid but cannot be applied yet; it stays pending."""


class WebhookRejected(Exception):
    def __init__(self, row_id: str):
        super().__init__("webhook signature did not verify")
        self.row_id = row_id


def _truncated(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")[:MAX_UNVERIFIED_PAYLOAD_BYTES]
    while True:
        payload = json.dumps({"unverified_body": text}, ensure_ascii=False)
        if len(payload.encode("utf-8")) <= MAX_UNVERIFIED_PAYLOAD_BYTES:
            return payload
        text = text[:-256]


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


class BillingWebhooks:
    def __init__(self, provider: BillingProvider, *, catalog_version: str, settings: Any = None) -> None:
        self.provider = provider
        self.catalog_version = catalog_version  # pinned onto new subscriptions (DP-8)
        self.settings = settings  # Task 26: entitlement-dependent automation is reconciled after a change

    # ---- ingestion ----------------------------------------------------------------
    def ingest(self, conn: dbapi.Connection, *, headers: Mapping[str, str], body: bytes, now: datetime) -> str:
        try:
            event = self.provider.verify_webhook(headers=headers, body=body, now=now)
        except WebhookSignatureInvalid:
            row_id = f"whk_{uuid.uuid4().hex[:20]}"
            conn.execute("INSERT INTO billing_webhook_events (id, provider, provider_event_id, received_at, "
                         "signature_verified, payload_json) VALUES (?, ?, NULL, ?, 0, ?)",
                         (row_id, self.provider.name, now.isoformat(), _truncated(body)))
            raise WebhookRejected(row_id) from None
        existing = conn.execute("SELECT id FROM billing_webhook_events WHERE provider_event_id = ?",
                                (event.provider_event_id,)).fetchone()
        if existing is not None:
            return existing[0]
        envelope = {"event": {"id": event.provider_event_id, "type": event.type,
                              "subscription_id": event.subscription_id, "customer_id": event.customer_id,
                              "occurred_at": event.occurred_at.isoformat()},
                    "raw": dict(event.raw)}
        row_id = f"whk_{uuid.uuid4().hex[:20]}"
        try:
            conn.execute("INSERT INTO billing_webhook_events (id, provider, provider_event_id, received_at, "
                         "signature_verified, payload_json) VALUES (?, ?, ?, ?, 1, ?)",
                         (row_id, self.provider.name, event.provider_event_id, now.isoformat(),
                          json.dumps(envelope, sort_keys=True, default=str)))
        except dbapi.IntegrityError:  # a concurrent delivery of the same event stored it first
            return conn.execute("SELECT id FROM billing_webhook_events WHERE provider_event_id = ?",
                                (event.provider_event_id,)).fetchone()[0]
        # Durable processing (Task 18): the worker retries until the event applies
        # (e.g. an early webhook whose checkout is not recorded yet).
        from webapp.worker.runner import enqueue
        enqueue(conn, kind="billing.webhook.process", payload={"event_row_id": row_id},
                dedupe_key=f"billing-webhook:{row_id}", now=now)
        return row_id

    # ---- processing ---------------------------------------------------------------
    def process_pending(self, conn: dbapi.Connection, *, now: datetime, limit: int = 100) -> int:
        pending = [r[0] for r in conn.execute(
            "SELECT id FROM billing_webhook_events WHERE processed_at IS NULL AND signature_verified = 1 "
            "AND provider = ? ORDER BY received_at, id LIMIT ?", (self.provider.name, limit))]
        for row_id in pending:
            self.process_event(conn, row_id, now=now)
        return len(pending)

    def process_event(self, conn: dbapi.Connection, row_id: str, *, now: datetime) -> None:
        row = conn.execute("SELECT * FROM billing_webhook_events WHERE id = ?", (row_id,)).fetchone()
        if row is None or row["processed_at"] is not None or not row["signature_verified"]:
            return
        event = json.loads(row["payload_json"])["event"]
        conn.execute("SAVEPOINT billing_webhook")
        try:
            if event["subscription_id"]:
                self._apply(conn, event, now=now)
        except Exception as exc:  # noqa: BLE001 - every failure leaves the event pending for retry
            conn.execute("ROLLBACK TO SAVEPOINT billing_webhook")
            conn.execute("RELEASE SAVEPOINT billing_webhook")
            if not isinstance(exc, RetryLater):
                log.warning("billing_webhook_failed", extra={"event_row": row_id, "error": type(exc).__name__})
            conn.execute("UPDATE billing_webhook_events SET attempts = attempts + 1, process_error = ? WHERE id = ?",
                         (f"{type(exc).__name__}: {exc}"[:500], row_id))
            return
        conn.execute("RELEASE SAVEPOINT billing_webhook")
        conn.execute("UPDATE billing_webhook_events SET attempts = attempts + 1, process_error = NULL, "
                     "processed_at = ? WHERE id = ?", (now.isoformat(), row_id))

    def _apply(self, conn: dbapi.Connection, event: dict[str, Any], *, now: datetime) -> None:
        snapshot = self.provider.fetch_subscription(event["subscription_id"])
        existing = conn.execute("SELECT * FROM subscriptions WHERE provider_subscription_id = ?",
                                (snapshot.provider_subscription_id,)).fetchone()
        existing = None if existing is None else dict(existing)
        if existing is not None:
            account_id = existing["account_id"]
        else:
            owner = conn.execute("SELECT account_id FROM billing_customers WHERE provider = ? "
                                 "AND provider_customer_id = ?",
                                 (self.provider.name, snapshot.provider_customer_id)).fetchone()
            if owner is None:
                raise RetryLater(f"no account for provider customer {snapshot.provider_customer_id}")
            account_id = owner[0]

        current = existing["state"] if existing else None
        new_state, updates = transition(current, snapshot, now=now)
        if new_state == "UNKNOWN" and current != "UNKNOWN":
            # §12.2: fail closed to Free; ops sees it (admin alerts arrive with the ops console).
            log.warning("billing_unknown_status", extra={"account_id": account_id,
                                                         "subscription": snapshot.provider_subscription_id})
        body_hash = hashlib.sha256(rows.snapshot_json(snapshot).encode()).hexdigest()
        if existing is not None and existing["state"] == new_state and existing["snapshot_hash"] == body_hash:
            return  # a duplicate: nothing changed at the provider
        past_due_since = updates["past_due_since"] if "past_due_since" in updates \
            else _parse(existing["past_due_since"] if existing else None)

        if existing is None and new_state not in TERMINAL:
            self._supersede(conn, account_id, event, now=now)
        row_id, previous = rows.upsert_subscription(
            conn, account_id=account_id, provider=self.provider.name, snapshot=snapshot, state=new_state,
            catalog_version=existing["catalog_version"] if existing else self.catalog_version, now=now,
            past_due_since=past_due_since)
        if existing is None:
            self._settle_checkout(conn, account_id, snapshot.plan_id, new_state, now=now)

        plan_changed = existing is not None and existing["plan_id"] != snapshot.plan_id
        if previous != new_state or plan_changed:
            self._record(conn, row_id, account_id, previous, new_state, event, now=now,
                         detail={"plan_id": snapshot.plan_id, "interval": snapshot.interval})
            if self.settings is not None:  # §20.3: a downgrade below Power disables schedules at once
                from webapp.services.search_schedules import reconcile_entitlements
                reconcile_entitlements(conn, account_id, settings=self.settings, now=now)
        if new_state == "PAST_DUE" and previous != "PAST_DUE":
            self._notify(conn, account_id, "billing.payment_failed", row_id,
                         f"billing.payment_failed:{row_id}:{past_due_since.isoformat()}", now=now)
        if plan_changed:
            self._notify(conn, account_id, "billing.subscription_changed", row_id,
                         f"billing.subscription_changed:{row_id}:{event['id']}", now=now,
                         detail={"from_plan": existing["plan_id"], "to_plan": snapshot.plan_id})
        if new_state == "ENDED" and previous != "ENDED":
            self._notify(conn, account_id, "billing.subscription_canceled", row_id,
                         f"billing.subscription_canceled:{row_id}", now=now)

    def _supersede(self, conn: dbapi.Connection, account_id: str, event: dict[str, Any], *, now: datetime) -> None:
        """A new live subscription replaces an abandoned INCOMPLETE one; any other
        live subscription is a conflict for operations, not something to guess at."""
        other = rows.live_subscription(conn, account_id)
        if other is None:
            return
        if other["state"] != "INCOMPLETE":
            raise RetryLater(f"account {account_id} already has a live subscription {other['id']}")
        conn.execute("UPDATE subscriptions SET state = 'INCOMPLETE_EXPIRED', updated_at = ? WHERE id = ?",
                     (now.isoformat(), other["id"]))
        self._record(conn, other["id"], account_id, "INCOMPLETE", "INCOMPLETE_EXPIRED",
                     {**event, "type": "superseded"}, now=now, detail={})

    def _settle_checkout(self, conn: dbapi.Connection, account_id: str, plan_id: str, state: str, *,
                         now: datetime) -> None:
        status = "EXPIRED" if state in ("INCOMPLETE", "INCOMPLETE_EXPIRED") else "COMPLETED"
        conn.execute("UPDATE checkout_sessions SET status = ?, completed_at = ? WHERE account_id = ? "
                     "AND plan_id = ? AND status = 'OPEN'", (status, now.isoformat(), account_id, plan_id))

    def _record(self, conn: dbapi.Connection, subscription_row_id: str, account_id: str, from_state: str | None,
                to_state: str, event: dict[str, Any], *, now: datetime, detail: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO subscription_events (id, subscription_id, account_id, from_state, to_state, cause, "
            "provider_event_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"sev_{uuid.uuid4().hex[:20]}", subscription_row_id, account_id, from_state, to_state, event["type"],
             event.get("id"), now.isoformat()))
        audit(conn, actor_type="PROVIDER", actor_id=self.provider.name, account_id=account_id,
              action="SUBSCRIPTION_STATE_CHANGED", now=now, target_type="subscription",
              target_id=subscription_row_id, detail={"from": from_state, "to": to_state, **detail})

    def _notify(self, conn: dbapi.Connection, account_id: str, kind: str, subscription_row_id: str,
                dedupe_key: str, *, now: datetime, detail: dict[str, Any] | None = None) -> None:
        notifications.notify(conn, account_id=account_id, kind=kind, subject_type="subscription",
                             subject_id=subscription_row_id, dedupe_key=dedupe_key, detail=detail or {}, now=now)


# Module-level names from the plan's interface (Task 18 moves processing onto the worker).
def ingest_webhook(conn, provider: BillingProvider, *, headers, body, now, catalog_version: str) -> str:
    return BillingWebhooks(provider, catalog_version=catalog_version).ingest(conn, headers=headers, body=body, now=now)


def process_webhook_event(conn, event_row_id: str, *, provider: BillingProvider, now, catalog_version: str) -> None:
    BillingWebhooks(provider, catalog_version=catalog_version).process_event(conn, event_row_id, now=now)


def process_pending_webhooks(conn, *, provider: BillingProvider, now, catalog_version: str) -> int:
    return BillingWebhooks(provider, catalog_version=catalog_version).process_pending(conn, now=now)
