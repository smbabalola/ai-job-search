"""Bundle 7 Task 21 (spec §14.6): the CV library routes and pages."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
from tests.webapp.services.review_fixtures import docx_bytes
from tests.webapp.services.test_cv_library import pdf_bytes
from webapp.app import create_app
from webapp.config import Settings

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        auth_required_in_local=True)
    with TestClient(create_app(settings)) as c:
        publish_legal_documents(settings)
        sign_up_and_verify(c)
        sign_in(c)
        yield c


def _create(client, title="My CV", content=None, filename="cv.docx", media_type=DOCX):
    return client.post("/api/cvs", data={"title": title, "note": "first"},
                       files={"file": (filename, content if content is not None else docx_bytes(title), media_type)},
                       headers={"X-CSRF-Token": csrf_token(client)})


def test_create_list_add_version_and_download_exact_bytes(client):
    created = _create(client)
    assert created.status_code == 201, created.text
    item_id = created.json()["item"]["id"]
    pdf = pdf_bytes()
    added = client.post(f"/api/cvs/{item_id}/versions", data={"note": "PDF version"},
                        files={"file": ("cv.pdf", pdf, "application/pdf")}, headers={"X-CSRF-Token": csrf_token(client)})
    assert added.status_code == 201, added.text
    version = added.json()["version"]
    assert version["version_no"] == 2 and version["media_type"] == "application/pdf"
    listed = client.get("/api/cvs").json()["items"]
    assert [(i["title"], i["latest_version_no"]) for i in listed] == [("My CV", 2)]
    download = client.get(f"/api/cvs/{item_id}/versions/{version['id']}/download")
    assert download.status_code == 200 and download.content == pdf
    assert download.headers["content-type"] == "application/pdf"
    assert 'filename="cv.pdf"' in download.headers["content-disposition"]


def test_a_refused_upload_returns_its_code(client):
    refused = _create(client, content=b"", filename="cv.docx")
    assert refused.status_code == 400 and refused.json()["error"] == "DOCUMENT_REJECTED"
    assert refused.json()["detail"]["code"] == "EMPTY"
    assert client.get("/api/cvs").json()["items"] == []


def test_the_free_item_limit_refuses_a_new_cv_but_existing_ones_stay_usable(client):
    for n in range(5):  # Free: library.cv_items 5 (dev catalog)
        assert _create(client, title=f"CV {n}").status_code == 201
    refused = _create(client, title="CV 6")
    assert refused.status_code == 402 and refused.json()["error"] == "ALLOWANCE_EXHAUSTED"
    assert refused.json()["detail"]["allowance"] == "library.cv_items"
    items = client.get("/api/cvs").json()["items"]
    assert len(items) == 5
    new_version = client.post(f"/api/cvs/{items[0]['id']}/versions", data={},
                              files={"file": ("cv.docx", docx_bytes("v2"), DOCX)},
                              headers={"X-CSRF-Token": csrf_token(client)})
    assert new_version.status_code == 201  # a new version of an existing CV is not a new item


def test_archive_and_unarchive(client):
    item_id = _create(client).json()["item"]["id"]
    assert client.post(f"/api/cvs/{item_id}/archive", headers={"X-CSRF-Token": csrf_token(client)}).status_code == 200
    assert client.get("/api/cvs").json()["items"][0]["status"] == "ARCHIVED"
    assert client.post(f"/api/cvs/{item_id}/unarchive",
                       headers={"X-CSRF-Token": csrf_token(client)}).status_code == 200


def test_the_pages_render(client):
    item_id = _create(client, title="Drilling CV").json()["item"]["id"]
    index = client.get("/cvs")
    assert index.status_code == 200 and "Drilling CV" in index.text
    page = client.get(f"/cvs/{item_id}")
    assert page.status_code == 200 and "v1" in page.text and "Used by" in page.text


def test_another_accounts_cv_is_404(client, tmp_path):
    item_id = _create(client).json()["item"]["id"]
    other = TestClient(client.app)
    sign_up_and_verify(other, email="eve@example.com")
    sign_in(other, email="eve@example.com")
    assert other.get(f"/cvs/{item_id}").status_code == 404
    assert other.post(f"/api/cvs/{item_id}/archive", headers={"X-CSRF-Token": csrf_token(other)}).status_code == 404
