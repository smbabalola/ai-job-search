"""Outbound communications (Bundle 7 spec §18).

Task 8 stub: ``enqueue`` records messages in memory so the auth flows can be
built and tested; Task 19 replaces it with the transactional outbox, keeping
this signature."""
from __future__ import annotations

from datetime import datetime
from typing import Any

_OUTBOX: list[dict[str, Any]] = []


def enqueue(conn, *, category: str, template_id: str, to_address: str, payload: dict[str, Any],
            account_id: str | None = None, user_id: str | None = None, idempotency_key: str,
            locale: str = "en", now: datetime) -> str | None:
    if any(m["idempotency_key"] == idempotency_key for m in _OUTBOX):
        return None
    _OUTBOX.append({"category": category, "template_id": template_id, "to_address": to_address,
                    "payload": payload, "account_id": account_id, "user_id": user_id,
                    "idempotency_key": idempotency_key, "locale": locale, "created_at": now.isoformat()})
    return idempotency_key


def outbox_for_tests() -> list[dict[str, Any]]:
    return list(_OUTBOX)


def reset_outbox_for_tests() -> None:
    _OUTBOX.clear()
