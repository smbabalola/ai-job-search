# webapp/persistence/fill.py
"""Bundle 6D-B persistence (spec §15, §19). Append-only evidence; no function
commits (callers own the transaction); current state is derived by seq.

No cleartext at rest (spec I9, D18): every JSON payload written here is
scanned, and a payload carrying a cleartext-bearing key is refused with
CleartextAtRestError. Plans, observations, events and results carry hashes
and references only."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from product.autonomy_contract import canonical_json, to_utc_iso

# Keys that would carry an answer or field value in clear. Hash-named keys
# (value_hash, rendered_value_hash, current_value_hash, value_state) are fine.
CLEARTEXT_KEYS = frozenset({"value", "rendered_value", "display_value", "cleartext", "answer_value",
                            "current_value", "typed_value"})


class CleartextAtRestError(ValueError):
    """A payload destined for storage carries a cleartext value."""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _check_no_cleartext(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in CLEARTEXT_KEYS:
                raise CleartextAtRestError(f"cleartext-bearing key {key!r} at {path}")
            _check_no_cleartext(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _check_no_cleartext(item, f"{path}[{i}]")


def _json(value: Any) -> str:
    _check_no_cleartext(value)
    return canonical_json(value)


def _insert(conn, table: str, values: dict[str, Any]) -> dict[str, Any]:
    cols = ", ".join(values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in values)})",
                       tuple(values.values()))
    return dict(conn.execute(f"SELECT * FROM {table} WHERE seq = ?", (cur.lastrowid,)).fetchone())


def _decode(row, *json_cols: str) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for col in json_cols:
        key = col[:-5] if col.endswith("_json") else col
        out[key] = json.loads(out[col])
    return out


# ---- observations ---------------------------------------------------------------------

def insert_observation(conn, *, account_id: str, application_workspace_id: str, fill_run_id: str | None, phase: str,
                       action_index: int | None, structure_fingerprint: str, observation_fingerprint: str,
                       observation: dict[str, Any], now: datetime) -> dict[str, Any]:
    return _decode(_insert(conn, "fill_observations", {
        "id": _id("fobs"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "fill_run_id": fill_run_id, "phase": phase, "action_index": action_index,
        "structure_fingerprint": structure_fingerprint, "observation_fingerprint": observation_fingerprint,
        "observation_json": _json(observation), "created_at": to_utc_iso(now)}), "observation_json")


def get_observation(conn, observation_id: str) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT * FROM fill_observations WHERE id = ?", (observation_id,)).fetchone(),
                   "observation_json")


def latest_observation(conn, fill_run_id: str, phase: str | None = None) -> dict[str, Any] | None:
    sql, args = "SELECT * FROM fill_observations WHERE fill_run_id = ?", [fill_run_id]
    if phase is not None:
        sql, args = sql + " AND phase = ?", args + [phase]
    return _decode(conn.execute(sql + " ORDER BY seq DESC LIMIT 1", args).fetchone(), "observation_json")


# ---- plans, mapping choices, confirmations ----------------------------------------------

def insert_plan(conn, *, account_id: str, application_workspace_id: str, plan: dict[str, Any], plan_hash: str,
                approval_id: str, approval_binding_hash: str, observation_id: str, now: datetime) -> dict[str, Any]:
    """Idempotent per plan_hash: an identical rebuilt plan returns the stored row."""
    existing = get_plan_by_hash(conn, plan_hash)
    if existing is not None:
        return existing
    return _decode(_insert(conn, "fill_plans", {
        "id": _id("fplan"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "plan_hash": plan_hash, "approval_id": approval_id, "approval_binding_hash": approval_binding_hash,
        "observation_id": observation_id, "plan_json": _json(plan), "created_at": to_utc_iso(now)}), "plan_json")


def get_plan_by_hash(conn, plan_hash: str) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT * FROM fill_plans WHERE plan_hash = ?", (plan_hash,)).fetchone(), "plan_json")


def insert_mapping_choice(conn, *, account_id: str, application_workspace_id: str, observation_id: str,
                          page_field_key: str, field_fingerprint: str, answer_key: str | None, choice: str,
                          actor: str, now: datetime) -> dict[str, Any]:
    return _insert(conn, "fill_plan_mapping_choices", {
        "id": _id("fmap"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "observation_id": observation_id, "page_field_key": page_field_key, "field_fingerprint": field_fingerprint,
        "answer_key": answer_key, "choice": choice, "actor": actor, "created_at": to_utc_iso(now)})


def current_mapping_choices(conn, observation_id: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT * FROM fill_plan_mapping_choices WHERE observation_id = ? ORDER BY seq",
                            (observation_id,)).fetchall():
        out[row["page_field_key"]] = dict(row)
    return out


def insert_plan_confirmation(conn, *, account_id: str, application_workspace_id: str, plan_hash: str,
                             approval_id: str, approval_binding_hash: str, actor: str, now: datetime) -> dict[str, Any]:
    return _insert(conn, "fill_plan_confirmations", {
        "id": _id("fconf"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "plan_hash": plan_hash, "approval_id": approval_id, "approval_binding_hash": approval_binding_hash,
        "actor": actor, "created_at": to_utc_iso(now)})


def plan_confirmed(conn, plan_hash: str, approval_id: str, approval_binding_hash: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM fill_plan_confirmations WHERE plan_hash = ? AND approval_id = ? AND approval_binding_hash = ?",
        (plan_hash, approval_id, approval_binding_hash)).fetchone() is not None


# ---- R7 classification ------------------------------------------------------------------

def insert_classification_proposal(conn, *, account_id: str, application_workspace_id: str, delta_id: str,
                                   subject: str, basis: str, now: datetime) -> dict[str, Any]:
    return _insert(conn, "delta_classification_proposals", {
        "id": _id("fprop"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "delta_id": delta_id, "subject": subject, "basis": basis, "created_at": to_utc_iso(now)})


def latest_proposal(conn, delta_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM delta_classification_proposals WHERE delta_id = ? ORDER BY seq DESC LIMIT 1",
                       (delta_id,)).fetchone()
    return dict(row) if row else None


def insert_classification_confirmation(conn, *, account_id: str, application_workspace_id: str, delta_id: str,
                                       proposal_id: str, subject: str, successor_delta_id: str, actor: str,
                                       now: datetime) -> dict[str, Any]:
    return _insert(conn, "delta_classification_confirmations", {
        "id": _id("fclass"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "delta_id": delta_id, "proposal_id": proposal_id, "subject": subject,
        "successor_delta_id": successor_delta_id, "actor": actor, "created_at": to_utc_iso(now)})


def classification_confirmation(conn, delta_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM delta_classification_confirmations WHERE delta_id = ?", (delta_id,)).fetchone()
    return dict(row) if row else None


# ---- runs and run events ----------------------------------------------------------------

def insert_run(conn, *, account_id: str, application_workspace_id: str, handoff_session_id: str,
               executor_instance_id: str, browser_session_id: str, execution_tab_id: int, timing_version: str,
               now: datetime) -> dict[str, Any]:
    return _insert(conn, "fill_runs", {
        "id": _id("frun"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "handoff_session_id": handoff_session_id, "executor_instance_id": executor_instance_id,
        "browser_session_id": browser_session_id, "execution_tab_id": execution_tab_id,
        "timing_version": timing_version, "created_at": to_utc_iso(now)})


def get_run(conn, fill_run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM fill_runs WHERE id = ?", (fill_run_id,)).fetchone()
    return dict(row) if row else None


def append_run_event(conn, *, fill_run_id: str, event: str, reason: str | None = None,
                     detail: dict[str, Any] | None = None, now: datetime) -> dict[str, Any]:
    return _decode(_insert(conn, "fill_run_events", {
        "id": _id("frev"), "fill_run_id": fill_run_id, "event": event, "reason": reason,
        "detail_json": _json(detail or {}), "created_at": to_utc_iso(now)}), "detail_json")


def run_events(conn, fill_run_id: str) -> list[dict[str, Any]]:
    return [_decode(r, "detail_json") for r in conn.execute(
        "SELECT * FROM fill_run_events WHERE fill_run_id = ? ORDER BY seq", (fill_run_id,)).fetchall()]


def run_state(conn, fill_run_id: str) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT * FROM fill_run_events WHERE fill_run_id = ? ORDER BY seq DESC LIMIT 1",
                                (fill_run_id,)).fetchone(), "detail_json")


def runs_for_application(conn, application_workspace_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM fill_runs WHERE application_workspace_id = ? ORDER BY seq",
                                          (application_workspace_id,)).fetchall()]


# ---- grant binding ----------------------------------------------------------------------

def insert_grant_binding(conn, *, fill_run_id: str, grant_id: str, approval_id: str, approval_binding_hash: str,
                         plan_hash: str, structure_fingerprint: str, observation_fingerprint: str, ruleset_hash: str,
                         now: datetime) -> dict[str, Any]:
    return _insert(conn, "fill_run_grant_bindings", {
        "id": _id("fgb"), "fill_run_id": fill_run_id, "grant_id": grant_id, "approval_id": approval_id,
        "approval_binding_hash": approval_binding_hash, "plan_hash": plan_hash,
        "structure_fingerprint": structure_fingerprint, "observation_fingerprint": observation_fingerprint,
        "ruleset_hash": ruleset_hash, "created_at": to_utc_iso(now)})


def get_grant_binding(conn, fill_run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM fill_run_grant_bindings WHERE fill_run_id = ?", (fill_run_id,)).fetchone()
    return dict(row) if row else None


# ---- per-action events --------------------------------------------------------------------

def append_action_event(conn, *, fill_run_id: str, action_index: int, event: str, outcome: str | None = None,
                        envelope_id: str | None = None, readback_hash: str | None = None,
                        detail: dict[str, Any] | None = None, now: datetime) -> dict[str, Any]:
    return _decode(_insert(conn, "fill_action_events", {
        "id": _id("fact"), "fill_run_id": fill_run_id, "action_index": action_index, "event": event,
        "outcome": outcome, "envelope_id": envelope_id, "readback_hash": readback_hash,
        "detail_json": _json(detail or {}), "created_at": to_utc_iso(now)}), "detail_json")


def action_events(conn, fill_run_id: str, action_index: int | None = None) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM fill_action_events WHERE fill_run_id = ?", [fill_run_id]
    if action_index is not None:
        sql, args = sql + " AND action_index = ?", args + [action_index]
    return [_decode(r, "detail_json") for r in conn.execute(sql + " ORDER BY seq", args).fetchall()]


def action_outcome(conn, fill_run_id: str, action_index: int) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT * FROM fill_action_events WHERE fill_run_id = ? AND action_index = ? "
                                "AND event = 'OUTCOME'", (fill_run_id, action_index)).fetchone(), "detail_json")


def issued_envelope(conn, fill_run_id: str, action_index: int) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT * FROM fill_action_events WHERE fill_run_id = ? AND action_index = ? "
                                "AND event = 'ENVELOPE_ISSUED'", (fill_run_id, action_index)).fetchone(),
                   "detail_json")


# ---- quarantine, detections, results ------------------------------------------------------

def append_quarantine_event(conn, *, fill_run_id: str, phase: str, ruleset_hash: str | None,
                            action_index: int | None = None, now: datetime) -> dict[str, Any]:
    return _insert(conn, "fill_quarantine_events", {
        "id": _id("fq"), "fill_run_id": fill_run_id, "phase": phase, "ruleset_hash": ruleset_hash,
        "action_index": action_index, "created_at": to_utc_iso(now)})


def quarantine_events(conn, fill_run_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM fill_quarantine_events WHERE fill_run_id = ? ORDER BY seq",
                                          (fill_run_id,)).fetchall()]


def append_detection_event(conn, *, fill_run_id: str, kind: str, detail: dict[str, Any] | None = None,
                           now: datetime) -> dict[str, Any]:
    return _decode(_insert(conn, "fill_detection_events", {
        "id": _id("fdet"), "fill_run_id": fill_run_id, "kind": kind, "detail_json": _json(detail or {}),
        "created_at": to_utc_iso(now)}), "detail_json")


def detection_events(conn, fill_run_id: str) -> list[dict[str, Any]]:
    return [_decode(r, "detail_json") for r in conn.execute(
        "SELECT * FROM fill_detection_events WHERE fill_run_id = ? ORDER BY seq", (fill_run_id,)).fetchall()]


def insert_result(conn, *, fill_run_id: str, result: dict[str, Any], result_hash: str,
                  now: datetime) -> dict[str, Any]:
    return _decode(_insert(conn, "fill_results", {
        "id": _id("fres"), "fill_run_id": fill_run_id, "result_hash": result_hash, "result_json": _json(result),
        "created_at": to_utc_iso(now)}), "result_json")


def get_result(conn, fill_run_id: str) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT * FROM fill_results WHERE fill_run_id = ?", (fill_run_id,)).fetchone(),
                   "result_json")


# ---- operational (mutable): concurrency keys and leases -----------------------------------

def claim_active_run(conn, *, application_workspace_id: str, fill_run_id: str, context_key: str) -> None:
    """Raises sqlite3.IntegrityError if the application or the execution
    context already has a non-terminal run."""
    conn.execute("INSERT INTO active_fill_runs (application_workspace_id, fill_run_id, context_key) VALUES (?, ?, ?)",
                 (application_workspace_id, fill_run_id, context_key))


def release_active_run(conn, fill_run_id: str) -> None:
    conn.execute("DELETE FROM active_fill_runs WHERE fill_run_id = ?", (fill_run_id,))


def active_run_for(conn, application_workspace_id: str) -> str | None:
    row = conn.execute("SELECT fill_run_id FROM active_fill_runs WHERE application_workspace_id = ?",
                       (application_workspace_id,)).fetchone()
    return row[0] if row else None


def touch_lease(conn, *, fill_run_id: str, expires_at: datetime, now: datetime) -> None:
    conn.execute("INSERT INTO fill_run_leases (fill_run_id, expires_at, updated_at) VALUES (?, ?, ?) "
                 "ON CONFLICT(fill_run_id) DO UPDATE SET expires_at = excluded.expires_at, "
                 "updated_at = excluded.updated_at", (fill_run_id, to_utc_iso(expires_at), to_utc_iso(now)))


def get_lease(conn, fill_run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM fill_run_leases WHERE fill_run_id = ?", (fill_run_id,)).fetchone()
    return dict(row) if row else None


def delete_lease(conn, fill_run_id: str) -> None:
    conn.execute("DELETE FROM fill_run_leases WHERE fill_run_id = ?", (fill_run_id,))


def expired_leases(conn, now: datetime) -> list[str]:
    """Runs whose lease expired strictly before `now` (to_utc_iso is fixed-width, so strings compare)."""
    return [r[0] for r in conn.execute("SELECT fill_run_id FROM fill_run_leases WHERE expires_at < ? ORDER BY expires_at",
                                       (to_utc_iso(now),)).fetchall()]
