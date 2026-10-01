from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from product.discovery_search import CliDiscoveryPortalRunner, available_discovery_source_ids
from webapp.api.dependencies import get_account_scope, get_conn, get_extensions_dir
from webapp.persistence.discovery import set_discovery_candidate_status
from webapp.persistence.discovery_sources import list_discovery_source_settings, list_enabled_discovery_source_ids
from webapp.services.discovery import (
    DiscoveryServiceError,
    evaluate_discovery_candidate,
    grouped_discovery_candidates,
    promote_discovery_candidate,
    run_discovery_search,
)
from webapp.services.autonomy_providers import request_providers
from webapp.services.extension_registry import resolve_active_extensions
from webapp.services.metered_provider import reraise_metering_refusal
from webapp.services.ownership import AccountScope, OwnedResourceNotFound
from webapp.persistence import dbapi
from webapp.persistence import usage as usage_rows
from webapp.api.route_classes import USER


router = APIRouter(dependencies=[Depends(USER)], tags=["discovery"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchBody(StrictBody):
    sources: list[str] | None = None
    queries: list[str] | None = None
    locations: list[str] | None = None
    limit_per_source: int = Field(default=20, ge=1, le=50)


class LifecycleBody(StrictBody):
    status: Literal["new", "saved", "dismissed", "expired"]


class EvaluateBody(StrictBody):
    candidate_ids: list[str] = Field(min_length=1, max_length=50)
    extension_ids: list[str] = Field(default_factory=list)
    request_id: str


def _error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _authorize_search_workspace(
    scope: AccountScope,
    conn: dbapi.Connection,
    search_workspace_id: str,
) -> None:
    try:
        scope.require_search_workspace(conn, search_workspace_id)
    except OwnedResourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/search-workspaces/{search_workspace_id}/discovery/sources")
def get_sources(
    search_workspace_id: str,
    request: Request,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    _authorize_search_workspace(scope, conn, search_workspace_id)
    registry = {row["source_id"]: row for row in list_discovery_source_settings(conn)}
    available = available_discovery_source_ids(
        list_enabled_discovery_source_ids(conn, hosted=request.app.state.settings.is_hosted)  # DP-9
    )
    return {
        "sources": [
            {"source_id": source_id, "display_name": registry[source_id]["display_name"]}
            for source_id in available
        ]
    }


@router.post("/api/search-workspaces/{search_workspace_id}/discovery/search")
def post_search(
    body: SearchBody,
    request: Request,
    search_workspace_id: str,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    _authorize_search_workspace(scope, conn, search_workspace_id)
    runner = getattr(request.app.state, "discovery_portal_runner", None)
    if runner is None:
        runner = CliDiscoveryPortalRunner(Path(request.app.state.settings.profile_root).resolve())
    run_key = f"discovery:{search_workspace_id}:{uuid.uuid4().hex}"
    try:
        return request.app.state.metering.metered(
            conn, scope, feature="discovery.on_demand", allowance="discovery.on_demand_runs",
            subject_type="search_workspace", subject_id=search_workspace_id, key=lambda window_key: run_key,
            action=f"discovery:{search_workspace_id}",
            settlement_ref=lambda result: result["run"]["id"],  # authorizes the run's candidates for evaluation
            work=lambda: run_discovery_search(
                conn, runner, search_workspace_id=search_workspace_id,
                sources=body.sources, queries=body.queries,
                locations=body.locations, limit_per_source=body.limit_per_source,
                account_id=scope.account_id,
                deployment_ceiling=request.app.state.settings.autonomy_deployment_ceiling(),
                hosted=request.app.state.settings.is_hosted,
            ))
    except DiscoveryServiceError as exc:
        raise _error(exc) from exc


@router.get("/api/search-workspaces/{search_workspace_id}/discovery/candidates")
def get_candidates(
    search_workspace_id: str,
    conn: dbapi.Connection = Depends(get_conn),
    extensions_dir: Path = Depends(get_extensions_dir),
    scope: AccountScope = Depends(get_account_scope),
):
    _authorize_search_workspace(scope, conn, search_workspace_id)
    return {"groups": grouped_discovery_candidates(
        conn, search_workspace_id=search_workspace_id,
        extensions_dir=extensions_dir, account_id=scope.account_id,
    )}


@router.patch("/api/search-workspaces/{search_workspace_id}/discovery/candidates/{candidate_id}")
def patch_candidate(
    candidate_id: str,
    body: LifecycleBody,
    search_workspace_id: str,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    _authorize_search_workspace(scope, conn, search_workspace_id)
    try:
        return {"candidate": set_discovery_candidate_status(
            conn, candidate_id, body.status,
            search_workspace_id=search_workspace_id,
            account_id=scope.account_id,
        )}
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/api/search-workspaces/{search_workspace_id}/discovery/evaluate")
def post_evaluate(
    body: EvaluateBody,
    request: Request,
    search_workspace_id: str,
    conn: dbapi.Connection = Depends(get_conn),
    extensions_dir: Path = Depends(get_extensions_dir),
    scope: AccountScope = Depends(get_account_scope),
):
    _authorize_search_workspace(scope, conn, search_workspace_id)
    try:
        extensions = resolve_active_extensions(extensions_dir, body.extension_ids)
    except Exception as exc:
        raise _error(exc) from exc
    # Candidate evaluation is part of the discovery action (Bundle 7 §11.4): it needs the discovery
    # feature, and only a candidate found by a run that consumed a discovery allowance reaches the AI.
    # Every AI call still passes the metered boundary (AI switch, hidden cost ceiling, cost recorded).
    metering = request.app.state.metering
    metering.require_feature(conn, scope, "discovery.on_demand")
    providers = request_providers(request.app.state, scope, "search_workspace", search_workspace_id)
    understanding_provider, semantic_adapter = providers.understanding, providers.semantic_adapter
    results = []
    for index, candidate_id in enumerate(body.candidate_ids):
        if metering.enforced and not usage_rows.candidate_from_metered_run(
                conn, account_id=scope.account_id, candidate_id=candidate_id):
            results.append({"candidate_id": candidate_id, "status": "failed",
                            "error": "this job was not found by a discovery search on your plan"})
            continue
        try:
            fit = evaluate_discovery_candidate(
                conn, candidate_id, semantic_adapter,
                search_workspace_id=search_workspace_id,
                request_id=f"{body.request_id}-{index + 1}",
                understanding_provider=understanding_provider,
                active_extensions=extensions,
                account_id=scope.account_id,
            )
            results.append({"candidate_id": candidate_id, "status": "completed", "fit": fit})
        except Exception as exc:
            reraise_metering_refusal(exc)  # the whole batch stops at the fair-use limit / AI switched off
            results.append({"candidate_id": candidate_id, "status": "failed", "error": str(exc)})
    return {"results": results}


@router.post("/api/search-workspaces/{search_workspace_id}/discovery/candidates/{candidate_id}/promote")
def post_promote(
    candidate_id: str,
    search_workspace_id: str,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    _authorize_search_workspace(scope, conn, search_workspace_id)
    try:
        return promote_discovery_candidate(
            conn, candidate_id, search_workspace_id=search_workspace_id,
            account_id=scope.account_id,
        )
    except DiscoveryServiceError as exc:
        raise _error(exc) from exc
