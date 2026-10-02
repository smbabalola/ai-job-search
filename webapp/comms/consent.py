"""Communication consent records (Bundle 7 spec §18.1): append-only; the
latest row per (user, channel, purpose) is the current state; no row means
not granted. Service mail never depends on consent."""
from __future__ import annotations

import uuid
from datetime import datetime

from webapp.persistence import dbapi
from webapp.persistence.audit import audit

CHANNELS = ("EMAIL", "SMS", "WHATSAPP")
PURPOSES = ("MARKETING",)
STATES = ("GRANTED", "WITHDRAWN")


def record_consent(conn: dbapi.Connection, *, account_id: str, user_id: str, channel: str, purpose: str, state: str,
                   wording_version: str, source: str, now: datetime) -> None:
    """No commit."""
    if channel not in CHANNELS or purpose not in PURPOSES or state not in STATES:
        raise ValueError("unknown consent channel, purpose or state")
    conn.execute("INSERT INTO communication_consents (id, account_id, user_id, channel, purpose, state, "
                 "wording_version, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (f"cons_{uuid.uuid4().hex[:20]}", account_id, user_id, channel, purpose, state, wording_version,
                  source, now.isoformat(timespec="microseconds")))
    audit(conn, actor_type="USER", actor_id=user_id, account_id=account_id, action="CONSENT_CHANGED",
          target_type="user", target_id=user_id, now=now,
          detail={"channel": channel, "purpose": purpose, "state": state, "wording_version": wording_version})


def current_consent(conn: dbapi.Connection, *, user_id: str, channel: str, purpose: str) -> bool:
    row = conn.execute("SELECT state FROM communication_consents WHERE user_id = ? AND channel = ? AND purpose = ? "
                       "ORDER BY seq DESC LIMIT 1", (user_id, channel, purpose)).fetchone()
    return row is not None and row[0] == "GRANTED"
