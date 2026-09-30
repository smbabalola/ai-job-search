"""Bundle 7 Task 23 (spec §16.4): /preferences — preferences, job families and
CVs, and rules; every save is a new document version."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
from webapp.app import create_app
from webapp.config import Settings


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        auth_required_in_local=True)
    with TestClient(create_app(settings)) as c:
        publish_legal_documents(settings)
        sign_up_and_verify(c)
        sign_in(c)
        yield c


def _put(client, body):
    return client.put("/api/rules", json=body, headers={"X-CSRF-Token": csrf_token(client)})


def test_the_page_has_three_tabs_and_the_approval_note(client):
    page = client.get("/preferences")
    assert page.status_code == 200
    for text in ("Preferences", "Job families and CVs", "Rules",
                 "applies to new preparations; existing approvals are unaffected"):
        assert text in page.text


def test_well_known_rules_are_saved_as_new_policy_versions(client):
    assert client.get("/api/rules").json()["rules"] == []
    saved = _put(client, {"rule_id": "pref.salary_floor", "params": {"amount": 60000, "currency": "GBP",
                                                                     "effect": "BLOCK", "on_unknown": "REQUIRE_USER"}})
    assert saved.status_code == 200, saved.text
    body = client.get("/api/rules").json()
    assert body["currency"] == "GBP"
    assert [r["id"] for r in body["rules"]] == ["pref.salary_floor"]
    _put(client, {"rule_id": "pref.excluded_employers", "params": {"employers": ["Acme Drilling Ltd"]}})
    body = client.get("/api/rules").json()
    assert sorted(r["id"] for r in body["rules"]) == ["pref.excluded_employers", "pref.salary_floor"]
    assert body["employer_lists"]["pref.excluded_employers"] == ["name:acme drilling ltd"]
    removed = _put(client, {"rule_id": "pref.salary_floor", "params": None})
    assert removed.status_code == 200
    assert [r["id"] for r in client.get("/api/rules").json()["rules"]] == ["pref.excluded_employers"]


def test_an_invalid_rule_is_refused(client):
    refused = _put(client, {"rule_id": "pref.salary_floor", "params": {"amount": 60000, "currency": "GBP",
                                                                       "effect": "BLOCK", "on_unknown": "NO_EFFECT"}})
    assert refused.status_code == 400
    assert _put(client, {"rule_id": "pref.anything", "params": {}}).status_code == 400


def test_the_preferences_form_saves_v2_fields(client):
    workspaces = client.get("/api/search-workspaces").json()["search_workspaces"]
    sw = workspaces[0]["id"]
    saved = client.post("/preferences/profile", data={"search_workspace_id": sw, "rotation_preference": "rotation_only",
                                                      "acceptable_rotations": ["28/28", "14/14"],
                                                      "relocation": "within_country"},
                        headers={"X-CSRF-Token": csrf_token(client)}, follow_redirects=False)
    assert saved.status_code == 303, saved.text
    profile = client.get(f"/api/search-workspaces/{sw}/user-profile").json()["user_profile"]["payload"]
    assert (profile["rotation_preference"], profile["acceptable_rotations"], profile["relocation"]) == (
        "rotation_only", ["14/14", "28/28"], "within_country")
