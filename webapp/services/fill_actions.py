"""Bundle 6D-B per-action protocol (spec §11.3, §12.4, §13, §16.2).

Intent (server, BEGIN IMMEDIATE, keyed (run, action_index)): the grant is
ISSUED and unexpired with an unchanged manifest; FILL authority is
recomputed WITHOUT consuming budget (pause, kill switch/sentinel, ceiling,
the grant's policy version); the 6D-A approval is still the run's and G4
still holds; the confirmation is valid; the index is the next one. Only then
WRITE_INTENT (authoritative) and, for WRITE/ATTACH_LOCAL, one envelope with
a 30 s lifetime. The cleartext is re-rendered for the envelope and never
stored. OMIT/IGNORE get an intent but no envelope.

Outcome: at most one per action (unique index). A failure, a reported
readback that is not the plan's hash, or a post-action diff stops the run
before the next action. After MAY_HAVE_WRITTEN with no outcome the run is
WRITE_OUTCOME_UNKNOWN, never retried."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from product.autonomy_contract import Capability, parse_utc, to_utc_iso
from product.fill_certification import CATALOGUE
from product.fill_constants import VALUE_ENVELOPE_TTL
from product.fill_hash import fill_value_hash
from product.fill_manifest import manifest_hash
from product.fill_observation import observation_fingerprint, structure_fingerprint, validate_observation
from product.fill_plan import DeltaSpec, _observed, _question, _render, derive_manifest, g4_violations
from product.fill_vocab import DETECTION_KINDS, FAILURE_OUTCOMES
from product.standing_policy import policy_hash
from webapp.config import Settings
from webapp.persistence import fill as f
from webapp.persistence import review_approval as ra
from webapp.persistence.autonomy_authority import current_policy, is_paused, kill_switch_state, resolve_authority
from webapp.persistence.autonomy_ledger import get_grant
from webapp.services import autonomy_context
from webapp.services.autonomy_controls import engage_kill_switch_in_transaction, run_immediate, sentinel_present
from webapp.services.fill_plans import _open_deltas, approval_context, approved_values
from webapp.services.fill_runs import (
    FillRefused, completed_actions, confirmation_ids, current_state, diff_stop_in_transaction, finish_in_transaction,
    is_terminal, stop_run_in_transaction,
)

PRECHECK_KEYS = frozenset({"ruleset_hash", "structure_fingerprint", "field_fingerprint", "siblings_contained"})
_SUCCESS_FOR_KIND = {"WRITE": {"WRITTEN_VERIFIED", "NOOP_ALREADY_EQUAL"}, "ATTACH_LOCAL": {"ATTACH_LOCAL_VERIFIED"},
                     "OMIT": {"OMIT_VERIFIED"}, "IGNORE_NON_APPLICATION": {"IGNORE_RECORDED"}}
_REPORTABLE_FAILURES = frozenset(FAILURE_OUTCOMES) - {"WRITE_OUTCOME_UNKNOWN", "ENVELOPE_EXPIRED"}


@dataclass(frozen=True)
class Envelope:
    envelope_id: str | None
    action_index: int
    action_kind: str
    rendered_value: str | None
    rendered_value_hash: str | None
    document: dict[str, Any] | None
    expires_at: str | None


@dataclass
class IntentResult:
    envelope: Envelope | None
    stop_reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# ---- shared run context ---------------------------------------------------------------------

def _run_context(conn, run_id: str) -> dict[str, Any]:
    run = f.get_run(conn, run_id)
    if run is None:
        raise LookupError(run_id)
    binding = f.get_grant_binding(conn, run_id)
    plan = f.get_plan_by_hash(conn, binding["plan_hash"]) if binding else None
    return {"run": run, "binding": binding, "plan": plan["plan"] if plan else None,
            "plan_row": plan}


def _next_index(conn, run_id: str, plan: Mapping[str, Any]) -> int | None:
    done = {e["action_index"] for e in f.action_events(conn, run_id) if e["event"] == "OUTCOME"}
    return next((i for i in range(len(plan["actions"])) if i not in done), None)


def _authority_reduction(conn, *, settings: Settings, account_id: str, ws: str, grant: Mapping[str, Any],
                         now: datetime) -> str | None:
    """The 6B reducers, recomputed without reserving anything."""
    search_ws = autonomy_context.get_search_workspace_for_application(conn, ws)
    scopes = [("APPLICATION", ws)] + ([("SEARCH_WORKSPACE", search_ws)] if search_ws else [])
    if any(is_paused(conn, account_id=account_id, scope_type=t, scope_id=i) for t, i in scopes):
        return "PAUSED"
    if sentinel_present(settings.autonomy_sentinel_path) and not kill_switch_state(conn, account_id)["engaged"]:
        engage_kill_switch_in_transaction(conn, account_id=account_id, actor="sentinel",
                                          reason=f"sentinel file present: {settings.autonomy_sentinel_path}", now=now)
    if kill_switch_state(conn, account_id)["engaged"]:
        return "KILL_SWITCH"
    account_max, ceiling = resolve_authority(conn, account_id=account_id, search_workspace_id=search_ws)
    if min(account_max, ceiling, settings.autonomy_deployment_ceiling()) < Capability.FILL:
        return "CEILING"
    policy = current_policy(conn, account_id)
    if policy is None or policy_hash(policy["doc"]) != grant["binding"]["policy_version_hash"]:
        return "POLICY"
    return None


def _approval_problem(conn, *, settings: Settings, ctx: Mapping[str, Any], grant: Mapping[str, Any],
                      now: datetime) -> tuple[str, dict[str, Any]] | None:
    run, plan = ctx["run"], ctx["plan"]
    approval = approval_context(conn, settings=settings, account_id=run["account_id"],
                                application_workspace_id=run["application_workspace_id"], now=now)
    if approval is None or (approval["approval_id"], approval["binding_hash"]) != (
            ctx["binding"]["approval_id"], ctx["binding"]["approval_binding_hash"]):
        return "APPROVAL_NOT_EFFECTIVE", {}
    manifest = derive_manifest(plan, binding=approval["binding"],
                               confirmation_ids=confirmation_ids(conn, approval["binding"]))
    violations = g4_violations(manifest, plan, approval["binding"], binding_hash_value=approval["binding_hash"])
    if violations:
        return "APPROVAL_NOT_EFFECTIVE", {"g4": violations}
    if manifest_hash(manifest) != grant["binding"]["fill_manifest_hash"]:
        return "APPROVAL_NOT_EFFECTIVE", {"cause": "MANIFEST_CHANGED"}
    if not f.plan_confirmed(conn, plan["plan_hash"], approval["approval_id"], approval["binding_hash"]):
        return "PLAN_CONFIRMATION_STALE", {}
    return None


def _stop(conn, run_id: str, reason: str, detail: Mapping[str, Any], now: datetime) -> IntentResult:
    stop_run_in_transaction(conn, run_id=run_id, reason=reason, detail=detail, now=now)
    return IntentResult(None, reason, dict(detail))


# ---- intent ---------------------------------------------------------------------------------

def _envelope(conn, *, ctx: Mapping[str, Any], action_index: int, envelope_id: str | None,
              expires_at: str | None) -> Envelope:
    run, plan = ctx["run"], ctx["plan"]
    action = plan["actions"][action_index]
    kind = action["action_kind"]
    if kind == "WRITE":
        base = f.get_observation(conn, ctx["plan_row"]["observation_id"])["observation"]
        element = next(e for e in base["elements"] if e["page_field_key"] == action["page_field_key"])
        binding = next(b for b in approval_binding_fields(conn, ctx) if b["answer_key"] == action["answer_key"])
        values = approved_values(conn, run["account_id"], {"fields": [binding]})
        _, rendered = _render(element, values[action["answer_key"]], binding["permitted_transforms"])
        if fill_value_hash(rendered) != action["rendered_value_hash"]:
            raise AssertionError("re-rendered value does not match the plan")
        return Envelope(envelope_id, action_index, kind, rendered, action["rendered_value_hash"], None, expires_at)
    if kind == "ATTACH_LOCAL":
        doc = action["document"]
        return Envelope(envelope_id, action_index, kind, None, action["rendered_value_hash"],
                        {"document_version_id": doc["document_version_id"], "sha256": doc["sha256"],
                         "byte_length": doc["byte_length"], "filename": doc["filename"],
                         "media_type": doc["media_type"], "document_kind": action["document_kind"]}, expires_at)
    return Envelope(None, action_index, kind, None, None, None, None)


def approval_binding_fields(conn, ctx: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The fields of the run's bound 6D-A approval (for re-rendering only)."""
    return ra.get_approval(conn, ctx["binding"]["approval_id"])["binding"]["fields"]


