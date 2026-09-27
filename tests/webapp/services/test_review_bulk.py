from __future__ import annotations

import pytest

from webapp.persistence import review_approval as ra
from webapp.services import review_approval as svc
from webapp.services import review_documents as rd
from tests.webapp.services.review_fixtures import NOW, V2_ACCOUNT, docx_bytes, v2_chain  # noqa: F401


def _present(world):
    """The page presents what it rendered: the current exposed hash."""
    return svc.record_presented(world.conn, settings=world.settings, account_id=V2_ACCOUNT,
                                application_workspace_id=world.ws, expected_binding_hash=world.state().binding_hash,
                                actor="u", now=NOW)


def _bulk(world, items):
    return svc.approve_selected(world.conn, settings=world.settings, account_id=V2_ACCOUNT, items=items, actor="u",
                                now=NOW)


def _second(world):
    """A second approvable application in the same database."""
    from tests.webapp.services.test_application_pack import _seed_completion_ready
    from webapp.persistence.workspaces import create_workspace
    from webapp.services.application_documents import generate_application_documents, select_application_document
    from webapp.services.application_pack import confirm_application_pack
    from tests.webapp.services.review_fixtures import V2World
    ws = create_workspace(world.conn, company="Beta Corp", title="Engineer")["id"]
    _seed_completion_ready(world.conn, ws)
    generated = generate_application_documents(world.conn, ws, documents_root=world.settings.documents_root,
                                               extensions_dir=world.settings.extensions_dir, account_id=V2_ACCOUNT)
    revisions = {}
    for row in generated["documents"]:
        revisions[row["document_kind"]] = select_application_document(
            world.conn, ws, kind=row["document_kind"], document_version_id=row["id"], expected_revision=0,
            account_id=V2_ACCOUNT)["revision"]
    confirm_application_pack(world.conn, ws, effective_date="2026-09-24", documents_root=world.settings.documents_root,
                             account_id=V2_ACCOUNT, document_selection_revisions=revisions)
    other = V2World(world.conn, ws, world.settings, generated)
    other.set_target(url="https://jobs.example.test/beta/9")
    return other


def test_bulk_creates_one_record_per_application_with_one_batch_id(v2_chain):
    other = _second(v2_chain)
    items = []
    for world in (v2_chain, other):
        world.make_approvable()
        items.append({"workspace_id": world.ws, "displayed_binding_hash": _present(world)})
    out = _bulk(v2_chain, items)
    assert [o["outcome"] for o in out] == ["approved", "approved"]
    batches = {ra.latest_approval(v2_chain.conn, w.ws)["batch_id"] for w in (v2_chain, other)}
    assert len(batches) == 1 and None not in batches


def test_bulk_refuses_only_the_changed_item(v2_chain):
    other = _second(v2_chain)
    items = []
    for world in (v2_chain, other):
        world.make_approvable()
        items.append({"workspace_id": world.ws, "displayed_binding_hash": _present(world)})
    other.set_target(url="https://jobs.example.test/beta/changed")  # changes between list render and click
    out = {o["workspace_id"]: o["outcome"] for o in _bulk(v2_chain, items)}
    assert out[v2_chain.ws] == "approved" and out[other.ws] in ("not_presented", "stale")
    assert ra.latest_approval(v2_chain.conn, other.ws) is None


def test_presented_is_bound_to_the_hash(v2_chain):
    v2_chain.make_approvable()
    shown = _present(v2_chain)
    v2_chain.set_target(url="https://jobs.example.test/acme/other")
    v2_chain.make_approvable()
    current = v2_chain.state().binding_hash
    assert current != shown
    [result] = _bulk(v2_chain, [{"workspace_id": v2_chain.ws, "displayed_binding_hash": current}])
    assert result["outcome"] == "not_presented"


def test_no_presented_and_no_bulk_without_the_exact_pack(v2_chain):
    rd.replace_document(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                        application_workspace_id=v2_chain.ws, kind="cv", filename="x.docx", content=docx_bytes("x"),
                        expected_revision=v2_chain.selection("cv")["revision"], actor="u", now=NOW)
    assert _present(v2_chain) is None
    assert not [e for e in ra.events(v2_chain.conn, v2_chain.ws) if e["event"] == "REVIEW_PRESENTED"]
    [result] = _bulk(v2_chain, [{"workspace_id": v2_chain.ws, "displayed_binding_hash": "sha256:x"}])
    assert result["outcome"] == "not_presented"


def test_unknown_application_is_not_found(v2_chain):
    [result] = _bulk(v2_chain, [{"workspace_id": "ws_nope", "displayed_binding_hash": "sha256:x"}])
    assert result["outcome"] == "not_found"
