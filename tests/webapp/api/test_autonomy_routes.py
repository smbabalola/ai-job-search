from __future__ import annotations

from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.db import connect
from webapp.persistence.workspaces import create_workspace


def client_for(tmp_path, **settings_kwargs):
    settings = Settings(db_path=tmp_path / "db.sqlite3", **settings_kwargs)
    client = TestClient(create_app(settings))
    client.__enter__()
    return client, settings


def test_settings_start_at_none_and_enable_is_explicit(tmp_path):
    client, _ = client_for(tmp_path)
    body = client.get("/api/autonomy").json()
    assert (body["account_max"], body["default_workspace_ceiling"], body["policy"]) == ("NONE", "NONE", None)
    assert client.post("/api/autonomy/enable-preparation", json={"timezone": "Europe/London"}).status_code == 200
    body = client.get("/api/autonomy").json()
    assert (body["account_max"], body["default_workspace_ceiling"]) == ("PREPARE", "PREPARE")
    assert body["policy"]["timezone"] == "Europe/London"


def test_policy_validation_errors_are_422_not_500(tmp_path):
    client, _ = client_for(tmp_path)
    client.post("/api/autonomy/enable-preparation", json={"timezone": "Europe/London"})
    doc = client.get("/api/autonomy").json()["policy"]
    doc["rules"] = [{"id": "fit", "description": "", "when": {"attr": "fit.overall_score", "op": "lt", "value": 75.5},
                     "effect": {"type": "REDUCE_TO", "level": "PREPARE"}, "on_unknown": {"type": "REDUCE_TO", "level": "PREPARE"}}]
    response = client.put("/api/autonomy/policy", json={"doc": doc})
    assert response.status_code == 422
    assert any("integer" in e for e in response.json()["errors"])


def test_kill_switch_sentinel_and_resume_all(tmp_path):
    client, settings = client_for(tmp_path)
    assert client.post("/api/autonomy/kill-switch/engage", json={"reason": "stop"}).status_code == 200
    assert client.get("/api/autonomy").json()["kill_switch"]["halted"] is True
    client.post("/api/autonomy/kill-switch/release", json={"reason": "ok"})
    assert client.get("/api/autonomy").json()["kill_switch"]["halted"] is True
    settings.autonomy_sentinel_path.write_text("halt")
    assert client.post("/api/autonomy/resume-all", json={"reason": "go"}).status_code == 409
    settings.autonomy_sentinel_path.unlink()
    assert client.post("/api/autonomy/resume-all", json={"reason": "go"}).status_code == 200
    assert client.get("/api/autonomy").json()["kill_switch"]["halted"] is False


def test_capability_rejects_foreign_workspace_and_unknown_values(tmp_path):
    client, _ = client_for(tmp_path)
    r = client.post("/api/autonomy/capability", json={"scope_type": "WORKSPACE_CEILING", "scope_id": "sw_not_mine",
                                                      "capability": "SUBMIT"})
    assert r.status_code == 404
    r = client.post("/api/autonomy/capability", json={"scope_type": "ACCOUNT_MAX", "scope_id": "x", "capability": "GODMODE"})
    assert r.status_code == 422


def test_dossier_and_pages(tmp_path):
    client, settings = client_for(tmp_path)
    conn = connect(settings.db_path)
    ws = create_workspace(conn, company="Acme", title="Eng")["id"]
    conn.close()
    assert client.get(f"/api/workspaces/{ws}/autonomy/dossier").json()["schema_version"] == "autonomy-dossier.v1"
    assert client.get("/api/workspaces/ws_missing/autonomy/dossier").status_code == 404
    assert client.get("/autonomy").status_code == 200
    assert client.get(f"/workspaces/{ws}/autonomy").status_code == 200


def test_apply_target_confirmation_requires_the_resolved_target(tmp_path):
    client, settings = client_for(tmp_path)
    conn = connect(settings.db_path)
    ws = create_workspace(conn, company="Acme", title="Eng")["id"]
    conn.close()
    r = client.post(f"/api/workspaces/{ws}/autonomy/apply-target/confirm", json={"url": "https://evil.example/jobs/1"})
    assert r.status_code == 409


# ---- hardening beyond the plan's brief ---------------------------------------

def _job_ws(settings, record=None):
    from webapp.persistence.application_identity import save_application_identity
    conn = connect(settings.db_path)
    try:
        ws = create_workspace(conn, company="Acme", title="Eng")["id"]
        if record:
            save_application_identity(conn, application_workspace_id=ws, source_record=record)
            conn.commit()
        return ws
    finally:
        conn.close()


def test_pause_requires_an_owned_application_or_search_workspace(tmp_path):
    client, settings = client_for(tmp_path)
    ws = _job_ws(settings)
    ok = client.post("/api/autonomy/pause", json={"scope_type": "APPLICATION", "scope_id": ws, "reason": "r"})
    assert ok.status_code == 200
    assert client.post("/api/autonomy/resume", json={"scope_type": "APPLICATION", "scope_id": ws,
                                                     "reason": "r"}).status_code == 200
    foreign = client.post("/api/autonomy/pause", json={"scope_type": "APPLICATION", "scope_id": "ws_nope", "reason": "r"})
    assert foreign.status_code == 404
    account = client.post("/api/autonomy/pause", json={"scope_type": "ACCOUNT", "scope_id": "account_local",
                                                       "reason": "r"})
    assert account.status_code == 422


def test_apply_target_confirmation_needs_a_durable_identity(tmp_path, monkeypatch):
    from webapp.api import autonomy as autonomy_api
    from webapp.services.workspace_view import ApplyTarget
    url = "https://boards.greenhouse.io/acme/jobs/5"
    monkeypatch.setattr(autonomy_api, "resolve_apply_target",
                        lambda c, workspace_id, account_id: ApplyTarget(url=url, provenance="user_supplied"))
    client, settings = client_for(tmp_path)
    weak = _job_ws(settings)
    assert client.post(f"/api/workspaces/{weak}/autonomy/apply-target/confirm", json={"url": url}).status_code == 409
    strong = _job_ws(settings, {"source": "greenhouse", "source_record_id": "5", "source_url": url,
                                "company": "Acme", "title": "Eng", "location": "UK"})
    assert client.post(f"/api/workspaces/{strong}/autonomy/apply-target/confirm", json={"url": url}).status_code == 200


def test_rule_acknowledgement_binds_server_computed_hashes(tmp_path):
    client, settings = client_for(tmp_path)
    client.post("/api/autonomy/enable-preparation", json={"timezone": "Europe/London"})
    doc = client.get("/api/autonomy").json()["policy"]
    doc["rules"] = [{"id": "low_fit", "description": "", "when": {"attr": "fit.overall_score", "op": "lt", "value": 75},
                     "effect": {"type": "REQUIRE_USER"}, "on_unknown": {"type": "REQUIRE_USER"}}]
    assert client.put("/api/autonomy/policy", json={"doc": doc}).status_code == 200
    ws = _job_ws(settings)
    ok = client.post(f"/api/workspaces/{ws}/autonomy/rule-acknowledgements",
                     json={"rule_id": "low_fit", "disposition": "PROCEED"})
    assert ok.status_code == 200, ok.text
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT rule_hash, observed_fingerprint FROM rule_acknowledgements").fetchone()
    finally:
        conn.close()
    assert row["rule_hash"].startswith("sha256:") and row["observed_fingerprint"].startswith("sha256:")
    assert client.post(f"/api/workspaces/{ws}/autonomy/rule-acknowledgements",
                       json={"rule_id": "nope", "disposition": "PROCEED"}).status_code == 404