def request_intent(conn, *, settings: Settings, run_id: str, action_index: int, precheck: Mapping[str, Any],
                   now: datetime) -> IntentResult:
    if set(precheck) != PRECHECK_KEYS:
        raise ValueError(f"precheck keys must be exactly {sorted(PRECHECK_KEYS)}")

    def work() -> IntentResult:
        ctx = _run_context(conn, run_id)
        if current_state(conn, run_id) != "FILLING" or ctx["binding"] is None:
            raise FillRefused("run_not_active")
        run, plan, binding = ctx["run"], ctx["plan"], ctx["binding"]
        if not 0 <= action_index < len(plan["actions"]):
            raise FillRefused("unknown_action")
        issued = f.issued_envelope(conn, run_id, action_index)
        intent = [e for e in f.action_events(conn, run_id, action_index) if e["event"] == "WRITE_INTENT"]
        if intent and f.action_outcome(conn, run_id, action_index) is None:  # a retry of an unused intent
            if issued is not None and parse_utc(issued["detail"]["expires_at"]) < now:
                f.append_action_event(conn, fill_run_id=run_id, action_index=action_index, event="OUTCOME",
                                      outcome="ENVELOPE_EXPIRED", envelope_id=issued["envelope_id"], now=now)
                return _stop(conn, run_id, "ENVELOPE_EXPIRED", {"action_index": action_index}, now)
            return IntentResult(_envelope(conn, ctx=ctx, action_index=action_index,
                                          envelope_id=issued["envelope_id"] if issued else None,
                                          expires_at=issued["detail"]["expires_at"] if issued else None))
        if _next_index(conn, run_id, plan) != action_index:
            raise FillRefused("out_of_order")
        grant = get_grant(conn, binding["grant_id"])
        reduction = _authority_reduction(conn, settings=settings, account_id=run["account_id"],
                                         ws=run["application_workspace_id"], grant=grant, now=now)
        if reduction is not None:  # the stop revokes the run's grant
            return _stop(conn, run_id, "AUTHORITY_REDUCED", {"reduction": reduction}, now)
        if grant["status"] != "ISSUED" or grant["expires_at"] <= to_utc_iso(now):
            return _stop(conn, run_id, "GRANT_EXPIRED", {"status": grant["status"]}, now)
        problem = _approval_problem(conn, settings=settings, ctx=ctx, grant=grant, now=now)
        if problem is not None:
            return _stop(conn, run_id, *problem, now)
        action = plan["actions"][action_index]
        f.append_action_event(conn, fill_run_id=run_id, action_index=action_index, event="PRECHECK",
                              detail=dict(precheck), now=now)
        if precheck["ruleset_hash"] != binding["ruleset_hash"]:
            return _stop(conn, run_id, "QUARANTINE_RULESET_CHANGED", {"action_index": action_index}, now)
        if precheck["siblings_contained"] is not True:
            return _stop(conn, run_id, "SIBLING_EMPLOYER_CONTEXT_OPEN", {"action_index": action_index}, now)
        if precheck["structure_fingerprint"] != binding["structure_fingerprint"]:
            return _stop(conn, run_id, "STRUCTURE_CHANGED", {"action_index": action_index}, now)
        if precheck["field_fingerprint"] != action["field_fingerprint"]:
            return _stop(conn, run_id, "TARGET_CHANGED", {"action_index": action_index}, now)
        f.append_action_event(conn, fill_run_id=run_id, action_index=action_index, event="WRITE_INTENT", now=now)
        if action["action_kind"] not in ("WRITE", "ATTACH_LOCAL"):
            return IntentResult(_envelope(conn, ctx=ctx, action_index=action_index, envelope_id=None, expires_at=None))
        envelope_id, expires_at = f"fenv_{uuid.uuid4().hex[:20]}", to_utc_iso(now + VALUE_ENVELOPE_TTL)
        envelope = _envelope(conn, ctx=ctx, action_index=action_index, envelope_id=envelope_id, expires_at=expires_at)
        f.append_action_event(conn, fill_run_id=run_id, action_index=action_index, event="ENVELOPE_ISSUED",
                              envelope_id=envelope_id, detail={"expires_at": expires_at}, now=now)
        return IntentResult(envelope)
    return run_immediate(conn, work)


