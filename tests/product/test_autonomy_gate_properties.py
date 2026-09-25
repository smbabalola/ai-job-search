# tests/product/test_autonomy_gate_properties.py
"""Property-based tests for the authorization gate (spec §17).

Fix round 1 (2026-09-25, HEAD a43dea9): widened for rulings K and L --
K: a required field with no permitted source caps capability at FILL
   whenever requirements are evaluated (FILL or SUBMIT), not only at SUBMIT;
L: a BLOCK rule's on_unknown may only be BLOCK or REQUIRE_USER.
See task-7-report.md for the full case analysis behind every guard below.
"""
from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal

from hypothesis import given, settings, strategies as st

from product.autonomy_contract import (
    BudgetState, Capability, CounterState, EmployerKeyStrength,
    IdentityStrength, Mode, ProvenanceTier, RepresentationRequirement, ResultKind,
    RuleAcknowledgement, UNKNOWN,
)
from product.autonomy_gate import evaluate_authorization
from product.standing_policy import observed_fingerprint, referenced_attributes, rule_hash
from tests.product.autonomy_fixtures import NOW, answer, good_target, make_ctx, make_policy

C = Capability
levels = st.sampled_from(list(C))
stages = st.sampled_from([C.PREPARE, C.FILL, C.SUBMIT])

REDUCIBLE_LEVELS = ["NONE", "PREPARE", "FILL"]
ATTRS = ["fit.overall_score", "job.employment_type", "company.key"]
EMPLOYER_LIST_NAME = "deny"

FRESHNESS_SUBJECT = "employment.notice_period"  # freshness_days=60, ACCOUNT reach, no context_keys
SENSITIVE_SUBJECT = "demographic.eeo"  # sensitive, submit_eligible=False


# ---------------------------------------------------------------------------
# Deterministic requirement-state builders, shared between the generator and
# the dedicated chain properties below (item 2), so both exercise exactly the
# same states the pinned unit tests in test_autonomy_gate_representation.py do.
# ---------------------------------------------------------------------------

def _req_fresh(key, required):
    return RepresentationRequirement(key=key, subject=FRESHNESS_SUBJECT, required=required,
                                      evidence_available=False,
                                      candidates=(answer(FRESHNESS_SUBJECT, confirmed_at=NOW),))


def _req_expired(key, required):
    return RepresentationRequirement(key=key, subject=FRESHNESS_SUBJECT, required=required,
                                      evidence_available=False,
                                      candidates=(answer(FRESHNESS_SUBJECT, confirmed_at=NOW - timedelta(days=61)),))


def _req_missing(key, required):
    return RepresentationRequirement(key=key, subject=FRESHNESS_SUBJECT, required=required,
                                      evidence_available=False, candidates=())


def _req_contradicted(key, required):
    return RepresentationRequirement(key=key, subject=FRESHNESS_SUBJECT, required=required,
                                      evidence_available=False,
                                      candidates=(answer(FRESHNESS_SUBJECT, contradicted=True),))


def _req_unclassified(key, required):
    return RepresentationRequirement(key=key, subject=None, required=required,
                                      evidence_available=False, candidates=())


def _req_sensitive(key, required):
    return RepresentationRequirement(key=key, subject=SENSITIVE_SUBJECT, required=required,
                                      evidence_available=False, candidates=())


def _req_evidence(key, required):
    return RepresentationRequirement(key=key, subject=FRESHNESS_SUBJECT, required=required,
                                      evidence_available=True, candidates=())


WORST_TIER = [("missing", _req_missing), ("contradicted", _req_contradicted),
              ("unclassified", _req_unclassified), ("sensitive", _req_sensitive)]
ALL_STATES = [("fresh", _req_fresh), ("expired", _req_expired), *WORST_TIER]


@st.composite
def requirement(draw, key):
    required = draw(st.booleans())
    kind = draw(st.sampled_from(
        ["fresh", "expired", "missing", "contradicted", "unclassified", "sensitive", "evidence"]))
    factory = {"fresh": _req_fresh, "expired": _req_expired, "missing": _req_missing,
               "contradicted": _req_contradicted, "unclassified": _req_unclassified,
               "sensitive": _req_sensitive, "evidence": _req_evidence}[kind]
    return factory(key, required)


