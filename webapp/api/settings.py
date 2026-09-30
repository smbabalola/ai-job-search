"""Pricing, plan selection, billing, usage and account settings pages (Bundle 7
spec §12.3, §13.3, §20.1). Pages read the same services as the JSON API; the
hidden AI cost ceiling is never shown as a number."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from product.entitlements import FEATURES, GAUGE_ALLOWANCES, HIDDEN_ALLOWANCES
from webapp.api.dependencies import get_account_scope, get_conn, restricted_scope
from webapp.api.route_classes import PUBLIC, USER
from webapp.persistence import dbapi
from webapp.services.ownership import AccountScope

router = APIRouter()

FEATURE_LABELS = {
    "workspace.tracking": "Track applications", "library.cv": "CV library", "profile.onboarding": "Guided setup",
    "profile.cv_import": "Import your profile from a CV", "ai.prepare": "AI application preparation",
    "ai.cv_tailor": "Tailored CVs", "apply.assisted_fill": "Form filling in your browser",
    "apply.human_submit": "Submit with one click after you review", "discovery.on_demand": "Job search",
    "discovery.scheduled": "Scheduled job searches", "automation.screening": "Automatic screening of new jobs",
    "automation.prepare": "Automatic preparation (you still review)", "rules.enforced_automation":
        "Rules applied to automation", "notifications.digest": "Daily email summary",
}
ALLOWANCE_LABELS = {
    "applications.prepare": "Prepared applications", "cv.tailor": "Tailored CVs", "profile.cv_import": "CV imports",
    "library.cv_items": "CVs in your library", "storage.bytes": "Storage",
    "discovery.on_demand_runs": "Job searches", "discovery.scheduled_searches": "Scheduled searches",
    "automation.prepare": "Automatic preparations",
}
SECURITY_ACTIONS = ("LOGIN_SUCCEEDED", "LOGIN_FAILED", "LOGOUT", "SESSIONS_REVOKED", "PASSWORD_CHANGED",
                    "PASSWORD_RESET", "EMAIL_VERIFIED", "EMAIL_CHANGED", "EXTENSION_DEVICE_PAIRED",
                    "EXTENSION_DEVICE_REVOKED", "EXTENSION_TOKEN_REUSE", "CONSENT_CHANGED",
                    "ACCOUNT_DELETION_REQUESTED", "ACCOUNT_DELETION_CANCELED", "DATA_EXPORT_REQUESTED",
                    "DATA_EXPORT_DOWNLOADED")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _render(request: Request, template: str, context: dict[str, Any]):
    return request.app.state.templates.TemplateResponse(request, template, context)


def format_limit(allowance: str, value: int | None) -> str:
    if value is None:
        return "—"
    if allowance == "storage.bytes":
        return f"{value / 1_000_000:,.0f} MB"
    return f"{value:,}"


def plan_rows(catalog: Any) -> list[dict[str, Any]]:
    plans = catalog.ranked()
    return [{"plan_id": p.plan_id, "name": p.display_name, "trial_days": p.trial_days,
             "prices": {interval: ("Shown at checkout" if price else "—")
                        for interval, price in {"month": p.provider_prices.get("month"),
                                                "year": p.provider_prices.get("year")}.items()},
             "features": {f: bool(p.features.get(f)) for f in FEATURES},
             "allowances": {a: format_limit(a, p.allowances.get(a)) for a in ALLOWANCE_LABELS}} for p in plans]


@router.get("/pricing", dependencies=[Depends(PUBLIC)])
def pricing_page(request: Request):
    catalog = request.app.state.billing_service.catalog
    return _render(request, "pricing.html", {"plans": plan_rows(catalog), "feature_labels": FEATURE_LABELS,
                                             "allowance_labels": ALLOWANCE_LABELS})


@router.get("/plans", dependencies=[Depends(USER)])
def plans_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
               scope: AccountScope = Depends(restricted_scope)):
    catalog = request.app.state.billing_service.catalog
    status = request.app.state.billing_service.status(conn, scope)
    return _render(request, "plans.html", {"plans": plan_rows(catalog), "feature_labels": FEATURE_LABELS,
                                           "allowance_labels": ALLOWANCE_LABELS, "current": status["plan_id"]})


@router.post("/plans/choose", dependencies=[Depends(USER)])
def choose_plan(request: Request, plan_id: str = Form(...), interval: str = Form("month"),
                conn: dbapi.Connection = Depends(get_conn), scope: AccountScope = Depends(get_account_scope)):
    """Free needs no provider; a paid plan starts the provider's checkout."""
    if plan_id == "free":
        return RedirectResponse("/", status_code=303)
    from webapp.api.billing import _service
    url = _service(request).start_checkout(conn, scope, plan_id=plan_id, interval=interval, now=_now())
    conn.commit()
    return RedirectResponse(url, status_code=303)


@router.get("/settings", dependencies=[Depends(USER)])
def settings_home():
    return RedirectResponse("/settings/account", status_code=303)


