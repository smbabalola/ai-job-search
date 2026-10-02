"""Extension credentials for tests (Bundle 7 spec §9): pair a device directly
through the service and hand back its bearer access token."""
from __future__ import annotations

from datetime import datetime, timezone

from webapp.services import extension_auth


class SessionCredential(str):
    """A handoff session token that also carries the bearer token of a device
    paired to the same account (session routes need both since Bundle 7)."""

    bearer: str

    def __new__(cls, session_token: str, bearer: str):
        value = super().__new__(cls, session_token)
        value.bearer = bearer
        return value

    def headers(self) -> dict[str, str]:
        return {"X-Handoff-Session-Token": str(self), "Authorization": f"Bearer {self.bearer}"}


def device_bearer(conn, account_id: str, user_id: str | None = None, *, secret: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    _, code = extension_auth.create_pairing_code(conn, account_id=account_id, user_id=user_id, now=now)
    result = extension_auth.pair_device(conn, raw_code=code, device_label="test device", now=now, secret=secret)
    conn.commit()
    return result.access_token


def session_credential(conn, account_id: str, session_token: str) -> SessionCredential:
    return SessionCredential(session_token, device_bearer(conn, account_id))
