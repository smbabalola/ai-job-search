from __future__ import annotations

import pytest

from webapp.persistence import review_approval as ra
from webapp.services import review_approval as svc
from webapp.services import review_documents as rd
from webapp.services.review_application import ReviewRefused
from tests.webapp.services.review_fixtures import (  # noqa: F401
    NOW, V2_ACCOUNT, answer, diff_counts, docx_bytes, table_counts, v2_chain,
)

NOTICE = "employment.notice_period"


def _open(world, **kw):
    values = dict(account_id=V2_ACCOUNT, application_workspace_id=world.ws, kind="NEW_QUESTION", answer_key=None,
                  subject=None, required=False, question="Anything else?", observed={"field_key": "f1"},
                  source="FILL_SESSION:s1", now=NOW)
    values.update(kw)
    return svc.open_review_delta(world.conn, **values)


def _mode(world):
    return svc.review_view_mode(world.conn, settings=world.settings, account_id=V2_ACCOUNT,
                                application_workspace_id=world.ws, now=NOW)


def _approved(world):
    world.make_approvable()
    return world.approve()


def _resolved_events(world):
    return [e for e in ra.events(world.conn, world.ws) if e["event"] == "DELTA_RESOLVED"]


def test_delta_makes_needs_review_immediately(v2_chain):
    _approved(v2_chain)
    _open(v2_chain)
    assert v2_chain.state().state == "NEEDS_REVIEW"
    assert any(e["event"] == "DELTA_OPENED" for e in ra.events(v2_chain.conn, v2_chain.ws))


def test_delta_only_when_only_delta_fields_differ(v2_chain):
    _approved(v2_chain)
    d = _open(v2_chain)
    mode = _mode(v2_chain)
    assert mode["mode"] == "delta_only" and mode["delta_keys"] == [f"delta:{d['id']}"]
    assert mode["previous_hash"] == ra.latest_approval(v2_chain.conn, v2_chain.ws)["binding_hash"]


def test_any_other_change_shows_full_changed_sections(v2_chain):
    _approved(v2_chain)
    _open(v2_chain)
    v2_chain.set_target(url="https://jobs.example.test/acme/moved")
    mode = _mode(v2_chain)
    assert mode["mode"] == "full" and "apply_target" in mode["changed_sections"]


def test_first_review_mode_without_an_approval(v2_chain):
    assert _mode(v2_chain)["mode"] == "first_review"


def test_omit_field_reported_mandatory_is_a_delta_and_cannot_be_omitted_again(v2_chain):
    _open(v2_chain, subject=NOTICE, required=False, observed={"field_key": "notice"})  # an optional field
    _approved(v2_chain)  # the user left it blank (OMIT)
    omitted = next(f for f in v2_chain.state().binding["fields"] if f["disposition"] == "OMIT")
    with pytest.raises(ReviewRefused):
        _open(v2_chain, kind="OMIT_FIELD_REQUIRED", answer_key="subject:not-omitted", required=True)
    _open(v2_chain, kind="OMIT_FIELD_REQUIRED", answer_key=omitted["answer_key"], required=False)
    field = next(f for f in v2_chain.reviewable().fields if f.answer_key == omitted["answer_key"])
    assert field.required and field.disposition != "OMIT"
    assert v2_chain.state().state == "NEEDS_REVIEW"


def test_non_field_deltas_force_full_view_and_resolve_only_by_their_component(v2_chain):
    _approved(v2_chain)
    current = v2_chain.state().binding["apply_target"]["canonical_url"]
    target = _open(v2_chain, kind="TARGET_CHANGE", required=True,
                   observed={"canonical_url": "url:jobs.example.test/acme/new-target"})
    upload = _open(v2_chain, kind="NEW_UPLOAD", required=True, observed={"kind": "portfolio"})
    mode = _mode(v2_chain)
    assert mode["mode"] == "full" and "apply_target" in mode["changed_sections"]
    assert target["answer_key"] is None and upload["answer_key"] is None
    assert all(f.answer_key not in (f"delta:{target['id']}", f"delta:{upload['id']}")
               for f in v2_chain.reviewable().fields)
    assert current != target["observed"]["canonical_url"]


