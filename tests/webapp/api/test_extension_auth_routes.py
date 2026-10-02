"""Bundle 7 spec §9.2: extension pairing, token refresh, bearer scope and revocation over HTTP."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.webapp.auth_helpers import PASSWORD, post, publish_legal_documents, sign_in, sign_up_and_verify
from webapp.app import create_app
from webapp.config import Settings


@pytest.fixture
def world(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        extensions_dir=Path(__file__).parents[2] / "fixtures" / "extensions",
                        auth_required_in_local=True)
    app = create_app(settings)
    with TestClient(app) as web:
        publish_legal_documents(settings)
        sign_up_and_verify(web)
        sign_in(web)
        yield app, web


def _pair(app, web):
    code = post(web, "/api/ext/pairing-codes").json()["code"]
    extension = TestClient(app)
    paired = extension.post("/api/ext/pair", json={"code": code, "device_label": "Chrome"})
    assert paired.status_code == 201, paired.text
    return extension, paired.json()


def test_pair_then_call_with_the_bearer_token(world):
    app, web = world
    extension, tokens = _pair(app, web)
    whoami = extension.get("/api/ext/whoami", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert whoami.status_code == 200
    assert whoami.json()["account_id"] == web.get("/auth/me").json()["account_id"]
    assert tokens["account_label"] == "a***@example.com"


def test_pairing_codes_need_a_signed_in_user(world):
    app, _ = world
    assert TestClient(app).post("/api/ext/pairing-codes").status_code in (401, 403)


def test_a_bad_or_reused_code_is_refused(world):
    app, web = world
    code = post(web, "/api/ext/pairing-codes").json()["code"]
    extension = TestClient(app)
    assert extension.post("/api/ext/pair", json={"code": code, "device_label": "a"}).status_code == 201
    again = extension.post("/api/ext/pair", json={"code": code, "device_label": "b"})
    assert again.status_code == 401 and again.json()["error"] == "PAIRING_CODE_INVALID"


def test_refresh_rotates_and_reuse_is_device_revoked(world):
    app, web = world
    extension, tokens = _pair(app, web)
    body = {"device_id": tokens["device_id"], "refresh_token": tokens["refresh_token"]}
    rotated = extension.post("/api/ext/token", json=body)
    assert rotated.status_code == 200 and rotated.json()["refresh_token"] != tokens["refresh_token"]
    reused = extension.post("/api/ext/token", json=body)
    assert reused.status_code == 401 and reused.json()["error"] == "DEVICE_REVOKED"
    assert extension.get("/api/ext/whoami", headers={
        "Authorization": f"Bearer {rotated.json()['access_token']}"}).json()["error"] == "DEVICE_REVOKED"


def test_the_legacy_credential_header_no_longer_works(world):
    app, _ = world
    response = TestClient(app).post("/api/handoff/sessions", json={}, headers={"X-Handoff-Credential": "legacy"})
    assert response.status_code == 401


def test_self_revoke_and_settings_revoke(world):
    app, web = world
    extension, tokens = _pair(app, web)
    auth = {"Authorization": f"Bearer {tokens['access_token']}"}
    devices = web.get("/api/settings/devices").json()["devices"]
    assert [d["label"] for d in devices] == ["Chrome"]
    assert extension.post("/api/ext/devices/self/revoke", headers=auth).status_code == 204
    assert extension.get("/api/ext/whoami", headers=auth).status_code == 401
    second, tokens2 = _pair(app, web)
    device_id = tokens2["device_id"]
    assert post(web, f"/api/settings/devices/{device_id}/revoke").status_code == 204
    assert second.get("/api/ext/whoami", headers={"Authorization": f"Bearer {tokens2['access_token']}"}).status_code == 401


def test_password_change_revokes_devices(world):
    app, web = world
    extension, tokens = _pair(app, web)
    changed = post(web, "/settings/password", data={"current_password": PASSWORD,
                                                    "new_password": "another long passphrase 7"})
    assert changed.status_code == 200
    whoami = extension.get("/api/ext/whoami", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert whoami.status_code == 401


def test_handoff_tickets_are_issued_only_for_the_users_own_workspaces(world):
    from webapp.persistence.db import connect
    from webapp.persistence.workspaces import create_workspace

    app, web = world
    account_id = web.get("/auth/me").json()["account_id"]
    conn = connect(app.state.settings)
    workspace = create_workspace(conn, company="Acme", title="Engineer", account_id=account_id)
    conn.close()
    response = post(web, "/api/ext/handoff-tickets", json={"workspace_id": workspace["id"], "purpose": "HANDOFF"})
    assert response.status_code == 201 and response.json()["ticket"].startswith("v1.")
    foreign = post(web, "/api/ext/handoff-tickets", json={"workspace_id": "ws_not_mine", "purpose": "HANDOFF"})
    assert foreign.status_code == 404
