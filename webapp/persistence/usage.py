"""Usage reservation rows (Bundle 7 spec §13.1). A window's usage is the sum of
its RESERVED and CONSUMED reservations; nothing here decides a limit (that is
``webapp.services.usage``)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from webapp.persistence import dbapi

LIVE_STATUSES = ("RESERVED", "CONSUMED")


def ts(moment: datetime) -> str:
    # fixed width, so stored timestamps compare correctly as text
    return moment.isoformat(timespec="microseconds")


def _one(conn: dbapi.Connection, sql: str, params: tuple) -> dict[str, Any] | None:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else dict(row)


def get(conn: dbapi.Connection, reservation_id: str) -> dict[str, Any] | None:
    return _one(conn, "SELECT * FROM usage_reservations WHERE id = ?", (reservation_id,))


def live_by_key(conn: dbapi.Connection, idempotency_key: str) -> dict[str, Any] | None:
    return _one(conn, "SELECT * FROM usage_reservations WHERE idempotency_key = ? AND status IN ('RESERVED', 'CONSUMED')",
                (idempotency_key,))


def used(conn: dbapi.Connection, *, account_id: str, allowance: str, window_key: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM usage_reservations WHERE account_id = ? AND allowance = ? "
        "AND window_key = ? AND status IN ('RESERVED', 'CONSUMED')",
        (account_id, allowance, window_key)).fetchone()
    return int(row[0])


def insert(conn: dbapi.Connection, *, account_id: str, allowance: str, amount: int, subject_type: str,
           subject_id: str, idempotency_key: str, window_key: str, now: datetime,
           expires_at: datetime) -> dict[str, Any]:
    reservation_id = f"ures_{uuid.uuid4().hex[:20]}"
    conn.execute(
        "INSERT INTO usage_reservations (id, account_id, allowance, amount, subject_type, subject_id, "
        "idempotency_key, window_key, status, reserved_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?)",
        (reservation_id, account_id, allowance, amount, subject_type, subject_id, idempotency_key, window_key,
         ts(now), ts(expires_at)))
    return get(conn, reservation_id)  # type: ignore[return-value]


def settle(conn: dbapi.Connection, reservation_id: str, *, status: str, now: datetime,
           settlement_ref: str | None = None) -> bool:
    """RESERVED → ``status``. False when the row was not RESERVED (already
    settled, or released by the sweeper)."""
    cursor = conn.execute(
        "UPDATE usage_reservations SET status = ?, settled_at = ?, settlement_ref = ? WHERE id = ? AND status = 'RESERVED'",
        (status, ts(now), settlement_ref, reservation_id))
    return cursor.rowcount == 1


def release_expired(conn: dbapi.Connection, *, account_id: str, now: datetime) -> int:
    cursor = conn.execute(
        "UPDATE usage_reservations SET status = 'RELEASED', settled_at = ?, settlement_ref = 'expired' "
        "WHERE account_id = ? AND status = 'RESERVED' AND expires_at <= ?",
        (ts(now), account_id, ts(now)))
    return cursor.rowcount


def accounts_with_expired(conn: dbapi.Connection, *, now: datetime) -> list[str]:
    rows = conn.execute("SELECT DISTINCT account_id FROM usage_reservations WHERE status = 'RESERVED' "
                        "AND expires_at <= ? ORDER BY account_id", (ts(now),)).fetchall()
    return [row[0] for row in rows]


def claim_action(conn: dbapi.Connection, *, account_id: str, action_key: str, now: datetime,
                 lease_expires_at: datetime, arrived_at: datetime) -> str | None:
    """Under the account lock: mark a RUNNING claim past its lease ABANDONED,
    then take a new RUNNING claim. None while one is live, or when one
    succeeded after ``arrived_at`` (the request arrived while it ran and only
    got the lock afterwards: it is the same logical action, not a rerun)."""
    conn.execute("UPDATE metered_actions SET status = 'ABANDONED', finished_at = ? WHERE account_id = ? "
                 "AND action_key = ? AND status = 'RUNNING' AND lease_expires_at <= ?",
                 (ts(now), account_id, action_key, ts(now)))
    if _one(conn, "SELECT id FROM metered_actions WHERE account_id = ? AND action_key = ? AND status = 'RUNNING'",
            (account_id, action_key)) is not None:
        return None
    if _one(conn, "SELECT id FROM metered_actions WHERE account_id = ? AND action_key = ? AND status = 'SUCCEEDED' "
            "AND finished_at > ?", (account_id, action_key, ts(arrived_at))) is not None:
        return None
    claim_id = f"mact_{uuid.uuid4().hex[:20]}"
    conn.execute("INSERT INTO metered_actions (id, account_id, action_key, status, started_at, lease_expires_at) "
                 "VALUES (?, ?, ?, 'RUNNING', ?, ?)", (claim_id, account_id, action_key, ts(now), ts(lease_expires_at)))
    return claim_id


def finish_action(conn: dbapi.Connection, claim_id: str, *, status: str, now: datetime) -> bool:
    """RUNNING → ``status``. False when the claim was already taken over."""
    cursor = conn.execute("UPDATE metered_actions SET status = ?, finished_at = ? WHERE id = ? AND status = 'RUNNING'",
                          (status, ts(now), claim_id))
    return cursor.rowcount == 1


def storage_bytes(conn: dbapi.Connection, account_id: str) -> int:
    row = conn.execute("SELECT COALESCE(SUM(byte_length), 0) FROM application_document_versions WHERE account_id = ?",
                       (account_id,)).fetchone()
    return int(row[0])
