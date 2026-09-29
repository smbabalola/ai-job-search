"""Bundle 6D-A Prepared Applications routes (spec §10, §16)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from product.review_contract import WarningLevel, derive_review_state
from webapp.api.dependencies import get_account_scope, get_conn
from webapp.api.review_approval import _now
from webapp.persistence import review_approval as ra
from webapp.services import review_approval
from webapp.services.fill_results import fill_summary
from webapp.services.human_submit import submission_status
from webapp.services.ownership import AccountScope
from webapp.services.review_application import review_snapshot
from webapp.persistence import dbapi

router = APIRouter(prefix="/api/applications", tags=["review"])
LISTED = ("READY_FOR_REVIEW", "NEEDS_REVIEW", "APPROVED_FOR_FILL")


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str
    displayed_binding_hash: str


class ApproveSelectedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[_Item]


def prepared_applications(conn, *, settings, account_id: str) -> list[dict[str, Any]]:
    now = _now()
    out = []
    rows = conn.execute("SELECT id, company, title FROM workspaces WHERE account_id = ? AND kind = 'job' "
                        "ORDER BY rowid", (account_id,)).fetchall()
    for row in rows:
        snapshot = review_snapshot(conn, settings=settings, account_id=account_id, application_workspace_id=row["id"],
                                   now=now)
        state = derive_review_state(snapshot)
        if state.state not in LISTED:
            continue
        warnings = snapshot.reviewable.warnings if snapshot.reviewable else ()
        out.append({
            "workspace_id": row["id"], "company": row["company"], "title": row["title"], "state": state.state,
            "reasons": list(state.reasons), "blocking_count": len(state.blocking),
            "attention_count": sum(1 for w in warnings if w.level is WarningLevel.ATTENTION and not w.acknowledged),
            "binding_hash": state.binding_hash,
            "presented_at_current_hash": bool(state.binding_hash and ra.presented_at(conn, row["id"],
                                                                                    state.binding_hash)),
            # 6D-B: fill status next to the unchanged 6D-A review state (spec §16.2)
            "fill_status": fill_summary(conn, settings=settings, account_id=account_id,
                                        application_workspace_id=row["id"], now=now),
            # 6E-A: submission status next to the fill status (spec §16.2)
            "submission_status": submission_status(conn, settings=settings, account_id=account_id,
                                                   application_workspace_id=row["id"], now=now),
        })
    return out


@router.get("/prepared")
def get_prepared(request: Request, conn: dbapi.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return {"applications": prepared_applications(conn, settings=request.app.state.settings,
                                                  account_id=scope.account_id)}


@router.post("/approve-selected")
def post_approve_selected(body: ApproveSelectedBody, request: Request, conn: dbapi.Connection = Depends(get_conn),
                          scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    results = review_approval.approve_selected(
        conn, settings=request.app.state.settings, account_id=scope.account_id,
        items=[item.model_dump() for item in body.items], actor=scope.account_id, now=_now())
    return {"results": results}
