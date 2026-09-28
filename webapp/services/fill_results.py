"""Bundle 6D-B fill-result.v1 (spec §15): an immutable, hashed artifact
written once at a run's terminal state. References, hashes, indices,
fingerprints and outcomes only (no cleartext), plus the explicit non-claims."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from product.autonomy_contract import canonical_hash, to_utc_iso
from product.fill_vocab import TERMINAL_RUN_EVENTS
from webapp.persistence import fill as f

RESULT_SCHEMA = "fill-result.v1"
NON_CLAIMS = {
    "employer_received_answers": False, "employer_received_documents": False,
    "employer_persisted_application": False, "employer_accepted_application": False,
    "submitted": False, "submission_authorized": False,
    "fit_for_submission_without_6E_revalidation": False,
}


def run_plan_hash(conn, run_id: str) -> str | None:
    """The plan a run revalidates against: the REVALIDATING event's plan_hash."""
    for event in f.run_events(conn, run_id):
        if event["event"] == "REVALIDATING":
            return event["detail"]["plan_hash"]
    return None


def write_result(conn, run_id: str, *, now: datetime) -> dict[str, Any]:
    """Idempotent: a run has at most one result (the first terminal state)."""
    existing = f.get_result(conn, run_id)
    if existing is not None:
        return existing
    run = f.get_run(conn, run_id)
    events = f.run_events(conn, run_id)
    terminal = next((e for e in reversed(events) if e["event"] in TERMINAL_RUN_EVENTS), None)
    if terminal is None:
        raise ValueError(f"run {run_id} is not terminal")
    binding = f.get_grant_binding(conn, run_id)
    plan_hash = binding["plan_hash"] if binding else run_plan_hash(conn, run_id)
    plan = f.get_plan_by_hash(conn, plan_hash) if plan_hash else None
    actions = []
    for event in f.action_events(conn, run_id):
        if event["event"] == "OUTCOME":
            actions.append({"action_index": event["action_index"], "outcome": event["outcome"],
                            "envelope_id": event["envelope_id"], "readback_hash": event["readback_hash"],
                            "document_sha256_verified": event["detail"].get("document_sha256_verified")})
    final = f.latest_observation(conn, run_id)
    quarantine = f.quarantine_events(conn, run_id)
    active = next((e for e in reversed(events) if e["event"] == "QUARANTINE_ACTIVE"), None)
    delta_ids = sorted({d for e in events for d in e["detail"].get("delta_ids", [])})
    result = {
        "schema_version": RESULT_SCHEMA,
        "run": {k: run[k] for k in ("id", "account_id", "application_workspace_id", "handoff_session_id",
                                    "executor_instance_id", "browser_session_id", "execution_tab_id",
                                    "timing_version")},
        "approval_id": binding["approval_id"] if binding else (plan["approval_id"] if plan else None),
        "approval_binding_hash": binding["approval_binding_hash"] if binding else (
            plan["approval_binding_hash"] if plan else None),
        "plan_hash": plan_hash,
        "grant_id": binding["grant_id"] if binding else None,
        "terminal_state": terminal["event"],
        "stop_reason": terminal["reason"],
        "stop_detail": terminal["detail"],
        "actions": sorted(actions, key=lambda a: a["action_index"]),
        "final_structure_fingerprint": final["structure_fingerprint"] if final else None,
        "final_observation_fingerprint": final["observation_fingerprint"] if final else None,
        "ruleset_hash": active["detail"]["ruleset_hash"] if active else None,
        "quarantine_status": quarantine[-1]["phase"] if quarantine else "NONE",
        "delta_ids": delta_ids,
        "started_at": run["created_at"],
        "ended_at": to_utc_iso(now),
        "non_claims": dict(NON_CLAIMS),
    }
    return f.insert_result(conn, fill_run_id=run_id, result=result,
                           result_hash=canonical_hash("fill-result", "v1", result), now=now)
