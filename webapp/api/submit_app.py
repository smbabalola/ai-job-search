"""Bundle 6E-A app routes (spec §18): the Submit Review state, the single
"Submit application" authorization, cancel, and ambiguity resolution.
Ownership failures are 404; SubmitRefused is 409 with the exact reason;
extra body keys are 422. The state carries hashes, never cleartext."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from webapp.api.dependencies import get_account_scope, get_conn
from webapp.persistence.workspaces import get_workspace
from webapp.services import human_submit as hs
from webapp.services import submit_review as sr
from webapp.services.ownership import AccountScope

router = APIRouter(prefix="/api/workspaces/{workspace_id}/submit", tags=["submit"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthorizeBody(_Body):
    review_hash: str


class CancelBody(_Body):
    authorization_id: str


class ResolveBody(_Body):
    submitted: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def call(action: Callable[[], Any]) -> Any:
    try:
        return action()
    except sr.SubmitRefused as exc:
        raise HTTPException(status_code=404 if exc.reason == "not_found" else 409, detail=exc.reason) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc


def _owned(conn, workspace_id: str, scope: AccountScope) -> None:
    workspace = get_workspace(conn, workspace_id, account_id=scope.account_id)
    if workspace is None or workspace.get("kind") != "job":
        raise HTTPException(status_code=404, detail="not found")


def submit_state(conn, *, settings, account_id: str, workspace_id: str, now: datetime) -> dict[str, Any]:
    """Read only: the current review (snapshot of hashes) and status."""
    return {"review": sr.current_review(conn, settings=settings, account_id=account_id,
                                        application_workspace_id=workspace_id, now=now),
            "status": hs.submission_status(conn, settings=settings, account_id=account_id,
                                           application_workspace_id=workspace_id, now=now)}


@router.get("/state")
def get_state(workspace_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
              scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _owned(conn, workspace_id, scope)
    return submit_state(conn, settings=request.app.state.settings, account_id=scope.account_id,
                        workspace_id=workspace_id, now=_now())


@router.post("/authorize", status_code=201)
def post_authorize(workspace_id: str, body: AuthorizeBody, request: Request,
                   conn: sqlite3.Connection = Depends(get_conn),
                   scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _owned(conn, workspace_id, scope)
    return call(lambda: hs.authorize(conn, settings=request.app.state.settings, account_id=scope.account_id,
                                     application_workspace_id=workspace_id, review_hash=body.review_hash,
                                     actor=scope.account_id, now=_now()))


@router.post("/cancel")
def post_cancel(workspace_id: str, body: CancelBody, conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _owned(conn, workspace_id, scope)
    return call(lambda: hs.cancel_authorization(conn, account_id=scope.account_id,
                                                authorization_id=body.authorization_id, actor=scope.account_id,
                                                now=_now()))


@router.post("/attempts/{attempt_id}/resolve")
def post_resolve(workspace_id: str, attempt_id: str, body: ResolveBody, conn: sqlite3.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _owned(conn, workspace_id, scope)
    return {"state": call(lambda: hs.resolve(conn, account_id=scope.account_id, attempt_id=attempt_id,
                                             submitted=body.submitted, actor=scope.account_id, now=_now()))}