# ---- outcomes -------------------------------------------------------------------------------

def _store_observation(conn, run: Mapping[str, Any], phase: str, action_index: int | None,
                       observation: dict[str, Any], now: datetime) -> dict[str, Any]:
    return f.insert_observation(conn, account_id=run["account_id"], application_workspace_id=run["application_workspace_id"],
                                fill_run_id=run["id"], phase=phase, action_index=action_index,
                                structure_fingerprint=structure_fingerprint(observation),
                                observation_fingerprint=observation_fingerprint(observation), observation=observation,
                                now=now)


def _validity_delta(conn, ctx: Mapping[str, Any], action: Mapping[str, Any], now: datetime) -> list[str]:
    """spec §13: the page rejected the rendered value → a TRANSFORM_FAILURE delta."""
    run = ctx["run"]
    base = f.get_observation(conn, ctx["plan_row"]["observation_id"])["observation"]
    element = next(e for e in base["elements"] if e["page_field_key"] == action["page_field_key"])
    spec = DeltaSpec("TRANSFORM_FAILURE", action["answer_key"], None, bool(action["required"]), _question(element),
                     _observed(element, value_hash=action["value_hash"], cause="page_validity"))
    return _open_deltas(conn, account_id=run["account_id"], ws=run["application_workspace_id"], specs=[spec],
                        source=f"FILL_RUN:{run['id']}", now=now)


