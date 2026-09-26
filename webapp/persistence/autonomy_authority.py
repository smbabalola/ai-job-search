"""Autonomy authority records (6B spec §4, §11.4, §15): explicit user
authorizations, kill switch, pause/resume control events, standing-policy
versions and autonomous runs. All append-only; "current" is always by seq."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from product.autonomy_contract import Capability, canonical_json, to_utc_iso
from product.standing_policy import policy_hash, validate_standing_policy


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, Any], commit: bool) -> dict[str, Any]:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    row = conn.execute(f"SELECT * FROM {table} WHERE seq = ?", (cur.lastrowid,)).fetchone()
    if commit:
        conn.commit()
    return dict(row)


def record_authorization(conn, *, account_id: str, scope_type: str, scope_id: str,
                         capability: Capability, set_by: str, now: datetime, commit: bool = True) -> dict[str, Any]:
    return _insert(conn, "autonomy_authorizations", {
        "id": _id("auth"), "account_id": account_id, "scope_type": scope_type, "scope_id": scope_id,
        "capability": Capability(capability).name, "set_by": set_by, "created_at": to_utc_iso(now),
    }, commit)


def current_capability(conn, *, account_id: str, scope_type: str, scope_id: str) -> Capability | None:
    row = conn.execute(
        "SELECT capability FROM autonomy_authorizations "
        "WHERE account_id = ? AND scope_type = ? AND scope_id = ? ORDER BY seq DESC LIMIT 1",
        (account_id, scope_type, scope_id),
    ).fetchone()
    return Capability[row["capability"]] if row else None


def resolve_authority(conn, *, account_id: str, search_workspace_id: str | None) -> tuple[Capability, Capability]:
    account_max = current_capability(conn, account_id=account_id, scope_type="ACCOUNT_MAX", scope_id=account_id)
    ceiling = None
    if search_workspace_id is not None:
        ceiling = current_capability(conn, account_id=account_id, scope_type="WORKSPACE_CEILING",
                                     scope_id=search_workspace_id)
    if ceiling is None:
        ceiling = current_capability(conn, account_id=account_id, scope_type="DEFAULT_WORKSPACE_CEILING",
                                     scope_id=account_id)
    return (account_max or Capability.NONE, ceiling or Capability.NONE)


def record_kill_switch(conn, *, account_id: str, engaged: bool, reason: str, actor: str,
                       now: datetime, commit: bool = True) -> dict[str, Any]:
    return _insert(conn, "autonomy_kill_switch", {
        "id": _id("kill"), "account_id": account_id, "engaged": 1 if engaged else 0,
        "reason": reason, "actor": actor, "created_at": to_utc_iso(now),
    }, commit)


def kill_switch_state(conn, account_id: str) -> dict[str, Any]:
    latest = conn.execute(
        "SELECT engaged FROM autonomy_kill_switch WHERE account_id = ? ORDER BY seq DESC LIMIT 1", (account_id,),
    ).fetchone()
    engage = conn.execute(
        "SELECT MAX(seq) AS s FROM autonomy_kill_switch WHERE account_id = ? AND engaged = 1", (account_id,),
    ).fetchone()["s"]
    acked = conn.execute(
        "SELECT MAX(kill_switch_seq_acknowledged) AS s FROM autonomy_control_events "
        "WHERE account_id = ? AND action = 'RESUME_ALL'", (account_id,),
    ).fetchone()["s"]
    engaged = bool(latest and latest["engaged"])
    halted = engaged or (engage is not None and (acked is None or engage > acked))
    return {"engaged": engaged, "latest_engage_seq": engage, "resume_acknowledged_seq": acked, "halted": halted}


def record_control_event(conn, *, account_id: str, scope_type: str, scope_id: str, action: str, actor: str,
                         reason: str, now: datetime, kill_switch_seq_acknowledged: int | None = None,
                         commit: bool = True) -> dict[str, Any]:
    return _insert(conn, "autonomy_control_events", {
        "id": _id("ctl"), "account_id": account_id, "scope_type": scope_type, "scope_id": scope_id,
        "action": action, "kill_switch_seq_acknowledged": kill_switch_seq_acknowledged,
        "actor": actor, "reason": reason, "created_at": to_utc_iso(now),
    }, commit)


def is_paused(conn, *, account_id: str, scope_type: str, scope_id: str) -> bool:
    pause = conn.execute(
        "SELECT MAX(seq) AS s FROM autonomy_control_events WHERE account_id = ? AND scope_type = ? "
        "AND scope_id = ? AND action = 'PAUSE'", (account_id, scope_type, scope_id),
    ).fetchone()["s"]
    if pause is None:
        return False
    resume = conn.execute(
        "SELECT MAX(seq) AS s FROM autonomy_control_events WHERE account_id = ? AND ("
        "(scope_type = ? AND scope_id = ? AND action = 'RESUME') OR action = 'RESUME_ALL')",
        (account_id, scope_type, scope_id),
    ).fetchone()["s"]
    return resume is None or pause > resume


def save_policy_version(conn, *, account_id: str, doc: dict[str, Any], created_by: str,
                        now: datetime, commit: bool = True) -> dict[str, Any]:
    validate_standing_policy(doc)
    return _insert(conn, "standing_policy_versions", {
        "id": _id("pol"), "account_id": account_id, "policy_json": canonical_json(doc),
        "policy_hash": policy_hash(doc), "created_by": created_by, "created_at": to_utc_iso(now),
    }, commit)


def current_policy(conn, account_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM standing_policy_versions WHERE account_id = ? ORDER BY seq DESC LIMIT 1", (account_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "doc": json.loads(row["policy_json"]), "policy_hash": row["policy_hash"], "seq": row["seq"]}


def start_run(conn, *, account_id: str, started_by: str, now: datetime, run_id: str | None = None,
              commit: bool = True) -> dict[str, Any]:
    return _insert(conn, "autonomy_runs", {
        "run_id": run_id or _id("run"), "account_id": account_id, "started_by": started_by,
        "started_at": to_utc_iso(now),
    }, commit)


def end_run(conn, *, run_id: str, end_reason: str, now: datetime, commit: bool = True) -> dict[str, Any]:
    return _insert(conn, "autonomy_run_ends", {
        "run_id": run_id, "end_reason": end_reason, "ended_at": to_utc_iso(now),
    }, commit)


def get_run(conn, run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT r.*, e.ended_at, e.end_reason FROM autonomy_runs r "
        "LEFT JOIN autonomy_run_ends e ON e.run_id = r.run_id WHERE r.run_id = ?", (run_id,),
    ).fetchone()
    return dict(row) if row else None
