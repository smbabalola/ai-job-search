from __future__ import annotations

import dataclasses
import logging

from fastapi.testclient import TestClient

from product.autonomy_contract import Capability
from webapp.app import create_app
from webapp.persistence.autonomy_ledger import list_decisions
from webapp.persistence.db import connect
from webapp.services import autonomy_shadow
from webapp.services.autonomy_shadow import record_shadow_decision
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn  # noqa: F401
from tests.webapp.services.test_autonomy_context import seeded, settings  # noqa: F401
from tests.webapp.services.test_autonomy_decide import authorize_all


def test_disabled_by_default(conn, settings, seeded):
    assert record_shadow_decision(conn, settings=settings, account_id=ACCOUNT, workspace_id=seeded,
                                  stage=Capability.PREPARE, now=NOW) is None
    assert list_decisions(conn, seeded) == []


def test_records_non_executable_shadow_decision(conn, settings, seeded):
    authorize_all(conn)
    on = dataclasses.replace(settings, autonomy_shadow_enabled=True)
    row = record_shadow_decision(conn, settings=on, account_id=ACCOUNT, workspace_id=seeded,
                                 stage=Capability.FILL, now=NOW)
    assert row["mode"] == "SHADOW" and row["grantable"] == 0
    assert conn.execute("SELECT COUNT(*) FROM autonomy_grants").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM limit_reservations").fetchone()[0] == 0


def test_shadow_failures_never_break_the_user_flow(conn, settings, seeded, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("shadow bug")
    monkeypatch.setattr(autonomy_shadow, "decide_and_record", boom)
    on = dataclasses.replace(settings, autonomy_shadow_enabled=True)
    assert record_shadow_decision(conn, settings=on, account_id=ACCOUNT, workspace_id=seeded,
                                  stage=Capability.PREPARE, now=NOW) is None
    assert "shadow" in caplog.text.lower()


def test_paused_application_records_nothing_and_logs_no_error(conn, settings, seeded, caplog):
    from webapp.services.autonomy_controls import pause
    pause(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id=seeded, actor="u", reason="r", now=NOW)
    on = dataclasses.replace(settings, autonomy_shadow_enabled=True)
    with caplog.at_level(logging.WARNING):
        assert record_shadow_decision(conn, settings=on, account_id=ACCOUNT, workspace_id=seeded,
                                      stage=Capability.PREPARE, now=NOW) is None
    assert list_decisions(conn, seeded) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---- the real route hooks -----------------------------------------------------

def _app(tmp_path, monkeypatch, *, shadow):
    from tests.webapp.api.test_workspace_routes import _settings

    def fake_run(conn, workspace_id, adapter, *, request_id, active_extensions=None,
                 extension_paths=None, account_id=None):
        return {"id": "art_fit", "artifact_type": "job_fit_result",
                "payload": {"gate_assessments": [], "dimension_assessments": []}}
    monkeypatch.setattr("webapp.services.http_api.run_job_fit", fake_run)
    s = dataclasses.replace(_settings(tmp_path), autonomy_shadow_enabled=shadow)
    app = create_app(s)
    app.state.semantic_adapter = object()
    return app, s


def _fit(client):
    from tests.webapp.api.test_workspace_routes import _create
    workspace_id = _create(client)
    response = client.post(f"/api/workspaces/{workspace_id}/fit", json={"request_id": "fit_1"})
    assert response.status_code == 200, response.text
    return workspace_id


def test_fit_route_records_a_shadow_decision(tmp_path, monkeypatch, caplog):
    app, s = _app(tmp_path, monkeypatch, shadow=True)
    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        workspace_id = _fit(client)
    c = connect(s.db_path)
    try:
        rows = list_decisions(c, workspace_id)
    finally:
        c.close()
    assert [(r["mode"], r["requested_stage"]) for r in rows] == [("SHADOW", "PREPARE")]
    assert not [r for r in caplog.records if "shadow" in r.getMessage().lower()]


def test_fit_route_survives_a_shadow_failure(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch, shadow=True)
    monkeypatch.setattr(autonomy_shadow, "decide_and_record",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("shadow bug")))
    with TestClient(app) as client:
        _fit(client)
