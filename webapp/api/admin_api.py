"""The staff console's JSON API (Bundle 7 spec §19). Every endpoint declares
one permission through ``get_admin_scope``; ``ADMIN_ENDPOINTS`` lists them for
the permission-matrix test. Destructive permissions also need a password +
TOTP authentication within the last five minutes (``REAUTH_REQUIRED``)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse

from webapp.api.dependencies import get_conn
from webapp.api.route_classes import ADMIN, PERMISSION_DENIED, ScopeRefused
from webapp.persistence import dbapi
from webapp.services import admin_actions as actions
from webapp.services import admin_read_models as reads
from webapp.services.staff_auth import (
    DESTRUCTIVE_PERMISSIONS, StaffScope, recently_authenticated, staff_roles,
)

router = APIRouter(dependencies=[Depends(ADMIN)], tags=["admin"])

# (method, path, permission) of every admin API endpoint; the matrix test walks it.
ADMIN_ENDPOINTS: list[tuple[str, str, str]] = []


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_admin_scope(permission: str | None) -> Callable[..., StaffScope]:
    """A STAFF session (the ADMIN route class checked its kind) whose user
    holds a role granting ``permission``; destructive permissions need a
    recent authentication."""
    def dependency(request: Request, conn: dbapi.Connection = Depends(get_conn)) -> StaffScope:
        user, session = request.state.user, request.state.session
        roles = staff_roles(conn, user["id"])
        if user["status"] != "ACTIVE" or not roles:
            raise ScopeRefused(*PERMISSION_DENIED)
        scope = StaffScope(user_id=user["id"], roles=roles, session=session)
        if permission is not None and permission not in scope.permissions:
            raise ScopeRefused(*PERMISSION_DENIED)
        if permission in DESTRUCTIVE_PERMISSIONS and not recently_authenticated(session, now=_now()):
            raise ScopeRefused("REAUTH_REQUIRED", 403,
                               "Confirm your password and code again to do this (it has been over 5 minutes).")
        return scope
    dependency.admin_permission = permission  # type: ignore[attr-defined]
    return dependency


def _endpoint(method: str, path: str, permission: str):
    ADMIN_ENDPOINTS.append((method, path, permission))
    return getattr(router, method.lower())(path)


def _refused(exc: actions.AdminActionRefused) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": exc.message, "detail": {}}, status_code=exc.status)


def _settings(request: Request):
    return request.app.state.settings


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ---- reads ----------------------------------------------------------------------------------------

@_endpoint("GET", "/api/admin/dashboard", "accounts.view")
def dashboard(request: Request, conn: dbapi.Connection = Depends(get_conn),
              staff: StaffScope = Depends(get_admin_scope("accounts.view"))):
    return reads.dashboard(conn, settings=_settings(request), now=_now())


@_endpoint("GET", "/api/admin/accounts", "accounts.view")
def accounts(request: Request, q: str = "", status: str = "", plan: str = "",
             conn: dbapi.Connection = Depends(get_conn),
             staff: StaffScope = Depends(get_admin_scope("accounts.view"))):
    return {"accounts": reads.search_accounts(conn, settings=_settings(request), now=_now(), query=q, status=status,
                                              plan=plan)}


def view_account(request: Request, conn: dbapi.Connection, staff: StaffScope, account_id: str):
    from webapp.persistence.audit import audit
    detail = reads.account_detail(conn, account_id, settings=_settings(request), now=_now())
    if detail is not None:
        audit(conn, actor_type="STAFF", actor_id=staff.user_id, account_id=account_id, action="ADMIN_ACCOUNT_VIEWED",
              now=_now(), target_type="account", target_id=account_id, secret=_settings(request).secret_key)
        conn.commit()
    return detail


@_endpoint("GET", "/api/admin/accounts/{account_id}", "accounts.view")
def account(account_id: str, request: Request, conn: dbapi.Connection = Depends(get_conn),
            staff: StaffScope = Depends(get_admin_scope("accounts.view"))):
    detail = view_account(request, conn, staff, account_id)
    if detail is None:
        return JSONResponse({"error": "NOT_FOUND", "message": "No such customer account.", "detail": {}},
                            status_code=404)
    return detail


@_endpoint("GET", "/api/admin/billing/events", "billing.view")
def billing_events(conn: dbapi.Connection = Depends(get_conn),
                   staff: StaffScope = Depends(get_admin_scope("billing.view"))):
    return reads.billing_events(conn)


@_endpoint("GET", "/api/admin/jobs", "ops.retry")
def jobs(status: str = "", conn: dbapi.Connection = Depends(get_conn),
         staff: StaffScope = Depends(get_admin_scope("ops.retry"))):
    return {"jobs": reads.jobs(conn, status=status)}


@_endpoint("GET", "/api/admin/outbox", "ops.retry")
def outbox(status: str = "", conn: dbapi.Connection = Depends(get_conn),
           staff: StaffScope = Depends(get_admin_scope("ops.retry"))):
    return {"messages": reads.outbox(conn, status=status)}


@_endpoint("GET", "/api/admin/announcements", "announcements.manage")
def announcements(conn: dbapi.Connection = Depends(get_conn),
                  staff: StaffScope = Depends(get_admin_scope("announcements.manage"))):
    return {"announcements": reads.announcements(conn)}


@_endpoint("GET", "/api/admin/controls", "controls.manage")
def controls(request: Request, conn: dbapi.Connection = Depends(get_conn),
             staff: StaffScope = Depends(get_admin_scope("controls.manage"))):
    return reads.controls(conn, settings=_settings(request))


@_endpoint("GET", "/api/admin/staff", "staff.manage")
def staff_list(conn: dbapi.Connection = Depends(get_conn),
               staff: StaffScope = Depends(get_admin_scope("staff.manage"))):
    return {"staff": reads.staff_members(conn)}


@_endpoint("GET", "/api/admin/audit", "audit.all")
def audit_log(conn: dbapi.Connection = Depends(get_conn), staff: StaffScope = Depends(get_admin_scope("audit.all"))):
    return {"entries": reads.audit_entries(conn)}


# ---- actions ---------------------------------------------------------------------------------------

def _run(work: Callable[[], object]):
    try:
        result = work()
    except actions.AdminActionRefused as exc:
        return _refused(exc)
    return {"ok": True, "result": result}


@_endpoint("POST", "/api/admin/accounts/{account_id}/resend-verification", "accounts.resend")
def resend_verification(account_id: str, request: Request, conn: dbapi.Connection = Depends(get_conn),
                        staff: StaffScope = Depends(get_admin_scope("accounts.resend"))):
    return _run(lambda: actions.resend_verification(conn, staff, account_id, settings=_settings(request), now=_now()))


@_endpoint("POST", "/api/admin/accounts/{account_id}/password-reset", "accounts.resend")
def password_reset(account_id: str, request: Request, conn: dbapi.Connection = Depends(get_conn),
                   staff: StaffScope = Depends(get_admin_scope("accounts.resend"))):
    return _run(lambda: actions.send_password_reset(conn, staff, account_id, settings=_settings(request), now=_now()))


@_endpoint("POST", "/api/admin/accounts/{account_id}/suspend", "accounts.suspend")
def suspend(account_id: str, request: Request, reason: str = Form(""), conn: dbapi.Connection = Depends(get_conn),
            staff: StaffScope = Depends(get_admin_scope("accounts.suspend"))):
    return _run(lambda: actions.suspend(conn, staff, account_id, reason=reason, settings=_settings(request),
                                        now=_now()))


@_endpoint("POST", "/api/admin/accounts/{account_id}/unsuspend", "accounts.suspend")
def unsuspend(account_id: str, request: Request, reason: str = Form(""), conn: dbapi.Connection = Depends(get_conn),
              staff: StaffScope = Depends(get_admin_scope("accounts.suspend"))):
    return _run(lambda: actions.unsuspend(conn, staff, account_id, reason=reason, settings=_settings(request),
                                          now=_now()))


@_endpoint("POST", "/api/admin/accounts/{account_id}/revoke-sessions", "accounts.suspend")
def revoke_sessions(account_id: str, request: Request, conn: dbapi.Connection = Depends(get_conn),
                    staff: StaffScope = Depends(get_admin_scope("accounts.suspend"))):
    return _run(lambda: actions.revoke_sessions(conn, staff, account_id, settings=_settings(request), now=_now()))


@_endpoint("POST", "/api/admin/accounts/{account_id}/kill-switch", "accounts.kill_switch")
def kill_switch(account_id: str, request: Request, engage: bool = Form(True), reason: str = Form(""),
                conn: dbapi.Connection = Depends(get_conn),
                staff: StaffScope = Depends(get_admin_scope("accounts.kill_switch"))):
    return _run(lambda: actions.kill_switch(conn, staff, account_id, engage=engage, reason=reason or "staff action",
                                            settings=_settings(request), now=_now()))


@_endpoint("POST", "/api/admin/accounts/{account_id}/grants", "grants.manage")
def create_grant(account_id: str, request: Request, kind: str = Form(...), plan_id: str = Form(""),
                 allowance: str = Form(""), amount: int | None = Form(None), expires_at: str = Form(""),
                 reason: str = Form(...), conn: dbapi.Connection = Depends(get_conn),
                 staff: StaffScope = Depends(get_admin_scope("grants.manage"))):
    return _run(lambda: actions.create_grant(conn, staff, account_id, kind=kind, plan_id=plan_id or None,
                                             allowance=allowance or None, amount=amount, expires_at=_date(expires_at),
                                             reason=reason, settings=_settings(request), now=_now()))


@_endpoint("POST", "/api/admin/grants/{grant_id}/revoke", "grants.manage")
def revoke_grant(grant_id: str, request: Request, conn: dbapi.Connection = Depends(get_conn),
                 staff: StaffScope = Depends(get_admin_scope("grants.manage"))):
    return _run(lambda: actions.revoke_grant(conn, staff, grant_id, settings=_settings(request), now=_now()))


@_endpoint("POST", "/api/admin/jobs/{job_id}/retry", "ops.retry")
def retry_job(job_id: str, request: Request, conn: dbapi.Connection = Depends(get_conn),
              staff: StaffScope = Depends(get_admin_scope("ops.retry"))):
    return _run(lambda: actions.retry_job(conn, staff, job_id, settings=_settings(request), now=_now()))


@_endpoint("POST", "/api/admin/outbox/{message_id}/retry", "ops.retry")
def retry_outbox(message_id: str, request: Request, conn: dbapi.Connection = Depends(get_conn),
                 staff: StaffScope = Depends(get_admin_scope("ops.retry"))):
    return _run(lambda: actions.retry_outbox(conn, staff, message_id, settings=_settings(request), now=_now()))


@_endpoint("POST", "/api/admin/announcements", "announcements.manage")
def create_announcement(request: Request, title: str = Form(...), body_markdown: str = Form(...),
                        audience: str = Form("ALL"), severity: str = Form("INFO"), expires_at: str = Form(""),
                        conn: dbapi.Connection = Depends(get_conn),
                        staff: StaffScope = Depends(get_admin_scope("announcements.manage"))):
    return _run(lambda: actions.create_announcement(conn, staff, title=title, body_markdown=body_markdown,
                                                    audience=audience, severity=severity,
                                                    expires_at=_date(expires_at), settings=_settings(request),
                                                    now=_now()))


@_endpoint("POST", "/api/admin/announcements/{announcement_id}/publish", "announcements.manage")
def publish_announcement(announcement_id: str, request: Request, email: bool = Form(False),
                         conn: dbapi.Connection = Depends(get_conn),
                         staff: StaffScope = Depends(get_admin_scope("announcements.manage"))):
    return _run(lambda: actions.publish_announcement(conn, staff, announcement_id, email=email,
                                                     settings=_settings(request), now=_now()))


@_endpoint("POST", "/api/admin/announcements/{announcement_id}/withdraw", "announcements.manage")
def withdraw_announcement(announcement_id: str, request: Request, conn: dbapi.Connection = Depends(get_conn),
                          staff: StaffScope = Depends(get_admin_scope("announcements.manage"))):
    return _run(lambda: actions.withdraw_announcement(conn, staff, announcement_id, settings=_settings(request),
                                                      now=_now()))


@_endpoint("POST", "/api/admin/controls", "controls.manage")
def set_control(request: Request, key: str = Form(...), value: bool = Form(...), reason: str = Form(""),
                conn: dbapi.Connection = Depends(get_conn),
                staff: StaffScope = Depends(get_admin_scope("controls.manage"))):
    return _run(lambda: actions.set_control(conn, staff, key, value, reason=reason, settings=_settings(request),
                                            now=_now()))


@_endpoint("POST", "/api/admin/staff/{user_id}/roles", "staff.manage")
def set_role(user_id: str, request: Request, role: str = Form(...), grant: bool = Form(True),
             reason: str = Form(...), conn: dbapi.Connection = Depends(get_conn),
             staff: StaffScope = Depends(get_admin_scope("staff.manage"))):
    return _run(lambda: actions.set_staff_role(conn, staff, user_id, role, grant=grant, reason=reason,
                                               settings=_settings(request), now=_now()))
