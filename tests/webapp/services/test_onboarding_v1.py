"""Bundle 7 Task 24 (spec §15.2): onboarding.v1 steps and the readiness that
ai.prepare checks (ONBOARDING_INCOMPLETE lists what is missing)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.webapp.services.review_fixtures import docx_bytes
from webapp.persistence import identity
from webapp.persistence.db import connect, init_db
from webapp.persistence.search_workspaces import list_search_workspaces
from webapp.persistence.user_profile import get_current_user_profile, save_user_profile
from webapp.services import cv_library as lib
from webapp.services import cv_strategy
from webapp.services import onboarding_v1 as ob
from webapp.services.ownership import AccountScope
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    conn = connect(path)
    created = identity.create_user_with_account(
        conn, email="ada@example.com", password_hash="h", display_name="Ada Lovelace", legal_document_ids=[],
        now=NOW, profile_store=DatabaseProfileSourceStore())
    conn.execute("UPDATE users SET email_verified_at = ?, status = 'ACTIVE' WHERE id = ?",
                 (NOW.isoformat(), created["user"]["id"]))
    conn.commit()
    scope = AccountScope(account_id=created["account"]["id"], profile_root=tmp_path, user_id=created["user"]["id"],
                         profile_store=DatabaseProfileSourceStore())
    yield conn, scope, tmp_path / "documents"
    conn.close()


def test_the_steps():
    assert ob.ONBOARDING_STEPS == ("about", "cv", "import", "eligibility", "preferences", "families", "rules",
                                   "extension")
    assert ob.OPTIONAL_STEPS == frozenset({"import", "rules"})


def test_readiness_lists_each_missing_prerequisite_until_satisfied(world):
    conn, scope, documents = world
    ready = ob.readiness(conn, scope, now=NOW)
    assert not ready.prepare_ok
    assert ready.prepare_missing == ["cv", "default_cv_rule", "target_role"]
    assert not ready.fill_ok and ready.fill_missing == ["extension_device"]
    item = lib.create_item(conn, scope, title="My CV", now=NOW)
    lib.add_version(conn, scope, item_id=item["id"], content=docx_bytes("cv"), filename="cv.docx",
                    media_type_hint=None, documents_root=documents, now=NOW)
    conn.commit()
    assert ob.readiness(conn, scope, now=NOW).prepare_missing == ["default_cv_rule", "target_role"]
    cv_strategy.save_cv_strategy(conn, scope, {"default": {"mode": "LATEST_VERSION", "item_id": item["id"]},
                                               "by_family": {}}, now=NOW)
    sw = [w for w in list_search_workspaces(conn, account_id=scope.account_id) if w["status"] == "active"][0]
    current = get_current_user_profile(conn, sw["id"], account_id=scope.account_id)
    save_user_profile(conn, {"target_roles": ["Drilling Engineer"]}, search_workspace_id=sw["id"],
                      expected_revision=current["profile_revision"] if current else 0, account_id=scope.account_id)
    conn.commit()
    assert ob.readiness(conn, scope, now=NOW).prepare_ok


def test_an_unverified_email_is_listed(world):
    conn, scope, _ = world
    conn.execute("UPDATE users SET email_verified_at = NULL WHERE id = ?", (scope.user_id,))
    conn.commit()
    assert "email_verified" in ob.readiness(conn, scope, now=NOW).prepare_missing


def test_steps_are_marked_and_skipping_an_optional_step_completes_it(world):
    conn, scope, _ = world
    state = ob.onboarding_state(conn, scope)
    assert state["steps"] == {step: "TODO" for step in ob.ONBOARDING_STEPS} and not state["complete"]
    for step in ob.ONBOARDING_STEPS:
        ob.mark_step(conn, scope, step, "SKIPPED" if step != "about" else "DONE", now=NOW)
    conn.commit()
    assert ob.onboarding_state(conn, scope)["complete"]


def test_the_first_step_cannot_be_skipped(world):
    conn, scope, _ = world
    with pytest.raises(ValueError):
        ob.mark_step(conn, scope, "about", "SKIPPED", now=NOW)
    with pytest.raises(ValueError):
        ob.mark_step(conn, scope, "made-up", "DONE", now=NOW)


def test_the_migrated_local_account_is_onboarded(tmp_path):
    """The local operator account (configured before Bundle 7) never sees the checklist."""
    from webapp.persistence.bundle7_migrations import _seed_local_onboarding
    path = tmp_path / "db.sqlite3"
    init_db(path)
    conn = connect(path)
    try:
        conn.execute("DELETE FROM account_onboarding")
        from webapp.services.pipeline import ensure_profile_workspace
        from webapp.persistence.artifacts import save_artifact
        ws = ensure_profile_workspace(conn, account_id="account_local")
        save_artifact(conn, workspace_id=ws["id"], artifact_type="profile_snapshot", payload={"claims": []})
        conn.commit()
        _seed_local_onboarding(conn)
        conn.commit()
        local = AccountScope(account_id="account_local", profile_root=tmp_path)
        assert ob.onboarding_state(conn, local)["complete"]
    finally:
        conn.close()


def test_prepare_answers_onboarding_incomplete_with_the_missing_items(tmp_path):
    from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
    from tests.webapp.api.test_workspace_routes import _source_record
    from webapp.app import create_app
    from webapp.config import Settings
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        auth_required_in_local=True)

    class NeverCalled:
        provider_id, model_id, model_version = "fake", "fake", "fake"

        def extract(self, request):
            raise AssertionError("an incomplete onboarding must never reach the AI provider")

    app = create_app(settings)
    app.state.job_understanding_provider = NeverCalled()
    with TestClient(app) as client:
        publish_legal_documents(settings)
        sign_up_and_verify(client)
        sign_in(client)
        headers = {"X-CSRF-Token": csrf_token(client)}
        ws = client.post("/api/workspaces", json={"company": "Acme", "title": "Backend Engineer",
                                                  "source_record": _source_record(),
                                                  "source_record_origin": "manual_entry"},
                         headers=headers).json()["workspace"]["id"]
        response = client.post(f"/api/workspaces/{ws}/understand", json={"request_id": "r1"}, headers=headers)
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["error"] == "ONBOARDING_INCOMPLETE"
        assert body["detail"]["missing"] == ["cv", "default_cv_rule", "target_role"]
        conn = connect(settings)
        try:
            assert conn.execute("SELECT COUNT(*) FROM usage_reservations").fetchone()[0] == 0  # nothing charged
        finally:
            conn.close()
