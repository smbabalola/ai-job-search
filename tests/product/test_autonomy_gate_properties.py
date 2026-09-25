# tests/product/test_autonomy_gate_properties.py
"""Property-based tests for the authorization gate (spec §17).

Fix round 1 (2026-09-25, HEAD a43dea9): widened for rulings K and L --
K: a required field with no permitted source caps capability at FILL
   whenever requirements are evaluated (FILL or SUBMIT), not only at SUBMIT;
L: a BLOCK rule's on_unknown may only be BLOCK or REQUIRE_USER.

Fix round 2 (2026-09-25, HEAD cdb976e): rulings M, N, O plus a coordinator
review that found round 1's properties near-vacuous (the widened base was
almost never grantable, ~1.5%, because grant_binding_drift was non-empty
~51% of the time). `contexts()` now draws a `permissive` half the time,
forcing every restrictive dimension to its most permissive value so a
substantial share of generated bases are actually grantable at both FILL
and SUBMIT. Example counts are restored to at least their pre-round-1
values.

Fix round 3 (2026-09-25): round 2's permissive profile turned out to be a
near-constant EMPTY context (grantable, but with no rules/requirements/
acks to act on), making rule/unknown/answer/ack tightenings vacuous; the
permissive profile is now RICH but still grantable (see contexts() and
test_rich_permissive_bases_are_grantable_and_flip_across_every_op_family,
which is derandomized and measures/reports rates and flip counts per op
family). Also: a direct, stage-by-stage ruling-O property for
completion_blockers, and a decision_fingerprint property that recomputes
the canonical hash directly (via dataclasses.fields) and checks sensitivity
to every field individually, rather than inferring it from whether the
whole output tuple changed. See task-7-report.md for the full case
analysis behind every guard below.
"""
from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal

from hypothesis import given, settings, strategies as st

