"""Bundle 7 Task 22 (spec §14.3, §14.4): per-application CV resolution at
prepare, recorded with why; tailoring bounded by plan; a required choice
blocks approval; a user override is recorded; the approval binds the
resolved version exactly."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from product.entitlements import load_catalog, parse_catalog
from tests.webapp.services.review_fixtures import V2_ACCOUNT, docx_bytes, v2_chain  # noqa: F401
from webapp.config import Settings
from webapp.persistence import identity
from webapp.persistence.artifacts import save_artifact
from webapp.persistence.db import connect, init_db
from webapp.persistence.workspaces import create_workspace
from webapp.services import cv_library as lib
from webapp.services import cv_strategy as cs
from webapp.services.entitlements import EntitlementGate
from webapp.services.ownership import AccountScope
from webapp.services.usage import Metering, UsageService
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
PLANS = Path(__file__).parents[3] / "product" / "plans"
FREE = load_catalog(PLANS / "plan-catalog.dev.json")  # Free: no ai.cv_tailor


def _tailor_catalog():
    doc = json.loads((PLANS / "plan-catalog.dev.json").read_text(encoding="utf-8"))
    doc["catalog_version"] = "test-tailor"
    doc["plans"]["free"]["features"]["ai.cv_tailor"] = True
    doc["plans"]["free"]["allowances"]["cv.tailor"]["limit"] = 5
    return parse_catalog(doc)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    conn = connect(path)
    created = identity.create_user_with_account(
        conn, email="ada@example.com", password_hash="h", display_name="Ada", legal_document_ids=[], now=NOW,
        profile_store=DatabaseProfileSourceStore())
    conn.commit()
    scope = AccountScope(account_id=created["account"]["id"], profile_root=tmp_path, user_id=created["user"]["id"])
    yield conn, scope, tmp_path / "documents", path
    conn.close()


def _cv(conn, scope, documents, title, *contents):
    item = lib.create_item(conn, scope, title=title, now=NOW)
    versions = [lib.add_version(conn, scope, item_id=item["id"], content=docx_bytes(c), filename=f"{title}.docx",
                                media_type_hint=None, documents_root=documents, now=NOW) for c in contents]
    conn.commit()
    return item, versions


def _job(conn, scope, title="Senior Drilling Engineer"):
    ws = create_workspace(conn, company="Acme", title=title, account_id=scope.account_id)["id"]
    save_artifact(conn, workspace_id=ws, artifact_type="job_posting_snapshot", payload={"title": title})
    conn.commit()
    return ws


def _setup(conn, scope, families, strategy):
    cs.save_job_families(conn, scope, {"families": families}, now=NOW)
    cs.save_cv_strategy(conn, scope, strategy, now=NOW)
    conn.commit()


DRILLING = {"id": "drilling", "name": "Drilling", "match": {"title_any": ["drilling"]}, "priority": 0}


def _selected(conn, scope, ws):
    row = conn.execute("SELECT document_version_id FROM application_document_selections WHERE workspace_id = ? "
                       "AND document_kind = 'cv'", (ws,)).fetchone()
    return None if row is None else row[0]


def _metering(path, catalog):
    gate = EntitlementGate(catalog, settings=Settings(db_path=path))
    return Metering(gate, UsageService(gate), enforced=True, clock=lambda: NOW)


def test_fixed_version_selects_that_exact_version(world):
    conn, scope, documents, path = world
    item, (v1, _v2) = _cv(conn, scope, documents, "Drilling CV", "one", "two")
    _setup(conn, scope, [DRILLING], {"default": {"mode": "LATEST_VERSION", "item_id": None},
                                     "by_family": {"drilling": {"mode": "FIXED_VERSION", "version_id": v1["id"]}}})
    ws = _job(conn, scope)
    out = cs.resolve_for_workspace(conn, scope, workspace_id=ws, metering=_metering(path, FREE), now=NOW)
    conn.commit()
    assert (out["outcome"], out["family_id"], out["version_id"]) == ("RESOLVED_VERSION", "drilling", v1["id"])
    assert _selected(conn, scope, ws) == v1["document_version_id"]


def test_latest_version_selects_the_newest_visible(world):
    conn, scope, documents, path = world
    item, (_v1, v2) = _cv(conn, scope, documents, "Drilling CV", "one", "two")
    _setup(conn, scope, [DRILLING], {"default": {"mode": "LATEST_VERSION", "item_id": item["id"]}, "by_family": {}})
    ws = _job(conn, scope, "Office Manager")  # UNKNOWN family → the default
    out = cs.resolve_for_workspace(conn, scope, workspace_id=ws, metering=_metering(path, FREE), now=NOW)
    conn.commit()
    assert (out["outcome"], out["family_id"], out["version_id"]) == ("RESOLVED_VERSION", "UNKNOWN", v2["id"])
    assert json.loads(out["family_match_json"])["reason"] == "no_match"


def test_the_initial_default_needs_a_user_choice(world):
    conn, scope, _, path = world
    ws = _job(conn, scope)
    out = cs.resolve_for_workspace(conn, scope, workspace_id=ws, metering=_metering(path, FREE), now=NOW)
    assert out["outcome"] == "NEEDS_USER_CHOICE" and out["version_id"] is None
    assert _selected(conn, scope, ws) is None


def _fake_generator(calls):
    def generate(conn, scope, *, workspace_id, base_version, template_id, documents_root):
        calls.append((base_version["id"], template_id))
        from webapp.services.application_documents import record_uploaded_version, store_upload_blob
        blob = store_upload_blob(kind="cv", filename="Tailored_CV.docx", content=docx_bytes("tailored"),
                                 documents_root=documents_root, account_id=scope.account_id)
        return record_uploaded_version(conn, workspace_id, kind="cv", filename="Tailored_CV.docx", blob=blob,
                                       account_id=scope.account_id)["id"]
    return generate


def test_tailoring_on_a_plan_with_it_creates_a_hidden_tailored_version_and_consumes(world):
    conn, scope, documents, path = world
    item, (_v1, v2) = _cv(conn, scope, documents, "Drilling CV", "one", "two")
    _setup(conn, scope, [DRILLING], {"default": {"mode": "LATEST_VERSION", "item_id": item["id"]}, "by_family": {
        "drilling": {"mode": "TAILOR_FROM", "item_id": item["id"], "template_id": "standard@1"}}})
    ws = _job(conn, scope)
    metering = _metering(path, _tailor_catalog())
    requested = cs.resolve_for_workspace(conn, scope, workspace_id=ws, metering=metering, now=NOW)
    conn.commit()
    assert requested["outcome"] == "TAILOR_REQUESTED"
    calls = []
    done = cs.fulfil_tailoring(conn, scope, workspace_id=ws, metering=metering, generator=_fake_generator(calls),
                               documents_root=documents, now=NOW)
    assert calls == [(v2["id"], "standard@1")]
    tailored = lib.get_version(conn, account_id=scope.account_id, version_id=done["version_id"])
    assert (tailored["origin"], tailored["library_visible"], tailored["parent_version_id"], tailored["template_id"]) == (
        "AI_TAILORED", 0, v2["id"], "standard@1")
    assert lib.latest_visible_version(conn, account_id=scope.account_id, item_id=item["id"])["id"] == v2["id"]
    assert _selected(conn, scope, ws) == tailored["document_version_id"]
    statuses = [tuple(r) for r in conn.execute("SELECT allowance, status FROM usage_reservations")]
    assert statuses == [("cv.tailor", "CONSUMED")]


def test_tailoring_without_the_feature_falls_back_to_the_latest_and_says_so(world):
    conn, scope, documents, path = world
    item, (_v1, v2) = _cv(conn, scope, documents, "Drilling CV", "one", "two")
    _setup(conn, scope, [DRILLING], {"default": {"mode": "LATEST_VERSION", "item_id": item["id"]}, "by_family": {
        "drilling": {"mode": "TAILOR_FROM", "item_id": item["id"], "template_id": "standard@1"}}})
    ws = _job(conn, scope)
    out = cs.resolve_for_workspace(conn, scope, workspace_id=ws, metering=_metering(path, FREE), now=NOW)
    conn.commit()
    assert (out["outcome"], out["version_id"]) == ("RESOLVED_VERSION", v2["id"])
    assert json.loads(out["rule_json"])["fallback"] == "TAILOR_NOT_IN_PLAN"
    assert conn.execute("SELECT COUNT(*) FROM usage_reservations").fetchone()[0] == 0


def test_resolutions_are_append_only_and_reprepare_appends(world):
    from webapp.persistence import dbapi
    conn, scope, documents, path = world
    item, _ = _cv(conn, scope, documents, "Drilling CV", "one")
    _setup(conn, scope, [DRILLING], {"default": {"mode": "LATEST_VERSION", "item_id": item["id"]}, "by_family": {}})
    ws = _job(conn, scope)
    for _ in range(2):
        cs.resolve_for_workspace(conn, scope, workspace_id=ws, metering=_metering(path, FREE), now=NOW)
        conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM application_cv_resolutions").fetchone()[0] == 2
    with pytest.raises(dbapi.IntegrityError):
        conn.execute("UPDATE application_cv_resolutions SET outcome = 'NEEDS_USER_CHOICE'")
    conn.rollback()


# ---- with the 6D-A review chain -----------------------------------------------------------------

def _chain_scope(v2_chain):
    return AccountScope(account_id=V2_ACCOUNT, profile_root=Path("."), user_id=None)


def _library_version_of_selected_cv(v2_chain):
    """A library item whose v1 is the document the chain already selected."""
    from webapp.persistence import cv_library as rows
    scope = _chain_scope(v2_chain)
    item = lib.create_item(v2_chain.conn, scope, title="Chain CV", now=NOW)
    document_id = v2_chain.selection("cv")["document_version_id"]
    version = rows.insert_version(v2_chain.conn, account_id=V2_ACCOUNT, item_id=item["id"],
                                  document_version_id=document_id, origin="USER_UPLOAD", parent_version_id=None,
                                  template_id=None, library_visible=True, note="", created_by="u", now=NOW)
    v2_chain.conn.commit()
    return item, version


def test_a_required_choice_blocks_approval_until_the_user_chooses(v2_chain):
    from webapp.services.review_approval import ReviewRefused
    item, version = _library_version_of_selected_cv(v2_chain)
    scope = _chain_scope(v2_chain)
    out = cs.resolve_for_workspace(v2_chain.conn, scope, workspace_id=v2_chain.ws, metering=None, now=NOW)
    v2_chain.conn.commit()
    assert out["outcome"] == "NEEDS_USER_CHOICE"
    v2_chain.make_approvable()
    assert any(b.startswith("cv_choice_required") for b in v2_chain.state().blocking)
    with pytest.raises(ReviewRefused, match="blocking"):
        v2_chain.approve()
    chosen = cs.choose_cv(v2_chain.conn, scope, workspace_id=v2_chain.ws, version_id=version["id"], actor="u", now=NOW)
    v2_chain.conn.commit()
    assert chosen["overridden_by_user"] == 1 and chosen["outcome"] == "RESOLVED_VERSION"
    rows = [tuple(r) for r in v2_chain.conn.execute(
        "SELECT outcome, overridden_by_user FROM application_cv_resolutions ORDER BY seq")]
    assert rows == [("NEEDS_USER_CHOICE", 0), ("RESOLVED_VERSION", 1)]
    events = [r[0] for r in v2_chain.conn.execute("SELECT event FROM application_review_events")]
    assert "SELECTION_CHANGED" in events
    v2_chain.make_approvable()
    approval = v2_chain.approve()
    binding = json.loads(v2_chain.conn.execute("SELECT binding_json FROM application_approvals WHERE id = ?",
                                               (approval["approval_id"],)).fetchone()[0])
    cv = [d for d in binding["documents"] if d["kind"] == "cv"][0]
    assert cv["document_version_id"] == version["document_version_id"]  # the approval binds the resolved version


def test_the_cv_used_label(world):
    conn, scope, documents, path = world
    item, (v1,) = _cv(conn, scope, documents, "Drilling CV", "one")
    _setup(conn, scope, [DRILLING], {"default": {"mode": "LATEST_VERSION", "item_id": item["id"]}, "by_family": {}})
    ws = _job(conn, scope)
    cs.resolve_for_workspace(conn, scope, workspace_id=ws, metering=None, now=NOW)
    conn.commit()
    assert cs.cv_used_label(conn, account_id=scope.account_id, workspace_id=ws) == "Drilling CV v1 (uploaded)"
