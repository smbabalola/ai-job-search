"""The staff console pages (Bundle 7 spec §7, §19.2).

Sign-in is two steps. ``/admin/login`` checks the password of a user holding a
staff role and sets a signed, five-minute pending cookie; ``/admin/totp``
checks the TOTP code (showing the enrolment secret on the first sign-in) and
only then creates the STAFF session. ``/admin/reauth`` repeats both checks and
rotates the session, which opens the five-minute window for destructive
actions. Pages read through ``admin_read_models`` only."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from webapp.api.admin_api import get_admin_scope, view_account
from webapp.api.auth import _client_ip, _clear_session_cookie, session_cookie_name
from webapp.api.dependencies import get_conn
from webapp.api.route_classes import ADMIN, PUBLIC
from webapp.persistence import dbapi, identity
from webapp.persistence.audit import audit
from webapp.services import admin_read_models as reads
from webapp.services import staff_auth
from webapp.services.passwords import DUMMY_PASSWORD_HASH, verify_password
from webapp.services.rate_limit import enforce
from webapp.services.sessions import LIFETIMES, SessionService
from webapp.services.staff_auth import StaffScope

router = APIRouter(tags=["admin"])
PENDING_COOKIE = "js_staff_pending"
LOGIN_FAILED = "That email, password or code is not right."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _page(request: Request, template: str, *, status: int = 200, **context: Any) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, f"admin/{template}", context, status_code=status)


def _staff_user(conn: dbapi.Connection, email: str, password: str) -> dict[str, Any] | None:
    """A password-verified ACTIVE user with at least one staff role and no account."""
    try:
        user = identity.get_user_by_email(conn, identity.normalize_email(email))
    except ValueError:
        user = None
    stored = identity.get_password_hash(conn, user["id"]) if user else None
    matched = verify_password(stored or DUMMY_PASSWORD_HASH, password or "")
    if not (user and stored and matched and user["status"] == "ACTIVE"):
        return None
    if not staff_auth.staff_roles(conn, user["id"]) or identity.get_owner_account(conn, user["id"]) is not None:
        return None
    return user


def _audit(conn, request: Request, action: str, user_id: str | None, detail: dict | None = None) -> None:
    settings = request.app.state.settings
    audit(conn, actor_type="STAFF", actor_id=user_id, account_id=None, action=action, now=_now(),
          ip=_client_ip(request), detail=detail, secret=settings.secret_key,
          request_id=getattr(request.state, "request_id", None))


def _set_cookie(request: Request, response, name: str, value: str, max_age: int) -> None:
    settings = request.app.state.settings
    response.set_cookie(name, value, max_age=max_age, path="/", secure=settings.is_hosted, httponly=True,
                        samesite="strict" if name == PENDING_COOKIE else "lax")


def _start_staff_session(conn, request: Request, user_id: str, response) -> None:
    settings = request.app.state.settings
    raw, _ = SessionService(settings.secret_key).create(conn, user_id=user_id, kind="STAFF", now=_now(),
                                                        ip=_client_ip(request),
                                                        user_agent=request.headers.get("user-agent"))
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now().isoformat(), user_id))
    _set_cookie(request, response, session_cookie_name(settings), raw,
                int(LIFETIMES["STAFF"][1].total_seconds()))
    response.delete_cookie(PENDING_COOKIE, path="/")


# ---- sign-in ----------------------------------------------------------------------------------------

@router.get("/admin/login", response_class=HTMLResponse, dependencies=[Depends(PUBLIC)])
def login_page(request: Request):
    return _page(request, "login.html", errors=[])


@router.post("/admin/login", dependencies=[Depends(PUBLIC)])
def login(request: Request, email: str = Form(""), password: str = Form(""),
          conn: dbapi.Connection = Depends(get_conn)):
    settings = request.app.state.settings
    enforce(conn, "login", f"staff:{_client_ip(request)}", now=_now())
    user = _staff_user(conn, email, password)
    if user is None:
        _audit(conn, request, "LOGIN_FAILED", None, {"console": "staff"})
        conn.commit()
        return _page(request, "login.html", status=401, errors=[LOGIN_FAILED])
    response = RedirectResponse("/admin/totp", status_code=303)
    _set_cookie(request, response, PENDING_COOKIE, staff_auth.pending_token(settings, user["id"], now=_now()),
                int(staff_auth.PENDING_TTL.total_seconds()))
    return response


def _pending_user(request: Request) -> str | None:
    return staff_auth.read_pending_token(request.app.state.settings, request.cookies.get(PENDING_COOKIE), now=_now())


@router.get("/admin/totp", response_class=HTMLResponse, dependencies=[Depends(PUBLIC)])
def totp_page(request: Request, conn: dbapi.Connection = Depends(get_conn)):
    user_id = _pending_user(request)
    if user_id is None:
        return RedirectResponse("/admin/login", status_code=303)
    enrolment = None
    if not staff_auth.has_confirmed_totp(conn, user_id):  # enrolment is forced on the first sign-in
        secret = staff_auth.start_totp_enrolment(conn, request.app.state.settings, user_id)
        conn.commit()
        email = identity.get_user(conn, user_id)["email_display"]
        enrolment = {"secret": secret, "uri": staff_auth.provisioning_uri(secret, email)}
    response = _page(request, "totp.html", errors=[], enrolment=enrolment)
    response.headers["Cache-Control"] = "no-store"  # the enrolment key is on this page
    return response


@router.post("/admin/totp", dependencies=[Depends(PUBLIC)])
def totp(request: Request, code: str = Form(""), conn: dbapi.Connection = Depends(get_conn)):
    settings = request.app.state.settings
    user_id = _pending_user(request)
    if user_id is None:
        return RedirectResponse("/admin/login", status_code=303)
    enforce(conn, "login", f"staff-totp:{user_id}", now=_now())
    enrolling = not staff_auth.has_confirmed_totp(conn, user_id)
    if not staff_auth.verify_totp(conn, settings, user_id, code, now=_now()):
        _audit(conn, request, "LOGIN_FAILED", user_id, {"console": "staff", "step": "totp"})
        conn.commit()
        return _page(request, "totp.html", status=401, errors=[LOGIN_FAILED], enrolment=None)
    if enrolling:
        _audit(conn, request, "STAFF_TOTP_ENROLLED", user_id)
    response = RedirectResponse("/admin", status_code=303)
    _start_staff_session(conn, request, user_id, response)
    _audit(conn, request, "LOGIN_SUCCEEDED", user_id, {"console": "staff"})
    conn.commit()
    return response


@router.get("/admin/reauth", response_class=HTMLResponse, dependencies=[Depends(ADMIN)])
def reauth_page(request: Request, next: str = "/admin", staff: StaffScope = Depends(get_admin_scope(None))):
    return _page(request, "reauth.html", errors=[], next=next if next.startswith("/admin") else "/admin")


@router.post("/admin/reauth", dependencies=[Depends(ADMIN)])
def reauth(request: Request, password: str = Form(""), code: str = Form(""), next: str = Form("/admin"),
           conn: dbapi.Connection = Depends(get_conn), staff: StaffScope = Depends(get_admin_scope(None))):
    """Password + TOTP again; the session is rotated (spec A4: rotate at privilege change)."""
    settings = request.app.state.settings
    enforce(conn, "login", f"staff-reauth:{staff.user_id}", now=_now())
    user = identity.get_user(conn, staff.user_id)
    target = next if next.startswith("/admin") else "/admin"
    if _staff_user(conn, user["email_normalized"], password) is None \
            or not staff_auth.verify_totp(conn, settings, staff.user_id, code, now=_now()):
        _audit(conn, request, "LOGIN_FAILED", staff.user_id, {"console": "staff", "step": "reauth"})
        conn.commit()
        return _page(request, "reauth.html", status=401, errors=[LOGIN_FAILED], next=target)
    SessionService(settings.secret_key).revoke(conn, request.state.raw_session, reason="REAUTHENTICATED")
    response = RedirectResponse(target, status_code=303)
    _start_staff_session(conn, request, staff.user_id, response)
    _audit(conn, request, "STAFF_REAUTHENTICATED", staff.user_id)
    conn.commit()
    return response


@router.post("/admin/logout", dependencies=[Depends(ADMIN)])
def logout(request: Request, conn: dbapi.Connection = Depends(get_conn)):
    settings = request.app.state.settings
    SessionService(settings.secret_key).revoke(conn, request.state.raw_session, reason="LOGOUT")
    _audit(conn, request, "LOGOUT", request.state.user["id"], {"console": "staff"})
    conn.commit()
    response = RedirectResponse("/admin/login", status_code=303)
    _clear_session_cookie(request, response)
    return response


# ---- pages ------------------------------------------------------------------------------------------

def _console(request: Request, template: str, staff: StaffScope, **context: Any) -> HTMLResponse:
    return _page(request, template, staff=staff, permissions=staff.permissions, **context)


@router.get("/admin", response_class=HTMLResponse, dependencies=[Depends(ADMIN)])
def dashboard_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
                   staff: StaffScope = Depends(get_admin_scope("accounts.view"))):
    return _console(request, "dashboard.html", staff,
                    data=reads.dashboard(conn, settings=request.app.state.settings, now=_now()))


@router.get("/admin/accounts", response_class=HTMLResponse, dependencies=[Depends(ADMIN)])
def accounts_page(request: Request, q: str = "", status: str = "", plan: str = "",
                  conn: dbapi.Connection = Depends(get_conn),
                  staff: StaffScope = Depends(get_admin_scope("accounts.view"))):
    rows = reads.search_accounts(conn, settings=request.app.state.settings, now=_now(), query=q, status=status,
                                 plan=plan)
    return _console(request, "accounts.html", staff, accounts=rows, filters={"q": q, "status": status, "plan": plan})


@router.get("/admin/accounts/{account_id}", response_class=HTMLResponse, dependencies=[Depends(ADMIN)])
def account_page(account_id: str, request: Request, conn: dbapi.Connection = Depends(get_conn),
                 staff: StaffScope = Depends(get_admin_scope("accounts.view"))):
    detail = view_account(request, conn, staff, account_id)
    if detail is None:
        return _console(request, "account.html", staff, account=None)
    return _console(request, "account.html", staff, account=detail)


@router.get("/admin/billing/events", response_class=HTMLResponse, dependencies=[Depends(ADMIN)])
def billing_events_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
                        staff: StaffScope = Depends(get_admin_scope("billing.view"))):
    return _console(request, "billing_events.html", staff, data=reads.billing_events(conn))


@router.get("/admin/jobs", response_class=HTMLResponse, dependencies=[Depends(ADMIN)])
def jobs_page(request: Request, status: str = "", conn: dbapi.Connection = Depends(get_conn),
              staff: StaffScope = Depends(get_admin_scope("ops.retry"))):
    return _console(request, "jobs.html", staff, jobs=reads.jobs(conn, status=status), status_filter=status)


@router.get("/admin/outbox", response_class=HTMLResponse, dependencies=[Depends(ADMIN)])
def outbox_page(request: Request, status: str = "", conn: dbapi.Connection = Depends(get_conn),
                staff: StaffScope = Depends(get_admin_scope("ops.retry"))):
    return _console(request, "outbox.html", staff, messages=reads.outbox(conn, status=status), status_filter=status)


@router.get("/admin/announcements", response_class=HTMLResponse, dependencies=[Depends(ADMIN)])
def announcements_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
                       staff: StaffScope = Depends(get_admin_scope("announcements.manage"))):
    from webapp.services.announcements import AUDIENCES, SEVERITIES, render_markdown
    items = [{**a, "html": render_markdown(a["body_markdown"])} for a in reads.announcements(conn)]
    return _console(request, "announcements.html", staff, announcements=items, audiences=AUDIENCES,
                    severities=SEVERITIES)


@router.get("/admin/controls", response_class=HTMLResponse, dependencies=[Depends(ADMIN)])
def controls_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
                  staff: StaffScope = Depends(get_admin_scope("controls.manage"))):
    return _console(request, "controls.html", staff, data=reads.controls(conn, settings=request.app.state.settings))


@router.get("/admin/staff", response_class=HTMLResponse, dependencies=[Depends(ADMIN)])
def staff_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
               staff: StaffScope = Depends(get_admin_scope("staff.manage"))):
    return _console(request, "staff.html", staff, members=reads.staff_members(conn), roles=staff_auth.ROLES)


@router.get("/admin/audit", response_class=HTMLResponse, dependencies=[Depends(ADMIN)])
def audit_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
               staff: StaffScope = Depends(get_admin_scope("audit.all"))):
    return _console(request, "audit.html", staff, entries=reads.audit_entries(conn))
