from __future__ import annotations

from pathlib import Path
from typing import Iterator

from fastapi import Depends, HTTPException, Request

from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID, get_account
from webapp.persistence.db import connect
from webapp.api.route_classes import SIGN_IN_REQUIRED, ScopeRefused
from webapp.services.ownership import AccountScope, account_profile_root
from webapp.storage.profile_sources import profile_source_store_from_settings
from webapp.persistence import dbapi


def get_conn(request: Request) -> Iterator[dbapi.Connection]:
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_extensions_dir(request: Request) -> Path:
    return request.app.state.settings.extensions_dir


def get_documents_root(request: Request) -> Path:
    return request.app.state.settings.documents_root


def require_cv_quality_v2_enabled(request: Request) -> None:
    if not request.app.state.settings.cv_quality_v2_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


ACTIVE_ACCOUNT_STATUSES = frozenset({"ACTIVE"})
RESTRICTED_ACCOUNT_STATUSES = frozenset({"ACTIVE", "SUSPENDED", "DELETION_REQUESTED"})


def _scope_for(request: Request, account_id: str, user_id: str | None) -> AccountScope:
    settings = request.app.state.settings
    return AccountScope(
        account_id=account_id,
        profile_root=account_profile_root(settings.profile_root, account_id),
        profile_store=profile_source_store_from_settings(settings),
        user_id=user_id,
    )


def _session_scope(request: Request, *, allowed_statuses: frozenset[str], require_verified: bool) -> AccountScope:
    """Spec §6.2: session -> user -> OWNER membership -> account. Never falls
    back to another account."""
    user = getattr(request.state, "user", None)
    account = getattr(request.state, "account", None)
    if not user:
        raise ScopeRefused(*SIGN_IN_REQUIRED)
    if user["status"] == "PURGED" or account is None or account["status"] == "PURGED":
        raise ScopeRefused("ACCOUNT_UNAVAILABLE", 403, "This account is no longer available.")
    if account["status"] not in allowed_statuses:
        if account["status"] == "SUSPENDED":
            raise ScopeRefused("ACCOUNT_SUSPENDED", 403, "This account is suspended.")
        raise ScopeRefused("ACCOUNT_UNAVAILABLE", 403, "This account is not available.")
    if require_verified and user["email_verified_at"] is None:
        raise ScopeRefused("EMAIL_NOT_VERIFIED", 403, "Please verify your email address first.")
    return _scope_for(request, account["id"], user["id"])


def get_account_scope(
    request: Request,
    conn: dbapi.Connection = Depends(get_conn),
) -> AccountScope:
    """The caller's account. Auth mode: from the signed-in session (active
    account, verified email). Local single-user mode: the configured account."""
    if request.app.state.settings.auth_enabled:
        return _session_scope(request, allowed_statuses=ACTIVE_ACCOUNT_STATUSES, require_verified=True)
    account_id = request.app.state.settings.account_id or DEFAULT_ACCOUNT_ID
    if get_account(conn, account_id) is None:
        raise HTTPException(status_code=503, detail="configured account is unavailable")
    return _scope_for(request, account_id, None)


def restricted_scope(
    request: Request,
    conn: dbapi.Connection = Depends(get_conn),
) -> AccountScope:
    """For the never-gated surfaces (spec §11.5): export, deletion, sign-out,
    device revocation, billing portal. Suspended, deletion-requested and
    unverified users still reach these."""
    if request.app.state.settings.auth_enabled:
        return _session_scope(request, allowed_statuses=RESTRICTED_ACCOUNT_STATUSES, require_verified=False)
    return get_account_scope(request, conn)
