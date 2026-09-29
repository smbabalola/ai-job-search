# webapp/persistence/submit.py
"""Bundle 6E-A persistence (spec §17). Append-only evidence; no function
commits (callers own the transaction); current state is derived by seq.
Every JSON payload is scanned for cleartext (J9), exactly as in 6D-B."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from product.autonomy_contract import to_utc_iso
from webapp.persistence.fill import _decode, _id, _insert, _json


# ---- authorizations -------------------------------------------------------------------

def new_authorization_id() -> str:
    return _id("hsa")


def insert_authorization(conn, *, id: str | None = None, account_id: str, application_workspace_id: str,
                         fill_run_id: str, review_hash: str, review: dict[str, Any], grant_id: str, actor: str,
                         now: datetime) -> dict[str, Any]:
    return _decode(_insert(conn, "human_submit_authorizations", {
        "id": id or new_authorization_id(), "account_id": account_id,
        "application_workspace_id": application_workspace_id, "fill_run_id": fill_run_id,
        "review_hash": review_hash, "review_json": _json(review), "grant_id": grant_id, "actor": actor,
        "created_at": to_utc_iso(now)}), "review_json")


def get_authorization(conn, authorization_id: str) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT * FROM human_submit_authorizations WHERE id = ?",
                                (authorization_id,)).fetchone(), "review_json")


def authorization_for_run(conn, fill_run_id: str) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT * FROM human_submit_authorizations WHERE fill_run_id = ?",
                                (fill_run_id,)).fetchone(), "review_json")


def authorization_for_grant(conn, grant_id: str) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT * FROM human_submit_authorizations WHERE grant_id = ?",
                                (grant_id,)).fetchone(), "review_json")


def authorizations_for_application(conn, application_workspace_id: str) -> list[dict[str, Any]]:
    return [_decode(r, "review_json") for r in conn.execute(
        "SELECT * FROM human_submit_authorizations WHERE application_workspace_id = ? ORDER BY seq",
        (application_workspace_id,))]


# ---- re-observation requests ----------------------------------------------------------

def insert_reobservation_request(conn, *, account_id: str, fill_run_id: str, now: datetime) -> dict[str, Any]:
    return _insert(conn, "submit_reobservation_requests", {
        "id": _id("sreq"), "account_id": account_id, "fill_run_id": fill_run_id, "requested_at": to_utc_iso(now)})


def latest_reobservation_request(conn, fill_run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM submit_reobservation_requests WHERE fill_run_id = ? ORDER BY seq DESC LIMIT 1",
                       (fill_run_id,)).fetchone()
    return dict(row) if row else None


# ---- observations ---------------------------------------------------------------------

def insert_submit_observation(conn, *, account_id: str, application_workspace_id: str, fill_run_id: str,
                              attempt_id: str | None, phase: str, structure_fingerprint: str,
                              observation_fingerprint: str, observation: dict[str, Any],
                              now: datetime) -> dict[str, Any]:
    return _decode(_insert(conn, "submit_observations", {
        "id": _id("sobs"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "fill_run_id": fill_run_id, "attempt_id": attempt_id, "phase": phase,
        "structure_fingerprint": structure_fingerprint, "observation_fingerprint": observation_fingerprint,
        "observation_json": _json(observation), "created_at": to_utc_iso(now)}), "observation_json")


def latest_submit_observation(conn, fill_run_id: str, phase: str) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT * FROM submit_observations WHERE fill_run_id = ? AND phase = ? "
                                "ORDER BY seq DESC LIMIT 1", (fill_run_id, phase)).fetchone(), "observation_json")


# ---- events and results ---------------------------------------------------------------

def append_submit_event(conn, *, authorization_id: str, attempt_id: str | None, event: str,
                        detail: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    return _decode(_insert(conn, "submit_events", {
        "id": _id("sev"), "authorization_id": authorization_id, "attempt_id": attempt_id, "event": event,
        "detail_json": _json(detail or {}), "created_at": to_utc_iso(now)}), "detail_json")


def submit_events(conn, authorization_id: str) -> list[dict[str, Any]]:
    return [_decode(r, "detail_json") for r in conn.execute(
        "SELECT * FROM submit_events WHERE authorization_id = ? ORDER BY seq", (authorization_id,))]


def insert_submission_result(conn, *, attempt_id: str, result: dict[str, Any], result_hash: str,
                             now: datetime) -> dict[str, Any]:
    return _decode(_insert(conn, "submission_results", {
        "id": _id("sres"), "attempt_id": attempt_id, "result_hash": result_hash, "result_json": _json(result),
        "created_at": to_utc_iso(now)}), "result_json")


def get_submission_result(conn, attempt_id: str) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT * FROM submission_results WHERE attempt_id = ?", (attempt_id,)).fetchone(),
                   "result_json")


# ---- attempts (6B tables, read-only view for 6E-A) ------------------------------------

def attempts_for_application(conn, application_workspace_id: str) -> list[dict[str, Any]]:
    """Every submission attempt for the application, oldest first, each with
    its latest attempt-event state."""
    return [dict(r) for r in conn.execute(
        "SELECT a.*, (SELECT e.state FROM submission_attempt_events e WHERE e.attempt_id = a.id "
        "ORDER BY e.seq DESC LIMIT 1) AS state FROM submission_attempts a "
        "WHERE a.application_workspace_id = ? ORDER BY a.seq", (application_workspace_id,))]