# ---------------------------------------------------------------------------
# Standing-policy generation: predicates (with combinators and in_list),
# rules (with the full valid on_unknown combination per effect type, ruling
# L), and rule acknowledgements (matching, and lapsed).
# ---------------------------------------------------------------------------

@st.composite
def _leaf_predicate(draw, lists):
    attr = draw(st.sampled_from(ATTRS))
    if attr == "fit.overall_score":
        return {"attr": attr, "op": "lt", "value": draw(st.integers(0, 100))}
    if attr == "company.key" and lists and draw(st.booleans()):
        return {"attr": attr, "op": "in_list", "value": draw(st.sampled_from(sorted(lists)))}
    return {"attr": attr, "op": "eq", "value": draw(st.sampled_from(["PERMANENT", "CONTRACT", "name:acme"]))}


@st.composite
def _predicate(draw, lists, depth=0):
    if depth >= 2 or draw(st.booleans()):
        return draw(_leaf_predicate(lists))
    kind = draw(st.sampled_from(["all", "any", "not"]))
    if kind == "not":
        return {"not": draw(_predicate(lists, depth + 1))}
    children = [draw(_predicate(lists, depth + 1)) for _ in range(draw(st.integers(1, 2)))]
    return {kind: children}


def _on_unknown_choices(effect_type):
    if effect_type == "BLOCK":
        return [{"type": "BLOCK"}, {"type": "REQUIRE_USER"}]
    return ([{"type": "REQUIRE_USER"}, {"type": "BLOCK"}, {"type": "NO_EFFECT"}]
            + [{"type": "REDUCE_TO", "level": lvl} for lvl in REDUCIBLE_LEVELS])


@st.composite
def rules(draw, lists):
    count = draw(st.integers(0, 3))
    out = []
    for i in range(count):
        pred = draw(_predicate(lists))
        effect_type = draw(st.sampled_from(["REDUCE_TO", "REQUIRE_USER", "BLOCK"]))
        effect = ({"type": "REDUCE_TO", "level": draw(st.sampled_from(REDUCIBLE_LEVELS))}
                  if effect_type == "REDUCE_TO" else {"type": effect_type})
        on_unknown = draw(st.sampled_from(_on_unknown_choices(effect_type)))
        out.append({"id": f"r{i}", "description": "", "when": pred, "effect": effect, "on_unknown": on_unknown})
    return out


@st.composite
def maybe_ack(draw, rules_list, attributes, employer_lists):
    if not rules_list or not draw(st.booleans()):
        return ()
    r = draw(st.sampled_from(rules_list))
    rh, fp = rule_hash(r), observed_fingerprint(r, attributes, employer_lists)
    if not draw(st.booleans()):  # lapsed: rule content or observed attrs/lists changed since ack
        if draw(st.booleans()):
            rh += "_stale"
        else:
            fp += "_stale"
    disposition = draw(st.sampled_from(["PROCEED", "DO_NOT_PROCEED"]))
    return (RuleAcknowledgement(rule_id=r["id"], rule_hash=rh, observed_fingerprint=fp, disposition=disposition),)


# ---------------------------------------------------------------------------
# The base-context generator (item 1 and item 4): widened over answer-state
# dimensions, rule acknowledgements, employer key strength, apply-target
# verification flags, intent state, counters/budgets, hard stops, drift and
# identity conflict, plus attributes that may already be UNKNOWN.
# ---------------------------------------------------------------------------

