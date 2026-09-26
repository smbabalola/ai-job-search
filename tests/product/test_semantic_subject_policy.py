"""Per-subject policy data for the autonomy contract (spec §6). Each subject
has an answer kind, reach hierarchy, context requirements, freshness window,
submission eligibility, and sensitivity classification."""

from __future__ import annotations

import copy

import pytest

from product.semantic_subject_policy import (
    SubjectPolicyError, load_subject_policy, subject_entry, subject_policy_hash,
    validate_subject_policy,
)
from product.semantic_subject_registry import SEMANTIC_SUBJECTS


def test_policy_covers_exactly_the_registry():
    doc = load_subject_policy()
    assert set(doc["subjects"]) == set(SEMANTIC_SUBJECTS)


def test_baseline_values():
    doc = load_subject_policy()
    salary = subject_entry(doc, "compensation.salary_expectation")
    assert salary["context_keys"] == ["currency", "region", "employment_type"]
    assert salary["freshness_days"] == 60 and salary["max_reach"] == "SEARCH_WORKSPACE"
    assert subject_entry(doc, "work_authorization.right_to_work")["freshness_days"] is None
    assert subject_entry(doc, "motivation.employer_specific")["max_reach"] == "EMPLOYER"
    assert subject_entry(doc, "motivation.role_type")["answer_kind"] == "FREE_TEXT"
    for key in ("legal.attestation", "demographic.eeo", "background.criminal_record", "health.disability"):
        entry = subject_entry(doc, key)
        assert entry["sensitive"] is not None and entry["submit_eligible"] is False
    assert subject_entry(doc, "no.such.subject") is None


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["subjects"].pop("licence.driving"), "missing"),
    (lambda d: d["subjects"].__setitem__("made.up", d["subjects"]["licence.driving"]), "unknown"),
    (lambda d: d["subjects"]["legal.attestation"].update(submit_eligible=True), "sensitive"),
    (lambda d: d["subjects"]["motivation.employer_specific"].update(default_reach="ACCOUNT"), "default_reach"),
    (lambda d: d["subjects"]["licence.driving"].update(freshness_days=0), "freshness_days"),
    (lambda d: d["subjects"]["motivation.role_type"].update(answer_kind="PROSE"), "answer_kind"),
])
def test_invalid_subject_policy_rejected(mutate, message):
    doc = copy.deepcopy(load_subject_policy())
    mutate(doc)
    with pytest.raises(SubjectPolicyError) as exc:
        validate_subject_policy(doc)
    assert any(message in e for e in exc.value.errors), exc.value.errors


def test_hash_is_stable():
    doc = load_subject_policy()
    assert subject_policy_hash(doc) == subject_policy_hash(copy.deepcopy(doc))


PROFILE_FACTS = {"work_authorization.right_to_work", "work_authorization.sponsorship_required", "licence.driving",
                 "employment.notice_period", "employment.availability_start"}


def test_requires_current_profile_basis_baseline():
    # Ruling P: candidate/profile facts need a checkable basis for unattended
    # SUBMIT; preferences, free-text motivation and sensitive subjects do not.
    doc = load_subject_policy()
    flagged = {k for k, e in doc["subjects"].items() if e["requires_current_profile_basis"]}
    assert flagged == PROFILE_FACTS


@pytest.mark.parametrize("mutate", [
    lambda d: d["subjects"]["licence.driving"].pop("requires_current_profile_basis"),
    lambda d: d["subjects"]["licence.driving"].update(requires_current_profile_basis="yes"),
    lambda d: d["subjects"]["licence.driving"].update(requires_current_profile_basis=None),
])
def test_requires_current_profile_basis_is_a_mandatory_boolean(mutate):
    doc = copy.deepcopy(load_subject_policy())
    mutate(doc)
    with pytest.raises(SubjectPolicyError):
        validate_subject_policy(doc)
