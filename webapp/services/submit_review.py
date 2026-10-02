"""Bundle 6E-A Submit Review (spec §7, §16.1).

The web app asks the extension (through the run heartbeat) for a fresh
REVIEW observation of the filled, still-quarantined page; the snapshot is
assembled only when every §7.2 precondition holds, and hashed over stable
values only (submission-review.v1). Readiness is a dry HUMAN_SUBMIT gate
evaluation: nothing is written by reading the review.

Precondition reasons (closed): no_filled_run, lease_expired,
already_authorized, post_fill_change, observation_missing,
observation_stale, submit_control_not_unique, page_changed_since_fill,
approval_not_effective, plan_mismatch, adapter_not_submit_certified,
adapter_not_live_certified."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from product.autonomy_contract import ENGINE_VERSION, AuthorityKind, Capability, Mode, parse_utc
from product.autonomy_gate import evaluate_authorization
from product.fill_certification import CATALOGUE
from product.fill_observation import observation_fingerprint, structure_fingerprint, validate_observation
from product.fill_plan import derive_manifest
from product.semantic_subject_policy import subject_policy_hash
from product.submit_certification import submission_permitted, submit_certified
from product.submit_constants import SUBMIT_REVIEW_OBSERVATION_MAX_AGE
from product.submit_review import ReviewInputs, build_review_snapshot, review_hash
from webapp.config import Settings
from webapp.persistence import fill as f
from webapp.persistence import submit as sp
from webapp.services.autonomy_context import build_context
from webapp.services.autonomy_controls import run_immediate, sentinel_present
from webapp.services.fill_plans import approval_context
from webapp.services.fill_results import run_plan_hash
from webapp.services.fill_runs import confirmation_ids, current_state, target_observation

FILLED = "FILLED_AWAITING_SUBMISSION"


class SubmitRefused(Exception):
    """A domain refusal (routes: 409 with the reason)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Assembled:
    run: dict[str, Any]
    plan: dict[str, Any]
    approval: dict[str, Any]
    observation: dict[str, Any]
    inputs: ReviewInputs
    snapshot: dict[str, Any]
    review_hash: str


# ---- runs and re-observation --------------------------------------------------------------

def filled_run(conn, application_workspace_id: str) -> dict[str, Any] | None:
    """The latest fill run of the application, if it is FILLED."""
    runs = f.runs_for_application(conn, application_workspace_id)
    if not runs:
        return None
    latest = runs[-1]
    return latest if current_state(conn, latest["id"]) == FILLED else None


def _lease_live(conn, run_id: str, now: datetime) -> bool:
    lease = f.get_lease(conn, run_id)
    return lease is not None and parse_utc(lease["expires_at"]) > now


def request_reobservation(conn, *, account_id: str, application_workspace_id: str,
                          now: datetime) -> dict[str, Any] | None:
    """Ask the extension (via the heartbeat) for a REVIEW observation. None
    when there is no FILLED run with a live lease to observe."""
    def work():
        run = filled_run(conn, application_workspace_id)
        if run is None or run["account_id"] != account_id or not _lease_live(conn, run["id"], now):
            return None
        if sp.authorization_for_run(conn, run["id"]) is not None:
            return None
        return sp.insert_reobservation_request(conn, account_id=account_id, fill_run_id=run["id"], now=now)
    return run_immediate(conn, work)


def pending_reobservation(conn, fill_run_id: str) -> dict[str, Any] | None:
    request = sp.latest_reobservation_request(conn, fill_run_id)
    if request is None:
        return None
    observed = sp.latest_submit_observation(conn, fill_run_id, "REVIEW")
    if observed is not None and observed["created_at"] >= request["requested_at"]:
        return None
    return request


def record_submit_observation(conn, *, settings: Settings, run_id: str, phase: str, attempt_id: str | None,
                              observation: dict[str, Any], now: datetime) -> dict[str, Any]:
    validate_observation(observation, CATALOGUE)

    def work():
        run = f.get_run(conn, run_id)
        if run is None:
            raise SubmitRefused("run_not_found")
        authorization = sp.authorization_for_run(conn, run_id)
        if phase == "REVIEW":
            if current_state(conn, run_id) != FILLED:
                raise SubmitRefused("unexpected_phase")
            if authorization is not None:
                raise SubmitRefused("already_authorized")
        elif authorization is None:
            raise SubmitRefused("not_authorized")
        row = sp.insert_submit_observation(
            conn, account_id=run["account_id"], application_workspace_id=run["application_workspace_id"],
            fill_run_id=run_id, attempt_id=attempt_id, phase=phase,
            structure_fingerprint=structure_fingerprint(observation),
            observation_fingerprint=observation_fingerprint(observation), observation=observation, now=now)
        if authorization is not None:
            # Revalidation (spec §12.2): is the page still exactly the
            # authorized one? The controller continues only on True.
            row["matches_review"] = (row["observation_fingerprint"]
                                     == authorization["review"]["observation"]["observation_fingerprint"])
        return row
    return run_immediate(conn, work)


