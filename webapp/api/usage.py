"""``GET /api/usage``: the account's visible allowances (Bundle 7 spec §13.3)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request

from webapp.api.dependencies import get_conn, restricted_scope
from webapp.api.route_classes import USER
from webapp.persistence import dbapi
from webapp.services.ownership import AccountScope

router = APIRouter(dependencies=[Depends(USER)], prefix="/api", tags=["usage"])


@router.get("/usage")
def get_usage(request: Request, conn: dbapi.Connection = Depends(get_conn),
              scope: AccountScope = Depends(restricted_scope)) -> dict[str, Any]:
    metering = request.app.state.metering
    resolved = metering.gate.entitlements(conn, scope, now=datetime.now(timezone.utc))
    return {"plan_id": resolved.plan_id,
            "usage": metering.usage.summary(conn, scope, now=datetime.now(timezone.utc))}
