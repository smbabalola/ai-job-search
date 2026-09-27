"""A2 gate corrections: exact selection revisions, validated v2 packs,
current-only proposals, and just-resolved deltas in the review mode."""
from __future__ import annotations

import pytest

from product.autonomy_contract import Reach
from webapp.persistence import review_approval as ra
from webapp.persistence.artifacts import get_current_artifact, save_artifact
from webapp.persistence.autonomy_answers import save_proposed_answer
from webapp.services import review_answers as rv
from webapp.services import review_approval as svc
from webapp.services import review_documents as rd
from webapp.services.review_application import ReviewRefused
from tests.webapp.services.review_fixtures import (  # noqa: F401
    NOW, V2_ACCOUNT, blocker, diff_counts, docx_bytes, table_counts, v2_chain,
)


def _select(world, kind, version_id):
    return rd.select_document(world.conn, settings=world.settings, account_id=V2_ACCOUNT,
                              application_workspace_id=world.ws, kind=kind, document_version_id=version_id,
                              expected_revision=world.selection(kind)["revision"], actor="u", now=NOW)


def _save(world):
    return rd.save_changes(world.conn, settings=world.settings, account_id=V2_ACCOUNT,
                           application_workspace_id=world.ws, actor="u", now=NOW)


def _pack(world):
    return get_current_artifact(world.conn, world.ws, "application_pack")


# ---- 1. the exact pack binds the exact selection revisions --------------------------

def test_reselecting_the_same_document_needs_save_changes(v2_chain):
    original_cv = v2_chain.selection("cv")["document_version_id"]
    first = v2_chain.state()
    assert _pack(v2_chain)["payload"]["selection_revisions"] == {
        k: v2_chain.selection(k)["revision"] for k in ("cv", "cover_letter")}
    rd.replace_document(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                        application_workspace_id=v2_chain.ws, kind="cv", filename="b.docx", content=docx_bytes("B"),
                        expected_revision=v2_chain.selection("cv")["revision"], actor="u", now=NOW)
    _select(v2_chain, "cv", original_cv)  # the same document ID as the saved pack, a newer revision
    state = v2_chain.state()
    assert state.binding_hash is None and any(b.startswith("save_document_changes") for b in state.blocking)
    with pytest.raises(ReviewRefused) as caught:
        v2_chain.approve(displayed=first.binding_hash)
    assert caught.value.reason == "no_pack"
    _save(v2_chain)
    after = v2_chain.state()
    assert after.binding_hash is not None
    assert _pack(v2_chain)["payload"]["selection_revisions"]["cv"] == v2_chain.selection("cv")["revision"]
    # the documents are identical; the pack content hash still binds the new revisions
    assert after.binding["documents"] == first.binding["documents"]
    assert after.binding["pack"]["content_hash"] != first.binding["pack"]["content_hash"]


def test_a_legacy_v2_pack_without_revisions_is_valid_but_not_exact(v2_chain):
    from product.application_pack_v2_contract import validate_application_pack_v2
    legacy = dict(_pack(v2_chain)["payload"])
    legacy.pop("selection_revisions")
    validate_application_pack_v2(legacy)  # historical shape stays valid
    save_artifact(v2_chain.conn, workspace_id=v2_chain.ws, artifact_type="application_pack", payload=legacy)
    state = v2_chain.state()
    assert state.binding_hash is None and any(b.startswith("save_document_changes") for b in state.blocking)


# ---- 2. a malformed v2 pack is never trusted ------------------------------------------

def test_a_malformed_v2_pack_fails_closed(v2_chain):
    import copy
    broken = copy.deepcopy(_pack(v2_chain)["payload"])
    broken["final_documents"]["cv"]["sha256"] = "not-a-sha"  # ids and revisions still match the selections
    save_artifact(v2_chain.conn, workspace_id=v2_chain.ws, artifact_type="application_pack", payload=broken)
    state = v2_chain.state()
    assert state.binding_hash is None and any(b.startswith("save_document_changes") for b in state.blocking)
    assert {d.kind for d in v2_chain.reviewable().documents} == {"cv", "cover_letter"}  # shown from selections
    assert _save(v2_chain)["binding_hash"]


# ---- 3. only a currently pending proposal can be accepted ------------------------------

NOTICE = "employment.notice_period"


def _accept(world, proposal_id):
    return rv.accept_proposal(world.conn, settings=world.settings, account_id=V2_ACCOUNT,
                              application_workspace_id=world.ws, proposal_id=proposal_id, edited_value=None,
                              reach=Reach.ACCOUNT, actor="u", now=NOW)


def test_a_current_pending_proposal_is_accepted(v2_chain):
    b = blocker(v2_chain.conn, v2_chain.ws, NOTICE)
    proposal = save_proposed_answer(v2_chain.conn, blocker_id=b["id"], subject=NOTICE, value="1 month", now=NOW)
    assert _accept(v2_chain, proposal["id"])["approved_answer_id"]


