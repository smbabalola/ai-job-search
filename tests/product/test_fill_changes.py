"""6D-B change classification (spec §13): every row, combined diffs, and the
expected evolution of value states during FILLING."""
from __future__ import annotations

import copy

import pytest

from product.fill_changes import classify_changes
from product.fill_hash import fill_value_hash
from product.fill_observation import field_fingerprint
from tests.product.fill_observation_fixtures import element
from tests.product.fill_plan_fixtures import DOC_SHA, ENTRY, binding, build, page

BASE = page()
PLAN = build(obs=BASE).plan
IDX = {a["page_field_key"]: i for i, a in enumerate(PLAN["actions"])}


def _classify(new, completed=()):
    return classify_changes(BASE, new, plan=PLAN, completed=set(completed), entry=ENTRY)


def _with(mutate):
    new = copy.deepcopy(BASE)
    mutate(new)
    return new


def _set_identity(new, key, **changes):
    for e in new["elements"]:
        if e["page_field_key"] == key:
            e["identity"].update(changes)
            e["field_fingerprint"] = field_fingerprint(e["identity"])


def _set_state(new, key, state):
    for e in new["elements"]:
        if e["page_field_key"] == key:
            e["value_state"] = state


def test_an_unchanged_page_is_clean():
    result = _classify(copy.deepcopy(BASE))
    assert result.deltas == [] and result.stop_reason is None


@pytest.mark.parametrize("extra,kind", [
    (element("gh:decl", label="I certify that all information is true", question="I certify that all information is true",
             name="decl", id="decl"), "DECLARATION"),
    (element("gh:portfolio", control_kind="file", type="file", label="Portfolio", question="Portfolio", name="p", id="p"),
     "NEW_UPLOAD"),
    (element("gh:hear", label="How did you hear about us?", question="How did you hear about us?", name="h", id="h"),
     "NEW_QUESTION"),
])
def test_a_new_application_field_opens_the_right_delta(extra, kind):
    result = _classify(_with(lambda n: n["elements"].append(copy.deepcopy(extra))))
    assert [d.kind for d in result.deltas] == [kind] and result.stop_reason == "DELTA_OPENED"


def test_a_new_classified_question_carries_its_subject():
    extra = element("gh:start", label="When can you start?", question="When can you start?", name="s", id="s")
    [delta] = _classify(_with(lambda n: n["elements"].append(extra))).deltas
    assert delta.subject == "employment.availability_start"


def test_an_omit_field_that_became_required_is_omit_field_required():
    base = page()
    bind = binding(omit=("subject:mobility.relocation",))
    plan = build(obs=base, bind=bind).plan
    new = copy.deepcopy(base)
    _set_identity(new, "gh:relocate", required=True)
    result = classify_changes(base, new, plan=plan, completed=set(), entry=ENTRY)
    assert [(d.kind, d.answer_key) for d in result.deltas] == [("OMIT_FIELD_REQUIRED", "subject:mobility.relocation")]


@pytest.mark.parametrize("changes", [dict(label="Notice period (weeks)", question="Notice period (weeks)"),
                                     dict(required=False)])
def test_changed_wording_or_requiredness_on_a_planned_field_is_changed_question(changes):
    result = _classify(_with(lambda n: _set_identity(n, "gh:notice", **changes)))
    assert [(d.kind, d.answer_key) for d in result.deltas] == [("CHANGED_QUESTION", "subject:employment.notice_period")]


def test_changed_options_on_a_planned_select_is_changed_question():
    result = _classify(_with(lambda n: _set_identity(n, "gh:relocate", options=[
        {"option_value": "", "option_text": "Select"}, {"option_value": "Yes", "option_text": "Yes"}])))
    assert [d.kind for d in result.deltas] == ["CHANGED_QUESTION"]


def test_a_target_change_is_a_target_change_delta():
    result = _classify(_with(lambda n: n["context"].update(canonical_url=n["context"]["canonical_url"] + "?moved=1")))
    [delta] = result.deltas
    assert delta.kind == "TARGET_CHANGE" and delta.observed["canonical_url"].endswith("?moved=1")


