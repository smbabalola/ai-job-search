from __future__ import annotations

import copy

import pytest

from product.autonomy_contract import UNKNOWN
from product.standing_policy import (
    DEFAULT_LIMITS, StandingPolicyError, default_policy_document, evaluate_predicate,
    evaluate_rules, normalize_employment_type, observed_fingerprint, policy_hash,
    rule_hash, validate_standing_policy,
)


def _doc(*rules, lists=None):
    doc = default_policy_document("Europe/London")
    doc["rules"] = list(rules)
    if lists:
        doc["employer_lists"] = lists
    return doc


MIN_FIT = {
    "id": "min-fit-75", "description": "Only prepare below fit 75",
    "when": {"attr": "fit.overall_score", "op": "lt", "value": 75},
    "effect": {"type": "REDUCE_TO", "level": "PREPARE"},
    "on_unknown": {"type": "REDUCE_TO", "level": "PREPARE"},
}
PERMANENT_ONLY = {
    "id": "permanent-only", "description": "Permanent roles only",
    "when": {"attr": "job.employment_type", "op": "ne", "value": "PERMANENT"},
    "effect": {"type": "REQUIRE_USER"},
    "on_unknown": {"type": "REQUIRE_USER"},
}
DENY_LIST = {
    "id": "deny-list", "description": "Never apply to denied employers",
    "when": {"attr": "company.key", "op": "in_list", "value": "deny"},
    "effect": {"type": "BLOCK"},
    "on_unknown": {"type": "REQUIRE_USER"},
}


def test_default_document_is_valid_and_has_default_limits():
    doc = default_policy_document("Europe/London")
    validate_standing_policy(doc)
    assert doc["limits"]["submit_per_day"] == DEFAULT_LIMITS["submit_per_day"] == 3
    assert doc["limits"]["submit_per_run"] == 3
    assert doc["limits"]["fill_per_day"] == 10
    assert doc["limits"]["submit_per_employer_30d"] == 2
    assert doc["rules"] == []


def test_valid_rules_accepted():
    validate_standing_policy(_doc(MIN_FIT, PERMANENT_ONLY, DENY_LIST, lists={"deny": ["name:acme"]}))


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(timezone="Mars/Olympus"), "timezone"),
    (lambda d: d.pop("timezone"), "timezone"),
    (lambda d: d["rules"].append({**MIN_FIT, "effect": {"type": "GRANT", "level": "SUBMIT"}}), "effect"),
    (lambda d: d["rules"].append({**MIN_FIT, "effect": {"type": "REDUCE_TO", "level": "SUBMIT"}}), "level"),
    (lambda d: d["rules"].append({k: v for k, v in MIN_FIT.items() if k != "on_unknown"}), "on_unknown"),
    (lambda d: d["rules"].append({**DENY_LIST, "on_unknown": {"type": "NO_EFFECT"}}), "NO_EFFECT"),
    (lambda d: d["rules"].append({**MIN_FIT, "when": {"attr": "salary.secret", "op": "lt", "value": 1}}), "attr"),
    (lambda d: d["rules"].append({**MIN_FIT, "when": {"attr": "fit.overall_score", "op": "approx", "value": 1}}), "op"),
    (lambda d: d["rules"].append({**MIN_FIT, "when": {"attr": "fit.overall_score", "op": "lt", "value": 75.5}}), "integer"),
    (lambda d: d["rules"].extend([MIN_FIT, MIN_FIT]), "duplicate"),
    (lambda d: d["limits"].update(submit_per_day=-1), "submit_per_day"),
    (lambda d: d["limits"]["budgets"].update(LLM={"per_day": "abc", "per_application": "1"}), "budget"),
    (lambda d: d.update(surprise=True), "unknown"),
    (lambda d: d["rules"].append({**DENY_LIST}), "employer list"),
])
def test_invalid_documents_rejected_with_all_errors(mutate, message):
    doc = default_policy_document("Europe/London")
    mutate(doc)
    with pytest.raises(StandingPolicyError) as exc:
        validate_standing_policy(doc)
    assert any(message in e for e in exc.value.errors), exc.value.errors