# ---- assembly -----------------------------------------------------------------------------

def _plan_confirmation_id(conn, plan: Mapping[str, Any]) -> str | None:
    row = conn.execute("SELECT id FROM fill_plan_confirmations WHERE plan_hash = ? AND approval_id = ? "
                       "AND approval_binding_hash = ? ORDER BY seq DESC LIMIT 1",
                       (plan["plan_hash"], plan["approval_id"], plan["approval_binding_hash"])).fetchone()
    return row[0] if row else None


def human_context(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                  plan: Mapping[str, Any], origin: str, now: datetime, sentinel: bool | None = None):
    """The HUMAN_SUBMIT authorization context for this application."""
    return build_context(conn, settings=settings, account_id=account_id,
                         application_workspace_id=application_workspace_id, requested_stage=Capability.SUBMIT,
                         mode=Mode.LIVE, now=now,
                         sentinel_present=sentinel_present(settings.autonomy_sentinel_path) if sentinel is None
                         else sentinel,
                         observation=target_observation(plan), authority=AuthorityKind.HUMAN_SUBMIT,
                         submit_origin=origin)


def human_manifest(conn, plan: Mapping[str, Any], approval: Mapping[str, Any]) -> dict[str, Any]:
    return derive_manifest(plan, binding=approval["binding"], confirmation_ids=confirmation_ids(conn, approval["binding"]))


def assemble(conn, *, settings: Settings, account_id: str, application_workspace_id: str, now: datetime,
             observation_row: Mapping[str, Any] | None = None, ctx=None) -> Assembled:
    """Every §7.2 precondition, then the snapshot. Raises SubmitRefused with
    the first failing reason. `observation_row` defaults to the latest
    REVIEW observation (PRE_SUBMIT passes its own)."""
    run = filled_run(conn, application_workspace_id)
    if run is None or run["account_id"] != account_id:
        raise SubmitRefused("no_filled_run")
    run_id = run["id"]
    if not _lease_live(conn, run_id, now):
        raise SubmitRefused("lease_expired")
    if observation_row is None and sp.authorization_for_run(conn, run_id) is not None:
        raise SubmitRefused("already_authorized")
    if any(e["kind"] == "POST_FILL_CHANGE_OBSERVED" for e in f.detection_events(conn, run_id)):
        raise SubmitRefused("post_fill_change")
    obs = observation_row or sp.latest_submit_observation(conn, run_id, "REVIEW")
    if obs is None:
        raise SubmitRefused("observation_missing")
    if now - parse_utc(obs["created_at"]) > SUBMIT_REVIEW_OBSERVATION_MAX_AGE:
        raise SubmitRefused("observation_stale")
    controls = obs["observation"]["submit_controls"]
    if len(controls) != 1:
        raise SubmitRefused("submit_control_not_unique")
    final = f.latest_observation(conn, run_id, "FINAL")
    if final is None or obs["observation_fingerprint"] != final["observation_fingerprint"]:
        raise SubmitRefused("page_changed_since_fill")
    plan = f.get_plan_by_hash(conn, run_plan_hash(conn, run_id))["plan"]
    approval = approval_context(conn, settings=settings, account_id=account_id,
                                application_workspace_id=application_workspace_id, now=now)
    if approval is None or (approval["approval_id"], approval["binding_hash"]) != (plan["approval_id"],
                                                                                   plan["approval_binding_hash"]):
        raise SubmitRefused("approval_not_effective")
    confirmation = _plan_confirmation_id(conn, plan)
    if confirmation is None:
        raise SubmitRefused("plan_mismatch")
    context = obs["observation"]["context"]
    cert = submit_certified(context["adapter_id"], context["adapter_version"])
    permitted, why = submission_permitted(cert, context["origin"],
                                          fixture_origins_enabled=settings.submit_fixture_origins_enabled)
    if not permitted:
        raise SubmitRefused(why)
    ctx = ctx or human_context(conn, settings=settings, account_id=account_id,
                               application_workspace_id=application_workspace_id, plan=plan,
                               origin=context["origin"], now=now)
    confirmations = confirmation_ids(conn, approval["binding"])
    answers = tuple(sorted((a["source_ref"], confirmations.get(a["answer_key"], ""), a["rendered_value_hash"])
                           for a in plan["actions"] if a["action_kind"] == "WRITE"))
    documents = tuple(sorted((a["document_kind"], a["document"]["filename"], a["document"]["sha256"])
                             for a in plan["actions"] if a["action_kind"] == "ATTACH_LOCAL"))
    binding = f.get_grant_binding(conn, run_id)
    inputs = ReviewInputs(
        account_id=account_id, application_workspace_id=application_workspace_id,
        identity_key=ctx.identity_key or "", employer_key=ctx.employer_key or "",
        canonical_url=context["canonical_url"], origin=context["origin"], adapter_id=context["adapter_id"],
        adapter_version=context["adapter_version"], tenant_key=context["tenant_key"] or "",
        ats_job_id=context["ats_job_id"] or "", certification_id=cert.certification_id,
        certification_status=cert.status, fill_run_id=run_id, plan_hash=plan["plan_hash"],
        plan_confirmation_id=confirmation, fill_result_hash=f.get_result(conn, run_id)["result_hash"],
        final_observation_fingerprint=final["observation_fingerprint"], ruleset_hash_total=binding["ruleset_hash"],
        executor_instance_id=run["executor_instance_id"], browser_session_id=run["browser_session_id"],
        execution_tab_id=run["execution_tab_id"], observation_fingerprint=obs["observation_fingerprint"],
        structure_fingerprint=obs["structure_fingerprint"],
        submit_control_fingerprint=controls[0]["control_fingerprint"], approval_id=approval["approval_id"],
        approval_binding_hash=approval["binding_hash"], answers=answers, documents=documents,
        engine_version=ENGINE_VERSION, subject_policy_hash=subject_policy_hash(ctx.subject_policy),
        control_epoch=ctx.control_epoch or 0)
    snapshot = build_review_snapshot(inputs)
    return Assembled(run=run, plan=plan, approval=approval, observation=obs, inputs=inputs, snapshot=snapshot,
                     review_hash=review_hash(snapshot))


