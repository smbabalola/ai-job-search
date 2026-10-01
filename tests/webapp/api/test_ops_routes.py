"""Bundle 7 spec §20.6, §10.7 (Task 29): readiness, metrics and dead-letter retry."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from webapp.api import ops
from webapp.app import create_app
from webapp.persistence.db import connect
from webapp.persistence.dbapi import LOCK_STATS
from webapp.worker.runner import enqueue
from tests.webapp.admin_helpers import SECRET_KEY, admin_settings, make_staff, staff_login, staff_post

TOKEN = "metrics-token-for-tests"


@pytest.fixture
def settings(tmp_path):
    return dataclasses.replace(admin_settings(tmp_path), metrics_token=TOKEN)


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_ready_is_200_when_every_check_passes(client):
    response = client.get("/ready")
    assert response.status_code == 200 and response.json() == {"status": "ready"}


@pytest.mark.parametrize("check", list(ops.READINESS_PROBES))
def test_each_failing_check_is_named_without_secrets(client, monkeypatch, check):
    def broken(settings):
        raise RuntimeError(f"cannot reach it with {SECRET_KEY}")
    monkeypatch.setitem(ops.READINESS_PROBES, check, broken)
    response = client.get("/ready")
    assert response.status_code == 503 and response.json()["failing"] == [check]
    assert SECRET_KEY not in response.text and "cannot reach" not in response.text


def test_hosted_readiness_requires_a_real_email_provider_and_a_secret_key(settings):
    hosted = dataclasses.replace(settings, deployment="hosted", email_provider="console", secret_key="short")
    assert ops._email_provider(hosted) is False and ops._secret_key(hosted) is False
    assert ops._email_provider(settings) is True and ops._secret_key(settings) is True  # local mode


def test_metrics_need_the_token(settings):
    app = create_app(dataclasses.replace(settings, metrics_token=None))
    with TestClient(app) as anonymous:
        assert anonymous.get("/metrics").status_code == 404
    app = create_app(settings)
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_metrics_render_the_spec_names(client, settings):
    LOCK_STATS.reset()
    LOCK_STATS.acquired("webapp/services/usage.py:reserve", 0.25)
    LOCK_STATS.released("webapp/services/usage.py:reserve", 0.05)
    LOCK_STATS.timed_out("webapp/services/usage.py:reserve")
    conn = connect(settings)
    try:
        enqueue(conn, kind="usage.sweep", payload={}, now=datetime.now(timezone.utc))
        conn.commit()
    finally:
        conn.close()
    client.get("/health")
    body = client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"}).text
    for name in ("http_requests_total", "http_request_duration_seconds_bucket", "jobs{", "outbox_messages",
                 "billing_webhook_lag_seconds", "ai_calls_total", "database_busy_total",
                 'db_writer_lock_wait_seconds_sum{site="webapp/services/usage.py:reserve"} 0.25',
                 'db_writer_lock_hold_seconds_max{site="webapp/services/usage.py:reserve"} 0.05',
                 'db_writer_lock_timeouts_total{site="webapp/services/usage.py:reserve"} 1'):
        assert name in body, name
    assert 'route="/health"' in body and SECRET_KEY not in body
    LOCK_STATS.reset()


def test_the_admin_dashboard_shows_lock_timeouts_and_the_busiest_sites(client, settings):
    LOCK_STATS.reset()
    for site, wait in (("a.py:1", 0.1), ("b.py:2", 0.9), ("c.py:3", 0.5)):
        LOCK_STATS.acquired(site, wait)
    LOCK_STATS.timed_out("b.py:2")
    summary = ops.writer_lock_summary()
    assert summary["timeouts"] == 1 and [s["site"] for s in summary["top_sites"]] == ["b.py:2", "c.py:3", "a.py:1"]
    staff = staff_login(client.app, make_staff(settings, "ADMIN"))
    page = staff.get("/admin").text
    assert "1 timeouts in 24 h" in page and "b.py:2" in page
    LOCK_STATS.reset()


def test_a_dead_job_retried_by_staff_is_queued_again_and_audited(client, settings):
    now = datetime.now(timezone.utc)
    conn = connect(settings)
    try:
        job_id = enqueue(conn, kind="usage.sweep", payload={}, now=now)
        conn.execute("UPDATE jobs SET status = 'DEAD', attempts = 8, last_error = 'boom' WHERE id = ?", (job_id,))
        conn.commit()
    finally:
        conn.close()
    staff = staff_login(client.app, make_staff(settings, "OPERATIONS"))
    assert staff_post(staff, f"/api/admin/jobs/{job_id}/retry").status_code == 200
    conn = connect(settings)
    try:
        row = conn.execute("SELECT status, attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert tuple(row) == ("QUEUED", 0)
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'DEAD_LETTER_RETRIED' AND target_id = ?",
                            (job_id,)).fetchone()[0] == 1
    finally:
        conn.close()
    assert staff_post(staff, f"/api/admin/jobs/{job_id}/retry").status_code == 409  # only DEAD jobs
