"""User notifications (Bundle 7 spec §17).

Task 11 stub: ``notify`` records in memory; Task 20 replaces it with the
notification log and email fan-out, keeping this signature."""
from __future__ import annotations

from datetime import datetime
from typing import Any

_RECORDED: list[dict[str, Any]] = []


def notify(conn, *, account_id: str, kind: str, subject_type: str, subject_id: str, dedupe_key: str,
           detail: dict[str, Any], now: datetime) -> bool:
    if any(n["account_id"] == account_id and n["dedupe_key"] == dedupe_key for n in _RECORDED):
        return False
    _RECORDED.append({"account_id": account_id, "kind": kind, "subject_type": subject_type, "subject_id": subject_id,
                      "dedupe_key": dedupe_key, "detail": detail, "created_at": now.isoformat()})
    return True


def recorded_for_tests() -> list[dict[str, Any]]:
    return list(_RECORDED)


def reset_for_tests() -> None:
    _RECORDED.clear()