def gate_reasons(decision) -> list[str]:
    """Plain reason codes for a non-grantable HUMAN_SUBMIT decision."""
    if decision.deny_reason:
        return [decision.deny_reason]
    codes = [r.code for r in decision.reasons
             if r.code not in ("ceiling", "standing_policy_not_applied", "non_live_mode")
             and (dict(r.params).get("to") not in (None, "SUBMIT") or r.code == "human_submit_disabled")]
    codes += [f"{i.kind}:{i.ref}" if hasattr(i, "ref") else i.kind for i in decision.require_user_items]
    if decision.result.value == "BLOCK":
        codes.append("blocked")
    return sorted(set(codes)) or ["not_grantable"]


def current_review(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                   now: datetime) -> dict[str, Any]:
    empty = {"snapshot": None, "review_hash": None, "decision": None}
    run = filled_run(conn, application_workspace_id)
    if run is not None and run["account_id"] == account_id and pending_reobservation(conn, run["id"]) is not None \
            and sp.authorization_for_run(conn, run["id"]) is None:
        return {"state": "PENDING_OBSERVATION", "reasons": [], **empty}
    try:
        assembled = assemble(conn, settings=settings, account_id=account_id,
                             application_workspace_id=application_workspace_id, now=now)
    except SubmitRefused as refusal:
        return {"state": "UNAVAILABLE", "reasons": [refusal.reason], **empty}
    ctx = human_context(conn, settings=settings, account_id=account_id,
                        application_workspace_id=application_workspace_id, plan=assembled.plan,
                        origin=assembled.inputs.origin, now=now)
    decision = evaluate_authorization(ctx)
    out = {"snapshot": assembled.snapshot, "review_hash": assembled.review_hash,
           "decision": {"result": decision.result.value, "effective_capability": decision.effective_capability.name,
                        "deny_reason": decision.deny_reason}}
    if decision.grantable:
        return {"state": "READY", "reasons": [], **out}
    return {"state": "BLOCKED", "reasons": gate_reasons(decision), **out}
