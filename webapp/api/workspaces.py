from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from product.autonomy_contract import Capability
from webapp.api.dependencies import get_account_scope, get_conn, get_extensions_dir
from webapp.services.ownership import AccountScope
from webapp.services.http_api import (
    JobWorkspaceNotFound,
    create_job_workspace,
    fit_job,
    generate_application_intelligence,
    get_job_workspace,
    list_job_workspaces,
    list_public_extensions,
    understand_job,
)
from webapp.services.autonomy_providers import request_providers
from webapp.services.autonomy_shadow import record_shadow_decision
from webapp.services.pipeline import PipelineError
from webapp.persistence import dbapi
from webapp.api.route_classes import USER

router = APIRouter(dependencies=[Depends(USER)], prefix="/api", tags=["workspaces"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateWorkspaceBody(StrictBody):
    company: str
    title: str
    source_record: dict[str, Any]
    # Server-trusted classification of which intake boundary built this
    # request — never inferred from source_record's own contents, so
    # normalize_job_source_record can safely grant a stronger source_url
    # provenance to "manual_entry"/"manual_paste" than to "imported_json"
    # without trusting anything the caller put inside source_record itself.
    source_record_origin: Literal["manual_entry", "manual_paste", "imported_json"]


class ProcessingBody(StrictBody):
    request_id: str


class FitBody(ProcessingBody):
    extension_ids: list[str] = Field(default_factory=list)


def _prepare_stage(request: Request, conn: dbapi.Connection, scope: AccountScope, workspace_id: str, stage: str,
                   work):
    """Bundle 7 §11.4: every AI prepare stage is gated (``ai.prepare``) and
    metered (``applications.prepare``), and runs at most once at a time per
    workspace (a duplicate in flight is ACTION_IN_PROGRESS). Ownership is
    checked first, so a foreign workspace is a 404 and never touches the ledger."""
    get_job_workspace(conn, workspace_id, account_id=scope.account_id)
    metering = request.app.state.metering
    if metering.enforced:  # Bundle 7 15.2: ready to prepare (after the entitlement check), before any charge
        from datetime import datetime, timezone
        from webapp.services.onboarding_v1 import require_prepare_ready
        metering.require_feature(conn, scope, "ai.prepare")
        require_prepare_ready(conn, scope, now=datetime.now(timezone.utc))
    return metering.prepare(conn, scope, workspace_id, work, stage=stage)


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, JobWorkspaceNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/extensions")
def get_extensions(extensions_dir: Path = Depends(get_extensions_dir)):
    try:
        return {"extensions": list_public_extensions(extensions_dir)}
    except PipelineError as exc:
        raise _service_error(exc) from exc


@router.post("/workspaces", status_code=201)
def post_workspace(
    body: CreateWorkspaceBody,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    try:
        return create_job_workspace(
            conn, company=body.company, title=body.title,
            source_record=body.source_record, account_id=scope.account_id,
            source_record_origin=body.source_record_origin,
        )
    except PipelineError as exc:
        raise _service_error(exc) from exc


@router.get("/workspaces")
def get_workspaces(
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    return {
        "workspaces": list_job_workspaces(conn, account_id=scope.account_id)
    }


@router.get("/workspaces/{workspace_id}")
def get_workspace_detail(
    workspace_id: str,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    try:
        return {
            "workspace": get_job_workspace(
                conn, workspace_id, account_id=scope.account_id
            )
        }
    except JobWorkspaceNotFound as exc:
        raise _service_error(exc) from exc


@router.post("/workspaces/{workspace_id}/understand")
def post_understand(
    workspace_id: str, body: ProcessingBody, request: Request,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    provider = _providers(request, scope, workspace_id).understanding
    try:
        artifact = _prepare_stage(request, conn, scope, workspace_id, "understand", lambda: understand_job(
            conn, workspace_id, provider, request_id=body.request_id,
            account_id=scope.account_id,
        ))
    except (PipelineError, JobWorkspaceNotFound) as exc:
        raise _service_error(exc) from exc
    _resolve_cv(request, conn, scope, workspace_id)
    return {"artifact": artifact}


def _resolve_cv(request: Request, conn: dbapi.Connection, scope: AccountScope, workspace_id: str) -> None:
    """Bundle 7 §14.3: which CV this application uses, recorded after understanding.
    Best effort: a resolution problem never fails the understanding stage."""
    from datetime import datetime, timezone
    from webapp.services import cv_strategy
    try:
        cv_strategy.resolve_for_workspace(conn, scope, workspace_id=workspace_id,
                                          metering=request.app.state.metering, now=datetime.now(timezone.utc))
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        import logging
        logging.getLogger("webapp.cv_strategy").exception("cv_resolution_failed workspace=%s", workspace_id)


def _fulfil_tailoring(request: Request, conn: dbapi.Connection, scope: AccountScope, workspace_id: str) -> None:
    """Bundle 7 §14.3 step 4: a pending tailoring runs after the intelligence stage (it needs the fit)."""
    from datetime import datetime, timezone
    from webapp.services import cv_strategy
    settings = request.app.state.settings

    def generate(conn, scope, *, workspace_id, base_version, template_id, documents_root):
        from webapp.services.application_documents import generate_application_documents
        generated = generate_application_documents(conn, workspace_id, documents_root=documents_root,
                                                   extensions_dir=settings.extensions_dir,
                                                   account_id=scope.account_id)
        return next(d["id"] for d in generated["documents"] if d["document_kind"] == "cv")
    try:  # best effort: the intelligence stage already succeeded; the pending tailoring stays visible
        cv_strategy.fulfil_tailoring(conn, scope, workspace_id=workspace_id, metering=request.app.state.metering,
                                     generator=generate, documents_root=settings.documents_root,
                                     now=datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001
        if conn.in_transaction:
            conn.rollback()
        import logging
        logging.getLogger("webapp.cv_strategy").exception("cv_tailoring_failed workspace=%s", workspace_id)


@router.post("/workspaces/{workspace_id}/fit")
def post_fit(
    workspace_id: str, body: FitBody, request: Request,
    conn: dbapi.Connection = Depends(get_conn),
    extensions_dir: Path = Depends(get_extensions_dir),
    scope: AccountScope = Depends(get_account_scope),
):
    try:
        artifact = _prepare_stage(request, conn, scope, workspace_id, "fit", lambda: fit_job(
            conn, workspace_id, _providers(request, scope, workspace_id).semantic_adapter,
            request_id=body.request_id,
            extension_ids=body.extension_ids, extensions_dir=extensions_dir,
            account_id=scope.account_id,
        ))
    except (PipelineError, JobWorkspaceNotFound) as exc:
        raise _service_error(exc) from exc
    record_shadow_decision(conn, settings=request.app.state.settings, account_id=scope.account_id,
                           workspace_id=workspace_id, stage=Capability.PREPARE)
    return {"artifact": artifact}


@router.post("/workspaces/{workspace_id}/application-intelligence")
def post_application_intelligence(
    workspace_id: str, body: ProcessingBody, request: Request,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    try:
        artifact = _prepare_stage(
            request, conn, scope, workspace_id, "application-intelligence", lambda: generate_application_intelligence(
                conn, workspace_id, _providers(request, scope, workspace_id).intelligence,
                request_id=body.request_id,
                account_id=scope.account_id,
            ))
    except (PipelineError, JobWorkspaceNotFound) as exc:
        raise _service_error(exc) from exc
    _fulfil_tailoring(request, conn, scope, workspace_id)
    _notify_prepared(conn, scope, workspace_id, artifact)
    return {"artifact": artifact}


def _notify_prepared(conn: dbapi.Connection, scope: AccountScope, workspace_id: str, artifact: Any) -> None:
    """Bundle 7 §17.2: the last prepare stage succeeded → prepared, and the pack is ready for review."""
    from datetime import datetime, timezone
    from webapp.services.notifications import notify
    artifact_id = artifact.get("id") if isinstance(artifact, dict) else None
    now = datetime.now(timezone.utc)
    for kind in ("application.prepared", "application.review_required"):
        notify(conn, account_id=scope.account_id, kind=kind, subject_type="workspace", subject_id=workspace_id,
               dedupe_key=f"{kind}:{workspace_id}:{artifact_id}", detail={"artifact_id": artifact_id}, now=now)
    conn.commit()


def _providers(request: Request, scope: AccountScope, workspace_id: str):
    return request_providers(request.app.state, scope, "workspace", workspace_id)
