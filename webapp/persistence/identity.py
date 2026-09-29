"""Users, account memberships, account bootstrap and email tokens
(Bundle 7 spec §6). Nothing here commits: callers own the transaction."""
from __future__ import annotations

import hashlib
import secrets
import unicodedata
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from webapp.persistence import dbapi

USER_STATUSES = ("PENDING_VERIFICATION", "ACTIVE", "SUSPENDED", "DELETION_REQUESTED", "PURGED")
EMAIL_TOKEN_TTL = {
    "VERIFY_EMAIL": timedelta(hours=48),
    "PASSWORD_RESET": timedelta(hours=1),
    "EMAIL_CHANGE": timedelta(hours=24),
}
MAX_EMAIL_LENGTH = 254
CANDIDATE_SOURCE = ".claude/skills/job-application-assistant/01-candidate-profile.md"

# Called inside sign-up's transaction as hook(conn, account_id=, user_id=, now=).
# Later Bundle 7 tasks register the account's default documents here
# (standing policy v2, CV strategy, onboarding state).
ACCOUNT_BOOTSTRAP_HOOKS: list[Callable[..., None]] = []


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_email(raw: str) -> str:
    if not isinstance(raw, str):
        raise ValueError("email is required")
    email = unicodedata.normalize("NFKC", raw).strip().casefold()
    local, at, domain = email.partition("@")
    if not at or not local or not domain or "@" in domain or any(c.isspace() for c in email):
        raise ValueError("email address is not valid")
    if len(email) > MAX_EMAIL_LENGTH:
        raise ValueError("email address is too long")
    return email


def get_user(conn: dbapi.Connection, user_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(conn: dbapi.Connection, email_normalized: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE email_normalized = ?", (email_normalized,)).fetchone()
    return dict(row) if row else None


def get_password_hash(conn: dbapi.Connection, user_id: str) -> str | None:
    row = conn.execute(
        "SELECT secret_hash FROM user_identities WHERE user_id = ? AND provider = 'password' AND revoked_at IS NULL",
        (user_id,),
    ).fetchone()
    return row["secret_hash"] if row else None


def get_owner_account(conn: dbapi.Connection, user_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT a.* FROM account_memberships m JOIN accounts a ON a.id = m.account_id "
        "WHERE m.user_id = ? AND m.role = 'OWNER' AND m.revoked_at IS NULL",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


def set_user_status(conn: dbapi.Connection, user_id: str, status: str, *, now: datetime) -> None:
    if status not in USER_STATUSES:
        raise ValueError(f"unknown user status {status!r}")
    conn.execute("UPDATE users SET status = ?, updated_at = ? WHERE id = ?", (status, _iso(now), user_id))


def set_password_hash(conn: dbapi.Connection, user_id: str, password_hash: str, *, now: datetime) -> None:
    conn.execute(
        "UPDATE user_identities SET revoked_at = ? WHERE user_id = ? AND provider = 'password' AND revoked_at IS NULL",
        (_iso(now), user_id),
    )
    conn.execute(
        "INSERT INTO user_identities (id, user_id, provider, secret_hash, created_at) VALUES (?, ?, 'password', ?, ?)",
        (f"uid_{uuid.uuid4().hex[:20]}", user_id, password_hash, _iso(now)),
    )
    conn.execute("UPDATE users SET password_changed_at = ?, updated_at = ? WHERE id = ?",
                 (_iso(now), _iso(now), user_id))


def create_user_with_account(
    conn: dbapi.Connection, *, email: str, password_hash: str, display_name: str,
    legal_document_ids: list[str], now: datetime, profile_store: Any,
) -> dict[str, Any]:
    """Spec §6.3: user, password identity, candidate account, OWNER membership,
    profile and default search workspaces, initial profile source, legal
    acceptances, bootstrap hooks and a VERIFY_EMAIL token. No commit."""
    from webapp.persistence.accounts import create_account
    from webapp.persistence.search_workspaces import create_search_workspace
    from webapp.persistence.workspaces import ensure_profile_workspace
    from webapp.services.profile_setup import render_basic_profile

    email_normalized = normalize_email(email)
    name = " ".join((display_name or "").split())
    if not name:
        raise ValueError("display name is required")
    user_id = f"user_{uuid.uuid4().hex[:20]}"
    stamp = _iso(now)
    conn.execute(
        "INSERT INTO users (id, email_normalized, email_display, display_name, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'PENDING_VERIFICATION', ?, ?)",
        (user_id, email_normalized, email.strip(), name, stamp, stamp),
    )
    conn.execute(
        "INSERT INTO user_identities (id, user_id, provider, secret_hash, created_at) VALUES (?, ?, 'password', ?, ?)",
        (f"uid_{uuid.uuid4().hex[:20]}", user_id, password_hash, stamp),
    )
    account = create_account(conn, display_name=name, commit=False)
    conn.execute(
        "INSERT INTO account_memberships (account_id, user_id, role, created_at) VALUES (?, ?, 'OWNER', ?)",
        (account["id"], user_id, stamp),
    )
    ensure_profile_workspace(conn, account_id=account["id"], commit=False)
    create_search_workspace(conn, name="Default search", account_id=account["id"], commit=False)
    profile_store.for_account(conn, account["id"]).write(CANDIDATE_SOURCE, render_basic_profile({"name": name}))
    for document_id in legal_document_ids:
        conn.execute(
            "INSERT INTO legal_acceptances (id, user_id, legal_document_id, accepted_at) VALUES (?, ?, ?, ?)",
            (f"lacc_{uuid.uuid4().hex[:20]}", user_id, document_id, stamp),
        )
    for hook in ACCOUNT_BOOTSTRAP_HOOKS:
        hook(conn, account_id=account["id"], user_id=user_id, now=now)
    verify_token = issue_email_token(conn, user_id=user_id, purpose="VERIFY_EMAIL", now=now)
    return {"user": get_user(conn, user_id), "account": dict(
        conn.execute("SELECT * FROM accounts WHERE id = ?", (account["id"],)).fetchone()), "verify_token": verify_token}


def issue_email_token(conn: dbapi.Connection, *, user_id: str, purpose: str, now: datetime,
                      new_email: str | None = None) -> str:
    if purpose not in EMAIL_TOKEN_TTL:
        raise ValueError(f"unknown email token purpose {purpose!r}")
    if (purpose == "EMAIL_CHANGE") != (new_email is not None):
        raise ValueError("an email-change token needs exactly the new address")
    raw = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO email_tokens (id, user_id, purpose, token_hash, new_email_normalized, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (f"etok_{uuid.uuid4().hex[:20]}", user_id, purpose, token_hash(raw),
         normalize_email(new_email) if new_email is not None else None,
         _iso(now), _iso(now + EMAIL_TOKEN_TTL[purpose])),
    )
    return raw


def consume_email_token(conn: dbapi.Connection, *, raw: str, purpose: str, now: datetime) -> dict[str, Any] | None:
    """Marks the token used and returns it, or None when unknown, used,
    expired or issued for another purpose. Single use even under races: the
    consuming UPDATE is conditional."""
    row = conn.execute(
        "SELECT * FROM email_tokens WHERE token_hash = ? AND purpose = ?", (token_hash(raw or ""), purpose)
    ).fetchone()
    if row is None or row["consumed_at"] is not None or _iso(now) > row["expires_at"]:
        return None
    updated = conn.execute(
        "UPDATE email_tokens SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL", (_iso(now), row["id"])
    )
    if updated.rowcount != 1:
        return None
    return {**dict(row), "consumed_at": _iso(now)}
