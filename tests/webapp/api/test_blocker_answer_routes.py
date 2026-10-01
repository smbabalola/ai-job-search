"""Answering an application's open questions (the governing blockers the job
check raised) from the review page. The 6B gate refuses a FILL grant while
any governing blocker is open, so a signed-in user must be able to answer
them; the 6C answer_blocker service does the resolution."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
from tests.webapp.fixtures.journey import install_fake_providers, posting
from tests.webapp.test_free_journey import onboard
from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.db import connect


def _post(client, url, **kwargs):
    return client.post(url, headers={"X-CSRF-Token": csrf_token(client)}, follow_redirects=False, **kwargs)


@pytest.fixture
def world(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        auth_required_in_local=True)
    app = create_app(settings)
    install_fake_providers(app.state)
    with TestClient(app) as client:
        publish_legal_documents(settings)
        sign_up_and_verify(client)
        sign_in(client)
        onboard(client)
        job = posting()
        ws = client.post("/api/workspaces", headers={"X-CSRF-Token": csrf_token(client)}, json={
            "company": job["company"], "title": job["title"], "source_record_origin": "manual_entry",
            "source_record": {"schema_version": "job-source-record.v0", "source": "manual",
                              "captured_at": "2026-10-01T00:00:00Z", "company": job["company"],
                              "title": job["title"], "description": job["text"], "raw_text": job["text"]},
        }).json()["workspace"]["id"]
        for stage in ("understand", "fit"):
            assert _post(client, f"/api/workspaces/{ws}/{stage}", json={"request_id": stage}).status_code == 200
        yield SimpleNamespace(client=client, settings=settings, ws=ws)


def _status(world, blocker_id):
    conn = connect(world.settings)
    try:
        return conn.execute("SELECT status FROM application_blockers WHERE id = ?", (blocker_id,)).fetchone()[0]
    finally:
        conn.close()


def test_the_open_questions_are_listed_and_answered(world):
    listed = world.client.get(f"/api/workspaces/{world.ws}/review/blockers")
    assert listed.status_code == 200, listed.text
    blockers = listed.json()["blockers"]
    assert blockers and all(b["status"] == "open" for b in blockers)
    first = blockers[0]
    assert {"id", "question", "allowed_scopes", "status"} <= set(first)
    answered = _post(world.client, f"/api/workspaces/{world.ws}/review/blockers/{first['id']}/answer",
                     json={"answer": "Yes, I have the right to work in the UK.", "scope": "APPLICATION_ONLY",
                           "request_id": "a1"})
    assert answered.status_code == 200, answered.text
    assert _status(world, first["id"]) == "resolved"
    # a retry with the same request id changes nothing
    again = _post(world.client, f"/api/workspaces/{world.ws}/review/blockers/{first['id']}/answer",
                  json={"answer": "Yes, I have the right to work in the UK.", "scope": "APPLICATION_ONLY",
                        "request_id": "a1"})
    assert again.status_code == 200
    remaining = world.client.get(f"/api/workspaces/{world.ws}/review/blockers").json()["blockers"]
    assert [b["status"] for b in remaining if b["id"] == first["id"]] == ["resolved"]


def test_a_scope_the_question_does_not_allow_is_refused(world):
    blocker = world.client.get(f"/api/workspaces/{world.ws}/review/blockers").json()["blockers"][0]
    refused = _post(world.client, f"/api/workspaces/{world.ws}/review/blockers/{blocker['id']}/answer",
                    json={"answer": "x", "scope": "SEARCH_WORKSPACE", "request_id": "a2"})
    assert refused.status_code == 422, refused.text  # a manual application has no search workspace
    assert _status(world, blocker["id"]) == "open"


def test_another_workspaces_blocker_is_not_found(world):
    blocker = world.client.get(f"/api/workspaces/{world.ws}/review/blockers").json()["blockers"][0]
    other = world.client.post("/api/workspaces", headers={"X-CSRF-Token": csrf_token(world.client)}, json={
        "company": "Other", "title": "Other", "source_record_origin": "manual_entry",
        "source_record": {"schema_version": "job-source-record.v0", "source": "manual",
                          "captured_at": "2026-10-01T00:00:00Z", "company": "Other", "title": "Other",
                          "description": "Other."}}).json()["workspace"]["id"]
    response = _post(world.client, f"/api/workspaces/{other}/review/blockers/{blocker['id']}/answer",
                     json={"answer": "x", "scope": "APPLICATION_ONLY", "request_id": "a3"})
    assert response.status_code == 404
    assert _status(world, blocker["id"]) == "open"


def test_the_review_page_lists_the_open_questions_with_an_answer_form(world):
    blocker = world.client.get(f"/api/workspaces/{world.ws}/review/blockers").json()["blockers"][0]
    page = world.client.get(f"/workspaces/{world.ws}/review")
    assert page.status_code == 200
    assert f'data-blocker="{blocker["id"]}"' in page.text
    done = world.client.post(f"/workspaces/{world.ws}/review/blockers/{blocker['id']}/answer",
                             data={"csrf_token": csrf_token(world.client), "answer": "Yes.",
                                   "scope": "APPLICATION_ONLY"}, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == f"/workspaces/{world.ws}/review"
    assert _status(world, blocker["id"]) == "resolved"


def test_the_answer_is_stored_as_the_plain_value(world):
    """As every other blocker resolution stores it, so the review compares like with like."""
    import json
    blocker = world.client.get(f"/api/workspaces/{world.ws}/review/blockers").json()["blockers"][0]
    _post(world.client, f"/api/workspaces/{world.ws}/review/blockers/{blocker['id']}/answer",
          json={"answer": "yes", "scope": "APPLICATION_ONLY", "request_id": "a4"})
    conn = connect(world.settings)
    try:
        stored = conn.execute("SELECT answer_value FROM blocker_resolutions WHERE blocker_id = ?",
                              (blocker["id"],)).fetchone()[0]
    finally:
        conn.close()
    assert json.loads(stored) == "yes"
