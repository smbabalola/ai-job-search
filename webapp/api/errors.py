"""Stable error contract for domain refusals (Bundle 7 spec §21.3).

API callers get JSON ``{"error": CODE, "message", "detail"}``. A page request
(a browser navigation or form post outside /api/) gets the same refusal as a
human page with an action: upgrade, verify, finish onboarding. Never a stack
trace or a generic error."""
from __future__ import annotations

from html import escape

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

# §21.3 plus the codes Bundle 7 added (ledgered): every refusal a user can meet.
ERROR_PAGE_ACTIONS: dict[str, tuple[str, str]] = {
    "FEATURE_NOT_IN_PLAN": ("See plans", "/plans"),
    "ALLOWANCE_EXHAUSTED": ("See plans", "/plans"),
    "FAIR_USE_LIMIT_REACHED": ("See your usage", "/settings/usage"),
    "ONBOARDING_INCOMPLETE": ("Finish setting up", "/onboarding"),
    "EMAIL_NOT_VERIFIED": ("Verify your email", "/check-email"),
    "ACCOUNT_SUSPENDED": ("Your account", "/settings/account"),
    "ACCOUNT_UNAVAILABLE": ("Your account", "/settings/account"),
    "DEVICE_REVOKED": ("Pair the extension", "/pairing"),
    "ACCOUNT_MISMATCH": ("Pair the extension", "/pairing"),
    "TICKET_INVALID": ("Back to your applications", "/"),
    "RATE_LIMITED": ("Back", "/"),
    "DATABASE_BUSY": ("Try again", "/"),
    "CSRF_FAILED": ("Reload", "/"),
    "PLAN_UNAVAILABLE": ("See plans", "/plans"),
    "RULE_ACKNOWLEDGEMENT_REQUIRED": ("Review your rules", "/preferences?tab=rules"),
    "REAUTH_REQUIRED": ("Sign in again", "/login"),
    "SIGNUP_UNAVAILABLE": ("Back to the home page", "/"),
    "ACTION_IN_PROGRESS": ("Back to your applications", "/"),
    "DOCUMENT_REJECTED": ("Back to My CVs", "/cvs"),
    # billing refusals (Task 14) and sign-in
    "INVALID_PLAN": ("See plans", "/plans"),
    "SUBSCRIPTION_EXISTS": ("Manage billing", "/settings/billing"),
    "NO_SUBSCRIPTION": ("See plans", "/plans"),
    "SIGN_IN_REQUIRED": ("Sign in", "/login"),
    # the staff console (Task 27)
    "STAFF_SIGN_IN_REQUIRED": ("Staff sign-in", "/admin/login"),
    "PERMISSION_DENIED": ("Back to the start", "/"),
}
ERROR_CODES = tuple(ERROR_PAGE_ACTIONS)


def error_response(code: str, message: str, status: int, *, detail: dict | None = None,
                   headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, "detail": detail or {}}, status_code=status,
                        headers=headers)


def render_error_page(code: str, message: str) -> str:
    label, href = ERROR_PAGE_ACTIONS.get(code, ("Back to your applications", "/"))
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>Something needs your attention · Job Search Workspace</title>"
            "<link rel=\"stylesheet\" href=\"/static/app.css\"></head><body><main class=\"page-shell\">"
            f"<section class=\"panel\" data-error-code=\"{escape(code)}\"><h1>{escape(message)}</h1>"
            f"<p><a class=\"button\" href=\"{escape(href)}\">{escape(label)}</a></p></section></main></body></html>")


def wants_page(request: Request) -> bool:
    path = request.url.path
    accept = request.headers.get("accept", "")
    return not path.startswith(("/api/", "/webhooks/", "/auth/", "/ext/")) and "application/json" not in accept \
        and ("text/html" in accept or request.method in ("GET", "POST"))


def error_response_for(request: Request, code: str, message: str, status: int, *, detail: dict | None = None,
                       headers: dict | None = None):
    if wants_page(request) and "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(render_error_page(code, message), status_code=status, headers=headers)
    return error_response(code, message, status, detail=detail, headers=headers)


async def csrf_failed_handler(request: Request, exc: Exception):
    return error_response_for(request, "CSRF_FAILED", "This form has expired. Reload the page and try again.", 403)


