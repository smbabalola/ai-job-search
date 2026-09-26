"""Autonomy controls (6B spec §4.2, §11.4, §15.2). Every safety-relevant
control is an append-only record. Releasing a halt never resumes: only
resume_all, with the sentinel absent, makes applications eligible for fresh
evaluation.

Deviations from the task-12 brief:
  1. _in_transaction refuses to run while the connection already has an open
     transaction, instead of failing on BEGIN or committing the caller's
     half-done work. Callers holding a transaction use the *_in_transaction
     variants.
  2. resume_all checks the sentinel inside its BEGIN IMMEDIATE transaction
     (spec §15.2: checked synchronously wherever the kill switch is read),
     not before it.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from product.autonomy_contract import Capability
from product.standing_policy import default_policy_document
from webapp.persistence.autonomy_authority import (
    current_capability, current_policy, kill_switch_state, record_authorization,
    record_control_event, record_kill_switch, save_policy_version,
)
from webapp.persistence.autonomy_ledger import revoke_issued_grants, wake_queue_items


class AutonomyHalted(Exception):
    pass


def sentinel_present(path: Path | str) -> bool:
    return Path(path).exists()


def _in_transaction(conn: sqlite3.Connection, work):
    if conn.in_transaction:
        raise RuntimeError("autonomy control called inside an open transaction; use the *_in_transaction variant")
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = work()
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def engage_kill_switch_in_transaction(conn, *, account_id: str, actor: str, reason: str,
                                      now: datetime) -> dict[str, Any]:
    """Engage inside a transaction the caller already holds (used by the
    pre-click transaction when it observes the sentinel)."""
    recorded = False
    if not kill_switch_state(conn, account_id)["engaged"]:
        record_kill_switch(conn, account_id=account_id, engaged=True, reason=reason, actor=actor, now=now, commit=False)
        recorded = True
    revoked = revoke_issued_grants(conn, account_id=account_id, reason="kill_switch", now=now)
    return {"engaged": True, "revoked": revoked, "recorded": recorded}


def engage_kill_switch(conn, *, account_id: str, actor: str, reason: str, now: datetime) -> dict[str, Any]:
    return _in_transaction(conn, lambda: engage_kill_switch_in_transaction(
        conn, account_id=account_id, actor=actor, reason=reason, now=now))


def observe_sentinel(conn, *, account_id: str, sentinel_path: Path, now: datetime) -> bool:
    present = sentinel_present(sentinel_path)
    if present and not kill_switch_state(conn, account_id)["engaged"]:
        engage_kill_switch(conn, account_id=account_id, actor="sentinel",
                           reason=f"sentinel file present: {sentinel_path}", now=now)
    return present


def release_kill_switch(conn, *, account_id: str, actor: str, reason: str, now: datetime) -> dict[str, Any]:
    def work():
        if not kill_switch_state(conn, account_id)["engaged"]:
            return {"recorded": False}
        record_kill_switch(conn, account_id=account_id, engaged=False, reason=reason, actor=actor, now=now, commit=False)
        return {"recorded": True}
    return _in_transaction(conn, work)


def resume_all(conn, *, account_id: str, actor: str, reason: str, now: datetime, sentinel_path: Path) -> dict[str, Any]:
    def work():
        if sentinel_present(sentinel_path):
            raise AutonomyHalted(f"remove the sentinel file first: {sentinel_path}")
        state = kill_switch_state(conn, account_id)
        if state["engaged"]:
            record_kill_switch(conn, account_id=account_id, engaged=False, reason=reason, actor=actor, now=now, commit=False)
        event = record_control_event(
            conn, account_id=account_id, scope_type="ACCOUNT", scope_id=account_id, action="RESUME_ALL",
            actor=actor, reason=reason, now=now, kill_switch_seq_acknowledged=state["latest_engage_seq"], commit=False,
        )
        woken = wake_queue_items(conn, account_id=account_id, now=now)
        return {"event_id": event["id"], "woken": woken}
    return _in_transaction(conn, work)


def _control(conn, action: str, *, account_id, scope_type, scope_id, actor, reason, now) -> dict[str, Any]:
    def work():
        event = record_control_event(conn, account_id=account_id, scope_type=scope_type, scope_id=scope_id,
                                     action=action, actor=actor, reason=reason, now=now, commit=False)
        if scope_type == "APPLICATION":
            conn.execute("UPDATE autonomy_queue_items SET paused = ? WHERE application_workspace_id = ?",
                         (1 if action == "PAUSE" else 0, scope_id))
        return event
    return _in_transaction(conn, work)


def pause(conn, **kwargs) -> dict[str, Any]:
    return _control(conn, "PAUSE", **kwargs)


def resume(conn, **kwargs) -> dict[str, Any]:
    return _control(conn, "RESUME", **kwargs)


def set_capability(conn, *, account_id: str, scope_type: str, scope_id: str, capability: Capability,
                   actor: str, now: datetime) -> dict[str, Any]:
    return record_authorization(conn, account_id=account_id, scope_type=scope_type, scope_id=scope_id,
                                capability=capability, set_by=actor, now=now)


def enable_autonomous_preparation(conn, *, account_id: str, actor: str, timezone: str, now: datetime) -> dict[str, Any]:
    """The explicit enabling act (spec §4.2): writes ACCOUNT_MAX and
    DEFAULT_WORKSPACE_CEILING = PREPARE where they are absent or lower, and an
    initial standing policy if none exists. Never lowers an existing grant."""
    def work():
        written = []
        for scope_type in ("ACCOUNT_MAX", "DEFAULT_WORKSPACE_CEILING"):
            current = current_capability(conn, account_id=account_id, scope_type=scope_type, scope_id=account_id)
            if current is None or current < Capability.PREPARE:
                record_authorization(conn, account_id=account_id, scope_type=scope_type, scope_id=account_id,
                                     capability=Capability.PREPARE, set_by=actor, now=now, commit=False)
                written.append(scope_type)
        if current_policy(conn, account_id) is None:
            save_policy_version(conn, account_id=account_id, doc=default_policy_document(timezone),
                                created_by=actor, now=now, commit=False)
            written.append("standing_policy")
        return {"written": written}
    return _in_transaction(conn, work)
