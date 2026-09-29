"""Bundle 6D-B extension routes (spec §20), under the handoff session token.

A run belongs to the handoff session that started it: a foreign session, or
a run of another session/account, is 404. FillRefused/ReviewRefused are 409
with the exact reason; invalid bodies and observations are 422. The intent
response is the only body that may carry a cleartext value. No route
here submits, lifts a quarantine or accepts a SUBMIT stage (6E-A's submit routes live in
webapp/api/submit_extension.py)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from webapp.api.dependencies import get_conn
from webapp.api.handoff import get_session_scope
from webapp.persistence import fill as f
from webapp.services import fill_actions, fill_runs
from webapp.services.fill_results import fill_status, run_plan_hash
from webapp.services.handoff import SessionScope
from webapp.services.review_application import ReviewRefused

router = APIRouter(prefix="/api/handoff/sessions/{session_id}/fill", tags=["fill"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartBody(_Body):
    executor_instance_id: str
    browser_session_id: str
    execution_tab_id: int


class ObservationBody(_Body):
    phase: str
    action_index: int | None = None
    observation: dict[str, Any]


class QuarantineBody(_Body):
    phase: str
    ruleset_hash: str | None = None
    action_index: int | None = None


class IntentBody(_Body):
    precheck: dict[str, Any]


class OutcomeBody(_Body):
    envelope_id: str | None = None
    outcome: str
    readback_hash: str | None = None
    post_observation: dict[str, Any]


class DetectionBody(_Body):
    kind: str
    detail: dict[str, Any] = {}


class FinalBody(_Body):
    observation: dict[str, Any]


class StopBody(_Body):
    reason: str
    detail: dict[str, Any] = {}


_ACTION_KEYS = ("page_field_key", "field_fingerprint", "action_kind", "rendered_value_hash", "document",
                "document_kind")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def call(action: Callable[[], Any]) -> Any:
    try:
        return action()
    except (fill_runs.FillRefused, ReviewRefused) as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _scope(session_id: str, scope: SessionScope) -> SessionScope:
    if scope.handoff_session_id != session_id:
        raise HTTPException(status_code=404, detail="not found")
    return scope


def _owned_run(conn, scope: SessionScope, session_id: str, run_id: str) -> dict[str, Any]:
    _scope(session_id, scope)
    run = f.get_run(conn, run_id)
    if run is None or run["account_id"] != scope.account_id or run["handoff_session_id"] != scope.handoff_session_id:
        raise HTTPException(status_code=404, detail="not found")
    return run


@router.post("/runs", status_code=201)
def post_run(session_id: str, body: StartBody, request: Request, conn: sqlite3.Connection = Depends(get_conn),
             scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _scope(session_id, scope)
    return call(lambda: fill_runs.start_run(
        conn, settings=request.app.state.settings, account_id=scope.account_id,
        handoff_session_id=scope.handoff_session_id, application_workspace_id=scope.workspace_id,
        executor_instance_id=body.executor_instance_id, browser_session_id=body.browser_session_id,
        execution_tab_id=body.execution_tab_id, now=_now()))


@router.post("/runs/{run_id}/observations")
def post_observation(session_id: str, run_id: str, body: ObservationBody, request: Request,
                     conn: sqlite3.Connection = Depends(get_conn),
                     scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_run(conn, scope, session_id, run_id)
    return call(lambda: fill_runs.record_observation(
        conn, settings=request.app.state.settings, run_id=run_id, phase=body.phase, action_index=body.action_index,
        observation=body.observation, now=_now()))


@router.get("/runs/{run_id}/plan-status")
def get_plan_status(session_id: str, run_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                    scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    run = _owned_run(conn, scope, session_id, run_id)
    last = f.run_state(conn, run_id)
    binding = f.get_grant_binding(conn, run_id)
    plan_hash = binding["plan_hash"] if binding else run_plan_hash(conn, run_id)
    plan = f.get_plan_by_hash(conn, plan_hash)["plan"] if plan_hash else None
    status = call(lambda: fill_status(conn, settings=request.app.state.settings, account_id=scope.account_id,
                                      application_workspace_id=run["application_workspace_id"], now=_now()))
    return {"run_id": run_id, "state": last["event"], "reason": last["reason"], "detail": last["detail"],
            "grant_id": binding["grant_id"] if binding else None,
            "ruleset_hash": binding["ruleset_hash"] if binding else None, "fill_status": status,
            # The plan carries references and hashes only (no cleartext): the
            # executor's local pre-check compares the page against it.
            "plan_hash": plan_hash, "structure_fingerprint": plan["structure_fingerprint"] if plan else None,
            "actions": [{k: a[k] for k in _ACTION_KEYS} for a in plan["actions"]] if plan else []}


@router.post("/runs/{run_id}/quarantine")
def post_quarantine(session_id: str, run_id: str, body: QuarantineBody, conn: sqlite3.Connection = Depends(get_conn),
                    scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_run(conn, scope, session_id, run_id)
    return call(lambda: fill_runs.record_quarantine(conn, run_id=run_id, phase=body.phase,
                                                    ruleset_hash=body.ruleset_hash, action_index=body.action_index,
                                                    now=_now()))


@router.post("/runs/{run_id}/grant")
def post_grant(session_id: str, run_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
               scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_run(conn, scope, session_id, run_id)
    out = call(lambda: fill_runs.request_fill_grant(conn, settings=request.app.state.settings, run_id=run_id,
                                                    now=_now()))
    return {"granted": out.granted, "grant_id": out.grant_id, "stop_reason": out.stop_reason, "detail": out.detail}


@router.post("/runs/{run_id}/actions/{action_index}/intent")
def post_intent(session_id: str, run_id: str, action_index: int, body: IntentBody, request: Request,
                conn: sqlite3.Connection = Depends(get_conn),
                scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_run(conn, scope, session_id, run_id)
    out = call(lambda: fill_actions.request_intent(conn, settings=request.app.state.settings, run_id=run_id,
                                                   action_index=action_index, precheck=body.precheck, now=_now()))
    envelope = out.envelope
    return {"envelope": None if envelope is None else {
        "envelope_id": envelope.envelope_id, "action_index": envelope.action_index,
        "action_kind": envelope.action_kind, "rendered_value": envelope.rendered_value,
        "rendered_value_hash": envelope.rendered_value_hash, "document": envelope.document,
        "expires_at": envelope.expires_at}, "stop_reason": out.stop_reason, "detail": out.detail}


@router.post("/runs/{run_id}/actions/{action_index}/outcome")
def post_outcome(session_id: str, run_id: str, action_index: int, body: OutcomeBody,
                 conn: sqlite3.Connection = Depends(get_conn),
                 scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_run(conn, scope, session_id, run_id)
    return call(lambda: fill_actions.record_outcome(
        conn, run_id=run_id, action_index=action_index, envelope_id=body.envelope_id, outcome=body.outcome,
        readback_hash=body.readback_hash, post_observation=body.post_observation, now=_now()))


@router.post("/runs/{run_id}/actions/{action_index}/unknown")
def post_unknown(session_id: str, run_id: str, action_index: int, conn: sqlite3.Connection = Depends(get_conn),
                 scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_run(conn, scope, session_id, run_id)
    return call(lambda: fill_actions.record_unknown_outcome(conn, run_id=run_id, action_index=action_index,
                                                            now=_now()))


@router.post("/runs/{run_id}/heartbeat")
def post_heartbeat(session_id: str, run_id: str, conn: sqlite3.Connection = Depends(get_conn),
                   scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_run(conn, scope, session_id, run_id)
    now = _now()
    out = call(lambda: fill_runs.heartbeat(conn, run_id=run_id, now=now))
    # 6E-A (spec E12): additive directives -- a pending Submit Review
    # re-observation request, and an issued human SUBMIT authorization.
    from webapp.services.human_submit import pending_authorization
    from webapp.services.submit_review import pending_reobservation
    request = pending_reobservation(conn, run_id)
    out["reobserve"] = {"request_id": request["id"]} if request else None
    out["authorization"] = pending_authorization(conn, run_id, now)
    return out


@router.post("/runs/{run_id}/detections")
def post_detection(session_id: str, run_id: str, body: DetectionBody, conn: sqlite3.Connection = Depends(get_conn),
                   scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_run(conn, scope, session_id, run_id)
    return call(lambda: fill_actions.record_detection(conn, run_id=run_id, kind=body.kind, detail=body.detail,
                                                      now=_now()))


@router.post("/runs/{run_id}/final")
def post_final(session_id: str, run_id: str, body: FinalBody, conn: sqlite3.Connection = Depends(get_conn),
               scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    _owned_run(conn, scope, session_id, run_id)
    return call(lambda: fill_actions.final_validate(conn, run_id=run_id, observation=body.observation, now=_now()))


@router.post("/runs/{run_id}/stop")
def post_stop(session_id: str, run_id: str, body: StopBody, conn: sqlite3.Connection = Depends(get_conn),
              scope: SessionScope = Depends(get_session_scope)) -> dict[str, Any]:
    """Reduce-only: the executor may end its own run for a condition only it
    can observe (fill_runs.EXECUTOR_STOP_REASONS), never extend it."""
    _owned_run(conn, scope, session_id, run_id)
    return call(lambda: fill_runs.executor_stop(conn, run_id=run_id, reason=body.reason, detail=body.detail,
                                                now=_now()))
