"""Server-side web sessions (Bundle 7 spec A4).

The cookie carries an opaque random id; only its SHA-256 is stored. Customer
sessions idle out after 7 days and end after 30; staff sessions after 30
minutes and 8 hours. The CSRF token is derived from the raw session id with an
HMAC under the deployment secret, so it never needs storing and dies with the
session (spec A5)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from webapp.persistence import dbapi
from webapp.persistence.identity import token_hash

LIFETIMES = {
    "CUSTOMER": (timedelta(days=7), timedelta(days=30)),
    "STAFF": (timedelta(minutes=30), timedelta(hours=8)),
}
TOUCH_INTERVAL = timedelta(seconds=60)
LOCAL_DEVELOPMENT_SECRET = "local-development-only-not-a-secret"  # local mode is loopback-only


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _ip_hash(secret: str, ip: str | None) -> str | None:
    if not ip:
        return None
    return hmac.new(secret.encode(), f"ip:{ip}".encode(), hashlib.sha256).hexdigest()[:32]


def _user_agent_summary(user_agent: str | None) -> str | None:
    return user_agent[:120] if user_agent else None


class SessionService:
    def __init__(self, secret_key: str | None) -> None:
        self.secret = secret_key or LOCAL_DEVELOPMENT_SECRET

    def csrf_token(self, raw_session_id: str) -> str:
        return hmac.new(self.secret.encode(), f"csrf:{raw_session_id}".encode(), hashlib.sha256).hexdigest()

    def create(self, conn: dbapi.Connection, *, user_id: str, kind: Literal["CUSTOMER", "STAFF"], now: datetime,
               ip: str | None, user_agent: str | None) -> tuple[str, str]:
        idle, absolute = LIFETIMES[kind]
        raw = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO web_sessions (id_hash, user_id, kind, created_at, last_seen_at, idle_expires_at, "
            "absolute_expires_at, ip_hash, user_agent_summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (token_hash(raw), user_id, kind, _iso(now), _iso(now), _iso(now + idle), _iso(now + absolute),
             _ip_hash(self.secret, ip), _user_agent_summary(user_agent)),
        )
        return raw, self.csrf_token(raw)

    def resolve(self, conn: dbapi.Connection, raw_session_id: str | None, *, now: datetime) -> dict[str, Any] | None:
        if not raw_session_id:
            return None
        row = conn.execute("SELECT * FROM web_sessions WHERE id_hash = ?", (token_hash(raw_session_id),)).fetchone()
        stamp = _iso(now)
        if row is None or row["revoked_at"] is not None or stamp > row["idle_expires_at"] \
                or stamp > row["absolute_expires_at"]:
            return None
        session = dict(row)
        if now - datetime.fromisoformat(row["last_seen_at"]) >= TOUCH_INTERVAL:
            idle, _ = LIFETIMES[row["kind"]]
            idle_expires = min(_iso(now + idle), row["absolute_expires_at"])
            caller_transaction = conn.in_transaction
            conn.execute("UPDATE web_sessions SET last_seen_at = ?, idle_expires_at = ? WHERE id_hash = ?",
                         (stamp, idle_expires, row["id_hash"]))
            if not caller_transaction:
                conn.commit()  # the touch is independent of the request's own work
            session.update(last_seen_at=stamp, idle_expires_at=idle_expires)
        return session

    def revoke(self, conn: dbapi.Connection, raw_session_id: str, *, reason: str) -> None:
        conn.execute(
            "UPDATE web_sessions SET revoked_at = COALESCE(revoked_at, ?), revoke_reason = COALESCE(revoke_reason, ?) "
            "WHERE id_hash = ?",
            (_iso(datetime.now(timezone.utc)), reason, token_hash(raw_session_id)),
        )

    def revoke_all_for_user(self, conn: dbapi.Connection, user_id: str, *, reason: str,
                            except_session_id: str | None = None) -> int:
        keep = token_hash(except_session_id) if except_session_id else ""
        cursor = conn.execute(
            "UPDATE web_sessions SET revoked_at = ?, revoke_reason = ? "
            "WHERE user_id = ? AND revoked_at IS NULL AND id_hash <> ?",
            (_iso(datetime.now(timezone.utc)), reason, user_id, keep),
        )
        return cursor.rowcount
