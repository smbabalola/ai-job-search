"""Bundle 7 Task 23 (spec §16.2): standing-policy v2 — v1 compatible, new job
attributes, well-known preference rules, manual-mode advisories."""
from __future__ import annotations

import copy

import pytest

from product.rule_attributes import UNKNOWN
from product.standing_policy import (
    STANDING_POLICY_SCHEMA_VERSION, WELL_KNOWN_RULES, StandingPolicyError, build_well_known_rule,
    default_policy_document, evaluate_manual, evaluate_rules, policy_hash, rule_hash, upgrade_policy,
    validate_standing_policy,
)

V1_RULE = {"id": "low-fit", "description": "Low fit", "when": {"attr": "fit.overall_score", "op": "lt", "value": 60},
           "effect": {"type": "REDUCE_TO", "level": "NONE"}, "on_unknown": {"type": "REQUIRE_USER"}}


def _v1(*rules):
    doc = default_policy_document("Europe/London")
    doc["schema_version"] = "standing-policy.v1"
    doc["rules"] = list(rules)
    return doc


def test_the_schema_version_is_v2_and_v1_still_validates_and_upgrades():
    assert STANDING_POLICY_SCHEMA_VERSION == "standing-policy.v2"
    v1 = _v1(V1_RULE)
    validate_standing_policy(v1)
    upgraded = upgrade_policy(v1)
    assert upgraded["schema_version"] == "standing-policy.v2" and upgraded["rules"] == v1["rules"]
    validate_standing_policy(upgraded)
    assert v1["schema_version"] == "standing-policy.v1"  # the input is not mutated


def test_a_v2_policy_with_v1_rules_evaluates_identically_and_keeps_rule_hashes():
    v1 = _v1(V1_RULE)
    v2 = upgrade_policy(v1)
    attrs = {"fit.overall_score": 40}
    assert evaluate_rules(v1, attrs) == evaluate_rules(v2, attrs)
    assert rule_hash(V1_RULE) == evaluate_rules(v2, attrs)[0].rule_hash  # acknowledgements stay bound
    assert policy_hash(v1) != policy_hash(v2)


def test_rules_still_cannot_grant():
    for effect in ({"type": "GRANT"}, {"type": "RAISE_TO", "level": "SUBMIT"}, {"type": "ALLOW"}):
        rule = {**V1_RULE, "effect": effect}
        with pytest.raises(StandingPolicyError):
            validate_standing_policy(upgrade_policy(_v1(rule)))


def test_the_new_attributes_validate():
    doc = upgrade_policy(_v1())
    doc["currency"] = "GBP"
    doc["rules"] = [
        {"id": "family", "description": "d", "when": {"attr": "job.family", "op": "in", "value": ["drilling"]},
         "effect": {"type": "REQUIRE_USER"}, "on_unknown": {"type": "NO_EFFECT"}},
        {"id": "remote", "description": "d", "when": {"attr": "job.remote_mode", "op": "eq", "value": "onsite"},
         "effect": {"type": "REQUIRE_USER"}, "on_unknown": {"type": "NO_EFFECT"}},
    ]
    validate_standing_policy(doc)
    doc["currency"] = "pounds"
    with pytest.raises(StandingPolicyError, match="currency"):
        validate_standing_policy(doc)


def test_the_well_known_rules():
    assert WELL_KNOWN_RULES == {"pref.salary_floor", "pref.excluded_locations", "pref.rotation",
                                "pref.excluded_employers"}
    floor = build_well_known_rule("pref.salary_floor", {"amount": 60_000, "effect": "BLOCK",
                                                        "on_unknown": "REQUIRE_USER"})
    assert floor["when"] == {"attr": "job.compensation_max_annual", "op": "lt", "value": 60_000}
    assert (floor["effect"], floor["on_unknown"]) == ({"type": "BLOCK"}, {"type": "REQUIRE_USER"})
    with pytest.raises(StandingPolicyError, match="BLOCK"):  # never weaken a prohibition on missing data
        build_well_known_rule("pref.salary_floor", {"amount": 60_000, "effect": "BLOCK", "on_unknown": "NO_EFFECT"})
    lenient = build_well_known_rule("pref.salary_floor", {"amount": 60_000, "effect": "REQUIRE_USER",
                                                          "on_unknown": "NO_EFFECT"})
    assert lenient["on_unknown"] == {"type": "NO_EFFECT"}
    places = build_well_known_rule("pref.excluded_locations", {"countries": ["ru", "IR"]})
    assert places["when"] == {"attr": "job.country", "op": "in", "value": ["IR", "RU"]}
    rotation = build_well_known_rule("pref.rotation", {"accepted": ["28/28", "14/14"], "effect": "REQUIRE_USER",
                                                       "on_unknown": "NO_EFFECT"})
    assert rotation["when"] == {"attr": "job.rotation", "op": "not_in", "value": ["14/14", "28/28"]}
    employers = build_well_known_rule("pref.excluded_employers", {})
    assert employers["when"] == {"attr": "company.key", "op": "in_list", "value": "pref.excluded_employers"}
    with pytest.raises(StandingPolicyError):
        build_well_known_rule("pref.unknown", {})


def _salary_policy(amount=60_000, effect="BLOCK", on_unknown="REQUIRE_USER"):
    doc = upgrade_policy(_v1())
    doc["currency"] = "GBP"
    doc["rules"] = [build_well_known_rule("pref.salary_floor", {"amount": amount, "effect": effect,
                                                                "on_unknown": on_unknown})]
    validate_standing_policy(doc)
    return doc


def test_a_day_rate_above_the_floor_is_not_blocked():
    # £250/day → 65,000 a year ≥ the £60k floor
    assert evaluate_manual(_salary_policy(), {"job.compensation_max_annual": 65_000}) == []


def test_below_the_floor_is_a_block_advisory():
    advisories = evaluate_manual(_salary_policy(), {"job.compensation_max_annual": 50_000})
    assert [(a.rule_id, a.effect) for a in advisories] == [("pref.salary_floor", "BLOCK")]
    assert advisories[0].message_key == "rule.pref.salary_floor.BLOCK"


def test_a_foreign_currency_is_unknown_and_on_unknown_applies():
    assert [a.effect for a in evaluate_manual(_salary_policy(), {"job.compensation_max_annual": UNKNOWN})] == [
        "REQUIRE_USER"]
    lenient = _salary_policy(effect="REQUIRE_USER", on_unknown="NO_EFFECT")
    assert evaluate_manual(lenient, {"job.compensation_max_annual": UNKNOWN}) == []


def test_the_input_document_is_never_mutated_by_evaluation():
    doc = _salary_policy()
    before = copy.deepcopy(doc)
    evaluate_manual(doc, {"job.compensation_max_annual": 1})
    assert doc == before
