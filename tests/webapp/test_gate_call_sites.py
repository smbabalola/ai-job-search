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
    monkeypatch.setattr(discovery_api, "run_discovery_search", lambda *a, **k: {"run": {"id": "run_fake"}, "candidate_ids": []})
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


def _running(settings, account_id, action_key):
    """An execution of ``action_key`` already in flight (another tab, a double click)."""
    conn = connect(settings)
    conn.execute("INSERT INTO metered_actions (id, account_id, action_key, status, started_at, lease_expires_at) "
                 "VALUES ('mact_inflight', ?, ?, 'RUNNING', '2000-01-01T00:00:00.000000', '9999-01-01T00:00:00.000000')",
                 (account_id, action_key))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("stage,service", [("understand", "understand_job"), ("fit", "fit_job"),
                                           ("application-intelligence", "generate_application_intelligence")])
def test_a_prepare_stage_already_in_flight_is_refused_without_running(world, monkeypatch, stage, service):
    _, client, settings, ids, account_id, _ = world
    executions = []
    monkeypatch.setattr(workspaces_api, service, lambda *a, **k: executions.append(1) or {"id": "artifact"})
    monkeypatch.setattr(workspaces_api, "record_shadow_decision", lambda *a, **k: None)
    _running(settings, account_id, f"prepare:{stage}:{ids['workspace_id']}")
    response = _post(client, f"/api/workspaces/{ids['workspace_id']}/{stage}", json={"request_id": "r2"})
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "ACTION_IN_PROGRESS"
    assert response.headers["Retry-After"] == "5"
    assert executions == []


def test_a_discovery_run_already_in_flight_is_refused_without_running(world, monkeypatch):
    _, client, settings, ids, account_id, _ = world
    executions = []
    monkeypatch.setattr(discovery_api, "run_discovery_search", lambda *a, **k: executions.append(1) or {"run": {"id": "run_fake"}, "candidate_ids": []})
    _running(settings, account_id, f"discovery:{ids['search_workspace_id']}")
    response = _post(client, f"/api/search-workspaces/{ids['search_workspace_id']}/discovery/search",
                     json={"sources": [], "queries": ["engineer"], "locations": []})
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "ACTION_IN_PROGRESS"
    assert executions == []


class _CountingUnderstanding:
    provider_id, model_id, model_version = "openai", "gpt-5.4-mini", "test"

    def __init__(self):
        self.calls = 0

    def extract(self, request):
        self.calls += 1
        raise AssertionError("the provider must not be called past the ceiling")


def test_the_ai_cost_ceiling_refuses_a_prepare_stage_with_the_fair_use_code(world):
    """Task 17: the route's providers sit behind the metered boundary, and the
    refusal reaches the caller as FAIR_USE_LIMIT_REACHED, not a pipeline 400."""
    app, client, settings, ids, account_id, _ = world
    provider = _CountingUnderstanding()
    app.state.job_understanding_provider = provider
    conn = connect(settings)
    conn.execute("INSERT INTO ai_cost_events (id, account_id, subject_type, subject_id, provider, model, input_tokens, "
                 "output_tokens, cost_micro_usd, request_ref, created_at) VALUES ('aic_1', ?, 'workspace', 'w', "
                 "'openai', 'm', 0, 0, 2000000000, NULL, ?)",
                 (account_id, __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                  .isoformat(timespec="microseconds")))
    conn.commit()
    conn.close()
    from tests.webapp.api.test_workspace_routes import _source_record
    created = _post(client, "/api/workspaces", json={"company": "Acme", "title": "Backend Engineer",
                                                      "source_record": _source_record(),
                                                      "source_record_origin": "manual_entry"})
    assert created.status_code == 201, created.text
    workspace_id = created.json()["workspace"]["id"]
    response = _post(client, f"/api/workspaces/{workspace_id}/understand", json={"request_id": "r9"})
    assert response.status_code == 429, response.text
    assert response.json()["error"] == "FAIR_USE_LIMIT_REACHED"
    assert "2000000000" not in response.text and "micro" not in response.text  # the ceiling is never shown
    assert provider.calls == 0
    conn = connect(settings)
    statuses = [r[0] for r in conn.execute("SELECT status FROM usage_reservations")]
    conn.close()
    assert statuses == ["RELEASED"]  # a refused stage is not charged


# ---- discovery candidate evaluation inherits the discovery run's authorization ----

class _DiscoveryRunner:
    def search(self, source, **kwargs):
        return [{"id": "planner-1", "title": "Project Planner", "company": "Energy Co", "location": "Aberdeen",
                 "date": "2026-08-20", "url": "https://freehire.me/jobs/planner-1", "description": "Plan work."}]


class _MeteredProbe:
    """Stands in for the AI evaluation: it calls the (metered) understanding
    provider the route hands it, so the boundary is exercised for real."""

    def __init__(self):
        self.provider_calls = 0

    def extract(self, request):
        self.provider_calls += 1
        return SimpleNamespace(payload={}, audit=SimpleNamespace(input_tokens=1000, output_tokens=100))


