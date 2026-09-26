from __future__ import annotations

import pytest

from webapp.persistence.review import SYSTEM_AUTO_CONFIRMED, save_review_decision
from webapp.services.application_pack import (
    SYSTEM_GATE4_NOTE, OutstandingReviewItems, list_outstanding_review_items, system_confirm_application_pack,
)
from webapp.services.pipeline import PipelineError
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, conn, make_workspace, prepared_chain, ready_chain  # noqa: F401


def test_system_provenance_requires_a_basis_and_acknowledgement(conn):
    from webapp.persistence.artifacts import save_artifact
    ws = make_workspace(conn)
    art = save_artifact(conn, workspace_id=ws, artifact_type="application_intelligence_result", payload={"x": 1})
    common = dict(workspace_id=ws, review_item_type="content_unit", source_artifact_id=art["id"], domain_item_id="u1")
    with pytest.raises(ValueError):
        save_review_decision(conn, disposition="acknowledged_and_proceed", decision_provenance=SYSTEM_AUTO_CONFIRMED,
                             **common)
    with pytest.raises(ValueError):
        save_review_decision(conn, disposition="omit_from_positioning", decision_provenance=SYSTEM_AUTO_CONFIRMED,
                             system_basis={"reason": "grounded_ready_unit", "item_content_hash": "h",
                                           "pack_revision": "r"}, **common)
    with pytest.raises(ValueError):
        save_review_decision(conn, disposition="acknowledged_and_proceed", system_basis={"reason": "x"}, **common)
    row = save_review_decision(conn, disposition="acknowledged_and_proceed", decision_provenance=SYSTEM_AUTO_CONFIRMED,
                               system_basis={"reason": "grounded_ready_unit", "item_content_hash": "h",
                                             "pack_revision": "r"}, **common)
    assert row["decision_provenance"] == SYSTEM_AUTO_CONFIRMED and '"pack_revision"' in row["system_basis_json"]
    assert save_review_decision(conn, disposition="acknowledged_and_proceed", **common)["decision_provenance"] == "USER"


def test_outstanding_items_are_structured_and_subclass_pipeline_error(prepared_chain):
    conn, ws, settings = prepared_chain
    items = list_outstanding_review_items(conn, ws, extensions_dir=settings.extensions_dir, account_id=ACCOUNT)
    assert items and {"item_type", "item_id", "source_artifact_id", "source"} <= set(items[0])
    assert issubclass(OutstandingReviewItems, PipelineError)
    assert any(i["item_type"] == "content_unit" for i in items)


def test_system_confirm_aborts_when_precheck_raises_and_writes_nothing(ready_chain):
    conn, ws, settings = ready_chain
    before = conn.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0]

    def refuse(pack, profile_artifact):
        raise PipelineError("authority changed")
    with pytest.raises(PipelineError):
        system_confirm_application_pack(conn, ws, effective_date="2026-09-24", documents_root=settings.documents_root,
                                        extensions_dir=settings.extensions_dir, account_id=ACCOUNT, precheck=refuse)
    assert conn.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0] == before


def test_system_confirm_writes_system_note(ready_chain):
    conn, ws, settings = ready_chain
    out = system_confirm_application_pack(conn, ws, effective_date="2026-09-24", documents_root=settings.documents_root,
                                          extensions_dir=settings.extensions_dir, account_id=ACCOUNT,
                                          precheck=lambda pack, profile: None)
    assert out["workflow_event"]["note"] == SYSTEM_GATE4_NOTE
    assert out["workflow_event"]["new_status"] == "drafted"
