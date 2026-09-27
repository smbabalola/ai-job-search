"""Bundle 6D-A Task 11 step 1: pin the Phase 3 human handoff lifecycle before
the SUBMIT guard, so the guard provably does not affect it. The human (not
JobSearch) submits; JobSearch records the user's own confirmation."""
from __future__ import annotations

from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.persistence.artifacts import save_artifact
from webapp.persistence.db import connect
from webapp.persistence.workspaces import create_workspace, ensure_profile_workspace
from tests.webapp.test_handoff_browser_smoke import _live_server_settings


def test_human_handoff_lifecycle_is_unchanged(tmp_path):
    settings = _live_server_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        ensure_profile_workspace(conn)
        workspace = create_workspace(conn, company="Acme", title="Engineer")
        artifact = save_artifact(conn, workspace_id=workspace["id"], artifact_type="application_pack",
                                 payload={"schema_version": "application-pack.v1"})
        conn.close()

        one_time = client.post("/api/handoff/pairing/generate").json()["one_time_secret"]
        credential = client.post("/api/handoff/pairing/exchange",
                                 json={"one_time_secret": one_time}).json()["durable_secret"]
        started = client.post(
            "/api/handoff/sessions", headers={"X-Handoff-Credential": credential},
            json={"workspace_id": workspace["id"], "pack_artifact_id": artifact["id"],
                  "target_url": "http://testserver/test-fixtures/handoff/generic_fixture.html",
                  "target_domain": "testserver", "ats_adapter_id": "generic", "ats_adapter_version": "generic@1"})
        assert started.status_code == 201
        session_id = started.json()["id"]
        headers = {"X-Handoff-Session-Token": started.json()["session_token"]}
        for field_name, value in [("name", "Test User"), ("email", "test@example.com")]:
            response = client.post(
                f"/api/handoff/sessions/{session_id}/events", headers=headers,
                json={"event_id": f"evt_{field_name}", "event_type": "value_inserted",
                      "event_payload": {"value": value}, "normalized_field_type": field_name,
                      "page_field_key": f"generic:{field_name}"})
            assert response.status_code == 201
        confirmed = client.post(f"/api/handoff/sessions/{session_id}/confirm-submission", headers=headers,
                                json={"mark_workflow_applied": False})
        assert confirmed.status_code == 201
        assert confirmed.json()["session"]["status"] == "user_confirmed_submitted"

        conn = connect(settings.db_path)
        assert conn.execute("SELECT COUNT(*) FROM submission_confirmations").fetchone()[0] == 1
        # the human flow never used autonomy SUBMIT authority
        for table in ("autonomy_grants", "submission_attempts"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
        conn.close()
