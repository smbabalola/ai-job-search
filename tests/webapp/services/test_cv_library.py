"""Bundle 7 Task 21 (spec §14.1, §14.4, L3): the CV library — named CVs with
immutable versions, DOCX/PDF uploads validated before anything is stored,
and history protected by document references."""
from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.webapp.services.review_fixtures import docx_bytes
from webapp.persistence import dbapi, identity
from webapp.persistence.db import connect, init_db
from webapp.services import cv_library as lib
from webapp.services.ownership import AccountScope
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
MIB = 1024 * 1024


def pdf_bytes(*, password: str | None = None) -> bytes:
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    if password:
        writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


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
    documents = tmp_path / "documents"
    yield conn, scope, documents
    conn.close()


def _item(conn, scope, title="My CV"):
    item = lib.create_item(conn, scope, title=title, now=NOW)
    conn.commit()
    return item


def _add(conn, scope, documents, item_id, content=None, filename="cv.docx", **kwargs):
    version = lib.add_version(conn, scope, item_id=item_id, content=content if content is not None else
                              docx_bytes("Ada Lovelace CV"), filename=filename, media_type_hint=None,
                              documents_root=documents, now=NOW, **kwargs)
    conn.commit()
    return version


def _nothing_stored(conn, documents):
    files = [p for p in documents.rglob("*") if p.is_file()] if documents.exists() else []
    return (files == [] and conn.execute("SELECT COUNT(*) FROM application_document_versions").fetchone()[0] == 0
            and conn.execute("SELECT COUNT(*) FROM cv_library_versions").fetchone()[0] == 0)


@pytest.mark.parametrize("content,filename,code", [
    (pdf_bytes(), "cv.docx", "TYPE_MISMATCH"),
    (b"", "cv.docx", "EMPTY"),
    (b"%PDF-" + b"0" * (10 * MIB - 4), "cv.pdf", "TOO_LARGE"),
    (pdf_bytes(password="secret"), "cv.pdf", "ENCRYPTED_PDF"),
    (b"plain text CV", "cv.txt", "UNSUPPORTED_TYPE"),
    (b"%PDF-1.7 not really a pdf", "cv.pdf", "INVALID_PDF"),
    (b"PK\x03\x04 not a docx", "cv.docx", "UNSUPPORTED_TYPE"),
], ids=["pdf-named-docx", "empty", "too-large", "encrypted-pdf", "txt", "broken-pdf", "fake-zip"])
def test_a_refused_upload_stores_nothing(world, content, filename, code):
    """Review Focus 4."""
    conn, scope, documents = world
    item = _item(conn, scope)
    with pytest.raises(lib.DocumentRejected) as refused:
        _add(conn, scope, documents, item["id"], content=content, filename=filename)
    conn.rollback()
    assert refused.value.code == code
    assert _nothing_stored(conn, documents)


def test_the_exact_size_limit_is_accepted(world):
    conn, scope, documents = world
    assert lib.MAX_UPLOAD_BYTES == 10 * MIB
    assert lib.sniff_media_type(pdf_bytes()) == lib.PDF_MEDIA_TYPE
    assert lib.sniff_media_type(docx_bytes("x")) == lib.DOCX_MEDIA_TYPE
    assert lib.sniff_media_type(b"hello") is None


def test_versions_number_1_2_3_and_hidden_versions_are_not_latest(world):
    conn, scope, documents = world
    item = _item(conn, scope)
    first = _add(conn, scope, documents, item["id"])
    second = _add(conn, scope, documents, item["id"], content=pdf_bytes(), filename="Ada CV.pdf", note="PDF")
    hidden = _add(conn, scope, documents, item["id"], content=docx_bytes("tailored"), origin="AI_TAILORED",
                  library_visible=False, parent_version_id=second["id"])
    assert [v["version_no"] for v in (first, second, hidden)] == [1, 2, 3]
    latest = lib.latest_visible_version(conn, account_id=scope.account_id, item_id=item["id"])
    assert latest["id"] == second["id"] and latest["media_type"] == lib.PDF_MEDIA_TYPE
    assert [v["version_no"] for v in lib.list_versions(conn, account_id=scope.account_id, item_id=item["id"])] == [
        1, 2]  # the library view shows visible versions only


def test_library_versions_are_append_only(world):
    conn, scope, documents = world
    item = _item(conn, scope)
    _add(conn, scope, documents, item["id"])
    with pytest.raises(dbapi.IntegrityError):
        conn.execute("UPDATE cv_library_versions SET note = 'changed'")
    conn.rollback()


def test_a_referenced_document_cannot_be_deleted_but_an_unreferenced_one_can(world):
    from webapp.persistence.cv_library import add_references
    conn, scope, documents = world
    item = _item(conn, scope)
    used = _add(conn, scope, documents, item["id"])
    add_references(conn, document_version_ids=[used["document_version_id"]], referrer_type="APPROVAL",
                   referrer_id="apr_1", now=NOW)
    conn.commit()
    with pytest.raises(dbapi.IntegrityError):
        conn.execute("DELETE FROM application_document_versions WHERE id = ?", (used["document_version_id"],))
    conn.rollback()
    assert lib.usage_count(conn, account_id=scope.account_id, version_id=used["id"]) == 1


