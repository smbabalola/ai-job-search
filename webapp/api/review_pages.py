"""Bundle 6D-A human-facing pages (spec §6, §10, §16). The review page is the
only caller of record_presented: rendering the full review at an approvable
binding hash is what makes an application eligible for bulk approval."""
from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from webapp.api.applications import prepared_applications
from webapp.api.dependencies import get_account_scope, get_conn
from webapp.api.review_approval import _now, review_payload
from product.review_contract import FIELD_DELTA_KINDS
from webapp.persistence import fill as fill_store
from webapp.persistence import review_approval as ra
from webapp.persistence.workspaces import get_workspace
from webapp.services import fill_plans, review_approval
from webapp.services.fill_results import fill_summary
from webapp.services import submit_review
from webapp.services.human_submit import submission_status
from webapp.services.ownership import AccountScope
from webapp.persistence import dbapi

router = APIRouter(tags=["review"])
PROVENANCE_WORDS = {
    "discovery_verified": "verified from discovery",
    "user_confirmed_apply_target": "you confirmed this exact URL",
    "user_supplied": "you supplied it; filling only",
    "imported_source": "imported with the job; filling only",
}


@router.get("/applications/prepared", response_class=HTMLResponse)
def prepared_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
                  scope: AccountScope = Depends(get_account_scope)):
    applications = prepared_applications(conn, settings=request.app.state.settings, account_id=scope.account_id)
    return request.app.state.templates.TemplateResponse(request, "prepared_applications.html",
                                                        {"applications": applications})


@router.get("/workspaces/{workspace_id}/review", response_class=HTMLResponse)
def review_page(workspace_id: str, request: Request, conn: dbapi.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)):
    settings = request.app.state.settings
    try:
        payload = review_payload(conn, settings=settings, account_id=scope.account_id, workspace_id=workspace_id)
        # The one place REVIEW_PRESENTED is recorded (never by the data API).
        # Only the hash of the content rendered below may become approvable.
        presented = review_approval.record_presented(conn, settings=settings, account_id=scope.account_id,
                                                     application_workspace_id=workspace_id,
                                                     expected_binding_hash=payload["binding_hash"],
                                                     actor=scope.account_id, now=_now())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    # Document controls carry the revisions of the rendered snapshot, never a later read.
    return request.app.state.templates.TemplateResponse(request, "review_application.html", {
        "ws": workspace_id, "review": payload, "displayed_binding_hash": presented,
        "revisions": payload["selection_revisions"],
        "classification_proposals": classification_proposals(conn, workspace_id),
        "provenance_words": PROVENANCE_WORDS})


def classification_proposals(conn, workspace_id: str) -> list[dict]:
    """6D-B R7 (spec §9): each open, unclassified field delta with its latest
    (non-authoritative) proposal; the user's Confirm carries its id."""
    out = []
    for delta in ra.open_deltas(conn, workspace_id):
        if delta["kind"] not in FIELD_DELTA_KINDS or delta["subject"] is not None:
            continue
        proposal = fill_store.latest_proposal(conn, delta["id"])
        out.append({"delta_id": delta["id"], "kind": delta["kind"], "question": delta["question"],
                    "required": bool(delta["required"]), "proposal": proposal})
    return out


@router.get("/workspaces/{workspace_id}/fill-plan", response_class=HTMLResponse)
def fill_plan_page(workspace_id: str, request: Request, conn: dbapi.Connection = Depends(get_conn),
                   scope: AccountScope = Depends(get_account_scope)):
    """Rendered from ONE snapshot (fill_plan_presentation): the WRITE rows'
    cleartext and the displayed plan hash come from the same read."""
    workspace = get_workspace(conn, workspace_id, account_id=scope.account_id)
    if workspace is None or workspace.get("kind") != "job":
        raise HTTPException(status_code=404, detail="not found")
    settings = request.app.state.settings
    observation = fill_store.latest_workspace_observation(conn, workspace_id, "INITIAL")
    view = None
    if observation is not None:
        try:
            view = fill_plans.fill_plan_presentation(conn, settings=settings, account_id=scope.account_id,
                                                     application_workspace_id=workspace_id,
                                                     observation_id=observation["id"], now=_now())
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="not found") from exc
    status = fill_summary(conn, settings=settings, account_id=scope.account_id, application_workspace_id=workspace_id,
                          now=_now())
    return request.app.state.templates.TemplateResponse(request, "fill_plan.html", {
        "ws": workspace_id, "workspace": workspace, "observation_id": observation["id"] if observation else None,
        "plan": view, "fill_status": status})


