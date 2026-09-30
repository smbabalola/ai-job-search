"""CSRF protection (Bundle 7 spec A5, T3).

Applies when auth is enabled, to every unsafe request that could ride on
ambient browser credentials. Signed-in requests must carry the session's
HMAC-derived token; signed-out form posts (login, sign-up, reset) carry the
double-submit pre-session token, which also blocks login CSRF. A present
Origin (or Referer) must be this application's origin.

Exempt: bearer-authenticated requests without a session cookie (the
extension), extension endpoints under /api/ext/, and signed provider webhooks.
"""
from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import Request

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
EXEMPT_PREFIXES = ("/webhooks/", "/api/ext/")


class CsrfFailed(Exception):
    """CSRF_FAILED (403)."""


def presession_cookie_name(settings) -> str:
    return "__Host-js_presession" if settings.is_hosted else "js_presession"


def expected_origin(request: Request) -> str:
    settings = request.app.state.settings
    if settings.is_hosted:
        return settings.public_origin.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}"


def _origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


async def require_csrf(request: Request) -> None:
    from webapp.api.auth import session_cookie_name

    settings = request.app.state.settings
    if not settings.auth_enabled or request.method in SAFE_METHODS:
        return
    if request.url.path.startswith(EXEMPT_PREFIXES):
        return
    has_session_cookie = session_cookie_name(settings) in request.cookies
    if request.headers.get("authorization", "").lower().startswith("bearer ") and not has_session_cookie:
        return
    origin = request.headers.get("origin") or _origin_of(request.headers.get("referer", ""))
    if origin and origin != expected_origin(request):
        raise CsrfFailed("cross-origin request")
    expected = getattr(request.state, "csrf_token", None)
    presented = request.headers.get("x-csrf-token")
    if not presented and request.headers.get("content-type", "").startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        presented = form.get("csrf_token")
    if not expected or not presented or not hmac.compare_digest(str(expected), str(presented)):
        raise CsrfFailed("missing or invalid token")