def test_unclassified_required_delta_stays_open_after_approval_attempt(v2_chain):
    _approved(v2_chain)
    d = _open(v2_chain, required=True)
    v2_chain.make_approvable()
    with pytest.raises(ReviewRefused) as caught:
        v2_chain.approve()
    assert caught.value.reason == "blocking"
    assert d["id"] in {x["id"] for x in ra.open_deltas(v2_chain.conn, v2_chain.ws)}


def test_intake_normalizes_a_subject_only_field_delta(v2_chain):
    d = _open(v2_chain, subject=NOTICE, required=True)
    assert d["answer_key"] == f"subject:{NOTICE}"


# ---- classification lifecycle (spec §11 R7) -----------------------------------------

def test_unclassified_required_delta_blocks(v2_chain):
    _open(v2_chain, required=True)
    assert any(b.startswith("unclassified_required_question") for b in v2_chain.state().blocking)


def test_classified_successor_closes_only_its_predecessor(v2_chain):
    old = _open(v2_chain, required=True, observed={"field_key": "notice"})
    other = _open(v2_chain, required=True, observed={"field_key": "other"})
    new = _open(v2_chain, subject=NOTICE, required=True, observed={"field_key": "notice"})
    open_ids = {x["id"] for x in ra.open_deltas(v2_chain.conn, v2_chain.ws)}
    assert old["id"] not in open_ids and {other["id"], new["id"]} <= open_ids
    [event] = [e for e in _resolved_events(v2_chain) if e["detail"]["delta_id"] == old["id"]]
    assert event["detail"] == {"delta_id": old["id"], "reason": "classified", "successor_delta_id": new["id"]}


def test_classified_successor_stays_open_with_subject_key(v2_chain):
    _open(v2_chain, required=True, observed={"field_key": "notice"})
    new = _open(v2_chain, subject=NOTICE, required=True, observed={"field_key": "notice"})
    assert new["answer_key"] == f"subject:{NOTICE}"
    assert new["id"] in {x["id"] for x in ra.open_deltas(v2_chain.conn, v2_chain.ws)}
    keys = {f.answer_key for f in v2_chain.reviewable().fields}
    assert f"subject:{NOTICE}" in keys and not any(k.startswith("delta:") for k in keys)


def test_successor_resolves_after_answer_and_approval_leaving_no_open_delta(v2_chain):
    _approved(v2_chain)
    _open(v2_chain, required=True, observed={"field_key": "notice"})
    _open(v2_chain, subject=NOTICE, required=True, observed={"field_key": "notice"})
    answer(v2_chain.conn, NOTICE, "1 month")
    v2_chain.make_approvable()
    v2_chain.approve()
    assert ra.open_deltas(v2_chain.conn, v2_chain.ws) == [] and v2_chain.state().approval_effective


def test_retried_classification_writes_nothing(v2_chain):
    _open(v2_chain, required=True, observed={"field_key": "notice"})
    first = _open(v2_chain, subject=NOTICE, required=True, observed={"field_key": "notice"})
    before = table_counts(v2_chain.conn)
    again = _open(v2_chain, subject=NOTICE, required=True, observed={"field_key": "notice"})
    assert again["id"] == first["id"] and diff_counts(before, table_counts(v2_chain.conn)) == {}


def test_approve_from_a_stale_tab_is_refused(v2_chain):
    v2_chain.make_approvable()
    shown_in_tab_a = v2_chain.state().binding_hash
    rd.replace_document(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                        application_workspace_id=v2_chain.ws, kind="cv", filename="b.docx",
                        content=docx_bytes("tab b"), expected_revision=v2_chain.selection("cv")["revision"],
                        actor="u", now=NOW)
    rd.save_changes(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                    application_workspace_id=v2_chain.ws, actor="u", now=NOW)
    before = table_counts(v2_chain.conn)
    with pytest.raises(ReviewRefused) as caught:
        v2_chain.approve(displayed=shown_in_tab_a)
    assert caught.value.reason == "stale" and diff_counts(before, table_counts(v2_chain.conn)) == {}
