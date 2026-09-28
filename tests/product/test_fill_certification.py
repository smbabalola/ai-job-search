"""6D-B adapter certification catalogue (spec §7.4, D15; Task 1 S3 ruling:
supported control kinds are framework-specific; React checkbox/radio are not
certified)."""
from __future__ import annotations

import pytest

from product.fill_certification import (
    CATALOGUE, CONTACT_ANSWER_KEYS, FRAMEWORK_CONTROL_KINDS, NETWORK_MODEL, certified, deterministic_classification,
    deterministic_mapping, is_declaration, verify_non_application_rule,
)
from product.semantic_subject_registry import SEMANTIC_SUBJECTS
from tests.product.fill_observation_fixtures import element


def test_generic_unknown_and_pre_fill_adapter_versions_are_never_certified():
    assert certified("generic", "generic@1") is None
    assert certified("unknown", "x@1") is None
    assert certified("greenhouse", "greenhouse@1") is None  # the Phase 3 autofill adapter, not fill-capable
    assert certified("greenhouse", "greenhouse@2") is not None
    assert certified("lever", "lever@2") is not None


def test_every_entry_declares_the_contained_network_model():
    assert CATALOGUE
    for entry in CATALOGUE.values():
        assert entry.network_model == NETWORK_MODEL == "NO_UNCONTAINED_PERSISTENT_CHANNELS"


def test_react_rendered_forms_never_advertise_checkbox_or_radio():
    assert not {"checkbox", "radio"} & FRAMEWORK_CONTROL_KINDS["react"]
    assert {"checkbox", "radio"} <= FRAMEWORK_CONTROL_KINDS["vue"]  # S3 evidence
    for entry in CATALOGUE.values():
        assert entry.supported_control_kinds == FRAMEWORK_CONTROL_KINDS[entry.framework]
        if entry.framework != "vue":
            assert not {"checkbox", "radio"} & entry.supported_control_kinds, entry.adapter_version


def test_every_rule_has_positive_and_negative_fixtures_that_behave():
    for entry in CATALOGUE.values():
        for rule in (*entry.mapping_rules, *entry.classification_rules, *entry.non_application_rules):
            assert rule.positives and rule.negatives, rule.rule_id
            for fixture in rule.positives:
                assert rule.matches(fixture), (rule.rule_id, fixture)
            for fixture in rule.negatives:
                assert not rule.matches(fixture), (rule.rule_id, fixture)


def test_rules_target_only_registered_subjects_contact_keys_or_documents():
    for entry in CATALOGUE.values():
        for rule in entry.mapping_rules:
            kind, target = rule.target
            assert (kind == "answer_key" and target in CONTACT_ANSWER_KEYS) or \
                (kind == "document_kind" and target in ("cv", "cover_letter")), rule.rule_id
        for rule in entry.classification_rules:
            assert rule.target in SEMANTIC_SUBJECTS, rule.rule_id


def test_rule_ids_are_versioned_and_unique_per_adapter():
    for entry in CATALOGUE.values():
        ids = [r.rule_id for r in (*entry.mapping_rules, *entry.classification_rules, *entry.non_application_rules)]
        assert len(ids) == len(set(ids))
        assert all("@" in i for i in ids)


@pytest.mark.parametrize("label,expected", [
    ("Full name", ("answer_key", "contact:full_name")),
    ("First Name", None),                  # 6D-A binds full_name only: a first-name field needs review
    ("Email", ("answer_key", "contact:email")),
    ("Phone", ("answer_key", "contact:phone")),
    ("Location (City)", ("answer_key", "contact:location")),
    ("Reference First Name", None),        # a third party's name
    ("Company location", None),            # an employer-scoped location
    ("Work location", None),
    ("Favourite colour", None),
])
def test_deterministic_contact_mapping(label, expected):
    entry = certified("greenhouse", "greenhouse@2")
    el = element("k", control_kind="text", label=label, question=label)
    result = deterministic_mapping(entry, el)
    assert (result.target if result else None) == expected


def test_file_inputs_map_to_documents_and_text_rules_never_fire_on_files():
    entry = certified("greenhouse", "greenhouse@2")
    cv = element("r", control_kind="file", type="file", label="Resume/CV", question="Resume/CV")
    letter = element("c", control_kind="file", type="file", label="Cover Letter", question="Cover Letter")
    assert deterministic_mapping(entry, cv).target == ("document_kind", "cv")
    assert deterministic_mapping(entry, letter).target == ("document_kind", "cover_letter")
    assert deterministic_mapping(entry, element("e", control_kind="file", type="file", label="Email")) is None


def test_two_matching_rules_are_not_deterministic():
    entry = certified("greenhouse", "greenhouse@2")
    both = element("x", control_kind="text", label="Email or phone", question="Email or phone")
    assert deterministic_mapping(entry, both) is None


@pytest.mark.parametrize("question,subject", [
    ("What is your notice period?", "employment.notice_period"),
    ("Are you legally authorized to work in the UK?", "work_authorization.right_to_work"),
    ("Will you now or in the future require visa sponsorship?", "work_authorization.sponsorship_required"),
    ("What are your salary expectations?", "compensation.salary_expectation"),
    ("How did you hear about us?", None),
])
def test_deterministic_classification(question, subject):
    result = deterministic_classification(certified("greenhouse", "greenhouse@2"), question)
    assert (result.subject if result else None) == subject


@pytest.mark.parametrize("text,expected", [
    ("I certify that the information above is true", True),
    ("By signing, I attest to the above", True),
    ("Electronic signature", True),
    ("Under penalty of perjury", True),
    ("I agree to the privacy notice", True),
    ("Notice period", False),
])
def test_declaration_patterns(text, expected):
    assert is_declaration(certified("greenhouse", "greenhouse@2"), text) is expected


def test_non_application_rules_are_re_verified_on_the_server():
    entry = certified("greenhouse", "greenhouse@2")
    token = element("t", control_kind="hidden", type="hidden", name="authenticity_token", visible=False)
    other = element("o", control_kind="hidden", type="hidden", name="custom_question_4", visible=False)
    assert verify_non_application_rule(entry, "greenhouse.site_state_hidden@1", token)
    assert not verify_non_application_rule(entry, "greenhouse.site_state_hidden@1", other)
    assert not verify_non_application_rule(entry, "greenhouse.unknown@1", token)
