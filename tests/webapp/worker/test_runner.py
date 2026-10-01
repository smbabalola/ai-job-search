"""Bundle 7 Task 18 (spec §20.2): durable jobs, leases, backoff, dead letters
and periodic scheduling."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from webapp.config import Settings
from webapp.persistence.db import connect, init_db
from webapp.worker.runner import (
    JOB_KINDS, PermanentFailure, RetryLater, Worker, backoff_seconds, enqueue,
)
from webapp.worker.schedule import enqueue_periodic

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class Reporter:
    def __init__(self):
        self.events = []

    def report(self, exc, context):
        self.events.append((type(exc).__name__, context))


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    return Settings(db_path=path)


def _enqueue(settings, **kwargs):
    conn = connect(settings)
    try:
        job_id = enqueue(conn, now=kwargs.pop("now", NOW), **kwargs)
        conn.commit()
        return job_id
    finally:
        conn.close()


def _job(settings, job_id):
    conn = connect(settings)
    try:
        return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    finally:
        conn.close()


def test_the_job_kinds_are_the_spec_vocabulary():
    assert JOB_KINDS == (
        "outbox.dispatch", "billing.webhook.process", "email.webhook.process", "usage.sweep", "tokens.sweep",
        "notify.digest", "notify.approval_expiry_scan", "discovery.scheduled_run", "account.purge",
        "account.export", "autonomy.tick")


def test_unknown_kinds_are_refused(settings):
    with pytest.raises(ValueError):
        _enqueue(settings, kind="made.up", payload={})


def test_a_due_job_runs_once_and_succeeds(settings):
    seen = []
    job_id = _enqueue(settings, kind="usage.sweep", payload={"x": 1})
    worker = Worker(settings, {"usage.sweep": lambda ctx, payload: seen.append(payload)}, clock=Clock(),
                    worker_id="w1")
    assert worker.run_once() == 1
    assert seen == [{"x": 1}]
    job = _job(settings, job_id)
    assert (job["status"], job["attempts"], job["lease_holder"]) == ("SUCCEEDED", 1, None)
    assert worker.run_once() == 0


def test_a_future_job_waits_for_its_run_at(settings):
    _enqueue(settings, kind="usage.sweep", payload={}, run_at=NOW + timedelta(minutes=5))
    clock = Clock()
    worker = Worker(settings, {"usage.sweep": lambda ctx, payload: None}, clock=clock, worker_id="w1")
    assert worker.run_once() == 0
    clock.now = NOW + timedelta(minutes=5)
    assert worker.run_once() == 1


def test_dedupe_returns_none_for_a_second_enqueue(settings):
    assert _enqueue(settings, kind="usage.sweep", payload={}, dedupe_key="slot-1") is not None
    assert _enqueue(settings, kind="usage.sweep", payload={}, dedupe_key="slot-1") is None


def test_two_workers_racing_for_one_job_run_it_once(settings):
    _enqueue(settings, kind="usage.sweep", payload={})
    runs = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def handler(ctx, payload):
        with lock:
            runs.append(ctx.worker_id)

    def work(worker_id):
        worker = Worker(settings, {"usage.sweep": handler}, clock=Clock(), worker_id=worker_id)
        barrier.wait()
        worker.run_once()

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(runs) == 1


def test_the_backoff_schedule():
    assert [backoff_seconds(n) for n in range(1, 11)] == [
        60, 120, 240, 480, 960, 1920, 3840, 7680, 15360, 21600]


def test_a_failing_job_retries_with_backoff_then_goes_dead_with_an_ops_alert(settings):
    job_id = _enqueue(settings, kind="usage.sweep", payload={})
    clock = Clock()
    reporter = Reporter()

    def boom(ctx, payload):
        raise RuntimeError("sweep failed")

    worker = Worker(settings, {"usage.sweep": boom}, clock=clock, worker_id="w1", reporter=reporter)
    for attempt in range(1, 9):
        assert worker.run_once() == 1
        job = _job(settings, job_id)
        if attempt < 8:
            assert job["status"] == "QUEUED" and job["attempts"] == attempt
            assert datetime.fromisoformat(job["run_at"]) == clock.now + timedelta(seconds=backoff_seconds(attempt))
            assert "sweep failed" in job["last_error"]
            clock.now = datetime.fromisoformat(job["run_at"])
    job = _job(settings, job_id)
    assert (job["status"], job["attempts"]) == ("DEAD", 8)
    assert job["finished_at"] is not None
    assert [name for name, _ in reporter.events] == ["JobDead"]
    assert reporter.events[0][1]["job_id"] == job_id and reporter.events[0][1]["kind"] == "usage.sweep"


def test_permanent_failure_fails_at_once(settings):
    job_id = _enqueue(settings, kind="usage.sweep", payload={})

    def refuse(ctx, payload):
        raise PermanentFailure("bad payload")

    Worker(settings, {"usage.sweep": refuse}, clock=Clock(), worker_id="w1").run_once()
    job = _job(settings, job_id)
    assert (job["status"], job["attempts"]) == ("FAILED", 1)
    assert "bad payload" in job["last_error"]


def test_retry_later_uses_its_own_delay(settings):
    job_id = _enqueue(settings, kind="billing.webhook.process", payload={})

    def later(ctx, payload):
        raise RetryLater(after_seconds=17)

    Worker(settings, {"billing.webhook.process": later}, clock=Clock(), worker_id="w1").run_once()
    job = _job(settings, job_id)
    assert job["status"] == "QUEUED" and datetime.fromisoformat(job["run_at"]) == NOW + timedelta(seconds=17)


def test_an_expired_lease_lets_another_worker_reclaim(settings):
    job_id = _enqueue(settings, kind="usage.sweep", payload={})
    conn = connect(settings)
    conn.execute("UPDATE jobs SET status = 'RUNNING', attempts = 1, lease_holder = 'dead-worker', "
                 "lease_expires_at = ? WHERE id = ?", ((NOW - timedelta(seconds=1)).isoformat(timespec="microseconds"),
                                                       job_id))
    conn.commit()
    conn.close()
    ran = []
    Worker(settings, {"usage.sweep": lambda ctx, p: ran.append(ctx.worker_id)}, clock=Clock(),
           worker_id="w2").run_once()
    assert ran == ["w2"]
    assert _job(settings, job_id)["attempts"] == 2


def test_a_live_lease_is_not_reclaimed(settings):
    job_id = _enqueue(settings, kind="usage.sweep", payload={})
    conn = connect(settings)
    conn.execute("UPDATE jobs SET status = 'RUNNING', lease_holder = 'busy', lease_expires_at = ? WHERE id = ?",
                 ((NOW + timedelta(seconds=30)).isoformat(timespec="microseconds"), job_id))
    conn.commit()
    conn.close()
    assert Worker(settings, {"usage.sweep": lambda ctx, p: None}, clock=Clock(), worker_id="w2").run_once() == 0


def test_a_job_without_a_handler_is_failed_not_lost(settings):
    job_id = _enqueue(settings, kind="account.export", payload={})
    Worker(settings, {}, clock=Clock(), worker_id="w1").run_once()
    assert _job(settings, job_id)["status"] == "FAILED"


def test_periodic_slots_are_enqueued_once_per_slot(settings):
    conn = connect(settings)
    try:
        enqueue_periodic(conn, settings=settings, now=NOW)
        enqueue_periodic(conn, settings=settings, now=NOW + timedelta(minutes=1))  # same slots
        conn.commit()
        kinds = sorted({r[0] for r in conn.execute("SELECT kind FROM jobs")})
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'tokens.sweep'").fetchone()[0] == 1
        assert kinds == ["discovery.scheduled_run", "notify.approval_expiry_scan", "notify.digest", "outbox.dispatch",
                         "tokens.sweep", "usage.sweep"]
        enqueue_periodic(conn, settings=settings, now=NOW + timedelta(minutes=5))  # the next usage.sweep slot
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'usage.sweep'").fetchone()[0] == 2
    finally:
        conn.close()


def test_autonomy_ticks_are_scheduled_in_hosted_mode_only(settings, tmp_path):
    hosted = Settings(db_path=settings.db_path, deployment="hosted", autonomy_tick_interval=30.0)
    conn = connect(settings)
    try:
        enqueue_periodic(conn, settings=hosted, now=NOW)
        enqueue_periodic(conn, settings=hosted, now=NOW + timedelta(seconds=10))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'autonomy.tick'").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_autonomy_tick_handler_runs_one_driver_tick(settings, monkeypatch):
    from webapp.worker import handlers
    calls = []
    monkeypatch.setattr(handlers, "tick_once", lambda conn, settings, providers, **kw: calls.append(kw["worker_id"]))
    registry = handlers.default_handlers(settings, providers_factory=lambda: object())
    _enqueue(settings, kind="autonomy.tick", payload={})
    Worker(settings, registry, clock=Clock(), worker_id="w9").run_once()
    assert calls == ["w9"]


def test_the_usage_sweep_handler_releases_expired_reservations(settings):
    from webapp.worker import handlers
    registry = handlers.default_handlers(settings, providers_factory=lambda: object())
    assert set(registry) >= {"usage.sweep", "tokens.sweep", "billing.webhook.process", "autonomy.tick"}
    job_id = _enqueue(settings, kind="usage.sweep", payload={})
    Worker(settings, registry, clock=Clock(), worker_id="w1").run_once()
    assert _job(settings, job_id)["status"] == "SUCCEEDED"


def test_the_tokens_sweep_deletes_only_expired_rows(settings):
    from webapp.persistence import identity
    from webapp.storage.profile_sources import DatabaseProfileSourceStore
    from webapp.worker.handlers import sweep_tokens
    conn = connect(settings)
    created = identity.create_user_with_account(
        conn, email="ada@example.com", password_hash="h", display_name="Ada", legal_document_ids=[], now=NOW,
        profile_store=DatabaseProfileSourceStore())
    account_id = created["account"]["id"]
    for code_id, expires in (("old", NOW - timedelta(minutes=1)), ("live", NOW + timedelta(minutes=5))):
        conn.execute("INSERT INTO pairing_codes (id, account_id, user_id, code_hash, created_at, expires_at) "
                     "VALUES (?, ?, NULL, ?, ?, ?)", (code_id, account_id, f"h-{code_id}", NOW.isoformat(),
                                                    expires.isoformat()))
    conn.commit()
    assert sweep_tokens(conn, now=NOW) >= 1
    conn.commit()
    assert [r[0] for r in conn.execute("SELECT id FROM pairing_codes")] == ["live"]
    conn.close()
