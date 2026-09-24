# tests/product/test_autonomy_gate.py
from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from product.autonomy_contract import (
    BudgetState, Capability, CounterState, EmployerKeyStrength, IdentityStrength, Mode,
    ProvenanceTier, RequireUserItem, ResultKind, RuleAcknowledgement, UNKNOWN,
)
from product.autonomy_gate import evaluate_authorization
from product.standing_policy import evaluate_rules
from product.autonomy_contract import RepresentationRequirement
from tests.product.autonomy_fixtures import NOW, answer, good_target, make_ctx, make_policy

C = Capability
R = ResultKind

MIN_FIT = {"id": "min-fit", "description": "", "when": {"attr": "fit.overall_score", "op": "lt", "value": 75},
           "effect": {"type": "REDUCE_TO", "level": "PREPARE"}, "on_unknown": {"type": "REDUCE_TO", "level": "PREPARE"}}
PERMANENT_ONLY = {"id": "perm", "description": "", "when": {"attr": "job.employment_type", "op": "ne", "value": "PERMANENT"},
                  "effect": {"type": "REQUIRE_USER"}, "on_unknown": {"type": "REQUIRE_USER"}}
DENY = {"id": "deny", "description": "", "when": {"attr": "company.key", "op": "in_list", "value": "deny"},
        "effect": {"type": "BLOCK"}, "on_unknown": {"type": "REQUIRE_USER"}}


def codes(decision):
    return {r.code for r in decision.reasons}


def test_fully_permitted_context_allows_submit():
    d = evaluate_authorization(make_ctx())
    assert (d.result, d.effective_capability, d.grantable, d.deny_reason) == (R.ALLOW, C.SUBMIT, True, None)
    assert "ceiling" in codes(d)
    assert d.input_fingerprint.startswith("sha256:") and d.policy_version_hash and d.subject_policy_hash


@pytest.mark.parametrize("field", ["deployment_ceiling", "account_max", "workspace_ceiling"])
@pytest.mark.parametrize("level", [C.NONE, C.PREPARE, C.FILL])
def test_ceiling_is_min_of_three(field, level):
    d = evaluate_authorization(make_ctx(**{field: level}))
    assert d.effective_capability == level and d.result is R.ALLOW and not d.grantable


def test_no_authorization_means_none():
    d = evaluate_authorization(make_ctx(account_max=C.NONE, requested_stage=C.PREPARE))
    assert d.effective_capability == C.NONE and not d.grantable


def test_missing_standing_policy_means_none():
    d = evaluate_authorization(make_ctx(standing_policy=None, requested_stage=C.PREPARE))
    assert d.effective_capability == C.NONE and "standing_policy_missing" in codes(d)


@pytest.mark.parametrize("mode", [Mode.SHADOW, Mode.DRY_RUN])
def test_non_live_modes_never_grantable(mode):
    d = evaluate_authorization(make_ctx(mode=mode))
    assert d.result is R.ALLOW and d.effective_capability == C.SUBMIT and not d.grantable
    assert "non_live_mode" in codes(d)


def test_rule_reduce_on_match_and_on_unknown():
    d = evaluate_authorization(make_ctx(standing_policy=make_policy(MIN_FIT), attributes={"fit.overall_score": 60}))
    assert d.effective_capability == C.PREPARE and not d.grantable
    d = evaluate_authorization(make_ctx(standing_policy=make_policy(MIN_FIT), attributes={}))
    assert d.effective_capability == C.PREPARE
    assert any(r.code == "rule_reduce" and ("via", "unknown") in r.params for r in d.reasons)


def test_rule_block_and_require_user():
    policy = make_policy(DENY, lists={"deny": ["name:acme"]})
    assert evaluate_authorization(make_ctx(standing_policy=policy)).result is R.BLOCK
    d = evaluate_authorization(make_ctx(standing_policy=make_policy(PERMANENT_ONLY),
                                        attributes={"job.employment_type": "CONTRACT"}))
    assert d.result is R.REQUIRE_USER and RequireUserItem("rule", "perm") in d.require_user_items


