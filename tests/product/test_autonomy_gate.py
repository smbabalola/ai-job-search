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
