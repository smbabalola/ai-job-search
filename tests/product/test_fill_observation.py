"""6D-B observation v1 (spec §7): validation, both fingerprints, closed
non-application proofs, unsupported causes and identity ambiguity."""
from __future__ import annotations

import copy

import pytest

from product.fill_certification import CATALOGUE
from product.fill_observation import (
    ObservationError, ambiguous_identities, observation_fingerprint, structure_fingerprint, unsupported_causes,
    validate_observation,
)
from tests.product.fill_observation_fixtures import ORIGIN, element, observation


def _base():
    return observation([
        element("gh:first_name", label="First Name", question="First Name", name="first_name", id="first_name"),
        element("gh:notice", label="Notice period", question="Notice period", name="notice", id="notice"),
    ])


def test_a_well_formed_observation_validates():
    validate_observation(_base())


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("submit_controls"),
    lambda d: d["context"].pop("origin"),
    lambda d: d["elements"][0].pop("value_state"),
    lambda d: d["elements"][0].update(extra="x"),
    lambda d: d["elements"][0].update(classification="MAYBE"),
    lambda d: d["elements"][0].update(control_kind="slider"),
    lambda d: d["elements"][0].update(value_state={"state": "NONBLANK"}),
    lambda d: d["elements"][0].update(value_state={"state": "NONBLANK", "current_value_hash": "plain"}),
    lambda d: d.update(schema_version="fill-observation.v0"),
])
def test_malformed_observations_are_refused(mutate):
    doc = _base()
    mutate(doc)
    with pytest.raises(ObservationError):
        validate_observation(doc)


def test_the_server_recomputes_every_field_fingerprint():
    doc = _base()
    doc["elements"][0]["identity"]["label"] = "Tampered"  # fingerprint no longer matches the identity
    with pytest.raises(ObservationError, match="field_fingerprint"):
        validate_observation(doc)


def test_page_field_keys_must_be_unique():
    doc = _base()
    doc["elements"][1]["page_field_key"] = doc["elements"][0]["page_field_key"]
    with pytest.raises(ObservationError, match="duplicate"):
        validate_observation(doc)


def test_structure_changes_with_identity_but_not_with_value_state():
    base = _base()
    valued = copy.deepcopy(base)
    valued["elements"][0]["value_state"] = {"state": "NONBLANK", "current_value_hash": "sha256:" + "b" * 64}
    assert structure_fingerprint(base) == structure_fingerprint(valued)
    assert observation_fingerprint(base) != observation_fingerprint(valued)
    for change in (dict(label="Given name"), dict(required=True), dict(accept=".pdf"), dict(frame_path="0.1")):
        other = observation([element("gh:first_name", **{**dict(label="First Name", question="First Name",
                                                                   name="first_name", id="first_name"), **change}),
                             base["elements"][1]])
        assert structure_fingerprint(other) != structure_fingerprint(base), change


def test_option_order_is_part_of_the_structure():
    a = observation([element("s", control_kind="select", tag="select",
                             options=[{"option_value": "GB", "option_text": "UK"},
                                      {"option_value": "DK", "option_text": "Denmark"}])])
    b = observation([element("s", control_kind="select", tag="select",
                             options=[{"option_value": "DK", "option_text": "Denmark"},
                                      {"option_value": "GB", "option_text": "UK"}])])
    assert structure_fingerprint(a) != structure_fingerprint(b)


def test_non_application_needs_one_of_the_three_proofs():
    for proof in ({"kind": "OUTSIDE_APPLICATION_ROOT", "rule": None},
                  {"kind": "ADAPTER_NON_APPLICATION_RULE", "rule": "greenhouse.csrf_token@1"},
                  {"kind": "SUBMIT_CLASS_CONTROL", "rule": None}):
        validate_observation(observation([element("x", classification="NON_APPLICATION", proof=proof)]))
    for proof in (None, {"kind": "ROLE_SEARCH", "rule": None}, {"kind": "HIDDEN_INPUT", "rule": None},
                  {"kind": "ADAPTER_NON_APPLICATION_RULE", "rule": None}):
        with pytest.raises(ObservationError):
            validate_observation(observation([element("x", classification="NON_APPLICATION", proof=proof)]))


def test_an_application_element_carries_no_proof():
    with pytest.raises(ObservationError):
        validate_observation(observation([element("x", proof={"kind": "OUTSIDE_APPLICATION_ROOT", "rule": None})]))


def test_hidden_inputs_are_application_by_default():
    doc = observation([element("hidden_x", control_kind="hidden", type="hidden", visible=False)])
    validate_observation(doc)
    assert doc["elements"][0]["classification"] == "APPLICATION"


@pytest.mark.parametrize("doc,cause", [
    (observation([element("x")], adapter_id="generic", adapter_version="generic@1"), "UNCERTIFIED_ADAPTER"),
    (observation([element("x")], adapter_version="greenhouse@1"), "UNCERTIFIED_ADAPTER"),
    (observation([element("x")], root=False), "NO_APPLICATION_ROOT"),
    (observation([element("x")], multi_step=["NEXT_BUTTON"]), "MULTI_STEP"),
    (observation([element("x", frame_path="0.1")],
                 frames=[{"frame_path": "0", "origin": ORIGIN}, {"frame_path": "0.1", "origin": "https://other.test"}]),
     "CROSS_ORIGIN_APPLICATION_FRAME"),
    (observation([element("a", label="Phone", name="p"), element("b", label="Phone", name="p")]),
     "AMBIGUOUS_FIELD_IDENTITY"),
])
def test_unsupported_causes(doc, cause):
    validate_observation(doc)
    assert cause in unsupported_causes(doc, CATALOGUE)


def test_a_clean_certified_observation_has_no_unsupported_cause():
    assert unsupported_causes(_base(), CATALOGUE) == []


def test_identity_ambiguity_is_reported_never_resolved_by_order():
    doc = observation([element("a", label="Phone", name="p"), element("b", label="Phone", name="p"),
                       element("c", label="Email", name="e")])
    assert ambiguous_identities(doc) == [["a", "b"]]


def test_cross_origin_non_application_frames_are_fine():
    doc = observation([element("x"), element("ad", frame_path="0.1", classification="NON_APPLICATION",
                                             proof={"kind": "OUTSIDE_APPLICATION_ROOT", "rule": None})],
                      frames=[{"frame_path": "0", "origin": ORIGIN}, {"frame_path": "0.1", "origin": "https://ads.test"}])
    assert "CROSS_ORIGIN_APPLICATION_FRAME" not in unsupported_causes(doc, CATALOGUE)


def test_adapter_rule_proofs_are_re_verified_against_the_catalogue():
    token = element("t", control_kind="hidden", type="hidden", name="authenticity_token", visible=False,
                    classification="NON_APPLICATION",
                    proof={"kind": "ADAPTER_NON_APPLICATION_RULE", "rule": "greenhouse.site_state_hidden@1"})
    validate_observation(observation([token]), CATALOGUE)
    forged = element("q", control_kind="hidden", type="hidden", name="custom_question_4", visible=False,
                     classification="NON_APPLICATION",
                     proof={"kind": "ADAPTER_NON_APPLICATION_RULE", "rule": "greenhouse.site_state_hidden@1"})
    with pytest.raises(ObservationError, match="does not hold"):
        validate_observation(observation([forged]), CATALOGUE)  # an application field can't be hidden by a false proof