def _evaluate_through_provider(conn, candidate_id, semantic_adapter, *, understanding_provider, **kwargs):
    understanding_provider.extract({})
    return {"candidate_id": candidate_id}


def _cost_events(settings):
    conn = connect(settings)
    try:
        return conn.execute("SELECT COUNT(*) FROM ai_cost_events").fetchone()[0]
    finally:
        conn.close()


def test_candidates_from_a_metered_discovery_run_are_evaluated_behind_the_ai_boundary(world, monkeypatch):
    app, client, settings, ids, _, calls = world
    probe = _MeteredProbe()
    app.state.discovery_portal_runner = _DiscoveryRunner()
    app.state.job_understanding_provider = probe
    monkeypatch.setattr(discovery_api, "evaluate_discovery_candidate", _evaluate_through_provider)
    base = f"/api/search-workspaces/{ids['search_workspace_id']}/discovery"
    searched = _post(client, f"{base}/search", json={"queries": ["planner"], "locations": []})
    assert searched.status_code == 200, searched.text
    candidate_ids = searched.json()["candidate_ids"]
    calls.clear()
    response = _post(client, f"{base}/evaluate", json={"candidate_ids": candidate_ids, "request_id": "e1"})
    assert response.status_code == 200, response.text
    assert [r["status"] for r in response.json()["results"]] == ["completed"]
    assert ("feature", "discovery.on_demand") in calls  # the discovery action's feature
    assert ("reserve", "discovery.on_demand_runs") not in calls  # no second allowance: the run's covers it
    assert probe.provider_calls == 1 and _cost_events(settings) == 1  # AI switch + ceiling passed, cost recorded


def test_candidates_without_a_metered_discovery_run_never_reach_the_ai(world, monkeypatch):
    app, client, settings, ids, account_id, _ = world
    from webapp.services.discovery import run_discovery_search
    probe = _MeteredProbe()
    app.state.job_understanding_provider = probe
    monkeypatch.setattr(discovery_api, "evaluate_discovery_candidate", _evaluate_through_provider)
    conn = connect(settings)
    try:  # a run that never passed the allowance (no consumed reservation)
        run = run_discovery_search(conn, _DiscoveryRunner(), search_workspace_id=ids["search_workspace_id"],
                                   queries=["planner"], locations=[], account_id=account_id)
        conn.commit()
    finally:
        conn.close()
    base = f"/api/search-workspaces/{ids['search_workspace_id']}/discovery"
    response = _post(client, f"{base}/evaluate", json={"candidate_ids": run["candidate_ids"], "request_id": "e2"})
    assert response.status_code == 200, response.text
    assert [r["status"] for r in response.json()["results"]] == ["failed"]
    assert probe.provider_calls == 0 and _cost_events(settings) == 0


def test_candidate_evaluation_is_refused_when_discovery_is_switched_off(world, monkeypatch):
    app, client, settings, ids, _, _ = world
    from datetime import datetime, timezone
    from webapp.services.entitlements import set_platform_control
    probe = _MeteredProbe()
    app.state.discovery_portal_runner = _DiscoveryRunner()
    app.state.job_understanding_provider = probe
    monkeypatch.setattr(discovery_api, "evaluate_discovery_candidate", _evaluate_through_provider)
    base = f"/api/search-workspaces/{ids['search_workspace_id']}/discovery"
    candidate_ids = _post(client, f"{base}/search", json={"queries": ["planner"], "locations": []}).json()["candidate_ids"]
    conn = connect(settings)
    set_platform_control(conn, "DISCOVERY_ENABLED", False, actor_user_id=None, reason="t",
                         now=datetime.now(timezone.utc))
    conn.commit()
    conn.close()
    response = _post(client, f"{base}/evaluate", json={"candidate_ids": candidate_ids, "request_id": "e3"})
    assert response.status_code == 402 and response.json()["error"] == "FEATURE_NOT_IN_PLAN"
    assert probe.provider_calls == 0


def test_a_completed_prepare_notifies_prepared_and_review_required(world, monkeypatch):
    """Bundle 7 §17.2: the last prepare stage (application intelligence) produces both."""
    _, client, settings, ids, _, _ = world
    monkeypatch.setattr(workspaces_api, "generate_application_intelligence", lambda *a, **k: {"id": "art_intel_1"})
    response = _post(client, f"/api/workspaces/{ids['workspace_id']}/application-intelligence",
                     json={"request_id": "r1"})
    assert response.status_code == 200, response.text
    conn = connect(settings)
    kinds = sorted(r[0] for r in conn.execute("SELECT kind FROM notifications WHERE subject_id = ? "
                                              "AND kind LIKE 'application.%'", (ids["workspace_id"],)))
    conn.close()
    assert kinds == ["application.prepared", "application.review_required"]
