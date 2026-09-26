"""Bundle 6C persistence (spec §4). History/event tables are append-only and
"current" is always derived by seq. The queue/lease helpers (below, Task 5)
mutate coordination state only. Nothing here commits: callers own the
transaction (usually autonomy_controls.run_immediate)."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable

from product.autonomy_contract import Capability, canonical_json, to_utc_iso
from webapp.persistence.autonomy_ledger import upsert_queue_item


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> dict[str, Any]:
    cols, marks = ", ".join(values), ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    return dict(conn.execute(f"SELECT * FROM {table} WHERE seq = ?", (cur.lastrowid,)).fetchone())


# ---- enrolment --------------------------------------------------------------

def record_enrolment(conn, *, account_id: str, application_workspace_id: str, action: str, actor_type: str,
                     actor: str, reason: str | None, now: datetime) -> dict[str, Any]:
    return _insert(conn, "autonomy_enrolments", {
        "id": _id("enrol"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "action": action, "actor_type": actor_type, "actor": actor, "reason": reason, "created_at": to_utc_iso(now),
    })


def current_enrolment(conn, application_workspace_id: str) -> str | None:
    row = conn.execute(
        "SELECT action FROM autonomy_enrolments WHERE application_workspace_id = ? ORDER BY seq DESC LIMIT 1",
        (application_workspace_id,),
    ).fetchone()
    return row["action"] if row else None


def is_enrolled(conn, application_workspace_id: str) -> bool:
    return current_enrolment(conn, application_workspace_id) == "ENROL"


def enrolled_applications(conn, account_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT e.application_workspace_id FROM autonomy_enrolments e WHERE e.account_id = ? AND e.action = 'ENROL' "
        "AND e.seq = (SELECT MAX(x.seq) FROM autonomy_enrolments x "
        "             WHERE x.application_workspace_id = e.application_workspace_id) ORDER BY e.seq",
        (account_id,),
    ).fetchall()
    return [r["application_workspace_id"] for r in rows]


# ---- explicit retries -------------------------------------------------------

def record_retry_request(conn, *, account_id: str, subject_type: str, subject_id: str, step_kind: str,
                         input_fingerprint: str, actor: str, now: datetime) -> dict[str, Any]:
    return _insert(conn, "autonomy_retry_requests", {
        "id": _id("retry"), "account_id": account_id, "subject_type": subject_type, "subject_id": subject_id,
        "step_kind": step_kind, "input_fingerprint": input_fingerprint, "actor": actor,
        "created_at": to_utc_iso(now),
    })


def latest_retry_request(conn, *, subject_type: str, subject_id: str, step_kind: str,
                         input_fingerprint: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM autonomy_retry_requests WHERE subject_type = ? AND subject_id = ? AND step_kind = ? "
        "AND input_fingerprint = ? ORDER BY seq DESC LIMIT 1",
        (subject_type, subject_id, step_kind, input_fingerprint),
    ).fetchone()
    return dict(row) if row else None


# ---- human-review latches ---------------------------------------------------

def record_latch(conn, *, application_workspace_id: str, pack_revision: str, reason: str, actor: str,
                 now: datetime) -> dict[str, Any]:
    return _insert(conn, "autonomy_review_latches", {
        "id": _id("latch"), "application_workspace_id": application_workspace_id, "pack_revision": pack_revision,
        "reason": reason, "actor": actor, "created_at": to_utc_iso(now),
    })


def has_latch(conn, application_workspace_id: str, pack_revision: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM autonomy_review_latches WHERE application_workspace_id = ? AND pack_revision = ?",
        (application_workspace_id, pack_revision),
    ).fetchone() is not None


# ---- notifications (append-only event history) -----------------------------

def _notification_events(conn, account_id: str, key: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM autonomy_notification_events WHERE account_id = ?"
    params: list[Any] = [account_id]
    if key is not None:
        sql += " AND notification_key = ?"
        params.append(key)
    return conn.execute(sql + " ORDER BY seq", params).fetchall()


def _fold(events: Iterable[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    """Latest CREATED cycle per key; open until a later RESOLVED."""
    state: dict[str, dict[str, Any]] = {}
    for e in events:
        key = e["notification_key"]
        if e["event"] == "CREATED":
            state[key] = {"key": key, "kind": e["kind"], "subject_type": e["subject_type"],
                          "subject_id": e["subject_id"], "detail": json.loads(e["detail_json"]),
                          "created_at": e["created_at"], "seen": False, "open": True}
        elif key in state and e["event"] == "SEEN":
            state[key]["seen"] = True
        elif key in state and e["event"] == "RESOLVED":
            state[key]["open"] = False
    return state


def _event(conn, *, account_id: str, key: str, kind: str, subject_type: str, subject_id: str, event: str,
           detail: dict[str, Any], now: datetime) -> None:
    _insert(conn, "autonomy_notification_events", {
        "id": _id("note"), "account_id": account_id, "notification_key": key, "kind": kind,
        "subject_type": subject_type, "subject_id": subject_id, "event": event,
        "detail_json": canonical_json(detail), "created_at": to_utc_iso(now),
    })


def create_notification(conn, *, account_id: str, key: str, kind: str, subject_type: str, subject_id: str,
                        detail: dict[str, Any], now: datetime) -> bool:
    current = _fold(_notification_events(conn, account_id, key)).get(key)
    if current is not None and current["open"]:
        return False
    _event(conn, account_id=account_id, key=key, kind=kind, subject_type=subject_type, subject_id=subject_id,
           event="CREATED", detail=detail, now=now)
    return True


def _transition(conn, *, account_id: str, key: str, event: str, now: datetime) -> bool:
    current = _fold(_notification_events(conn, account_id, key)).get(key)
    if current is None or not current["open"] or (event == "SEEN" and current["seen"]):
        return False
    _event(conn, account_id=account_id, key=key, kind=current["kind"], subject_type=current["subject_type"],
           subject_id=current["subject_id"], event=event, detail={}, now=now)
    return True


def mark_seen(conn, *, account_id: str, key: str, now: datetime) -> bool:
    return _transition(conn, account_id=account_id, key=key, event="SEEN", now=now)


def resolve_notification(conn, *, account_id: str, key: str, now: datetime) -> bool:
    return _transition(conn, account_id=account_id, key=key, event="RESOLVED", now=now)


def open_notifications(conn, account_id: str) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in item.items() if k != "open"}
        for item in _fold(_notification_events(conn, account_id)).values() if item["open"]
    ]


# ---- candidate screenings, promotions, exceptions --------------------------

def _parse_screening(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for field in ("reasons", "require_user", "authority"):
        item[field] = json.loads(item.pop(f"{field}_json"))
    item["could_unlock"] = bool(item["could_unlock"])
    return item


def insert_screening(conn, *, account_id: str, search_workspace_id: str, candidate_id: str,
                     discovery_run_id: str | None, discovery_fit_id: str | None, outcome: str, reason_code: str,
                     reasons: list, require_user: list, could_unlock: bool, retry_at: datetime | None,
                     input_fingerprint: str, authority: dict, policy_version_hash: str | None,
                     subject_policy_hash: str | None, engine_version: str, now: datetime) -> dict[str, Any]:
    row = _insert(conn, "autonomy_candidate_screenings", {
        "id": _id("scr"), "account_id": account_id, "search_workspace_id": search_workspace_id,
        "candidate_id": candidate_id, "discovery_run_id": discovery_run_id, "discovery_fit_id": discovery_fit_id,
        "outcome": outcome, "reason_code": reason_code, "reasons_json": canonical_json(reasons),
        "require_user_json": canonical_json(require_user), "could_unlock": 1 if could_unlock else 0,
        "retry_at": to_utc_iso(retry_at) if retry_at else None, "input_fingerprint": input_fingerprint,
        "authority_json": canonical_json(authority), "policy_version_hash": policy_version_hash,
        "subject_policy_hash": subject_policy_hash, "engine_version": engine_version,
        "created_at": to_utc_iso(now),
    })
    return _parse_screening(conn.execute("SELECT * FROM autonomy_candidate_screenings WHERE seq = ?",
                                         (row["seq"],)).fetchone())


def latest_screening(conn, candidate_id: str) -> dict[str, Any] | None:
    return _parse_screening(conn.execute(
        "SELECT * FROM autonomy_candidate_screenings WHERE candidate_id = ? ORDER BY seq DESC LIMIT 1",
        (candidate_id,)).fetchone())


def get_screening(conn, screening_id: str) -> dict[str, Any] | None:
    return _parse_screening(conn.execute(
        "SELECT * FROM autonomy_candidate_screenings WHERE id = ?", (screening_id,)).fetchone())


def insert_promotion(conn, *, screening_id: str | None, candidate_id: str, search_workspace_id: str,
                     application_workspace_id: str, actor_type: str, actor: str, now: datetime) -> dict[str, Any]:
    return _insert(conn, "autonomy_candidate_promotions", {
        "id": _id("promo"), "screening_id": screening_id, "candidate_id": candidate_id,
        "search_workspace_id": search_workspace_id, "application_workspace_id": application_workspace_id,
        "actor_type": actor_type, "actor": actor, "created_at": to_utc_iso(now),
    })


def promotion_for_candidate(conn, candidate_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM autonomy_candidate_promotions WHERE candidate_id = ? ORDER BY seq DESC LIMIT 1",
                       (candidate_id,)).fetchone()
    return dict(row) if row else None


def open_candidate_exception(conn, *, account_id: str, search_workspace_id: str, candidate_id: str,
                             screening_id: str, items: list, now: datetime) -> dict[str, Any]:
    return _insert(conn, "autonomy_candidate_exceptions", {
        "id": _id("cexc"), "account_id": account_id, "search_workspace_id": search_workspace_id,
        "candidate_id": candidate_id, "screening_id": screening_id, "items_json": canonical_json(items),
        "created_at": to_utc_iso(now),
    })


def resolve_candidate_exception(conn, *, exception_id: str, resolution: str, actor: str, reason: str | None,
                                now: datetime) -> dict[str, Any]:
    return _insert(conn, "autonomy_candidate_exception_resolutions", {
        "id": _id("cres"), "exception_id": exception_id, "resolution": resolution, "actor": actor,
        "reason": reason, "created_at": to_utc_iso(now),
    })


def _with_resolution(conn, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["items"] = json.loads(item.pop("items_json"))
    res = conn.execute("SELECT * FROM autonomy_candidate_exception_resolutions WHERE exception_id = ? "
                       "ORDER BY seq DESC LIMIT 1", (item["id"],)).fetchone()
    item["resolution"] = dict(res) if res else None
    return item


def get_candidate_exception(conn, exception_id: str) -> dict[str, Any] | None:
    return _with_resolution(conn, conn.execute("SELECT * FROM autonomy_candidate_exceptions WHERE id = ?",
                                               (exception_id,)).fetchone())


def current_candidate_exception(conn, candidate_id: str) -> dict[str, Any] | None:
    return _with_resolution(conn, conn.execute(
        "SELECT * FROM autonomy_candidate_exceptions WHERE candidate_id = ? ORDER BY seq DESC LIMIT 1",
        (candidate_id,)).fetchone())


def open_candidate_exceptions(conn, account_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT e.* FROM autonomy_candidate_exceptions e WHERE e.account_id = ? AND NOT EXISTS ("
        "  SELECT 1 FROM autonomy_candidate_exception_resolutions r WHERE r.exception_id = e.id) ORDER BY e.seq",
        (account_id,),
    ).fetchall()
    return [_with_resolution(conn, r) for r in rows]


# ---- step-attempt log (observational only) ---------------------------------

_TERMINAL = ("SUCCEEDED", "REUSED", "FAILED", "ABANDONED")


def _parse_attempt(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["artifact_refs"] = json.loads(item.pop("artifact_refs_json"))
    item["reservation_ids"] = json.loads(item.pop("reservation_ids_json"))
    item["cost"] = json.loads(item.pop("cost_json"))
    return item


def next_attempt_no(conn, subject_type: str, subject_id: str, step_kind: str) -> int:
    row = conn.execute(
        "SELECT MAX(attempt_no) AS n FROM autonomy_prepare_steps WHERE subject_type = ? AND subject_id = ? "
        "AND step_kind = ?", (subject_type, subject_id, step_kind)).fetchone()
    return (row["n"] or 0) + 1


def start_attempt(conn, *, subject_type: str, subject_id: str, step_kind: str, attempt_no: int,
                  input_fingerprint: str, authorization_decision_id: str | None, retry_request_id: str | None,
                  lease_generation: int, worker_id: str, reservation_ids: list[str], now: datetime,
                  run_id: str | None = None) -> str:
    attempt_id = _id("att6c")
    _insert(conn, "autonomy_prepare_steps", {
        "id": _id("step"), "attempt_id": attempt_id, "subject_type": subject_type, "subject_id": subject_id,
        "step_kind": step_kind, "attempt_no": attempt_no, "event": "STARTED", "input_fingerprint": input_fingerprint,
        "authorization_decision_id": authorization_decision_id, "retry_request_id": retry_request_id,
        "lease_generation": lease_generation, "worker_id": worker_id, "run_id": run_id,
        "artifact_refs_json": "[]", "reservation_ids_json": canonical_json(list(reservation_ids)),
        "cost_json": "{}", "error_class": None, "error_code": None, "error_detail": None,
        "created_at": to_utc_iso(now),
    })
    return attempt_id


def finish_attempt(conn, *, attempt_id: str, event: str, now: datetime, artifact_refs=(), cost=None,
                   error_class: str | None = None, error_code: str | None = None,
                   error_detail: str | None = None) -> dict[str, Any]:
    if event not in _TERMINAL:
        raise ValueError(f"not a terminal attempt event: {event}")
    started = conn.execute("SELECT * FROM autonomy_prepare_steps WHERE attempt_id = ? AND event = 'STARTED'",
                           (attempt_id,)).fetchone()
    if started is None:
        raise LookupError(attempt_id)
    row = _insert(conn, "autonomy_prepare_steps", {
        "id": _id("step"), "attempt_id": attempt_id, "subject_type": started["subject_type"],
        "subject_id": started["subject_id"], "step_kind": started["step_kind"], "attempt_no": started["attempt_no"],
        "event": event, "input_fingerprint": started["input_fingerprint"],
        "authorization_decision_id": started["authorization_decision_id"],
        "retry_request_id": started["retry_request_id"], "lease_generation": started["lease_generation"],
        "worker_id": started["worker_id"], "run_id": started["run_id"],
        "artifact_refs_json": canonical_json(list(artifact_refs)),
        "reservation_ids_json": started["reservation_ids_json"], "cost_json": canonical_json(cost or {}),
        "error_class": error_class, "error_code": error_code, "error_detail": error_detail,
        "created_at": to_utc_iso(now),
    })
    return _parse_attempt(conn.execute("SELECT * FROM autonomy_prepare_steps WHERE seq = ?",
                                       (row["seq"],)).fetchone())


def attempt_rows(conn, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM autonomy_prepare_steps WHERE subject_type = ? AND subject_id = ? ORDER BY seq",
                        (subject_type, subject_id)).fetchall()
    return [_parse_attempt(r) for r in rows]


def orphaned_attempts(conn, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.* FROM autonomy_prepare_steps s WHERE s.subject_type = ? AND s.subject_id = ? "
        "AND s.event = 'STARTED' AND NOT EXISTS (SELECT 1 FROM autonomy_prepare_steps t "
        "  WHERE t.attempt_id = s.attempt_id AND t.event != 'STARTED') ORDER BY s.seq",
        (subject_type, subject_id)).fetchall()
    return [_parse_attempt(r) for r in rows]


def cycle_failures(conn, *, subject_type: str, subject_id: str, step_kind: str, input_fingerprint: str,
                   retry_request_id: str | None) -> int:
    """Consecutive TRANSIENT failures (incl. ABANDONED) of this step + input
    fingerprint within the current cycle (same retry_request_id), newest
    first, stopping at the first other terminal outcome."""
    rows = conn.execute(
        "SELECT event, error_class, retry_request_id FROM autonomy_prepare_steps WHERE subject_type = ? "
        "AND subject_id = ? AND step_kind = ? AND input_fingerprint = ? AND event != 'STARTED' ORDER BY seq DESC",
        (subject_type, subject_id, step_kind, input_fingerprint)).fetchall()
    count = 0
    for r in rows:
        if r["retry_request_id"] != retry_request_id:
            break
        if r["event"] == "ABANDONED" or (r["event"] == "FAILED" and r["error_class"] == "TRANSIENT"):
            count += 1
            continue
        break
    return count


# ---- queues and fenced leases (mutable coordination state) -----------------

QUEUES = {
    "APPLICATION": ("autonomy_queue_items", "application_workspace_id"),
    "CANDIDATE": ("autonomy_candidate_queue", "candidate_id"),
}


def enqueue_application(conn, *, application_workspace_id: str, account_id: str, now: datetime) -> None:
    upsert_queue_item(conn, application_workspace_id=application_workspace_id, account_id=account_id,
                      next_stage=Capability.PREPARE, next_eligible_at=now, now=now)


def enqueue_candidate(conn, *, candidate_id: str, account_id: str, search_workspace_id: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO autonomy_candidate_queue (candidate_id, account_id, search_workspace_id, next_eligible_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(candidate_id) DO UPDATE SET "
        "next_eligible_at = excluded.next_eligible_at, updated_at = excluded.updated_at",
        (candidate_id, account_id, search_workspace_id, to_utc_iso(now), to_utc_iso(now)),
    )


def wake(conn, *, queue: str, item_id: str, now: datetime) -> bool:
    table, key = QUEUES[queue]
    cur = conn.execute(f"UPDATE {table} SET next_eligible_at = ?, updated_at = ? WHERE {key} = ?",
                       (to_utc_iso(now), to_utc_iso(now), item_id))
    return cur.rowcount == 1


def wake_account(conn, *, account_id: str, now: datetime, queues=("APPLICATION", "CANDIDATE")) -> int:
    total = 0
    for queue in queues:
        table, _ = QUEUES[queue]
        total += conn.execute(f"UPDATE {table} SET next_eligible_at = ?, updated_at = ? WHERE account_id = ?",
                              (to_utc_iso(now), to_utc_iso(now), account_id)).rowcount
    return total


def set_dormant(conn, *, queue: str, item_id: str, now: datetime) -> None:
    table, key = QUEUES[queue]
    conn.execute(f"UPDATE {table} SET next_eligible_at = NULL, updated_at = ? WHERE {key} = ?",
                 (to_utc_iso(now), item_id))


def due_items(conn, *, queue: str, now: datetime, limit: int) -> list[dict[str, Any]]:
    table, key = QUEUES[queue]
    t = to_utc_iso(now)
    rows = conn.execute(
        f"SELECT {key} AS item_id, account_id FROM {table} WHERE next_eligible_at IS NOT NULL "
        f"AND next_eligible_at <= ? AND (lease_holder IS NULL OR lease_expires_at <= ?) "
        f"ORDER BY next_eligible_at, {key} LIMIT ?", (t, t, limit)).fetchall()
    return [dict(r) for r in rows]


def acquire_lease(conn, *, queue: str, item_id: str, worker_id: str, now: datetime, ttl: timedelta) -> int | None:
    table, key = QUEUES[queue]
    t = to_utc_iso(now)
    cur = conn.execute(
        f"UPDATE {table} SET lease_holder = ?, lease_generation = lease_generation + 1, lease_expires_at = ?, "
        f"updated_at = ? WHERE {key} = ? AND next_eligible_at IS NOT NULL AND next_eligible_at <= ? "
        f"AND (lease_holder IS NULL OR lease_expires_at <= ?)",
        (worker_id, to_utc_iso(now + ttl), t, item_id, t, t))
    if cur.rowcount != 1:
        return None
    return conn.execute(f"SELECT lease_generation FROM {table} WHERE {key} = ?", (item_id,)).fetchone()[0]


def lease_is_held(conn, *, queue: str, item_id: str, worker_id: str, generation: int, now: datetime) -> bool:
    table, key = QUEUES[queue]
    return conn.execute(
        f"SELECT 1 FROM {table} WHERE {key} = ? AND lease_holder = ? AND lease_generation = ? AND lease_expires_at > ?",
        (item_id, worker_id, generation, to_utc_iso(now))).fetchone() is not None


def finalize_lease(conn, *, queue: str, item_id: str, worker_id: str, generation: int, now: datetime,
                   next_eligible_at: datetime | None) -> bool:
    table, key = QUEUES[queue]
    cur = conn.execute(
        f"UPDATE {table} SET lease_holder = NULL, lease_expires_at = NULL, next_eligible_at = ?, updated_at = ? "
        f"WHERE {key} = ? AND lease_holder = ? AND lease_generation = ? AND lease_expires_at > ?",
        (to_utc_iso(next_eligible_at) if next_eligible_at else None, to_utc_iso(now), item_id, worker_id,
         generation, to_utc_iso(now)))
    return cur.rowcount == 1


def release_lease(conn, *, queue: str, item_id: str, worker_id: str, generation: int, now: datetime) -> bool:
    table, key = QUEUES[queue]
    cur = conn.execute(
        f"UPDATE {table} SET lease_holder = NULL, lease_expires_at = NULL, updated_at = ? "
        f"WHERE {key} = ? AND lease_holder = ? AND lease_generation = ? AND lease_expires_at > ?",
        (to_utc_iso(now), item_id, worker_id, generation, to_utc_iso(now)))
    return cur.rowcount == 1