@pytest.mark.parametrize("mutate", [
    lambda n: n["elements"].pop(),                                                          # a field removed
    lambda n: n["submit_controls"].append({"control_fingerprint": "sha256:" + "e" * 64}),  # submit controls changed
    lambda n: n["context"]["multi_step_indicators"].append("NEXT_BUTTON"),                  # a wizard appeared
    lambda n: _set_identity(n, "gh:notice", visible=False),                                  # a non-wording change
    lambda n: n["elements"].append(element("x", classification="NON_APPLICATION",
                                           proof={"kind": "OUTSIDE_APPLICATION_ROOT", "rule": None})),
])
def test_structural_changes_stop_as_structure_changed(mutate):
    result = _classify(_with(mutate))
    assert result.deltas == [] and result.stop_reason == "STRUCTURE_CHANGED"


def test_all_deltas_from_one_diff_are_reported_together():
    def mutate(n):
        n["elements"].append(element("gh:hear", label="How did you hear about us?", question="How did you hear about us?",
                                     name="h", id="h"))
        n["elements"].append(element("gh:decl", label="I agree to the terms", question="I agree to the terms",
                                     name="d", id="d"))
        _set_identity(n, "gh:notice", label="Notice (weeks)", question="Notice (weeks)")
    result = _classify(_with(mutate))
    assert sorted(d.kind for d in result.deltas) == ["CHANGED_QUESTION", "DECLARATION", "NEW_QUESTION"]
    assert result.stop_reason == "DELTA_OPENED"


def test_completed_writes_must_hold_their_rendered_value():
    notice = fill_value_hash("1 month")
    ok = _with(lambda n: _set_state(n, "gh:notice", {"state": "NONBLANK", "current_value_hash": notice}))
    assert _classify(ok, completed={IDX["gh:notice"]}).stop_reason is None
    reverted = _with(lambda n: _set_state(n, "gh:notice", {"state": "BLANK"}))
    assert _classify(reverted, completed={IDX["gh:notice"]}).stop_reason == "FIELD_VALUE_REVERTED"


def test_review_focus_5_a_user_typing_into_a_pending_target_is_a_conflict_not_an_overwrite():
    typed = _with(lambda n: _set_state(n, "gh:notice", {"state": "NONBLANK",
                                                        "current_value_hash": fill_value_hash("2 months")}))
    result = _classify(typed)
    assert result.stop_reason == "PREFILLED_VALUE_CONFLICT" and result.detail == {"page_field_key": "gh:notice"}
    equal = _with(lambda n: _set_state(n, "gh:notice", {"state": "NONBLANK",
                                                        "current_value_hash": fill_value_hash("1 month")}))
    assert _classify(equal).stop_reason is None  # already equal is fine (a verified no-op later)


def test_review_focus_5_a_user_editing_a_completed_field_is_reverted():
    edited = _with(lambda n: _set_state(n, "gh:email", {"state": "NONBLANK",
                                                        "current_value_hash": fill_value_hash("other@x.test")}))
    assert _classify(edited, completed={IDX["gh:email"]}).stop_reason == "FIELD_VALUE_REVERTED"


def test_an_omit_field_must_stay_blank():
    base = page()
    bind = binding(omit=("subject:mobility.relocation",))
    plan = build(obs=base, bind=bind).plan
    new = copy.deepcopy(base)
    _set_state(new, "gh:relocate", {"state": "NONBLANK", "current_value_hash": fill_value_hash("No")})
    assert classify_changes(base, new, plan=plan, completed=set(), entry=ENTRY).stop_reason == "OMIT_FIELD_NOT_BLANK"


def test_an_attached_document_must_keep_its_exact_bytes():
    good = _with(lambda n: _set_state(n, "gh:resume", {"state": "NONBLANK",
                                                       "current_value_hash": fill_value_hash(DOC_SHA["cv"])}))
    assert _classify(good, completed={IDX["gh:resume"]}).stop_reason is None
    bad = _with(lambda n: _set_state(n, "gh:resume", {"state": "NONBLANK",
                                                      "current_value_hash": fill_value_hash("f" * 64)}))
    assert _classify(bad, completed={IDX["gh:resume"]}).stop_reason == "FIELD_VALUE_REVERTED"


def test_deltas_take_precedence_over_value_problems():
    def mutate(n):
        n["elements"].append(element("gh:hear", label="How did you hear?", question="How did you hear?", name="h", id="h"))
        _set_state(n, "gh:notice", {"state": "NONBLANK", "current_value_hash": fill_value_hash("2 months")})
    result = _classify(_with(mutate))
    assert result.stop_reason == "DELTA_OPENED" and [d.kind for d in result.deltas] == ["NEW_QUESTION"]
