"""Bundle 6D-B fill-result.v1 (spec §15): an immutable, hashed artifact
written once at a run's terminal state. References, hashes, indices,
fingerprints and outcomes only (no cleartext), plus the explicit non-claims."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from product.autonomy_contract import canonical_hash, to_utc_iso
from product.fill_vocab import TERMINAL_RUN_EVENTS
from webapp.config import Settings
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


# ---- fill status (spec §16.2) ---------------------------------------------------------------

_IN_PROGRESS = frozenset({"OBSERVING", "PLAN_PROPOSED", "REVALIDATING", "QUARANTINE_ACTIVE", "FILLING",
                          "FINAL_VALIDATING"})


def _environment_ready(conn, account_id: str, latest_run: Mapping[str, Any] | None) -> bool:
    paired = conn.execute("SELECT 1 FROM extension_credentials WHERE account_id = ? AND revoked_at IS NULL",
                          (account_id,)).fetchone() is not None
    if latest_run is not None:
        last = f.run_state(conn, latest_run["id"])
        if last and last["reason"] == "PERMISSIONS_MISSING":
            return False
    return paired


def fill_status(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                now: datetime) -> dict[str, Any]:
    """Derived from the latest run, next to the unchanged 6D-A review state.
    stale: the current approval or confirmed plan is not the run's (or the
    filled surface changed after FILLED). ready_to_fill: an effective
    approval, a current supported observation, a confirmed matching plan and
    the environment (a live extension pairing, permissions not missing)."""
    from webapp.services.fill_plans import approval_context, fill_plan_presentation
    ws = application_workspace_id
    runs = [r for r in f.runs_for_application(conn, ws) if r["account_id"] == account_id]
    latest = runs[-1] if runs else None
    approval = approval_context(conn, settings=settings, account_id=account_id, application_workspace_id=ws, now=now)
    observation = f.latest_workspace_observation(conn, ws, "INITIAL")
    view = fill_plan_presentation(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                                  observation_id=observation["id"], now=now) if observation and approval else None
    ready = bool(approval and view and view["state"] == "PLAN_PROPOSED" and view["confirmed"]
                 and _environment_ready(conn, account_id, latest))
    if latest is None:
        return {"status": "NOT_STARTED", "stale": False, "ready_to_fill": ready, "run_id": None, "stop_reason": None}
    last = f.run_state(conn, latest["id"])
    state = last["event"]
    if state in _IN_PROGRESS:
        status = "FILLING"
    elif state == "FILLED_AWAITING_SUBMISSION":
        lease = f.get_lease(conn, latest["id"])
        live = lease is not None and lease["expires_at"] >= to_utc_iso(now)
        status = "FILLED_AWAITING_SUBMISSION" if live else "FILLED_CONTEXT_UNVERIFIED"
    else:
        status = state
    binding = f.get_grant_binding(conn, latest["id"])
    plan_hash = binding["plan_hash"] if binding else run_plan_hash(conn, latest["id"])
    plan = f.get_plan_by_hash(conn, plan_hash) if plan_hash else None
    stale = False
    if plan is not None:
        stale = approval is None or (approval["approval_id"], approval["binding_hash"]) != (
            plan["approval_id"], plan["approval_binding_hash"]) or not f.plan_confirmed(
            conn, plan_hash, plan["approval_id"], plan["approval_binding_hash"])
    if status in ("FILLED_AWAITING_SUBMISSION", "FILLED_CONTEXT_UNVERIFIED") and any(
            d["kind"] == "POST_FILL_CHANGE_OBSERVED" for d in f.detection_events(conn, latest["id"])):
        stale = True
    return {"status": status, "stale": stale, "ready_to_fill": ready, "run_id": latest["id"],
            "stop_reason": last["reason"] if state == "FILL_STOPPED" else None}


APPROVED_WORDING = "Approved: open the employer page to prepare filling"
READY_WORDING = "Ready to fill"


def fill_status_label(status: Mapping[str, Any], *, approval_effective: bool) -> str | None:
    """The one wording of spec §16.2 statuses (list, dossier, fill-plan page)."""
    stale = " (stale: the approval or plan changed since)" if status.get("stale") else ""
    code = status["status"]
    if code == "FILLING":
        return "Filling (the page is quarantined)"
    if code == "FILLED_AWAITING_SUBMISSION":
        return "Filled and quarantined; submission is a later, separate step" + stale
    if code == "FILLED_CONTEXT_UNVERIFIED":
        return "Filled earlier, but the filled page can no longer be verified" + stale
    if status.get("ready_to_fill"):
        return READY_WORDING
    if code == "PLAN_NEEDS_REVIEW":
        return "The fill plan needs your review"
    if code == "UNSUPPORTED_FORM":
        return "This form is not supported for safe filling"
    if code == "FILL_STOPPED":
        return f"Filling stopped ({status.get('stop_reason')})"
    return APPROVED_WORDING if approval_effective else None


def fill_summary(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                 now: datetime) -> dict[str, Any]:
    from webapp.services.fill_plans import approval_context
    status = fill_status(conn, settings=settings, account_id=account_id,
                         application_workspace_id=application_workspace_id, now=now)
    effective = approval_context(conn, settings=settings, account_id=account_id,
                                 application_workspace_id=application_workspace_id, now=now) is not None
    return {**status, "label": fill_status_label(status, approval_effective=effective)}
