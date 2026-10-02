"""Bundle 7 Task 22 (spec §14.6): the job families / CV strategy API and editor."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
from tests.webapp.services.review_fixtures import docx_bytes
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


def _h(client):
    return {"X-CSRF-Token": csrf_token(client)}


def _cv(client, title):
    return client.post("/api/cvs", data={"title": title}, files={"file": ("cv.docx", docx_bytes(title), DOCX)},
                       headers=_h(client)).json()


def test_documents_default_and_validate(client):
    assert client.get("/api/job-families").json() == {"doc": {"families": []}, "doc_hash": None}
    assert client.get("/api/cv-strategy").json()["doc"]["default"] == {"mode": "LATEST_VERSION", "item_id": None}
    bad = client.put("/api/cv-strategy", json={"default": {"mode": "LATEST_VERSION", "item_id": "cvi_foreign"},
                                                "by_family": {}}, headers=_h(client))
    assert bad.status_code == 400
    item = _cv(client, "Drilling CV")["item"]
    families = client.put("/api/job-families", json={"families": [{"id": "drilling", "name": "Drilling",
                                                                     "match": {"title_any": ["drilling"]},
                                                                     "priority": 0}]}, headers=_h(client))
    assert families.status_code == 200 and families.json()["doc_hash"].startswith("sha256:")
    ok = client.put("/api/cv-strategy", json={"default": {"mode": "LATEST_VERSION", "item_id": item["id"]},
                                              "by_family": {"drilling": {"mode": "LATEST_VERSION",
                                                                         "item_id": item["id"]}}}, headers=_h(client))
    assert ok.status_code == 200


def test_the_strategy_editor_saves_families_and_rules(client):
    drilling = _cv(client, "Drilling CV")["item"]
    general = _cv(client, "General CV")["item"]
    page = client.get("/cvs/strategy")
    assert page.status_code == 200 and "Drilling CV" in page.text
    saved = client.post("/cvs/strategy", data={
        "default_cv": f"latest:{general['id']}",
        "family_name_0": "Drilling engineer", "family_words_0": "drilling, well engineer", "family_exclude_0": "sales",
        "family_cv_0": f"latest:{drilling['id']}",
        "family_name_1": "", "family_words_1": "", "family_cv_1": ""}, headers=_h(client), follow_redirects=False)
    assert saved.status_code == 303, saved.text
    families = client.get("/api/job-families").json()["doc"]["families"]
    assert [(f["id"], f["match"]["title_any"], f["match"]["title_none"]) for f in families] == [
        ("drilling-engineer", ["drilling", "well engineer"], ["sales"])]
    strategy = client.get("/api/cv-strategy").json()["doc"]
    assert strategy["default"] == {"mode": "LATEST_VERSION", "item_id": general["id"]}
    assert strategy["by_family"] == {"drilling-engineer": {"mode": "LATEST_VERSION", "item_id": drilling["id"]}}


def test_a_workspace_cv_choice_is_recorded(client):
    cv = _cv(client, "Drilling CV")
    from tests.webapp.api.test_workspace_routes import _source_record
    created = client.post("/api/workspaces", json={"company": "Acme", "title": "Backend Engineer",
                                                   "source_record": _source_record(),
                                                   "source_record_origin": "manual_entry"}, headers=_h(client))
    ws = created.json()["workspace"]["id"]
    chosen = client.post(f"/api/workspaces/{ws}/cv-choice", json={"version_id": cv["version"]["id"]},
                         headers=_h(client))
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["resolution"]["overridden_by_user"] == 1
    assert client.post(f"/api/workspaces/{ws}/cv-choice", json={"version_id": "cvv_nope"},
                       headers=_h(client)).status_code == 404


def test_the_workspace_page_shows_the_cv_used(client):
    from tests.webapp.api.test_workspace_routes import _source_record
    cv = _cv(client, "Drilling CV")
    ws = client.post("/api/workspaces", json={"company": "Acme", "title": "Backend Engineer",
                                              "source_record": _source_record(), "source_record_origin": "manual_entry"},
                     headers=_h(client)).json()["workspace"]["id"]
    before = client.get(f"/workspaces/{ws}")
    assert before.status_code == 200 and "Drilling CV (v1)" in before.text and "CV used:" not in before.text
    client.post(f"/api/workspaces/{ws}/cv-choice", json={"version_id": cv["version"]["id"]}, headers=_h(client))
    assert "CV used:</strong> Drilling CV v1 (uploaded)" in client.get(f"/workspaces/{ws}").text