from product.autonomy_contract import (
    BudgetState, Capability, CompletionBlocker, CounterState, EmployerKeyStrength,
    IdentityStrength, Mode, ProvenanceTier, RepresentationRequirement, RequireUserItem,
    ResultKind, RuleAcknowledgement, UNKNOWN, canonical_hash, reason as make_reason,
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


def _false_predicate_for(attr, attributes):
    """A predicate over `attr` guaranteed FALSE (never UNKNOWN, never TRUE)
    for `attributes`'s current values -- used to build "dormant" rules for
    the rich permissive profile (fix round 3 item 1): present, referencing a
    real attribute, but inert until that attribute is deliberately marked
    UNKNOWN by a tighten op, at which point on_unknown activates -- a real
    tightening rather than a no-op on an empty policy."""
    if attr == "fit.overall_score":
        return {"attr": attr, "op": "gt", "value": 1000}  # score is always 0-100
    if attr == "job.employment_type":
        other = "CONTRACT" if attributes["job.employment_type"] == "PERMANENT" else "PERMANENT"
        return {"attr": attr, "op": "eq", "value": other}
    return {"attr": attr, "op": "eq", "value": "name:definitely-not-acme"}  # company.key is always "name:acme"


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
    if attr == "job.employment_type":
        # Real values only (matches contexts()'s attribute draw) -- a shared
        # value pool across attributes meant many eq predicates could never
        # match, under-exercising rule evaluation (fix round 2 item 3).
        return {"attr": attr, "op": "eq", "value": draw(st.sampled_from(["PERMANENT", "CONTRACT"]))}
    # company.key
    if lists and draw(st.booleans()):
        return {"attr": attr, "op": "in_list", "value": draw(st.sampled_from(sorted(lists)))}
    return {"attr": attr, "op": "eq", "value": draw(st.sampled_from(["name:acme", "name:other"]))}


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

_DORMANT_ON_UNKNOWN_CHOICES = [
    {"type": "BLOCK"}, {"type": "REQUIRE_USER"}, {"type": "REDUCE_TO", "level": "NONE"},
]


@st.composite
def contexts(draw):
    # Fix round 2 item 1: bias `contexts()` so a substantial share of bases
    # are grantable. Fix round 3 item 1: round 2's permissive profile was a
    # near-constant EMPTY context (no rules/requirements/acks/counters/
    # budgets/UNKNOWN attrs) -- so rule/UNKNOWN/answer/ack tightenings never
    # had anything to act on starting from a grantable base (0 observed
    # flips). The profile below is RICH but still grantable: 1-3 "dormant"
    # rules (predicate FALSE for the base attributes, on_unknown restrictive
    # -- inert now, a real tightening once a referenced attribute goes
    # UNKNOWN), one REQUIRE_USER rule that DOES match paired with a valid
    # PROCEED acknowledgement (removing/lapsing it is a real tightening),
    # required+optional requirements in fresh/evidence-backed states
    # (aging/contradicting/dropping the required one is a real tightening),
    # a counter below its limit and a budget under its cap, and -- only on an
    # attribute none of the dormant/ack rules reference, so it can't
    # accidentally break grantability -- an already-UNKNOWN attribute.
    #
    # Every dimension below is drawn UNCONDITIONALLY, regardless of
    # `permissive`, and only the *value actually used* is chosen afterwards.
    # An earlier version skipped the restrictive/rich draws entirely on the
    # branch not taken, making that branch far shorter/cheaper to generate;
    # measured empirically, Hypothesis's example generation grows the size/
    # complexity of its byte buffer as a run progresses (to explore more of
    # the input space), which systematically starved whichever branch was
    # shorter (round 2: ~14% instead of an intended ~50%) -- confirmed via a
    # direct sampling script both times. Drawing the same amount either way
    # removes that size asymmetry.
    permissive = draw(st.integers(1, 10)) <= 6

    fit_score = draw(st.integers(0, 100))
    employment_type = draw(st.sampled_from(["PERMANENT", "CONTRACT"]))
    attributes = {"fit.overall_score": fit_score, "job.employment_type": employment_type, "company.key": "name:acme"}

    # Generic (unrestricted) already-UNKNOWN attribute -- the non-permissive
    # branch's existing dimension, unrelated to the rich profile's own
    # UNKNOWN placement below.
    generic_blank_attr = draw(st.sampled_from(ATTRS))
    generic_blank_flag = draw(st.booleans())

    employer_lists = {EMPLOYER_LIST_NAME: draw(st.sampled_from([[], ["name:acme"], ["name:other"]]))}
    drawn_rules_list = draw(rules(employer_lists))

    # -- rich profile: dormant rules + one matching REQUIRE_USER rule --
    num_dormant = draw(st.integers(1, 3))
    dormant_rules = []
    dormant_referenced = set()
    for i in range(num_dormant):
        d_attr = draw(st.sampled_from(ATTRS))
        dormant_referenced.add(d_attr)
        d_on_unknown = draw(st.sampled_from(_DORMANT_ON_UNKNOWN_CHOICES))
        dormant_rules.append({
            "id": f"dormant{i}", "description": "", "when": _false_predicate_for(d_attr, attributes),
            "effect": {"type": "REQUIRE_USER"}, "on_unknown": d_on_unknown,
        })
    ack_rule = {
        "id": "ack_rule", "description": "",
        "when": {"attr": "company.key", "op": "eq", "value": "name:acme"},  # always TRUE for the base attributes
        "effect": {"type": "REQUIRE_USER"}, "on_unknown": {"type": "REQUIRE_USER"},
    }
    rich_rules_list = dormant_rules + [ack_rule]
    rich_referenced = dormant_referenced | {"company.key"}
    rich_ack = RuleAcknowledgement(
        rule_id="ack_rule", rule_hash=rule_hash(ack_rule),
        observed_fingerprint=observed_fingerprint(ack_rule, attributes, employer_lists),
        disposition="PROCEED",
    )

    # Rich profile's own UNKNOWN placement: only on an attribute none of the
    # dormant/ack rules reference (drawn unconditionally either way, for size
    # parity -- see comment above).
    unreferenced = [a for a in ATTRS if a not in rich_referenced]
    rich_blank_flag = draw(st.booleans())
    rich_blank_attr = draw(st.sampled_from(unreferenced)) if unreferenced else None

    if permissive:
        rules_list = rich_rules_list
        if rich_blank_flag and rich_blank_attr is not None:
            attributes[rich_blank_attr] = UNKNOWN
    else:
        rules_list = drawn_rules_list
        if generic_blank_flag:
            attributes[generic_blank_attr] = UNKNOWN
    policy = make_policy(*rules_list, lists=employer_lists)

    drawn_acks = draw(maybe_ack(drawn_rules_list, attributes, employer_lists))
    acks = (rich_ack,) if permissive else drawn_acks

    bool_or_unknown = st.sampled_from([True, False, UNKNOWN])
    drawn_target = good_target(
        provenance=draw(st.sampled_from([None, *ProvenanceTier])),
        adapter_submit_capable=draw(st.booleans()),
        landing_within_redirect_set=draw(bool_or_unknown),
        tenant_matches_employer=draw(bool_or_unknown),
        unexplained_redirect=draw(bool_or_unknown),
        ats_job_id_matches=draw(st.sampled_from([None, True, False, UNKNOWN])),
    )
    apply_target = good_target() if permissive else drawn_target  # DISCOVERY_VERIFIED, submit-capable, all True

    drawn_requirements = tuple(draw(requirement(f"req{i}")) for i in range(draw(st.integers(0, 2))))
    # req0 always required+fresh: real material for the answer-state tighten
    # ops (age/contradict/drop). req1 optional 50% of the time, mixing
    # required/optional and fresh/evidence-backed, per fix round 3 item 1.
    include_req1 = draw(st.booleans())
    req1_required = draw(st.booleans())
    req1_evidence = draw(st.booleans())
    req1 = _req_evidence("req1", req1_required) if req1_evidence else _req_fresh("req1", req1_required)
    rich_requirements = (_req_fresh("req0", True), req1) if include_req1 else (_req_fresh("req0", True),)
    requirements = rich_requirements if permissive else drawn_requirements

    drawn_counters = tuple(
        CounterState(f"c{i}", draw(stages), draw(st.integers(0, 5)), draw(st.integers(1, 5)), None)
        for i in range(draw(st.integers(0, 2)))
    )
    drawn_budgets = tuple(
        BudgetState(draw(st.sampled_from(["LLM", "BROWSER", "EXTERNAL_API", "OTHER"])), "daily",
                    Decimal(draw(st.integers(0, 100))), Decimal(0), Decimal(draw(st.integers(0, 100))),
                    Decimal(0), None)
        for _ in range(draw(st.integers(0, 2)))
    )
    drawn_hard_stops = tuple(draw(st.lists(
        st.sampled_from(["captcha", "login_wall", "email_verification"]), max_size=2, unique=True)))
    drawn_drift = tuple(draw(st.lists(
        st.sampled_from(["pack_hash", "manifest_hash"]), max_size=2, unique=True)))

    rich_counter_used = draw(st.integers(0, 2))
    rich_counter_headroom = draw(st.integers(1, 3))
    rich_counter_stage = draw(stages)
    rich_counters = (CounterState("rich_c", rich_counter_stage, rich_counter_used,
                                   rich_counter_used + rich_counter_headroom, None),)
    rich_budget_used = draw(st.integers(0, 50))
    rich_budget_headroom = draw(st.integers(1, 50))
    rich_budget_category = draw(st.sampled_from(["LLM", "BROWSER", "EXTERNAL_API", "OTHER"]))
    rich_budgets = (BudgetState(rich_budget_category, "daily", Decimal(rich_budget_used), Decimal(0),
                                 Decimal(rich_budget_used + rich_budget_headroom), Decimal(0), None),)

    counters = rich_counters if permissive else drawn_counters
    budgets = rich_budgets if permissive else drawn_budgets
    executor_hard_stops = () if permissive else drawn_hard_stops
    grant_binding_drift = () if permissive else drawn_drift

    drawn_deployment, drawn_account, drawn_workspace = draw(levels), draw(levels), draw(levels)
    drawn_identity_strength = draw(st.sampled_from(list(IdentityStrength)))
    drawn_identity_conflict = draw(st.booleans())
    drawn_pack_auto_confirmable = draw(st.booleans())
    drawn_employer_key_strength = draw(st.sampled_from(list(EmployerKeyStrength)))
    drawn_employer_key = draw(st.sampled_from([None, "name:acme"]))
    drawn_intent_state = draw(st.sampled_from([None, "CLAIMED", "CONFIRMED"]))
    drawn_intent_overridden = draw(st.booleans())

    return make_ctx(
        requested_stage=draw(stages),
        deployment_ceiling=Capability.SUBMIT if permissive else drawn_deployment,
        account_max=Capability.SUBMIT if permissive else drawn_account,
        workspace_ceiling=Capability.SUBMIT if permissive else drawn_workspace,
        standing_policy=policy,
        attributes=attributes,
        identity_strength=IdentityStrength.SOURCE_RECORD if permissive else drawn_identity_strength,
        identity_conflict=False if permissive else drawn_identity_conflict,
        apply_target=apply_target,
        pack_auto_confirmable=True if permissive else drawn_pack_auto_confirmable,
        requirements=requirements,
        rule_acknowledgements=acks,
        employer_key_strength=EmployerKeyStrength.ATS_TENANT if permissive else drawn_employer_key_strength,
        employer_key="name:acme" if permissive else drawn_employer_key,
        existing_intent_state=None if permissive else drawn_intent_state,
        intent_overridden=False if permissive else drawn_intent_overridden,
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


def _on_unknown_cap_impact(rule):
    """The capability impact when this rule's predicate is UNKNOWN, per the
    gate's actual behaviour including ruling N (spec §5.2 "a stricter stop on
    unknown keeps the rule's cap"): a REDUCE_TO(X) rule whose on_unknown
    stops (REQUIRE_USER/BLOCK) instead of reducing still applies the cap to
    X, in addition to the stop -- so its unknown-cap impact equals the
    known-match cap, not None as it would without ruling N."""
    effect, on_unknown = rule["effect"], rule["on_unknown"]
    if on_unknown["type"] == "REDUCE_TO":
        return Capability[on_unknown["level"]]
    if effect["type"] == "REDUCE_TO" and on_unknown["type"] in ("REQUIRE_USER", "BLOCK"):
        return Capability[effect["level"]]
    return None


def _restricts_result(effect):
    return effect["type"] in ("BLOCK", "REQUIRE_USER")


def _rule_safe_for_unknown(rule):
    effect, on_unknown = rule["effect"], rule["on_unknown"]
    # on_unknown=REDUCE_TO(NONE) is safe regardless of effect (fix round 2
    # item 3): it forces effective_capability to the absolute floor, which
    # can never be an increase over anything, and NONE can never satisfy
    # `cap >= requested_stage` for any valid (non-NONE) requested_stage, so
    # grantable is unconditionally False -- it can never turn a non-grantable
    # decision grantable either. This also covers a REQUIRE_USER-effect rule
    # with on_unknown=REDUCE_TO(NONE), which the result-axis check below
    # would otherwise wrongly exclude (REDUCE_TO isn't in (BLOCK,
    # REQUIRE_USER)) even though it is safe by this floor argument.
    if on_unknown == {"type": "REDUCE_TO", "level": "NONE"}:
        return True
    e_cap, u_cap = _cap_impact(effect), _on_unknown_cap_impact(rule)
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
    """Yields (family, tightened_ctx) pairs. Families are tracked by fix
    round 3's flip-diversity meta-test to confirm each of the op families the
    coordinator named (unknown-marking, answer aging/contradiction/drop, ack
    removal/lapse) actually produces a non-trivial number of
    grantable->non-grantable flips against the rich permissive profile above
    -- not just that the properties hold vacuously."""
    lower = lambda c: C(max(0, c - 1))
    yield "ceiling", dataclasses.replace(ctx, deployment_ceiling=lower(ctx.deployment_ceiling))
    yield "ceiling", dataclasses.replace(ctx, account_max=lower(ctx.account_max))
    yield "ceiling", dataclasses.replace(ctx, workspace_ceiling=lower(ctx.workspace_ceiling))
    yield "stop", dataclasses.replace(ctx, kill_switch_engaged=True)
    yield "stop", dataclasses.replace(ctx, sentinel_present=True)
    yield "identity", dataclasses.replace(ctx, identity_strength=IdentityStrength.WEAK)
    yield "identity", dataclasses.replace(ctx, identity_conflict=True)
    yield "pack", dataclasses.replace(ctx, pack_auto_confirmable=False)
    # §9.3 step 4: CLAIMED always denies; CONFIRMED denies unless overridden.
    # Switching CLAIMED -> CONFIRMED while intent_overridden is already True
    # would LOOSEN the decision (CLAIMED denies unconditionally; CONFIRMED
    # + override does not) -- skip only that one combination.
    if not (ctx.existing_intent_state == "CLAIMED" and ctx.intent_overridden):
        yield "intent", dataclasses.replace(ctx, existing_intent_state="CONFIRMED")
    # CLAIMED always denies unconditionally (no override escape hatch), the
    # strictest of the three intent states -- unlike the CONFIRMED op above,
    # this needs no guard: moving to CLAIMED can only add the duplicate
    # denial, never remove one, from any starting state (None: adds it;
    # CONFIRMED+override: adds it, since override no longer applies;
    # CONFIRMED+no-override or already CLAIMED: already denied, unchanged).
    yield "intent", dataclasses.replace(ctx, existing_intent_state="CLAIMED")
    yield "stop", dataclasses.replace(ctx, governing_auto_reject=True)
    # §8.1: "no target" already caps at PREPARE, stricter than imported_source's
    # FILL cap, so substituting IMPORTED_SOURCE is only a valid tightening when
    # a target already exists (see task-7-report.md's original NEEDS_CONTEXT).
    if ctx.apply_target.provenance is not None:
        yield "apply_target", dataclasses.replace(ctx, apply_target=good_target(provenance=ProvenanceTier.IMPORTED_SOURCE))
    # Append rather than replace: a varied base context may already carry its
    # own counters/blockers/drift/hard-stops, and replacing them outright
    # could silently drop an existing restriction -- a loosening.
    yield "limit", dataclasses.replace(
        ctx, counters=ctx.counters + (CounterState("tighten_x", ctx.requested_stage, 1, 1, None),))
    yield "governing", dataclasses.replace(
        ctx, unresolved_governing_require_user=ctx.unresolved_governing_require_user + ("tighten_blk",))
    yield "drift", dataclasses.replace(ctx, grant_binding_drift=ctx.grant_binding_drift + ("tighten_field",))
    yield "hard_stop", dataclasses.replace(ctx, executor_hard_stops=ctx.executor_hard_stops + ("tighten_stop",))
    yield "budget", dataclasses.replace(ctx, budgets=ctx.budgets + (
        BudgetState("OTHER", "daily", Decimal(999), Decimal(0), Decimal(1), Decimal(0), None),))
    # An acknowledgement can only ever lift a restriction the user themselves
    # wrote (spec §9.3 step 5); removing (or lapsing, i.e. corrupting its
    # rule_hash so it no longer matches -- functionally identical to removal
    # in _apply_standing_policy's validity check) it can only add restriction
    # back.
    if ctx.rule_acknowledgements:
        yield "ack", dataclasses.replace(ctx, rule_acknowledgements=())
        yield "ack", dataclasses.replace(ctx, rule_acknowledgements=tuple(
            dataclasses.replace(a, rule_hash=a.rule_hash + "_lapsed") for a in ctx.rule_acknowledgements))
    yield "rule", dataclasses.replace(ctx, standing_policy=make_policy(
        *ctx.standing_policy["rules"],
        {"id": "extra", "description": "", "when": {"attr": "fit.overall_score", "op": "lt", "value": 101},
         "effect": {"type": "REDUCE_TO", "level": "PREPARE"}, "on_unknown": {"type": "REDUCE_TO", "level": "PREPARE"}},
        lists=ctx.standing_policy.get("employer_lists")))
    # Answer-state tightenings (spec §9.3 step 3 "answers" bullet): aging a
    # fresh answer past its subject's freshness window, or marking any
    # candidate contradicted, can only ever add a blocker or a REQUIRE_USER
    # item, never remove one -- safe unconditionally from any starting state.
    yield "answer", dataclasses.replace(ctx, requirements=tuple(
        dataclasses.replace(r, candidates=tuple(
            dataclasses.replace(c, confirmed_at=NOW - timedelta(days=99999)) for c in r.candidates))
        for r in ctx.requirements))
    yield "answer", dataclasses.replace(ctx, requirements=tuple(
        dataclasses.replace(r, candidates=tuple(
            dataclasses.replace(c, contradicted=True) for c in r.candidates))
        for r in ctx.requirements))
    # Dropping candidates entirely is safe EXCEPT when a candidate is already
    # contradicted: a contradicted answer's REQUIRE_USER item surfaces at
    # FILL too (raise_at_fill=True), while a missing answer's item is
    # SUBMIT-only -- removing the candidate would silence an already-surfaced
    # FILL-stage item, loosening the decision at a FILL request.
    if all(not any(c.contradicted for c in r.candidates) for r in ctx.requirements):
        yield "answer", dataclasses.replace(
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
            yield "unknown", dataclasses.replace(ctx, attributes={**ctx.attributes, attr: UNKNOWN})


@settings(max_examples=300, deadline=None)
@given(contexts())
def test_monotonicity_and_unknown_safety(ctx):
    base = evaluate_authorization(ctx)
    for _family, tighter in tighten_ops(ctx):
        d = evaluate_authorization(tighter)
        assert d.effective_capability <= base.effective_capability
        assert not (d.grantable and not base.grantable)


@settings(max_examples=200, deadline=None)
@given(contexts(), st.randoms())
def test_rule_order_independence(ctx, rnd):
    rules_ = list(ctx.standing_policy["rules"])
    rnd.shuffle(rules_)
    shuffled = dataclasses.replace(ctx, standing_policy=make_policy(
        *rules_, lists=ctx.standing_policy.get("employer_lists")))
    a, b = evaluate_authorization(ctx), evaluate_authorization(shuffled)
    assert (a.result, a.effective_capability, a.grantable, a.reasons, a.require_user_items) == \
           (b.result, b.effective_capability, b.grantable, b.reasons, b.require_user_items)


@settings(max_examples=200, deadline=None)
@given(contexts())
def test_determinism_and_authority_source(ctx):
    a, b = evaluate_authorization(ctx), evaluate_authorization(ctx)
    assert a == b
    assert a.effective_capability <= min(ctx.deployment_ceiling, ctx.account_max, ctx.workspace_ceiling)


@settings(max_examples=100, deadline=None)
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

@settings(max_examples=200, deadline=None)
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


@settings(max_examples=200, deadline=None)
@given(contexts(), st.sampled_from([Capability.FILL, Capability.SUBMIT]))
def test_optional_field_state_never_lowers_below_baseline(ctx, stage):
    """Ruling M: an optional (non-required) field's contradicted answer is
    now omitted (optional_omitted why=contradicted) rather than raising its
    own REQUIRE_USER item, so it no longer needs the special case this test
    used to carve out for "contradicted" -- every optional worst-tier state,
    contradicted included, now matches the no-field baseline exactly, both
    in effective_capability and in grantable."""
    base_ctx = dataclasses.replace(ctx, requested_stage=stage, requirements=())
    baseline = evaluate_authorization(base_ctx)
    for name, factory in ALL_STATES:
        d = evaluate_authorization(dataclasses.replace(base_ctx, requirements=(factory("f", False),)))
        assert d.effective_capability == baseline.effective_capability, name
        assert not (baseline.grantable and not d.grantable), name


# ---------------------------------------------------------------------------
# Item 3: PREPARE control -- field state is not evaluated before the form is
# inspected (_apply_requirements returns immediately below Capability.FILL).
# ---------------------------------------------------------------------------

@settings(max_examples=150, deadline=None)
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


@settings(max_examples=80, deadline=None)
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
# for attributes and the standing-policy document -- deepened per fix round 2
# item 3 to reorder keys recursively (each rule dict, each nested predicate
# dict, employer_lists, limits), not just the policy's own top-level keys,
# and to independently rebuild apply_target and requirements (fresh dataclass
# instances, with their own nested dicts -- job_context, candidate context --
# also reordered) rather than only touching dicts.
# ---------------------------------------------------------------------------

def _reorder_dict_keys(obj):
    if isinstance(obj, dict):
        return {k: _reorder_dict_keys(obj[k]) for k in reversed(list(obj.keys()))}
    if isinstance(obj, list):
        return [_reorder_dict_keys(v) for v in obj]
    return obj


def _rebuild_requirement(r):
    return dataclasses.replace(
        r,
        job_context=_reorder_dict_keys(dict(r.job_context)),
        candidates=tuple(
            dataclasses.replace(c, context=_reorder_dict_keys(dict(c.context))) for c in r.candidates
        ),
    )


@settings(max_examples=150, deadline=None)
@given(contexts())
def test_determinism_under_reordered_dict_keys(ctx):
    reordered_attrs = _reorder_dict_keys(dict(ctx.attributes))
    reordered_policy = _reorder_dict_keys(ctx.standing_policy) if ctx.standing_policy is not None else None
    rebuilt_target = dataclasses.replace(ctx.apply_target)  # fresh instance, same field values
    rebuilt_requirements = tuple(_rebuild_requirement(r) for r in ctx.requirements)
    ctx2 = dataclasses.replace(ctx, attributes=reordered_attrs, standing_policy=reordered_policy,
                                apply_target=rebuilt_target, requirements=rebuilt_requirements)
    assert evaluate_authorization(ctx) == evaluate_authorization(ctx2)


# ---------------------------------------------------------------------------
# Item 7: composing two tightening operations in sequence stays monotonic.
# ---------------------------------------------------------------------------

@settings(max_examples=150, deadline=None)
@given(contexts(), st.data())
def test_composed_tightenings_still_monotonic(ctx, data):
    base = evaluate_authorization(ctx)
    first_ops = [c for _family, c in tighten_ops(ctx)]
    if not first_ops:
        return
    ctx1 = data.draw(st.sampled_from(first_ops))
    once = evaluate_authorization(ctx1)
    assert once.effective_capability <= base.effective_capability
    assert not (once.grantable and not base.grantable)
    second_ops = [c for _family, c in tighten_ops(ctx1)]
    if not second_ops:
        return
    ctx2 = data.draw(st.sampled_from(second_ops))
    twice = evaluate_authorization(ctx2)
    assert twice.effective_capability <= once.effective_capability
    assert not (twice.grantable and not once.grantable)
    assert twice.effective_capability <= base.effective_capability
    assert not (twice.grantable and not base.grantable)


# ---------------------------------------------------------------------------
# Fix round 2 item 1: demonstrate that permissive bases are actually
# grantable at both FILL and SUBMIT (the concrete failure round 1 review
# found: the widened base was grantable only ~1.5% of the time). This test
# draws many contexts() samples directly inside one Hypothesis example (via
# st.data(), not one context per example) so it can compute and assert on an
# aggregate rate in a single place, rather than relying on external
# --hypothesis-show-statistics tooling.
# ---------------------------------------------------------------------------

def test_rich_permissive_bases_are_grantable_and_flip_across_every_op_family():
    """Fix round 3 item 1 + item 4. Sample via a normal per-example @given
    run (not st.data().draw() in a loop inside one example -- that pattern
    chains hundreds of draws into a single Hypothesis buffer and was
    observed to blow the internal generation-size budget, at which point
    Hypothesis's fallback generation systematically favours the cheapest
    choice for every strategy, silently corrupting the very distribution
    this test exists to measure -- see fix round 2's report). A plain
    accumulator mutated by a normally-run, derandomized @given sampler has no
    such failure mode and reproduces the exact same examples on every run
    (item 4): every example is an ordinary, independent draw exactly like
    the rest of this file's properties.

    Round 2's permissive profile was grantable but near-constant EMPTY (no
    rules/requirements/acks/counters/budgets/UNKNOWN attrs), so the
    rule/unknown/answer/ack tighten_ops families never had anything to act
    on starting from a grantable base -- 0 observed flips. This test
    measures, against the round-3 RICH permissive profile: (a) the fraction
    of bases that are grantable AND carry >=1 rule, (b) grantable AND carry
    >=1 requirement, both against a >=30% floor; and (c) for each of the
    three op families the coordinator named (unknown-marking, answer
    aging/contradiction/drop, ack removal/lapse), the count of
    grantable->non-grantable flips over the whole run, each against a
    floor chosen well below every observed count in this file's own
    diagnostic runs (see task-7-report.md) so it cannot flake."""
    counts = {"n": 0, "fill": 0, "submit": 0, "rule_and_grantable": 0, "req_and_grantable": 0}
    flips = {"unknown": 0, "answer": 0, "ack": 0}

    @settings(max_examples=400, deadline=None, derandomize=True)
    @given(contexts())
    def _sample(ctx):
        counts["n"] += 1
        d = evaluate_authorization(ctx)
        if evaluate_authorization(dataclasses.replace(ctx, requested_stage=Capability.FILL)).grantable:
            counts["fill"] += 1
        if evaluate_authorization(dataclasses.replace(ctx, requested_stage=Capability.SUBMIT)).grantable:
            counts["submit"] += 1
        if d.grantable:
            if ctx.standing_policy is not None and len(ctx.standing_policy["rules"]) >= 1:
                counts["rule_and_grantable"] += 1
            if len(ctx.requirements) >= 1:
                counts["req_and_grantable"] += 1
            for family, tightened in tighten_ops(ctx):
                if family in flips and not evaluate_authorization(tightened).grantable:
                    flips[family] += 1

    _sample()
    n = counts["n"]
    fill_rate, submit_rate = counts["fill"] / n, counts["submit"] / n
    rule_rate, req_rate = counts["rule_and_grantable"] / n, counts["req_and_grantable"] / n
    assert fill_rate >= 0.30, f"grantable-at-FILL rate too low: {fill_rate:.1%} ({counts['fill']}/{n})"
    assert submit_rate >= 0.30, f"grantable-at-SUBMIT rate too low: {submit_rate:.1%} ({counts['submit']}/{n})"
    assert rule_rate >= 0.30, (
        f"grantable-with->=1-rule rate too low: {rule_rate:.1%} ({counts['rule_and_grantable']}/{n})")
    assert req_rate >= 0.30, (
        f"grantable-with->=1-requirement rate too low: {req_rate:.1%} ({counts['req_and_grantable']}/{n})")
    for family, floor in (("unknown", 20), ("answer", 20), ("ack", 20)):
        assert flips[family] >= floor, f"{family} family flip count too low: {flips[family]} (floor {floor})"


# ---------------------------------------------------------------------------
# Fix round 2/3 item 4 (item 2 in round 3): properties for the new outputs
# (ruling O's completion_blockers and decision_fingerprint).
#
# completion_blockers pruning property: comparing the same context with a
# blocking requirement made *optional* is deliberately avoided -- ruling M
# means an optional field is never a completion blocker to begin with, so
# that comparison would conflate rulings M and O rather than isolate O.
# Instead: completion_blockers and the required_field_unresolved capability
# reduction are produced together, in the same unresolved_required loop in
# _apply_requirements (one CompletionBlocker and one reduce() call per
# unresolved required field, from the same trigger conditions) -- so
# *deleting* the requirements that produced a context's completion_blockers
# entirely removes both their reduction and their REQUIRE_USER item, which
# can only make the decision at least as permissive as before, never more
# restrictive. Kept unchanged from round 2 per the round-3 instruction.
# ---------------------------------------------------------------------------

@settings(max_examples=250, deadline=None)
@given(contexts(), st.sampled_from([Capability.FILL, Capability.SUBMIT]))
def test_removing_completion_blocker_requirements_never_lowers_capability_or_grantable(ctx, stage):
    test_ctx = dataclasses.replace(ctx, requested_stage=stage)
    before = evaluate_authorization(test_ctx)
    blocked_keys = {b.field_key for b in before.completion_blockers}
    if not blocked_keys:
        return
    pruned = tuple(r for r in test_ctx.requirements if r.key not in blocked_keys)
    after = evaluate_authorization(dataclasses.replace(test_ctx, requirements=pruned))
    assert after.effective_capability >= before.effective_capability
    assert not (before.grantable and not after.grantable)


@settings(max_examples=250, deadline=None)
@given(contexts(), st.sampled_from(WORST_TIER))
def test_completion_blockers_match_ruling_o_at_every_stage(ctx, worst_choice):
    """Fix round 3 item 2: round 2's completion_blockers property was
    vacuous (blockers present in only 9.5% of examples, never at a FILL
    decision, since it only ever *observed* whatever completion_blockers a
    fully-random context happened to produce). This directly exercises
    ruling O: add a REQUIRED field in a no-fillable-value state to an
    otherwise baseline-grantable context (isolating ruling O's own effect --
    skip when the baseline isn't independently grantable at both FILL and
    SUBMIT, so unrelated restrictions already in ctx can't muddy the
    assertions) and check the exact stage-by-stage contract.

    "contradicted" is the one state whose REQUIRE_USER item already surfaces
    at FILL too (raise_at_fill=True in _apply_requirements -- pre-existing,
    spec-mandated, confirmed by test_contradiction_requires_user_even_at_fill
    in test_autonomy_gate_representation.py and documented since round 1):
    completion_blockers still records it at FILL regardless (ruling O is
    stage-independent once requirements are evaluated at all), but result/
    grantable at FILL differ for that one state, unlike missing/unclassified/
    sensitive, which stay silent (SUBMIT-only items) at FILL."""
    name, factory = worst_choice
    required_worst = factory("blocked_required", True)
    optional_worst = factory("blocked_optional", False)
    expired_required = _req_expired("blocked_expired", True)
    reqs = ctx.requirements + (required_worst, optional_worst, expired_required)

    baseline_fill = evaluate_authorization(dataclasses.replace(ctx, requested_stage=Capability.FILL))
    baseline_submit = evaluate_authorization(dataclasses.replace(ctx, requested_stage=Capability.SUBMIT))
    if not (baseline_fill.grantable and baseline_submit.grantable):
        return

    def at(stage):
        return evaluate_authorization(dataclasses.replace(ctx, requested_stage=stage, requirements=reqs))

    fill_d, submit_d, prepare_d = at(Capability.FILL), at(Capability.SUBMIT), at(Capability.PREPARE)
    fill_keys = {b.field_key for b in fill_d.completion_blockers}
    submit_keys = {b.field_key for b in submit_d.completion_blockers}

    assert fill_d.effective_capability == Capability.FILL
    assert "blocked_required" in fill_keys
    if name == "contradicted":
        assert fill_d.result is ResultKind.REQUIRE_USER and not fill_d.grantable
    else:
        assert fill_d.result is ResultKind.ALLOW and fill_d.grantable

    assert submit_d.effective_capability == Capability.FILL
    assert not submit_d.grantable
    assert "blocked_required" in submit_keys

    assert prepare_d.completion_blockers == ()

    # blockers do not block FILL (they never affect result/effective/grantable
    # on their own beyond ruling O's own FILL cap, already asserted above) --
    # and optional / expired-required fields never produce one.
    assert "blocked_optional" not in fill_keys and "blocked_optional" not in submit_keys
    assert "blocked_expired" not in fill_keys and "blocked_expired" not in submit_keys

    # Every blocker corresponds to a required requirement actually present.
    req_by_key = {r.key: r for r in reqs}
    for b in set(fill_d.completion_blockers) | set(submit_d.completion_blockers):
        assert b.field_key in req_by_key and req_by_key[b.field_key].required


# ---------------------------------------------------------------------------
# Fix round 3 item 3: decision_fingerprint must be EXACTLY the canonical hash
# of every other AuthorizationDecision field (derived from dataclasses.fields
# so a new field can't be silently skipped), and sensitive to each of them
# individually -- round 2's property only checked "if the tuple of outputs
# differs, the fingerprint differs", which in practice was satisfied entirely
# by input_fingerprint differing (a near-certainty between a context and its
# tightened variant) rather than by isolating any single decision field.
# ---------------------------------------------------------------------------

def _decision_payload(d):
    return {f.name: getattr(d, f.name) for f in dataclasses.fields(d) if f.name != "decision_fingerprint"}


def _recompute_fingerprint(d):
    return canonical_hash("autonomy-decision", "v1", _decision_payload(d))


def _alternate_for_field(name, value):
    if name == "mode":
        return Mode.DRY_RUN if value != Mode.DRY_RUN else Mode.SHADOW
    if name == "result":
        return ResultKind.BLOCK if value != ResultKind.BLOCK else ResultKind.DENY
    if name in ("requested_stage", "effective_capability"):
        return Capability.NONE if value != Capability.NONE else Capability.SUBMIT
    if name in ("grantable", "retryable"):
        return not value
    if name == "deny_reason":
        return "sentinel_alt" if value != "sentinel_alt" else "sentinel_alt2"
    if name == "reasons":
        return value + (make_reason("sentinel_marker"),)
    if name == "require_user_items":
        return value + (RequireUserItem("sentinel_kind", "sentinel_ref"),)
    if name == "completion_blockers":
        return value + (CompletionBlocker("sentinel_field", None, "missing_answer", Capability.SUBMIT),)
    if name == "retry_at":
        return NOW if value != NOW else NOW + timedelta(seconds=1)
    if name in ("input_fingerprint", "engine_version"):
        return (value or "") + "_alt"
    if name in ("policy_version_hash", "subject_policy_hash"):
        return ((value or "") + "_alt") if value is not None else "sentinel_hash_alt"
    raise AssertionError(f"no alternate value defined for AuthorizationDecision field {name!r}")


@settings(max_examples=200, deadline=None)
@given(contexts())
def test_decision_fingerprint_matches_recomputed_hash_and_is_sensitive_to_every_field(ctx):
    d = evaluate_authorization(ctx)
    base_hash = _recompute_fingerprint(d)
    assert base_hash == d.decision_fingerprint
    for f in dataclasses.fields(d):
        if f.name == "decision_fingerprint":
            continue
        altered = dataclasses.replace(d, **{f.name: _alternate_for_field(f.name, getattr(d, f.name))})
        assert _recompute_fingerprint(altered) != base_hash, f.name


# ---------------------------------------------------------------------------
# Bonus (not a numbered fix-round-2 item, but directly covers a gate change
# mentioned in the same review: duplicate requirement keys -> invalid_input).
# ---------------------------------------------------------------------------

@settings(max_examples=60, deadline=None)
@given(contexts())
def test_duplicate_requirement_keys_are_invalid_input(ctx):
    dup = _req_fresh("dup", True)
    d = evaluate_authorization(dataclasses.replace(ctx, requirements=ctx.requirements + (dup, dup)))
    assert d.result is ResultKind.DENY and d.deny_reason == "invalid_input"
