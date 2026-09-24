"""HTTP adapters for CV Quality v2 (Phase 2B-5A).

Thin routes over the existing CV-v2 services. Every plan-specific route
operates on the exact ``statement_plan_artifact_id`` in its path -- never on a
"current" plan -- after proving that artifact belongs to the caller's own job
workspace. Mutating routes reuse the existing application-document
submission lock.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from product.cv_review_projection import CvReviewProjectionError
from product.cv_generation_basis_contract import CvGenerationBasisContractError
from webapp.api.dependencies import get_account_scope, get_conn, require_cv_quality_v2_enabled
from webapp.persistence.artifacts import get_artifact, list_artifact_history
from webapp.services.application_documents import _require_writable_workspace
from webapp.services.cv_generation_basis import build_and_persist_cv_generation_basis
from webapp.services.cv_generation_v2 import plan_and_persist_cv_generation_v2
from webapp.services.cv_statement_review import (
    list_cv_statement_review_items,
    resolve_cv_statement_review_state,
    save_cv_statement_review_decision,
)
from webapp.services.http_api import JobWorkspaceNotFound, require_job_workspace
from webapp.services.ownership import AccountScope
from webapp.services.pipeline import PipelineError

router = APIRouter(
    prefix="/api/workspaces/{workspace_id}/cv-v2", tags=["cv-generation-v2"],
    dependencies=[Depends(require_cv_quality_v2_enabled)],
)


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement_id: str
    disposition: str
    note: str | None = None


def _not_found(label: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{label} not found")


def require_owned_artifact(
    conn: sqlite3.Connection, workspace_id: str, artifact_id: str, *, artifact_type: str,
) -> dict[str, Any]:
    """Exact artifact belonging to this (already account-scoped) workspace, or 404.

    Never reveals whether an artifact with this ID exists elsewhere.
    """

    artifact = get_artifact(conn, artifact_id)
    if artifact is None or artifact["workspace_id"] != workspace_id or artifact["artifact_type"] != artifact_type:
        raise _not_found(artifact_type)
    return artifact


def _require_readable(conn: sqlite3.Connection, workspace_id: str, account_id: str) -> None:
    try:
        require_job_workspace(conn, workspace_id, account_id=account_id)
    except JobWorkspaceNotFound as exc:
        raise _not_found("job workspace") from exc


def _require_writable(conn: sqlite3.Connection, workspace_id: str, account_id: str) -> None:
    try:
        _require_writable_workspace(conn, workspace_id, account_id)
    except PipelineError as exc:
        if "not found" in str(exc):
            raise _not_found("job workspace") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def build_cv_v2_review_view(
    conn: sqlite3.Connection, workspace_id: str, plan_id: str,
) -> dict[str, Any]:
    """Exact review state for one pinned plan plus the bases built from it.

    Caller must already have proven ownership of ``plan_id``.
    """

    state = resolve_cv_statement_review_state(conn, workspace_id, plan_id)
    authorized, omitted = set(state["authorized"]), set(state["omitted"])
    items = [
        {
            **item,
            "decision": (
                "authorized" if item["statement_id"] in authorized
                else "omitted" if item["statement_id"] in omitted
                else None
            ),
        }
        for item in list_cv_statement_review_items(conn, plan_id)
    ]
    bases = [
        {
            "basis_artifact_id": basis["id"],
            "created_at": basis["created_at"],
            "authorized_statement_ids": basis["payload"]["review"]["authorized_statement_ids"],
            "omitted_statement_ids": basis["payload"]["review"]["omitted_statement_ids"],
        }
        for basis in list_artifact_history(conn, workspace_id, "cv_generation_basis")
        if basis["payload"].get("review", {}).get("statement_plan_artifact_id") == plan_id
    ]
    return {"statement_plan_artifact_id": plan_id, "items": items, "state": state, "bases": bases}


@router.post("/plans", status_code=201)
def post_plan(
    workspace_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    _require_writable(conn, workspace_id, scope.account_id)
    try:
        result = plan_and_persist_cv_generation_v2(conn, workspace_id, account_id=scope.account_id)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    plan_id = result["statement_plan_artifact"]["id"]
    return {
        "statement_plan_artifact_id": plan_id,
        "content_plan_artifact_id": result["content_plan_artifact"]["id"],
        "review_url": f"/workspaces/{workspace_id}/cv-v2/{plan_id}",
    }


@router.get("/plans/{plan_id}")
def get_plan(
    workspace_id: str, plan_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    _require_readable(conn, workspace_id, scope.account_id)
    require_owned_artifact(conn, workspace_id, plan_id, artifact_type="cv_statement_plan")
    try:
        return build_cv_v2_review_view(conn, workspace_id, plan_id)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/plans/{plan_id}/decisions", status_code=201)
def post_decision(
    workspace_id: str, plan_id: str, body: DecisionBody,
    conn: sqlite3.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    _require_writable(conn, workspace_id, scope.account_id)
    require_owned_artifact(conn, workspace_id, plan_id, artifact_type="cv_statement_plan")
    try:
        return save_cv_statement_review_decision(
            conn, workspace_id, statement_plan_artifact_id=plan_id,
            statement_id=body.statement_id, disposition=body.disposition, note=body.note,
        )
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/plans/{plan_id}/basis", status_code=201)
def post_basis(
    workspace_id: str, plan_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    _require_writable(conn, workspace_id, scope.account_id)
    require_owned_artifact(conn, workspace_id, plan_id, artifact_type="cv_statement_plan")
    try:
        result = build_and_persist_cv_generation_basis(
            conn, workspace_id, statement_plan_artifact_id=plan_id, account_id=scope.account_id,
        )
    except (PipelineError, CvReviewProjectionError, CvGenerationBasisContractError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"basis_artifact_id": result["basis_artifact"]["id"]}
