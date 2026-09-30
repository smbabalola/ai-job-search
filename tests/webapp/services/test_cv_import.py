"""Bundle 7 Task 24 (spec §15.3): CV import runs and proposals that require the
user's confirmation. Nothing reaches the profile or the answers before a
resolution; an accept batch is one profile source revision."""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from product.cv_extraction_providers import FakeCvExtractionProvider
from product.entitlements import load_catalog
from tests.webapp.services.review_fixtures import docx_bytes
from webapp.config import Settings
from webapp.persistence import identity
from webapp.persistence.db import connect, init_db
from webapp.services import cv_import
from webapp.services import cv_library as lib
from webapp.services.entitlements import EntitlementGate
from webapp.services.ownership import AccountScope
from webapp.services.usage import Metering, UsageService
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
DEV = load_catalog(Path(__file__).parents[3] / "product" / "plans" / "plan-catalog.dev.json")

EMPLOYMENT = {"target": "PROFILE_ENTRY", "kind": "employment", "confidence": 0.9, "source_excerpt": "Drilling, Acme",
              "fields": {"job_title": "Drilling Engineer", "employer": "Acme Energy", "date_range": "2019 - 2024",
                         "location": "Aberdeen", "details": "Planned and drilled wells."}}
SKILL = {"target": "PROFILE_ENTRY", "kind": "technical_skill", "confidence": 0.8, "source_excerpt": "WellPlan",
         "fields": {"subsection": "Software", "value": "WellPlan"}}
NOTICE = {"target": "ANSWER", "kind": "employment.notice_period", "confidence": 0.7, "source_excerpt": "3 months",
          "fields": {"value": "3 months"}}
INVALID = {"target": "PROFILE_ENTRY", "kind": "hobby", "confidence": 0.9, "source_excerpt": "", "fields": {"value": "x"}}


def blank_pdf() -> bytes:
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)  # a scanned page: no text layer
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    conn = connect(path)
    created = identity.create_user_with_account(
        conn, email="ada@example.com", password_hash="h", display_name="Ada Lovelace", legal_document_ids=[],
        now=NOW, profile_store=DatabaseProfileSourceStore())
    conn.commit()
    scope = AccountScope(account_id=created["account"]["id"], profile_root=tmp_path, user_id=created["user"]["id"],
                         profile_store=DatabaseProfileSourceStore())
    gate = EntitlementGate(DEV, settings=Settings(db_path=path))
    metering = Metering(gate, UsageService(gate), enforced=True, clock=lambda: NOW)
    documents = tmp_path / "documents"
    yield conn, scope, metering, documents
    conn.close()


def _upload(conn, scope, documents, content=None, filename="cv.docx"):
    item = lib.create_item(conn, scope, title="My CV", now=NOW)
    version = lib.add_version(conn, scope, item_id=item["id"],
                              content=content if content is not None else docx_bytes("Drilling Engineer at Acme"),
                              filename=filename, media_type_hint=None, documents_root=documents, now=NOW)
    conn.commit()
    return version


def _import(conn, scope, metering, documents, version, proposals):
    return cv_import.import_cv(conn, scope, document_version_id=version["document_version_id"], metering=metering,
                               provider=FakeCvExtractionProvider(proposals), documents_root=documents, now=NOW)


def _profile_text(conn, scope):
    from webapp.persistence.identity import CANDIDATE_SOURCE
    return scope.profile_sources(conn).reader().read(CANDIDATE_SOURCE)


def _reservations(conn):
    return [tuple(r) for r in conn.execute("SELECT allowance, status FROM usage_reservations")]


def test_a_pdf_without_text_fails_with_no_text_and_releases(world):
    conn, scope, metering, documents = world
    version = _upload(conn, scope, documents, content=blank_pdf(), filename="scan.pdf")
    run = _import(conn, scope, metering, documents, version, [EMPLOYMENT])
    assert (run["status"], run["error_code"]) == ("FAILED", "NO_TEXT")
    assert _reservations(conn) == [("profile.cv_import", "RELEASED")]


def test_proposals_are_stored_and_invalid_ones_dropped_and_counted_without_touching_the_profile(world):
    conn, scope, metering, documents = world
    before = _profile_text(conn, scope)
    version = _upload(conn, scope, documents)
    run = _import(conn, scope, metering, documents, version, [EMPLOYMENT, SKILL, NOTICE, INVALID])
    assert run["status"] == "PROPOSED" and json.loads(run["detail_json"])["dropped"] == 1
    kinds = sorted(p["kind"] for p in cv_import.list_proposals(conn, scope, run_id=run["id"]))
    assert kinds == ["employment", "employment.notice_period", "technical_skill"]
    assert _profile_text(conn, scope) == before  # nothing written before a resolution
    assert conn.execute("SELECT COUNT(*) FROM approved_answers").fetchone()[0] == 0
    assert _reservations(conn) == [("profile.cv_import", "CONSUMED")]


