"""The Bundle 6B authorization gate (spec §9).

evaluate_authorization is pure and deterministic: no I/O, no clock (ctx.now
is an input), no mutation, no limits consumed, no grants created. Every check
runs and leaves reasons (collect-then-resolve); the result is derived by the
fixed precedence of spec §9.4. Only the three ceilings can raise capability;
everything else applies min() or a non-ALLOW outcome.

The gate must never raise for any malformed input (spec §9.3 step 1):
`_context_errors` validates every closed-schema field and container's type
before anything downstream trusts it unconditionally, and `evaluate_authorization`
wraps the whole evaluation in a final catch-all as defense in depth.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from product.autonomy_contract import (
    CONTEXT_SCHEMA, CONTEXT_SCHEMA_VERSION, ENGINE_VERSION, REACH_ORDER,
    AnswerCandidate, ApplyTargetFacts, AuthorizationContext, AuthorizationDecision,
    BudgetState, CanonicalHashError, Capability, CounterState, EmployerKeyStrength,
    IdentityStrength, Mode, ProvenanceTier, Reach, Reason, RepresentationRequirement,
    RequireUserItem, ResultKind, RuleAcknowledgement, canonical_hash, is_unknown, reason,
)
from product.semantic_subject_policy import (
    subject_entry, subject_policy_hash, validate_subject_policy,
)
from product.standing_policy import (
    evaluate_rules, policy_hash, validate_standing_policy,
)

_SUBMIT_TIERS = {ProvenanceTier.DISCOVERY_VERIFIED, ProvenanceTier.USER_CONFIRMED_APPLY_TARGET}


def _aware(value: Any) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bool_or_unknown(value: Any) -> bool:
    return isinstance(value, bool) or is_unknown(value)


def _finite_decimal(value: Any) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _tuple_of(value: Any, element_ok: Any) -> bool:
    """True iff value is a tuple/list and every element satisfies element_ok.
    Short-circuits on the container check so a non-container value (None,
    a string, ...) is never iterated."""
    return isinstance(value, (tuple, list)) and all(element_ok(v) for v in value)


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
    inspect via attribute access (`.name`, `.value`, comparisons, iteration)
    or otherwise trust unconditionally. This function must never raise on a
    bad-type input: every container is isinstance-checked (tuple/list) before
    its contents are ever iterated, and every element is isinstance-checked
    before any attribute on it is read. Anything not validated here and later
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

    if not isinstance(ctx.account_id, str):
        errors.append("account_id_invalid")
    if not isinstance(ctx.application_workspace_id, str):
        errors.append("application_workspace_id_invalid")

    # -- string-or-None identifiers: wrong type (e.g. an int) must not pass
    # through unchecked -- with no check here, a wrong-type identity/employer
    # key or run/pack/workspace id silently compares/hashes "successfully"
    # and the request can come out grantable.
    for name, value in (
        ("identity_key", ctx.identity_key),
        ("employer_key", ctx.employer_key),
        ("pack_artifact_id", ctx.pack_artifact_id),
        ("search_workspace_id", ctx.search_workspace_id),
        ("run_id", ctx.run_id),
    ):
        if value is not None and not isinstance(value, str):
            errors.append(f"{name}_invalid")

    if not isinstance(ctx.attributes, Mapping):
        errors.append("attributes_invalid")

    # -- apply_target: validate the container itself before any attribute access --
    target = ctx.apply_target
    if not isinstance(target, ApplyTargetFacts):
        errors.append("apply_target_invalid")
        target = None
    if target is not None:
        if target.provenance is not None and not isinstance(target.provenance, ProvenanceTier):
            errors.append("apply_target_provenance_invalid")
        if target.adapter_id is not None and not isinstance(target.adapter_id, str):
            errors.append("apply_target_adapter_id_invalid")
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

    # -- containers of opaque strings --
    for name, value in (
        ("executor_hard_stops", ctx.executor_hard_stops),
        ("grant_binding_drift", ctx.grant_binding_drift),
        ("unresolved_governing_require_user", ctx.unresolved_governing_require_user),
    ):
        if not _tuple_of(value, lambda v: isinstance(v, str)):
            errors.append(f"{name}_invalid")

    # -- rule acknowledgements --
    # Identifiers are validated by position (idx), not by the acknowledgement's
    # own rule_id, since rule_id itself may be the invalid field.
    if not _tuple_of(ctx.rule_acknowledgements, lambda a: isinstance(a, RuleAcknowledgement)):
        errors.append("rule_acknowledgements_invalid")
    else:
        for idx, ack in enumerate(ctx.rule_acknowledgements):
            if not isinstance(ack.rule_id, str):
                errors.append(f"rule_acknowledgement_rule_id_invalid:{idx}")
            if not isinstance(ack.rule_hash, str):
                errors.append(f"rule_acknowledgement_rule_hash_invalid:{idx}")
            if not isinstance(ack.observed_fingerprint, str):
                errors.append(f"rule_acknowledgement_observed_fingerprint_invalid:{idx}")
            if ack.disposition not in ("PROCEED", "DO_NOT_PROCEED"):
                errors.append(f"rule_acknowledgement_disposition_invalid:{ack.rule_id}")

    # -- representation requirements and their candidates --
    if not _tuple_of(ctx.requirements, lambda r: isinstance(r, RepresentationRequirement)):
        errors.append("requirements_invalid")
    else:
        for idx, req in enumerate(ctx.requirements):
            if not isinstance(req.key, str):
                errors.append(f"requirement_key_invalid:{idx}")
            if req.subject is not None and not isinstance(req.subject, str):
                errors.append(f"requirement_subject_invalid:{idx}")
            if not _is_bool(req.required):
                errors.append(f"requirement_required_invalid:{req.key}")
            if not _is_bool(req.evidence_available):
                errors.append(f"requirement_evidence_available_invalid:{req.key}")
            if not isinstance(req.job_context, Mapping):
                errors.append(f"job_context_invalid:{req.key}")
            elif not all(isinstance(k, str) for k in req.job_context):
                errors.append(f"job_context_keys_invalid:{idx}")
            if not _tuple_of(req.candidates, lambda c: isinstance(c, AnswerCandidate)):
                errors.append(f"candidates_invalid:{req.key}")
                continue
            for cand_idx, cand in enumerate(req.candidates):
                if not isinstance(cand.approved_answer_id, str):
                    errors.append(f"candidate_approved_answer_id_invalid:{idx}:{cand_idx}")
                if not isinstance(cand.subject, str):
                    errors.append(f"candidate_subject_invalid:{idx}:{cand_idx}")
                if cand.scope_id is not None and not isinstance(cand.scope_id, str):
                    errors.append(f"candidate_scope_id_invalid:{idx}:{cand_idx}")
                if cand.basis_hash_at_approval is not None and not isinstance(cand.basis_hash_at_approval, str):
                    errors.append(f"candidate_basis_hash_at_approval_invalid:{idx}:{cand_idx}")
                if cand.basis_hash_current is not None and not isinstance(cand.basis_hash_current, str):
                    errors.append(f"candidate_basis_hash_current_invalid:{idx}:{cand_idx}")
                if not _is_bool(cand.contradicted):
                    errors.append(f"candidate_contradicted_invalid:{cand.approved_answer_id}")
                if cand.basis_kind not in ("EVIDENCE", "USER_ASSERTION"):
                    errors.append(f"candidate_basis_kind_invalid:{cand.approved_answer_id}")
                if not isinstance(cand.reach, Reach):
                    errors.append(f"candidate_reach_invalid:{cand.approved_answer_id}")
                if not isinstance(cand.context, Mapping):
                    errors.append(f"candidate_context_invalid:{cand.approved_answer_id}")
                elif not all(isinstance(k, str) for k in cand.context):
                    errors.append(f"candidate_context_keys_invalid:{idx}:{cand_idx}")
                if not _aware(cand.confirmed_at):
                    errors.append(f"confirmed_at_naive:{cand.approved_answer_id}")
                elif now_ok and cand.confirmed_at > ctx.now:
                    errors.append(f"confirmed_at_future:{cand.approved_answer_id}")

    # -- counters --
    if not _tuple_of(ctx.counters, lambda c: isinstance(c, CounterState)):
        errors.append("counters_invalid")
    else:
        for idx, counter in enumerate(ctx.counters):
            if not isinstance(counter.name, str):
                errors.append(f"counter_name_invalid:{idx}")
            if not _is_int(counter.used):
                errors.append(f"counter_used_invalid:{counter.name}")
            if not _is_int(counter.limit):
                errors.append(f"counter_limit_invalid:{counter.name}")
            if not isinstance(counter.stage, Capability):
                errors.append(f"counter_stage_invalid:{counter.name}")
            if counter.retry_at is not None and not _aware(counter.retry_at):
                errors.append("retry_at_naive")

    # -- budgets --
    if not _tuple_of(ctx.budgets, lambda b: isinstance(b, BudgetState)):
        errors.append("budgets_invalid")
    else:
        for idx, budget in enumerate(ctx.budgets):
            if not isinstance(budget.category, str):
                errors.append(f"budget_category_invalid:{idx}")
            if not isinstance(budget.window, str):
                errors.append(f"budget_window_invalid:{idx}")
            for field_name in ("used", "reserved", "cap", "estimate"):
                if not _finite_decimal(getattr(budget, field_name)):
                    errors.append(f"budget_{field_name}_invalid:{budget.category}")
            if budget.retry_at is not None and not _aware(budget.retry_at):
                errors.append("retry_at_naive")

    if ctx.standing_policy is not None and not isinstance(ctx.standing_policy, Mapping):
        errors.append("standing_policy_not_mapping")
    if not isinstance(ctx.subject_policy, Mapping):
        errors.append("subject_policy_not_mapping")

    try:
        validate_subject_policy(ctx.subject_policy)
    except Exception:
        errors.append("subject_policy_invalid")
    if ctx.standing_policy is not None:
        try:
            validate_standing_policy(ctx.standing_policy)
        except Exception:
            errors.append("standing_policy_invalid")
    return errors


def _decision_mode(ctx: Any) -> Mode | None:
    """getattr with a default: ctx may not even be an AuthorizationContext
    (see evaluate_authorization's upfront check and its final catch-all)."""
    mode = getattr(ctx, "mode", None)
    return mode if isinstance(mode, Mode) else None


def _decision_stage(ctx: Any) -> Capability | None:
    stage = getattr(ctx, "requested_stage", None)
    return stage if isinstance(stage, Capability) else None


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return "<unrepr-able>"


def _error_reason(ctx: Any, code: str) -> Reason:
    """mode/requested_stage are the two fields the decision itself may null
    out (see _decision_mode/_decision_stage): keep the raw offending value in
    the reason so the audit trail never loses it. getattr with a default:
    ctx may not even be an AuthorizationContext."""
    if code == "mode_invalid":
        return reason("invalid_input", detail=code, raw=_safe_repr(getattr(ctx, "mode", None)))
    if code == "requested_stage_invalid":
        return reason("invalid_input", detail=code, raw=_safe_repr(getattr(ctx, "requested_stage", None)))
    return reason("invalid_input", detail=code)


def _safe_facts(ctx: Any) -> tuple[Reason, ...]:
    """Independent, always-safe-to-report facts surfaced alongside invalid_input
    (spec §9.3 step 1): a validly-typed engaged kill switch or present sentinel.
    Never raises: getattr with a default, then the same type check used in
    _context_errors (ctx may not even be an AuthorizationContext)."""
    extra: list[Reason] = []
    try:
        kill_switch_engaged = getattr(ctx, "kill_switch_engaged", None)
        if _is_bool(kill_switch_engaged) and kill_switch_engaged:
            extra.append(reason("kill_switch"))
        sentinel_present = getattr(ctx, "sentinel_present", None)
        if _is_bool(sentinel_present) and sentinel_present:
            extra.append(reason("sentinel_present"))
    except Exception:
        pass
    return tuple(extra)


def _invalid_decision(
    ctx: Any, errors: list[str],
    extra_reasons: tuple[Reason, ...] = (), fingerprint: str | None = None,
) -> AuthorizationDecision:
    """Builds the DENY(invalid_input) decision. Never raises regardless of
    what ctx actually is (a malformed AuthorizationContext, or not an
    AuthorizationContext at all -- see evaluate_authorization)."""
    if fingerprint is None:
        try:
            fingerprint = canonical_hash(CONTEXT_SCHEMA, CONTEXT_SCHEMA_VERSION, ctx)
        except Exception:
            fingerprint = canonical_hash(CONTEXT_SCHEMA, "invalid", {"errors": sorted(errors)})
    subject_hash = None
    try:
        sp = getattr(ctx, "subject_policy", None)
        if isinstance(sp, Mapping):
            validate_subject_policy(sp)
            subject_hash = subject_policy_hash(sp)
    except Exception:
        subject_hash = None
    policy_version_hash = None
    try:
        stp = getattr(ctx, "standing_policy", None)
        if isinstance(stp, Mapping):
            validate_standing_policy(stp)
            policy_version_hash = policy_hash(stp)
    except Exception:
        policy_version_hash = None
    reasons = tuple(sorted(set(extra_reasons) | {_error_reason(ctx, e) for e in errors}))
    return AuthorizationDecision(
        mode=_decision_mode(ctx), result=ResultKind.DENY, requested_stage=_decision_stage(ctx),
        effective_capability=Capability.NONE, grantable=False, deny_reason="invalid_input",
        reasons=reasons, require_user_items=(), retry_at=None, retryable=False,
        input_fingerprint=fingerprint, engine_version=ENGINE_VERSION,
        policy_version_hash=policy_version_hash, subject_policy_hash=subject_hash,
    )


def evaluate_authorization(ctx: AuthorizationContext) -> AuthorizationDecision:
    if not isinstance(ctx, AuthorizationContext):
        # The type hint promises an AuthorizationContext, but the gate must
        # still fail closed -- never raise -- if a caller passes anything
        # else (including None); mode/requested_stage are unknowable, so
        # both are None rather than guessed at (see _decision_mode/_decision_stage).
        return _invalid_decision(ctx, ["not_a_context"], _safe_facts(ctx))
    try:
        return _evaluate(ctx)
    except Exception as exc:  # final guard: the gate must never raise (spec §9.3 step 1)
        return _invalid_decision(ctx, [f"unexpected:{type(exc).__name__}"], _safe_facts(ctx))


def _evaluate(ctx: AuthorizationContext) -> AuthorizationDecision:
    errors = _context_errors(ctx)
    fingerprint: str | None
    try:
        fingerprint = canonical_hash(CONTEXT_SCHEMA, CONTEXT_SCHEMA_VERSION, ctx)
    except CanonicalHashError:
        errors.append("unhashable_context")
        fingerprint = None
    if errors:
        return _invalid_decision(ctx, errors, _safe_facts(ctx), fingerprint=fingerprint)

    # errors is empty: subject_policy and (if present) standing_policy are
    # already known valid, so these cannot raise or return None here.
    subject_hash = subject_policy_hash(ctx.subject_policy)
    policy_version_hash = policy_hash(ctx.standing_policy) if ctx.standing_policy is not None else None

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
    # Relevance baseline (spec §9.3 step 5 "Relevance"): the *structural* cap
    # -- ceilings and non-actionable reductions only (standing-policy
    # REDUCE_TO, identity, apply-target tier/adapter/verification, employer
    # key) -- snapshotted before the actionable reductions below (pack
    # confirmability, answer freshness) so an actionable reduction can never
    # suppress a field/question item that is otherwise relevant.
    structural_cap = acc.cap
    _apply_requirements(ctx, acc, structural_cap)
    if not ctx.pack_auto_confirmable or not ctx.pack_artifact_id:
        acc.reduce(Capability.PREPARE, "pack_not_auto_confirmable")
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
    """Apply-target reductions apply at every stage; the corresponding
    REQUIRE_USER items (page-identity mismatches) are stage-scoped to
    FILL/SUBMIT only (spec §9.3 step 5 "Stage scoping")."""
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
    item_eligible = ctx.requested_stage >= Capability.FILL
    for name, value in sorted(checks.items()):
        if value is False:
            acc.reduce(Capability.FILL, "target_mismatch", check=name)
            if item_eligible:
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


def _apply_requirements(ctx: AuthorizationContext, acc: _Acc, structural_cap: Capability) -> None:
    """Field/question items (spec §7, §9.3 step 5). Only for FILL/SUBMIT.
    Contradictions are raised at FILL and SUBMIT; other field items only at
    SUBMIT. Relevance is judged against structural_cap (ceilings and
    non-actionable reductions only, computed by the caller) -- an item is
    raised only if resolving it could reach the requested stage even with
    every actionable blocker cleared; otherwise it is a silent reason. A
    non-required field's own not-submit-ready answer (expired, stale-by-basis,
    context-unknown, not submit-eligible) is omitted, never a reduction --
    only a required field's does that (spec §9.3 step 3 "answers" bullet).

    Ruling K: a required field that SUBMIT needs and that has no permitted
    source at all -- missing_answer, contradicted_answer, unclassified_field,
    sensitive_field -- caps capability at FILL exactly as an expired required
    answer does (the `reductions` loop below), independent of whether the
    item ends up surfaced or silent (an actionable reduction, applied after
    relevance/structural_cap are already fixed, so it can never affect them).
    Applied whenever requirements are evaluated at all (requested_stage >=
    FILL, this function's own entry guard below), not only at SUBMIT:
    effective_capability always states the highest level that can actually
    proceed *now*, for any requested stage, and a worse answer state must
    never record a higher capability than a better one would (spec §9.3
    step 3, and the FILL-is-authority-not-completeness note in §9.5) --
    gating this on requested_stage == SUBMIT would let a FILL request quietly
    over-report capability for a required field that is, in fact, missing."""
    if ctx.requested_stage < Capability.FILL:
        return
    relevant = structural_cap >= ctx.requested_stage
    items: list[tuple[RequireUserItem, bool]] = []
    reductions: list[tuple[str, str]] = []
    unresolved_required: list[tuple[str, str]] = []
    for req in sorted(ctx.requirements, key=lambda r: r.key):
        entry = subject_entry(ctx.subject_policy, req.subject)
        if entry is None:
            if req.subject is None and req.evidence_available:
                continue
            if req.required:
                items.append((RequireUserItem("unclassified_field", req.key), False))
                unresolved_required.append((req.key, "unclassified_field"))
            else:
                acc.note("optional_omitted", field=req.key, why="unclassified")
            continue
        if entry["sensitive"] is not None:
            if req.required:
                items.append((RequireUserItem("sensitive_field", req.key), False))
                unresolved_required.append((req.key, "sensitive_field"))
            else:
                acc.note("optional_omitted", field=req.key, why="sensitive")
            continue
        if req.evidence_available:
            continue
        in_reach = [c for c in req.candidates if c.subject == req.subject and _in_reach(c, entry, ctx)]
        if any(c.contradicted for c in in_reach):
            items.append((RequireUserItem("contradicted_answer", req.key), True))
            if req.required:
                unresolved_required.append((req.key, "contradicted_answer"))
            continue
        usable = [c for c in in_reach if not _context_known_different(c, req, entry)]
        blockers = [_submit_blocker(c, req, entry, ctx.now) for c in usable]
        if any(b is None for b in blockers):
            continue
        if usable:
            if req.required:
                reductions.append((req.key, blockers[0]))
            else:
                acc.note("optional_omitted", field=req.key, why=blockers[0])
            continue
        if req.required:
            items.append((RequireUserItem("missing_answer", req.key), False))
            unresolved_required.append((req.key, "missing_answer"))
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
    for field, kind in unresolved_required:
        acc.reduce(Capability.FILL, "required_field_unresolved", field=field, kind=kind)


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
    # A CLAIMED (in-flight) intent always denies -- overrides apply only to a
    # CONFIRMED submission, never to an in-flight claim (spec §9.3 step 4).
    if ctx.existing_intent_state == "CLAIMED":
        acc.denies.add("duplicate")
        acc.note("duplicate_intent", state=ctx.existing_intent_state)
    elif ctx.existing_intent_state == "CONFIRMED":
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
