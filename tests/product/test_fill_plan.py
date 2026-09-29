"""6D-B plan builder (spec §8): every §8.2 / §8.3 row, hashes-only plans,
compatible-only review candidates, Review Focus 3 (duplicate options)."""
from __future__ import annotations

import json

import pytest

from product.fill_hash import fill_value_hash
from product.fill_plan import plan_hash
from tests.product.fill_observation_fixtures import element
from tests.product.fill_plan_fixtures import DOC_SHA, VALUES, binding, build, field, page

SENTINELS = [v for v in VALUES.values() if len(v) > 3]  # cleartext that must never appear in a plan


def _action(result, key):
    return next(a for a in result.plan["actions"] if a["page_field_key"] == key)


def test_the_standard_page_builds_a_complete_closed_plan():
    result = build()
    assert result.deltas == [] and result.needs_review == [] and result.unsupported == [] and result.stop is None
    kinds = {a["page_field_key"]: a["action_kind"] for a in result.plan["actions"]}
    assert kinds == {"gh:full_name": "WRITE", "gh:email": "WRITE", "gh:notice": "WRITE", "gh:relocate": "WRITE",
                     "gh:resume": "ATTACH_LOCAL", "gh:csrf": "IGNORE_NON_APPLICATION"}
    notice = _action(result, "gh:notice")
    assert notice["answer_key"] == "subject:employment.notice_period"
    assert notice["mapping_basis"] == "ADAPTER_RULE(greenhouse.notice_period@1)"
    assert notice["rendered_value_hash"] == fill_value_hash("1 month") and notice["transform_id"] == "identity"
    assert _action(result, "gh:email")["mapping_basis"] == "ADAPTER_RULE(greenhouse.contact_email@1)"
    resume = _action(result, "gh:resume")
    assert resume["document"]["sha256"] == DOC_SHA["cv"] and resume["document_kind"] == "cv"
    assert _action(result, "gh:csrf")["mapping_basis"] == "NON_APPLICATION_PROOF(ADAPTER_NON_APPLICATION_RULE)"


def test_the_plan_carries_hashes_never_cleartext():
    text = json.dumps(build().plan)
    for secret in SENTINELS:
        assert secret not in text, secret


def test_plan_hash_is_stable_and_sensitive():
    a, b = build(), build()
    assert plan_hash(a.plan) == plan_hash(b.plan) == a.plan["plan_hash"]
    other = build(bind=binding(extra_fields=[field("subject:employment.availability_start", required=False,
                                                   value="2026-11-01")]))
    assert other.plan["plan_hash"] != a.plan["plan_hash"]  # the binding hash changed


def test_select_option_is_matched_exactly_and_rendered_as_the_option_value():
    relocate = _action(build(), "gh:relocate")
    assert relocate["rendered_value_hash"] == fill_value_hash("Yes")


def test_no_matching_option_is_a_transform_failure_delta():
    values = {**VALUES, "subject:mobility.relocation": "Maybe"}
    bind = binding(extra_fields=[])
    bind["fields"] = [({**f, "value_hash": __import__("product.fill_manifest", fromlist=["value_hash"]).value_hash("Maybe")}
                       if f["answer_key"] == "subject:mobility.relocation" else f) for f in bind["fields"]]
    result = build(bind=bind, values=values)
    assert [(d.kind, d.answer_key) for d in result.deltas] == [("TRANSFORM_FAILURE", "subject:mobility.relocation")]
    assert result.plan is None


def test_review_focus_3_duplicate_options_by_case_or_whitespace_are_a_transform_failure():
    dup = element("gh:relocate", control_kind="select", tag="select", type="select-one",
                  label="Are you willing to relocate?", question="Are you willing to relocate?", name="relocate",
                  id="relocate", options=[{"option_value": "yes ", "option_text": "Yes"},
                                          {"option_value": "YES", "option_text": "yes"}])
    obs = page()
    obs["elements"] = [dup if e["page_field_key"] == "gh:relocate" else e for e in obs["elements"]]
    result = build(obs=obs)
    assert [d.kind for d in result.deltas] == ["TRANSFORM_FAILURE"] and result.plan is None


def test_maxlength_that_cannot_hold_the_value_is_a_transform_failure():
    obs = page()
    for e in obs["elements"]:
        if e["page_field_key"] == "gh:notice":
            e["identity"]["maxlength"] = 3
            from product.fill_observation import field_fingerprint
            e["field_fingerprint"] = field_fingerprint(e["identity"])
    assert [d.kind for d in build(obs=obs).deltas] == ["TRANSFORM_FAILURE"]


def test_a_document_the_page_clearly_cannot_accept_is_a_conversion_delta():
    obs = page()
    for e in obs["elements"]:
        if e["page_field_key"] == "gh:resume":
            e["identity"]["accept"] = ".pdf"
            from product.fill_observation import field_fingerprint
            e["field_fingerprint"] = field_fingerprint(e["identity"])
    result = build(obs=obs)
    assert [(d.kind, d.observed["kind"], d.observed["required_media_type"]) for d in result.deltas] == [
        ("DOCUMENT_CONVERSION", "cv", "application/pdf")]


