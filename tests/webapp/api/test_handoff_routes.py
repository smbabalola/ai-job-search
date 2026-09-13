from __future__ import annotations

from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.artifacts import save_artifact
from webapp.persistence.db import connect
from webapp.persistence.workspaces import create_workspace, ensure_profile_workspace


def _app(tmp_path):
    settings = Settings(
        db_path=tmp_path / "jobsearch.sqlite3", documents_root=tmp_path / "documents",
    )
    app = create_app(settings)
    with TestClient(app):
        conn = connect(settings.db_path)
        ensure_profile_workspace(conn)
        workspace = create_workspace(conn, company="Acme", title="Engineer")
        artifact = save_artifact(
            conn, workspace_id=workspace["id"], artifact_type="application_pack",
            payload={"schema_version": "application-pack.v1"},
        )
        conn.close()
    return app, workspace["id"], artifact["id"]


def _paired_credential(client) -> str:
    generated = client.post("/api/handoff/pairing/generate")
    assert generated.status_code == 201, generated.text
    one_time_secret = generated.json()["one_time_secret"]

    exchanged = client.post(
        "/api/handoff/pairing/exchange", json={"one_time_secret": one_time_secret}
    )
    assert exchanged.status_code == 201, exchanged.text
    return exchanged.json()["durable_secret"]


def test_pairing_generate_and_exchange_round_trip(tmp_path):
    app, _workspace_id, _artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        assert isinstance(credential, str)
        assert len(credential) >= 32


def test_start_session_requires_valid_extension_credential(tmp_path):
    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/handoff/sessions",
            headers={"X-Handoff-Credential": "not-a-real-credential"},
            json={
                "workspace_id": workspace_id, "pack_artifact_id": artifact_id,
                "target_url": "https://x.test/apply", "target_domain": "x.test",
                "ats_adapter_id": "generic", "ats_adapter_version": "generic@1",
            },
        )
        assert response.status_code == 401


def test_full_session_lifecycle_over_http(tmp_path):
    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)

        started = client.post(
            "/api/handoff/sessions", headers={"X-Handoff-Credential": credential},
            json={
                "workspace_id": workspace_id, "pack_artifact_id": artifact_id,
                "target_url": "https://x.test/apply", "target_domain": "x.test",
                "ats_adapter_id": "generic", "ats_adapter_version": "generic@1",
            },
        )
        assert started.status_code == 201, started.text
        session_id = started.json()["id"]
        assert "session_token" in started.json()
        headers = {"X-Handoff-Session-Token": started.json()["session_token"]}

        event_response = client.post(
            f"/api/handoff/sessions/{session_id}/events", headers=headers,
            json={
                "event_id": "evt_1", "event_type": "value_inserted",
                "event_payload": {"value": "shola@example.com"},
                "normalized_field_type": "email", "page_field_key": "generic:email",
            },
        )
        assert event_response.status_code == 201, event_response.text

        # retry with the same event_id must not create a duplicate
        retry_response = client.post(
            f"/api/handoff/sessions/{session_id}/events", headers=headers,
            json={
                "event_id": "evt_1", "event_type": "value_inserted",
                "event_payload": {"value": "different-value-should-be-ignored"},
                "normalized_field_type": "email", "page_field_key": "generic:email",
            },
        )
        assert retry_response.status_code == 201

        replay = client.get(
            f"/api/handoff/sessions/{session_id}/events", headers=headers
        )
        assert replay.status_code == 200
        events = replay.json()["events"]
        assert len(events) == 1
        assert events[0]["event_id"] == "evt_1"

        confirmed = client.post(
            f"/api/handoff/sessions/{session_id}/confirm-submission",
            headers=headers, json={"mark_workflow_applied": False},
        )
        assert confirmed.status_code == 201, confirmed.text
        assert confirmed.json()["session"]["status"] == "user_confirmed_submitted"


def test_sensitive_event_with_value_is_rejected_over_http(tmp_path):
    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        started = client.post(
            "/api/handoff/sessions", headers={"X-Handoff-Credential": credential},
            json={
                "workspace_id": workspace_id, "pack_artifact_id": artifact_id,
                "target_url": "https://x.test/apply", "target_domain": "x.test",
                "ats_adapter_id": "generic", "ats_adapter_version": "generic@1",
            },
        )
        session_id = started.json()["id"]
        headers = {"X-Handoff-Session-Token": started.json()["session_token"]}

        rejected = client.post(
            f"/api/handoff/sessions/{session_id}/events", headers=headers,
            json={
                "event_id": "evt_sensitive", "event_type": "user_value_present_observed",
                "event_payload": {"value": "should not be sent"},
                "normalized_field_type": "salary_expectation",
                "page_field_key": "generic:salary",
            },
        )
        assert rejected.status_code == 400