def test_a_proposal_of_a_superseded_blocker_cannot_be_accepted(v2_chain):
    b = blocker(v2_chain.conn, v2_chain.ws, NOTICE)
    proposal = save_proposed_answer(v2_chain.conn, blocker_id=b["id"], subject=NOTICE, value="1 month", now=NOW)
    save_artifact(v2_chain.conn, workspace_id=v2_chain.ws, artifact_type="job_fit_result", payload={"newer": 1})
    before = table_counts(v2_chain.conn)
    with pytest.raises(ReviewRefused) as caught:
        _accept(v2_chain, proposal["id"])
    assert caught.value.reason == "stale_proposal"
    assert diff_counts(before, table_counts(v2_chain.conn)) == {}


# ---- 4. just-resolved deltas decide the review mode ------------------------------------

def _open(world, **kw):
    values = dict(account_id=V2_ACCOUNT, application_workspace_id=world.ws, kind="NEW_QUESTION", answer_key=None,
                  subject=None, required=False, question="?", observed={"field_key": "f"}, source="s", now=NOW)
    values.update(kw)
    return svc.open_review_delta(world.conn, **values)


def _mode(world):
    return svc.review_view_mode(world.conn, settings=world.settings, account_id=V2_ACCOUNT,
                                application_workspace_id=world.ws, now=NOW)


def _approve(world):
    world.make_approvable()
    return world.approve()


def test_just_resolved_field_delta_is_delta_only_against_the_superseded_approval(v2_chain):
    first = _approve(v2_chain)
    d = _open(v2_chain)
    second = _approve(v2_chain)
    assert d["id"] in second["resolved_delta_ids"]
    mode = _mode(v2_chain)
    assert mode["mode"] == "delta_only" and mode["previous_hash"] == first["binding_hash"]
    assert mode["delta_keys"] == [f"delta:{d['id']}"]


def test_just_resolved_target_change_is_full_with_apply_target_marked(v2_chain):
    from webapp.services.autonomy_context import canonical_target_url
    first = _approve(v2_chain)
    url = "https://jobs.example.test/acme/new-home"
    d = _open(v2_chain, kind="TARGET_CHANGE", required=True, observed={"canonical_url": canonical_target_url(url)})
    v2_chain.set_target(url=url)
    second = _approve(v2_chain)
    assert d["id"] in second["resolved_delta_ids"]
    mode = _mode(v2_chain)
    assert mode["mode"] == "full" and "apply_target" in mode["changed_sections"]
    assert mode["previous_hash"] == first["binding_hash"]


def test_just_resolved_document_conversion_is_full_with_the_document_marked(v2_chain):
    from webapp.persistence.application_documents import get_document_version
    _approve(v2_chain)
    media = get_document_version(v2_chain.conn, v2_chain.selection("cv")["document_version_id"],
                                 account_id=V2_ACCOUNT)["media_type"]
    d = _open(v2_chain, kind="DOCUMENT_CONVERSION", required=True,
              observed={"kind": "cv", "required_media_type": media})
    second = _approve(v2_chain)
    assert d["id"] in second["resolved_delta_ids"]
    mode = _mode(v2_chain)
    assert mode["mode"] == "full" and "document:cv" in mode["changed_sections"]


# ---- A2 final: classification retry from history; view-mode rules A-D ------------------

def test_retried_classification_after_the_successor_resolved_writes_nothing(v2_chain):
    from webapp.services.review_answers import answer_field
    _approve(v2_chain)
    _open(v2_chain, required=True, observed={"field_key": "notice"})
    successor = _open(v2_chain, subject=NOTICE, required=True, observed={"field_key": "notice"})
    answer_field(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT, application_workspace_id=v2_chain.ws,
                 answer_key=f"subject:{NOTICE}", value="1 month", reach=Reach.ACCOUNT, actor="u", now=NOW)
    _approve(v2_chain)
    assert ra.open_deltas(v2_chain.conn, v2_chain.ws) == [] and v2_chain.state().approval_effective
    before = table_counts(v2_chain.conn)
    again = _open(v2_chain, subject=NOTICE, required=True, observed={"field_key": "notice"})
    assert again["id"] == successor["id"] and diff_counts(before, table_counts(v2_chain.conn)) == {}
    assert v2_chain.state().approval_effective  # not reopened


def test_ordinary_approval_without_deltas_is_full_with_no_changes(v2_chain):
    _approve(v2_chain)
    mode = _mode(v2_chain)
    assert mode["mode"] == "full" and mode["changed_sections"] == [] and mode["delta_keys"] == []


def test_a_change_after_a_delta_reapproval_is_full_with_the_new_section(v2_chain):
    _approve(v2_chain)
    _open(v2_chain)
    _approve(v2_chain)
    assert _mode(v2_chain)["mode"] == "delta_only"
    v2_chain.set_target(url="https://jobs.example.test/acme/after-reapproval")
    mode = _mode(v2_chain)
    assert mode["mode"] == "full" and "apply_target" in mode["changed_sections"]
    assert mode["previous_hash"] == ra.latest_approval(v2_chain.conn, v2_chain.ws)["binding_hash"]
