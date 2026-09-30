"""Sign-up, verification, login, password reset, email change and logout
(Bundle 7 spec §7).

Enumeration resistance: sign-up, reset requests and verification resends give
the same result whether or not the email exists; a login miss runs a full
argon2 verification against a dummy hash, exactly like a hit."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from webapp import comms
from webapp.persistence import dbapi, identity
from webapp.persistence.audit import audit
from webapp.services.entitlements import platform_control
from webapp.services.passwords import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    password_problems,
    verify_password,
)
from webapp.services.sessions import SessionService
from webapp.storage.profile_sources import profile_source_store_from_settings

LOGIN_STATUSES = frozenset({"PENDING_VERIFICATION", "ACTIVE", "SUSPENDED", "DELETION_REQUESTED"})
PASSWORD_MESSAGES = {
    "too_short": "Use at least 12 characters.",
    "too_long": "Use at most 128 characters.",
    "too_common": "That password is too common. Choose something less guessable.",
}

# Called with (conn, user_id=, reason=, now=) whenever all of a user's
# credentials must die (password change/reset). Task 11 registers extension
# device revocation here.
CREDENTIAL_REVOCATION_HOOKS: list[Callable[..., None]] = []


class SignupUnavailable(Exception):
    """SIGNUP_UNAVAILABLE: no published legal documents (DP-5) or sign-ups closed."""


@dataclass
class Outcome:
    ok: bool
    errors: list[str] = field(default_factory=list)
    session: tuple[str, str] | None = None  # (raw session id, csrf token)
    user: dict[str, Any] | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def latest_legal_documents(conn: dbapi.Connection) -> dict[str, str]:
    """kind -> id of the latest published version (TERMS, PRIVACY)."""
    latest: dict[str, str] = {}
    for row in conn.execute(
        "SELECT id, kind FROM legal_documents WHERE published_at <= ? ORDER BY kind, published_at, version",
        (_now().isoformat(),),
    ).fetchall():
        latest[row["kind"]] = row["id"]
    return latest


class AuthService:
    def __init__(self, settings: Any, *, clock: Callable[[], datetime] = _now, request_id: str | None = None) -> None:
        self.settings = settings
        self.sessions = SessionService(settings.secret_key)
        self.clock = clock
        self.request_id = request_id

    def _audit(self, conn, action: str, *, user_id: str | None, ip: str | None = None,
               detail: dict | None = None) -> None:
        account = identity.get_owner_account(conn, user_id) if user_id else None
        audit(conn, actor_type="USER", actor_id=user_id, account_id=account["id"] if account else None,
              action=action, target_type="user" if user_id else None, target_id=user_id,
              request_id=self.request_id, ip=ip, detail=detail, now=self.clock(), secret=self.settings.secret_key)

    # ---- helpers --------------------------------------------------------
    def _link(self, path: str, token: str) -> str:
        return f"{self.settings.app_origin}{path}?token={token}"

    def _mail(self, conn, *, template_id: str, to: str, user_id: str | None, payload: dict, category="SERVICE"):
        comms.enqueue(conn, category=category, template_id=template_id, to_address=to, payload=payload,
                      user_id=user_id, idempotency_key=f"{template_id}:{uuid.uuid4().hex}", now=self.clock())

    def _security_notice(self, conn, kind: str, user_id: str) -> None:
        """Bundle 7 §17.2: in-app security notification (its service mail is sent by the flow itself)."""
        from webapp.services.notifications import notify
        account = identity.get_owner_account(conn, user_id)
        if account is not None:
            now = self.clock()
            notify(conn, account_id=account["id"], kind=kind, subject_type="user", subject_id=user_id,
                   dedupe_key=f"{kind}:{user_id}:{now.isoformat()}", detail={}, now=now)

    def _start_session(self, conn, user: dict, *, ip: str | None, user_agent: str | None) -> tuple[str, str]:
        now = self.clock()
        session = self.sessions.create(conn, user_id=user["id"], kind="CUSTOMER", now=now, ip=ip,
                                       user_agent=user_agent)
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now.isoformat(), user["id"]))
        return session

    def _revoke_everything(self, conn, user_id: str, *, reason: str, keep_session: str | None = None) -> None:
        self.sessions.revoke_all_for_user(conn, user_id, reason=reason, except_session_id=keep_session)
        for hook in CREDENTIAL_REVOCATION_HOOKS:
            hook(conn, user_id=user_id, reason=reason, now=self.clock())

    # ---- sign-up and verification -----------------------------------------
    def signup(self, conn, *, email: str, password: str, display_name: str,
               accepted_legal_ids: list[str], ip: str | None = None) -> Outcome:
        legal = latest_legal_documents(conn)
        if set(legal) != {"TERMS", "PRIVACY"}:
            raise SignupUnavailable("no published Terms and Privacy Notice")
        if not platform_control(conn, "SIGNUPS_ENABLED", settings=self.settings):
            raise SignupUnavailable("sign-ups are closed")
        errors: list[str] = []
        try:
            normalized = identity.normalize_email(email)
        except ValueError:
            errors.append("Enter a valid email address.")
            normalized = None
        if not " ".join((display_name or "").split()):
            errors.append("Enter your name.")
        errors.extend(PASSWORD_MESSAGES[p] for p in password_problems(password))
        if set(accepted_legal_ids) != set(legal.values()):
            errors.append("Please accept the Terms and the Privacy Notice.")
        if errors:
            return Outcome(False, errors)
        existing = identity.get_user_by_email(conn, normalized)
        if existing is not None:
            self._mail(conn, template_id="auth.account_exists", to=existing["email_normalized"],
                       user_id=existing["id"], payload={"login_url": f"{self.settings.app_origin}/login",
                                                        "reset_url": f"{self.settings.app_origin}/reset-password"})
            conn.commit()
            return Outcome(True)
        created = identity.create_user_with_account(
            conn, email=email, password_hash=hash_password(password), display_name=display_name,
            legal_document_ids=sorted(legal.values()), now=self.clock(),
            profile_store=profile_source_store_from_settings(self.settings))
        self._mail(conn, template_id="auth.verify_email", to=normalized, user_id=created["user"]["id"],
                   payload={"token": created["verify_token"],
                            "verify_url": self._link("/verify-email", created["verify_token"])})
        conn.commit()  # the account and its verification mail commit together (§18.2)
        return Outcome(True, user=created["user"])

    def verify_email(self, conn, *, token: str) -> Outcome:
        now = self.clock()
        row = identity.consume_email_token(conn, raw=token, purpose="VERIFY_EMAIL", now=now)
        if row is None:
            conn.rollback()
            return Outcome(False, ["This verification link is invalid or has expired."])
        conn.execute("UPDATE users SET email_verified_at = COALESCE(email_verified_at, ?), updated_at = ?, "
                     "status = CASE WHEN status = 'PENDING_VERIFICATION' THEN 'ACTIVE' ELSE status END "
                     "WHERE id = ?", (now.isoformat(), now.isoformat(), row["user_id"]))
        self._audit(conn, "EMAIL_VERIFIED", user_id=row["user_id"])
        conn.commit()
        return Outcome(True, user=identity.get_user(conn, row["user_id"]))

    def resend_verification(self, conn, *, email: str) -> None:
        try:
            user = identity.get_user_by_email(conn, identity.normalize_email(email))
        except ValueError:
            return
        if user is None or user["email_verified_at"] is not None:
            return
        token = identity.issue_email_token(conn, user_id=user["id"], purpose="VERIFY_EMAIL", now=self.clock())
        self._mail(conn, template_id="auth.verify_email", to=user["email_normalized"], user_id=user["id"],
                   payload={"token": token, "verify_url": self._link("/verify-email", token)})
        conn.commit()

    # ---- login and logout ------------------------------------------------------
    def login(self, conn, *, email: str, password: str, ip: str | None = None,
              user_agent: str | None = None) -> Outcome:
        try:
            user = identity.get_user_by_email(conn, identity.normalize_email(email))
        except ValueError:
            user = None
        stored = identity.get_password_hash(conn, user["id"]) if user else None
        matched = verify_password(stored or DUMMY_PASSWORD_HASH, password or "")
        if not (user and stored and matched and user["status"] in LOGIN_STATUSES):
            email_hash = hashlib.sha256(" ".join((email or "").split()).casefold().encode()).hexdigest()
            self._audit(conn, "LOGIN_FAILED", user_id=user["id"] if user else None, ip=ip,
                        detail={"email_sha256": email_hash, "known_account": user is not None})
            conn.commit()
            return Outcome(False, ["That email and password don't match an account."])
        session = self._start_session(conn, user, ip=ip, user_agent=user_agent)
        self._audit(conn, "LOGIN_SUCCEEDED", user_id=user["id"], ip=ip)
        conn.commit()
        return Outcome(True, session=session, user=user)

    def logout(self, conn, *, raw_session: str | None) -> None:
        if raw_session:
            session = self.sessions.resolve(conn, raw_session, now=self.clock())
            self.sessions.revoke(conn, raw_session, reason="LOGOUT")
            if session:
                self._audit(conn, "LOGOUT", user_id=session["user_id"])
            conn.commit()

    def revoke_all(self, conn, *, user_id: str) -> None:
        self._revoke_everything(conn, user_id, reason="SIGN_OUT_EVERYWHERE")
        self._audit(conn, "SESSIONS_REVOKED", user_id=user_id)
        conn.commit()

    # ---- passwords ---------------------------------------------------------------
    def request_password_reset(self, conn, *, email: str) -> None:
        try:
            user = identity.get_user_by_email(conn, identity.normalize_email(email))
        except ValueError:
            return
        if user is None or user["status"] == "PURGED":
            return
        token = identity.issue_email_token(conn, user_id=user["id"], purpose="PASSWORD_RESET", now=self.clock())
        self._mail(conn, template_id="auth.password_reset", to=user["email_normalized"], user_id=user["id"],
                   payload={"token": token, "reset_url": self._link("/reset-password/confirm", token)})
        conn.commit()

    def confirm_password_reset(self, conn, *, token: str, password: str, ip: str | None = None,
                               user_agent: str | None = None) -> Outcome:
        problems = password_problems(password)
        if problems:
            return Outcome(False, [PASSWORD_MESSAGES[p] for p in problems])
        now = self.clock()
        row = identity.consume_email_token(conn, raw=token, purpose="PASSWORD_RESET", now=now)
        if row is None:
            conn.rollback()
            return Outcome(False, ["This reset link is invalid or has expired."])
        user = identity.get_user(conn, row["user_id"])
        identity.set_password_hash(conn, user["id"], hash_password(password), now=now)
        self._revoke_everything(conn, user["id"], reason="PASSWORD_RESET")
        session = self._start_session(conn, user, ip=ip, user_agent=user_agent)
        self._audit(conn, "PASSWORD_RESET", user_id=user["id"], ip=ip)
        self._security_notice(conn, "security.password_changed", user["id"])
        self._mail(conn, template_id="auth.password_changed", to=user["email_normalized"], user_id=user["id"],
                   payload={})
        conn.commit()
        return Outcome(True, session=session, user=user)

    def change_password(self, conn, *, user_id: str, current_password: str, new_password: str,
                        keep_session: str | None) -> Outcome:
        stored = identity.get_password_hash(conn, user_id)
        if not verify_password(stored or DUMMY_PASSWORD_HASH, current_password or "") or stored is None:
            return Outcome(False, ["Your current password is not correct."])
        problems = password_problems(new_password)
        if problems:
            return Outcome(False, [PASSWORD_MESSAGES[p] for p in problems])
        now = self.clock()
        identity.set_password_hash(conn, user_id, hash_password(new_password), now=now)
        self._revoke_everything(conn, user_id, reason="PASSWORD_CHANGED", keep_session=keep_session)
        self._audit(conn, "PASSWORD_CHANGED", user_id=user_id)
        self._security_notice(conn, "security.password_changed", user_id)
        user = identity.get_user(conn, user_id)
        self._mail(conn, template_id="auth.password_changed", to=user["email_normalized"], user_id=user_id,
                   payload={})
        conn.commit()
        return Outcome(True, user=user)

    # ---- email change ----------------------------------------------------------
    def request_email_change(self, conn, *, user_id: str, new_email: str, password: str) -> Outcome:
        stored = identity.get_password_hash(conn, user_id)
        if not verify_password(stored or DUMMY_PASSWORD_HASH, password or "") or stored is None:
            return Outcome(False, ["Your password is not correct."])
        try:
            normalized = identity.normalize_email(new_email)
        except ValueError:
            return Outcome(False, ["Enter a valid email address."])
        user = identity.get_user(conn, user_id)
        if normalized == user["email_normalized"]:
            return Outcome(False, ["That is already your email address."])
        token = identity.issue_email_token(conn, user_id=user_id, purpose="EMAIL_CHANGE", now=self.clock(),
                                           new_email=normalized)
        self._mail(conn, template_id="auth.email_change_confirm", to=normalized, user_id=user_id,
                   payload={"token": token, "confirm_url": self._link("/email-change/confirm", token)})
        self._mail(conn, template_id="auth.email_change_notice", to=user["email_normalized"], user_id=user_id,
                   payload={"new_email_hint": normalized[:2] + "…@" + normalized.split("@", 1)[1]})
        conn.commit()
        return Outcome(True)

    def confirm_email_change(self, conn, *, token: str) -> Outcome:
        now = self.clock()
        row = identity.consume_email_token(conn, raw=token, purpose="EMAIL_CHANGE", now=now)
        if row is None:
            conn.rollback()
            return Outcome(False, ["This confirmation link is invalid or has expired."])
        try:
            conn.execute("UPDATE users SET email_normalized = ?, email_display = ?, updated_at = ? WHERE id = ?",
                         (row["new_email_normalized"], row["new_email_normalized"], now.isoformat(), row["user_id"]))
        except dbapi.IntegrityError:
            conn.rollback()
            return Outcome(False, ["That email address is already in use."])
        self._audit(conn, "EMAIL_CHANGED", user_id=row["user_id"])
        self._security_notice(conn, "security.email_changed", row["user_id"])
        conn.commit()
        return Outcome(True, user=identity.get_user(conn, row["user_id"]))


def _revoke_extension_devices(conn, *, user_id: str, reason: str, now: datetime) -> None:
    from webapp.services.extension_auth import revoke_all_devices_for_user

    revoke_all_devices_for_user(conn, user_id, reason=reason, now=now)


CREDENTIAL_REVOCATION_HOOKS.append(_revoke_extension_devices)
