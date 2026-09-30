"""Billing routes (Bundle 7 spec §12.3, §21.2). Checkout and plan changes need an
active account; status, the portal and cancelling are never gated (§11.5)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from webapp.api.dependencies import get_account_scope, get_conn, restricted_scope
from webapp.api.route_classes import USER
from webapp.persistence import dbapi
from webapp.services.billing import BillingService
from webapp.services.ownership import AccountScope

router = APIRouter()


class PlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    interval: str = "month"


def _service(request: Request) -> BillingService:
    base: BillingService = request.app.state.billing_service
    return BillingService(base.provider, base.catalog, settings=base.settings,
                          request_id=getattr(request.state, "request_id", None))


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/api/billing/checkout", status_code=201, dependencies=[Depends(USER)])
def checkout(body: PlanBody, request: Request, scope: AccountScope = Depends(get_account_scope),
             conn: dbapi.Connection = Depends(get_conn)) -> dict[str, Any]:
    url = _service(request).start_checkout(conn, scope, plan_id=body.plan_id, interval=body.interval, now=_now())
    conn.commit()
    return {"url": url}


@router.post("/api/billing/change-plan", dependencies=[Depends(USER)])
def change_plan(body: PlanBody, request: Request, scope: AccountScope = Depends(get_account_scope),
                conn: dbapi.Connection = Depends(get_conn)) -> dict[str, Any]:
    return _service(request).change_plan(conn, scope, plan_id=body.plan_id, interval=body.interval, now=_now())


@router.post("/api/billing/portal", dependencies=[Depends(USER)])
def portal(request: Request, scope: AccountScope = Depends(restricted_scope),
           conn: dbapi.Connection = Depends(get_conn)) -> dict[str, Any]:
    return {"url": _service(request).portal_url(conn, scope, now=_now())}


@router.post("/api/billing/cancel", dependencies=[Depends(USER)])
def cancel(request: Request, scope: AccountScope = Depends(restricted_scope),
           conn: dbapi.Connection = Depends(get_conn)) -> dict[str, Any]:
    return _service(request).cancel(conn, scope, now=_now())


@router.post("/api/billing/resume", dependencies=[Depends(USER)])
def resume(request: Request, scope: AccountScope = Depends(restricted_scope),
           conn: dbapi.Connection = Depends(get_conn)) -> dict[str, Any]:
    return {"url": _service(request).resume(conn, scope, now=_now())}


@router.get("/api/billing/status", dependencies=[Depends(USER)])
def status(request: Request, scope: AccountScope = Depends(restricted_scope),
           conn: dbapi.Connection = Depends(get_conn)) -> dict[str, Any]:
    return _service(request).status(conn, scope)