def _proposal(conn, scope, run, kind):
    return next(p for p in cv_import.list_proposals(conn, scope, run_id=run["id"]) if p["kind"] == kind)


def test_an_edited_accept_writes_the_edited_fields_with_cv_import_provenance(world):
    conn, scope, metering, documents = world
    version = _upload(conn, scope, documents)
    run = _import(conn, scope, metering, documents, version, [EMPLOYMENT])
    proposal = _proposal(conn, scope, run, "employment")
    edited = {**EMPLOYMENT["fields"], "job_title": "Senior Drilling Engineer"}
    [resolution] = cv_import.resolve_batch(conn, scope, items=[(proposal["id"], "EDITED_ACCEPTED", edited)], now=NOW)
    assert resolution["resolution"] == "EDITED_ACCEPTED" and resolution["resulting_ref"].startswith("profile-entry-")
    final = json.loads(resolution["final_fields_json"])
    assert final["fields"]["job_title"] == "Senior Drilling Engineer"
    assert final["provenance"] == f"cv_import:{version['document_version_id']}"
    assert "Senior Drilling Engineer" in _profile_text(conn, scope)


def test_a_batch_accept_is_one_profile_source_revision(world):
    conn, scope, metering, documents = world
    version = _upload(conn, scope, documents)
    run = _import(conn, scope, metering, documents, version, [EMPLOYMENT, SKILL])
    before = conn.execute("SELECT COUNT(*) FROM profile_source_revisions").fetchone()[0]
    items = [(p["id"], "ACCEPTED", None) for p in cv_import.list_proposals(conn, scope, run_id=run["id"])]
    resolutions = cv_import.resolve_batch(conn, scope, items=items, now=NOW)
    assert len(resolutions) == 2
    assert conn.execute("SELECT COUNT(*) FROM profile_source_revisions").fetchone()[0] == before + 1
    text = _profile_text(conn, scope)
    assert "Drilling Engineer" in text and "WellPlan" in text


def test_an_answer_proposal_creates_an_approved_answer_and_a_confirmation(world):
    conn, scope, metering, documents = world
    version = _upload(conn, scope, documents)
    run = _import(conn, scope, metering, documents, version, [NOTICE])
    proposal = _proposal(conn, scope, run, "employment.notice_period")
    [resolution] = cv_import.resolve_batch(conn, scope, items=[(proposal["id"], "ACCEPTED", None)], now=NOW)
    answer = conn.execute("SELECT id, subject, value_json FROM approved_answers").fetchone()
    assert answer["subject"] == "employment.notice_period" and json.loads(answer["value_json"]) == {"value": "3 months"}
    assert resolution["resulting_ref"] == answer["id"]
    assert conn.execute("SELECT COUNT(*) FROM answer_confirmations WHERE approved_answer_id = ?",
                        (answer["id"],)).fetchone()[0] == 1


def test_a_rejection_writes_nothing_and_a_proposal_resolves_once(world):
    conn, scope, metering, documents = world
    before = None
    version = _upload(conn, scope, documents)
    run = _import(conn, scope, metering, documents, version, [SKILL])
    before = _profile_text(conn, scope)
    proposal = _proposal(conn, scope, run, "technical_skill")
    cv_import.resolve_batch(conn, scope, items=[(proposal["id"], "REJECTED", None)], now=NOW)
    assert _profile_text(conn, scope) == before
    with pytest.raises(cv_import.ImportError_):
        cv_import.resolve_batch(conn, scope, items=[(proposal["id"], "ACCEPTED", None)], now=NOW)


def test_another_accounts_proposal_is_not_found(world):
    conn, scope, metering, documents = world
    version = _upload(conn, scope, documents)
    run = _import(conn, scope, metering, documents, version, [SKILL])
    other = identity.create_user_with_account(
        conn, email="eve@example.com", password_hash="h", display_name="Eve", legal_document_ids=[], now=NOW,
        profile_store=DatabaseProfileSourceStore())
    conn.commit()
    eve = AccountScope(account_id=other["account"]["id"], profile_root=scope.profile_root, user_id=None,
                       profile_store=DatabaseProfileSourceStore())
    with pytest.raises(cv_import.ImportError_):
        cv_import.resolve_batch(conn, eve, items=[(_proposal(conn, scope, run, "technical_skill")["id"], "ACCEPTED",
                                                   None)], now=NOW)