def record_outcome(conn, *, run_id: str, action_index: int, envelope_id: str | None, outcome: str,
                   readback_hash: str | None, post_observation: dict[str, Any], now: datetime) -> dict[str, Any]:
    if outcome not in _REPORTABLE_FAILURES and not any(outcome in s for s in _SUCCESS_FOR_KIND.values()):
        raise ValueError(f"not a reportable outcome: {outcome!r}")
    validate_observation(post_observation, CATALOGUE)

    def work() -> dict[str, Any]:
        ctx = _run_context(conn, run_id)
        existing = f.action_outcome(conn, run_id, action_index)
        if existing is not None:
            if (existing["outcome"], existing["readback_hash"]) == (outcome, readback_hash):
                return {"state": current_state(conn, run_id), "duplicate": True}
            raise FillRefused("outcome_recorded")
        if current_state(conn, run_id) != "FILLING" or ctx["plan"] is None:
            raise FillRefused("run_not_active")
        if not 0 <= action_index < len(ctx["plan"]["actions"]):
            raise FillRefused("unknown_action")
        run, plan = ctx["run"], ctx["plan"]
        action = plan["actions"][action_index]
        if not [e for e in f.action_events(conn, run_id, action_index) if e["event"] == "WRITE_INTENT"]:
            raise FillRefused("no_intent")
        issued = f.issued_envelope(conn, run_id, action_index)
        if (issued["envelope_id"] if issued else None) != envelope_id:
            raise FillRefused("envelope_mismatch")
        if outcome not in _REPORTABLE_FAILURES and outcome not in _SUCCESS_FOR_KIND[action["action_kind"]]:
            raise FillRefused("outcome_kind_mismatch")

        def record(value: str, **detail: Any) -> None:
            f.append_action_event(conn, fill_run_id=run_id, action_index=action_index, event="OUTCOME", outcome=value,
                                  envelope_id=envelope_id, readback_hash=readback_hash, detail=detail, now=now)

        if issued is not None and parse_utc(issued["detail"]["expires_at"]) < now:
            record("ENVELOPE_EXPIRED")
            stop_run_in_transaction(conn, run_id=run_id, reason="ENVELOPE_EXPIRED", detail={"action_index": action_index},
                                    now=now)
            return {"state": "FILL_STOPPED", "reason": "ENVELOPE_EXPIRED"}
        _store_observation(conn, run, "POST_ACTION", action_index, post_observation, now)
        if outcome in _REPORTABLE_FAILURES:
            record(outcome)
            detail: dict[str, Any] = {"action_index": action_index}
            if outcome == "FIELD_VALIDITY_FAILED":
                detail["delta_ids"] = _validity_delta(conn, ctx, action, now)
            stop_run_in_transaction(conn, run_id=run_id, reason=outcome, detail=detail, now=now)
            return {"state": "FILL_STOPPED", "reason": outcome}
        if action["action_kind"] in ("WRITE", "ATTACH_LOCAL") and readback_hash != action["rendered_value_hash"]:
            failure = "READBACK_MISMATCH" if action["action_kind"] == "WRITE" else "ATTACH_LOCAL_MISMATCH"
            record(failure)
            stop_run_in_transaction(conn, run_id=run_id, reason=failure, detail={"action_index": action_index}, now=now)
            return {"state": "FILL_STOPPED", "reason": failure}
        record(outcome, document_sha256_verified=action["document"]["sha256"]
               if action["action_kind"] == "ATTACH_LOCAL" else None)
        stopped = diff_stop_in_transaction(conn, run=ctx["run"], plan_row=ctx["plan_row"], observation=post_observation,
                                           completed=completed_actions(conn, run_id), now=now)
        if stopped is not None:
            return stopped
        following = _next_index(conn, run_id, plan)
        if following is None:
            f.append_run_event(conn, fill_run_id=run_id, event="FINAL_VALIDATING", now=now)
            return {"state": "FINAL_VALIDATING"}
        return {"state": "FILLING", "next_action_index": following}
    return run_immediate(conn, work)


