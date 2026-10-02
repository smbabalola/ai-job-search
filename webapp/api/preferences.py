"""Preferences, job families and CVs, and rules (Bundle 7 spec §16.4). Every
save creates a new document version; rules apply to new preparations and
never to existing approvals. The Rules UI writes the well-known rules as
ordinary standing-policy rules; rules can only restrict, never grant."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from product.autonomy_contract import normalized_employer_key
from product.standing_policy import (
    EXCLUDED_EMPLOYERS_LIST, WELL_KNOWN_RULES, StandingPolicyError, build_well_known_rule, default_policy_document,
    upgrade_policy, validate_standing_policy,
)
from webapp.api.dependencies import get_account_scope, get_conn
from webapp.api.route_classes import USER
from webapp.persistence import dbapi
from webapp.persistence.autonomy_authority import current_policy
from webapp.services.ownership import AccountScope

router = APIRouter(dependencies=[Depends(USER)])

APPROVAL_NOTE = "applies to new preparations; existing approvals are unaffected"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _current(conn: dbapi.Connection, account_id: str) -> dict[str, Any]:
    policy = current_policy(conn, account_id)
    return upgrade_policy(policy["doc"]) if policy else default_policy_document("UTC")


class RuleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    params: dict[str, Any] | None


@router.get("/api/rules")
def get_rules(conn: dbapi.Connection = Depends(get_conn),
              scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    doc = _current(conn, scope.account_id)
    return {"rules": doc["rules"], "currency": doc.get("currency"), "employer_lists": doc.get("employer_lists", {}),
            "note": APPROVAL_NOTE}


def apply_rule(doc: dict[str, Any], rule_id: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """The policy with one well-known rule set (or removed when params is None)."""
    if rule_id not in WELL_KNOWN_RULES:
        raise StandingPolicyError([f"unknown rule {rule_id}"])
    rules = [r for r in doc["rules"] if r["id"] != rule_id]
    lists = dict(doc.get("employer_lists", {}))
    if params is not None:
        params = dict(params)
        if rule_id == "pref.salary_floor":
            currency = str(params.pop("currency", "") or doc.get("currency") or "").upper()
            if not currency:
                raise StandingPolicyError(["the salary floor needs a currency"])
            doc = {**doc, "currency": currency}
        if rule_id == "pref.excluded_employers":
            keys = sorted({k for k in (normalized_employer_key(e) for e in params.get("employers", [])) if k})
            if not keys:
                raise StandingPolicyError(["list at least one employer to exclude"])
            lists[EXCLUDED_EMPLOYERS_LIST] = keys
        rules.append(build_well_known_rule(rule_id, params))
    elif rule_id == "pref.excluded_employers":
        lists.pop(EXCLUDED_EMPLOYERS_LIST, None)
    updated = {**doc, "rules": rules, "employer_lists": lists}
    validate_standing_policy(updated)
    return updated


def _save(request: Request, conn: dbapi.Connection, scope: AccountScope, doc: dict[str, Any]) -> None:
    from webapp.services.autonomy_controls import save_standing_policy
    save_standing_policy(conn, account_id=scope.account_id, doc=doc, actor=scope.user_id or scope.account_id,
                         now=_now(), deployment_ceiling=request.app.state.settings.autonomy_deployment_ceiling())


@router.put("/api/rules")
def put_rule(body: RuleBody, request: Request, conn: dbapi.Connection = Depends(get_conn),
             scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    try:
        doc = apply_rule(_current(conn, scope.account_id), body.rule_id, body.params)
    except StandingPolicyError as exc:
        raise HTTPException(status_code=400, detail="; ".join(exc.errors) if hasattr(exc, "errors") else str(exc))
    _save(request, conn, scope, doc)
    return {"rules": doc["rules"], "note": APPROVAL_NOTE}


# ---- the page --------------------------------------------------------------------------------

def _active_search_workspaces(conn: dbapi.Connection, account_id: str) -> list[dict[str, Any]]:
    from webapp.persistence.search_workspaces import list_search_workspaces
    return [w for w in list_search_workspaces(conn, account_id=account_id) if w["status"] == "active"]


@router.get("/preferences")
def preferences_page(request: Request, tab: str = "preferences", saved: bool = False,
                     conn: dbapi.Connection = Depends(get_conn), scope: AccountScope = Depends(get_account_scope)):
    from product.user_profile import normalize_user_profile
    from webapp.persistence.user_profile import get_current_user_profile
    from webapp.services import cv_library, cv_strategy
    workspaces = _active_search_workspaces(conn, scope.account_id)
    profile = None
    if workspaces:
        current = get_current_user_profile(conn, workspaces[0]["id"], account_id=scope.account_id)
        profile = normalize_user_profile(current["payload"] if current else {})
    families, _ = cv_strategy.current_job_families(conn, scope.account_id)
    strategy, _ = cv_strategy.current_cv_strategy(conn, scope.account_id)
    items = {i["id"]: i for i in cv_library.list_items(conn, account_id=scope.account_id)}
    doc = _current(conn, scope.account_id)
    rules = {r["id"]: r for r in doc["rules"]}
    return request.app.state.templates.TemplateResponse(request, "preferences.html", {
        "tab": tab, "saved": saved, "note": APPROVAL_NOTE, "profile": profile,
        "search_workspace": workspaces[0] if workspaces else None, "families": families["families"],
        "strategy": strategy, "items": items, "rules": rules, "currency": doc.get("currency") or "",
        "excluded_employers": doc.get("employer_lists", {}).get(EXCLUDED_EMPLOYERS_LIST, [])})


@router.post("/preferences/profile")
async def save_profile_preferences(request: Request, conn: dbapi.Connection = Depends(get_conn),
                                   scope: AccountScope = Depends(get_account_scope)):
    from product.user_profile import UserProfileValidationError
    from webapp.persistence.user_profile import get_current_user_profile, save_user_profile
    form = await request.form()
    search_workspace_id = str(form.get("search_workspace_id") or "")
    if search_workspace_id not in {w["id"] for w in _active_search_workspaces(conn, scope.account_id)}:
        raise HTTPException(status_code=404, detail="search workspace not found")
    current = get_current_user_profile(conn, search_workspace_id, account_id=scope.account_id)
    payload = dict(current["payload"]) if current else {}
    payload.pop("schema_version", None)
    payload.update({"rotation_preference": form.get("rotation_preference") or "no_preference",
                    "acceptable_rotations": form.getlist("acceptable_rotations"),
                    "relocation": form.get("relocation") or "no"})
    try:
        save_user_profile(conn, payload, search_workspace_id=search_workspace_id,
                          expected_revision=current["profile_revision"] if current else 0, account_id=scope.account_id)
    except (UserProfileValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/preferences?tab=preferences&saved=1", status_code=303)


@router.post("/preferences/rules")
async def save_rules_form(request: Request, conn: dbapi.Connection = Depends(get_conn),
                          scope: AccountScope = Depends(get_account_scope)):
    form = await request.form()
    doc = _current(conn, scope.account_id)
    try:
        amount = str(form.get("salary_amount") or "").replace(",", "").strip()
        doc = apply_rule(doc, "pref.salary_floor", None if not amount else {
            "amount": int(amount), "currency": form.get("salary_currency") or "GBP",
            "effect": form.get("salary_effect") or "REQUIRE_USER", "on_unknown": form.get("salary_unknown") or "REQUIRE_USER"})
        countries = [c.strip() for c in str(form.get("excluded_countries") or "").split(",") if c.strip()]
        doc = apply_rule(doc, "pref.excluded_locations", {"countries": countries} if countries else None)
        rotations = form.getlist("accepted_rotations")
        doc = apply_rule(doc, "pref.rotation", {"accepted": rotations, "effect": form.get("rotation_effect") or
                                                "REQUIRE_USER"} if rotations else None)
        employers = [e.strip() for e in str(form.get("excluded_employers") or "").split("\n") if e.strip()]
        doc = apply_rule(doc, "pref.excluded_employers", {"employers": employers} if employers else None)
    except (StandingPolicyError, ValueError) as exc:
        detail = "; ".join(exc.errors) if isinstance(exc, StandingPolicyError) and hasattr(exc, "errors") else str(exc)
        raise HTTPException(status_code=400, detail=detail) from exc
    _save(request, conn, scope, doc)
    return RedirectResponse("/preferences?tab=rules&saved=1", status_code=303)
