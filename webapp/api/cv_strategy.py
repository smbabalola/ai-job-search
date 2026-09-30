"""Job families, CV strategy and the per-application CV choice (Bundle 7 spec
§14.2, §14.6). Every save is a new append-only document version; it applies
to new preparations, never to existing approvals."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from webapp.api.dependencies import get_account_scope, get_conn
from webapp.api.route_classes import USER
from webapp.persistence import dbapi
from webapp.services import cv_library as lib
from webapp.services import cv_strategy as cs
from webapp.services.ownership import AccountScope

router = APIRouter(dependencies=[Depends(USER)])

MAX_FORM_FAMILIES = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _save(conn: dbapi.Connection, action) -> dict[str, Any]:
    try:
        saved = action()
    except (cs.CvStrategyInvalid, cs.JobFamiliesInvalid) as exc:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn.commit()
    return saved


@router.get("/api/job-families")
def get_job_families(conn: dbapi.Connection = Depends(get_conn),
                     scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    doc, digest = cs.current_job_families(conn, scope.account_id)
    return {"doc": doc, "doc_hash": digest}


@router.put("/api/job-families")
async def put_job_families(request: Request, conn: dbapi.Connection = Depends(get_conn),
                           scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    doc = await request.json()
    return _save(conn, lambda: cs.save_job_families(conn, scope, doc, now=_now()))


@router.get("/api/cv-strategy")
def get_cv_strategy(conn: dbapi.Connection = Depends(get_conn),
                    scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    doc, digest = cs.current_cv_strategy(conn, scope.account_id)
    return {"doc": doc, "doc_hash": digest}


@router.put("/api/cv-strategy")
async def put_cv_strategy(request: Request, conn: dbapi.Connection = Depends(get_conn),
                          scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    doc = await request.json()
    return _save(conn, lambda: cs.save_cv_strategy(conn, scope, doc, now=_now()))


class ChoiceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: str


@router.post("/api/workspaces/{workspace_id}/cv-choice")
def post_cv_choice(workspace_id: str, body: ChoiceBody, conn: dbapi.Connection = Depends(get_conn),
                   scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    from webapp.persistence.workspaces import get_workspace
    if get_workspace(conn, workspace_id, account_id=scope.account_id) is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    try:
        resolution = cs.choose_cv(conn, scope, workspace_id=workspace_id, version_id=body.version_id,
                                  actor=scope.user_id or scope.account_id, now=_now())
    except LookupError as exc:
        conn.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # the workspace is past submission, or a stale selection
        conn.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    conn.commit()
    return {"resolution": resolution}


# ---- the editor page (/cvs/strategy) -----------------------------------------------------

def _slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKC", name).casefold()).strip("-")[:36] or "family"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    taken.add(slug)
    return slug


def _words(value: Any) -> list[str]:
    return [w.strip() for w in str(value or "").split(",") if w.strip()]


def _rule(value: Any) -> dict[str, Any] | None:
    """Form value "latest:<item>" or "tailor:<item>"; empty means no CV chosen."""
    mode, _, item_id = str(value or "").partition(":")
    if mode == "latest" and item_id:
        return {"mode": "LATEST_VERSION", "item_id": item_id}
    if mode == "tailor" and item_id:
        return {"mode": "TAILOR_FROM", "item_id": item_id, "template_id": "standard@1"}
    return None


def _describe(rule: dict[str, Any] | None) -> str:
    if not rule:
        return ""
    if rule["mode"] == "LATEST_VERSION":
        return f"latest:{rule['item_id']}" if rule.get("item_id") else ""
    if rule["mode"] == "TAILOR_FROM":
        return f"tailor:{rule['item_id']}"
    return ""


@router.get("/cvs/strategy")
def strategy_page(request: Request, saved: bool = False, conn: dbapi.Connection = Depends(get_conn),
                  scope: AccountScope = Depends(get_account_scope)):
    families, _ = cs.current_job_families(conn, scope.account_id)
    strategy, _ = cs.current_cv_strategy(conn, scope.account_id)
    rows = [{"name": f["name"], "words": ", ".join(f["match"]["title_any"]),
             "exclude": ", ".join(f["match"]["title_none"]),
             "cv": _describe(strategy["by_family"].get(f["id"]))} for f in families["families"]]
    rows.append({"name": "", "words": "", "exclude": "", "cv": ""})
    items = [i for i in lib.list_items(conn, account_id=scope.account_id) if i["status"] == "ACTIVE"]
    return request.app.state.templates.TemplateResponse(request, "cvs/strategy.html", {
        "rows": rows, "items": items, "default_cv": _describe(strategy["default"]), "saved": saved,
        "tailoring": _tailoring_available(request, conn, scope)})


def _tailoring_available(request: Request, conn: dbapi.Connection, scope: AccountScope) -> bool:
    metering = request.app.state.metering
    return not metering.enforced or metering.gate.entitlements(conn, scope, now=_now()).has("ai.cv_tailor")


@router.post("/cvs/strategy")
async def save_strategy_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
                             scope: AccountScope = Depends(get_account_scope)):
    form = await request.form()
    taken: set[str] = set()
    families, by_family = [], {}
    for index in range(MAX_FORM_FAMILIES + 1):
        name = " ".join(str(form.get(f"family_name_{index}") or "").split())
        words = _words(form.get(f"family_words_{index}"))
        if not name and not words:
            continue
        family_id = _slug(name or words[0], taken)
        families.append({"id": family_id, "name": name or words[0],
                         "match": {"title_any": words, "title_none": _words(form.get(f"family_exclude_{index}"))},
                         "priority": 0})
        rule = _rule(form.get(f"family_cv_{index}"))
        if rule:
            by_family[family_id] = rule
    default = _rule(form.get("default_cv")) or {"mode": "LATEST_VERSION", "item_id": None}
    now = _now()

    def save() -> None:
        cs.save_job_families(conn, scope, {"families": families}, now=now)
        cs.save_cv_strategy(conn, scope, {"default": default, "by_family": by_family}, now=now)
    _save(conn, save)
    return RedirectResponse("/cvs/strategy?saved=1", status_code=303)
