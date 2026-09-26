"""Pure pre-application admission and promotion screening (6C spec §7).
Reuses 6B authority and standing-policy semantics over candidate
attributes; never fabricates an application workspace or a 6B decision."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from product.autonomy_contract import Capability, IdentityStrength, canonical_hash
from product.standing_policy import evaluate_rules, policy_hash

ENGINE_VERSION = "candidate-promotion.v1"
ELIGIBLE_STATES = frozenset({"new", "saved"})
FINISHED_RUNS = frozenset({"completed", "partial"})


class ScreeningOutcome(str, Enum):
    PROMOTE = "PROMOTE"
    REQUIRE_USER = "REQUIRE_USER"
    BLOCK = "BLOCK"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    DENY = "DENY"
    DENY_TEMPORARY = "DENY_TEMPORARY"


@dataclass(frozen=True)
class CandidateContext:
    now: datetime
    account_id: str
    search_workspace_id: str
    candidate_id: str
    scheduler_enabled: bool
    kill_switch_engaged: bool
    sentinel_present: bool
    search_workspace_active: bool
    paused: bool
    deployment_ceiling: Capability
    account_max: Capability
    workspace_ceiling: Capability
    candidate_state: str
    run_status: str | None
    identity_key: str | None
    identity_strength: IdentityStrength
    existing_application: bool
    existing_intent: bool
    fit_present: bool
    fit_fresh: bool
    discovery_fit_id: str | None
    standing_policy: Mapping[str, Any] | None
    attributes: Mapping[str, Any]
    llm_budget_configured: bool
    evaluate_envelope_present: bool
    budget_available: bool
    budget_retry_at: datetime | None
    promotions_available: bool
    promotions_retry_at: datetime | None


@dataclass(frozen=True)
class AdmissionResult:
    admitted: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ScreeningResult:
    outcome: ScreeningOutcome
    reason_code: str
    reasons: tuple[dict[str, Any], ...]
    require_user: tuple[dict[str, str], ...]
    could_unlock: bool
    retry_at: datetime | None
    input_fingerprint: str
    policy_version_hash: str | None
    authority: dict[str, str]


def candidate_input_fingerprint(ctx: CandidateContext) -> str:
    payload = {f.name: getattr(ctx, f.name) for f in dataclasses.fields(ctx) if f.name != "now"}
    payload["standing_policy"] = policy_hash(ctx.standing_policy) if ctx.standing_policy is not None else None
    return canonical_hash("autonomy-candidate-context", "v1", payload)


def _ceiling(ctx: CandidateContext) -> Capability:
    return min(ctx.deployment_ceiling, ctx.account_max, ctx.workspace_ceiling)


def _strong(ctx: CandidateContext) -> bool:
    return bool(ctx.identity_key) and ctx.identity_strength is not IdentityStrength.WEAK


def admit_candidate_evaluation(ctx: CandidateContext) -> AdmissionResult:
    """Fresh admission before a paid EVALUATE (spec §7.1). Not the screening."""
    checks = [
        (ctx.scheduler_enabled, "scheduler_disabled"),
        (not (ctx.kill_switch_engaged or ctx.sentinel_present), "halted"),
        (ctx.search_workspace_active, "search_workspace_inactive"),
        (not ctx.paused, "paused"),
        (_ceiling(ctx) >= Capability.PREPARE, "capability_below_prepare"),
        (ctx.candidate_state in ELIGIBLE_STATES, "candidate_state"),
        (ctx.run_status in FINISHED_RUNS, "run_not_finished"),
        (_strong(ctx), "weak_identity"),
        (not ctx.existing_application, "existing_application"),
        (not ctx.existing_intent, "existing_intent"),
        (ctx.llm_budget_configured, "llm_budget_missing"),
        (ctx.evaluate_envelope_present, "evaluate_envelope_missing"),
        (ctx.budget_available, "budget_exhausted"),
    ]
    reasons = tuple(reason for ok, reason in checks if not ok)
    return AdmissionResult(admitted=not reasons, reasons=reasons)


def evaluate_candidate_promotion(ctx: CandidateContext) -> ScreeningResult:
    fingerprint = candidate_input_fingerprint(ctx)
    ceiling = _ceiling(ctx)
    authority = {"deployment": ctx.deployment_ceiling.name, "account": ctx.account_max.name,
                 "workspace": ctx.workspace_ceiling.name}
    p_hash = policy_hash(ctx.standing_policy) if ctx.standing_policy is not None else None
    reasons: list[dict[str, Any]] = [{"code": "ceiling", "params": authority}]

    def result(outcome, code, *, require=(), could_unlock=False, retry_at=None):
        return ScreeningResult(outcome, code, tuple(reasons), tuple(require), could_unlock, retry_at,
                               fingerprint, p_hash, authority)

    if ctx.kill_switch_engaged or ctx.sentinel_present:
        reasons.append({"code": "kill_switch", "params": {}})
        return result(ScreeningOutcome.DENY, "kill_switch")

    cap, blocked, require = ceiling, False, []
    if ctx.standing_policy is not None:
        for outcome in evaluate_rules(ctx.standing_policy, ctx.attributes):
            effect = outcome.applied_effect
            if effect is None:
                continue
            via = "unknown" if outcome.via_unknown else "match"
            if (outcome.via_unknown and outcome.declared_effect.get("type") == "REDUCE_TO"
                    and effect["type"] in ("REQUIRE_USER", "BLOCK")):
                cap = min(cap, Capability[outcome.declared_effect["level"]])  # ruling N
            if effect["type"] == "REDUCE_TO":
                cap = min(cap, Capability[effect["level"]])
                reasons.append({"code": "rule_reduce", "params": {"rule": outcome.rule_id, "via": via}})
            elif effect["type"] == "BLOCK":
                blocked = True
                reasons.append({"code": "rule_block", "params": {"rule": outcome.rule_id, "via": via}})
            else:
                require.append({"kind": "rule", "ref": outcome.rule_id})
                reasons.append({"code": "rule_require_user", "params": {"rule": outcome.rule_id, "via": via}})
    if blocked:
        return result(ScreeningOutcome.BLOCK, "rule_block")

    structural = [code for ok, code in [
        (ctx.standing_policy is not None, "standing_policy_missing"),
        (ctx.search_workspace_active, "search_workspace_inactive"),
        (ctx.candidate_state in ELIGIBLE_STATES, "candidate_state"),
        (ctx.run_status in FINISHED_RUNS, "run_not_finished"),
        (_strong(ctx), "weak_identity"),
        (not ctx.existing_application, "existing_application"),
        (not ctx.existing_intent, "existing_intent"),
        (ctx.fit_present, "fit_missing"),
        (ctx.fit_fresh, "fit_stale"),
        (cap >= Capability.PREPARE, "capability_below_prepare"),
        (ctx.llm_budget_configured, "llm_budget_missing"),
    ] if not ok]
    for code in structural:
        reasons.append({"code": code, "params": {}})
    if structural:
        return result(ScreeningOutcome.NOT_ELIGIBLE, structural[0], require=require)

    if not ctx.promotions_available:
        reasons.append({"code": "promotions_cap", "params": {}})
        return result(ScreeningOutcome.DENY_TEMPORARY, "promotions_cap", require=require,
                      retry_at=ctx.promotions_retry_at)
    if require:
        return result(ScreeningOutcome.REQUIRE_USER, "rule_require_user", require=require, could_unlock=True)
    return result(ScreeningOutcome.PROMOTE, "promote")