def _start_session(client, credential, workspace_id, artifact_id, **overrides):
    body = {
        "workspace_id": workspace_id, "pack_artifact_id": artifact_id,
        "target_url": "https://x.test/apply", "target_domain": "x.test",
        "ats_adapter_id": "generic", "ats_adapter_version": "generic@1",
    }
    body.update(overrides)
    response = client.post(
        "/api/handoff/sessions", headers={"X-Handoff-Credential": credential}, json=body,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_start_session_returns_session_token(tmp_path):
    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        started = _start_session(client, credential, workspace_id, artifact_id)
        assert "session_token" in started
        assert isinstance(started["session_token"], str)
        assert len(started["session_token"]) >= 32


def test_discover_sessions_returns_metadata_without_session_token(tmp_path):
    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        _start_session(client, credential, workspace_id, artifact_id)

        discovered = client.get(
            "/api/handoff/sessions/discover",
            headers={"X-Handoff-Credential": credential},
            params={"workspace_id": workspace_id, "target_domain": "x.test"},
        )
        assert discovered.status_code == 200, discovered.text
        sessions = discovered.json()["sessions"]
        assert len(sessions) == 1
        assert "session_token" not in sessions[0]


def test_discover_does_not_refresh_last_activity_at(tmp_path):
    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        started = _start_session(client, credential, workspace_id, artifact_id)
        original_activity = started["last_activity_at"]

        client.get(
            "/api/handoff/sessions/discover",
            headers={"X-Handoff-Credential": credential},
            params={"workspace_id": workspace_id, "target_domain": "x.test"},
        )

        discovered = client.get(
            "/api/handoff/sessions/discover",
            headers={"X-Handoff-Credential": credential},
            params={"workspace_id": workspace_id, "target_domain": "x.test"},
        )
        assert discovered.json()["sessions"][0]["last_activity_at"] == original_activity


def test_resume_session_rotates_token_and_invalidates_prior_one(tmp_path):
    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        started = _start_session(client, credential, workspace_id, artifact_id)
        session_id = started["id"]
        original_token = started["session_token"]

        resumed = client.post(
            f"/api/handoff/sessions/{session_id}/resume",
            headers={"X-Handoff-Credential": credential},
        )
        assert resumed.status_code == 201, resumed.text
        new_token = resumed.json()["session_token"]
        assert new_token != original_token

        # the original token no longer works
        stale_response = client.get(
            f"/api/handoff/sessions/{session_id}/events",
            headers={"X-Handoff-Session-Token": original_token},
        )
        assert stale_response.status_code == 401

        # the rotated token works
        fresh_response = client.get(
            f"/api/handoff/sessions/{session_id}/events",
            headers={"X-Handoff-Session-Token": new_token},
        )
        assert fresh_response.status_code == 200


def test_resume_session_rejects_wrong_account(tmp_path):
    from webapp.persistence.accounts import create_account

    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        conn = connect(app.state.settings.db_path)
        create_account(conn, account_id="account_other", display_name="Other")
        conn.close()

        credential = _paired_credential(client)
        started = _start_session(client, credential, workspace_id, artifact_id)
        session_id = started["id"]

        other_settings = Settings(
            db_path=app.state.settings.db_path,
            documents_root=app.state.settings.documents_root,
            account_id="account_other",
        )
    with TestClient(create_app(other_settings)) as other_client:
        other_credential = _paired_credential(other_client)
        resumed = other_client.post(
            f"/api/handoff/sessions/{session_id}/resume",
            headers={"X-Handoff-Credential": other_credential},
        )
        assert resumed.status_code == 404


def test_resume_session_rejects_expired_session(tmp_path):
    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        started = _start_session(client, credential, workspace_id, artifact_id)
        session_id = started["id"]

        conn = connect(app.state.settings.db_path)
        stale = "2020-01-01T00:00:00+00:00"
        conn.execute(
            "UPDATE handoff_sessions SET last_activity_at = ? WHERE id = ?",
            (stale, session_id),
        )
        conn.commit()
        conn.close()

        resumed = client.post(
            f"/api/handoff/sessions/{session_id}/resume",
            headers={"X-Handoff-Credential": credential},
        )
        assert resumed.status_code == 401


def test_resume_session_rejects_terminal_status_session(tmp_path):
    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        started = _start_session(client, credential, workspace_id, artifact_id)
        session_id = started["id"]
        headers = {"X-Handoff-Session-Token": started["session_token"]}

        confirmed = client.post(
            f"/api/handoff/sessions/{session_id}/confirm-submission",
            headers=headers, json={"mark_workflow_applied": False},
        )
        assert confirmed.status_code == 201, confirmed.text

        resumed = client.post(
            f"/api/handoff/sessions/{session_id}/resume",
            headers={"X-Handoff-Credential": credential},
        )
        assert resumed.status_code == 404


def test_send_event_rejects_durable_credential_header(tmp_path):
    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        started = _start_session(client, credential, workspace_id, artifact_id)
        session_id = started["id"]

        response = client.post(
            f"/api/handoff/sessions/{session_id}/events",
            headers={"X-Handoff-Credential": credential},
            json={
                "event_id": "evt_1", "event_type": "value_inserted",
                "event_payload": {"value": "x"},
                "normalized_field_type": "email", "page_field_key": "generic:email",
            },
        )
        assert response.status_code == 422  # missing required X-Handoff-Session-Token header


def test_session_token_for_one_session_rejected_against_another_sessions_path(tmp_path):
    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        session_a = _start_session(client, credential, workspace_id, artifact_id)
        session_b = _start_session(
            client, credential, workspace_id, artifact_id, target_domain="y.test",
        )

        cross_response = client.get(
            f"/api/handoff/sessions/{session_b['id']}/events",
            headers={"X-Handoff-Session-Token": session_a["session_token"]},
        )
        assert cross_response.status_code == 404


def test_send_event_refreshes_last_activity_at(tmp_path):
    from datetime import datetime, timedelta, timezone

    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        started = _start_session(client, credential, workspace_id, artifact_id)
        session_id = started["id"]
        headers = {"X-Handoff-Session-Token": started["session_token"]}

        # Stale but still within the 2-hour inactivity window, so the
        # refresh itself (not the expiry rejection) is what's observed.
        stale_but_not_expired = (
            datetime.now(timezone.utc) - timedelta(minutes=30)
        ).isoformat()
        conn = connect(app.state.settings.db_path)
        conn.execute(
            "UPDATE handoff_sessions SET last_activity_at = ? WHERE id = ?",
            (stale_but_not_expired, session_id),
        )
        conn.commit()
        conn.close()

        response = client.post(
            f"/api/handoff/sessions/{session_id}/events", headers=headers,
            json={
                "event_id": "evt_1", "event_type": "value_inserted",
                "event_payload": {"value": "x"},
                "normalized_field_type": "email", "page_field_key": "generic:email",
            },
        )
        assert response.status_code == 201, response.text

        conn = connect(app.state.settings.db_path)
        refreshed = conn.execute(
            "SELECT last_activity_at FROM handoff_sessions WHERE id = ?", (session_id,),
        ).fetchone()["last_activity_at"]
        conn.close()
        assert refreshed != stale_but_not_expired


def test_expired_session_token_rejected(tmp_path):
    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        started = _start_session(client, credential, workspace_id, artifact_id)
        session_id = started["id"]
        headers = {"X-Handoff-Session-Token": started["session_token"]}

        conn = connect(app.state.settings.db_path)
        stale = "2020-01-01T00:00:00+00:00"
        conn.execute(
            "UPDATE handoff_sessions SET last_activity_at = ? WHERE id = ?",
            (stale, session_id),
        )
        conn.commit()
        conn.close()

        response = client.get(
            f"/api/handoff/sessions/{session_id}/events", headers=headers,
        )
        assert response.status_code == 401


def test_cross_account_denial_over_http(tmp_path):
    from webapp.persistence.accounts import create_account

    app, workspace_id, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        conn = connect(app.state.settings.db_path)
        create_account(conn, account_id="account_other", display_name="Other")
        conn.close()

        # generate/exchange as account_other via a second app instance
        # pointed at the same account_id but a different db connection path
        other_settings = Settings(
            db_path=app.state.settings.db_path,
            documents_root=app.state.settings.documents_root,
            account_id="account_other",
        )
    with TestClient(create_app(other_settings)) as other_client:
        other_credential = _paired_credential(other_client)

    with TestClient(app) as client:
        response = client.post(
            "/api/handoff/sessions",
            headers={"X-Handoff-Credential": other_credential},
            json={
                "workspace_id": workspace_id, "pack_artifact_id": artifact_id,
                "target_url": "https://x.test/apply", "target_domain": "x.test",
                "ats_adapter_id": "generic", "ats_adapter_version": "generic@1",
            },
        )
        assert response.status_code == 404


_SAMPLE_CANDIDATE_SNAPSHOT = {
    "profile_schema_version": "profile.v1",
    "identity": {"name": {"value": "Ada Lovelace", "profile_evidence_ids": ["claim_name"]}},
    "contact": {
        "email": {"value": "ada@example.com", "profile_evidence_ids": ["claim_email"]},
        "phone": None, "linkedin": None, "github": None, "location": None,
    },
    "employment": [], "education": [], "certifications": [], "skills": [],
    "languages": [], "projects": [], "publications": [], "awards": [],
}


def _app_with_candidate_pack(tmp_path):
    settings = Settings(
        db_path=tmp_path / "jobsearch.sqlite3", documents_root=tmp_path / "documents",
    )
    app = create_app(settings)
    with TestClient(app):
        conn = connect(settings.db_path)
        ensure_profile_workspace(conn)
        workspace = create_workspace(conn, company="Acme", title="Engineer")
        artifact = save_artifact(
            conn, workspace_id=workspace["id"], artifact_type="application_pack",
            payload={
                "schema_version": "application-pack.v1",
                "candidate_snapshot": _SAMPLE_CANDIDATE_SNAPSHOT,
            },
        )
        conn.close()
    return app, workspace["id"], artifact["id"]


def test_post_snapshot_returns_projected_fields(tmp_path):
    app, workspace_id, artifact_id = _app_with_candidate_pack(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        started = _start_session(client, credential, workspace_id, artifact_id)
        session_id = started["id"]
        headers = {"X-Handoff-Session-Token": started["session_token"]}

        response = client.post(
            f"/api/handoff/sessions/{session_id}/snapshot", headers=headers,
            json={"normalized_field_types": ["name", "email"]},
        )
        assert response.status_code == 200, response.text
        snapshot = response.json()["snapshot"]
        assert set(snapshot.keys()) == {"name", "email"}
        assert snapshot["name"]["value"] == "Ada Lovelace"


def test_post_snapshot_requires_session_token(tmp_path):
    app, workspace_id, artifact_id = _app_with_candidate_pack(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        started = _start_session(client, credential, workspace_id, artifact_id)
        session_id = started["id"]

        # durable credential, not a session token — must be rejected
        response = client.post(
            f"/api/handoff/sessions/{session_id}/snapshot",
            headers={"X-Handoff-Credential": credential},
            json={"normalized_field_types": ["name"]},
        )
        assert response.status_code == 422


def test_post_snapshot_rejects_mismatched_session_path(tmp_path):
    app, workspace_id, artifact_id = _app_with_candidate_pack(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        session_a = _start_session(client, credential, workspace_id, artifact_id)
        session_b = _start_session(
            client, credential, workspace_id, artifact_id, target_domain="y.test",
        )

        response = client.post(
            f"/api/handoff/sessions/{session_b['id']}/snapshot",
            headers={"X-Handoff-Session-Token": session_a["session_token"]},
            json={"normalized_field_types": ["name"]},
        )
        assert response.status_code == 404


def test_post_snapshot_rejects_expired_session_token(tmp_path):
    app, workspace_id, artifact_id = _app_with_candidate_pack(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        started = _start_session(client, credential, workspace_id, artifact_id)
        session_id = started["id"]
        headers = {"X-Handoff-Session-Token": started["session_token"]}

        conn = connect(app.state.settings.db_path)
        stale = "2020-01-01T00:00:00+00:00"
        conn.execute(
            "UPDATE handoff_sessions SET last_activity_at = ? WHERE id = ?",
            (stale, session_id),
        )
        conn.commit()
        conn.close()

        response = client.post(
            f"/api/handoff/sessions/{session_id}/snapshot", headers=headers,
            json={"normalized_field_types": ["name"]},
        )
        assert response.status_code == 401


def test_post_snapshot_cannot_accept_arbitrary_json_path_instead_of_field_type(tmp_path):
    app, workspace_id, artifact_id = _app_with_candidate_pack(tmp_path)
    with TestClient(app) as client:
        credential = _paired_credential(client)
        started = _start_session(client, credential, workspace_id, artifact_id)
        session_id = started["id"]
        headers = {"X-Handoff-Session-Token": started["session_token"]}

        # The request schema has no field for a raw path/key — attempting
        # to smuggle one in as an extra body field must be rejected
        # outright (StrictBody's extra="forbid"), not silently ignored.
        response = client.post(
            f"/api/handoff/sessions/{session_id}/snapshot", headers=headers,
            json={
                "normalized_field_types": ["name"],
                "candidate_path": "identity.name.value",
            },
        )
        assert response.status_code == 422
