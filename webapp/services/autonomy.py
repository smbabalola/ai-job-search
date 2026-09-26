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
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from product.autonomy_contract import (
    ENGINE_VERSION, FILL_SESSION_TTL, SUBMIT_GRANT_TTL, AuthorizationContext, AuthorizationDecision,
    Capability, Mode, canonical_json, to_utc_iso,
)
from product.autonomy_gate import evaluate_authorization
from product.fill_manifest import manifest_hash, validate_fill_manifest
from product.semantic_subject_policy import subject_policy_hash
from product.standing_policy import policy_hash
from webapp.config import Settings
from webapp.persistence.autonomy_authority import is_paused, kill_switch_state
from webapp.persistence.autonomy_ledger import insert_decision, insert_grant, reserve_budget, try_reserve
from webapp.services import autonomy_context
from webapp.services.autonomy_context import (
    ApplyTargetObservation, RequirementSpec, build_context, day_window,
)
from webapp.services.autonomy_controls import engage_kill_switch_in_transaction, run_immediate, sentinel_present


class AutonomyPaused(Exception):
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