def test_deny_list_unknown_company_requires_user_not_permission():
    policy = make_policy(DENY, lists={"deny": ["name:acme"]})
    d = evaluate_authorization(make_ctx(standing_policy=policy, attributes={"company.key": UNKNOWN}))
    assert d.result is R.REQUIRE_USER


def _ack(policy, attributes, disposition="PROCEED", rule_id="perm"):
    outcome = next(o for o in evaluate_rules(policy, attributes) if o.rule_id == rule_id)
    return RuleAcknowledgement(rule_id, outcome.rule_hash, outcome.observed_fingerprint, disposition)


def test_rule_acknowledgement_proceed_lapse_and_do_not_proceed():
    attrs = {"job.employment_type": "CONTRACT", "fit.overall_score": 80}
    policy = make_policy(PERMANENT_ONLY, MIN_FIT)
    ack = _ack(policy, attrs)
    d = evaluate_authorization(make_ctx(standing_policy=policy, attributes=attrs, rule_acknowledgements=(ack,)))
    assert d.result is R.ALLOW and "rule_acknowledged_proceed" in codes(d)
    # Unrelated rule edited: acknowledgement still valid.
    edited = make_policy(PERMANENT_ONLY, {**MIN_FIT, "when": {**MIN_FIT["when"], "value": 70}})
    d = evaluate_authorization(make_ctx(standing_policy=edited, attributes=attrs, rule_acknowledgements=(ack,)))
    assert d.result is R.ALLOW
    # Acknowledged rule edited: lapses.
    changed_rule = make_policy({**PERMANENT_ONLY, "when": {**PERMANENT_ONLY["when"], "value": "CONTRACT"}}, MIN_FIT)
    d = evaluate_authorization(make_ctx(standing_policy=changed_rule, attributes={**attrs, "job.employment_type": "TEMPORARY"},
                                        rule_acknowledgements=(ack,)))
    assert d.result is R.REQUIRE_USER and "rule_acknowledgement_lapsed" in codes(d)
    # Observed attributes changed: lapses.
    d = evaluate_authorization(make_ctx(standing_policy=policy, attributes={**attrs, "job.employment_type": "TEMPORARY"},
                                        rule_acknowledgements=(ack,)))
    assert d.result is R.REQUIRE_USER
    # DO_NOT_PROCEED blocks.
    no = _ack(policy, attrs, disposition="DO_NOT_PROCEED")
    assert evaluate_authorization(make_ctx(standing_policy=policy, attributes=attrs, rule_acknowledgements=(no,))).result is R.BLOCK


@pytest.mark.parametrize("overrides, code", [
    (dict(identity_strength=IdentityStrength.WEAK), "identity_weak"),
    (dict(identity_key=None), "identity_weak"),
    (dict(identity_conflict=True), "identity_conflict"),
    (dict(employer_key_strength=EmployerKeyStrength.UNKNOWN), "employer_key_unknown"),
    (dict(apply_target=good_target(provenance=ProvenanceTier.USER_SUPPLIED)), "apply_target_tier"),
    (dict(apply_target=good_target(provenance=ProvenanceTier.IMPORTED_SOURCE)), "apply_target_tier"),
    (dict(apply_target=good_target(adapter_submit_capable=False)), "adapter_not_submit_capable"),
    (dict(apply_target=good_target(landing_within_redirect_set=UNKNOWN)), "target_unverified"),
    (dict(apply_target=good_target(unexplained_redirect=UNKNOWN)), "target_unverified"),
])
def test_reductions_cap_at_fill(overrides, code):
    d = evaluate_authorization(make_ctx(**overrides))
    assert d.effective_capability == C.FILL and code in codes(d) and not d.grantable


def test_user_confirmed_target_can_submit_and_ats_job_id_optional():
    d = evaluate_authorization(make_ctx(apply_target=good_target(
        provenance=ProvenanceTier.USER_CONFIRMED_APPLY_TARGET, ats_job_id_matches=None)))
    assert d.grantable