@st.composite
def contexts(draw):
    attributes = {
        "fit.overall_score": draw(st.integers(0, 100)),
        "job.employment_type": draw(st.sampled_from(["PERMANENT", "CONTRACT"])),
        "company.key": "name:acme",
    }
    if draw(st.booleans()):
        attributes[draw(st.sampled_from(ATTRS))] = UNKNOWN

    employer_lists = {EMPLOYER_LIST_NAME: draw(st.sampled_from([[], ["name:acme"], ["name:other"]]))}
    rules_list = draw(rules(employer_lists))
    policy = make_policy(*rules_list, lists=employer_lists)
    acks = draw(maybe_ack(rules_list, attributes, employer_lists))

    bool_or_unknown = st.sampled_from([True, False, UNKNOWN])
    apply_target = good_target(
        provenance=draw(st.sampled_from([None, *ProvenanceTier])),
        adapter_submit_capable=draw(st.booleans()),
        landing_within_redirect_set=draw(bool_or_unknown),
        tenant_matches_employer=draw(bool_or_unknown),
        unexplained_redirect=draw(bool_or_unknown),
        ats_job_id_matches=draw(st.sampled_from([None, True, False, UNKNOWN])),
    )

    requirements = tuple(draw(requirement(f"req{i}")) for i in range(draw(st.integers(0, 2))))

    counters = tuple(
        CounterState(f"c{i}", draw(stages), draw(st.integers(0, 5)), draw(st.integers(1, 5)), None)
        for i in range(draw(st.integers(0, 2)))
    )
    budgets = tuple(
        BudgetState(draw(st.sampled_from(["LLM", "BROWSER", "EXTERNAL_API", "OTHER"])), "daily",
                    Decimal(draw(st.integers(0, 100))), Decimal(0), Decimal(draw(st.integers(0, 100))),
                    Decimal(0), None)
        for _ in range(draw(st.integers(0, 2)))
    )
    executor_hard_stops = tuple(draw(st.lists(
        st.sampled_from(["captcha", "login_wall", "email_verification"]), max_size=2, unique=True)))
    grant_binding_drift = tuple(draw(st.lists(
        st.sampled_from(["pack_hash", "manifest_hash"]), max_size=2, unique=True)))

    return make_ctx(
        requested_stage=draw(stages),
        deployment_ceiling=draw(levels), account_max=draw(levels), workspace_ceiling=draw(levels),
        standing_policy=policy,
        attributes=attributes,
        identity_strength=draw(st.sampled_from(list(IdentityStrength))),
        identity_conflict=draw(st.booleans()),
        apply_target=apply_target,
        pack_auto_confirmable=draw(st.booleans()),
        requirements=requirements,
        rule_acknowledgements=acks,
        employer_key_strength=draw(st.sampled_from(list(EmployerKeyStrength))),
        employer_key=draw(st.sampled_from([None, "name:acme"])),
        existing_intent_state=draw(st.sampled_from([None, "CLAIMED", "CONFIRMED"])),
        intent_overridden=draw(st.booleans()),
        counters=counters,
        budgets=budgets,
        executor_hard_stops=executor_hard_stops,
        grant_binding_drift=grant_binding_drift,
    )


# ---------------------------------------------------------------------------
# Unknown-safety scoping (item 5): "on_unknown at least as restrictive as
# effect" now that on_unknown is drawn independently of effect. Capability
# and result are separate axes -- BLOCK never touches capability, REDUCE_TO
# never blocks the result on its own -- so both must be checked.
# ---------------------------------------------------------------------------

def _cap_impact(effect):
    return Capability[effect["level"]] if effect["type"] == "REDUCE_TO" else None


def _restricts_result(effect):
    return effect["type"] in ("BLOCK", "REQUIRE_USER")


def _rule_safe_for_unknown(rule):
    effect, on_unknown = rule["effect"], rule["on_unknown"]
    e_cap, u_cap = _cap_impact(effect), _cap_impact(on_unknown)
    cap_safe = True if e_cap is None else (u_cap is not None and u_cap <= e_cap)
    result_safe = True if not _restricts_result(effect) else on_unknown["type"] in ("BLOCK", "REQUIRE_USER")
    return cap_safe and result_safe


def _attr_safe_for_unknown(ctx, attr):
    if ctx.standing_policy is None:
        return True
    return all(_rule_safe_for_unknown(rule) for rule in ctx.standing_policy["rules"]
               if attr in referenced_attributes(rule["when"]))


# ---------------------------------------------------------------------------
# Tightening operations (item 4): every op below is a genuine tightening --
# never a decrease in restriction -- for ANY starting context, including the
# now much more varied bases contexts() produces. See the inline comments
# for the case analysis behind each guard.
# ---------------------------------------------------------------------------