def test_three_valued_leaf_and_combinators():
    attrs = {"fit.overall_score": 60, "job.employment_type": UNKNOWN}
    assert evaluate_predicate({"attr": "fit.overall_score", "op": "lt", "value": 75}, attrs, {}) is True
    assert evaluate_predicate({"attr": "fit.overall_score", "op": "gte", "value": 75}, attrs, {}) is False
    unknown_leaf = {"attr": "job.employment_type", "op": "eq", "value": "PERMANENT"}
    assert evaluate_predicate(unknown_leaf, attrs, {}) is UNKNOWN
    assert evaluate_predicate({"attr": "job.title", "op": "eq", "value": "x"}, attrs, {}) is UNKNOWN
    false_leaf = {"attr": "fit.overall_score", "op": "gt", "value": 90}
    true_leaf = {"attr": "fit.overall_score", "op": "lt", "value": 90}
    assert evaluate_predicate({"all": [false_leaf, unknown_leaf]}, attrs, {}) is False
    assert evaluate_predicate({"all": [true_leaf, unknown_leaf]}, attrs, {}) is UNKNOWN
    assert evaluate_predicate({"any": [true_leaf, unknown_leaf]}, attrs, {}) is True
    assert evaluate_predicate({"any": [false_leaf, unknown_leaf]}, attrs, {}) is UNKNOWN
    assert evaluate_predicate({"not": unknown_leaf}, attrs, {}) is UNKNOWN
    assert evaluate_predicate({"not": true_leaf}, attrs, {}) is False


def test_wrong_runtime_type_is_unknown_not_error():
    assert evaluate_predicate(
        {"attr": "fit.overall_score", "op": "lt", "value": 75}, {"fit.overall_score": "high"}, {},
    ) is UNKNOWN


def test_decimal_scores_compare_exactly():
    from decimal import Decimal
    pred = {"attr": "fit.overall_score", "op": "lt", "value": 75}
    assert evaluate_predicate(pred, {"fit.overall_score": Decimal("74.9")}, {}) is True
    assert evaluate_predicate(pred, {"fit.overall_score": Decimal("75.0")}, {}) is False
    assert evaluate_predicate(pred, {"fit.overall_score": 74.9}, {}) is UNKNOWN  # floats never trusted


def test_in_list_and_contains():
    lists = {"deny": ["name:acme"]}
    assert evaluate_predicate({"attr": "company.key", "op": "in_list", "value": "deny"}, {"company.key": "name:acme"}, lists) is True
    assert evaluate_predicate({"attr": "company.key", "op": "in_list", "value": "deny"}, {"company.key": UNKNOWN}, lists) is UNKNOWN
    assert evaluate_predicate({"attr": "job.title", "op": "contains", "value": "Fluids"}, {"job.title": "Senior drilling fluids engineer"}, {}) is True


def test_rule_outcomes_use_declared_on_unknown():
    doc = _doc(MIN_FIT, PERMANENT_ONLY)
    outcomes = {o.rule_id: o for o in evaluate_rules(doc, {"fit.overall_score": UNKNOWN, "job.employment_type": "PERMANENT"})}
    assert outcomes["min-fit-75"].applied_effect == {"type": "REDUCE_TO", "level": "PREPARE"}
    assert outcomes["min-fit-75"].via_unknown is True
    assert outcomes["permanent-only"].applied_effect is None


def test_no_effect_on_unknown_means_no_effect():
    rule = {**MIN_FIT, "on_unknown": {"type": "NO_EFFECT"}}
    (outcome,) = evaluate_rules(_doc(rule), {"fit.overall_score": UNKNOWN})
    assert outcome.applied_effect is None and outcome.via_unknown is True


