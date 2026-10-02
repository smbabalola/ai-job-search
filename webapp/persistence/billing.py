"""Billing rows (Bundle 7 spec §12.1). Subscriptions are derived from provider
snapshots; nothing here decides a state (that is ``product.subscription_state``)."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from product.entitlements import SubscriptionView
from webapp.billing.port import TERMINAL_STATES, SubscriptionSnapshot
from webapp.persistence import dbapi

_LIVE = "state IN ('INCOMPLETE', 'TRIALING', 'ACTIVE', 'PAST_DUE', 'CANCEL_SCHEDULED', 'UNKNOWN')"


def _one(conn: dbapi.Connection, sql: str, params: tuple) -> dict[str, Any] | None:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else dict(row)


# ---- customers ----------------------------------------------------------------

def customer(conn: dbapi.Connection, account_id: str) -> dict[str, Any] | None:
    return _one(conn, "SELECT * FROM billing_customers WHERE account_id = ?", (account_id,))


def insert_customer(conn: dbapi.Connection, *, account_id: str, provider: str, provider_customer_id: str,
                    now: datetime) -> None:
    conn.execute("INSERT INTO billing_customers (account_id, provider, provider_customer_id, created_at) "
                 "VALUES (?, ?, ?, ?)", (account_id, provider, provider_customer_id, now.isoformat()))


# ---- checkout sessions ----------------------------------------------------------

def open_checkout(conn: dbapi.Connection, account_id: str, plan_id: str, interval: str) -> dict[str, Any] | None:
    return _one(conn, "SELECT * FROM checkout_sessions WHERE account_id = ? AND plan_id = ? AND interval = ? "
                      "AND status = 'OPEN'", (account_id, plan_id, interval))


def insert_checkout(conn: dbapi.Connection, *, checkout_id: str, account_id: str, provider: str,
                    provider_session_id: str, plan_id: str, interval: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO checkout_sessions (id, account_id, provider, provider_session_id, plan_id, interval, status, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?)",
        (checkout_id, account_id, provider, provider_session_id, plan_id, interval, now.isoformat()))


def expire_checkout(conn: dbapi.Connection, checkout_id: str) -> None:
    conn.execute("UPDATE checkout_sessions SET status = 'EXPIRED' WHERE id = ? AND status = 'OPEN'", (checkout_id,))


def latest_checkout(conn: dbapi.Connection, account_id: str) -> dict[str, Any] | None:
    return _one(conn, "SELECT * FROM checkout_sessions WHERE account_id = ? ORDER BY created_at DESC, id DESC "
                      "LIMIT 1", (account_id,))


# ---- subscriptions --------------------------------------------------------------

def live_subscription(conn: dbapi.Connection, account_id: str) -> dict[str, Any] | None:
    return _one(conn, f"SELECT * FROM subscriptions WHERE account_id = ? AND {_LIVE}", (account_id,))


def has_subscription_history(conn: dbapi.Connection, account_id: str) -> bool:
    return conn.execute("SELECT 1 FROM subscriptions WHERE account_id = ? LIMIT 1", (account_id,)).fetchone() \
        is not None


def snapshot_json(snapshot: SubscriptionSnapshot) -> str:
    return json.dumps(dataclasses.asdict(snapshot), sort_keys=True, default=lambda v: v.isoformat())


def upsert_subscription(conn: dbapi.Connection, *, account_id: str, provider: str, snapshot: SubscriptionSnapshot,
                        state: str, catalog_version: str, now: datetime,
                        past_due_since: datetime | None = None) -> tuple[str, str | None]:
    """Write the account's row for this provider subscription. Returns
    (subscription row id, the state it had before, or None when new)."""
    body = snapshot_json(snapshot)
    values = (snapshot.plan_id, snapshot.interval, state, snapshot.current_period_start.isoformat(),
              snapshot.current_period_end.isoformat(), int(snapshot.cancel_at_period_end),
              None if past_due_since is None else past_due_since.isoformat(), body,
              hashlib.sha256(body.encode()).hexdigest(), now.isoformat())
    existing = _one(conn, "SELECT id, state FROM subscriptions WHERE provider_subscription_id = ?",
                    (snapshot.provider_subscription_id,))
    if existing is None:
        row_id = f"sub_{uuid.uuid4().hex[:20]}"
        conn.execute(
            "INSERT INTO subscriptions (plan_id, interval, state, current_period_start, current_period_end, "
            "cancel_at_period_end, past_due_since, snapshot_json, snapshot_hash, updated_at, id, account_id, "
            "provider, provider_subscription_id, catalog_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values + (row_id, account_id, provider, snapshot.provider_subscription_id, catalog_version))
        return row_id, None
    conn.execute(
        "UPDATE subscriptions SET plan_id = ?, interval = ?, state = ?, current_period_start = ?, "
        "current_period_end = ?, cancel_at_period_end = ?, past_due_since = ?, snapshot_json = ?, "
        "snapshot_hash = ?, updated_at = ? WHERE id = ?", values + (existing["id"],))
    return existing["id"], existing["state"]


def subscription_view(conn: dbapi.Connection, account_id: str) -> SubscriptionView | None:
    """The entitlement gate's reader (§11.4): the account's live subscription."""
    row = live_subscription(conn, account_id)
    if row is None or row["state"] in TERMINAL_STATES:
        return None
    return SubscriptionView(
        state=row["state"], plan_id=row["plan_id"], catalog_version=row["catalog_version"],
        current_period_start=datetime.fromisoformat(row["current_period_start"]),
        current_period_end=datetime.fromisoformat(row["current_period_end"]),
        past_due_since=None if row["past_due_since"] is None else datetime.fromisoformat(row["past_due_since"]),
    )
