"""Extension pairing, tokens, devices and handoff tickets (Bundle 7 spec §9.2, §21.2)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from webapp.api.dependencies import get_account_scope, get_conn, restricted_scope
from webapp.api.route_classes import EXTENSION, PUBLIC, USER, ScopeRefused
from webapp.persistence import dbapi, identity
from webapp.services import extension_auth as ext
from webapp.services.ownership import AccountScope
from webapp.services.rate_limit import enforce

router = APIRouter()


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PairBody(_Body):
    code: str
    device_label: str = "Browser extension"


class TokenBody(_Body):
    device_id: str
    refresh_token: str


class TicketBody(_Body):
    workspace_id: str
    purpose: str = "HANDOFF"


@dataclass(frozen=True)
class ExtensionScope(AccountScope):
    device_id: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _secret(request: Request) -> str | None:
    return request.app.state.settings.secret_key


def _refuse(exc: ext.ExtensionAuthError) -> ScopeRefused:
    status = 403 if isinstance(exc, (ext.AccountMismatch, ext.TicketInvalid)) else 401
    return ScopeRefused(exc.code, status, str(exc))


def get_extension_scope(request: Request, conn: dbapi.Connection = Depends(get_conn)) -> ExtensionScope:
    """Bearer device token -> device -> account (spec §9.2 "Call")."""
    header = request.headers.get("authorization", "")
    raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
    now = _now()
    principal = ext.resolve_access(conn, raw, now=now)
    if principal is None:
        if raw and ext.access_token_expired(conn, raw, now=now):
            raise ScopeRefused("TOKEN_EXPIRED", 401, "The access token expired; refresh it.")
        raise ScopeRefused("DEVICE_REVOKED", 401, "This extension is not paired. Pair it again.")
    account = conn.execute("SELECT * FROM accounts WHERE id = ?", (principal.account_id,)).fetchone()
    if account is None or account["status"] != "ACTIVE":
        code = "ACCOUNT_SUSPENDED" if account is not None and account["status"] == "SUSPENDED" else "ACCOUNT_UNAVAILABLE"
        raise ScopeRefused(code, 403, "This account is not available.")
    if principal.user_id is not None:
        user = identity.get_user(conn, principal.user_id)
        if user is None or user["email_verified_at"] is None or user["status"] not in ("ACTIVE",):
            raise ScopeRefused("EMAIL_NOT_VERIFIED", 403, "Verify your email address first.")
    from webapp.api.dependencies import _scope_for

    base = _scope_for(request, principal.account_id, principal.user_id)
    request.state.account_id = principal.account_id
    return ExtensionScope(**{**base.__dict__, "device_id": principal.device_id})


# ---- web (signed-in user) -----------------------------------------------------

@router.post("/api/ext/pairing-codes", status_code=201, dependencies=[Depends(USER)])
def create_pairing_code(scope: AccountScope = Depends(get_account_scope),
                        conn: dbapi.Connection = Depends(get_conn)) -> dict[str, Any]:
    now = _now()
    enforce(conn, "pairing_code", scope.user_id or scope.account_id, now=now)
    _, code = ext.create_pairing_code(conn, account_id=scope.account_id, user_id=scope.user_id, now=now)
    conn.commit()
    return {"code": code, "expires_at": (now + ext.PAIRING_CODE_TTL).isoformat()}


@router.post("/api/ext/handoff-tickets", status_code=201, dependencies=[Depends(USER)])
def create_handoff_ticket(body: TicketBody, request: Request, scope: AccountScope = Depends(get_account_scope),
                          conn: dbapi.Connection = Depends(get_conn)) -> dict[str, Any]:
    from webapp.services.ownership import OwnedResourceNotFound

    try:
        scope.require_job_workspace(conn, body.workspace_id)
    except OwnedResourceNotFound as exc:
        raise ScopeRefused("NOT_FOUND", 404, "workspace not found") from exc
    ticket = ext.issue_handoff_ticket(conn, account_id=scope.account_id, user_id=scope.user_id,
                                      workspace_id=body.workspace_id, purpose=body.purpose, now=_now(),
                                      secret=_secret(request))
    conn.commit()
    return {"ticket": ticket}


@router.get("/api/settings/devices", dependencies=[Depends(USER)])
def list_devices(scope: AccountScope = Depends(restricted_scope),
                 conn: dbapi.Connection = Depends(get_conn)) -> dict[str, Any]:
    return {"devices": ext.list_devices(conn, account_id=scope.account_id)}


@router.post("/api/settings/devices/{device_id}/revoke", status_code=204, dependencies=[Depends(USER)])
def revoke_device(device_id: str, request: Request, scope: AccountScope = Depends(restricted_scope),
                  conn: dbapi.Connection = Depends(get_conn)) -> Response:
    owned = conn.execute("SELECT 1 FROM extension_devices WHERE id = ? AND account_id = ?",
                         (device_id, scope.account_id)).fetchone()
    if owned is None:
        raise ScopeRefused("NOT_FOUND", 404, "device not found")
    ext.revoke_device(conn, device_id=device_id, reason="USER_REVOKED", now=_now(), secret=_secret(request))
    conn.commit()
    return Response(status_code=204)


# ---- extension ----------------------------------------------------------------

@router.post("/api/ext/pair", status_code=201, dependencies=[Depends(PUBLIC)])
def pair(body: PairBody, request: Request, conn: dbapi.Connection = Depends(get_conn)) -> dict[str, Any]:
    now = _now()
    enforce(conn, "pairing_code", f"pair:{request.client.host if request.client else 'unknown'}", now=now)
    try:
        result = ext.pair_device(conn, raw_code=body.code, device_label=body.device_label, now=now,
                                 secret=_secret(request))
    except ext.ExtensionAuthError as exc:
        conn.rollback()
        raise _refuse(exc) from exc
    conn.commit()
    return asdict(result)


@router.post("/api/ext/token", dependencies=[Depends(PUBLIC)])
def token(body: TokenBody, request: Request, conn: dbapi.Connection = Depends(get_conn)) -> dict[str, Any]:
    now = _now()
    enforce(conn, "token_refresh", body.device_id, now=now)
    try:
        pair_ = ext.refresh(conn, device_id=body.device_id, raw_refresh=body.refresh_token, now=now,
                            secret=_secret(request))
    except ext.ExtensionAuthError as exc:
        raise _refuse(exc) from exc
    conn.commit()
    return asdict(pair_)


@router.post("/api/ext/devices/self/revoke", status_code=204, dependencies=[Depends(EXTENSION)])
def revoke_self(request: Request, scope: ExtensionScope = Depends(get_extension_scope),
                conn: dbapi.Connection = Depends(get_conn)) -> Response:
    ext.revoke_device(conn, device_id=scope.device_id, reason="SIGNED_OUT", now=_now(), secret=_secret(request))
    conn.commit()
    return Response(status_code=204)


@router.get("/api/ext/whoami", dependencies=[Depends(EXTENSION)])
def whoami(scope: ExtensionScope = Depends(get_extension_scope),
           conn: dbapi.Connection = Depends(get_conn)) -> dict[str, Any]:
    user = identity.get_user(conn, scope.user_id) if scope.user_id else None
    return {"device_id": scope.device_id, "account_id": scope.account_id,
            "account_label": ext.mask_email(user["email_normalized"]) if user else "Local user"}