def tighten_ops(ctx):
    lower = lambda c: C(max(0, c - 1))
    yield dataclasses.replace(ctx, deployment_ceiling=lower(ctx.deployment_ceiling))
    yield dataclasses.replace(ctx, account_max=lower(ctx.account_max))
    yield dataclasses.replace(ctx, workspace_ceiling=lower(ctx.workspace_ceiling))
    yield dataclasses.replace(ctx, kill_switch_engaged=True)
    yield dataclasses.replace(ctx, sentinel_present=True)
    yield dataclasses.replace(ctx, identity_strength=IdentityStrength.WEAK)
    yield dataclasses.replace(ctx, identity_conflict=True)
    yield dataclasses.replace(ctx, pack_auto_confirmable=False)
    # §9.3 step 4: CLAIMED always denies; CONFIRMED denies unless overridden.
    # Switching CLAIMED -> CONFIRMED while intent_overridden is already True
    # would LOOSEN the decision (CLAIMED denies unconditionally; CONFIRMED
    # + override does not) -- skip only that one combination.
    if not (ctx.existing_intent_state == "CLAIMED" and ctx.intent_overridden):
        yield dataclasses.replace(ctx, existing_intent_state="CONFIRMED")
    yield dataclasses.replace(ctx, governing_auto_reject=True)
    # §8.1: "no target" already caps at PREPARE, stricter than imported_source's
    # FILL cap, so substituting IMPORTED_SOURCE is only a valid tightening when
    # a target already exists (see task-7-report.md's original NEEDS_CONTEXT).
    if ctx.apply_target.provenance is not None:
        yield dataclasses.replace(ctx, apply_target=good_target(provenance=ProvenanceTier.IMPORTED_SOURCE))
    # Append rather than replace: a varied base context may already carry its
    # own counters/blockers/drift/hard-stops, and replacing them outright
    # could silently drop an existing restriction -- a loosening.
    yield dataclasses.replace(
        ctx, counters=ctx.counters + (CounterState("tighten_x", ctx.requested_stage, 1, 1, None),))
    yield dataclasses.replace(
        ctx, unresolved_governing_require_user=ctx.unresolved_governing_require_user + ("tighten_blk",))
    yield dataclasses.replace(ctx, grant_binding_drift=ctx.grant_binding_drift + ("tighten_field",))
    yield dataclasses.replace(ctx, executor_hard_stops=ctx.executor_hard_stops + ("tighten_stop",))
    yield dataclasses.replace(ctx, budgets=ctx.budgets + (
        BudgetState("OTHER", "daily", Decimal(999), Decimal(0), Decimal(1), Decimal(0), None),))
    # An acknowledgement can only ever lift a restriction the user themselves
    # wrote (spec §9.3 step 5); removing it can only add restriction back.
    if ctx.rule_acknowledgements:
        yield dataclasses.replace(ctx, rule_acknowledgements=())
    yield dataclasses.replace(ctx, standing_policy=make_policy(
        *ctx.standing_policy["rules"],
        {"id": "extra", "description": "", "when": {"attr": "fit.overall_score", "op": "lt", "value": 101},
         "effect": {"type": "REDUCE_TO", "level": "PREPARE"}, "on_unknown": {"type": "REDUCE_TO", "level": "PREPARE"}},
        lists=ctx.standing_policy.get("employer_lists")))
    # Answer-state tightenings (spec §9.3 step 3 "answers" bullet): aging a
    # fresh answer past its subject's freshness window, or marking any
    # candidate contradicted, can only ever add a blocker or a REQUIRE_USER
    # item, never remove one -- safe unconditionally from any starting state.
    yield dataclasses.replace(ctx, requirements=tuple(
        dataclasses.replace(r, candidates=tuple(
            dataclasses.replace(c, confirmed_at=NOW - timedelta(days=99999)) for c in r.candidates))
        for r in ctx.requirements))
    yield dataclasses.replace(ctx, requirements=tuple(
        dataclasses.replace(r, candidates=tuple(
            dataclasses.replace(c, contradicted=True) for c in r.candidates))
        for r in ctx.requirements))
    # Dropping candidates entirely is safe EXCEPT when a candidate is already
    # contradicted: a contradicted answer's REQUIRE_USER item surfaces at
    # FILL too (raise_at_fill=True), while a missing answer's item is
    # SUBMIT-only -- removing the candidate would silence an already-surfaced
    # FILL-stage item, loosening the decision at a FILL request.
    if all(not any(c.contradicted for c in r.candidates) for r in ctx.requirements):
        yield dataclasses.replace(
            ctx, requirements=tuple(dataclasses.replace(r, candidates=()) for r in ctx.requirements))
    for attr in ATTRS:
        # Unknown-safety holds only for rules whose on_unknown is at least as
        # restrictive as their effect (documented via _rule_safe_for_unknown):
        # with item 5's arbitrary on_unknown combinations, marking an
        # attribute UNKNOWN can legitimately loosen the decision when a rule
        # referencing it declares a weaker on_unknown (e.g. effect=
        # REDUCE_TO(NONE), on_unknown=BLOCK, which doesn't touch capability
        # at all) -- skip those attributes rather than assert a false property.
        if _attr_safe_for_unknown(ctx, attr):
            yield dataclasses.replace(ctx, attributes={**ctx.attributes, attr: UNKNOWN})


