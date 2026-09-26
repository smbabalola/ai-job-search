"""Autonomy orchestration (6B spec §9.6, §10). Decisions are always recorded
(including denials); grants are issued only for grantable LIVE decisions and
are bound to the exact action inputs; the pre-click transaction (below) is
the authorization point of no return.

Deviations from the task-14 brief:
  1. Pause (spec §11.4, user ruling 2026-09-26): an application or its search
     workspace being paused means no new decisions and no new grants.
     Checked inside the transaction; raises AutonomyPaused, writes nothing.
     The pure gate never infers pause.
  2. The sentinel is observed inside the same BEGIN IMMEDIATE transaction as
     the decision (spec §15.2), engaging the kill switch there if present.
  3. run_immediate (shared with autonomy_controls) refuses to run inside a
     caller's open transaction.
  4. FILL grants also require a fill manifest (spec §8: the manifest hash is
     bound into FILL and SUBMIT grants).
  5. The manifest must belong to the requested workspace and, when an
     observation is supplied, to the observed adapter id and version.
  6. The binding also carries the apply target's canonical URL, adapter
     version and tenant key (spec §10.1). ATS job id and the permitted
     redirect set have no 6B source (executor, 6D) and are not bound yet;
     answer confirmations are bound by confirmed_at.

Deviations from the task-15 brief:
  7. SUBMIT requires a valid, active run of the account whenever the
     standing policy configures submit_per_run (user ruling 2026-09-26):
     request_grant(SUBMIT) and pre_click_commit raise RunRequired before any
     write rather than omitting the per-run limit. The run id is bound into
     the grant (binding key run_id), so a grant cannot be consumed by
     another run.
  8. pre_click_commit checks pause first (AutonomyPaused, nothing written),
     rebuilds the binding with the same observation and canonical target
     URL request_grant used, and follows spec §10.3 order: reserve, then
     consume, claim, create attempt.
  9. An unconsumed SUBMIT grant that is revoked (pre-click, kill switch) or
     expires releases the reservations made with it (budgets reserved at
     issuance) -- done in the ledger's revoke_grant/expire_grants. FILL
     grants keep theirs; a consumed grant's are settled by its attempt.

Deviation from the task-16 brief:
 10. A human submission recorded while an autonomous attempt holds the
     CLAIMED intent turns that intent CONFIRMED (record_human_intent). The
     attempt then may not dispatch (record_click_dispatched ends it
     EXPIRED_UNCLICKED, reason intent_confirmed_by_human), and releasing or
     confirming an attempt only moves an intent that is still CLAIMED.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from product.autonomy_contract import (
    CLICK_DISPATCH_TTL, ENGINE_VERSION, FILL_SESSION_TTL, SUBMIT_GRANT_TTL, AuthorizationContext,
    AuthorizationDecision, Capability, Mode, canonical_json, parse_utc, to_utc_iso,
)
from product.autonomy_gate import evaluate_authorization
from product.fill_manifest import manifest_hash, validate_fill_manifest
from product.semantic_subject_policy import subject_policy_hash
from product.standing_policy import policy_hash
from webapp.config import Settings
from webapp.persistence.autonomy_authority import current_policy, get_run, is_paused, kill_switch_state
from webapp.persistence.autonomy_ledger import (
    append_attempt_event, attempt_state, claim_intent, consume_grant, create_attempt, get_grant, insert_decision,
    insert_grant, reserve_budget, revoke_grant, set_intent_state, set_reservation_status, try_reserve,
)
from webapp.services import autonomy_context
from webapp.services.autonomy_context import (
    ApplyTargetObservation, RequirementSpec, build_context, day_window,
)
from webapp.services.autonomy_controls import engage_kill_switch_in_transaction, run_immediate, sentinel_present


class AutonomyPaused(Exception):
    pass


class RunRequired(ValueError):
    pass


@dataclass(frozen=True)
class GrantOutcome:
    decision: AuthorizationDecision
    decision_row: dict[str, Any]
    grant: dict[str, Any] | None


def build_binding(ctx: AuthorizationContext, *, stage: Capability, fill_manifest: dict | None,
                  observation: ApplyTargetObservation | None = None,
                  target_url: str | None = None) -> dict[str, Any]:
    target = ctx.apply_target
    return {
        "stage": stage.name,
        "account_id": ctx.account_id,
        "application_workspace_id": ctx.application_workspace_id,
        "pack_artifact_id": ctx.pack_artifact_id,
        "fill_manifest": fill_manifest,
        "fill_manifest_hash": manifest_hash(fill_manifest) if fill_manifest is not None else None,
        "answers": sorted(
            [c.approved_answer_id, to_utc_iso(c.confirmed_at)]
            for r in ctx.requirements for c in r.candidates
        ),
        "apply_target": {
            "canonical_url": target_url,
            "provenance": target.provenance.value if target.provenance else None,
            "adapter_id": target.adapter_id,
            "adapter_version": observation.adapter_version if observation else None,
            "tenant_key": observation.tenant_key if observation else None,
        },
        "employer_key": ctx.employer_key,
        "identity_key": ctx.identity_key,
        "run_id": ctx.run_id,
        "policy_version_hash": None if ctx.standing_policy is None else policy_hash(ctx.standing_policy),
        "subject_policy_hash": subject_policy_hash(ctx.subject_policy),
        "engine_version": ENGINE_VERSION,
    }


def binding_drift(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> tuple[str, ...]:
    keys = set(expected) | set(actual)
    return tuple(sorted(k for k in keys if canonical_json(expected.get(k)) != canonical_json(actual.get(k))))


def _check_not_paused(conn, *, account_id: str, application_workspace_id: str) -> None:
    scopes = [("APPLICATION", application_workspace_id)]
    # Same lookup build_context uses, so the pause check and the decision
    # always agree on which search workspace governs this application.
    search_ws = autonomy_context.get_search_workspace_for_application(conn, application_workspace_id)
    if search_ws is not None:
        scopes.append(("SEARCH_WORKSPACE", search_ws))
    for scope_type, scope_id in scopes:
        if is_paused(conn, account_id=account_id, scope_type=scope_type, scope_id=scope_id):
            raise AutonomyPaused(f"{scope_type} {scope_id} is paused")


def _observe_sentinel_in_transaction(conn, *, settings: Settings, account_id: str, now: datetime) -> bool:
    present = sentinel_present(settings.autonomy_sentinel_path)
    if present and not kill_switch_state(conn, account_id)["engaged"]:
        engage_kill_switch_in_transaction(conn, account_id=account_id, actor="sentinel",
                                          reason=f"sentinel file present: {settings.autonomy_sentinel_path}",
                                          now=now)
    return present


def _require_active_run(conn, *, account_id: str, run_id: str | None) -> None:
    policy = current_policy(conn, account_id)
    if policy is None or "submit_per_run" not in policy["doc"]["limits"]:
        return
    run = get_run(conn, run_id) if run_id is not None else None
    if run is None or run["account_id"] != account_id or run["ended_at"] is not None:
        raise RunRequired(f"SUBMIT needs an active run of this account (submit_per_run is configured); got {run_id!r}")


def _check_manifest(fill_manifest: dict, *, application_workspace_id: str,
                    observation: ApplyTargetObservation | None) -> None:
    validate_fill_manifest(fill_manifest)
    if fill_manifest["application_workspace_id"] != application_workspace_id:
        raise ValueError("fill manifest belongs to a different application workspace")
    if observation is not None and (fill_manifest["adapter_id"], fill_manifest["adapter_version"]) != (
            observation.adapter_id, observation.adapter_version):
        raise ValueError("fill manifest adapter does not match the observed adapter")


def decide_and_record(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                      requested_stage: Capability, mode: Mode, now: datetime,
                      **context_kwargs: Any) -> tuple[AuthorizationDecision, dict[str, Any]]:
    def work():
        _check_not_paused(conn, account_id=account_id, application_workspace_id=application_workspace_id)
        sentinel = _observe_sentinel_in_transaction(conn, settings=settings, account_id=account_id, now=now)
        ctx = build_context(conn, settings=settings, account_id=account_id,
                            application_workspace_id=application_workspace_id, requested_stage=requested_stage,
                            mode=mode, now=now, sentinel_present=sentinel, **context_kwargs)
        decision = evaluate_authorization(ctx)
        return decision, insert_decision(conn, ctx=ctx, decision=decision, commit=False)
    return run_immediate(conn, work)


def request_grant(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                  stage: Capability, now: datetime, fill_manifest: dict | None,
                  requirements: Sequence[RequirementSpec] = (), observation: ApplyTargetObservation | None = None,
                  run_id: str | None = None, cost_estimates: Mapping[str, Decimal] | None = None) -> GrantOutcome:
    if stage not in (Capability.FILL, Capability.SUBMIT):
        raise ValueError("grants exist only for FILL and SUBMIT")
    if fill_manifest is None:
        raise ValueError(f"a {stage.name} grant must bind a fill manifest")
    _check_manifest(fill_manifest, application_workspace_id=application_workspace_id, observation=observation)

    def work():
        _check_not_paused(conn, account_id=account_id, application_workspace_id=application_workspace_id)
        if stage == Capability.SUBMIT:
            _require_active_run(conn, account_id=account_id, run_id=run_id)
        sentinel = _observe_sentinel_in_transaction(conn, settings=settings, account_id=account_id, now=now)
        ctx = build_context(conn, settings=settings, account_id=account_id,
                            application_workspace_id=application_workspace_id, requested_stage=stage,
                            mode=Mode.LIVE, now=now, sentinel_present=sentinel, requirements=requirements,
                            observation=observation, run_id=run_id, cost_estimates=cost_estimates)
        decision = evaluate_authorization(ctx)
        row = insert_decision(conn, ctx=ctx, decision=decision, commit=False)
        if not decision.grantable:
            return GrantOutcome(decision, row, None)
        ttl = FILL_SESSION_TTL if stage == Capability.FILL else SUBMIT_GRANT_TTL
        target_url = autonomy_context.apply_target_url(conn, workspace_id=application_workspace_id,
                                                       account_id=account_id)
        grant = insert_grant(conn, decision_id=row["id"], account_id=account_id,
                             application_workspace_id=application_workspace_id, stage=stage,
                             binding=build_binding(ctx, stage=stage, fill_manifest=fill_manifest,
                                                   observation=observation, target_url=target_url),
                             issued_at=now, expires_at=now + ttl, commit=False)
        if stage == Capability.FILL:
            day, _ = day_window(now, ctx.standing_policy["timezone"])
            reserved = try_reserve(conn, account_id=account_id, counter_name="fill_per_day", window_key=day,
                                   limit=ctx.standing_policy["limits"]["fill_per_day"], now=now, grant_id=grant["id"])
            if reserved is None:  # impossible under BEGIN IMMEDIATE unless the gate is wrong
                raise RuntimeError("fill_per_day reservation failed after an ALLOW decision")
        for budget in ctx.budgets:
            key = day_window(now, ctx.standing_policy["timezone"])[0] if budget.window == "day" else application_workspace_id
            reserve_budget(conn, account_id=account_id, counter_name=f"budget:{budget.category}:{budget.window}",
                           window_key=key, amount=budget.estimate, grant_id=grant["id"], now=now)
        return GrantOutcome(decision, row, grant)
    return run_immediate(conn, work)


# ---- pre-click transaction and attempt lifecycle (spec §10.3, §10.4) --------

@dataclass(frozen=True)
class PreClickResult:
    authorized: bool
    attempt_id: str | None
    decision_id: str
    reason: str | None


def _verification_matches(fill_manifest: dict, verification: Mapping[str, str]) -> bool:
    expected = {e["page_field_key"]: e["value_hash"] for page in fill_manifest["pages"] for e in page["entries"]}
    return dict(verification) == expected


def _settle_reservations(conn, grant_id: str, status: str, now: datetime) -> None:
    for row in conn.execute("SELECT id FROM limit_reservations WHERE grant_id = ? AND status = 'RESERVED'",
                            (grant_id,)).fetchall():
        set_reservation_status(conn, reservation_id=row["id"], status=status, now=now)


def pre_click_commit(conn, *, settings: Settings, grant_id: str, verification: Mapping[str, str], now: datetime,
                     requirements: Sequence[RequirementSpec] = (), observation: ApplyTargetObservation | None = None,
                     run_id: str | None = None) -> PreClickResult:
    """Spec §10.3: one BEGIN IMMEDIATE transaction re-checks the kill switch and
    sentinel, re-evaluates against current state, compares with the grant
    binding and the verification snapshot, reserves limits, consumes the
    single-use grant, claims the intent and creates the AUTHORIZED attempt.
    Commit is the authorization point of no return -- not proof of submission.
    Any exception rolls back every write of the transaction."""
    initial = get_grant(conn, grant_id)
    if initial is None or initial["stage"] != "SUBMIT":
        raise ValueError(f"{grant_id!r} is not a SUBMIT grant")
    account_id, ws = initial["account_id"], initial["application_workspace_id"]

    def work() -> PreClickResult:
        _check_not_paused(conn, account_id=account_id, application_workspace_id=ws)
        _require_active_run(conn, account_id=account_id, run_id=run_id)
        sentinel = _observe_sentinel_in_transaction(conn, settings=settings, account_id=account_id, now=now)
        grant = get_grant(conn, grant_id)
        fill_manifest = grant["binding"]["fill_manifest"]
        ctx = build_context(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                            requested_stage=Capability.SUBMIT, mode=Mode.LIVE, now=now, sentinel_present=sentinel,
                            requirements=requirements, observation=observation, run_id=run_id)
        target_url = autonomy_context.apply_target_url(conn, workspace_id=ws, account_id=account_id)
        current = build_binding(ctx, stage=Capability.SUBMIT, fill_manifest=fill_manifest,
                                observation=observation, target_url=target_url)
        drift = list(binding_drift(grant["binding"], current))
        if not _verification_matches(fill_manifest, verification):
            drift.append("verification_snapshot")
        ctx = dataclasses.replace(ctx, grant_binding_drift=tuple(drift))
        decision = evaluate_authorization(ctx)
        row = insert_decision(conn, ctx=ctx, decision=decision, grant_id=grant_id, commit=False)
        if not decision.grantable:
            reason = decision.deny_reason or decision.result.value
            revoke_grant(conn, grant_id=grant_id, reason=f"pre_click:{reason}", now=now)
            return PreClickResult(False, None, row["id"], reason)
        if grant["status"] != "ISSUED" or parse_utc(grant["expires_at"]) <= now:
            return PreClickResult(False, None, row["id"], "grant_not_consumable")
        doc = ctx.standing_policy
        day, _ = day_window(now, doc["timezone"])
        limits = doc["limits"]
        reservations = [
            ("submit_per_day", day, min(limits["submit_per_day"], settings.autonomy_live_submit_daily_cap), None),
            ("submit_per_employer_30d", ctx.employer_key, limits["submit_per_employer_30d"],
             now - timedelta(days=30)),
        ]
        if "submit_per_run" in limits:
            reservations.append(("submit_per_run", run_id, limits["submit_per_run"], None))
        for name, key, limit, since in reservations:
            if try_reserve(conn, account_id=account_id, counter_name=name, window_key=key, limit=limit,
                           now=now, since=since, grant_id=grant_id) is None:
                raise RuntimeError(f"{name} reservation failed after an ALLOW decision")
        if not consume_grant(conn, grant_id=grant_id, now=now):
            raise RuntimeError("grant consumption failed after it was checked consumable")
        intent = claim_intent(conn, account_id=account_id, job_identity_key=ctx.identity_key,
                              application_workspace_id=ws, source="AUTONOMOUS", now=now)
        attempt = create_attempt(conn, grant_id=grant_id, intent_id=intent["id"], application_workspace_id=ws,
                                 run_id=run_id, now=now)
        set_intent_state(conn, intent_id=intent["id"], state="CLAIMED", now=now, attempt_id=attempt["id"])
        return PreClickResult(True, attempt["id"], row["id"], None)

    return run_immediate(conn, work)


def _attempt(conn, attempt_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM submission_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if row is None:
        raise LookupError(attempt_id)
    return dict(row)


def _intent_state(conn, intent_id: str) -> str:
    return conn.execute("SELECT state FROM submission_intents WHERE id = ?", (intent_id,)).fetchone()["state"]


def _release(conn, attempt: dict[str, Any], now: datetime) -> None:
    if _intent_state(conn, attempt["intent_id"]) == "CLAIMED":  # never release a human-CONFIRMED intent
        set_intent_state(conn, intent_id=attempt["intent_id"], state="RELEASED", now=now)
    _settle_reservations(conn, attempt["grant_id"], "RELEASED", now)


def _confirm(conn, attempt: dict[str, Any], now: datetime) -> None:
    if _intent_state(conn, attempt["intent_id"]) == "CLAIMED":
        set_intent_state(conn, intent_id=attempt["intent_id"], state="CONFIRMED", now=now)
    _settle_reservations(conn, attempt["grant_id"], "CONSUMED", now)


def record_click_dispatched(conn, *, attempt_id: str, now: datetime) -> bool:
    """Server acknowledgement that CLICK_DISPATCHED is durable. The executor
    must not click unless this returns True."""
    def work() -> bool:
        attempt = _attempt(conn, attempt_id)
        if attempt_state(conn, attempt_id) != "AUTHORIZED":
            return False
        if _intent_state(conn, attempt["intent_id"]) != "CLAIMED":
            append_attempt_event(conn, attempt_id=attempt_id, state="EXPIRED_UNCLICKED", source="SERVER",
                                 evidence={"reason": "intent_confirmed_by_human"}, now=now)
            _release(conn, attempt, now)
            return False
        if now - parse_utc(attempt["created_at"]) >= CLICK_DISPATCH_TTL:
            append_attempt_event(conn, attempt_id=attempt_id, state="EXPIRED_UNCLICKED", source="SERVER",
                                 evidence={"reason": "dispatch_ttl_elapsed"}, now=now)
            _release(conn, attempt, now)
            return False
        append_attempt_event(conn, attempt_id=attempt_id, state="CLICK_DISPATCHED", source="EXECUTOR",
                             evidence={}, now=now)
        return True
    return run_immediate(conn, work)


def _attempts_in_state(conn, state: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT a.* FROM submission_attempts a WHERE ("
        "  SELECT e.state FROM submission_attempt_events e WHERE e.attempt_id = a.id ORDER BY e.seq DESC LIMIT 1"
        ") = ? ORDER BY a.seq", (state,),
    ).fetchall()
    return [dict(r) for r in rows]


def expire_unclicked(conn, *, now: datetime) -> int:
    def work() -> int:
        expired = 0
        for attempt in _attempts_in_state(conn, "AUTHORIZED"):
            if now - parse_utc(attempt["created_at"]) >= CLICK_DISPATCH_TTL:
                append_attempt_event(conn, attempt_id=attempt["id"], state="EXPIRED_UNCLICKED", source="SERVER",
                                     evidence={"reason": "dispatch_ttl_elapsed"}, now=now)
                _release(conn, attempt, now)
                expired += 1
        return expired
    return run_immediate(conn, work)


def mark_stale_dispatches_ambiguous(conn, *, now: datetime, result_timeout: timedelta) -> int:
    def work() -> int:
        marked = 0
        for attempt in _attempts_in_state(conn, "CLICK_DISPATCHED"):
            dispatched_at = conn.execute(
                "SELECT created_at FROM submission_attempt_events WHERE attempt_id = ? AND state = 'CLICK_DISPATCHED' "
                "ORDER BY seq DESC LIMIT 1", (attempt["id"],),
            ).fetchone()["created_at"]
            if now - parse_utc(dispatched_at) >= result_timeout:
                append_attempt_event(conn, attempt_id=attempt["id"], state="SUBMISSION_AMBIGUOUS", source="SERVER",
                                     evidence={"reason": "no_result_within_timeout"}, now=now)
                marked += 1
        return marked
    return run_immediate(conn, work)


def record_submission_result(conn, *, attempt_id: str, state: str, source: str, evidence: dict[str, Any],
                             now: datetime) -> str:
    if state not in ("CONFIRMED_SUCCESS", "SUBMISSION_AMBIGUOUS", "SUBMISSION_FAILED"):
        raise ValueError(state)

    def work() -> str:
        attempt = _attempt(conn, attempt_id)
        recorded = state
        if state == "SUBMISSION_FAILED" and evidence.get("proven_not_submitted") is not True:
            recorded = "SUBMISSION_AMBIGUOUS"  # spec §10.4: anything short of proof is ambiguous
        append_attempt_event(conn, attempt_id=attempt_id, state=recorded, source=source, evidence=evidence, now=now)
        if recorded == "CONFIRMED_SUCCESS":
            _confirm(conn, attempt, now)
        elif recorded == "SUBMISSION_FAILED":
            _release(conn, attempt, now)
        return recorded
    return run_immediate(conn, work)


def resolve_ambiguous(conn, *, attempt_id: str, submitted: bool, actor: str, now: datetime) -> str:
    def work() -> str:
        attempt = _attempt(conn, attempt_id)
        if attempt_state(conn, attempt_id) != "SUBMISSION_AMBIGUOUS":
            raise ValueError("only an ambiguous attempt can be resolved by the user")
        state = "CONFIRMED_SUCCESS" if submitted else "SUBMISSION_FAILED"
        append_attempt_event(conn, attempt_id=attempt_id, state=state, source="USER",
                             evidence={"attested_by": actor}, now=now)
        (_confirm if submitted else _release)(conn, attempt, now)
        return state
    return run_immediate(conn, work)
