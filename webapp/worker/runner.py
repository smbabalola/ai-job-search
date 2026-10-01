"""Durable jobs and the worker loop (Bundle 7 spec §20.2).

A job is claimed by a conditional UPDATE (a QUEUED row that is due, or a
RUNNING row whose lease expired), so two workers never run the same claim.
The lease (60 s) is renewed while the handler runs. Outcomes: success →
SUCCEEDED; ``PermanentFailure`` → FAILED at once; ``RetryLater`` → QUEUED
after its own delay; any other exception → QUEUED after
``min(2^attempts × 30 s, 6 h)``; once ``attempts`` reaches ``max_attempts``
the job is DEAD and an ops alert is reported. Every finishing write is
conditional on still holding the lease, so a reclaimed job is never
overwritten by its former holder. Handlers are idempotent.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import threading
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from webapp.persistence import dbapi
from webapp.persistence.bundle7_migrations import JOB_KINDS

__all__ = [
    "Handler", "JOB_KINDS", "JobContext", "JobDead", "PermanentFailure", "RetryLater", "Worker", "backoff_seconds",
    "enqueue",
]

logger = logging.getLogger("webapp.worker")

LEASE = timedelta(seconds=60)
RENEW_EVERY = 20.0
MAX_BACKOFF_SECONDS = 6 * 3600
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_TIMEOUT_SECONDS = 300.0
TIMEOUTS = {"autonomy.tick": 900.0, "discovery.scheduled_run": 900.0, "account.export": 1800.0,
            "account.purge": 1800.0}


class RetryLater(Exception):
    """Valid work that cannot run yet; retried after ``after_seconds`` (or the backoff)."""

    def __init__(self, reason: str = "retry later", *, after_seconds: float | None = None):
        super().__init__(reason)
        self.after_seconds = after_seconds


class PermanentFailure(Exception):
    """The job can never succeed (bad payload, missing subject): FAILED at once."""


class JobDead(Exception):
    """Reported to ops when a job exhausts its attempts."""


@dataclass(frozen=True)
class JobContext:
    job_id: str
    kind: str
    account_id: str | None
    attempt: int
    worker_id: str
    settings: Any
    clock: Callable[[], datetime]

    def connect(self) -> dbapi.Connection:
        from webapp.persistence.db import connect
        return connect(self.settings)


Handler = Callable[[JobContext, dict], None]


def ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def backoff_seconds(attempts: int) -> int:
    return min(2 ** attempts * 30, MAX_BACKOFF_SECONDS)


def enqueue(conn: dbapi.Connection, *, kind: str, payload: Mapping[str, Any], account_id: str | None = None,
            run_at: datetime | None = None, dedupe_key: str | None = None, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
            now: datetime) -> str | None:
    """Inside the caller's transaction (no commit). None when ``dedupe_key`` exists."""
    if kind not in JOB_KINDS:
        raise ValueError(f"unknown job kind {kind}")
    job_id = f"job_{uuid.uuid4().hex[:20]}"
    cursor = conn.execute(
        "INSERT INTO jobs (id, kind, account_id, payload_json, dedupe_key, run_at, attempts, max_attempts, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'QUEUED', ?, ?) ON CONFLICT DO NOTHING",
        (job_id, kind, account_id, json.dumps(dict(payload), sort_keys=True, default=str), dedupe_key,
         ts(run_at or now), max_attempts, ts(now), ts(now)))
    return job_id if cursor.rowcount == 1 else None


_DUE = "((status = 'QUEUED' AND run_at <= ?) OR (status = 'RUNNING' AND lease_expires_at <= ?))"