def test_target_mismatch_requires_user():
    d = evaluate_authorization(make_ctx(apply_target=good_target(tenant_matches_employer=False)))
    assert d.result is R.REQUIRE_USER and RequireUserItem("apply_target", "tenant_matches_employer") in d.require_user_items


def test_no_target_and_unconfirmable_pack_cap_at_prepare():
    assert evaluate_authorization(make_ctx(apply_target=good_target(provenance=None))).effective_capability == C.PREPARE
    assert evaluate_authorization(make_ctx(pack_auto_confirmable=False)).effective_capability == C.PREPARE


@pytest.mark.parametrize("stage", [C.PREPARE, C.FILL, C.SUBMIT])
def test_kill_switch_and_sentinel_deny_every_stage(stage):
    for overrides in (dict(kill_switch_engaged=True), dict(sentinel_present=True)):
        d = evaluate_authorization(make_ctx(requested_stage=stage, **overrides))
        assert (d.result, d.deny_reason, d.grantable) == (R.DENY, "kill_switch", False)


def test_stale_binding_denies():
    d = evaluate_authorization(make_ctx(grant_binding_drift=("fill_manifest_hash",)))
    assert (d.result, d.deny_reason) == (R.DENY, "stale_binding")


def test_auto_reject_blocks_and_duplicates_deny():
    assert evaluate_authorization(make_ctx(governing_auto_reject=True)).result is R.BLOCK
    for state in ("CLAIMED", "CONFIRMED"):
        d = evaluate_authorization(make_ctx(existing_intent_state=state, requested_stage=C.PREPARE))
        assert (d.result, d.deny_reason) == (R.DENY, "duplicate")
    d = evaluate_authorization(make_ctx(existing_intent_state="CONFIRMED", intent_overridden=True))
    assert d.grantable and "duplicate_overridden" in codes(d)


def test_limits_and_budgets_are_temporary_with_retry_at():
    retry = NOW + timedelta(hours=12)
    d = evaluate_authorization(make_ctx(counters=(CounterState("submit_per_day", C.SUBMIT, 3, 3, retry),)))
    assert (d.result, d.deny_reason, d.retry_at) == (R.DENY_TEMPORARY, "limit", retry)
    # A counter for another stage is irrelevant.
    assert evaluate_authorization(make_ctx(counters=(CounterState("fill_per_day", C.FILL, 10, 10, retry),))).grantable
    d = evaluate_authorization(make_ctx(budgets=(BudgetState("LLM", "day", Decimal("4.5"), Decimal("0.4"), Decimal("5"), Decimal("0.2"), None),)))
    assert (d.result, d.deny_reason, d.retry_at) == (R.DENY_TEMPORARY, "budget", None)


def test_governing_blockers_and_hard_stops():
    d = evaluate_authorization(make_ctx(unresolved_governing_require_user=("blk_1",), requested_stage=C.PREPARE))
    assert d.result is R.REQUIRE_USER and RequireUserItem("governing_blocker", "blk_1") in d.require_user_items
    d = evaluate_authorization(make_ctx(executor_hard_stops=("captcha",), requested_stage=C.FILL))
    assert RequireUserItem("hard_stop", "captcha") in d.require_user_items
    d = evaluate_authorization(make_ctx(executor_hard_stops=("captcha",), requested_stage=C.PREPARE))
    assert d.result is R.ALLOW


FLAGS = {
    "kill": dict(kill_switch_engaged=True),
    "block": dict(governing_auto_reject=True),
    "duplicate": dict(existing_intent_state="CONFIRMED"),
    "limit": dict(counters=(CounterState("submit_per_day", C.SUBMIT, 3, 3, None),)),
    "require_user": dict(unresolved_governing_require_user=("blk_1",)),
}
EXPECTED = [("kill", R.DENY), ("block", R.BLOCK), ("duplicate", R.DENY), ("limit", R.DENY_TEMPORARY), ("require_user", R.REQUIRE_USER)]


