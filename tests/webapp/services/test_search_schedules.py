"""Bundle 7 spec §20.3 (Task 26): Power saved searches that run on a schedule,
the worker fan-out, DP-9 hosted sources, and disabling on a lost entitlement."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from product.entitlements import FeatureNotInPlan
from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.db import connect
from webapp.persistence.discovery_sources import list_enabled_discovery_source_ids, set_discovery_source_enabled
from webapp.services import search_schedules as schedules
from webapp.services.entitlements import add_grant, set_platform_control
from webapp.services.ownership import AccountScope
from webapp.services.usage import AllowanceExhausted
from webapp.worker.handlers import default_handlers
from webapp.worker.runner import JobContext
from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
from tests.webapp.factories import build_account_graph

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


class Runner:
    def __init__(self):
        self.calls = 0

    def search(self, source, **kwargs):
        self.calls += 1
        return [{"id": f"planner-{self.calls}", "title": "Project Planner", "company": "Energy Co",
                 "location": "Aberdeen", "date": "2026-09-20", "url": f"https://freehire.me/jobs/planner-{self.calls}",
                 "description": "Plan work."}]


@pytest.fixture
def world(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        extensions_dir=Path(__file__).parents[2] / "fixtures" / "extensions",
                        auth_required_in_local=True)
    app = create_app(settings)
    with TestClient(app) as client:
        publish_legal_documents(settings)
        sign_up_and_verify(client)
        sign_in(client)
        account_id = client.get("/auth/me").json()["account_id"]
        conn = connect(settings)
        graph = build_account_graph(conn, account_id=account_id, documents_root=settings.documents_root)
        conn.commit()
        scope = AccountScope(account_id=account_id, profile_root=settings.profile_root, user_id=None)
        yield SimpleNamespace(app=app, client=client, settings=settings, conn=conn, scope=scope,
                              sws=graph["ids"]["search_workspace_id"], account_id=account_id)
        conn.close()


def _power(w, *, expires_at=NOW + timedelta(days=365)):
    set_platform_control(w.conn, "AUTOMATION_ENABLED", True, actor_user_id=None, reason="test", now=NOW)
    grant = add_grant(w.conn, account_id=w.account_id, kind="PLAN_OVERRIDE", plan_id="power", reason="test",
                      actor_user_id=None, starts_at=NOW - timedelta(days=1), expires_at=expires_at, now=NOW)
    w.conn.commit()
    return grant


def _enable(w, sws=None, cadence="DAILY", now=NOW):
    return schedules.enable_schedule(w.conn, w.scope, search_workspace_id=sws or w.sws, cadence=cadence, now=now,
                                     metering=w.app.state.metering)


def _second_search_workspace(w, name):
    from webapp.persistence.search_workspaces import create_search_workspace
    row = create_search_workspace(w.conn, name=name, account_id=w.account_id)
    w.conn.commit()
    return row["id"]


def test_pro_or_free_cannot_enable_a_schedule(world):
    with pytest.raises(FeatureNotInPlan):
        _enable(world)
    assert schedules.get_schedule(world.conn, world.sws) is None
    response = world.client.post(f"/api/search-workspaces/{world.sws}/schedule", json={"cadence": "DAILY"},
                                 headers={"X-CSRF-Token": csrf_token(world.client)})
    assert response.status_code == 402 and response.json()["error"] == "FEATURE_NOT_IN_PLAN"


def test_power_can_enable_up_to_the_gauge(world, monkeypatch):
    _power(world)
    first = _enable(world)
    assert (first["enabled"], first["cadence"], first["next_run_at"]) == (1, "DAILY", NOW.isoformat())
    assert _enable(world, cadence="WEEKLY")["cadence"] == "WEEKLY"  # re-enabling is not a second slot
    gate = world.app.state.metering.gate
    resolved = gate.entitlements(world.conn, world.scope, now=NOW)
    monkeypatch.setattr(gate, "entitlements", lambda *a, **k: dataclasses.replace(
        resolved, allowances={**resolved.allowances, "discovery.scheduled_searches": 1}))
    other = _second_search_workspace(world, "Second search")
    with pytest.raises(AllowanceExhausted):
        _enable(world, sws=other)
    assert schedules.get_schedule(world.conn, other) is None


def test_disabling_is_never_gated(world):
    grant = _power(world)
    _enable(world)
    world.conn.execute("UPDATE entitlement_grants SET revoked_at = ? WHERE id = ?", (NOW.isoformat(), grant))
    world.conn.commit()
    response = world.client.post(f"/api/search-workspaces/{world.sws}/schedule/disable",
                                 headers={"X-CSRF-Token": csrf_token(world.client)})
    assert response.status_code == 200
    assert (response.json()["schedule"]["enabled"], response.json()["schedule"]["disabled_reason"]) == (0, "USER")


def test_a_scheduled_run_finds_candidates_queues_screening_and_notifies(world):
    _power(world)
    settings = dataclasses.replace(world.settings, autonomy_max_capability="PREPARE")
    from webapp.services.autonomy_controls import enable_autonomous_preparation
    enable_autonomous_preparation(world.conn, account_id=world.account_id, actor="u", timezone="Europe/London",
                                  now=NOW)
    _enable(world)
    runner = Runner()
    result = schedules.run_scheduled(world.conn, settings=settings, runner=runner, search_workspace_id=world.sws,
                                     now=NOW + timedelta(minutes=1))
    assert result["status"] == "ran" and result["new_matches"] >= 1
    queued = world.conn.execute("SELECT COUNT(*) FROM autonomy_candidate_queue WHERE account_id = ?",
                                (world.account_id,)).fetchone()[0]
    assert queued == result["new_matches"]
    kinds = [r[0] for r in world.conn.execute("SELECT kind FROM notifications WHERE account_id = ?",
                                              (world.account_id,)).fetchall()]
    assert "discovery.new_matches" in kinds
    schedule = schedules.get_schedule(world.conn, world.sws)
    assert schedule["last_run_id"] == result["run_id"]
    assert schedule["next_run_at"] == (NOW + timedelta(minutes=1, days=1)).isoformat()
    again = schedules.run_scheduled(world.conn, settings=settings, runner=runner, search_workspace_id=world.sws,
                                    now=NOW + timedelta(minutes=2))
    assert again == {"status": "not_due"}  # a retried job never runs the same slot twice


def test_without_screening_the_run_queues_nothing(world):
    _power(world)
    _enable(world)
    from webapp.services.autonomy_controls import enable_autonomous_preparation
    enable_autonomous_preparation(world.conn, account_id=world.account_id, actor="u", timezone="Europe/London",
                                  now=NOW)
    set_platform_control(world.conn, "AUTOMATION_ENABLED", False, actor_user_id=None, reason="ops", now=NOW)
    world.conn.commit()  # the platform control turns automation.screening off; scheduled search stays
    settings = dataclasses.replace(world.settings, autonomy_max_capability="PREPARE")
    result = schedules.run_scheduled(world.conn, settings=settings, runner=Runner(), search_workspace_id=world.sws,
                                     now=NOW)
    assert result["status"] == "ran"
    assert world.conn.execute("SELECT COUNT(*) FROM autonomy_candidate_queue").fetchone()[0] == 0


def test_a_run_after_losing_power_disables_the_schedule_without_searching(world):
    grant = _power(world)
    _enable(world)
    world.conn.execute("UPDATE entitlement_grants SET revoked_at = ? WHERE id = ?", (NOW.isoformat(), grant))
    world.conn.commit()
    runner = Runner()
    assert schedules.run_scheduled(world.conn, settings=world.settings, runner=runner, search_workspace_id=world.sws,
                                   now=NOW)["status"] == "disabled"
    assert runner.calls == 0
    schedule = schedules.get_schedule(world.conn, world.sws)
    assert (schedule["enabled"], schedule["disabled_reason"]) == (0, "ENTITLEMENT")


def test_the_worker_fans_out_due_schedules_once_per_slot(world):
    _power(world)
    _enable(world)
    handlers = default_handlers(world.settings, providers_factory=lambda: None, discovery_runner=lambda s: Runner())
    ctx = JobContext(job_id="job_1", kind="discovery.scheduled_run", account_id=None, attempt=1, worker_id="w1",
                     settings=world.settings, clock=lambda: NOW + timedelta(minutes=1))
    handlers["discovery.scheduled_run"](ctx, {})
    handlers["discovery.scheduled_run"](ctx, {})
    jobs = world.conn.execute("SELECT payload_json FROM jobs WHERE kind = 'discovery.scheduled_run'").fetchall()
    assert len(jobs) == 1 and world.sws in jobs[0][0]
    handlers["discovery.scheduled_run"](ctx, {"search_workspace_id": world.sws})
    assert schedules.get_schedule(world.conn, world.sws)["last_run_id"] is not None


def test_hosted_sources_run_only_once_an_operator_enabled_them(world):
    seeded = list_enabled_discovery_source_ids(world.conn)
    assert seeded and list_enabled_discovery_source_ids(world.conn, hosted=True) == []
    set_discovery_source_enabled(world.conn, seeded[0], True)
    assert list_enabled_discovery_source_ids(world.conn, hosted=True) == [seeded[0]]