@settings(max_examples=150, deadline=None)
@given(contexts())
def test_monotonicity_and_unknown_safety(ctx):
    base = evaluate_authorization(ctx)
    for tighter in tighten_ops(ctx):
        d = evaluate_authorization(tighter)
        assert d.effective_capability <= base.effective_capability
        assert not (d.grantable and not base.grantable)


@settings(max_examples=100, deadline=None)
@given(contexts(), st.randoms())
def test_rule_order_independence(ctx, rnd):
    rules_ = list(ctx.standing_policy["rules"])
    rnd.shuffle(rules_)
    shuffled = dataclasses.replace(ctx, standing_policy=make_policy(
        *rules_, lists=ctx.standing_policy.get("employer_lists")))
    a, b = evaluate_authorization(ctx), evaluate_authorization(shuffled)
    assert (a.result, a.effective_capability, a.grantable, a.reasons, a.require_user_items) == \
           (b.result, b.effective_capability, b.grantable, b.reasons, b.require_user_items)


@settings(max_examples=100, deadline=None)
@given(contexts())
def test_determinism_and_authority_source(ctx):
    a, b = evaluate_authorization(ctx), evaluate_authorization(ctx)
    assert a == b
    assert a.effective_capability <= min(ctx.deployment_ceiling, ctx.account_max, ctx.workspace_ceiling)


@settings(max_examples=60, deadline=None)
@given(contexts(), st.sampled_from([Mode.SHADOW, Mode.DRY_RUN]))
def test_non_live_is_never_grantable(ctx, mode):
    assert not evaluate_authorization(dataclasses.replace(ctx, mode=mode)).grantable


# ---------------------------------------------------------------------------
# Item 2: dedicated answer-state chain properties, with everything else in
# the context held constant -- the pairwise comparisons are fresh -> expired,
# then expired -> each of the four worst-tier states independently (never
# chained through each other, since contradicted's item surfaces at FILL
# while missing/unclassified/sensitive's items are SUBMIT-only -- chaining
# through all four in a fixed order is not monotonic for `grantable`, as
# tests/product/test_autonomy_gate_representation.py's existing chain test
# documents; comparing each independently against "expired" avoids that).
# ---------------------------------------------------------------------------

@settings(max_examples=150, deadline=None)
@given(contexts(), st.sampled_from([Capability.FILL, Capability.SUBMIT]))
def test_required_field_state_never_increases_capability_or_grantable(ctx, stage):
    base_ctx = dataclasses.replace(ctx, requested_stage=stage, requirements=())
    fresh = evaluate_authorization(dataclasses.replace(base_ctx, requirements=(_req_fresh("f", True),)))
    expired = evaluate_authorization(dataclasses.replace(base_ctx, requirements=(_req_expired("f", True),)))
    assert expired.effective_capability <= fresh.effective_capability
    assert not (expired.grantable and not fresh.grantable)
    for name, factory in WORST_TIER:
        worst = evaluate_authorization(dataclasses.replace(base_ctx, requirements=(factory("f", True),)))
        assert worst.effective_capability <= expired.effective_capability, name
        assert not (worst.grantable and not expired.grantable), name