@pytest.mark.parametrize("combo", [c for n in range(1, 6) for c in itertools.combinations(FLAGS, n)])
def test_precedence_and_all_reasons_retained(combo):
    overrides = {}
    for name in combo:
        overrides.update(FLAGS[name])
    d = evaluate_authorization(make_ctx(**overrides))
    expected = next(result for name, result in EXPECTED if name in combo)
    assert d.result is expected
    assert len(d.reasons) >= len(combo) + 1  # every condition leaves a reason, plus the ceiling


@pytest.mark.parametrize("overrides", [
    dict(now=datetime(2026, 9, 24, 12, 0)),
    dict(requested_stage=C.NONE),
    dict(standing_policy={"schema_version": "nope"}),
    dict(subject_policy={"schema_version": "nope"}),
    dict(attributes={"fit.overall_score": 74.5}),
    dict(requirements=(RepresentationRequirement(
        key="notice", subject="employment.notice_period", required=True, evidence_available=False,
        candidates=(answer("employment.notice_period", confirmed_at=datetime(2026, 9, 1)),)),)),
])
def test_invalid_input_fails_closed(overrides):
    d = evaluate_authorization(make_ctx(**overrides))
    assert (d.result, d.deny_reason, d.effective_capability, d.grantable) == (R.DENY, "invalid_input", C.NONE, False)


def test_deterministic_and_fingerprint_tracks_inputs():
    a, b = evaluate_authorization(make_ctx()), evaluate_authorization(make_ctx())
    assert a == b
    assert evaluate_authorization(make_ctx(run_id="run_2")).input_fingerprint != a.input_fingerprint


# --- Fix round 1: fail-closed coverage for unrecognised/malformed input -----
# The brief's original implementation fails OPEN on several malformed inputs
# (e.g. a truthy UNKNOWN sentinel on a strict-bool field is falsy-checked and
# silently treated as satisfied; a non-enum value skips the intended branch
# entirely). Every case below must resolve to DENY(invalid_input) instead.

_FUTURE = NOW + timedelta(days=1)

