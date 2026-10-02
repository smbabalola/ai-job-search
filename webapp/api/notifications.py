"""Notifications, the inbox and communication preferences (Bundle 7 spec §17.4,
§18.4). Every route is account-scoped; none is gated by plan (§11.5)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from webapp.api.dependencies import get_account_scope, get_conn
from webapp.api.route_classes import USER
from webapp.persistence import dbapi
from webapp.services import inbox as inbox_service
from webapp.services import notifications as n
from webapp.services.ownership import AccountScope

router = APIRouter(dependencies=[Depends(USER)])

MARKETING_WORDING = "marketing-email.v1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def current_announcements(conn: dbapi.Connection, plan_id: str | None, now: datetime) -> list[dict[str, Any]]:
    audiences = ["ALL"] + ([f"PLAN:{plan_id}"] if plan_id else [])
    marks = ", ".join("?" for _ in audiences)
    stamp = now.isoformat(timespec="microseconds")
    rows = conn.execute(
        f"SELECT id, title, body_markdown, severity, published_at FROM announcements WHERE audience IN ({marks}) "
        f"AND published_at IS NOT NULL AND published_at <= ? AND withdrawn_at IS NULL "
        f"AND (expires_at IS NULL OR expires_at > ?) ORDER BY published_at DESC", (*audiences, stamp, stamp)).fetchall()
    return [dict(r) for r in rows]


def _plan_id(request: Request, conn: dbapi.Connection, scope: AccountScope) -> str | None:
    try:
        return request.app.state.metering.gate.entitlements(conn, scope, now=_now()).plan_id
    except Exception:  # noqa: BLE001 - announcements never fail the inbox
        return None


def _inbox(request: Request, conn: dbapi.Connection, scope: AccountScope) -> dict[str, Any]:
    settings = request.app.state.settings
    actions = inbox_service.action_required(conn, scope, settings=settings, now=_now())
    return {"action_required": [inbox_service.as_dict(i) for i in actions],
            "updates": n.list_notifications(conn, scope.account_id),
            "announcements": current_announcements(conn, _plan_id(request, conn, scope), _now()),
            "badge": len(actions) + n.unread_critical(conn, scope.account_id)}


@router.get("/api/notifications")
def get_notifications(archived: bool = False, conn: dbapi.Connection = Depends(get_conn),
                      scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return {"notifications": n.list_notifications(conn, scope.account_id, include_archived=archived)}


@router.post("/api/notifications/{notification_id}/read")
def post_read(notification_id: str, conn: dbapi.Connection = Depends(get_conn),
              scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    if not n.mark_read(conn, account_id=scope.account_id, notification_id=notification_id, now=_now()):
        raise HTTPException(status_code=404, detail="notification not found")
    conn.commit()
    return {"read": True}


@router.post("/api/notifications/{notification_id}/archive")
def post_archive(notification_id: str, conn: dbapi.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    if not n.archive(conn, account_id=scope.account_id, notification_id=notification_id, now=_now()):
        raise HTTPException(status_code=404, detail="notification not found")
    conn.commit()
    return {"archived": True}


@router.get("/api/inbox")
def get_inbox(request: Request, conn: dbapi.Connection = Depends(get_conn),
              scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return _inbox(request, conn, scope)


@router.get("/api/inbox/summary")
def get_inbox_summary(request: Request, conn: dbapi.Connection = Depends(get_conn),
                      scope: AccountScope = Depends(get_account_scope)) -> dict[str, int]:
    return {"badge": inbox_service.header_badge(conn, scope, settings=request.app.state.settings, now=_now())}


@router.get("/inbox")
def inbox_page(request: Request, tab: Literal["action", "updates", "announcements"] = "action",
               conn: dbapi.Connection = Depends(get_conn), scope: AccountScope = Depends(get_account_scope)):
    return request.app.state.templates.TemplateResponse(request, "inbox.html",
                                                        {"inbox": _inbox(request, conn, scope), "tab": tab})


class PreferenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    mode: str


@router.get("/api/notification-preferences")
def get_preferences(conn: dbapi.Connection = Depends(get_conn),
                    scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return {"email_modes": n.email_modes(conn, scope.account_id), "always_immediate": sorted(n.ALWAYS_IMMEDIATE)}


@router.put("/api/notification-preferences")
def put_preference(body: PreferenceBody, conn: dbapi.Connection = Depends(get_conn),
                   scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    try:
        n.set_email_mode(conn, account_id=scope.account_id, category=body.category, mode=body.mode, now=_now())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn.commit()
    return {"email_modes": n.email_modes(conn, scope.account_id)}


def _marketing(conn: dbapi.Connection, scope: AccountScope) -> bool | None:
    from webapp.comms.consent import current_consent
    return None if not scope.user_id else current_consent(conn, user_id=scope.user_id, channel="EMAIL",
                                                           purpose="MARKETING")


@router.get("/settings/communications")
def communications_page(request: Request, saved: bool = False, conn: dbapi.Connection = Depends(get_conn),
                        scope: AccountScope = Depends(get_account_scope)):
    from webapp.comms.render import TEMPLATE_CATEGORIES
    service = sorted(t for t, c in TEMPLATE_CATEGORIES.items() if c == "SERVICE")
    return request.app.state.templates.TemplateResponse(request, "settings/communications.html", {
        "modes": n.email_modes(conn, scope.account_id), "configurable": n.CONFIGURABLE,
        "marketing": _marketing(conn, scope), "service_templates": service, "saved": saved})


@router.post("/settings/communications")
async def save_communications(request: Request, conn: dbapi.Connection = Depends(get_conn),
                              scope: AccountScope = Depends(get_account_scope)):
    form = await request.form()
    now = _now()
    try:
        for category in n.CONFIGURABLE:
            mode = form.get(f"mode_{category}")
            if mode:
                n.set_email_mode(conn, account_id=scope.account_id, category=category, mode=str(mode), now=now)
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    wanted = form.get("marketing_email") == "on"
    if scope.user_id and _marketing(conn, scope) != wanted:
        from webapp.comms.consent import record_consent
        record_consent(conn, account_id=scope.account_id, user_id=scope.user_id, channel="EMAIL", purpose="MARKETING",
                       state="GRANTED" if wanted else "WITHDRAWN", wording_version=MARKETING_WORDING,
                       source="settings", now=now)
    conn.commit()
    return RedirectResponse("/settings/communications?saved=1", status_code=303)
