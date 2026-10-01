from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from webapp.api.cv_generation_v2 import build_cv_v2_review_view, require_owned_artifact
from webapp.api.dependencies import get_account_scope, get_conn, require_cv_quality_v2_enabled
from product.user_profile import normalize_user_profile
from webapp.persistence.user_profile import get_current_user_profile
from product.discovery_search import available_discovery_source_ids
from webapp.persistence.discovery import get_latest_discovery_run
from webapp.persistence.discovery_sources import list_discovery_source_settings, list_enabled_discovery_source_ids
from webapp.persistence.search_workspaces import (
    DEFAULT_SEARCH_WORKSPACE_ID,
    get_search_workspace,
    list_search_workspaces,
)
from webapp.services.discovery import discovery_run_is_stale, grouped_discovery_candidates
from webapp.services.http_api import JobWorkspaceNotFound
from webapp.services.workspace_view import (
    build_dashboard_view_model,
    build_profile_view_model,
    build_workspace_view_model,
)
from webapp.services.profile_manager import get_profile_manager
from webapp.services.ownership import AccountScope
from product.onboarding_walkthroughs import WALKTHROUGH_LAUNCH_CONTEXTS
from webapp.services.onboarding import list_walkthrough_statuses
from webapp.persistence import dbapi
from webapp.api.route_classes import USER

router = APIRouter(dependencies=[Depends(USER)], tags=["views"])


def _search_context(
    conn: dbapi.Connection, account_id: str,
    selected_search_workspace: dict | None = None,
) -> dict:
    return {
        "search_workspaces": list_search_workspaces(
            conn, account_id=account_id, include_archived=True
        ),
        "selected_search_workspace": selected_search_workspace,
    }


def _require_search_workspace(
    conn: dbapi.Connection, search_workspace_id: str, account_id: str
) -> dict:
    workspace = get_search_workspace(
        conn, search_workspace_id, account_id=account_id
    )
    if workspace is None:
        raise HTTPException(status_code=404, detail="search workspace not found")
    return workspace


def _selected_search_workspace_id(
    request: Request, conn: dbapi.Connection, account_id: str
) -> str:
    selected = request.cookies.get("search_workspace_id", DEFAULT_SEARCH_WORKSPACE_ID)
    workspace = get_search_workspace(conn, selected, account_id=account_id)
    if workspace is not None and workspace["status"] == "active":
        return selected
    active = list_search_workspaces(conn, account_id=account_id)
    return active[0]["id"] if active else DEFAULT_SEARCH_WORKSPACE_ID


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, filter: str = "active",
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    if request.app.state.settings.auth_enabled and not getattr(request.state, "user", None):
        return request.app.state.templates.TemplateResponse(request, "landing.html", {})
    if filter not in {"all", "active", "drafted", "applied", "interview", "offer", "final"}:
        filter = "active"
    pending_replay = request.query_params.get("onboarding_replay")
    return request.app.state.templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            **build_dashboard_view_model(
                conn,
                filter_name=filter,
                extensions_dir=request.app.state.settings.extensions_dir,
                account_id=scope.account_id,
            ),
            "pending_workspace_replay": (
                pending_replay
                in ("job_workflow_intro", "document_workflow_intro")
            ),
            **_search_context(conn, scope.account_id),
            "onboarding_checklist": _onboarding_checklist(request, conn, scope),
        },
    )


def _onboarding_checklist(request: Request, conn, scope: AccountScope):
    """Bundle 7 15.4: the onboarding.v1 checklist until every required step is DONE or SKIPPED."""
    if not request.app.state.settings.auth_enabled:
        return None
    from webapp.api.onboarding_v1 import STEP_TITLES
    from webapp.services.onboarding_v1 import OPTIONAL_STEPS, onboarding_state
    state = onboarding_state(conn, scope)
    if state["complete"]:
        return None
    return [{"step": step, "title": STEP_TITLES[step], "state": value, "optional": step in OPTIONAL_STEPS}
            for step, value in state["steps"].items()]


