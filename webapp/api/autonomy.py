"""HTTP adapters for the Bundle 6B autonomy contract. Thin routes; every
state change is an explicit, attributed user action (actor = account id).

Deviations from the task-19 brief:
  1. Pause/resume accept only APPLICATION or SEARCH_WORKSPACE scopes, and the
     id must be the caller's own (404 otherwise). An ACCOUNT-scope pause is
     never read by the pause check, so accepting it would look effective
     while doing nothing; resume-all is the account-wide control.
  2. Apply-target confirmation is refused (409) when the application has no
     durable job identity: the confirmation is bound to the identity and
     could never upgrade provenance without one.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from product.autonomy_contract import Capability, Mode
from product.standing_policy import StandingPolicyError, evaluate_rules, policy_hash
from webapp.api.dependencies import get_account_scope, get_conn
from webapp.persistence.autonomy_answers import confirm_apply_target, record_rule_acknowledgement
from webapp.persistence.autonomy_authority import (
    current_capability, current_policy, kill_switch_state, save_policy_version,
)
from webapp.persistence.autonomy_ledger import workspace_identity
from webapp.persistence.workspaces import get_workspace
from webapp.services.autonomy_context import build_context, canonical_target_url
from webapp.services.autonomy_controls import (
    AutonomyHalted, enable_autonomous_preparation, engage_kill_switch, pause, release_kill_switch, resume,
    resume_all, save_standing_policy, sentinel_present, set_capability,
)
from webapp.services.autonomy_dossier import build_dossier
from webapp.services.ownership import AccountScope, OwnedResourceNotFound
from webapp.services.workspace_view import resolve_apply_target

router = APIRouter(tags=["autonomy"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimezoneBody(_Body):
    timezone: str


class CapabilityBody(_Body):
    scope_type: Literal["ACCOUNT_MAX", "DEFAULT_WORKSPACE_CEILING", "WORKSPACE_CEILING"]
    scope_id: str
    capability: Literal["NONE", "PREPARE", "FILL", "SUBMIT"]


class PolicyBody(_Body):
    doc: dict[str, Any]


class ReasonBody(_Body):
    reason: str


class ScopeBody(_Body):
    scope_type: Literal["APPLICATION", "SEARCH_WORKSPACE"]
    scope_id: str
    reason: str


class UrlBody(_Body):
    url: str


class AckBody(_Body):
    rule_id: str
    disposition: Literal["PROCEED", "DO_NOT_PROCEED"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_workspace(conn, workspace_id: str, account_id: str) -> dict[str, Any]:
    workspace = get_workspace(conn, workspace_id, account_id=account_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return workspace


@router.get("/api/autonomy")
def get_autonomy(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    settings = request.app.state.settings
    acct = scope.account_id
    policy = current_policy(conn, acct)
    cap = lambda st: (current_capability(conn, account_id=acct, scope_type=st, scope_id=acct) or Capability.NONE).name
    return {
        "account_max": cap("ACCOUNT_MAX"),
        "default_workspace_ceiling": cap("DEFAULT_WORKSPACE_CEILING"),
        "deployment_ceiling": settings.autonomy_deployment_ceiling().name,
        "kill_switch": kill_switch_state(conn, acct),
        "sentinel_path": str(settings.autonomy_sentinel_path),
        "sentinel_present": sentinel_present(settings.autonomy_sentinel_path),
        "policy": policy["doc"] if policy else None,
        "policy_hash": policy["policy_hash"] if policy else None,
        "scheduler_enabled": settings.autonomy_scheduler_enabled,
        "driver_running": bool(getattr(request.app.state, "autonomy_driver", {}).get("running")),
        "last_tick_at": getattr(request.app.state, "autonomy_driver", {}).get("last_tick_at"),
    }


@router.post("/api/autonomy/enable-preparation")
def post_enable(body: TimezoneBody, conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    try:
        return enable_autonomous_preparation(conn, account_id=scope.account_id, actor=scope.account_id,
                                             timezone=body.timezone, now=_now())
    except StandingPolicyError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc


@router.post("/api/autonomy/capability")
def post_capability(body: CapabilityBody, conn: sqlite3.Connection = Depends(get_conn),
                    scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    scope_id = body.scope_id
    if body.scope_type == "WORKSPACE_CEILING":
        try:
            scope.require_search_workspace(conn, scope_id)
        except OwnedResourceNotFound as exc:
            raise HTTPException(status_code=404, detail="search workspace not found") from exc
    else:
        scope_id = scope.account_id
    row = set_capability(conn, account_id=scope.account_id, scope_type=body.scope_type, scope_id=scope_id,
                         capability=Capability[body.capability], actor=scope.account_id, now=_now())
    return {"id": row["id"]}


@router.put("/api/autonomy/policy")
def put_policy(body: PolicyBody, conn: sqlite3.Connection = Depends(get_conn),
               scope: AccountScope = Depends(get_account_scope)):
    try:
        row = save_standing_policy(conn, account_id=scope.account_id, doc=body.doc, actor=scope.account_id,
                                  now=_now())
    except StandingPolicyError as exc:
        return JSONResponse(status_code=422, content={"errors": exc.errors})
    return {"policy_hash": row["policy_hash"]}


@router.post("/api/autonomy/kill-switch/engage")
def post_engage(body: ReasonBody, conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return engage_kill_switch(conn, account_id=scope.account_id, actor=scope.account_id, reason=body.reason, now=_now())


@router.post("/api/autonomy/kill-switch/release")
def post_release(body: ReasonBody, conn: sqlite3.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return release_kill_switch(conn, account_id=scope.account_id, actor=scope.account_id, reason=body.reason, now=_now())


@router.post("/api/autonomy/resume-all")
def post_resume_all(body: ReasonBody, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                    scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    try:
        return resume_all(conn, account_id=scope.account_id, actor=scope.account_id, reason=body.reason, now=_now(),
                          sentinel_path=request.app.state.settings.autonomy_sentinel_path)
    except AutonomyHalted as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _require_scope(conn, scope: AccountScope, body: ScopeBody) -> None:
    try:
        if body.scope_type == "APPLICATION":
            scope.require_job_workspace(conn, body.scope_id)
        else:
            scope.require_search_workspace(conn, body.scope_id)
    except OwnedResourceNotFound as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc


@router.post("/api/autonomy/pause")
def post_pause(body: ScopeBody, conn: sqlite3.Connection = Depends(get_conn),
               scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _require_scope(conn, scope, body)
    return pause(conn, account_id=scope.account_id, scope_type=body.scope_type, scope_id=body.scope_id,
                 actor=scope.account_id, reason=body.reason, now=_now())


@router.post("/api/autonomy/resume")
def post_resume(body: ScopeBody, conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _require_scope(conn, scope, body)
    return resume(conn, account_id=scope.account_id, scope_type=body.scope_type, scope_id=body.scope_id,
                  actor=scope.account_id, reason=body.reason, now=_now())


@router.post("/api/workspaces/{workspace_id}/autonomy/apply-target/confirm")
def post_confirm_target(workspace_id: str, body: UrlBody, conn: sqlite3.Connection = Depends(get_conn),
                        scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _require_workspace(conn, workspace_id, scope.account_id)
    target = resolve_apply_target(conn, workspace_id=workspace_id, account_id=scope.account_id)
    wanted = canonical_target_url(body.url)
    if target is None or wanted is None or wanted != canonical_target_url(target.url):
        raise HTTPException(status_code=409, detail="URL is not this application's resolved apply target")
    key, _, _ = workspace_identity(conn, workspace_id)
    if key is None:
        raise HTTPException(status_code=409, detail="this application has no durable job identity to bind the "
                                                    "confirmation to")
    row = confirm_apply_target(conn, application_workspace_id=workspace_id, job_identity_key=key,
                               canonical_url=wanted, confirmed_by=scope.account_id, now=_now())
    return {"id": row["id"]}


@router.post("/api/workspaces/{workspace_id}/autonomy/rule-acknowledgements")
def post_ack(workspace_id: str, body: AckBody, request: Request, conn: sqlite3.Connection = Depends(get_conn),
             scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _require_workspace(conn, workspace_id, scope.account_id)
    policy = current_policy(conn, scope.account_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="no standing policy")
    ctx = build_context(conn, settings=request.app.state.settings, account_id=scope.account_id,
                        application_workspace_id=workspace_id, requested_stage=Capability.PREPARE,
                        mode=Mode.SHADOW, now=_now(), sentinel_present=False)
    outcome = next((o for o in evaluate_rules(policy["doc"], ctx.attributes) if o.rule_id == body.rule_id), None)
    if outcome is None:
        raise HTTPException(status_code=404, detail="unknown rule")
    row = record_rule_acknowledgement(
        conn, account_id=scope.account_id, application_workspace_id=workspace_id, rule_id=body.rule_id,
        rule_hash=outcome.rule_hash, observed_fingerprint=outcome.observed_fingerprint,
        policy_version_hash=policy_hash(policy["doc"]), disposition=body.disposition,
        actor=scope.account_id, now=_now(),
    )
    return {"id": row["id"]}


@router.get("/api/workspaces/{workspace_id}/autonomy/dossier")
def get_dossier(workspace_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    try:
        return build_dossier(conn, account_id=scope.account_id, application_workspace_id=workspace_id,
                             settings=request.app.state.settings)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc


@router.get("/autonomy")
def autonomy_page(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                  scope: AccountScope = Depends(get_account_scope)):
    return request.app.state.templates.TemplateResponse(
        request, "autonomy.html", {"autonomy": get_autonomy(request, conn, scope)})


@router.get("/workspaces/{workspace_id}/autonomy")
def dossier_page(workspace_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)):
    return request.app.state.templates.TemplateResponse(
        request, "autonomy_dossier.html", {"dossier": get_dossier(workspace_id, request, conn, scope)})


# ---- Bundle 6C: inbox, enrolment, review latch, retry, candidate questions ----

class RetryBody(_Body):
    subject_type: Literal["APPLICATION", "CANDIDATE"]
    subject_id: str
    step_kind: Literal["EVALUATE", "UNDERSTAND", "FIT", "INTELLIGENCE", "SYSTEM_REVIEW", "GATE4"]


class ResolveBody(_Body):
    resolution: Literal["PROMOTE", "DISMISS"]
    reason: str | None = None


def _inbox(conn, account_id: str) -> dict[str, Any]:
    from webapp.services.autonomy_inbox import build_inbox, mark_inbox_seen
    view = build_inbox(conn, account_id=account_id)
    mark_inbox_seen(conn, account_id=account_id, now=_now())  # viewing is SEEN, never RESOLVED
    return view


@router.get("/api/autonomy/inbox")
def get_inbox(conn: sqlite3.Connection = Depends(get_conn),
              scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return _inbox(conn, scope.account_id)


@router.get("/api/autonomy/inbox/summary")
def get_inbox_summary(conn: sqlite3.Connection = Depends(get_conn),
                      scope: AccountScope = Depends(get_account_scope)) -> dict[str, int]:
    from webapp.services.autonomy_inbox import inbox_summary
    return inbox_summary(conn, scope.account_id)


@router.get("/autonomy/inbox")
def inbox_page(request: Request, conn: sqlite3.Connection = Depends(get_conn),
               scope: AccountScope = Depends(get_account_scope)):
    return request.app.state.templates.TemplateResponse(
        request, "autonomy_inbox.html", {"inbox": _inbox(conn, scope.account_id)})


@router.post("/api/workspaces/{workspace_id}/autonomy/enrol")
def post_enrol(workspace_id: str, conn: sqlite3.Connection = Depends(get_conn),
               scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    from webapp.services.autonomy_prepare import enrol
    try:
        enrol(conn, account_id=scope.account_id, application_workspace_id=workspace_id, actor=scope.account_id,
              now=_now())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    return {"enrolled": True}


@router.post("/api/workspaces/{workspace_id}/autonomy/unenrol")
def post_unenrol(workspace_id: str, conn: sqlite3.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    from webapp.services.autonomy_prepare import unenrol
    try:
        unenrol(conn, account_id=scope.account_id, application_workspace_id=workspace_id, actor=scope.account_id,
                now=_now())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    return {"enrolled": False}


@router.post("/api/workspaces/{workspace_id}/autonomy/review-pack")
def post_review_pack(workspace_id: str, conn: sqlite3.Connection = Depends(get_conn),
                     scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    from webapp.services.autonomy_prepare import request_pack_review
    try:
        revision = request_pack_review(conn, account_id=scope.account_id, application_workspace_id=workspace_id,
                                       actor=scope.account_id, now=_now())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"latched_revision": revision}


@router.post("/api/autonomy/retry")
def post_retry(body: RetryBody, conn: sqlite3.Connection = Depends(get_conn),
               scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    from webapp.services.autonomy_inbox import RetryNotEligible, retry_failure
    if body.subject_type == "APPLICATION":
        _require_workspace(conn, body.subject_id, scope.account_id)
    elif conn.execute("SELECT 1 FROM autonomy_candidate_queue WHERE candidate_id = ? AND account_id = ?",
                      (body.subject_id, scope.account_id)).fetchone() is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    try:
        request = retry_failure(conn, account_id=scope.account_id, subject_type=body.subject_type,
                                subject_id=body.subject_id, step_kind=body.step_kind, actor=scope.account_id,
                                now=_now())
    except RetryNotEligible as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"retry_request_id": request["id"]}


@router.post("/api/autonomy/candidate-exceptions/{exception_id}/resolve")
def post_resolve_candidate(exception_id: str, body: ResolveBody, request: Request,
                           conn: sqlite3.Connection = Depends(get_conn),
                           scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    from webapp.persistence.autonomy_prepare import get_candidate_exception
    from webapp.services.autonomy_candidates import CandidatePromotionRefused, resolve_candidate_question
    exception = get_candidate_exception(conn, exception_id)
    if exception is None or exception["account_id"] != scope.account_id:
        raise HTTPException(status_code=404, detail="question not found")
    try:
        out = resolve_candidate_question(conn, settings=request.app.state.settings, exception_id=exception_id,
                                         resolution=body.resolution, actor=scope.account_id, reason=body.reason,
                                         now=_now())
    except CandidatePromotionRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="candidate not found") from exc
    return {"resolution": body.resolution, "application_workspace_id": out.get("application_workspace_id")}