async def rate_limited_handler(request: Request, exc: Exception):
    retry_after = getattr(exc, "retry_after", 60)
    return error_response_for(request, "RATE_LIMITED", "Too many attempts. Please wait and try again.", 429,
                              detail={"retry_after": retry_after}, headers={"Retry-After": str(retry_after)})


_PAGE_REDIRECTS = {"SIGN_IN_REQUIRED": "/login", "EMAIL_NOT_VERIFIED": "/check-email",
                   "STAFF_SIGN_IN_REQUIRED": "/admin/login",
                   # Task 28: a suspended or deletion-requested account lands on what it can still do
                   "ACCOUNT_SUSPENDED": "/account/restricted", "ACCOUNT_UNAVAILABLE": "/account/restricted"}


async def scope_refused_handler(request: Request, exc: Exception):
    """API callers get the JSON contract; a page request is redirected to where
    the user can fix it (sign in, verify)."""
    from fastapi.responses import RedirectResponse
    from urllib.parse import quote

    accept = request.headers.get("accept", "")
    page = (request.method == "GET" and not request.url.path.startswith(("/api/", "/auth/"))
            and "application/json" not in accept)
    target = _PAGE_REDIRECTS.get(exc.code)
    if target == request.url.path:  # never redirect a page to itself
        target = None
    if page and target:
        if exc.code == "SIGN_IN_REQUIRED" and request.url.path not in ("/", "/login"):
            target = f"{target}?next={quote(request.url.path)}"
        return RedirectResponse(target, status_code=303)
    return error_response_for(request, exc.code, exc.message, exc.status)


async def feature_not_in_plan_handler(request: Request, exc: Exception):
    message = (f"Your plan doesn't include this. Upgrade to {exc.upgrade_to.title()} to use it."
               if exc.upgrade_to else "This feature is not available right now.")
    return error_response_for(request, "FEATURE_NOT_IN_PLAN", message, 402,
                              detail={"feature": exc.feature, "plan_id": exc.plan_id, "upgrade_to": exc.upgrade_to})


async def allowance_exhausted_handler(request: Request, exc: Exception):
    return error_response_for(
        request, "ALLOWANCE_EXHAUSTED", "You've used your plan's allowance for this period. Upgrade for more.", 402,
        detail={"allowance": exc.allowance, "used": exc.used, "limit": exc.limit,
                "window_end": exc.window_end.isoformat()})


async def database_busy_handler(request: Request, exc: Exception):
    from webapp.observability import METRICS
    METRICS.inc("database_busy_total", reason=getattr(exc, "reason", "unknown"))
    return error_response_for(request, "DATABASE_BUSY", "The service is busy. Please try again in a moment.", 503,
                              headers={"Retry-After": "2"})


async def action_in_progress_handler(request: Request, exc: Exception):
    retry_after = getattr(exc, "retry_after", 5)
    return error_response_for(request, "ACTION_IN_PROGRESS",
                              "This is already running. Refresh in a moment to see the result.", 409,
                              detail={"retry_after": retry_after}, headers={"Retry-After": str(retry_after)})


async def fair_use_limit_handler(request: Request, exc: Exception):
    # The ceiling is hidden (U3): never a number, only the fair-use message.
    return error_response_for(request, "FAIR_USE_LIMIT_REACHED",
                              "You've reached the fair-use limit for AI features this period. "
                              "It resets at the start of your next billing period.", 429)


async def document_rejected_handler(request: Request, exc: Exception):
    """Bundle 7 Review Focus 4: a refused upload names its reason; nothing was stored."""
    return error_response_for(request, "DOCUMENT_REJECTED", exc.message, 400, detail={"code": exc.code})


async def onboarding_incomplete_handler(request: Request, exc: Exception):
    """Bundle 7 15.2: what is missing, and where to fix it."""
    from webapp.services.onboarding_v1 import STEP_FOR_MISSING
    steps = sorted({STEP_FOR_MISSING[m] for m in exc.missing if STEP_FOR_MISSING.get(m)})
    return error_response_for(request, "ONBOARDING_INCOMPLETE",
                              "Finish setting up your account before preparing applications.", 409,
                              detail={"missing": exc.missing, "steps": [f"/onboarding/{s}" for s in steps]})
