"""Stable JSON error contract for domain refusals (Bundle 7 spec §21.3)."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


def error_response(code: str, message: str, status: int, *, detail: dict | None = None,
                   headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, "detail": detail or {}}, status_code=status,
                        headers=headers)


async def csrf_failed_handler(request: Request, exc: Exception) -> JSONResponse:
    return error_response("CSRF_FAILED", "This form has expired. Reload the page and try again.", 403)


async def rate_limited_handler(request: Request, exc: Exception) -> JSONResponse:
    retry_after = getattr(exc, "retry_after", 60)
    return error_response("RATE_LIMITED", "Too many attempts. Please wait and try again.", 429,
                          detail={"retry_after": retry_after}, headers={"Retry-After": str(retry_after)})


_PAGE_REDIRECTS = {"SIGN_IN_REQUIRED": "/login", "EMAIL_NOT_VERIFIED": "/check-email"}


async def scope_refused_handler(request: Request, exc: Exception):
    """API callers get the JSON contract; a page request is redirected to where
    the user can fix it (sign in, verify)."""
    from fastapi.responses import RedirectResponse
    from urllib.parse import quote

    accept = request.headers.get("accept", "")
    wants_page = (request.method == "GET" and not request.url.path.startswith(("/api/", "/auth/"))
                  and "application/json" not in accept)
    target = _PAGE_REDIRECTS.get(exc.code)
    if wants_page and target:
        if exc.code == "SIGN_IN_REQUIRED" and request.url.path not in ("/", "/login"):
            target = f"{target}?next={quote(request.url.path)}"
        return RedirectResponse(target, status_code=303)
    return error_response(exc.code, exc.message, exc.status)