@router.get("/profile", response_class=HTMLResponse)
def profile_page(
    request: Request,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    return_to = request.query_params.get("return_to", "")
    if not return_to.startswith("/workspaces/"):
        return_to = ""
    view = build_profile_view_model(
        conn, profile_root=scope.profile_sources(conn), account_id=scope.account_id
    )
    manager = None
    if not view["setup_required"]:
        manager = get_profile_manager(
            conn, root=scope.profile_sources(conn), account_id=scope.account_id
        )
    return request.app.state.templates.TemplateResponse(
        request, "profile.html", {
            **view,
            "profile_manager": manager,
            "return_to": return_to,
            **_search_context(conn, scope.account_id),
        }
    )


@router.get("/user-profile", response_class=HTMLResponse)
def user_profile_page(
    request: Request, conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    return RedirectResponse(
        f"/search-workspaces/{_selected_search_workspace_id(request, conn, scope.account_id)}/preferences",
        status_code=307,
    )


@router.get("/discover", response_class=HTMLResponse)
def discovery_page(
    request: Request, conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    return RedirectResponse(
        f"/search-workspaces/{_selected_search_workspace_id(request, conn, scope.account_id)}/discover",
        status_code=307,
    )


@router.get("/search-workspaces/{search_workspace_id}/preferences", response_class=HTMLResponse)
def scoped_user_profile_page(
    search_workspace_id: str,
    request: Request,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    workspace = _require_search_workspace(
        conn, search_workspace_id, scope.account_id
    )
    record = get_current_user_profile(
        conn, search_workspace_id, account_id=scope.account_id
    )
    response = request.app.state.templates.TemplateResponse(
        request,
        "user_profile.html",
        {
            "user_profile": record,
            "preferences": record["payload"] if record else normalize_user_profile({}),
            **_search_context(conn, scope.account_id, workspace),
        },
    )
    response.set_cookie("search_workspace_id", search_workspace_id, samesite="lax")
    return response


@router.get("/search-workspaces/{search_workspace_id}/discover", response_class=HTMLResponse)
def scoped_discovery_page(
    search_workspace_id: str,
    request: Request,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    workspace = _require_search_workspace(
        conn, search_workspace_id, scope.account_id
    )
    profile = get_current_user_profile(
        conn, search_workspace_id, account_id=scope.account_id
    )
    preferences = profile["payload"] if profile else normalize_user_profile({})
    latest_run = get_latest_discovery_run(conn, search_workspace_id)
    registry = {row["source_id"]: row for row in list_discovery_source_settings(conn)}
    available_sources = available_discovery_source_ids(
        list_enabled_discovery_source_ids(conn, hosted=request.app.state.settings.is_hosted)  # DP-9
    )
    response = request.app.state.templates.TemplateResponse(
        request,
        "discovery.html",
        {
            "user_profile": profile,
            "preferences": preferences,
            "sources": [
                {"source_id": source_id, "display_name": registry[source_id]["display_name"]}
                for source_id in available_sources
            ],
            "groups": grouped_discovery_candidates(
                conn,
                search_workspace_id=search_workspace_id,
                extensions_dir=request.app.state.settings.extensions_dir,
                account_id=scope.account_id,
            ),
            "latest_run": latest_run,
            "search_stale": discovery_run_is_stale(
                conn, latest_run, search_workspace_id=search_workspace_id,
                account_id=scope.account_id,
            ),
            **_search_context(conn, scope.account_id, workspace),
        },
    )
    response.set_cookie("search_workspace_id", search_workspace_id, samesite="lax")
    return response


@router.get("/new-job", response_class=HTMLResponse)
def new_job_page(
    request: Request, conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    return request.app.state.templates.TemplateResponse(
        request, "new_job.html", _search_context(conn, scope.account_id)
    )


@router.get("/how-it-works", response_class=HTMLResponse)
def how_it_works_page(
    request: Request, conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    return request.app.state.templates.TemplateResponse(
        request, "how_it_works.html", _search_context(conn, scope.account_id)
    )


@router.get("/pairing", response_class=HTMLResponse)
def pairing_page(
    request: Request, conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    from datetime import datetime, timezone

    from webapp.services.extension_auth import create_pairing_code

    _, one_time_secret = create_pairing_code(conn, account_id=scope.account_id, user_id=scope.user_id,
                                             now=datetime.now(timezone.utc))
    conn.commit()
    return request.app.state.templates.TemplateResponse(
        request, "pairing.html",
        {"one_time_secret": one_time_secret, **_search_context(conn, scope.account_id)},
    )


@router.get("/search-workspaces", response_class=HTMLResponse)
def search_workspaces_page(
    request: Request, conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    selected_id = _selected_search_workspace_id(
        request, conn, scope.account_id
    )
    selected = get_search_workspace(
        conn, selected_id, account_id=scope.account_id
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "search_workspaces.html",
        _search_context(conn, scope.account_id, selected),
    )


@router.get(
    "/workspaces/{workspace_id}/cv-v2/{plan_id}", response_class=HTMLResponse,
    dependencies=[Depends(require_cv_quality_v2_enabled)],
)
def cv_v2_review_page(
    workspace_id: str, plan_id: str, request: Request,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    try:
        view = build_workspace_view_model(
            conn, workspace_id,
            extensions_dir=request.app.state.settings.extensions_dir,
            account_id=scope.account_id,
        )
    except JobWorkspaceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    require_owned_artifact(conn, workspace_id, plan_id, artifact_type="cv_statement_plan")
    finalization = view["document_finalization"]
    return request.app.state.templates.TemplateResponse(
        request, "cv_v2_review.html",
        {
            "workspace": view["workspace"],
            "review": build_cv_v2_review_view(conn, workspace_id, plan_id),
            "can_mutate": finalization["can_upload"],
            "can_generate": finalization["can_generate"],
            "review_completion_friendly_issues": view["review_completion_friendly_issues"],
            **_search_context(conn, scope.account_id),
        },
    )


@router.get("/workspaces/{workspace_id}", response_class=HTMLResponse)
def workspace_detail_page(
    workspace_id: str, request: Request,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    try:
        view = build_workspace_view_model(
            conn, workspace_id,
            extensions_dir=request.app.state.settings.extensions_dir,
            account_id=scope.account_id,
        )
    except JobWorkspaceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    pending_replay = request.query_params.get("onboarding_replay")
    onboarding_replay_expected = (
        pending_replay
        if pending_replay in ("job_workflow_intro", "document_workflow_intro")
        else None
    )
    return request.app.state.templates.TemplateResponse(
        request, "workspace_detail.html",
        {
            **view,
            "cv_quality_v2_enabled": request.app.state.settings.cv_quality_v2_enabled,
            "onboarding_replay_expected": onboarding_replay_expected,
            **_cv_library_context(conn, scope.account_id, workspace_id),
            "prepare_cost": _prepare_cost(request, conn, scope, workspace_id),
            **_search_context(conn, scope.account_id),
        }
    )


def _prepare_cost(request: Request, conn, scope: AccountScope, workspace_id: str):
    from webapp.api.settings import prepare_cost
    try:
        return prepare_cost(request, conn, scope, workspace_id)
    except Exception:  # noqa: BLE001 - the disclosure never breaks the page
        return None


def _cv_library_context(conn, account_id: str, workspace_id: str) -> dict:
    """Bundle 7 §14.4/§14.6: which library CV this application uses, and the CVs it could use."""
    from webapp.services import cv_library, cv_strategy
    items = [i for i in cv_library.list_items(conn, account_id=account_id)
             if i["status"] == "ACTIVE" and i["latest_version_id"]]
    return {"cv_used": cv_strategy.cv_used_label(conn, account_id=account_id, workspace_id=workspace_id),
            "library_cvs": items}


@router.get("/walkthroughs", response_class=HTMLResponse)
def walkthroughs_page(
    request: Request,
    conn: dbapi.Connection = Depends(get_conn),
    scope: AccountScope = Depends(get_account_scope),
):
    statuses = list_walkthrough_statuses(conn, account_id=scope.account_id)
    replay_links = {}
    for item in statuses:
        launch = WALKTHROUGH_LAUNCH_CONTEXTS.get(item["walkthrough_id"])
        if launch is None:
            continue
        if launch["context"] == "page":
            replay_links[item["walkthrough_id"]] = (
                f"{launch['path']}?onboarding_replay={item['walkthrough_id']}"
            )
        else:
            replay_links[item["walkthrough_id"]] = (
                f"/?onboarding_replay={item['walkthrough_id']}"
            )
    return request.app.state.templates.TemplateResponse(
        request, "walkthroughs.html", {
            "walkthrough_statuses": statuses,
            "replay_links": replay_links,
            **_search_context(conn, scope.account_id),
        }
    )