_ROUND1_INVALID_CASES = [
    pytest.param(dict(mode="LIVE"), id="mode-not-enum"),
    pytest.param(dict(requested_stage=3), id="requested_stage-not-enum"),
    pytest.param(dict(deployment_ceiling="SUBMIT"), id="deployment_ceiling-not-enum"),
    pytest.param(dict(account_max=3), id="account_max-not-enum"),
    pytest.param(dict(workspace_ceiling=None), id="workspace_ceiling-not-enum"),
    pytest.param(dict(identity_strength="WEAK"), id="identity_strength-not-enum"),
    pytest.param(dict(employer_key_strength="ATS_TENANT"), id="employer_key_strength-not-enum"),
    pytest.param(dict(apply_target=good_target(provenance="discovery_verified")), id="provenance-not-enum"),
    pytest.param(dict(kill_switch_engaged=1), id="kill_switch_engaged-not-bool"),
    pytest.param(dict(sentinel_present="yes"), id="sentinel_present-not-bool"),
    pytest.param(dict(governing_auto_reject=0), id="governing_auto_reject-not-bool"),
    pytest.param(dict(pack_auto_confirmable=UNKNOWN), id="pack_auto_confirmable-not-bool"),
    pytest.param(dict(identity_conflict=1), id="identity_conflict-not-bool"),
    pytest.param(dict(intent_overridden="no"), id="intent_overridden-not-bool"),
    pytest.param(dict(apply_target=good_target(adapter_submit_capable=UNKNOWN)), id="adapter_submit_capable-unknown"),
    pytest.param(dict(requirements=(RepresentationRequirement(
        key="k", subject=None, required=1, evidence_available=True),)), id="requirement-required-not-bool"),
    pytest.param(dict(requirements=(RepresentationRequirement(
        key="k", subject=None, required=True, evidence_available="no"),)), id="requirement-evidence_available-not-bool"),
    pytest.param(dict(requirements=(RepresentationRequirement(
        key="k", subject="employment.notice_period", required=True, evidence_available=False,
        candidates=(answer("employment.notice_period", contradicted=1),)),)), id="candidate-contradicted-not-bool"),
    pytest.param(dict(apply_target=good_target(landing_within_redirect_set="yes")), id="landing_within_redirect_set-bad-type"),
    pytest.param(dict(apply_target=good_target(tenant_matches_employer=1)), id="tenant_matches_employer-bad-type"),
    pytest.param(dict(apply_target=good_target(unexplained_redirect="no")), id="unexplained_redirect-bad-type"),
    pytest.param(dict(apply_target=good_target(ats_job_id_matches="yes")), id="ats_job_id_matches-bad-type"),
    pytest.param(dict(existing_intent_state="confirmed"), id="existing_intent_state-bad-value"),
    pytest.param(dict(rule_acknowledgements=(_ack(make_policy(PERMANENT_ONLY),
                                                   {"job.employment_type": "CONTRACT"}, disposition="do_not_proceed"),)),
                 id="disposition-lowercase"),
    pytest.param(dict(rule_acknowledgements=(_ack(make_policy(PERMANENT_ONLY),
                                                   {"job.employment_type": "CONTRACT"}, disposition="GARBAGE"),)),
                 id="disposition-garbage"),
    pytest.param(dict(requirements=(RepresentationRequirement(
        key="k", subject="employment.notice_period", required=True, evidence_available=False,
        candidates=(answer("employment.notice_period", basis_kind="evidence"),)),)), id="basis_kind-lowercase"),
    pytest.param(dict(requirements=(RepresentationRequirement(
        key="k", subject="employment.notice_period", required=True, evidence_available=False,
        candidates=(answer("employment.notice_period", reach="EMPLOYER"),)),)), id="reach-not-enum"),
    pytest.param(dict(requirements=(RepresentationRequirement(
        key="k", subject="employment.notice_period", required=True, evidence_available=False,
        candidates=(answer("employment.notice_period", confirmed_at=_FUTURE),)),)), id="confirmed_at-future"),
]


@pytest.mark.parametrize("overrides", _ROUND1_INVALID_CASES)
def test_round1_fail_closed_on_malformed_input(overrides):
    d = evaluate_authorization(make_ctx(**overrides))
    assert (d.result, d.deny_reason, d.effective_capability, d.grantable) == (R.DENY, "invalid_input", C.NONE, False)


def test_empty_identity_key_treated_as_missing():
    d = evaluate_authorization(make_ctx(identity_key=""))
    assert d.effective_capability == C.FILL and "identity_weak" in codes(d)


def test_missing_pack_artifact_id_caps_at_prepare_even_if_confirmable():
    d = evaluate_authorization(make_ctx(pack_artifact_id=None))
    assert d.effective_capability == C.PREPARE and "pack_not_auto_confirmable" in codes(d)


def test_retryable_flag_only_true_for_deny_temporary():
    d = evaluate_authorization(make_ctx(counters=(CounterState("submit_per_day", C.SUBMIT, 3, 3, NOW + timedelta(hours=1)),)))
    assert d.result is R.DENY_TEMPORARY and d.retryable is True
    d = evaluate_authorization(make_ctx())
    assert d.result is R.ALLOW and d.retryable is False
    d = evaluate_authorization(make_ctx(kill_switch_engaged=True))
    assert d.result is R.DENY and d.retryable is False


def test_rule_acknowledgement_lapses_specifically_on_observed_attribute_change():
    attrs = {"job.employment_type": "CONTRACT", "fit.overall_score": 80}
    policy = make_policy(PERMANENT_ONLY, MIN_FIT)
    ack = _ack(policy, attrs)
    d = evaluate_authorization(make_ctx(standing_policy=policy, attributes={**attrs, "job.employment_type": "TEMPORARY"},
                                        rule_acknowledgements=(ack,)))
    assert d.result is R.REQUIRE_USER and "rule_acknowledgement_lapsed" in codes(d)


