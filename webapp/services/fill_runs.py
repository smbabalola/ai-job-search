"""Bundle 6D-B run lifecycle (spec §10.4, §11.1, §11.2, §11.4).

OBSERVING → (INITIAL observation, plan) → REVALIDATING → (REVALIDATION
observation must equal the confirmed plan's) → QUARANTINE_ACTIVE (the TOTAL
ruleset verified by the executor) → FILL grant (one BEGIN IMMEDIATE: approval
effective, confirmation valid, revalidation matching, quarantine active, G4
coverage, then the unchanged public 6B request_grant) → FILLING.

Every refusal is a stop that leaves the page quarantined: no server path
lifts a quarantine (there is no such phase). Leases: an expired lease is
EXECUTOR_LOST (FILLED_CONTEXT_UNVERIFIED after FILLED) and never authorizes
resumption; the reaper only reduces."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from product.autonomy_contract import Capability
from product.fill_certification import CATALOGUE, certified
from product.fill_changes import classify_changes
from product.fill_constants import FILL_TIMING_VERSION, RUN_LEASE_TTL
from product.fill_observation import observation_fingerprint, structure_fingerprint, validate_observation
from product.fill_plan import DeltaSpec, derive_manifest, g4_violations
from product.fill_vocab import (
    FAILURE_OUTCOMES, OBSERVATION_PHASES, QUARANTINE_PHASES, STOP_REASONS, TERMINAL_RUN_EVENTS,
)
from webapp.config import Settings
from webapp.persistence import fill as f
from webapp.persistence.autonomy_answers import latest_answer_confirmation_id
from webapp.persistence.autonomy_ledger import revoke_grant
from webapp.persistence.workspaces import get_workspace
from webapp.services.autonomy import AutonomyPaused, request_grant
from webapp.services.autonomy_context import ApplyTargetObservation
from webapp.services.autonomy_controls import run_immediate
from webapp.services.fill_plans import _open_deltas, approval_context, propose_plan_in_transaction
from webapp.services.fill_results import run_plan_hash, write_result
from webapp.persistence import dbapi


class FillRefused(Exception):
    """A domain refusal (routes: 409 with the reason). Nothing is written."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class MatchResult:
    matched: bool
    stop_reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    deltas: list[DeltaSpec] = field(default_factory=list)


@dataclass
class GrantResult:
    granted: bool
    grant_id: str | None = None
    stop_reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# ---- state helpers ------------------------------------------------------------------------

def current_state(conn, run_id: str) -> str | None:
    """The run's lifecycle state: the latest lifecycle event (detection-only
    events after FILLED are not lifecycle events)."""
    last = f.run_state(conn, run_id)
    return last["event"] if last else None


def is_terminal(state: str | None) -> bool:
    return state in TERMINAL_RUN_EVENTS or state == "FILLED_CONTEXT_UNVERIFIED"


def _run_for(conn, run_id: str) -> dict[str, Any]:
    run = f.get_run(conn, run_id)
    if run is None:
        raise LookupError(run_id)
    return run


def finish_in_transaction(conn, *, run_id: str, event: str, reason: str | None = None,
                          detail: Mapping[str, Any] | None = None, now: datetime) -> dict[str, Any]:
    """Append a terminal event, write fill-result.v1, release the concurrency
    slot. A FILLED run keeps its lease (continuity); any other terminal state
    drops it and revokes an unused FILL grant (reduce-only)."""
    if event not in TERMINAL_RUN_EVENTS:
        raise ValueError(f"not a terminal event: {event}")
    if reason is not None and reason not in STOP_REASONS:
        raise ValueError(f"not a stop reason: {reason}")
    f.append_run_event(conn, fill_run_id=run_id, event=event, reason=reason, detail=dict(detail or {}), now=now)
    result = write_result(conn, run_id, now=now)
    f.release_active_run(conn, run_id)
    if event != "FILLED_AWAITING_SUBMISSION":
        f.delete_lease(conn, run_id)
        binding = f.get_grant_binding(conn, run_id)
        if binding is not None:
            revoke_grant(conn, grant_id=binding["grant_id"], reason=f"fill_run_stopped:{reason or event}", now=now)
    return result


