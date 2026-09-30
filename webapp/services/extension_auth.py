"""Extension device credentials (Bundle 7 spec X2-X4, §9).

* Pairing: a signed-in user mints a 10-character Crockford-base32 code (10
  minutes, single use); the extension exchanges it for a device.
* Tokens: a 10-minute bearer access token plus a 30-day sliding refresh token.
  Refresh rotates; presenting an already-rotated refresh token is treated as
  theft: the whole family and the device are revoked, audited and notified.
* Handoff tickets: the web page gives the extension a server-signed ticket
  (5 minutes, single use) bound to user, account, workspace and purpose; the
  server refuses one presented by another user's device.

Tokens, codes and nonces are stored as SHA-256 hashes only.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from webapp import comms
from webapp.persistence import dbapi, identity
from webapp.persistence.audit import audit
from webapp.services import notifications

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PAIRING_CODE_TTL = timedelta(minutes=10)
ACCESS_TOKEN_TTL = timedelta(minutes=10)
REFRESH_TOKEN_TTL = timedelta(days=30)
HANDOFF_TICKET_TTL = timedelta(minutes=5)
LOCAL_DEVELOPMENT_SECRET = "local-development-only-not-a-secret"


class ExtensionAuthError(Exception):
    code = "EXTENSION_AUTH_FAILED"


class PairingCodeInvalid(ExtensionAuthError):
    code = "PAIRING_CODE_INVALID"


class DeviceRevoked(ExtensionAuthError):
    code = "DEVICE_REVOKED"


class TicketInvalid(ExtensionAuthError):
    code = "TICKET_INVALID"


class AccountMismatch(ExtensionAuthError):
    code = "ACCOUNT_MISMATCH"


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    access_expires_at: str
    refresh_token: str


@dataclass(frozen=True)
class PairResult:
    device_id: str
    access_token: str
    access_expires_at: str
    refresh_token: str
    account_label: str


@dataclass(frozen=True)
class ExtensionPrincipal:
    device_id: str
    user_id: str | None
    account_id: str


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _normalize_code(raw: str) -> str:
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum()).replace("O", "0").replace("I", "1").replace(
        "L", "1")


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _account_label(conn, user_id: str | None) -> str:
    if user_id is None:
        return "Local user"
    user = identity.get_user(conn, user_id)
    return mask_email(user["email_normalized"]) if user else "Unknown"


def _audit(conn, action: str, *, principal_user: str | None, account_id: str, device_id: str, now: datetime,
           secret: str | None, detail: dict | None = None) -> None:
    audit(conn, actor_type="EXTENSION_DEVICE", actor_id=device_id, account_id=account_id, action=action,
          target_type="extension_device", target_id=device_id, now=now, secret=secret,
          detail={"user_id": principal_user, **(detail or {})})


# ---- pairing ------------------------------------------------------------------

def create_pairing_code(conn: dbapi.Connection, *, account_id: str, user_id: str | None,
                        now: datetime) -> tuple[str, str]:
    code = "".join(secrets.choice(CROCKFORD) for _ in range(10))
    code_id = f"pcode_{uuid.uuid4().hex[:20]}"
    conn.execute(
        "INSERT INTO pairing_codes (id, account_id, user_id, code_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (code_id, account_id, user_id, _hash(code), _iso(now), _iso(now + PAIRING_CODE_TTL)),
    )
    return code_id, code


def _issue_tokens(conn, *, device_id: str, family_id: str, now: datetime) -> TokenPair:
    access, refresh_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    conn.execute("INSERT INTO extension_access_tokens (token_hash, device_id, expires_at) VALUES (?, ?, ?)",
                 (_hash(access), device_id, _iso(now + ACCESS_TOKEN_TTL)))
    conn.execute(
        "INSERT INTO extension_refresh_tokens (id, device_id, family_id, token_hash, issued_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (f"rtok_{uuid.uuid4().hex[:20]}", device_id, family_id, _hash(refresh_token), _iso(now),
         _iso(now + REFRESH_TOKEN_TTL)),
    )
    return TokenPair(access, _iso(now + ACCESS_TOKEN_TTL), refresh_token)


def pair_device(conn: dbapi.Connection, *, raw_code: str, device_label: str, now: datetime,
                secret: str | None = None) -> PairResult:
    row = conn.execute("SELECT * FROM pairing_codes WHERE code_hash = ?", (_hash(_normalize_code(raw_code)),)).fetchone()
    if row is None or row["consumed_at"] is not None or _iso(now) > row["expires_at"]:
        raise PairingCodeInvalid("pairing code is not valid")
    consumed = conn.execute("UPDATE pairing_codes SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                            (_iso(now), row["id"]))
    if consumed.rowcount != 1:
        raise PairingCodeInvalid("pairing code is not valid")
    device_id = f"dev_{uuid.uuid4().hex[:20]}"
    label = " ".join((device_label or "Browser extension").split())[:80]
    conn.execute(
        "INSERT INTO extension_devices (id, account_id, user_id, label, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        (device_id, row["account_id"], row["user_id"], label, _iso(now), _iso(now)),
    )
    tokens = _issue_tokens(conn, device_id=device_id, family_id=f"fam_{uuid.uuid4().hex[:20]}", now=now)
    _audit(conn, "EXTENSION_DEVICE_PAIRED", principal_user=row["user_id"], account_id=row["account_id"],
           device_id=device_id, now=now, secret=secret, detail={"label": label})
    if row["user_id"]:
        user = identity.get_user(conn, row["user_id"])
        comms.enqueue(conn, category="SERVICE", template_id="security.new_device_paired",
                      to_address=user["email_normalized"], payload={"device_label": label},
                      account_id=row["account_id"], user_id=row["user_id"],
                      idempotency_key=f"security.new_device_paired:{device_id}", now=now)
    return PairResult(device_id, tokens.access_token, tokens.access_expires_at, tokens.refresh_token,
                      _account_label(conn, row["user_id"]))


# ---- tokens -------------------------------------------------------------------

def _revoke_device_rows(conn, device_id: str, *, reason: str, now: datetime) -> None:
    conn.execute("UPDATE extension_devices SET revoked_at = COALESCE(revoked_at, ?), "
                 "revoke_reason = COALESCE(revoke_reason, ?) WHERE id = ?", (_iso(now), reason, device_id))
    conn.execute("UPDATE extension_refresh_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE device_id = ?",
                 (_iso(now), device_id))
    conn.execute("DELETE FROM extension_access_tokens WHERE device_id = ?", (device_id,))


def refresh(conn: dbapi.Connection, *, device_id: str, raw_refresh: str, now: datetime,
            secret: str | None = None) -> TokenPair:
    row = conn.execute(
        "SELECT r.*, d.account_id, d.user_id, d.revoked_at AS device_revoked_at FROM extension_refresh_tokens r "
        "JOIN extension_devices d ON d.id = r.device_id WHERE r.token_hash = ?", (_hash(raw_refresh or ""),),
    ).fetchone()
    if row is None or row["device_id"] != device_id or row["device_revoked_at"] is not None:
        raise DeviceRevoked("device is not paired")
    if row["rotated_at"] is not None or row["revoked_at"] is not None:
        # A refresh token used twice: someone else holds a copy. Kill the device.
        _revoke_device_rows(conn, device_id, reason="TOKEN_REUSE", now=now)
        _audit(conn, "EXTENSION_TOKEN_REUSE", principal_user=row["user_id"], account_id=row["account_id"],
               device_id=device_id, now=now, secret=secret)
        notifications.notify(conn, account_id=row["account_id"], kind="security.token_reuse_detected",
                             subject_type="extension_device", subject_id=device_id,
                             dedupe_key=f"token_reuse:{device_id}", detail={}, now=now)
        conn.commit()
        raise DeviceRevoked("refresh token reuse detected")
    if _iso(now) > row["expires_at"]:
        raise DeviceRevoked("refresh token expired; pair again")
    rotated = conn.execute("UPDATE extension_refresh_tokens SET rotated_at = ? WHERE id = ? AND rotated_at IS NULL",
                           (_iso(now), row["id"]))
    if rotated.rowcount != 1:
        conn.rollback()
        return refresh(conn, device_id=device_id, raw_refresh=raw_refresh, now=now, secret=secret)
    conn.execute("UPDATE extension_devices SET last_seen_at = ? WHERE id = ?", (_iso(now), device_id))
    return _issue_tokens(conn, device_id=device_id, family_id=row["family_id"], now=now)


def resolve_access(conn: dbapi.Connection, raw_access: str | None, *, now: datetime) -> ExtensionPrincipal | None:
    if not raw_access:
        return None
    row = conn.execute(
        "SELECT t.expires_at, d.id AS device_id, d.user_id, d.account_id, d.revoked_at FROM extension_access_tokens t "
        "JOIN extension_devices d ON d.id = t.device_id WHERE t.token_hash = ?", (_hash(raw_access),),
    ).fetchone()
    if row is None or row["revoked_at"] is not None or _iso(now) > row["expires_at"]:
        return None
    return ExtensionPrincipal(row["device_id"], row["user_id"], row["account_id"])


def access_token_expired(conn: dbapi.Connection, raw_access: str, *, now: datetime) -> bool:
    row = conn.execute("SELECT expires_at FROM extension_access_tokens WHERE token_hash = ?",
                       (_hash(raw_access),)).fetchone()
    return row is not None and _iso(now) > row["expires_at"]


def revoke_device(conn: dbapi.Connection, *, device_id: str, reason: str, now: datetime,
                  secret: str | None = None) -> None:
    device = conn.execute("SELECT * FROM extension_devices WHERE id = ?", (device_id,)).fetchone()
    if device is None:
        return
    _revoke_device_rows(conn, device_id, reason=reason, now=now)
    _audit(conn, "EXTENSION_DEVICE_REVOKED", principal_user=device["user_id"], account_id=device["account_id"],
           device_id=device_id, now=now, secret=secret, detail={"reason": reason})


def revoke_all_devices_for_user(conn: dbapi.Connection, user_id: str, *, reason: str, now: datetime) -> int:
    devices = conn.execute("SELECT id FROM extension_devices WHERE user_id = ? AND revoked_at IS NULL",
                           (user_id,)).fetchall()
    for device in devices:
        revoke_device(conn, device_id=device["id"], reason=reason, now=now)
    return len(devices)


def list_devices(conn: dbapi.Connection, *, account_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id, label, created_at, last_seen_at FROM extension_devices WHERE account_id = ? AND revoked_at IS NULL "
        "ORDER BY created_at", (account_id,)).fetchall()]


# ---- handoff tickets ------------------------------------------------------------

def _ticket_mac(secret: str | None, *, ticket_id: str, nonce: str, account_id: str, user_id: str | None,
                workspace_id: str, purpose: str) -> str:
    message = f"v1.{ticket_id}.{nonce}.{account_id}.{user_id or '-'}.{workspace_id}.{purpose}"
    return hmac.new((secret or LOCAL_DEVELOPMENT_SECRET).encode(), message.encode(), hashlib.sha256).hexdigest()


def issue_handoff_ticket(conn: dbapi.Connection, *, account_id: str, user_id: str | None, workspace_id: str,
                         purpose: str, now: datetime, secret: str | None = None) -> str:
    ticket_id, nonce = uuid.uuid4().hex[:20], secrets.token_urlsafe(16)
    conn.execute(
        "INSERT INTO handoff_tickets (id, account_id, user_id, workspace_id, purpose, nonce_hash, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, account_id, user_id, workspace_id, purpose, _hash(nonce), _iso(now), _iso(now + HANDOFF_TICKET_TTL)),
    )
    mac = _ticket_mac(secret, ticket_id=ticket_id, nonce=nonce, account_id=account_id, user_id=user_id,
                      workspace_id=workspace_id, purpose=purpose)
    return f"v1.{ticket_id}.{nonce}.{mac}"


def consume_handoff_ticket(conn: dbapi.Connection, raw: str, *, principal: ExtensionPrincipal, workspace_id: str,
                           purpose: str, now: datetime, secret: str | None = None) -> None:
    parts = (raw or "").split(".")
    if len(parts) != 4 or parts[0] != "v1":
        raise TicketInvalid("malformed ticket")
    _, ticket_id, nonce, mac = parts
    row = conn.execute("SELECT * FROM handoff_tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row is None or not hmac.compare_digest(row["nonce_hash"], _hash(nonce)):
        raise TicketInvalid("unknown ticket")
    expected = _ticket_mac(secret, ticket_id=ticket_id, nonce=nonce, account_id=row["account_id"],
                           user_id=row["user_id"], workspace_id=row["workspace_id"], purpose=row["purpose"])
    if not hmac.compare_digest(expected, mac):
        raise TicketInvalid("ticket signature does not verify")
    if row["consumed_at"] is not None or _iso(now) > row["expires_at"]:
        raise TicketInvalid("ticket used or expired")
    if row["workspace_id"] != workspace_id or row["purpose"] != purpose:
        raise TicketInvalid("ticket was issued for something else")
    if row["account_id"] != principal.account_id or row["user_id"] != principal.user_id:
        raise AccountMismatch("the extension is signed in to a different account")
    consumed = conn.execute("UPDATE handoff_tickets SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                            (_iso(now), ticket_id))
    if consumed.rowcount != 1:
        raise TicketInvalid("ticket used or expired")
