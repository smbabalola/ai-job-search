"""Bundle 7 spec §11.4: every gated entry point calls the gate with the
expected (feature, allowance). The work behind each entry is faked; the gate
and the ledger are real, so the calls are recorded as they happen."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from webapp.api import application_documents as documents_api
from webapp.api import discovery as discovery_api
from webapp.api import fill_extension
from webapp.api import workspaces as workspaces_api
from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.db import connect
from webapp.services import fill_runs
from webapp.services import human_submit
from webapp.services.entitlements import EntitlementGate
from webapp.services.handoff import SessionScope
from webapp.services.usage import UsageService
from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
from tests.webapp.factories import build_account_graph


@pytest.fixture
def world(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []
    real_feature, real_reserve, real_gauge = (EntitlementGate.require_feature, UsageService.reserve,
                                              UsageService.gauge_check)

    def require_feature(self, conn, scope, feature, *, now):
        calls.append(("feature", feature))
        return real_feature(self, conn, scope, feature, now=now)

    def reserve(self, conn, scope, *, allowance, **kwargs):
        calls.append(("reserve", allowance))
        return real_reserve(self, conn, scope, allowance=allowance, **kwargs)

    def gauge_check(self, conn, scope, *, allowance, **kwargs):
        calls.append(("gauge", allowance))
        return real_gauge(self, conn, scope, allowance=allowance, **kwargs)

    monkeypatch.setattr(EntitlementGate, "require_feature", require_feature)
    monkeypatch.setattr(UsageService, "reserve", reserve)
    monkeypatch.setattr(UsageService, "gauge_check", gauge_check)
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        extensions_dir=Path(__file__).parents[1] / "fixtures" / "extensions",
                        auth_required_in_local=True)
    app = create_app(settings)
    with TestClient(app) as client:
        publish_legal_documents(settings)
        sign_up_and_verify(client)
        sign_in(client)
        account_id = client.get("/auth/me").json()["account_id"]
        conn = connect(settings)
        graph = build_account_graph(conn, account_id=account_id, documents_root=settings.documents_root)
        conn.close()
        calls.clear()
        yield app, client, settings, graph["ids"], account_id, calls


def _post(client, url, **kwargs):
    return client.post(url, headers={"X-CSRF-Token": csrf_token(client)}, **kwargs)


@pytest.mark.parametrize("stage,service", [("understand", "understand_job"), ("fit", "fit_job"),
                                           ("application-intelligence", "generate_application_intelligence")])
def test_every_prepare_stage_requires_ai_prepare_and_reserves_a_prepare(world, monkeypatch, stage, service):
    app, client, _, ids, _, calls = world
    monkeypatch.setattr(workspaces_api, service, lambda *a, **k: {"id": "artifact"})
    monkeypatch.setattr(workspaces_api, "record_shadow_decision", lambda *a, **k: None)
    response = _post(client, f"/api/workspaces/{ids['workspace_id']}/{stage}", json={"request_id": "r1"})
    assert response.status_code == 200, response.text
    assert ("feature", "ai.prepare") in calls and ("reserve", "applications.prepare") in calls


def test_on_demand_discovery_requires_the_feature_and_reserves_a_run(world, monkeypatch):
    _, client, _, ids, _, calls = world
    monkeypatch.setattr(discovery_api, "run_discovery_search", lambda *a, **k: {"run": "ok"})
    response = _post(client, f"/api/search-workspaces/{ids['search_workspace_id']}/discovery/search",
                     json={"sources": [], "queries": ["engineer"], "locations": []})
    assert response.status_code == 200, response.text
    assert calls == [("feature", "discovery.on_demand"), ("reserve", "discovery.on_demand_runs")]


def test_document_upload_checks_the_storage_gauge(world, monkeypatch):
    _, client, _, ids, _, calls = world
    monkeypatch.setattr(documents_api, "upload_application_document", lambda *a, **k: {"id": "doc"})
    response = client.post(f"/api/workspaces/{ids['workspace_id']}/application-documents/upload/cv",
                           headers={"X-CSRF-Token": csrf_token(client)},
                           files={"file": ("cv.docx", b"PK-bytes", "application/octet-stream")})
    assert response.status_code == 201, response.text
    assert calls == [("gauge", "storage.bytes")]


def test_human_submit_authorization_requires_the_feature_only(world, monkeypatch):
    _, client, _, ids, _, calls = world
    monkeypatch.setattr(human_submit, "authorize", lambda *a, **k: {"authorized": True})
    response = _post(client, f"/api/workspaces/{ids['workspace_id']}/submit/authorize", json={"review_hash": "h"})
    assert response.status_code == 201, response.text
    assert calls == [("feature", "apply.human_submit")]


def test_fill_start_requires_the_feature_only(world, monkeypatch):
    app, _, settings, ids, account_id, calls = world
    monkeypatch.setattr(fill_runs, "start_run", lambda *a, **k: {"id": "run"})
    scope = SessionScope(account_id=account_id, handoff_session_id=ids["session_id"],
                         workspace_id=ids["workspace_id"], pack_artifact_id=ids["pack_artifact_id"])
    body = fill_extension.StartBody(executor_instance_id="e", browser_session_id="b", execution_tab_id=1)
    conn = connect(settings)
    try:
        result = fill_extension.post_run(ids["session_id"], body, SimpleNamespace(app=app), conn, scope)
    finally:
        conn.close()
    assert result == {"id": "run"}
    assert calls == [("feature", "apply.assisted_fill")]