def stop_run_in_transaction(conn, *, run_id: str, reason: str, detail: Mapping[str, Any] | None = None,
                            now: datetime) -> dict[str, Any]:
    return finish_in_transaction(conn, run_id=run_id, event="FILL_STOPPED", reason=reason, detail=detail, now=now)


def stop_run(conn, *, run_id: str, reason: str, detail: Mapping[str, Any] | None = None,
             now: datetime) -> dict[str, Any]:
    def work():
        if is_terminal(current_state(conn, run_id)):
            raise FillRefused("run_not_active")
        return stop_run_in_transaction(conn, run_id=run_id, reason=reason, detail=detail, now=now)
    return run_immediate(conn, work)


def diff_stop_in_transaction(conn, *, run: Mapping[str, Any], plan_row: Mapping[str, Any],
                             observation: Mapping[str, Any], completed: set[int], now: datetime) -> dict[str, Any] | None:
    """Classify a re-observation against the plan (spec §13); open every delta
    together and stop, or stop on a structural/value change. None = clean."""
    plan = plan_row["plan"]
    base = f.get_observation(conn, plan_row["observation_id"])["observation"]
    change = classify_changes(base, observation, plan=plan, completed=completed,
                              entry=certified(plan["adapter_id"], plan["adapter_version"]))
    if change.stop_reason is None:
        if structure_fingerprint(observation) != plan["structure_fingerprint"]:
            change.stop_reason, change.detail = "STRUCTURE_CHANGED", {"changes": ["structure_fingerprint"]}
        else:
            return None
    detail = dict(change.detail)
    if change.deltas:
        detail["delta_ids"] = _open_deltas(conn, account_id=run["account_id"], ws=run["application_workspace_id"],
                                           specs=change.deltas, source=f"FILL_RUN:{run['id']}", now=now)
    stop_run_in_transaction(conn, run_id=run["id"], reason=change.stop_reason, detail=detail, now=now)
    return {"state": "FILL_STOPPED", "reason": change.stop_reason, **detail}


def completed_actions(conn, run_id: str) -> set[int]:
    return {e["action_index"] for e in f.action_events(conn, run_id)
            if e["event"] == "OUTCOME" and e["outcome"] not in FAILURE_OUTCOMES}


# ---- executor-reported stops ------------------------------------------------------------------

# Only conditions the executor observes locally and the server cannot: a
# stop is reduce-only, so an executor may end its own run, never extend it.
EXECUTOR_STOP_REASONS = frozenset({
    "PERMISSIONS_MISSING", "SIBLING_EMPLOYER_CONTEXT_OPEN", "STRUCTURE_UNSTABLE", "QUARANTINE_RULESET_CHANGED",
    "EXECUTOR_LOST", "PREFILLED_VALUE_CONFLICT", "FIELD_VALUE_REVERTED", "OMIT_FIELD_NOT_BLANK",
})


def executor_stop(conn, *, run_id: str, reason: str, detail: Mapping[str, Any] | None, now: datetime) -> dict[str, Any]:
    if reason not in EXECUTOR_STOP_REASONS:
        raise ValueError(f"{reason!r} is not an executor-reportable stop reason")

    def work() -> dict[str, Any]:
        _run_for(conn, run_id)
        if is_terminal(current_state(conn, run_id)):
            raise FillRefused("run_not_active")
        stop_run_in_transaction(conn, run_id=run_id, reason=reason, detail={"reported_by": "executor",
                                                                            **dict(detail or {})}, now=now)
        return {"state": "FILL_STOPPED", "reason": reason}
    return run_immediate(conn, work)


# ---- start, observations --------------------------------------------------------------------

def context_key(executor_instance_id: str, browser_session_id: str, execution_tab_id: int) -> str:
    return f"{executor_instance_id}|{browser_session_id}|{execution_tab_id}"


