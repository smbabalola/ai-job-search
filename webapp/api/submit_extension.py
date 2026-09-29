"""Bundle 6E-A extension routes (spec §18), under the handoff session token.

A run belongs to the handoff session that started it, and an attempt to
the run whose authorization issued its grant: anything else is 404.
SubmitRefused is 409 with the exact reason (not_found is 404); invalid
bodies and observations are 422. No body carries a cleartext value."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from webapp.api.dependencies import get_conn
from webapp.api.fill_extension import _owned_run
from webapp.api.handoff import get_session_scope
from webapp.persistence import submit as sp
from webapp.services import human_submit as hs
from webapp.services import submit_review as sr
from webapp.services.handoff import SessionScope

router = APIRouter(prefix="/api/handoff/sessions/{session_id}/fill", tags=["submit"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubmitObservationBody(_Body):
    phase: str
    attempt_id: str | None = None
    observation: dict[str, Any]


class PreClickBody(_Body):
    grant_id: str
    observation: dict[str, Any]
    verification: dict[str, Any]


class EventBody(_Body):
    event: str
    detail: dict[str, Any] = {}


class ResultBody(_Body):
    evidence: dict[str, Any]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def call(action: Callable[[], Any]) -> Any:
    try:
        return action()
    except sr.SubmitRefused as exc:
        raise HTTPException(status_code=404 if exc.reason == "not_found" else 409, detail=exc.reason) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _owned_attempt(conn, run: dict[str, Any], attempt_id: str) -> None:
    row = conn.execute("SELECT grant_id FROM submission_attempts WHERE id = ?", (attempt_id,)).fetchone()
    auth = sp.authorization_for_grant(conn, row["grant_id"]) if row else None
    if auth is None or auth["fill_run_id"] != run["id"]:
        raise HTTPException(status_code=404, detail="not found")


@router.post("/runs/{run_id}/submit/observations")
def post_submit_observation(session_id: str, run_id: str, body: SubmitObservationBody, request: Request,
                            conn: sqlite3.Connection = Depends(get_conn),
                            scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_run(conn, scope, session_id, run_id)
    if body.phase == "PRE_SUBMIT":  # PRE_SUBMIT arrives only with the pre-click proof
        raise HTTPException(status_code=422, detail="PRE_SUBMIT is posted with the pre-click")
    row = call(lambda: sr.record_submit_observation(conn, settings=request.app.state.settings, run_id=run_id, phase=body.phase,
                                                    attempt_id=body.attempt_id, observation=body.observation,
                                                    now=_now()))
    return {"observation_id": row["id"], "observation_fingerprint": row["observation_fingerprint"]}


@router.post("/runs/{run_id}/submit/pre-click")
def post_pre_click(session_id: str, run_id: str, body: PreClickBody, request: Request,
                   conn: sqlite3.Connection = Depends(get_conn),
                   scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_run(conn, scope, session_id, run_id)
    return call(lambda: hs.human_pre_click_commit(conn, settings=request.app.state.settings, run_id=run_id,
                                                  grant_id=body.grant_id, observation=body.observation,
                                                  verification=body.verification, now=_now()))


@router.post("/runs/{run_id}/submit/{attempt_id}/dispatch")
def post_dispatch(session_id: str, run_id: str, attempt_id: str, request: Request,
                  conn: sqlite3.Connection = Depends(get_conn),
                  scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_attempt(conn, _owned_run(conn, scope, session_id, run_id), attempt_id)
    return {"dispatched": call(lambda: hs.human_record_click_dispatched(
        conn, settings=request.app.state.settings, attempt_id=attempt_id, now=_now()))}


@router.post("/runs/{run_id}/submit/{attempt_id}/events")
def post_event(session_id: str, run_id: str, attempt_id: str, body: EventBody,
               conn: sqlite3.Connection = Depends(get_conn),
               scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_attempt(conn, _owned_run(conn, scope, session_id, run_id), attempt_id)
    row = call(lambda: hs.record_submit_event(conn, attempt_id=attempt_id, event=body.event, detail=body.detail,
                                              now=_now()))
    return {"event_id": row["id"]}


@router.post("/runs/{run_id}/submit/{attempt_id}/result")
def post_result(session_id: str, run_id: str, attempt_id: str, body: ResultBody, request: Request,
                conn: sqlite3.Connection = Depends(get_conn),
                scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_attempt(conn, _owned_run(conn, scope, session_id, run_id), attempt_id)
    return call(lambda: hs.report_result(conn, settings=request.app.state.settings, attempt_id=attempt_id,
                                         evidence=body.evidence, now=_now()))


@router.post("/runs/{run_id}/submit/{attempt_id}/cancel")
def post_cancel(session_id: str, run_id: str, attempt_id: str, conn: sqlite3.Connection = Depends(get_conn),
                scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    run = _owned_run(conn, scope, session_id, run_id)
    _owned_attempt(conn, run, attempt_id)
    return call(lambda: hs.cancel_attempt(conn, account_id=run["account_id"], attempt_id=attempt_id,
                                          actor="extension", now=_now()))
