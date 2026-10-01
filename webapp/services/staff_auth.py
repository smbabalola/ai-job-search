"""Platform staff (Bundle 7 spec A6, §7, §19.1): roles, permissions, TOTP and
the re-authentication window.

Staff are dedicated users that own no customer account. Signing in takes the
password and then a TOTP code (enrolment is forced on the first sign-in);
only then is a ``STAFF`` session created, so every staff session is
TOTP-verified. Re-authenticating (password + TOTP again) rotates the session,
so the session's ``created_at`` is the time of the last full authentication:
destructive admin actions need it within ``REAUTH_WINDOW``.

The TOTP secret is encrypted at rest with AES-256-GCM under the hosted key
ring (``webapp.services.secret_box``), bound to the user id; the plaintext is
shown once, on the enrolment page, to the password-verified user, and is
never logged, audited or returned by an API."""
from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timedelta
from typing import Any, Mapping

import pyotp

from webapp.persistence import dbapi

__all__ = [
    "ADMIN_PERMISSIONS", "DESTRUCTIVE_PERMISSIONS", "PERMISSIONS", "REAUTH_WINDOW", "ROLES", "StaffScope",
    "create_staff_user", "grant_role", "has_confirmed_totp",
    "pending_token", "permissions_for", "provisioning_uri", "read_pending_token", "recently_authenticated",
    "revoke_role", "rotate_totp_secrets", "staff_roles", "start_totp_enrolment", "verify_totp",
]

ROLES = ("SUPPORT", "OPERATIONS", "BILLING", "ADMIN")
PERMISSIONS = ("accounts.view", "accounts.resend", "accounts.suspend", "accounts.kill_switch", "billing.view",
               "grants.manage", "ops.retry", "announcements.manage", "controls.manage", "staff.manage",
               "accounts.delete", "audit.all")

# §19.1 verbatim: role → permissions.
ADMIN_PERMISSIONS: Mapping[str, frozenset[str]] = {
    "SUPPORT": frozenset({"accounts.view", "accounts.resend"}),
    "OPERATIONS": frozenset({"accounts.view", "accounts.resend", "accounts.suspend", "accounts.kill_switch",
                             "ops.retry", "announcements.manage"}),
    "BILLING": frozenset({"accounts.view", "billing.view", "grants.manage"}),
    "ADMIN": frozenset(PERMISSIONS),
}
# Actions under these permissions change or remove access: they need a fresh authentication.
DESTRUCTIVE_PERMISSIONS = frozenset({"accounts.suspend", "accounts.kill_switch", "controls.manage", "staff.manage",
                                     "accounts.delete"})
REAUTH_WINDOW = timedelta(minutes=5)
PENDING_TTL = timedelta(minutes=5)
ISSUER = "JobSearch staff"
_DEV_KEY = "jobsearch-local-development-key"  # local mode without JOBSEARCH_SECRET_KEY only


class StaffScope:
    def __init__(self, *, user_id: str, roles: frozenset[str], session: Mapping[str, Any]) -> None:
        self.user_id = user_id
        self.roles = roles
        self.permissions = permissions_for(roles)
        self.session = session


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def permissions_for(roles: frozenset[str] | set[str]) -> frozenset[str]:
    out: set[str] = set()
    for role in roles:
        out |= ADMIN_PERMISSIONS.get(role, frozenset())
    return frozenset(out)


# ---- roles (append-only assignments; the latest action per role wins) ----------------------------

def staff_roles(conn: dbapi.Connection, user_id: str) -> frozenset[str]:
    latest: dict[str, str] = {}
    for row in conn.execute("SELECT role, action FROM platform_role_assignments WHERE user_id = ? ORDER BY seq",
                            (user_id,)).fetchall():
        latest[row[0]] = row[1]
    return frozenset(role for role, action in latest.items() if action == "GRANT")