def test_hashes():
    doc = _doc(MIN_FIT)
    reordered = copy.deepcopy(doc)
    reordered["rules"][0] = dict(reversed(list(reordered["rules"][0].items())))
    assert policy_hash(doc) == policy_hash(reordered)
    assert rule_hash(MIN_FIT) == rule_hash(dict(reversed(list(MIN_FIT.items()))))
    assert rule_hash(MIN_FIT) != rule_hash({**MIN_FIT, "when": {**MIN_FIT["when"], "value": 70}})


def test_observed_fingerprint_depends_only_on_referenced_attributes():
    a = observed_fingerprint(PERMANENT_ONLY, {"job.employment_type": "CONTRACT", "fit.overall_score": 10})
    b = observed_fingerprint(PERMANENT_ONLY, {"job.employment_type": "CONTRACT", "fit.overall_score": 99})
    c = observed_fingerprint(PERMANENT_ONLY, {"job.employment_type": "TEMPORARY"})
    assert a == b and a != c


def test_observed_fingerprint_includes_referenced_employer_list_contents():
    """Fix round 2 ruling G: editing a list an in_list rule references changes
    the fingerprint, even though the rule's truth value (and referenced
    attributes) are unchanged; a referenced-but-absent list is UNKNOWN."""
    attrs = {"company.key": "name:acme"}
    fp_small = observed_fingerprint(DENY_LIST, attrs, {"deny": ["name:acme"]})
    fp_grown = observed_fingerprint(DENY_LIST, attrs, {"deny": ["name:acme", "name:other"]})
    fp_list_absent = observed_fingerprint(DENY_LIST, attrs, {})
    fp_no_lists_arg = observed_fingerprint(DENY_LIST, attrs)
    assert fp_small != fp_grown
    assert fp_list_absent == fp_no_lists_arg
    assert fp_small != fp_list_absent


def test_observed_fingerprint_equality_unchanged_when_rule_does_not_reference_a_list():
    """Existing (pre-round-2) equality relations must still hold when the
    rule references no employer list, regardless of what employer_lists holds."""
    fp_no_lists = observed_fingerprint(PERMANENT_ONLY, {"job.employment_type": "CONTRACT"})
    fp_with_unrelated_list = observed_fingerprint(
        PERMANENT_ONLY, {"job.employment_type": "CONTRACT"}, {"deny": ["name:acme"]})
    assert fp_no_lists == fp_with_unrelated_list


@pytest.mark.parametrize("text, expected", [
    ("Permanent", "PERMANENT"), ("Full-time, permanent", "PERMANENT"),
    ("Contract", "CONTRACT"), ("Fixed-term contract", "CONTRACT"),
    ("Temporary", "TEMPORARY"), ("Internship", "INTERNSHIP"),
    ("Full-time", UNKNOWN), (None, UNKNOWN), ("Permanent or contract", UNKNOWN),
])
def test_normalize_employment_type(text, expected):
    assert normalize_employment_type(text) == expected


def test_evaluate_rules_with_float_attribute_no_exception():
    """evaluate_rules with float attr value (wrong type) does not raise, uses on_unknown."""
    doc = _doc(MIN_FIT)
    outcomes = evaluate_rules(doc, {"fit.overall_score": 74.9})
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.via_unknown is True
    assert outcome.applied_effect == {"type": "REDUCE_TO", "level": "PREPARE"}


def test_observed_fingerprint_treats_float_as_unknown():
    """Float attr value is observed as UNKNOWN, fingerprint same as missing attr."""
    from decimal import Decimal
    float_fp = observed_fingerprint(MIN_FIT, {"fit.overall_score": 74.9})
    missing_fp = observed_fingerprint(MIN_FIT, {})
    assert float_fp == missing_fp

    # Also test Decimal("NaN")
    nan_fp = observed_fingerprint(MIN_FIT, {"fit.overall_score": Decimal("NaN")})
    assert nan_fp == missing_fp

    # Also test str on int attribute
    str_fp = observed_fingerprint(MIN_FIT, {"fit.overall_score": "seventy-four"})
    assert str_fp == missing_fp
