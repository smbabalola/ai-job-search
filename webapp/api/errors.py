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


async def feature_not_in_plan_handler(request: Request, exc: Exception) -> JSONResponse:
    message = (f"Your plan doesn't include this. Upgrade to {exc.upgrade_to.title()} to use it."
               if exc.upgrade_to else "This feature is not available right now.")
    return error_response("FEATURE_NOT_IN_PLAN", message, 402,
                          detail={"feature": exc.feature, "plan_id": exc.plan_id, "upgrade_to": exc.upgrade_to})


async def allowance_exhausted_handler(request: Request, exc: Exception) -> JSONResponse:
    return error_response(
        "ALLOWANCE_EXHAUSTED", "You've used your plan's allowance for this period. Upgrade for more.", 402,
        detail={"allowance": exc.allowance, "used": exc.used, "limit": exc.limit,
                "window_end": exc.window_end.isoformat()})


async def database_busy_handler(request: Request, exc: Exception) -> JSONResponse:
    return error_response("DATABASE_BUSY", "The service is busy. Please try again in a moment.", 503,
                          headers={"Retry-After": "2"})


async def action_in_progress_handler(request: Request, exc: Exception) -> JSONResponse:
    retry_after = getattr(exc, "retry_after", 5)
    return error_response("ACTION_IN_PROGRESS", "This is already running. Refresh in a moment to see the result.", 409,
                          detail={"retry_after": retry_after}, headers={"Retry-After": str(retry_after)})


async def fair_use_limit_handler(request: Request, exc: Exception) -> JSONResponse:
    # The ceiling is hidden (U3): never a number, only the fair-use message.
    return error_response("FAIR_USE_LIMIT_REACHED",
                          "You've reached the fair-use limit for AI features this period. "
                          "It resets at the start of your next billing period.", 429)


async def document_rejected_handler(request: Request, exc: Exception) -> JSONResponse:
    """Bundle 7 Review Focus 4: a refused upload names its reason; nothing was stored."""
    return error_response("DOCUMENT_REJECTED", exc.message, 400, detail={"code": exc.code})
