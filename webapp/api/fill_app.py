"""Bundle 6D-B app routes (spec §20), under the user's session.

The fill-plan state GET is read-only and never carries a cleartext value
(the human-facing fill-plan page renders those server-side). Ownership
failures are 404, refusals are 409 with the reason, bodies forbid extra
keys. No route here submits or reaches any SUBMIT authority."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from webapp.api.dependencies import get_account_scope, get_conn
from webapp.persistence import fill as f
from webapp.persistence.workspaces import get_workspace
from webapp.services import fill_classification, fill_plans
from webapp.services.fill_results import fill_status
from webapp.services.fill_runs import FillRefused
from webapp.services.ownership import AccountScope
from webapp.services.review_application import ReviewRefused

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["fill"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MappingBody(_Body):
    observation_id: str
    page_field_key: str
    answer_key: str | None = None
    choice: str


class ConfirmBody(_Body):
    observation_id: str
    displayed_plan_hash: str


class ClassificationBody(_Body):
    displayed_proposal_id: str
    subject: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def call(action: Callable[[], Any]) -> Any:
    try:
        return action()
    except (ReviewRefused, FillRefused) as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc


def _owned(conn, workspace_id: str, scope: AccountScope) -> None:
    workspace = get_workspace(conn, workspace_id, account_id=scope.account_id)
    if workspace is None or workspace.get("kind") != "job":
        raise HTTPException(status_code=404, detail="not found")


def plan_state(conn, *, settings, account_id: str, workspace_id: str) -> dict[str, Any]:
    """Read only, one snapshot; rendered values are withheld (hashes only)."""
    now = _now()
    observation = f.latest_workspace_observation(conn, workspace_id, "INITIAL")
    view = None
    if observation is not None:
        view = fill_plans.fill_plan_presentation(conn, settings=settings, account_id=account_id,
                                                 application_workspace_id=workspace_id,
                                                 observation_id=observation["id"], now=now)
        view["rows"] = [{k: v for k, v in row.items() if k != "rendered_value"} for row in view["rows"]]
    status = fill_status(conn, settings=settings, account_id=account_id, application_workspace_id=workspace_id,
                         now=now)
    return {"observation_id": observation["id"] if observation else None, "plan": view, "fill_status": status}


@router.get("/fill-plan/state")
def get_plan_state(workspace_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                   scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _owned(conn, workspace_id, scope)
    return call(lambda: plan_state(conn, settings=request.app.state.settings, account_id=scope.account_id,
                                   workspace_id=workspace_id))


@router.post("/fill-plan/mappings")
def post_mapping(workspace_id: str, body: MappingBody, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _owned(conn, workspace_id, scope)
    return call(lambda: fill_plans.record_mapping_choice(
        conn, settings=request.app.state.settings, account_id=scope.account_id, application_workspace_id=workspace_id,
        observation_id=body.observation_id, page_field_key=body.page_field_key, answer_key=body.answer_key,
        choice=body.choice, actor=scope.account_id, now=_now()))


@router.post("/fill-plan/confirm")
def post_confirm(workspace_id: str, body: ConfirmBody, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _owned(conn, workspace_id, scope)
    return call(lambda: fill_plans.confirm_plan(
        conn, settings=request.app.state.settings, account_id=scope.account_id, application_workspace_id=workspace_id,
        observation_id=body.observation_id, displayed_plan_hash=body.displayed_plan_hash, actor=scope.account_id,
        now=_now()))


@router.post("/review/deltas/{delta_id}/classification/confirm")
def post_classification(workspace_id: str, delta_id: str, body: ClassificationBody,
                        conn: sqlite3.Connection = Depends(get_conn),
                        scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _owned(conn, workspace_id, scope)
    return call(lambda: fill_classification.confirm_classification(
        conn, account_id=scope.account_id, application_workspace_id=workspace_id, delta_id=delta_id,
        displayed_proposal_id=body.displayed_proposal_id, subject=body.subject, actor=scope.account_id, now=_now()))


def _run_summary(conn, run: dict[str, Any]) -> dict[str, Any]:
    last = f.run_state(conn, run["id"])
    return {"id": run["id"], "created_at": run["created_at"], "state": last["event"] if last else None,
            "reason": last["reason"] if last else None}


@router.get("/fill-runs")
def get_runs(workspace_id: str, conn: sqlite3.Connection = Depends(get_conn),
             scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _owned(conn, workspace_id, scope)
    runs = [r for r in f.runs_for_application(conn, workspace_id) if r["account_id"] == scope.account_id]
    return {"runs": [_run_summary(conn, r) for r in reversed(runs)]}


@router.get("/fill-runs/{run_id}")
def get_run(workspace_id: str, run_id: str, conn: sqlite3.Connection = Depends(get_conn),
            scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _owned(conn, workspace_id, scope)
    run = f.get_run(conn, run_id)
    if run is None or run["account_id"] != scope.account_id or run["application_workspace_id"] != workspace_id:
        raise HTTPException(status_code=404, detail="not found")
    result = f.get_result(conn, run_id)
    return {"run": {**run, **_run_summary(conn, run)}, "events": f.run_events(conn, run_id),
            "actions": f.action_events(conn, run_id), "quarantine": f.quarantine_events(conn, run_id),
            "detections": f.detection_events(conn, run_id), "grant_binding": f.get_grant_binding(conn, run_id),
            "result": result["result"] if result else None, "result_hash": result["result_hash"] if result else None}