def _assign(conn, *, user_id: str, role: str, action: str, actor_user_id: str | None, reason: str,
            now: datetime) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown staff role {role}")
    conn.execute("INSERT INTO platform_role_assignments (id, user_id, role, action, actor_user_id, reason, "
                 "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (f"pra_{uuid.uuid4().hex[:20]}", user_id, role, action, actor_user_id, reason, _iso(now)))


def grant_role(conn, *, user_id: str, role: str, actor_user_id: str | None, reason: str, now: datetime) -> None:
    """No commit."""
    _assign(conn, user_id=user_id, role=role, action="GRANT", actor_user_id=actor_user_id, reason=reason, now=now)


def revoke_role(conn, *, user_id: str, role: str, actor_user_id: str | None, reason: str, now: datetime) -> None:
    """No commit."""
    _assign(conn, user_id=user_id, role=role, action="REVOKE", actor_user_id=actor_user_id, reason=reason, now=now)


def create_staff_user(conn, *, email: str, password_hash: str, display_name: str, now: datetime) -> str:
    """No commit. A verified, ACTIVE user with a password and no account."""
    from webapp.persistence import identity
    email_normalized = identity.normalize_email(email)
    if identity.get_user_by_email(conn, email_normalized) is not None:
        raise ValueError("a user with that email already exists")
    user_id = f"user_{uuid.uuid4().hex[:20]}"
    conn.execute("INSERT INTO users (id, email_normalized, email_display, display_name, status, email_verified_at, "
                 "created_at, updated_at) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?)",
                 (user_id, email_normalized, email.strip(), display_name, _iso(now), _iso(now), _iso(now)))
    conn.execute("INSERT INTO user_identities (id, user_id, provider, secret_hash, created_at) "
                 "VALUES (?, ?, 'password', ?, ?)", (f"uid_{uuid.uuid4().hex[:20]}", user_id, password_hash, _iso(now)))
    return user_id


# ---- keys -----------------------------------------------------------------------------------------

def _signing_key(settings: Any) -> bytes:
    """HMAC key for the pending sign-in token (hosted mode validates JOBSEARCH_SECRET_KEY at startup)."""
    root = settings.secret_key or ("" if settings.is_hosted else _DEV_KEY)
    if not root:
        raise RuntimeError("JOBSEARCH_SECRET_KEY is required for staff sign-in")
    return hmac.new(root.encode(), b"staff-pending-signin", hashlib.sha256).digest()


def _seal(settings: Any, user_id: str, secret: str) -> str:
    from webapp.services import secret_box
    return secret_box.encrypt(settings, secret, purpose=f"staff_totp:{user_id}")


def _open(settings: Any, user_id: str, record: str) -> str:
    from webapp.services import secret_box
    return secret_box.decrypt(settings, record, purpose=f"staff_totp:{user_id}")


def rotate_totp_secrets(conn, settings: Any) -> int:
    """No commit. Re-encrypts every secret not under the active key (after a new key is put first)."""
    from webapp.services import secret_box
    rotated = 0
    for user_id, record in conn.execute("SELECT user_id, secret_encrypted FROM staff_totp").fetchall():
        if secret_box.needs_rotation(settings, record):
            conn.execute("UPDATE staff_totp SET secret_encrypted = ? WHERE user_id = ?",
                         (secret_box.reencrypt(settings, record, purpose=f"staff_totp:{user_id}"), user_id))
            rotated += 1
    return rotated


# ---- TOTP ---------------------------------------------------------------------------------------

def has_confirmed_totp(conn, user_id: str) -> bool:
    row = conn.execute("SELECT confirmed_at FROM staff_totp WHERE user_id = ?", (user_id,)).fetchone()
    return row is not None and row[0] is not None


def start_totp_enrolment(conn, settings: Any, user_id: str) -> str:
    """No commit. The secret of an unconfirmed enrolment (created once); a
    confirmed one is never shown again."""
    row = conn.execute("SELECT secret_encrypted, confirmed_at FROM staff_totp WHERE user_id = ?",
                       (user_id,)).fetchone()
    if row is not None and row[1] is not None:
        raise ValueError("TOTP is already enrolled")
    if row is not None:
        return _open(settings, user_id, row[0])
    secret = pyotp.random_base32()
    conn.execute("INSERT INTO staff_totp (user_id, secret_encrypted, confirmed_at) VALUES (?, ?, NULL)",
                 (user_id, _seal(settings, user_id, secret)))
    return secret


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def verify_totp(conn, settings: Any, user_id: str, code: str, *, now: datetime) -> bool:
    """No commit. The first valid code confirms the enrolment."""
    row = conn.execute("SELECT secret_encrypted, confirmed_at FROM staff_totp WHERE user_id = ?",
                       (user_id,)).fetchone()
    if row is None or not code or not code.strip().isdigit():
        return False
    if not pyotp.TOTP(_open(settings, user_id, row[0])).verify(code.strip(), for_time=now, valid_window=1):
        return False
    if row[1] is None:
        conn.execute("UPDATE staff_totp SET confirmed_at = ? WHERE user_id = ?", (_iso(now), user_id))
    return True


# ---- the password-verified, TOTP-pending step (a signed, short-lived cookie value) ----------------

def pending_token(settings: Any, user_id: str, *, now: datetime) -> str:
    mac = _signing_key(settings)
    body = f"{user_id}|{int((now + PENDING_TTL).timestamp())}"
    tag = hmac.new(mac, b"pending:" + body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}|{tag}".encode()).decode()


def read_pending_token(settings: Any, token: str | None, *, now: datetime) -> str | None:
    if not token:
        return None
    try:
        user_id, expires, tag = base64.urlsafe_b64decode(token.encode()).decode().split("|")
    except (ValueError, UnicodeDecodeError):
        return None
    mac = _signing_key(settings)
    expected = hmac.new(mac, f"pending:{user_id}|{expires}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(tag, expected) or int(expires) < now.timestamp():
        return None
    return user_id


def recently_authenticated(session: Mapping[str, Any], *, now: datetime) -> bool:
    return now - datetime.fromisoformat(session["created_at"]) <= REAUTH_WINDOW