@settings(max_examples=150, deadline=None)
@given(contexts(), st.sampled_from([Capability.FILL, Capability.SUBMIT]))
def test_optional_field_state_never_lowers_below_baseline(ctx, stage):
    base_ctx = dataclasses.replace(ctx, requested_stage=stage, requirements=())
    baseline = evaluate_authorization(base_ctx)
    for name, factory in ALL_STATES:
        d = evaluate_authorization(dataclasses.replace(base_ctx, requirements=(factory("f", False),)))
        assert d.effective_capability == baseline.effective_capability, name
        if name != "contradicted":
            # A contradicted answer is not a permitted source at all and
            # produces its own REQUIRE_USER item regardless of required/
            # optional (spec §9.3 step 3 "answers" bullet + step 5) -- the
            # one state where an optional field CAN legitimately lower
            # grantable below baseline, confirmed by the pinned unit test
            # test_optional_field_answer_state_chain_never_lowers_below_no_field_baseline
            # in test_autonomy_gate_representation.py, which deliberately
            # checks only effective_capability, not grantable, for this
            # exact reason.
            assert not (baseline.grantable and not d.grantable), name


# ---------------------------------------------------------------------------
# Item 3: PREPARE control -- field state is not evaluated before the form is
# inspected (_apply_requirements returns immediately below Capability.FILL).
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(contexts(), requirement("extra"))
def test_prepare_stage_ignores_requirement_state(ctx, extra_req):
    prepare_ctx = dataclasses.replace(ctx, requested_stage=Capability.PREPARE)
    base = evaluate_authorization(prepare_ctx)
    other = evaluate_authorization(
        dataclasses.replace(prepare_ctx, requirements=prepare_ctx.requirements + (extra_req,)))
    assert (other.result, other.effective_capability, other.grantable, other.reasons) == \
           (base.result, base.effective_capability, base.grantable, base.reasons)


# ---------------------------------------------------------------------------
# Item 5: a BLOCK rule's on_unknown may only be BLOCK or REQUIRE_USER
# (ruling L); a document violating this is invalid and must fail closed.
# ---------------------------------------------------------------------------

_BAD_ON_UNKNOWN_FOR_BLOCK = (
    [{"type": "REDUCE_TO", "level": lvl} for lvl in REDUCIBLE_LEVELS] + [{"type": "NO_EFFECT"}]
)


@settings(max_examples=40, deadline=None)
@given(contexts(), st.sampled_from(_BAD_ON_UNKNOWN_FOR_BLOCK))
def test_block_rule_with_lax_on_unknown_is_invalid_input(ctx, bad_on_unknown):
    bad_policy = make_policy({
        "id": "bad", "description": "", "when": {"attr": "fit.overall_score", "op": "lt", "value": 50},
        "effect": {"type": "BLOCK"}, "on_unknown": bad_on_unknown,
    })
    d = evaluate_authorization(dataclasses.replace(ctx, standing_policy=bad_policy))
    assert d.result is ResultKind.DENY and d.deny_reason == "invalid_input"


# ---------------------------------------------------------------------------
# Item 6: determinism holds under independently rebuilt (reordered) dict keys
# for attributes and the standing-policy document.
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(contexts())
def test_determinism_under_reordered_dict_keys(ctx):
    reordered_attrs = dict(reversed(list(ctx.attributes.items())))
    reordered_policy = (
        {k: ctx.standing_policy[k] for k in reversed(list(ctx.standing_policy.keys()))}
        if ctx.standing_policy is not None else None
    )
    ctx2 = dataclasses.replace(ctx, attributes=reordered_attrs, standing_policy=reordered_policy)
    assert evaluate_authorization(ctx) == evaluate_authorization(ctx2)


# ---------------------------------------------------------------------------
# Item 7: composing two tightening operations in sequence stays monotonic.
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(contexts(), st.data())
def test_composed_tightenings_still_monotonic(ctx, data):
    base = evaluate_authorization(ctx)
    first_ops = list(tighten_ops(ctx))
    if not first_ops:
        return
    ctx1 = data.draw(st.sampled_from(first_ops))
    once = evaluate_authorization(ctx1)
    assert once.effective_capability <= base.effective_capability
    assert not (once.grantable and not base.grantable)
    second_ops = list(tighten_ops(ctx1))
    if not second_ops:
        return
    ctx2 = data.draw(st.sampled_from(second_ops))
    twice = evaluate_authorization(ctx2)
    assert twice.effective_capability <= once.effective_capability
    assert not (twice.grantable and not once.grantable)
    assert twice.effective_capability <= base.effective_capability
    assert not (twice.grantable and not base.grantable)
