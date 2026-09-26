from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings, strategies as st

from product.autonomy_contract import Capability, IdentityStrength
from product.candidate_promotion import (
    CandidateContext, ScreeningOutcome, admit_candidate_evaluation, candidate_input_fingerprint,
    evaluate_candidate_promotion,
)
from product.standing_policy import default_policy_document

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
O = ScreeningOutcome


def policy(*rules):
    doc = default_policy_document("Europe/London")
    doc["rules"] = list(rules)
    return doc


def ctx(**kw) -> CandidateContext:
    values = dict(
        now=NOW, account_id="acct", search_workspace_id="sw", candidate_id="cand",
        scheduler_enabled=True, kill_switch_engaged=False, sentinel_present=False,
        search_workspace_active=True, paused=False,
        deployment_ceiling=Capability.PREPARE, account_max=Capability.PREPARE, workspace_ceiling=Capability.PREPARE,
        candidate_state="new", run_status="completed",
        identity_key="source:greenhouse:1", identity_strength=IdentityStrength.SOURCE_RECORD,
        existing_application=False, existing_intent=False,
        fit_present=True, fit_fresh=True, discovery_fit_id="dsfit_1",
        standing_policy=policy(), attributes={"fit.overall_score": 80, "company.key": "name:acme",
                                              "workspace.id": "sw", "job.employment_type": "PERMANENT"},
        llm_budget_configured=True, evaluate_envelope_present=True,
        budget_available=True, budget_retry_at=None,
        promotions_available=True, promotions_retry_at=None,
    )
    values.update(kw)
    return CandidateContext(**values)


def test_eligible_candidate_promotes_and_fingerprint_ignores_now():
    result = evaluate_candidate_promotion(ctx())
    assert result.outcome is O.PROMOTE and result.could_unlock is False
    assert candidate_input_fingerprint(ctx()) == candidate_input_fingerprint(ctx(now=NOW + timedelta(hours=3)))
    assert candidate_input_fingerprint(ctx()) != candidate_input_fingerprint(ctx(candidate_state="saved"))


def test_precedence():
    assert evaluate_candidate_promotion(ctx(kill_switch_engaged=True, existing_application=True)).outcome is O.DENY
    block = {"id": "deny", "description": "", "when": {"attr": "company.key", "op": "eq", "value": "name:acme"},
             "effect": {"type": "BLOCK"}, "on_unknown": {"type": "BLOCK"}}
    assert evaluate_candidate_promotion(ctx(standing_policy=policy(block), existing_application=True)).outcome is O.BLOCK
    r = evaluate_candidate_promotion(ctx(existing_application=True, promotions_available=False))
    assert (r.outcome, r.reason_code) == (O.NOT_ELIGIBLE, "existing_application")
    r = evaluate_candidate_promotion(ctx(promotions_available=False, promotions_retry_at=NOW + timedelta(hours=5)))
    assert (r.outcome, r.retry_at) == (O.DENY_TEMPORARY, NOW + timedelta(hours=5))


def _ask_when_unknown():
    return {"id": "fit_unknown", "description": "", "when": {"attr": "fit.overall_score", "op": "lt", "value": 70},
            "effect": {"type": "REDUCE_TO", "level": "NONE"}, "on_unknown": {"type": "REQUIRE_USER"}}


def test_require_user_could_unlock_only_when_it_is_the_sole_obstacle():
    from product.autonomy_contract import UNKNOWN
    rule = {"id": "ask", "description": "", "when": {"attr": "job.employment_type", "op": "eq", "value": "CONTRACT"},
            "effect": {"type": "REQUIRE_USER"}, "on_unknown": {"type": "REQUIRE_USER"}}
    unknown = {"fit.overall_score": 80, "company.key": "name:acme", "workspace.id": "sw",
               "job.employment_type": UNKNOWN}
    r = evaluate_candidate_promotion(ctx(standing_policy=policy(rule), attributes=unknown))
    assert (r.outcome, r.could_unlock) == (O.REQUIRE_USER, True)
    assert r.require_user == ({"kind": "rule", "ref": "ask"},)
    # a structural obstacle as well -> not surfaced
    r = evaluate_candidate_promotion(ctx(standing_policy=policy(rule), attributes=unknown, identity_key=None,
                                         identity_strength=IdentityStrength.WEAK))
    assert (r.outcome, r.could_unlock) == (O.NOT_ELIGIBLE, False)
    # ruling N: REDUCE_TO(NONE) with REQUIRE_USER on unknown keeps the cap -> structurally below PREPARE
    r = evaluate_candidate_promotion(ctx(standing_policy=policy(_ask_when_unknown()),
                                         attributes={**unknown, "fit.overall_score": UNKNOWN}))
    assert (r.outcome, r.reason_code, r.could_unlock) == (O.NOT_ELIGIBLE, "capability_below_prepare", False)


def test_no_standing_policy_and_no_budget_are_structural():
    assert evaluate_candidate_promotion(ctx(standing_policy=None)).reason_code == "standing_policy_missing"
    assert evaluate_candidate_promotion(ctx(llm_budget_configured=False)).reason_code == "llm_budget_missing"


def test_admission_requires_every_condition():
    assert admit_candidate_evaluation(ctx(fit_present=False)).admitted is True
    for field, value, reason in [
        ("scheduler_enabled", False, "scheduler_disabled"), ("kill_switch_engaged", True, "halted"),
        ("sentinel_present", True, "halted"), ("search_workspace_active", False, "search_workspace_inactive"),
        ("paused", True, "paused"), ("workspace_ceiling", Capability.NONE, "capability_below_prepare"),
        ("candidate_state", "dismissed", "candidate_state"), ("run_status", "failed", "run_not_finished"),
        ("identity_strength", IdentityStrength.WEAK, "weak_identity"),
        ("existing_application", True, "existing_application"), ("existing_intent", True, "existing_intent"),
        ("llm_budget_configured", False, "llm_budget_missing"),
        ("evaluate_envelope_present", False, "evaluate_envelope_missing"),
        ("budget_available", False, "budget_exhausted"),
    ]:
        result = admit_candidate_evaluation(ctx(**{field: value}))
        assert result.admitted is False and reason in result.reasons, field


RESTRICTIONS = [
    ("kill_switch_engaged", True), ("sentinel_present", True), ("search_workspace_active", False),
    ("workspace_ceiling", Capability.NONE), ("account_max", Capability.NONE), ("deployment_ceiling", Capability.NONE),
    ("candidate_state", "promoted"), ("run_status", "failed"), ("identity_strength", IdentityStrength.WEAK),
    ("existing_application", True), ("existing_intent", True), ("fit_fresh", False), ("fit_present", False),
    ("promotions_available", False), ("llm_budget_configured", False), ("standing_policy", None),
]
RANK = {O.PROMOTE: 5, O.REQUIRE_USER: 4, O.DENY_TEMPORARY: 3, O.NOT_ELIGIBLE: 2, O.BLOCK: 1, O.DENY: 0}


@settings(max_examples=300, deadline=None, derandomize=True)
@given(st.lists(st.sampled_from(range(len(RESTRICTIONS))), unique=True))
def test_restricting_any_input_never_yields_a_more_permissive_outcome(indices):
    base = ctx()
    restricted = dataclasses.replace(base, **{RESTRICTIONS[i][0]: RESTRICTIONS[i][1] for i in indices})
    assert RANK[evaluate_candidate_promotion(restricted).outcome] <= RANK[evaluate_candidate_promotion(base).outcome]
    if indices:
        assert evaluate_candidate_promotion(restricted).outcome is not O.PROMOTE