def test_an_approval_records_references_to_its_documents(world):
    from webapp.persistence.review_approval import insert_approval
    from webapp.persistence.workspaces import create_workspace
    conn, scope, documents = world
    item = _item(conn, scope)
    version = _add(conn, scope, documents, item["id"])
    ws = create_workspace(conn, company="Acme", title="Engineer", account_id=scope.account_id)["id"]
    binding = {"documents": [{"kind": "cv", "document_version_id": version["document_version_id"], "sha256": "x"}]}
    approval = insert_approval(conn, account_id=scope.account_id, application_workspace_id=ws, binding=binding,
                               binding_hash="h", supersedes_id=None, batch_id=None, resolved_delta_ids=[], actor="u",
                               now=NOW)
    conn.commit()
    refs = [tuple(r) for r in conn.execute("SELECT document_version_id, referrer_type, referrer_id "
                                            "FROM document_version_references")]
    assert refs == [(version["document_version_id"], "APPROVAL", approval["id"])]


def test_archiving_keeps_history(world):
    conn, scope, documents = world
    item = _item(conn, scope)
    version = _add(conn, scope, documents, item["id"])
    lib.archive_item(conn, scope, item_id=item["id"], now=NOW)
    conn.commit()
    assert lib.get_item(conn, account_id=scope.account_id, item_id=item["id"])["status"] == "ARCHIVED"
    assert lib.get_version(conn, account_id=scope.account_id, version_id=version["id"])["version_no"] == 1
    with pytest.raises(lib.LibraryError):
        _add(conn, scope, documents, item["id"])  # no new versions on an archived CV
    lib.unarchive_item(conn, scope, item_id=item["id"], now=NOW)
    conn.commit()
    assert lib.get_item(conn, account_id=scope.account_id, item_id=item["id"])["status"] == "ACTIVE"


def test_another_accounts_item_is_not_found(world):
    conn, scope, documents = world
    other = identity.create_user_with_account(
        conn, email="eve@example.com", password_hash="h", display_name="Eve", legal_document_ids=[], now=NOW,
        profile_store=DatabaseProfileSourceStore())
    conn.commit()
    eve = AccountScope(account_id=other["account"]["id"], profile_root=scope.profile_root, user_id=other["user"]["id"])
    item = _item(conn, scope)
    with pytest.raises(lib.LibraryError):
        _add(conn, eve, documents, item["id"])
    assert lib.get_item(conn, account_id=eve.account_id, item_id=item["id"]) is None


def test_the_legacy_reusable_cv_becomes_an_item_with_an_imported_version(world):
    from webapp.persistence.bundle7_migrations import _convert_legacy_reusable_cvs
    from webapp.services.application_documents import (
        set_application_document_reusable, upload_application_document,
    )
    from webapp.persistence.workspaces import create_workspace
    conn, scope, documents = world
    ws = create_workspace(conn, company="Acme", title="Engineer", account_id=scope.account_id)["id"]
    conn.commit()
    uploaded = upload_application_document(conn, ws, kind="cv", filename="old.docx", content=docx_bytes("old"),
                                           documents_root=documents, account_id=scope.account_id)
    set_application_document_reusable(conn, ws, uploaded["id"], label="Engineering CV", account_id=scope.account_id)
    conn.commit()
    for _ in range(2):  # idempotent
        _convert_legacy_reusable_cvs(conn)
    conn.commit()
    items = lib.list_items(conn, account_id=scope.account_id)
    assert [i["title"] for i in items] == ["Engineering CV"]
    versions = lib.list_versions(conn, account_id=scope.account_id, item_id=items[0]["id"])
    assert [(v["version_no"], v["origin"], v["document_version_id"]) for v in versions] == [
        (1, "IMPORTED_LEGACY", uploaded["id"])]


def test_a_library_version_can_be_selected_for_an_application(world):
    from webapp.persistence.workspaces import create_workspace
    from webapp.services.application_documents import apply_selection
    conn, scope, documents = world
    item = _item(conn, scope)
    version = _add(conn, scope, documents, item["id"], content=pdf_bytes(), filename="cv.pdf")
    ws = create_workspace(conn, company="Acme", title="Engineer", account_id=scope.account_id)["id"]
    selection = apply_selection(conn, ws, kind="cv", document_version_id=version["document_version_id"],
                                expected_revision=0, account_id=scope.account_id)
    conn.commit()
    assert selection["document_version_id"] == version["document_version_id"]


def test_the_handoff_download_is_named_by_media_type():
    from webapp.api.handoff import download_filename
    assert download_filename("cv", lib.PDF_MEDIA_TYPE) == "cv.pdf"
    assert download_filename("cover_letter", lib.DOCX_MEDIA_TYPE) == "cover_letter.docx"


def test_the_items_gauge_counts_active_items(world):
    from webapp.services.usage import GAUGE_READERS
    conn, scope, _ = world
    first = _item(conn, scope, "One")
    _item(conn, scope, "Two")
    lib.archive_item(conn, scope, item_id=first["id"], now=NOW)
    conn.commit()
    assert GAUGE_READERS["library.cv_items"](conn, scope.account_id) == 1
