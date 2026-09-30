"""Append-only audit log writer (Bundle 7 spec §20.1). No commit.

The account id is stored as data, not a foreign key: audit rows outlive the
account (retention class SECURITY_AUDIT) and some events have no account."""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime
from typing import Any

from product.audit_actions import ACTOR_TYPES, AUDIT_ACTIONS
from webapp.observability import redact
from webapp.persistence import dbapi

LOCAL_DEVELOPMENT_SECRET = "local-development-only-not-a-secret"


def ip_hash(secret: str | None, ip: str | None) -> str | None:
    if not ip:
        return None
    return hmac.new((secret or LOCAL_DEVELOPMENT_SECRET).encode(), f"ip:{ip}".encode(),
                    hashlib.sha256).hexdigest()[:32]


def audit(conn: dbapi.Connection, *, actor_type: str, actor_id: str | None, account_id: str | None, action: str,
          now: datetime, target_type: str | None = None, target_id: str | None = None,
          request_id: str | None = None, ip: str | None = None, detail: dict[str, Any] | None = None,
          secret: str | None = None) -> None:
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unknown audit action {action!r}")
    if actor_type not in ACTOR_TYPES:
        raise ValueError(f"unknown audit actor type {actor_type!r}")
    conn.execute(
        "INSERT INTO audit_log (id, occurred_at, actor_type, actor_id, account_id, action, target_type, target_id, "
        "request_id, ip_hash, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"aud_{uuid.uuid4().hex[:20]}", now.isoformat(), actor_type, actor_id, account_id, action, target_type,
         target_id, request_id, ip_hash(secret, ip), json.dumps(redact(detail or {}), sort_keys=True, default=str)),
    )