def test_naive_retry_at_on_counter_state_is_invalid_input():
    naive = datetime(2026, 9, 24, 18, 0)
    d = evaluate_authorization(make_ctx(counters=(CounterState("submit_per_day", C.SUBMIT, 3, 3, naive),)))
    assert (d.result, d.deny_reason, d.effective_capability, d.grantable) == (R.DENY, "invalid_input", C.NONE, False)


def test_employer_key_none_reduces_to_fill():
    d = evaluate_authorization(make_ctx(employer_key=None))
    assert d.effective_capability == C.FILL and "employer_key_unknown" in codes(d)


FLAGS_EXT = {
    "kill": dict(kill_switch_engaged=True),
    "stale_binding": dict(grant_binding_drift=("fill_manifest_hash",)),
    "block": dict(governing_auto_reject=True),
    "duplicate": dict(existing_intent_state="CONFIRMED"),
    "limit": dict(counters=(CounterState("submit_per_day", C.SUBMIT, 3, 3, None),)),
    "budget": dict(budgets=(BudgetState("LLM", "day", Decimal("4.5"), Decimal("0.4"), Decimal("5"), Decimal("0.2"), None),)),
    "require_user": dict(unresolved_governing_require_user=("blk_1",)),
}
# Precedence order (spec §9.4): kill_switch/stale_binding (kill wins the tie)
# > BLOCK > duplicate > limit/budget (limit wins the tie) > REQUIRE_USER.
PRECEDENCE_EXT = [
    ("kill", R.DENY, "kill_switch"),
    ("stale_binding", R.DENY, "stale_binding"),
    ("block", R.BLOCK, None),
    ("duplicate", R.DENY, "duplicate"),
    ("limit", R.DENY_TEMPORARY, "limit"),
    ("budget", R.DENY_TEMPORARY, "budget"),
    ("require_user", R.REQUIRE_USER, None),
]
FLAG_REASON_CODE_EXT = {
    "kill": "kill_switch",
    "stale_binding": "stale_binding",
    "block": "governing_auto_reject",
    "duplicate": "duplicate_intent",
    "limit": "limit_reached",
    "budget": "budget_exceeded",
    "require_user": "governing_blocker",
}


@pytest.mark.parametrize("combo", [c for n in range(1, len(FLAGS_EXT) + 1) for c in itertools.combinations(FLAGS_EXT, n)])
def test_precedence_extended_with_stale_binding_and_budget(combo):
    overrides = {}
    for name in combo:
        overrides.update(FLAGS_EXT[name])
    d = evaluate_authorization(make_ctx(**overrides))
    winner_name, winner_result, winner_deny_reason = next(
        (name, result, deny_reason) for name, result, deny_reason in PRECEDENCE_EXT if name in combo
    )
    assert d.result is winner_result
    if winner_result is R.DENY:
        assert d.deny_reason == winner_deny_reason
    elif winner_result is R.DENY_TEMPORARY:
        assert d.deny_reason == ("limit" if "limit" in combo else "budget")
    for name in combo:
        assert FLAG_REASON_CODE_EXT[name] in codes(d)


# --- Fix round 2: user rulings on gate semantics (spec commit ee68ae3) ------