SUBMIT_REASON_WORDS = {
    "no_filled_run": "Fill the application in the employer tab first.",
    "lease_expired": "The employer tab is no longer connected. Open the job page again and fill it.",
    "already_authorized": "This filled version has already been authorized.",
    "post_fill_change": "The employer page changed after filling. Close that tab and fill again in a fresh one.",
    "observation_missing": "Couldn't read the employer tab. Keep it open and reload this page.",
    "observation_stale": "Couldn't read the employer tab. Keep it open and reload this page.",
    "submit_control_not_unique": "The employer page's submit button couldn't be identified exactly.",
    "page_changed_since_fill": "The employer page no longer matches what was filled.",
    "approval_not_effective": "The application's approval is no longer effective.",
    "plan_mismatch": "The fill plan changed since it was filled.",
    "adapter_not_submit_certified": "Submission isn't supported for this site.",
    "adapter_not_live_certified": "Live submission for Greenhouse isn't certified yet.",
    "human_submit_disabled": "Submission is turned off for this deployment.",
    "kill_switch": "Automation is halted (kill switch).",
    "duplicate": "This job has already been submitted, or is being submitted.",
    "paused": "Automation is paused for this application.",
    "stale_binding": "Something changed since this review; reload the page.",
}


@router.get("/workspaces/{workspace_id}/submit", response_class=HTMLResponse)
def submit_review_page(workspace_id: str, request: Request, observed: int = 0,
                       conn: dbapi.Connection = Depends(get_conn),
                       scope: AccountScope = Depends(get_account_scope)):
    """6E-A Submit Review (spec §16.1). The first load asks the extension for
    a fresh look at the employer tab and waits for it; the page then renders
    ONE snapshot and its review hash, which the single "Submit application"
    act carries. The exact rendered values come from the fill-plan
    presentation only when its plan hash equals the snapshot's."""
    workspace = get_workspace(conn, workspace_id, account_id=scope.account_id)
    if workspace is None or workspace.get("kind") != "job":
        raise HTTPException(status_code=404, detail="not found")
    settings = request.app.state.settings
    now = _now()
    if not observed:
        submit_review.request_reobservation(conn, account_id=scope.account_id, application_workspace_id=workspace_id,
                                            now=now)
    review = submit_review.current_review(conn, settings=settings, account_id=scope.account_id,
                                          application_workspace_id=workspace_id, now=now)
    status = submission_status(conn, settings=settings, account_id=scope.account_id,
                               application_workspace_id=workspace_id, now=now)
    rows = []
    snapshot = review["snapshot"]
    observation = fill_store.latest_workspace_observation(conn, workspace_id, "INITIAL")
    if snapshot is not None and observation is not None:
        view = fill_plans.fill_plan_presentation(conn, settings=settings, account_id=scope.account_id,
                                                 application_workspace_id=workspace_id,
                                                 observation_id=observation["id"], now=now)
        if view.get("displayed_plan_hash") == snapshot["fill"]["plan_hash"]:
            rows = view["rows"]
    reasons = [SUBMIT_REASON_WORDS.get(r, r.replace("_", " ")) for r in review["reasons"]]
    return request.app.state.templates.TemplateResponse(request, "submit_review.html", {
        "ws": workspace_id, "workspace": workspace, "review": review, "reasons": reasons, "rows": rows,
        "status": status})