def test_an_unparseable_accept_list_is_unsupported_not_guessed():
    obs = page()
    for e in obs["elements"]:
        if e["page_field_key"] == "gh:resume":
            e["identity"]["accept"] = "documents please"
            from product.fill_observation import field_fingerprint
            e["field_fingerprint"] = field_fingerprint(e["identity"])
    assert build(obs=obs).unsupported == ["UNSUPPORTED_REQUIRED_WIDGET"]


def test_omit_is_a_non_write_with_a_blank_precondition():
    result = build(bind=binding(omit=("subject:mobility.relocation",)))
    relocate = _action(result, "gh:relocate")
    assert (relocate["action_kind"], relocate["preconditions"], relocate["rendered_value_hash"]) == ("OMIT", "BLANK", None)


def test_an_omitted_field_the_page_requires_is_a_delta():
    obs = page()
    for e in obs["elements"]:
        if e["page_field_key"] == "gh:relocate":
            e["identity"]["required"] = True
            from product.fill_observation import field_fingerprint
            e["field_fingerprint"] = field_fingerprint(e["identity"])
    result = build(obs=obs, bind=binding(omit=("subject:mobility.relocation",)))
    assert [(d.kind, d.answer_key) for d in result.deltas] == [("OMIT_FIELD_REQUIRED", "subject:mobility.relocation")]


def test_prefilled_values_equal_is_a_noop_precondition_and_conflict_stops():
    equal = {"gh:email": {"state": "NONBLANK", "current_value_hash": fill_value_hash("ada@example.com")}}
    assert _action(build(obs=page(value_states=equal)), "gh:email")["preconditions"] == "BLANK_OR_EQUAL"
    conflict = {"gh:email": {"state": "NONBLANK", "current_value_hash": fill_value_hash("someone@else.test")}}
    result = build(obs=page(value_states=conflict))
    assert result.stop == ("PREFILLED_VALUE_CONFLICT", {"page_field_key": "gh:email"}) and result.plan is None


def test_an_omit_field_that_is_not_blank_stops():
    nonblank = {"gh:relocate": {"state": "NONBLANK", "current_value_hash": fill_value_hash("No")}}
    result = build(obs=page(value_states=nonblank), bind=binding(omit=("subject:mobility.relocation",)))
    assert result.stop == ("OMIT_FIELD_NOT_BLANK", {"page_field_key": "gh:relocate"})


PHONE = element("gh:phone", control_kind="tel", type="tel", label="Phone", question="Phone", name="phone", id="phone")


def test_a_new_question_is_a_delta_classified_when_a_rule_says_so():
    extra = element("gh:start", label="When can you start?", question="When can you start?", name="start", id="start")
    unknown = element("gh:hear", label="How did you hear about us?", question="How did you hear about us?",
                      name="hear", id="hear")
    result = build(obs=page(PHONE, extra, unknown))  # every approved field is placed: nothing left to map
    by_key = {d.observed["field_key"]: d for d in result.deltas}
    assert by_key["gh:start"].kind == "NEW_QUESTION" and by_key["gh:start"].subject == "employment.availability_start"
    assert by_key["gh:hear"].kind == "NEW_QUESTION" and by_key["gh:hear"].subject is None


def test_a_declaration_is_a_declaration_delta_with_a_proposal_not_a_classification():
    decl = element("gh:certify", control_kind="text", label="I certify the above is true", question="I certify the above is true",
                   name="certify", id="certify", required=True)
    [delta] = build(obs=page(decl)).deltas
    assert (delta.kind, delta.subject, delta.proposal_subject) == ("DECLARATION", None, "legal.attestation")


def test_an_extra_file_input_beyond_the_approved_documents_is_a_new_upload_delta():
    extra = element("gh:portfolio", control_kind="file", type="file", label="Portfolio", question="Portfolio",
                    name="portfolio", id="portfolio")
    cover = element("gh:cover", control_kind="file", type="file", label="Cover Letter", question="Cover Letter",
                    name="cover_letter", id="cover_letter")
    result = build(obs=page(cover, extra))
    assert [(d.kind, d.observed["field_key"]) for d in result.deltas] == [("NEW_UPLOAD", "gh:portfolio")]


def test_approved_content_without_a_deterministic_mapping_needs_review_with_compatible_candidates_only():
    first = element("gh:first", label="First Name", question="First Name", name="first_name", id="first_name")
    result = build(obs=page(first))
    [row] = result.needs_review
    assert row.page_field_key == "gh:first" and result.plan is None
    # text-renderable approved answers not already placed; never the select-only or document ones
    # only approved fields not already placed on this page (phone is approved but not on the page)
    assert row.candidates == ("contact:phone",)


def test_a_known_meaning_that_is_not_approved_is_a_classified_delta_even_if_answers_are_unplaced():
    start = element("gh:start", label="When can you start?", question="When can you start?", name="start", id="start")
    result = build(obs=page(start))  # contact:phone is still unplaced, but the meaning is known and unapproved
    assert result.needs_review == []
    assert [(d.kind, d.subject) for d in result.deltas] == [("NEW_QUESTION", "employment.availability_start")]


