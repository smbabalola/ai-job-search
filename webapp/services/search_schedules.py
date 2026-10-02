"""Scheduled saved searches (Bundle 7 spec §20.3, Power).

A schedule belongs to a search workspace. Enabling needs the
``discovery.scheduled`` feature plus room in the ``discovery.scheduled_searches``
gauge. The worker's ``discovery.scheduled_run`` fans out the due schedules and
runs each: discovery with operator-enabled sources only (DP-9) → the 6C
screening queue when the plan has ``automation.screening`` → a
``discovery.new_matches`` notification (in-app, or the daily digest).

A downgrade below Power or a suspension disables schedules (``enabled = 0``,
with the reason). They are never deleted, and only the user's own enable turns
them back on. Every run re-reads the account's entitlements first."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from webapp.persistence import dbapi
from webapp.services.autonomy_entitlements import entitlement_ceiling, has_feature

__all__ = [
    "CADENCES", "ScheduleError", "active_schedule_count", "disable_for_account", "disable_schedule",
    "due_schedules", "enable_schedule", "get_schedule", "on_account_suspended", "reconcile_entitlements",
    "run_scheduled",
]

logger = logging.getLogger("webapp.search_schedules")

CADENCES = {"DAILY": timedelta(days=1), "WEEKLY": timedelta(days=7)}


class ScheduleError(ValueError):
    pass


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def get_schedule(conn: dbapi.Connection, search_workspace_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM search_schedules WHERE search_workspace_id = ?",
                       (search_workspace_id,)).fetchone()
    return None if row is None else dict(row)


def active_schedule_count(conn: dbapi.Connection, account_id: str) -> int:
    """The ``discovery.scheduled_searches`` gauge."""
    return conn.execute("SELECT COUNT(*) FROM search_schedules WHERE account_id = ? AND enabled = 1",
                        (account_id,)).fetchone()[0]


def enable_schedule(conn: dbapi.Connection, scope: Any, *, search_workspace_id: str, cadence: str, now: datetime,
                    metering: Any) -> dict[str, Any]:
    """Commits. The caller has checked the search workspace belongs to the scope."""
    if cadence not in CADENCES:
        raise ScheduleError(f"cadence must be one of {', '.join(CADENCES)}")
    workspace = conn.execute("SELECT status FROM search_workspaces WHERE id = ? AND account_id = ?",
                             (search_workspace_id, scope.account_id)).fetchone()
    if workspace is None:
        raise ScheduleError("unknown search workspace")
    if workspace[0] != "active":
        raise ScheduleError("archived search workspaces cannot be scheduled")
    metering.require_feature(conn, scope, "discovery.scheduled")
    with dbapi.account_transaction(conn, scope.account_id):
        existing = get_schedule(conn, search_workspace_id)
        if existing is None or not existing["enabled"]:
            metering.gauge_check(conn, scope, "discovery.scheduled_searches", adding=1)
        if existing is None:
            conn.execute(
                "INSERT INTO search_schedules (search_workspace_id, account_id, cadence, enabled, next_run_at, "
                "last_run_id, disabled_reason, updated_at) VALUES (?, ?, ?, 1, ?, NULL, NULL, ?)",
                (search_workspace_id, scope.account_id, cadence, _iso(now), _iso(now)))
        else:
            next_run = existing["next_run_at"] if existing["enabled"] else _iso(now)
            conn.execute("UPDATE search_schedules SET cadence = ?, enabled = 1, disabled_reason = NULL, "
                         "next_run_at = ?, updated_at = ? WHERE search_workspace_id = ?",
                         (cadence, next_run, _iso(now), search_workspace_id))
    return get_schedule(conn, search_workspace_id)


def disable_schedule(conn: dbapi.Connection, scope: Any, *, search_workspace_id: str, now: datetime) -> dict | None:
    """Commits. Never gated (turning automation off always works)."""
    with dbapi.account_transaction(conn, scope.account_id):
        conn.execute("UPDATE search_schedules SET enabled = 0, disabled_reason = 'USER', updated_at = ? "
                     "WHERE search_workspace_id = ? AND account_id = ?",
                     (_iso(now), search_workspace_id, scope.account_id))
    return get_schedule(conn, search_workspace_id)


def disable_for_account(conn: dbapi.Connection, account_id: str, *, reason: str, now: datetime) -> int:
    """No commit. Enabled schedules → ``enabled = 0`` with ``reason``; the rows stay."""
    return conn.execute("UPDATE search_schedules SET enabled = 0, disabled_reason = ?, updated_at = ? "
                        "WHERE account_id = ? AND enabled = 1", (reason, _iso(now), account_id)).rowcount


def reconcile_entitlements(conn: dbapi.Connection, account_id: str, *, settings: Any, now: datetime) -> int:
    """No commit. Called after a subscription change: an account that lost
    ``discovery.scheduled`` has its schedules disabled."""
    if has_feature(conn, account_id, "discovery.scheduled", settings=settings, now=now):
        return 0
    return disable_for_account(conn, account_id, reason="ENTITLEMENT", now=now)


def on_account_suspended(conn: dbapi.Connection, account_id: str, *, now: datetime) -> None:
    """No commit. Suspension (Task 27's admin action): schedules disabled and
    the account's 6C queue items paused. The user resumes them after the
    suspension is lifted; nothing restarts on its own."""
    disable_for_account(conn, account_id, reason="SUSPENDED", now=now)
    conn.execute("UPDATE autonomy_queue_items SET paused = 1, updated_at = ? WHERE account_id = ?",
                 (_iso(now), account_id))


def due_schedules(conn: dbapi.Connection, *, now: datetime) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM search_schedules WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at, "
        "search_workspace_id", (_iso(now),)).fetchall()]


def _advance(conn: dbapi.Connection, schedule: dict[str, Any], *, run_id: str | None, now: datetime) -> None:
    conn.execute("UPDATE search_schedules SET next_run_at = ?, last_run_id = COALESCE(?, last_run_id), "
                 "updated_at = ? WHERE search_workspace_id = ?",
                 (_iso(now + CADENCES[schedule["cadence"]]), run_id, _iso(now), schedule["search_workspace_id"]))


def run_scheduled(conn: dbapi.Connection, *, settings: Any, runner: Any, search_workspace_id: str,
                  now: datetime) -> dict[str, Any]:
    """Commits. Idempotent per due slot: a schedule that is not due (already
    advanced by an earlier attempt) or not enabled does nothing."""
    from webapp.services.discovery import DiscoveryServiceError, run_discovery_search
    from webapp.services.notifications import notify

    schedule = get_schedule(conn, search_workspace_id)
    if schedule is None or not schedule["enabled"] or schedule["next_run_at"] > _iso(now):
        return {"status": "not_due"}
    account_id = schedule["account_id"]
    if not has_feature(conn, account_id, "discovery.scheduled", settings=settings, now=now):
        with dbapi.account_transaction(conn, account_id):
            disable_for_account(conn, account_id, reason="ENTITLEMENT", now=now)
        return {"status": "disabled"}
    ceiling = None  # no screening queue without automation.screening (fail closed)
    if has_feature(conn, account_id, "automation.screening", settings=settings, now=now):
        ceiling = min(settings.autonomy_deployment_ceiling(),
                      entitlement_ceiling(conn, account_id, settings=settings, now=now))
    try:
        result = run_discovery_search(conn, runner, search_workspace_id=search_workspace_id, account_id=account_id,
                                      deployment_ceiling=ceiling, hosted=settings.is_hosted)
    except DiscoveryServiceError as exc:  # e.g. no profile, no enabled source: the next slot tries again
        if conn.in_transaction:
            conn.rollback()
        logger.info("scheduled_search_skipped", extra={"search_workspace_id": search_workspace_id,
                                                       "reason": str(exc)})
        with dbapi.account_transaction(conn, account_id):
            _advance(conn, schedule, run_id=None, now=now)
        return {"status": "skipped", "reason": str(exc)}
    run_id = result["run"]["id"]
    ids = result["candidate_ids"]
    fresh = 0
    if ids:
        marks = ", ".join("?" for _ in ids)
        fresh = conn.execute(f"SELECT COUNT(*) FROM discovery_candidates WHERE id IN ({marks}) "
                             "AND lifecycle_status = 'new'", tuple(ids)).fetchone()[0]
    with dbapi.account_transaction(conn, account_id):
        _advance(conn, schedule, run_id=run_id, now=now)
        if fresh:
            notify(conn, account_id=account_id, kind="discovery.new_matches", subject_type="search_workspace",
                   subject_id=search_workspace_id, dedupe_key=f"discovery.new_matches:{run_id}",
                   detail={"count": fresh, "run_id": run_id}, now=now)
    return {"status": "ran", "run_id": run_id, "new_matches": fresh}
