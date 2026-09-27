"""Bundle 6D-A human-facing pages (spec §6, §10, §16). The review page is the
only caller of record_presented: rendering the full review at an approvable
binding hash is what makes an application eligible for bulk approval."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from webapp.api.applications import prepared_applications
from webapp.api.dependencies import get_account_scope, get_conn
from webapp.api.review_approval import _now, review_payload
from webapp.services import review_approval
from webapp.services.ownership import AccountScope

router = APIRouter(tags=["review"])
PROVENANCE_WORDS = {
    "discovery_verified": "verified from discovery",
    "user_confirmed_apply_target": "you confirmed this exact URL",
    "user_supplied": "you supplied it; filling only",
    "imported_source": "imported with the job; filling only",
}


@router.get("/applications/prepared", response_class=HTMLResponse)
def prepared_page(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                  scope: AccountScope = Depends(get_account_scope)):
    applications = prepared_applications(conn, settings=request.app.state.settings, account_id=scope.account_id)
    return request.app.state.templates.TemplateResponse(request, "prepared_applications.html",
                                                        {"applications": applications})


@router.get("/workspaces/{workspace_id}/review", response_class=HTMLResponse)
def review_page(workspace_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)):
    settings = request.app.state.settings
    try:
        payload = review_payload(conn, settings=settings, account_id=scope.account_id, workspace_id=workspace_id)
        # The one place REVIEW_PRESENTED is recorded (never by the data API).
        presented = review_approval.record_presented(conn, settings=settings, account_id=scope.account_id,
                                                     application_workspace_id=workspace_id, actor=scope.account_id,
                                                     now=_now())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    from webapp.persistence.application_documents import get_selection
    revisions = {kind: (get_selection(conn, workspace_id, kind, account_id=scope.account_id) or {}).get("revision", 0)
                 for kind in ("cv", "cover_letter")}
    return request.app.state.templates.TemplateResponse(request, "review_application.html", {
        "ws": workspace_id, "review": payload, "displayed_binding_hash": presented, "revisions": revisions,
        "provenance_words": PROVENANCE_WORDS})