def test_a_field_whose_delta_the_user_answered_maps_to_that_answer():
    from product.fill_plan import build_plan
    from product.review_contract import binding_hash
    from tests.product.fill_plan_fixtures import DOCUMENTS, ENTRY
    hear = element("gh:hear", label="How did you hear about us?", question="How did you hear about us?",
                   name="hear", id="hear")
    bind = binding(extra_fields=[field("delta:dlt_9", value="A friend", subject="x")])
    result = build_plan(observation=page(PHONE, hear), binding=bind, approval_id="apr_1", binding_hash=binding_hash(bind),
                        catalogue_entry=ENTRY, mapping_choices={}, values={**VALUES, "delta:dlt_9": "A friend"},
                        documents=DOCUMENTS,
                        delta_fields={"gh:hear": {"answer_key": "delta:dlt_9", "delta_id": "dlt_9",
                                                  "field_fingerprint": hear["field_fingerprint"]}})
    assert result.deltas == [] and result.needs_review == []
    action = _action(result, "gh:hear")
    assert (action["answer_key"], action["mapping_basis"]) == ("delta:dlt_9", "USER_CONFIRMED(delta:dlt_9)")


def test_a_user_mapping_choice_completes_the_plan_with_a_user_confirmed_basis():
    first = element("gh:first", label="First Name", question="First Name", name="first_name", id="first_name")
    choices = {"gh:first": {"choice": "MAP", "answer_key": "contact:full_name", "field_fingerprint": first["field_fingerprint"],
                            "id": "fmap_1"}}
    result = build(obs=page(first), choices=choices)
    assert result.needs_review == [] and _action(result, "gh:first")["mapping_basis"] == "USER_CONFIRMED(fmap_1)"


def test_a_stale_mapping_choice_for_a_changed_field_is_ignored():
    first = element("gh:first", label="First Name", question="First Name", name="first_name", id="first_name")
    choices = {"gh:first": {"choice": "MAP", "answer_key": "contact:full_name", "field_fingerprint": "sha256:" + "0" * 64,
                            "id": "fmap_1"}}
    assert len(build(obs=page(first), choices=choices).needs_review) == 1


def test_an_incompatible_mapping_choice_is_not_honoured():
    first = element("gh:first", label="First Name", question="First Name", name="first_name", id="first_name")
    choices = {"gh:first": {"choice": "MAP", "answer_key": "document:cv", "field_fingerprint": first["field_fingerprint"],
                            "id": "fmap_1"}}
    assert len(build(obs=page(first), choices=choices).needs_review) == 1


def test_a_new_question_mapping_choice_opens_a_delta():
    first = element("gh:first", label="First Name", question="First Name", name="first_name", id="first_name")
    choices = {"gh:first": {"choice": "NEW_QUESTION", "answer_key": None, "field_fingerprint": first["field_fingerprint"],
                            "id": "fmap_1"}}
    [delta] = build(obs=page(first), choices=choices).deltas
    assert delta.kind == "NEW_QUESTION" and delta.observed["field_key"] == "gh:first"


@pytest.mark.parametrize("kind", ["checkbox", "radio", "custom", "hidden"])
def test_an_approved_answer_on_an_unsupported_widget_is_unsupported_never_downgraded(kind):
    widget = element("gh:w", control_kind=kind, label="What is your notice period?", question="What is your notice period?",
                     name="w", id="w", type=kind if kind != "custom" else "text")
    obs = page()
    obs["elements"] = [widget if e["page_field_key"] == "gh:notice" else e for e in obs["elements"]]
    result = build(obs=obs)
    assert result.unsupported == ["UNSUPPORTED_REQUIRED_WIDGET"] and result.plan is None


def test_an_approved_omit_on_an_unsupported_blank_widget_is_fine():
    widget = element("gh:w", control_kind="custom", label="Are you willing to relocate?",
                     question="Are you willing to relocate?", name="w", id="w")
    obs = page()
    obs["elements"] = [widget if e["page_field_key"] == "gh:relocate" else e for e in obs["elements"]]
    result = build(obs=obs, bind=binding(omit=("subject:mobility.relocation",)))
    assert result.unsupported == [] and _action(result, "gh:w")["action_kind"] == "OMIT"


def test_observation_level_unsupported_causes_are_passed_through():
    obs = page()
    obs["context"]["multi_step_indicators"] = ["NEXT_BUTTON"]
    assert "MULTI_STEP" in build(obs=obs).unsupported


def test_a_page_that_is_not_the_approved_apply_target_is_a_target_change_delta():
    obs = page()
    obs["context"]["canonical_url"] = "https://boards.example-ats.test/acme/jobs/999"
    [delta] = build(obs=obs).deltas
    assert delta.kind == "TARGET_CHANGE" and delta.observed["canonical_url"].endswith("/jobs/999")


def test_the_target_comparison_uses_the_approval_canonicalization():
    obs = page()
    obs["context"]["canonical_url"] = "HTTPS://Boards.Example-ATS.test/acme/jobs/123/"  # same target, other spelling
    assert build(obs=obs).plan is not None