@router.get("/settings/billing", dependencies=[Depends(USER)])
def billing_page(request: Request, checkout: str | None = None, conn: dbapi.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(restricted_scope)):
    status = request.app.state.billing_service.status(conn, scope)
    catalog = request.app.state.billing_service.catalog
    plan = catalog.plans.get(status["plan_id"])
    return _render(request, "settings/billing.html", {
        "status": status, "plan_name": plan.display_name if plan else status["plan_id"], "checkout": checkout,
        "has_billing_account": request.app.state.billing_service.provider is not None})


def usage_rows(request: Request, conn: dbapi.Connection, scope: AccountScope) -> list[dict[str, Any]]:
    metering = request.app.state.metering
    rows = []
    for row in metering.usage.summary(conn, scope, now=_now()):
        if row["allowance"] in HIDDEN_ALLOWANCES:
            continue
        rows.append({**row, "label": ALLOWANCE_LABELS.get(row["allowance"], row["allowance"]),
                     "used_label": format_limit(row["allowance"], row["used"]),
                     "limit_label": format_limit(row["allowance"], row["limit"]),
                     "gauge": row["allowance"] in GAUGE_ALLOWANCES})
    return rows


@router.get("/settings/usage", dependencies=[Depends(USER)])
def usage_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
               scope: AccountScope = Depends(restricted_scope)):
    catalog = request.app.state.billing_service.catalog
    return _render(request, "settings/usage.html", {"rows": usage_rows(request, conn, scope),
                                                   "plans": plan_rows(catalog), "allowance_labels": ALLOWANCE_LABELS})


@router.get("/settings/account", dependencies=[Depends(USER)])
def account_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(restricted_scope)):
    return _render(request, "settings/account.html", {"user": request.state.user})


@router.get("/settings/security", dependencies=[Depends(USER)])
def security_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
                  scope: AccountScope = Depends(restricted_scope)):
    marks = ", ".join("?" for _ in SECURITY_ACTIONS)
    entries = [dict(r) for r in conn.execute(
        f"SELECT occurred_at, action, actor_type FROM audit_log WHERE account_id = ? AND action IN ({marks}) "
        f"ORDER BY seq DESC LIMIT 100", (scope.account_id, *SECURITY_ACTIONS))]
    return _render(request, "settings/security.html", {"entries": entries})


@router.get("/settings/sessions", dependencies=[Depends(USER)])
def sessions_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
                  scope: AccountScope = Depends(restricted_scope)):
    sessions = [dict(r) for r in conn.execute(
        "SELECT created_at, last_seen_at, user_agent_summary FROM web_sessions WHERE user_id = ? AND revoked_at IS NULL "
        "AND absolute_expires_at > ? ORDER BY last_seen_at DESC", (scope.user_id, _now().isoformat()))]
    return _render(request, "settings/sessions.html", {"sessions": sessions})


@router.get("/settings/devices", dependencies=[Depends(USER)])
def devices_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(restricted_scope)):
    from webapp.services.extension_auth import list_devices
    return _render(request, "settings/devices.html", {"devices": list_devices(conn, account_id=scope.account_id)})


# ---- the header: plan badge, prepare meter, past-due banner ------------------------------------

def header_status(request: Request) -> dict[str, Any] | None:
    """For base.html; None when signed out or when the local operator is unmetered."""
    user = getattr(request.state, "user", None)
    settings = request.app.state.settings
    if not user or not settings.auth_enabled:
        return None
    from webapp.persistence.db import connect
    from webapp.services.ownership import AccountScope as Scope
    conn = connect(settings)
    try:
        account_id = conn.execute("SELECT account_id FROM account_memberships WHERE user_id = ? AND role = 'OWNER'",
                                  (user["id"],)).fetchone()
        if account_id is None:
            return None
        scope = Scope(account_id=account_id[0], profile_root=settings.profile_root, user_id=user["id"])
        metering = request.app.state.metering
        resolved = metering.gate.entitlements(conn, scope, now=_now())
        prepare = next((r for r in metering.usage.summary(conn, scope, now=_now())
                        if r["allowance"] == "applications.prepare"), None)
        status = request.app.state.billing_service.status(conn, scope)
        catalog = request.app.state.billing_service.catalog
        plan = catalog.plans.get(resolved.plan_id)
        return {"plan_name": plan.display_name if plan else resolved.plan_id,
                "prepare_used": prepare["used"] if prepare else 0, "prepare_limit": prepare["limit"] if prepare else 0,
                "past_due": status.get("state") == "PAST_DUE"}
    except Exception:  # noqa: BLE001 - the header never breaks a page
        return None
    finally:
        conn.close()


def prepare_cost(request: Request, conn: dbapi.Connection, scope: AccountScope, workspace_id: str) -> str | None:
    """§13.3: the cost stated before a consuming action."""
    metering = request.app.state.metering
    if not metering.enforced:
        return None
    from webapp.persistence import usage as rows
    from webapp.services.usage import prepare_key
    resolved = metering.gate.entitlements(conn, scope, now=_now())
    if rows.live_by_key(conn, prepare_key(workspace_id, resolved.window.key)) is not None:
        return "Already counted this period: preparing this job again costs nothing."
    limit = resolved.allowances.get("applications.prepare") or 0
    used = rows.used(conn, account_id=scope.account_id, allowance="applications.prepare",
                     window_key=resolved.window.key)
    return f"Uses 1 of your {max(limit - used, 0)} remaining prepares this period."
