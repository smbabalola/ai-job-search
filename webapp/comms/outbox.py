"""The transactional outbox (Bundle 7 spec §18.1-§18.2).

Messages are enqueued in the producing transaction and never sent from a
request. The ``outbox.dispatch`` job claims due rows under a 5-minute lease
(status SENDING), then per message: suppression check → marketing refusal →
render the versioned template → ``EmailProvider.send`` with the message's
idempotency key. Transient failures back off 1 m, 5 m, 30 m, 2 h, 6 h, then
FAILED (dead letter); permanent failures are FAILED at once. Once a message
is SENT, SUPPRESSED or CANCELED its payload's credentials (tokens and the
links that carry them) are redacted.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from webapp.comms.email_port import PermanentSendError, TransientSendError
from webapp.comms.render import TEMPLATE_CATEGORIES, TEMPLATE_VERSION, render
from webapp.persistence import dbapi

__all__ = [
    "CATEGORIES", "TRANSIENT_BACKOFF_SECONDS", "address_hash", "claim_due", "dispatch_batch", "dispatch_one",
    "enqueue", "provider_from_settings", "record_email_event", "suppress",
]

logger = logging.getLogger("webapp.comms")

CATEGORIES = ("SERVICE", "PRODUCT", "MARKETING")
TRANSIENT_BACKOFF_SECONDS = (60, 300, 1800, 7200, 21600)
SEND_LEASE = timedelta(minutes=5)
KICK_SLOT_SECONDS = 10
HARD_BOUNCE_GRACE = timedelta(days=30)
# SERVICE auth mail that may still reach an address whose hard bounce is over 30 days old.
BOUNCE_EXEMPT_TEMPLATES = frozenset({"auth.verify_email", "auth.password_reset"})
SECRET_PAYLOAD_KEYS = frozenset({"token", "verify_url", "reset_url", "confirm_url", "download_url"})
TERMINAL = frozenset({"SENT", "FAILED", "SUPPRESSED", "CANCELED"})


def ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def address_hash(address: str) -> str:
    from webapp.persistence.identity import normalize_email
    try:
        normalized = normalize_email(address)
    except ValueError:
        normalized = address.strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def enqueue(conn: dbapi.Connection, *, category: str, template_id: str, to_address: str, payload: Mapping[str, Any],
            account_id: str | None = None, user_id: str | None = None, idempotency_key: str, locale: str = "en",
            now: datetime) -> str | None:
    """Inside the producing transaction (no commit). None when the key exists."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown message category {category}")
    if template_id not in TEMPLATE_CATEGORIES:
        raise ValueError(f"unknown email template {template_id}")
    message_id = f"msg_{uuid.uuid4().hex[:20]}"
    cursor = conn.execute(
        "INSERT INTO outbound_messages (id, account_id, user_id, channel, category, template_id, template_version, "
        "locale, to_address, payload_json, idempotency_key, status, attempts, next_attempt_at, created_at) "
        "VALUES (?, ?, ?, 'EMAIL', ?, ?, ?, ?, ?, ?, ?, 'QUEUED', 0, ?, ?) ON CONFLICT DO NOTHING",
        (message_id, account_id, user_id, category, template_id, TEMPLATE_VERSION, locale, to_address,
         json.dumps(dict(payload), sort_keys=True, default=str), idempotency_key, ts(now), ts(now)))
    if cursor.rowcount != 1:
        return None
    from webapp.worker.runner import enqueue as enqueue_job
    slot = int(now.timestamp()) // KICK_SLOT_SECONDS
    enqueue_job(conn, kind="outbox.dispatch", payload={}, dedupe_key=f"outbox-kick:{slot}", max_attempts=3, now=now)
    return message_id


def suppress(conn: dbapi.Connection, address: str, *, reason: str, now: datetime) -> None:
    """No commit. A complaint is never downgraded to a bounce."""
    if reason not in ("HARD_BOUNCE", "COMPLAINT"):
        raise ValueError(f"unknown suppression reason {reason}")
    conn.execute(
        "INSERT INTO email_suppressions (address_hash, reason, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT (address_hash) DO UPDATE SET "
        "reason = CASE WHEN email_suppressions.reason = 'COMPLAINT' THEN 'COMPLAINT' ELSE excluded.reason END, "
        "created_at = excluded.created_at",
        (address_hash(address), reason, ts(now)))


def record_email_event(conn: dbapi.Connection, *, provider: str, provider_event_id: str, kind: str,
                       address: str | None, payload: Mapping[str, Any], now: datetime) -> bool:
    """Store a normalized provider event once and apply it (no commit).
    False for a redelivery."""
    cursor = conn.execute(
        "INSERT INTO email_provider_events (id, provider, provider_event_id, kind, address_hash, payload_json, "
        "received_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (f"eev_{uuid.uuid4().hex[:20]}", provider, provider_event_id, kind,
         address_hash(address) if address else None, json.dumps(dict(payload), sort_keys=True, default=str),
         ts(now)))
    if cursor.rowcount != 1:
        return False
    if kind in ("HARD_BOUNCE", "COMPLAINT") and address:
        suppress(conn, address, reason=kind, now=now)
    conn.execute("UPDATE email_provider_events SET processed_at = ? WHERE provider = ? AND provider_event_id = ?",
                 (ts(now), provider, provider_event_id))
    return True


_DUE = "((status = 'QUEUED' AND next_attempt_at <= ?) OR (status = 'SENDING' AND next_attempt_at <= ?))"


