from __future__ import annotations

import pytest

from webapp.persistence import review_approval as ra
from webapp.services import review_documents as rd
from webapp.services.review_application import ReviewRefused
from tests.webapp.services.review_fixtures import (  # noqa: F401
    NOW, V2_ACCOUNT, diff_counts, docx_bytes, table_counts, v2_chain,
)


def _replace(world, text="my cv", expected=None, kind="cv"):
    return rd.replace_document(world.conn, settings=world.settings, account_id=V2_ACCOUNT,
                               application_workspace_id=world.ws, kind=kind, filename="mine.docx",
                               content=docx_bytes(text), expected_revision=expected if expected is not None
                               else world.selection(kind)["revision"], actor="u", now=NOW)


def _save(world):
    return rd.save_changes(world.conn, settings=world.settings, account_id=V2_ACCOUNT,
                           application_workspace_id=world.ws, actor="u", now=NOW)


def _events(world, name):
    return [e for e in ra.events(world.conn, world.ws) if e["event"] == name]


def _boom(*a, **k):
    raise RuntimeError("audit failed")


def test_replace_and_its_event_are_atomic(v2_chain, monkeypatch):
    before = table_counts(v2_chain.conn)
    monkeypatch.setattr(rd.ra, "record_event", _boom)
    with pytest.raises(RuntimeError):
        _replace(v2_chain)
    assert diff_counts(before, table_counts(v2_chain.conn)) == {}


def test_select_and_its_event_are_atomic(v2_chain, monkeypatch):
    new = _replace(v2_chain)
    before, selection = table_counts(v2_chain.conn), v2_chain.selection("cv")
    monkeypatch.setattr(rd.ra, "record_event", _boom)
    with pytest.raises(RuntimeError):
        rd.select_document(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                           application_workspace_id=v2_chain.ws, kind="cv",
                           document_version_id=v2_chain.generated["documents"][0]["id"]
                           if v2_chain.generated["documents"][0]["document_kind"] == "cv"
                           else v2_chain.generated["documents"][1]["id"],
                           expected_revision=selection["revision"], actor="u", now=NOW)
    assert diff_counts(before, table_counts(v2_chain.conn)) == {}
    assert v2_chain.selection("cv") == selection and new["document_version_id"] == selection["document_version_id"]


def test_stale_expected_revision_is_refused_with_no_mutation_or_event(v2_chain):
    stale = v2_chain.selection("cv")["revision"] - 1
    before = table_counts(v2_chain.conn)
    with pytest.raises(ReviewRefused) as caught:
        _replace(v2_chain, expected=stale)
    assert caught.value.reason == "stale_selection"
    with pytest.raises(ReviewRefused):
        rd.select_document(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                           application_workspace_id=v2_chain.ws, kind="cv",
                           document_version_id=v2_chain.selection("cv")["document_version_id"],
                           expected_revision=stale, actor="u", now=NOW)
    assert diff_counts(before, table_counts(v2_chain.conn)) == {}


def test_replace_records_history_and_blocks_until_saved(v2_chain):
    _replace(v2_chain)
    [event] = _events(v2_chain, "DOCUMENT_REPLACED")
    assert event["detail"]["kind"] == "cv" and "based_on_generation_artifact_id" in event["detail"]
    assert v2_chain.state().binding_hash is None


def test_save_changes_pack_and_pack_confirmed_are_one_transaction(v2_chain, monkeypatch):
    _replace(v2_chain)
    before = table_counts(v2_chain.conn)
    monkeypatch.setattr(rd.ra, "record_event", _boom)
    with pytest.raises(RuntimeError):
        _save(v2_chain)
    assert diff_counts(before, table_counts(v2_chain.conn)) == {}


def test_save_changes_creates_exactly_one_pack_for_the_current_revisions(v2_chain):
    _replace(v2_chain)
    before = table_counts(v2_chain.conn)
    out = _save(v2_chain)
    delta = diff_counts(before, table_counts(v2_chain.conn))
    assert delta["artifacts"] == 1 and delta["application_review_events"] == 1
    state = v2_chain.state()
    assert state.binding_hash is not None and out["binding_hash"] == state.binding_hash
    [event] = _events(v2_chain, "PACK_CONFIRMED")
    assert event["binding_hash"] == state.binding_hash and event["detail"]["pack_artifact_id"] == out["pack_artifact_id"]


def test_save_changes_works_while_paused_and_halted(v2_chain):
    from webapp.services.autonomy_controls import engage_kill_switch, pause
    _replace(v2_chain)
    pause(v2_chain.conn, account_id=V2_ACCOUNT, scope_type="APPLICATION", scope_id=v2_chain.ws, actor="u",
          reason="r", now=NOW)
    engage_kill_switch(v2_chain.conn, account_id=V2_ACCOUNT, actor="u", reason="stop", now=NOW)
    assert _save(v2_chain)["binding_hash"]


def _regenerate(world):
    from webapp.services.application_documents import generate_application_documents
    return generate_application_documents(world.conn, world.ws, documents_root=world.settings.documents_root,
                                          extensions_dir=world.settings.extensions_dir, account_id=V2_ACCOUNT)


def _newer(world):
    return [w.key for w in world.reviewable().warnings if w.key.startswith("newer_ai_draft")]


def test_pipeline_rerun_never_moves_a_selection_and_the_newer_draft_is_detected_by_generation_pointer(v2_chain):
    selected = {k: v2_chain.selection(k) for k in ("cv", "cover_letter")}
    assert _newer(v2_chain) == []
    _regenerate(v2_chain)
    assert {k: v2_chain.selection(k) for k in ("cv", "cover_letter")} == selected
    assert len(_newer(v2_chain)) == 2


def test_user_document_warns_when_a_newer_generation_exists(v2_chain):
    _replace(v2_chain)
    assert _newer(v2_chain) == []  # based on the current generation
    _regenerate(v2_chain)
    assert any(k.startswith("newer_ai_draft:cv") for k in _newer(v2_chain))


def test_use_the_new_draft_is_an_explicit_selection(v2_chain):
    regenerated = _regenerate(v2_chain)
    cv = next(r for r in regenerated["documents"] if r["document_kind"] == "cv")
    rd.select_document(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                       application_workspace_id=v2_chain.ws, kind="cv", document_version_id=cv["id"],
                       expected_revision=v2_chain.selection("cv")["revision"], actor="u", now=NOW)
    assert not any(k.startswith("newer_ai_draft:cv") for k in _newer(v2_chain))
    assert _events(v2_chain, "SELECTION_CHANGED")