class Worker:
    def __init__(self, settings: Any, handlers: Mapping[str, Handler], *, clock: Callable[[], datetime] | None = None,
                 worker_id: str, reporter: Any = None, batch: int = 20) -> None:
        from webapp.observability import LogErrorReporter
        self.settings = settings
        self.handlers = dict(handlers)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.worker_id = worker_id
        self.reporter = reporter or LogErrorReporter()
        self.batch = batch

    def _connect(self) -> dbapi.Connection:
        from webapp.persistence.db import connect
        return connect(self.settings)

    # ---- claim -----------------------------------------------------------------
    def _claim(self, conn: dbapi.Connection) -> dict[str, Any] | None:
        now = ts(self.clock())
        candidates = [r[0] for r in conn.execute(
            f"SELECT id FROM jobs WHERE {_DUE} ORDER BY run_at, id LIMIT 5", (now, now))]
        conn.commit()
        for job_id in candidates:
            try:
                cursor = conn.execute(
                    f"UPDATE jobs SET status = 'RUNNING', lease_holder = ?, lease_expires_at = ?, "
                    f"attempts = attempts + 1, updated_at = ? WHERE id = ? AND {_DUE}",
                    (self.worker_id, ts(self.clock() + LEASE), now, job_id, now, now))
                conn.commit()
            except dbapi.DatabaseBusy:
                # PostgreSQL REPEATABLE READ refuses the losing concurrent claim
                # (serialization failure) where SQLite reports rowcount 0: either
                # way another worker has it. A job still due is claimed next tick.
                conn.rollback()
                continue
            if cursor.rowcount == 1:
                return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
        return None

    def _finish(self, conn: dbapi.Connection, job: Mapping[str, Any], *, status: str, error: str | None = None,
                run_at: datetime | None = None) -> bool:
        now = self.clock()
        finished = None if status == "QUEUED" else ts(now)
        cursor = conn.execute(
            "UPDATE jobs SET status = ?, lease_holder = NULL, lease_expires_at = NULL, last_error = ?, "
            "run_at = COALESCE(?, run_at), updated_at = ?, finished_at = ? WHERE id = ? AND lease_holder = ?",
            (status, error, ts(run_at) if run_at else None, ts(now), finished, job["id"], self.worker_id))
        conn.commit()
        if cursor.rowcount != 1:
            logger.warning("job_finished_after_reclaim job=%s kind=%s", job["id"], job["kind"])
        return cursor.rowcount == 1

    def _renew(self, job_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE id = ? AND lease_holder = ? "
                         "AND status = 'RUNNING'", (ts(self.clock() + LEASE), ts(self.clock()), job_id, self.worker_id))
            conn.commit()
        finally:
            conn.close()

    # ---- run -------------------------------------------------------------------
    def _execute(self, job: Mapping[str, Any], handler: Handler) -> None:
        context = JobContext(job_id=job["id"], kind=job["kind"], account_id=job["account_id"],
                             attempt=job["attempts"], worker_id=self.worker_id, settings=self.settings,
                             clock=self.clock)
        payload = json.loads(job["payload_json"])
        timeout = TIMEOUTS.get(job["kind"], DEFAULT_TIMEOUT_SECONDS)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"job-{job['kind']}")
        try:
            future = executor.submit(handler, context, payload)
            waited = 0.0
            while True:
                try:
                    future.result(timeout=min(RENEW_EVERY, timeout - waited))
                    return
                except concurrent.futures.TimeoutError:
                    waited += RENEW_EVERY
                    if waited >= timeout:
                        raise TimeoutError(f"{job['kind']} exceeded its {timeout:.0f} s timeout") from None
                    self._renew(job["id"])
        finally:
            executor.shutdown(wait=False)

    def _run(self, conn: dbapi.Connection, job: Mapping[str, Any]) -> None:
        handler = self.handlers.get(job["kind"])
        if handler is None:
            self._finish(conn, job, status="FAILED", error=f"no handler for {job['kind']}")
            return
        try:
            self._execute(job, handler)
        except PermanentFailure as exc:
            self._finish(conn, job, status="FAILED", error=_error_text(exc))
        except Exception as exc:  # noqa: BLE001 - every other failure is retried or dead-lettered
            after = exc.after_seconds if isinstance(exc, RetryLater) and exc.after_seconds is not None else None
            if job["attempts"] >= job["max_attempts"]:
                if self._finish(conn, job, status="DEAD", error=_error_text(exc)):
                    self.reporter.report(JobDead(f"{job['kind']} is dead after {job['attempts']} attempts"),
                                         {"job_id": job["id"], "kind": job["kind"], "account_id": job["account_id"],
                                          "attempts": job["attempts"], "last_error": _error_text(exc)})
            else:
                delay = after if after is not None else backoff_seconds(job["attempts"])
                self._finish(conn, job, status="QUEUED", error=_error_text(exc),
                             run_at=self.clock() + timedelta(seconds=delay))
        else:
            self._finish(conn, job, status="SUCCEEDED")

    def run_once(self) -> int:
        """Claim and run up to ``batch`` due jobs; the number run."""
        conn = self._connect()
        count = 0
        try:
            while count < self.batch:
                job = self._claim(conn)
                if job is None:
                    break
                self._run(conn, job)
                count += 1
        finally:
            conn.close()
        return count

    def run_forever(self, stop: threading.Event, *, poll_seconds: float = 2.0) -> None:
        from webapp.worker.schedule import enqueue_periodic
        while not stop.is_set():
            try:
                conn = self._connect()
                try:
                    enqueue_periodic(conn, settings=self.settings, now=self.clock())
                    conn.commit()
                finally:
                    conn.close()
                ran = self.run_once()
            except Exception:  # noqa: BLE001 - the loop survives a database hiccup
                logger.exception("worker_loop_error")
                ran = 0
            if not ran:
                stop.wait(poll_seconds)


def _error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    tail = traceback.format_exception_only(type(exc), exc)[-1].strip()
    return (text if text.strip() else tail)[:2000]