def start_run(conn, *, settings: Settings, account_id: str, handoff_session_id: str, application_workspace_id: str,
              executor_instance_id: str, browser_session_id: str, execution_tab_id: int,
              now: datetime) -> dict[str, Any]:
    ws = application_workspace_id

    def work() -> dict[str, Any]:
        workspace = get_workspace(conn, ws, account_id=account_id)
        if workspace is None or workspace.get("kind") != "job":
            raise LookupError(ws)
        _reap_in_transaction(conn, now=now)  # an expired run never blocks a fresh one
        run = f.insert_run(conn, account_id=account_id, application_workspace_id=ws,
                           handoff_session_id=handoff_session_id, executor_instance_id=executor_instance_id,
                           browser_session_id=browser_session_id, execution_tab_id=execution_tab_id,
                           timing_version=FILL_TIMING_VERSION, now=now)
        try:
            f.claim_active_run(conn, application_workspace_id=ws, fill_run_id=run["id"],
                               context_key=context_key(executor_instance_id, browser_session_id, execution_tab_id))
        except dbapi.IntegrityError:
            raise FillRefused("run_active" if f.active_run_for(conn, ws) else "context_in_use") from None
        f.touch_lease(conn, fill_run_id=run["id"], expires_at=now + RUN_LEASE_TTL, now=now)
        f.append_run_event(conn, fill_run_id=run["id"], event="OBSERVING", now=now)
        return run
    return run_immediate(conn, work)


def _route_initial(conn, *, settings: Settings, run: Mapping[str, Any], observation_id: str,
                   now: datetime) -> dict[str, Any]:
    run_id = run["id"]
    outcome = propose_plan_in_transaction(conn, settings=settings, account_id=run["account_id"],
                                          application_workspace_id=run["application_workspace_id"],
                                          observation_id=observation_id, now=now)
    if outcome.state == "PLAN_PROPOSED":
        f.append_run_event(conn, fill_run_id=run_id, event="PLAN_PROPOSED",
                           detail={"plan_hash": outcome.plan_hash, "confirmed": outcome.confirmed}, now=now)
        if not outcome.confirmed:  # the user confirms on the fill-plan page; the next click is a new run
            finish_in_transaction(conn, run_id=run_id, event="PLAN_NEEDS_REVIEW",
                                  detail={"cause": "PLAN_CONFIRMATION_REQUIRED", "plan_hash": outcome.plan_hash},
                                  now=now)
            return {"state": "PLAN_NEEDS_REVIEW", "plan_hash": outcome.plan_hash}
        f.append_run_event(conn, fill_run_id=run_id, event="REVALIDATING", detail={"plan_hash": outcome.plan_hash},
                           now=now)
        return {"state": "REVALIDATING", "plan_hash": outcome.plan_hash}
    if outcome.state in ("PLAN_NEEDS_REVIEW", "DELTAS_OPENED"):
        detail = {"cause": "MAPPING_REQUIRED",
                  "page_field_keys": [r["page_field_key"] for r in outcome.details["needs_review"]]} \
            if outcome.state == "PLAN_NEEDS_REVIEW" else {"cause": "DELTAS_OPENED", **outcome.details}
        finish_in_transaction(conn, run_id=run_id, event="PLAN_NEEDS_REVIEW", detail=detail, now=now)
        return {"state": "PLAN_NEEDS_REVIEW", **detail}
    if outcome.state == "UNSUPPORTED_FORM":
        finish_in_transaction(conn, run_id=run_id, event="UNSUPPORTED_FORM",
                              detail={"causes": outcome.details["causes"]}, now=now)
        return {"state": "UNSUPPORTED_FORM", "causes": outcome.details["causes"]}
    reason = outcome.details["reason"] if outcome.state == "STOPPED" else "APPROVAL_NOT_EFFECTIVE"
    detail = {k: v for k, v in outcome.details.items() if k != "reason"}
    stop_run_in_transaction(conn, run_id=run_id, reason=reason, detail=detail, now=now)
    return {"state": "FILL_STOPPED", "reason": reason}


