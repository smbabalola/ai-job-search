"""The Bundle 6B authorization gate (spec §9).

evaluate_authorization is pure and deterministic: no I/O, no clock (ctx.now
is an input), no mutation, no limits consumed, no grants created. Every check
runs and leaves reasons (collect-then-resolve); the result is derived by the
fixed precedence of spec §9.4. Only the three ceilings can raise capability;
everything else applies min() or a non-ALLOW outcome.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from product.autonomy_contract import (
    CONTEXT_SCHEMA, CONTEXT_SCHEMA_VERSION, ENGINE_VERSION, REACH_ORDER,
    AnswerCandidate, AuthorizationContext, AuthorizationDecision, CanonicalHashError,
    Capability, EmployerKeyStrength, IdentityStrength, Mode, ProvenanceTier, Reach,
    Reason, RepresentationRequirement, RequireUserItem, ResultKind, canonical_hash,
    is_unknown, reason,
)
from product.semantic_subject_policy import (
    SubjectPolicyError, subject_entry, subject_policy_hash, validate_subject_policy,
)
from product.standing_policy import (
    StandingPolicyError, evaluate_rules, policy_hash, validate_standing_policy,
)

_SUBMIT_TIERS = {ProvenanceTier.DISCOVERY_VERIFIED, ProvenanceTier.USER_CONFIRMED_APPLY_TARGET}


def _aware(value: Any) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _bool_or_unknown(value: Any) -> bool:
    return isinstance(value, bool) or is_unknown(value)


class _Acc:
    def __init__(self, ceiling: Capability) -> None:
        self.cap = ceiling
        self.reasons: list[Reason] = []
        self.items: list[RequireUserItem] = []
        self.denies: set[str] = set()
        self.temporary: list[tuple[str, datetime | None]] = []
        self.blocked = False

    def reduce(self, level: Capability, code: str, **params: Any) -> None:
        self.reasons.append(reason(code, to=level.name, **params))
        self.cap = min(self.cap, level)

    def note(self, code: str, **params: Any) -> None:
        self.reasons.append(reason(code, **params))


def _context_errors(ctx: AuthorizationContext) -> list[str]:
    """Type/shape validation for everything the rest of the gate would need to
    inspect via attribute access (`.name`, `.value`, comparisons) or otherwise
    trust unconditionally. This function must never raise on a bad-type
    input -- every check here uses isinstance/containment, never attribute
    access on an unverified value. Anything not validated here and later
    accessed unconditionally (e.g. `ctx.deployment_ceiling.name`) is only
    reached once this function returns no errors, so the gate fails closed
    before any such access."""
    errors: list[str] = []
    now_ok = _aware(ctx.now)
    if not now_ok:
        errors.append("now_naive")

    if not isinstance(ctx.mode, Mode):
        errors.append("mode_invalid")

    if not isinstance(ctx.requested_stage, Capability):
        errors.append("requested_stage_invalid")
    elif ctx.requested_stage == Capability.NONE:
        errors.append("requested_stage_none")

    for name, value in (
        ("deployment_ceiling", ctx.deployment_ceiling),
        ("account_max", ctx.account_max),
        ("workspace_ceiling", ctx.workspace_ceiling),
    ):
        if not isinstance(value, Capability):
            errors.append(f"{name}_invalid")

    if not isinstance(ctx.identity_strength, IdentityStrength):
        errors.append("identity_strength_invalid")
    if not isinstance(ctx.employer_key_strength, EmployerKeyStrength):
        errors.append("employer_key_strength_invalid")

    for name, value in (
        ("kill_switch_engaged", ctx.kill_switch_engaged),
        ("sentinel_present", ctx.sentinel_present),
        ("governing_auto_reject", ctx.governing_auto_reject),
        ("pack_auto_confirmable", ctx.pack_auto_confirmable),
        ("identity_conflict", ctx.identity_conflict),
        ("intent_overridden", ctx.intent_overridden),
    ):
        if not _is_bool(value):
            errors.append(f"{name}_invalid")

    if ctx.existing_intent_state not in (None, "CLAIMED", "CONFIRMED"):
        errors.append("existing_intent_state_invalid")

    target = ctx.apply_target
    if target.provenance is not None and not isinstance(target.provenance, ProvenanceTier):
        errors.append("apply_target_provenance_invalid")
    if not _is_bool(target.adapter_submit_capable):
        errors.append("apply_target_adapter_submit_capable_invalid")
    for name, value in (
        ("landing_within_redirect_set", target.landing_within_redirect_set),
        ("tenant_matches_employer", target.tenant_matches_employer),
        ("unexplained_redirect", target.unexplained_redirect),
    ):
        if not _bool_or_unknown(value):
            errors.append(f"apply_target_{name}_invalid")
    if not (target.ats_job_id_matches is None or _bool_or_unknown(target.ats_job_id_matches)):
        errors.append("apply_target_ats_job_id_matches_invalid")

    for ack in ctx.rule_acknowledgements:
        if ack.disposition not in ("PROCEED", "DO_NOT_PROCEED"):
            errors.append(f"rule_acknowledgement_disposition_invalid:{ack.rule_id}")

    for req in ctx.requirements:
        if not _is_bool(req.required):
            errors.append(f"requirement_required_invalid:{req.key}")
        if not _is_bool(req.evidence_available):
            errors.append(f"requirement_evidence_available_invalid:{req.key}")
        for cand in req.candidates:
            if not _is_bool(cand.contradicted):
                errors.append(f"candidate_contradicted_invalid:{cand.approved_answer_id}")
            if cand.basis_kind not in ("EVIDENCE", "USER_ASSERTION"):
                errors.append(f"candidate_basis_kind_invalid:{cand.approved_answer_id}")
            if not isinstance(cand.reach, Reach):
                errors.append(f"candidate_reach_invalid:{cand.approved_answer_id}")
            if not _aware(cand.confirmed_at):
                errors.append(f"confirmed_at_naive:{cand.approved_answer_id}")
            elif now_ok and cand.confirmed_at > ctx.now:
                errors.append(f"confirmed_at_future:{cand.approved_answer_id}")

    for item in (*ctx.counters, *ctx.budgets):
        if item.retry_at is not None and not _aware(item.retry_at):
            errors.append("retry_at_naive")

    try:
        validate_subject_policy(ctx.subject_policy)
    except SubjectPolicyError:
        errors.append("subject_policy_invalid")
    if ctx.standing_policy is not None:
        try:
            validate_standing_policy(ctx.standing_policy)
        except StandingPolicyError:
            errors.append("standing_policy_invalid")
    return errors


def evaluate_authorization(ctx: AuthorizationContext) -> AuthorizationDecision:
    errors = _context_errors(ctx)
    try:
        fingerprint = canonical_hash(CONTEXT_SCHEMA, CONTEXT_SCHEMA_VERSION, ctx)
    except CanonicalHashError:
        errors.append("unhashable_context")
        fingerprint = canonical_hash(CONTEXT_SCHEMA, "invalid", {"errors": sorted(errors)})
    subject_hash = None if "subject_policy_invalid" in errors else subject_policy_hash(ctx.subject_policy)
    policy_version_hash = None
    if ctx.standing_policy is not None and "standing_policy_invalid" not in errors:
        policy_version_hash = policy_hash(ctx.standing_policy)
    if errors:
        return AuthorizationDecision(
            mode=ctx.mode, result=ResultKind.DENY, requested_stage=ctx.requested_stage,
            effective_capability=Capability.NONE, grantable=False, deny_reason="invalid_input",
            reasons=tuple(sorted(reason("invalid_input", detail=e) for e in errors)),
            require_user_items=(), retry_at=None, retryable=False, input_fingerprint=fingerprint,
            engine_version=ENGINE_VERSION, policy_version_hash=policy_version_hash,
            subject_policy_hash=subject_hash,
        )

    acc = _Acc(min(ctx.deployment_ceiling, ctx.account_max, ctx.workspace_ceiling))
    acc.note("ceiling", deployment=ctx.deployment_ceiling.name,
             account=ctx.account_max.name, workspace=ctx.workspace_ceiling.name)
    if ctx.mode is not Mode.LIVE:
        acc.note("non_live_mode", mode=ctx.mode.value)
    _apply_standing_policy(ctx, acc)
    _apply_identity(ctx, acc)
    _apply_target(ctx, acc)
    if ctx.employer_key_strength is EmployerKeyStrength.UNKNOWN or not ctx.employer_key:
        acc.reduce(Capability.FILL, "employer_key_unknown")
    if not ctx.pack_auto_confirmable or not ctx.pack_artifact_id:
        acc.reduce(Capability.PREPARE, "pack_not_auto_confirmable")
    _apply_requirements(ctx, acc)
    _apply_stops(ctx, acc)
    _apply_questions(ctx, acc)
    return _resolve(ctx, acc, fingerprint, policy_version_hash, subject_hash)


def _apply_standing_policy(ctx: AuthorizationContext, acc: _Acc) -> None:
    if ctx.standing_policy is None:
        acc.reduce(Capability.NONE, "standing_policy_missing")
        return
    acks = {a.rule_id: a for a in ctx.rule_acknowledgements}
    for outcome in evaluate_rules(ctx.standing_policy, ctx.attributes):
        effect = outcome.applied_effect
        if effect is None:
            continue
        via = "unknown" if outcome.via_unknown else "match"
        if effect["type"] == "REDUCE_TO":
            acc.reduce(Capability[effect["level"]], "rule_reduce", rule=outcome.rule_id, via=via)
        elif effect["type"] == "BLOCK":
            acc.blocked = True
            acc.note("rule_block", rule=outcome.rule_id, via=via)
        else:
            ack = acks.get(outcome.rule_id)
            valid = (ack is not None and ack.rule_hash == outcome.rule_hash
                     and ack.observed_fingerprint == outcome.observed_fingerprint)
            if valid and ack.disposition == "DO_NOT_PROCEED":
                acc.blocked = True
                acc.note("rule_acknowledged_do_not_proceed", rule=outcome.rule_id)
            elif valid:
                acc.note("rule_acknowledged_proceed", rule=outcome.rule_id)
            else:
                if ack is not None:
                    acc.note("rule_acknowledgement_lapsed", rule=outcome.rule_id)
                acc.items.append(RequireUserItem("rule", outcome.rule_id))
                acc.note("rule_require_user", rule=outcome.rule_id, via=via)


def _apply_identity(ctx: AuthorizationContext, acc: _Acc) -> None:
    if not ctx.identity_key or ctx.identity_strength is IdentityStrength.WEAK:
        acc.reduce(Capability.FILL, "identity_weak")
    if ctx.identity_conflict:
        acc.reduce(Capability.FILL, "identity_conflict")


def _negate(value: Any) -> Any:
    return value if is_unknown(value) or not isinstance(value, bool) else not value


def _apply_target(ctx: AuthorizationContext, acc: _Acc) -> None:
    target = ctx.apply_target
    if target.provenance is None:
        acc.reduce(Capability.PREPARE, "no_apply_target")
        return
    if target.provenance not in _SUBMIT_TIERS:
        acc.reduce(Capability.FILL, "apply_target_tier", tier=target.provenance.value)
        return
    if not target.adapter_submit_capable:
        acc.reduce(Capability.FILL, "adapter_not_submit_capable", adapter=target.adapter_id or "none")
        return
    checks = {
        "landing_within_redirect_set": target.landing_within_redirect_set,
        "tenant_matches_employer": target.tenant_matches_employer,
        "no_unexplained_redirect": _negate(target.unexplained_redirect),
    }
    if target.ats_job_id_matches is not None:
        checks["ats_job_id_matches"] = target.ats_job_id_matches
    for name, value in sorted(checks.items()):
        if value is False:
            acc.reduce(Capability.FILL, "target_mismatch", check=name)
            acc.items.append(RequireUserItem("apply_target", name))
        elif value is not True:
            acc.reduce(Capability.FILL, "target_unverified", check=name)


def _in_reach(cand: AnswerCandidate, entry: dict, ctx: AuthorizationContext) -> bool:
    if REACH_ORDER[cand.reach] > REACH_ORDER[Reach(entry["max_reach"])]:
        return False
    if cand.reach is Reach.ACCOUNT:
        return True
    if cand.reach is Reach.SEARCH_WORKSPACE:
        return ctx.search_workspace_id is not None and cand.scope_id == ctx.search_workspace_id
    return (ctx.employer_key_strength is not EmployerKeyStrength.UNKNOWN
            and ctx.employer_key is not None and cand.scope_id == ctx.employer_key)


def _job_value(req: RepresentationRequirement, key: str) -> Any:
    value = req.job_context.get(key)
    return None if value is None or is_unknown(value) else value


def _context_known_different(cand: AnswerCandidate, req: RepresentationRequirement, entry: dict) -> bool:
    for key in entry["context_keys"]:
        mine, job = cand.context.get(key), _job_value(req, key)
        if mine is not None and job is not None and mine != job:
            return True
    return False


def _submit_blocker(cand: AnswerCandidate, req: RepresentationRequirement, entry: dict, now: datetime) -> str | None:
    if not entry["submit_eligible"]:
        return "not_submit_eligible"
    days = entry["freshness_days"]
    if days is not None and now - cand.confirmed_at > timedelta(days=days):
        return "expired"
    if cand.basis_kind != "USER_ASSERTION" and (
        cand.basis_hash_current is None or cand.basis_hash_current != cand.basis_hash_at_approval
    ):
        return "basis_changed"
    for key in entry["context_keys"]:
        if cand.context.get(key) is None or _job_value(req, key) is None:
            return "context_unknown"
    return None


def _apply_requirements(ctx: AuthorizationContext, acc: _Acc) -> None:
    """Field/question items (spec §7, §9.3 step 5). Only for FILL/SUBMIT.
    Contradictions are raised at FILL and SUBMIT; other field items only at
    SUBMIT; any item is raised only if resolving it could reach the
    requested stage (relevance) -- otherwise it is a silent reason."""
    if ctx.requested_stage < Capability.FILL:
        return
    relevant = acc.cap >= ctx.requested_stage
    items: list[tuple[RequireUserItem, bool]] = []
    reductions: list[tuple[str, str]] = []
    for req in sorted(ctx.requirements, key=lambda r: r.key):
        entry = subject_entry(ctx.subject_policy, req.subject)
        if entry is None:
            if req.subject is None and req.evidence_available:
                continue
            if req.required:
                items.append((RequireUserItem("unclassified_field", req.key), False))
            else:
                acc.note("optional_omitted", field=req.key, why="unclassified")
            continue
        if entry["sensitive"] is not None:
            if req.required:
                items.append((RequireUserItem("sensitive_field", req.key), False))
            else:
                acc.note("optional_omitted", field=req.key, why="sensitive")
            continue
        if req.evidence_available:
            continue
        in_reach = [c for c in req.candidates if c.subject == req.subject and _in_reach(c, entry, ctx)]
        if any(c.contradicted for c in in_reach):
            items.append((RequireUserItem("contradicted_answer", req.key), True))
            continue
        usable = [c for c in in_reach if not _context_known_different(c, req, entry)]
        blockers = [_submit_blocker(c, req, entry, ctx.now) for c in usable]
        if any(b is None for b in blockers):
            continue
        if usable:
            reductions.append((req.key, blockers[0]))
            continue
        if req.required:
            items.append((RequireUserItem("missing_answer", req.key), False))
        else:
            acc.note("optional_omitted", field=req.key, why="no_answer")
    for item, raise_at_fill in items:
        applies = ctx.requested_stage == Capability.SUBMIT or raise_at_fill
        if applies and relevant:
            acc.items.append(item)
            acc.note("require_user", kind=item.kind, ref=item.ref)
        else:
            acc.note("unresolved_silent", kind=item.kind, ref=item.ref)
    for field, why in reductions:
        acc.reduce(Capability.FILL, "answer_not_submit_ready", field=field, why=why)


def _apply_stops(ctx: AuthorizationContext, acc: _Acc) -> None:
    if ctx.kill_switch_engaged:
        acc.denies.add("kill_switch")
        acc.note("kill_switch")
    if ctx.sentinel_present:
        acc.denies.add("kill_switch")
        acc.note("sentinel_present")
    for field in sorted(ctx.grant_binding_drift):
        acc.denies.add("stale_binding")
        acc.note("stale_binding", field=field)
    if ctx.governing_auto_reject:
        acc.blocked = True
        acc.note("governing_auto_reject")
    if ctx.existing_intent_state in ("CLAIMED", "CONFIRMED"):
        if ctx.intent_overridden:
            acc.note("duplicate_overridden", state=ctx.existing_intent_state)
        else:
            acc.denies.add("duplicate")
            acc.note("duplicate_intent", state=ctx.existing_intent_state)
    for counter in ctx.counters:
        if counter.stage == ctx.requested_stage and counter.used >= counter.limit:
            acc.temporary.append(("limit", counter.retry_at))
            acc.note("limit_reached", name=counter.name, used=counter.used, limit=counter.limit)
    for budget in ctx.budgets:
        if budget.used + budget.reserved + budget.estimate > budget.cap:
            acc.temporary.append(("budget", budget.retry_at))
            acc.note("budget_exceeded", category=budget.category, window=budget.window)


def _apply_questions(ctx: AuthorizationContext, acc: _Acc) -> None:
    for blocker_id in sorted(ctx.unresolved_governing_require_user):
        acc.items.append(RequireUserItem("governing_blocker", blocker_id))
        acc.note("governing_blocker", blocker=blocker_id)
    if ctx.requested_stage >= Capability.FILL:
        for stop in sorted(set(ctx.executor_hard_stops)):
            acc.items.append(RequireUserItem("hard_stop", stop))
            acc.note("hard_stop", stop=stop)


def _resolve(ctx: AuthorizationContext, acc: _Acc, fingerprint: str,
             policy_version_hash: str | None, subject_hash: str | None) -> AuthorizationDecision:
    items = tuple(sorted(set(acc.items)))
    retry_at = None
    deny_reason = None
    if acc.denies & {"kill_switch", "stale_binding"}:
        result = ResultKind.DENY
        deny_reason = "kill_switch" if "kill_switch" in acc.denies else "stale_binding"
    elif acc.blocked:
        result = ResultKind.BLOCK
    elif "duplicate" in acc.denies:
        result, deny_reason = ResultKind.DENY, "duplicate"
    elif acc.temporary:
        result = ResultKind.DENY_TEMPORARY
        deny_reason = "limit" if any(kind == "limit" for kind, _ in acc.temporary) else "budget"
        times = [t for _, t in acc.temporary]
        retry_at = max(times) if all(t is not None for t in times) else None
    elif items:
        result = ResultKind.REQUIRE_USER
    else:
        result = ResultKind.ALLOW
    grantable = (result is ResultKind.ALLOW and acc.cap >= ctx.requested_stage
                 and ctx.mode is Mode.LIVE)
    retryable = result is ResultKind.DENY_TEMPORARY
    return AuthorizationDecision(
        mode=ctx.mode, result=result, requested_stage=ctx.requested_stage,
        effective_capability=acc.cap, grantable=grantable, deny_reason=deny_reason,
        reasons=tuple(sorted(set(acc.reasons))), require_user_items=items, retry_at=retry_at,
        retryable=retryable, input_fingerprint=fingerprint, engine_version=ENGINE_VERSION,
        policy_version_hash=policy_version_hash, subject_policy_hash=subject_hash,
    )