def claim_due(conn: dbapi.Connection, *, now: datetime, limit: int = 20) -> list[str]:
    """Lease due messages (QUEUED, or SENDING past its lease) as SENDING. No commit."""
    stamp = ts(now)
    candidates = [r[0] for r in conn.execute(
        f"SELECT id FROM outbound_messages WHERE {_DUE} ORDER BY next_attempt_at, id LIMIT ?", (stamp, stamp, limit))]
    claimed = []
    for message_id in candidates:
        cursor = conn.execute(f"UPDATE outbound_messages SET status = 'SENDING', next_attempt_at = ? "
                              f"WHERE id = ? AND {_DUE}", (ts(now + SEND_LEASE), message_id, stamp, stamp))
        if cursor.rowcount == 1:
            claimed.append(message_id)
    return claimed


def _redacted(payload_json: str) -> str:
    payload = json.loads(payload_json)
    return json.dumps({k: ("[REDACTED]" if k in SECRET_PAYLOAD_KEYS else v) for k, v in payload.items()},
                      sort_keys=True, default=str)


def _set(conn: dbapi.Connection, row: Mapping[str, Any], status: str, **fields: Any) -> str:
    values = {"status": status, **fields}
    if status in TERMINAL and status != "FAILED":
        values["payload_json"] = _redacted(row["payload_json"])
    assignments = ", ".join(f"{column} = ?" for column in values)
    conn.execute(f"UPDATE outbound_messages SET {assignments} WHERE id = ?", (*values.values(), row["id"]))
    return status


def _suppressed(conn: dbapi.Connection, row: Mapping[str, Any], now: datetime) -> bool:
    found = conn.execute("SELECT reason, created_at FROM email_suppressions WHERE address_hash = ?",
                         (address_hash(row["to_address"]),)).fetchone()
    if found is None:
        return False
    old_bounce = found[0] == "HARD_BOUNCE" and datetime.fromisoformat(found[1]) <= now - HARD_BOUNCE_GRACE
    return not (old_bounce and row["template_id"] in BOUNCE_EXEMPT_TEMPLATES)


def dispatch_one(conn: dbapi.Connection, message_id: str, *, provider: Any, now: datetime, app_origin: str) -> str:
    """Run the §18.2 steps for one message; returns the resulting status. No commit."""
    found = conn.execute("SELECT * FROM outbound_messages WHERE id = ?", (message_id,)).fetchone()
    if found is None:
        raise ValueError(f"no outbound message {message_id}")
    row = dict(found)
    if row["status"] in TERMINAL:
        return row["status"]
    if _suppressed(conn, row, now):
        return _set(conn, row, "SUPPRESSED", last_error="ADDRESS_SUPPRESSED")
    if row["category"] == "MARKETING":  # Bundle 7 has no marketing path
        return _set(conn, row, "CANCELED", last_error="NO_CONSENT_OR_NOT_ENABLED")
    try:
        rendered = render(row["template_id"], json.loads(row["payload_json"]), category=row["category"],
                          app_origin=app_origin, version=row["template_version"])
    except Exception as exc:  # noqa: BLE001 - a template that cannot render never will
        return _set(conn, row, "FAILED", last_error=f"render failed: {type(exc).__name__}: {exc}"[:2000])
    attempts = row["attempts"] + 1
    try:
        provider_message_id = provider.send(
            to=row["to_address"], subject=rendered.subject, text=rendered.text, html=rendered.html,
            idempotency_key=row["idempotency_key"],
            headers={"X-JobSearch-Category": row["category"], "X-JobSearch-Template": row["template_id"]})
    except PermanentSendError as exc:
        return _set(conn, row, "FAILED", attempts=attempts, provider=provider.name, last_error=str(exc)[:2000])
    except (TransientSendError, OSError, TimeoutError) as exc:
        if attempts > len(TRANSIENT_BACKOFF_SECONDS):
            logger.error("outbox_dead_letter message=%s template=%s", row["id"], row["template_id"])
            return _set(conn, row, "FAILED", attempts=attempts, provider=provider.name, last_error=str(exc)[:2000])
        retry_at = now + timedelta(seconds=TRANSIENT_BACKOFF_SECONDS[attempts - 1])
        return _set(conn, row, "QUEUED", attempts=attempts, provider=provider.name, last_error=str(exc)[:2000],
                    next_attempt_at=ts(retry_at))
    return _set(conn, row, "SENT", attempts=attempts, provider=provider.name,
                provider_message_id=str(provider_message_id), sent_at=ts(now), last_error=None)


def provider_from_settings(settings: Any) -> Any:
    if settings.email_provider == "console":
        from webapp.comms.console import ConsoleEmailProvider
        log_path = settings.db_path.parent / "outbox.log" if settings.db_path else None
        return ConsoleEmailProvider(log_path)
    if settings.email_provider == "smtp":
        from webapp.comms.smtp import SmtpEmailProvider
        smtp = settings.smtp
        return SmtpEmailProvider(smtp["host"], int(smtp.get("port", 587)), smtp.get("username"),
                                 smtp.get("password"), bool(smtp.get("starttls", True)), smtp["from_address"])
    raise ValueError(f"unknown email provider {settings.email_provider}")


def dispatch_batch(conn: dbapi.Connection, *, provider: Any, now: datetime, app_origin: str, limit: int = 20) -> int:
    """The ``outbox.dispatch`` handler body: claim a batch, dispatch each (commits per message)."""
    claimed = claim_due(conn, now=now, limit=limit)
    conn.commit()
    for message_id in claimed:
        try:
            dispatch_one(conn, message_id, provider=provider, now=now, app_origin=app_origin)
            conn.commit()
        except Exception:  # noqa: BLE001 - one bad message never blocks the batch; its lease lapses and it retries
            conn.rollback()
            logger.exception("outbox_dispatch_error message=%s", message_id)
    return len(claimed)
