from __future__ import annotations

import time

from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.config import Settings


def _settings(tmp_path, enabled):
    return Settings(db_path=tmp_path / "db.sqlite3", autonomy_scheduler_enabled=enabled, autonomy_tick_interval=0.05)


def test_in_app_driver_runs_only_when_the_gate_is_on_and_stops_on_shutdown(tmp_path):
    app = create_app(_settings(tmp_path, True))
    app.state.job_understanding_provider = object()
    app.state.semantic_adapter = object()
    app.state.application_intelligence_provider = object()
    with TestClient(app):
        for _ in range(100):
            if app.state.autonomy_driver.get("last_tick_at"):
                break
            time.sleep(0.02)
        assert app.state.autonomy_driver["running"] is True and app.state.autonomy_driver["last_tick_at"]
    assert app.state.autonomy_driver["running"] is False


def test_in_app_driver_is_absent_with_the_gate_off(tmp_path):
    app = create_app(_settings(tmp_path, False))
    with TestClient(app):
        assert app.state.autonomy_driver == {"running": False}