# Ruling A: the gate must never raise for any malformed closed-schema input,
# including containers of the wrong type or containing wrong-type elements
# (as opposed to round 1's scalar/enum-type checks).
_ROUND2_INVALID_CASES = [
    pytest.param(dict(apply_target=None), id="apply_target-none"),
    pytest.param(dict(rule_acknowledgements=None), id="rule_acknowledgements-none"),
    pytest.param(dict(requirements=None), id="requirements-none"),
    pytest.param(dict(executor_hard_stops=None), id="executor_hard_stops-none"),
    pytest.param(dict(grant_binding_drift=None), id="grant_binding_drift-none"),
    pytest.param(dict(unresolved_governing_require_user=None), id="unresolved_governing_require_user-none"),
    pytest.param(dict(counters="not-a-tuple"), id="counters-not-a-tuple"),
    pytest.param(dict(budgets="not-a-tuple"), id="budgets-not-a-tuple"),
    pytest.param(dict(counters=(CounterState("submit_per_day", C.SUBMIT, "3", 3, None),)), id="counter-used-not-int"),
    pytest.param(dict(counters=(CounterState("submit_per_day", "SUBMIT", 3, 3, None),)), id="counter-stage-not-capability"),
    pytest.param(dict(budgets=(BudgetState("LLM", "day", Decimal("4.5"), Decimal("0.4"), Decimal("5"), 0.2, None),)),
                 id="budget-estimate-float"),
    pytest.param(dict(attributes="not-a-mapping"), id="attributes-not-mapping"),
    pytest.param(dict(standing_policy=["not", "a", "mapping"]), id="standing_policy-not-mapping"),
    pytest.param(dict(subject_policy="not-a-mapping"), id="subject_policy-not-mapping"),
    pytest.param(dict(account_id=123), id="account_id-not-str"),
    pytest.param(dict(application_workspace_id=None), id="application_workspace_id-not-str"),
    pytest.param(dict(requirements=(RepresentationRequirement(
        key="k", subject="employment.notice_period", required=True, evidence_available=False,
        candidates="not-a-tuple"),)), id="candidates-not-a-tuple"),
    pytest.param(dict(requirements=(RepresentationRequirement(
        key="k", subject=None, required=True, evidence_available=True, job_context="not-a-mapping"),)),
        id="job_context-not-mapping"),
    pytest.param(dict(requirements=(RepresentationRequirement(
        key="k", subject="employment.notice_period", required=True, evidence_available=False,
        candidates=(answer("employment.notice_period", context="not-a-mapping"),)),)),
        id="candidate-context-not-mapping"),
]


@pytest.mark.parametrize("overrides", _ROUND2_INVALID_CASES)
def test_round2_fail_closed_on_malformed_containers_without_raising(overrides):
    d = evaluate_authorization(make_ctx(**overrides))
    assert (d.result, d.deny_reason, d.effective_capability, d.grantable) == (R.DENY, "invalid_input", C.NONE, False)


def test_invalid_input_still_reports_independent_safe_facts():
    """kill switch True + float attribute -> reasons include both invalid_input and kill_switch."""
    d = evaluate_authorization(make_ctx(kill_switch_engaged=True, attributes={"fit.overall_score": 74.5}))
    assert d.result is R.DENY and d.deny_reason == "invalid_input"
    assert "kill_switch" in codes(d) and "invalid_input" in codes(d)


def test_invalid_mode_normalizes_to_none_with_raw_value_in_reason():
    d = evaluate_authorization(make_ctx(mode="LIVE"))
    assert d.result is R.DENY and d.deny_reason == "invalid_input" and d.mode is None
    matches = [dict(r.params) for r in d.reasons if r.code == "invalid_input" and dict(r.params).get("detail") == "mode_invalid"]
    assert matches and matches[0]["raw"] == repr("LIVE")


def test_invalid_requested_stage_normalizes_to_none():
    d = evaluate_authorization(make_ctx(requested_stage=0))
    assert d.result is R.DENY and d.deny_reason == "invalid_input" and d.requested_stage is None


def test_valid_mode_preserved_when_other_field_is_the_invalid_one():
    """A valid-but-invalid-input context (naive `now`, valid mode SHADOW) still
    reports the real mode -- only the specific malformed field is nulled out."""
    naive_now = datetime(2026, 9, 24, 12, 0)
    d = evaluate_authorization(make_ctx(now=naive_now, mode=Mode.SHADOW))
    assert d.result is R.DENY and d.deny_reason == "invalid_input" and d.mode is Mode.SHADOW