def record_observation(conn, *, settings: Settings, run_id: str, phase: str, action_index: int | None,
                       observation: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Validate and store an observation (observational evidence). INITIAL
    routes the run through the plan; REVALIDATION must equal the confirmed
    plan's observation or the run stops before any write."""
    if phase not in OBSERVATION_PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    validate_observation(observation, CATALOGUE)

    def work() -> dict[str, Any]:
        run = _run_for(conn, run_id)
        state = current_state(conn, run_id)
        expected = {"INITIAL": ("OBSERVING",), "REVALIDATION": ("REVALIDATING",),
                    "POST_FILL": ("FILLED_AWAITING_SUBMISSION",)}.get(phase, ("FILLING", "FINAL_VALIDATING"))
        if state not in expected:
            raise FillRefused("unexpected_phase")
        stored = f.insert_observation(conn, account_id=run["account_id"],
                                      application_workspace_id=run["application_workspace_id"], fill_run_id=run_id,
                                      phase=phase, action_index=action_index,
                                      structure_fingerprint=structure_fingerprint(observation),
                                      observation_fingerprint=observation_fingerprint(observation),
                                      observation=observation, now=now)
        if phase == "INITIAL":
            return {"observation_id": stored["id"], **_route_initial(conn, settings=settings, run=run,
                                                                     observation_id=stored["id"], now=now)}
        if phase == "REVALIDATION":
            if not observation["context"]["application_root_found"]:  # the surface did not come back (§10.4)
                finish_in_transaction(conn, run_id=run_id, event="UNSUPPORTED_FORM",
                                      detail={"causes": ["RESET_SURFACE_MISMATCH"]}, now=now)
                return {"observation_id": stored["id"], "state": "UNSUPPORTED_FORM",
                        "causes": ["RESET_SURFACE_MISMATCH"]}
            match = _revalidation_match(conn, run_id)
            if not match.matched:
                _stop_on_mismatch(conn, run=run, match=match, now=now)
                return {"observation_id": stored["id"], "state": "FILL_STOPPED", "reason": match.stop_reason}
            return {"observation_id": stored["id"], "state": "REVALIDATING", "matched": True}
        if phase == "PRE_ACTION":  # the server re-checks the page before the next intent (§11.3 a, §13)
            binding = f.get_grant_binding(conn, run_id)
            stopped = diff_stop_in_transaction(conn, run=run, plan_row=f.get_plan_by_hash(conn, binding["plan_hash"]),
                                               observation=observation, completed=completed_actions(conn, run_id),
                                               now=now) if binding else None
            if stopped is not None:
                return {"observation_id": stored["id"], **stopped}
        return {"observation_id": stored["id"], "state": state}
    return run_immediate(conn, work)


# ---- revalidation -----------------------------------------------------------------------------

def _revalidation_match(conn, run_id: str) -> MatchResult:
    plan_hash = run_plan_hash(conn, run_id)
    plan_row = f.get_plan_by_hash(conn, plan_hash) if plan_hash else None
    new = f.latest_observation(conn, run_id, "REVALIDATION")
    if plan_row is None or new is None:
        return MatchResult(False, "OBSERVATION_MISMATCH", {"cause": "NO_REVALIDATION"})
    plan = plan_row["plan"]
    if new["observation_fingerprint"] == plan["observation_fingerprint"]:
        return MatchResult(True)
    if new["structure_fingerprint"] == plan["structure_fingerprint"]:
        return MatchResult(False, "OBSERVATION_MISMATCH", {"cause": "VALUE_STATES_DIFFER"})
    entry = certified(plan["adapter_id"], plan["adapter_version"])
    base = f.get_observation(conn, plan_row["observation_id"])["observation"]
    change = classify_changes(base, new["observation"], plan=plan, completed=set(), entry=entry)
    if change.stop_reason is None:  # different structure the diff cannot name: still a structural change
        return MatchResult(False, "STRUCTURE_CHANGED", {"changes": ["structure_fingerprint"]})
    return MatchResult(False, change.stop_reason, change.detail, list(change.deltas))


def revalidation_matches(conn, *, run_id: str) -> MatchResult:
    return _revalidation_match(conn, run_id)


def _stop_on_mismatch(conn, *, run: Mapping[str, Any], match: MatchResult, now: datetime) -> None:
    detail = dict(match.detail)
    if match.deltas:
        detail["delta_ids"] = _open_deltas(conn, account_id=run["account_id"], ws=run["application_workspace_id"],
                                           specs=match.deltas, source=f"FILL_RUN:{run['id']}", now=now)
    stop_run_in_transaction(conn, run_id=run["id"], reason=match.stop_reason, detail=detail, now=now)


# ---- quarantine ---------------------------------------------------------------------------------

def record_quarantine(conn, *, run_id: str, phase: str, ruleset_hash: str | None, action_index: int | None = None,
                      now: datetime) -> dict[str, Any]:
    """Executor-reported quarantine evidence. TOTAL_VERIFIED after a matching
    revalidation records QUARANTINE_ACTIVE; LOST stops the run (or marks a
    FILLED run's context unverified)."""
    if phase not in QUARANTINE_PHASES:
        raise ValueError(f"unknown quarantine phase {phase!r}")

    def work() -> dict[str, Any]:
        _run_for(conn, run_id)
        state = current_state(conn, run_id)
        if state == "FILLED_AWAITING_SUBMISSION" and phase == "LOST":
            f.append_quarantine_event(conn, fill_run_id=run_id, phase=phase, ruleset_hash=ruleset_hash,
                                      action_index=action_index, now=now)
            f.append_run_event(conn, fill_run_id=run_id, event="FILLED_CONTEXT_UNVERIFIED",
                               detail={"cause": "QUARANTINE_LOST"}, now=now)
            f.delete_lease(conn, run_id)
            return {"state": "FILLED_CONTEXT_UNVERIFIED"}
        if is_terminal(state):
            raise FillRefused("run_not_active")
        f.append_quarantine_event(conn, fill_run_id=run_id, phase=phase, ruleset_hash=ruleset_hash,
                                  action_index=action_index, now=now)
        if phase == "LOST":
            stop_run_in_transaction(conn, run_id=run_id, reason="QUARANTINE_LOST", now=now)
            return {"state": "FILL_STOPPED", "reason": "QUARANTINE_LOST"}
        if phase == "TOTAL_VERIFIED":
            if state != "REVALIDATING" or not ruleset_hash:
                raise FillRefused("unexpected_phase")
            if not _revalidation_match(conn, run_id).matched:
                raise FillRefused("revalidation_required")
            f.append_run_event(conn, fill_run_id=run_id, event="QUARANTINE_ACTIVE",
                               detail={"ruleset_hash": ruleset_hash}, now=now)
            return {"state": "QUARANTINE_ACTIVE"}
        return {"state": state}
    return run_immediate(conn, work)


# ---- the FILL grant (spec §11.2) --------------------------------------------------------------------

def confirmation_ids(conn, binding: Mapping[str, Any]) -> dict[str, str]:
    out = {}
    for fld in binding["fields"]:
        if fld["disposition"] == "ANSWER" and fld["source_kind"] == "APPROVED_ANSWER":
            confirmation = latest_answer_confirmation_id(conn, fld["source_ref"])
            if confirmation is not None:
                out[fld["answer_key"]] = confirmation
    return out


def target_observation(plan: Mapping[str, Any]) -> ApplyTargetObservation:
    """The 6B apply-target facts for a run whose revalidated observation equals
    the confirmed plan: the plan exists only when the observed canonical URL
    equals the 6D-A-approved apply target, so the landing is the approved
    target (no redirect), and its tenant is the approved target's. The ATS job
    id has no independent source (6B deviation 6), so it stays unknown."""
    return ApplyTargetObservation(adapter_id=plan["adapter_id"], adapter_version=plan["adapter_version"],
                                  landing_within_redirect_set=True, tenant_key=plan["tenant_key"],
                                  tenant_matches_employer=True, ats_job_id_matches=None, unexplained_redirect=False)


def request_fill_grant(conn, *, settings: Settings, run_id: str, now: datetime) -> GrantResult:
    def refuse(reason: str, detail: Mapping[str, Any]) -> GrantResult:
        stop_run_in_transaction(conn, run_id=run_id, reason=reason, detail=detail, now=now)
        return GrantResult(False, None, reason, dict(detail))

    def work() -> GrantResult:
        run = _run_for(conn, run_id)
        existing = f.get_grant_binding(conn, run_id)
        state = current_state(conn, run_id)
        if existing is not None and state == "FILLING":  # a retried request returns the bound grant
            return GrantResult(True, existing["grant_id"])
        if is_terminal(state):
            raise FillRefused("run_not_active")
        account_id, ws = run["account_id"], run["application_workspace_id"]
        if state != "QUARANTINE_ACTIVE":
            return refuse("GRANT_REFUSED", {"cause": "QUARANTINE_NOT_ACTIVE"})
        plan_row = f.get_plan_by_hash(conn, run_plan_hash(conn, run_id))
        plan = plan_row["plan"]
        ctx = approval_context(conn, settings=settings, account_id=account_id, application_workspace_id=ws, now=now)
        if ctx is None or (ctx["approval_id"], ctx["binding_hash"]) != (plan["approval_id"],
                                                                       plan["approval_binding_hash"]):
            return refuse("APPROVAL_NOT_EFFECTIVE", {})
        if not f.plan_confirmed(conn, plan["plan_hash"], ctx["approval_id"], ctx["binding_hash"]):
            return refuse("PLAN_CONFIRMATION_STALE", {})
        match = _revalidation_match(conn, run_id)
        if not match.matched:
            return refuse(match.stop_reason, match.detail)
        manifest = derive_manifest(plan, binding=ctx["binding"], confirmation_ids=confirmation_ids(conn, ctx["binding"]))
        violations = g4_violations(manifest, plan, ctx["binding"], binding_hash_value=ctx["binding_hash"])
        if violations:
            return refuse("APPROVAL_NOT_EFFECTIVE", {"g4": violations})
        try:
            outcome = request_grant(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                                    stage=Capability.FILL, now=now, fill_manifest=manifest,
                                    observation=target_observation(plan), in_transaction=True)
        except AutonomyPaused:
            return refuse("GRANT_REFUSED", {"cause": "PAUSED"})
        if outcome.grant is None:
            return refuse("GRANT_REFUSED", {"decision_id": outcome.decision_row["id"],
                                            "deny_reason": outcome.decision.deny_reason,
                                            "effective_capability": outcome.decision.effective_capability.name})
        revalidated = f.latest_observation(conn, run_id, "REVALIDATION")
        active = next(e for e in reversed(f.run_events(conn, run_id)) if e["event"] == "QUARANTINE_ACTIVE")
        f.insert_grant_binding(conn, fill_run_id=run_id, grant_id=outcome.grant["id"], approval_id=ctx["approval_id"],
                               approval_binding_hash=ctx["binding_hash"], plan_hash=plan["plan_hash"],
                               structure_fingerprint=revalidated["structure_fingerprint"],
                               observation_fingerprint=revalidated["observation_fingerprint"],
                               ruleset_hash=active["detail"]["ruleset_hash"], now=now)
        f.append_run_event(conn, fill_run_id=run_id, event="FILLING", detail={"grant_id": outcome.grant["id"]},
                           now=now)
        return GrantResult(True, outcome.grant["id"])
    return run_immediate(conn, work)


# ---- leases -------------------------------------------------------------------------------------

def heartbeat(conn, *, run_id: str, now: datetime) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        _run_for(conn, run_id)
        lease = f.get_lease(conn, run_id)
        if lease is None:
            raise FillRefused("run_not_active")
        if run_id in f.expired_leases(conn, now):
            _reap_one(conn, run_id, now=now)
            return {"state": current_state(conn, run_id), "lease_expired": True}
        f.touch_lease(conn, fill_run_id=run_id, expires_at=now + RUN_LEASE_TTL, now=now)
        return {"state": current_state(conn, run_id), "lease_expired": False}
    return run_immediate(conn, work)


def _reap_one(conn, run_id: str, *, now: datetime) -> None:
    state = current_state(conn, run_id)
    if state == "FILLED_AWAITING_SUBMISSION":
        f.append_run_event(conn, fill_run_id=run_id, event="FILLED_CONTEXT_UNVERIFIED",
                           detail={"cause": "EXECUTOR_LOST"}, now=now)
        f.delete_lease(conn, run_id)
    elif not is_terminal(state):
        stop_run_in_transaction(conn, run_id=run_id, reason="EXECUTOR_LOST", now=now)
    else:
        f.delete_lease(conn, run_id)


def _reap_in_transaction(conn, *, now: datetime) -> int:
    expired = f.expired_leases(conn, now)
    for run_id in expired:
        _reap_one(conn, run_id, now=now)
    return len(expired)


def reap_expired_leases(conn, *, now: datetime) -> int:
    """Reduce-only sweep: stops or marks unverified, never grants or resumes."""
    return run_immediate(conn, lambda: _reap_in_transaction(conn, now=now))

