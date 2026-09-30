"""Authentication routes and pages (Bundle 7 spec §7) and the session
resolver that puts the signed-in user on ``request.state``."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

import anyio
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from webapp.api.dependencies import get_conn
from webapp.persistence import dbapi, identity
from webapp.persistence.db import connect
from webapp.services.auth import AuthService, SignupUnavailable, latest_legal_documents
from webapp.services.csrf import presession_cookie_name
from webapp.services.rate_limit import enforce
from webapp.services.sessions import LIFETIMES

router = APIRouter()


def session_cookie_name(settings: Any) -> str:
    # __Host- requires Secure and no Domain: hosted is always https.
    return "__Host-js_session" if settings.is_hosted else "js_session"


class AuthContextMiddleware:
    """When auth is enabled, resolves the session cookie once per request and
    exposes ``request.state.session``/``user``/``account`` (or None)."""

    def __init__(self, app, *, settings: Any) -> None:
        self.app = app
        self.settings = settings
        self.service = AuthService(settings)

    def _resolve(self, raw: str | None) -> dict[str, Any]:
        from datetime import datetime, timezone

        if not raw:
            return {}
        conn = connect(self.settings)
        try:
            session = self.service.sessions.resolve(conn, raw, now=datetime.now(timezone.utc))
            if session is None:
                return {}
            user = identity.get_user(conn, session["user_id"])
            account = identity.get_owner_account(conn, user["id"]) if user else None
            return {"session": session, "user": user, "account": account, "raw_session": raw,
                    "csrf_token": self.service.sessions.csrf_token(raw)}
        finally:
            conn.close()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.settings.auth_enabled:
            await self.app(scope, receive, send)
            return
        cookies: dict[str, str] = {}
        for key, value in scope.get("headers", []):
            if key == b"cookie":
                for part in value.decode("latin-1").split(";"):
                    k, _, v = part.strip().partition("=")
                    cookies[k] = v
        state = scope.setdefault("state", {})
        resolved = await anyio.to_thread.run_sync(self._resolve, cookies.get(session_cookie_name(self.settings)))
        for key in ("session", "user", "account", "raw_session", "csrf_token"):
            state[key] = resolved.get(key)
        if state.get("account"):
            state["account_id"] = state["account"]["id"]
        new_presession = None
        if not state.get("csrf_token"):
            # Signed out: the double-submit pre-session token (spec A5).
            presession = cookies.get(presession_cookie_name(self.settings))
            if not presession:
                presession = new_presession = secrets.token_urlsafe(32)
            state["csrf_token"] = presession

        async def send_with_cookie(message):
            if message["type"] == "http.response.start" and new_presession:
                attributes = "Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if self.settings.is_hosted else "")
                cookie = f"{presession_cookie_name(self.settings)}={new_presession}; {attributes}"
                message.setdefault("headers", []).append((b"set-cookie", cookie.encode("latin-1")))
            await send(message)

        await self.app(scope, receive, send_with_cookie)


def _service(request: Request) -> AuthService:
    return AuthService(request.app.state.settings, request_id=getattr(request.state, "request_id", None))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _email_key(email: str) -> str:
    try:
        return identity.normalize_email(email)
    except ValueError:
        return "invalid"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _page(request: Request, template: str, *, status: int = 200, **context) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, template, context, status_code=status)


def _set_session_cookie(request: Request, response: Response, raw: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(session_cookie_name(settings), raw, max_age=int(LIFETIMES["CUSTOMER"][1].total_seconds()),
                        path="/", secure=settings.is_hosted, httponly=True, samesite="lax")


def _clear_session_cookie(request: Request, response: Response) -> None:
    settings = request.app.state.settings
    response.delete_cookie(session_cookie_name(settings), path="/", secure=settings.is_hosted, httponly=True,
                           samesite="lax")


def _require_user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="SIGN_IN_REQUIRED")
    return user


# ---- pages -------------------------------------------------------------------

@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, conn: dbapi.Connection = Depends(get_conn)):
    return _page(request, "auth/signup.html", legal=latest_legal_documents(conn), errors=[], values={})


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _page(request, "auth/login.html", errors=[], values={})


@router.get("/check-email", response_class=HTMLResponse)
def check_email_page(request: Request):
    return _page(request, "auth/check_email.html")


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email_page(request: Request, token: str = ""):
    return _page(request, "auth/verify_email.html", token=token, errors=[])


@router.get("/reset-password", response_class=HTMLResponse)
def reset_request_page(request: Request):
    return _page(request, "auth/reset_request.html", sent=False)


@router.get("/reset-password/confirm", response_class=HTMLResponse)
def reset_confirm_page(request: Request, token: str = ""):
    return _page(request, "auth/reset_confirm.html", token=token, errors=[])


@router.get("/email-change/confirm", response_class=HTMLResponse)
def email_change_page(request: Request, token: str = ""):
    return _page(request, "auth/email_change_confirm.html", token=token, errors=[])


# ---- actions -----------------------------------------------------------------

@router.post("/auth/signup", response_class=HTMLResponse)
def signup(request: Request, email: str = Form(""), password: str = Form(""), display_name: str = Form(""),
           accept_terms: str = Form(""), accept_privacy: str = Form(""),
           conn: dbapi.Connection = Depends(get_conn)):
    accepted = [value for value in (accept_terms, accept_privacy) if value]
    enforce(conn, "signup", _client_ip(request) or "unknown", now=_utcnow())
    try:
        outcome = _service(request).signup(conn, email=email, password=password, display_name=display_name,
                                           accepted_legal_ids=accepted, ip=_client_ip(request))
    except SignupUnavailable:
        return _page(request, "auth/unavailable.html", status=503, code="SIGNUP_UNAVAILABLE")
    if not outcome.ok:
        return _page(request, "auth/signup.html", status=400, legal=latest_legal_documents(conn),
                     errors=outcome.errors, values={"email": email, "display_name": display_name})
    return _page(request, "auth/check_email.html")


@router.post("/auth/login")
def login(request: Request, email: str = Form(""), password: str = Form(""),
          conn: dbapi.Connection = Depends(get_conn)):
    enforce(conn, "login", f"{_client_ip(request)}:{_email_key(email)}", now=_utcnow())
    outcome = _service(request).login(conn, email=email, password=password, ip=_client_ip(request),
                                      user_agent=request.headers.get("user-agent"))
    if not outcome.ok:
        return _page(request, "auth/login.html", status=401, errors=outcome.errors, values={})
    destination = "/" if outcome.user["email_verified_at"] else "/check-email"
    response = RedirectResponse(destination, status_code=303)
    _set_session_cookie(request, response, outcome.session[0])
    return response


@router.post("/auth/logout")
def logout(request: Request, conn: dbapi.Connection = Depends(get_conn)):
    _service(request).logout(conn, raw_session=getattr(request.state, "raw_session", None))
    response = RedirectResponse("/login", status_code=303)
    _clear_session_cookie(request, response)
    return response


@router.post("/auth/verify-email")
def verify_email(request: Request, token: str = Form(""), conn: dbapi.Connection = Depends(get_conn)):
    outcome = _service(request).verify_email(conn, token=token)
    if not outcome.ok:
        return _page(request, "auth/verify_email.html", status=400, token="", errors=outcome.errors)
    return RedirectResponse("/", status_code=303)


@router.post("/auth/resend-verification", response_class=HTMLResponse)
def resend_verification(request: Request, email: str = Form(""), conn: dbapi.Connection = Depends(get_conn)):
    enforce(conn, "verify_resend", _email_key(email), now=_utcnow())
    _service(request).resend_verification(conn, email=email)
    return _page(request, "auth/check_email.html")


@router.post("/auth/password-reset/request", response_class=HTMLResponse)
def password_reset_request(request: Request, email: str = Form(""), conn: dbapi.Connection = Depends(get_conn)):
    enforce(conn, "password_reset", _email_key(email), now=_utcnow())
    _service(request).request_password_reset(conn, email=email)
    return _page(request, "auth/reset_request.html", sent=True)


@router.post("/auth/password-reset/confirm")
def password_reset_confirm(request: Request, token: str = Form(""), password: str = Form(""),
                           conn: dbapi.Connection = Depends(get_conn)):
    outcome = _service(request).confirm_password_reset(conn, token=token, password=password,
                                                       ip=_client_ip(request),
                                                       user_agent=request.headers.get("user-agent"))
    if not outcome.ok:
        return _page(request, "auth/reset_confirm.html", status=400, token=token, errors=outcome.errors)
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(request, response, outcome.session[0])
    return response


@router.post("/auth/email-change/confirm")
def email_change_confirm(request: Request, token: str = Form(""), conn: dbapi.Connection = Depends(get_conn)):
    outcome = _service(request).confirm_email_change(conn, token=token)
    if not outcome.ok:
        return _page(request, "auth/email_change_confirm.html", status=400, token="", errors=outcome.errors)
    return RedirectResponse("/", status_code=303)


@router.post("/settings/password")
def change_password(request: Request, current_password: str = Form(""), new_password: str = Form(""),
                    conn: dbapi.Connection = Depends(get_conn)):
    user = _require_user(request)
    outcome = _service(request).change_password(conn, user_id=user["id"], current_password=current_password,
                                                new_password=new_password,
                                                keep_session=request.state.raw_session)
    return JSONResponse({"ok": outcome.ok, "errors": outcome.errors}, status_code=200 if outcome.ok else 400)


@router.post("/settings/email")
def change_email(request: Request, new_email: str = Form(""), password: str = Form(""),
                 conn: dbapi.Connection = Depends(get_conn)):
    user = _require_user(request)
    outcome = _service(request).request_email_change(conn, user_id=user["id"], new_email=new_email,
                                                     password=password)
    return JSONResponse({"ok": outcome.ok, "errors": outcome.errors}, status_code=200 if outcome.ok else 400)


@router.post("/settings/sessions/revoke-all")
def revoke_all(request: Request, conn: dbapi.Connection = Depends(get_conn)):
    user = _require_user(request)
    _service(request).revoke_all(conn, user_id=user["id"])
    response = RedirectResponse("/login", status_code=303)
    _clear_session_cookie(request, response)
    return response


@router.get("/auth/csrf")
def csrf(request: Request):
    """The token the caller's next unsafe request must carry (readable only
    same-origin; cross-origin pages cannot read this response)."""
    return {"csrf_token": getattr(request.state, "csrf_token", None)}


@router.get("/auth/me")
def me(request: Request):
    user = _require_user(request)
    account = getattr(request.state, "account", None)
    return {"user_id": user["id"], "email": user["email_normalized"], "display_name": user["display_name"],
            "status": user["status"], "email_verified": user["email_verified_at"] is not None,
            "account_id": account["id"] if account else None}
