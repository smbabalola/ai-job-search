"""Scheduled saved searches (Bundle 7 spec §20.3). Enabling is gated
(``discovery.scheduled`` plus the ``discovery.scheduled_searches`` gauge);
reading and disabling never are."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from webapp.api.dependencies import get_account_scope, get_conn
from webapp.api.discovery import _authorize_search_workspace
from webapp.api.route_classes import USER
from webapp.persistence import dbapi
from webapp.services import search_schedules as schedules
from webapp.services.ownership import AccountScope

router = APIRouter(dependencies=[Depends(USER)], tags=["discovery"])


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cadence: Literal["DAILY", "WEEKLY"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/api/search-workspaces/{search_workspace_id}/schedule")
def get_schedule(search_workspace_id: str, conn: dbapi.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)):
    _authorize_search_workspace(scope, conn, search_workspace_id)
    return {"schedule": schedules.get_schedule(conn, search_workspace_id)}


@router.post("/api/search-workspaces/{search_workspace_id}/schedule")
def post_schedule(search_workspace_id: str, body: ScheduleBody, request: Request,
                  conn: dbapi.Connection = Depends(get_conn), scope: AccountScope = Depends(get_account_scope)):
    _authorize_search_workspace(scope, conn, search_workspace_id)
    try:
        schedule = schedules.enable_schedule(conn, scope, search_workspace_id=search_workspace_id,
                                             cadence=body.cadence, now=_now(), metering=request.app.state.metering)
    except schedules.ScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"schedule": schedule}


@router.post("/api/search-workspaces/{search_workspace_id}/schedule/disable")
def disable_schedule(search_workspace_id: str, conn: dbapi.Connection = Depends(get_conn),
                     scope: AccountScope = Depends(get_account_scope)):
    _authorize_search_workspace(scope, conn, search_workspace_id)
    return {"schedule": schedules.disable_schedule(conn, scope, search_workspace_id=search_workspace_id,
                                                   now=_now())}