def record_unknown_outcome(conn, *, run_id: str, action_index: int, now: datetime) -> dict[str, Any]:
    """The executor lost execution after MAY_HAVE_WRITTEN: never retried."""
    def work() -> dict[str, Any]:
        if is_terminal(current_state(conn, run_id)):
            raise FillRefused("run_not_active")
        if f.action_outcome(conn, run_id, action_index) is not None:
            raise FillRefused("outcome_recorded")
        if not [e for e in f.action_events(conn, run_id, action_index) if e["event"] == "WRITE_INTENT"]:
            raise FillRefused("no_intent")
        issued = f.issued_envelope(conn, run_id, action_index)
        f.append_action_event(conn, fill_run_id=run_id, action_index=action_index, event="OUTCOME",
                              outcome="WRITE_OUTCOME_UNKNOWN", envelope_id=issued["envelope_id"] if issued else None,
                              now=now)
        stop_run_in_transaction(conn, run_id=run_id, reason="WRITE_OUTCOME_UNKNOWN",
                                detail={"action_index": action_index}, now=now)
        return {"state": "FILL_STOPPED", "reason": "WRITE_OUTCOME_UNKNOWN"}
    return run_immediate(conn, work)


# ---- final validation and detections --------------------------------------------------------

def final_validate(conn, *, run_id: str, observation: dict[str, Any], now: datetime) -> dict[str, Any]:
    validate_observation(observation, CATALOGUE)

    def work() -> dict[str, Any]:
        ctx = _run_context(conn, run_id)
        if current_state(conn, run_id) != "FINAL_VALIDATING":
            raise FillRefused("run_not_active")
        run, plan = ctx["run"], ctx["plan"]
        _store_observation(conn, run, "FINAL", None, observation, now)
        completed = completed_actions(conn, run_id)
        if completed != set(range(len(plan["actions"]))):
            stop_run_in_transaction(conn, run_id=run_id, reason="STRUCTURE_CHANGED",
                                    detail={"cause": "ACTIONS_INCOMPLETE"}, now=now)
            return {"state": "FILL_STOPPED", "reason": "STRUCTURE_CHANGED"}
        stopped = diff_stop_in_transaction(conn, run=ctx["run"], plan_row=ctx["plan_row"], observation=observation,
                                           completed=completed, now=now)
        if stopped is not None:
            return stopped
        finish_in_transaction(conn, run_id=run_id, event="FILLED_AWAITING_SUBMISSION", now=now)
        return {"state": "FILLED_AWAITING_SUBMISSION"}
    return run_immediate(conn, work)


_DETECTION_STOPS = {"SUBMIT_ATTEMPT_OBSERVED": "SUBMIT_ATTEMPT_OBSERVED",
                    "NAVIGATION_ATTEMPT_OBSERVED": "NAVIGATION_ATTEMPT_OBSERVED",
                    "EXECUTION_CONTEXT_CLOSED": "EXECUTION_CONTEXT_CLOSED",
                    "POST_FILL_CHANGE_OBSERVED": "STRUCTURE_CHANGED"}


def record_detection(conn, *, run_id: str, kind: str, detail: Mapping[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Observational. An active run stops; after FILLED it is an event only
    (a post-fill change marks the filled surface stale for 6E)."""
    if kind not in DETECTION_KINDS:
        raise ValueError(f"unknown detection kind {kind!r}")

    def work() -> dict[str, Any]:
        if f.get_run(conn, run_id) is None:
            raise LookupError(run_id)
        state = current_state(conn, run_id)
        f.append_detection_event(conn, fill_run_id=run_id, kind=kind, detail=dict(detail or {}), now=now)
        if is_terminal(state):
            return {"state": state}
        reason = _DETECTION_STOPS[kind]
        stop_run_in_transaction(conn, run_id=run_id, reason=reason, detail={"detection": kind}, now=now)
        return {"state": "FILL_STOPPED", "reason": reason}
    return run_immediate(conn, work)