# Ruling B: apply-target mismatch REQUIRE_USER items are stage-scoped to
# FILL/SUBMIT; the FILL cap reduction itself still applies at every stage.
def test_apply_target_mismatch_item_suppressed_before_fill():
    d = evaluate_authorization(make_ctx(
        apply_target=good_target(tenant_matches_employer=False), requested_stage=C.PREPARE))
    assert RequireUserItem("apply_target", "tenant_matches_employer") not in d.require_user_items
    assert d.effective_capability == C.FILL
    assert d.grantable  # PREPARE requested, FILL cap suffices


# Ruling C: hard stops always surface at FILL/SUBMIT, independent of relevance.
def test_hard_stop_surfaces_even_when_structural_cap_is_below_requested_stage():
    d = evaluate_authorization(make_ctx(
        apply_target=good_target(provenance=ProvenanceTier.USER_SUPPLIED),  # structural cap -> FILL
        executor_hard_stops=("captcha",), requested_stage=C.SUBMIT))
    assert d.result is R.REQUIRE_USER and RequireUserItem("hard_stop", "captcha") in d.require_user_items


# Ruling D: a non-required field's not-submit-ready answer is omitted, never
# a reduction; only a required field's does that.
def test_optional_expired_answer_does_not_reduce_and_is_omitted():
    expired = answer("employment.notice_period", confirmed_at=NOW - timedelta(days=100))  # freshness_days=60
    d = evaluate_authorization(make_ctx(requirements=(RepresentationRequirement(
        key="notice", subject="employment.notice_period", required=False, evidence_available=False,
        candidates=(expired,)),)))
    assert d.grantable and d.effective_capability == C.SUBMIT
    assert any(r.code == "optional_omitted" and ("why", "expired") in r.params for r in d.reasons)


# Ruling E: relevance is judged against the structural cap, computed before
# the actionable pack/answer-freshness reductions -- so an unconfirmable pack
# must not suppress an otherwise-relevant required-field item.
def test_relevance_uses_structural_cap_not_pack_reduction():
    d = evaluate_authorization(make_ctx(
        pack_auto_confirmable=False,
        requirements=(RepresentationRequirement(
            key="notice", subject="employment.notice_period", required=True, evidence_available=False),)))
    assert d.result is R.REQUIRE_USER and RequireUserItem("missing_answer", "notice") in d.require_user_items


# Ruling F: a CLAIMED (in-flight) intent always denies; only a CONFIRMED
# intent can be overridden.
def test_claimed_intent_denies_even_when_overridden():
    d = evaluate_authorization(make_ctx(existing_intent_state="CLAIMED", intent_overridden=True))
    assert (d.result, d.deny_reason) == (R.DENY, "duplicate")


# Ruling G: an acknowledgement's observed fingerprint includes the contents
# of any employer list its rule references via in_list.
WATCH_LIST = {"id": "watch", "description": "", "when": {"attr": "company.key", "op": "in_list", "value": "watch"},
              "effect": {"type": "REQUIRE_USER"}, "on_unknown": {"type": "REQUIRE_USER"}}


def test_employer_list_edit_lapses_rule_acknowledgement():
    attrs = {"company.key": "name:acme"}
    policy = make_policy(WATCH_LIST, lists={"watch": ["name:acme"]})
    ack = _ack(policy, attrs, rule_id="watch")
    d = evaluate_authorization(make_ctx(standing_policy=policy, attributes=attrs, rule_acknowledgements=(ack,)))
    assert d.result is R.ALLOW and "rule_acknowledged_proceed" in codes(d)
    edited = make_policy(WATCH_LIST, lists={"watch": ["name:acme", "name:other"]})
    d = evaluate_authorization(make_ctx(standing_policy=edited, attributes=attrs, rule_acknowledgements=(ack,)))
    assert d.result is R.REQUIRE_USER and "rule_acknowledgement_lapsed" in codes(d)
