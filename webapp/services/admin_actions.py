"""Staff actions (Bundle 7 spec §19.1). Every action checks its permission
(the routes check it first; this is the second line), and writes a STAFF
audit row in the same transaction as its change. Actions never read or
return candidate content."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from webapp.persistence import dbapi
from webapp.persistence.audit import audit
from webapp.services.staff_auth import StaffScope

__all__ = [
    "AdminActionRefused", "create_announcement", "create_grant", "kill_switch", "publish_announcement",
    "resend_verification", "retry_job", "retry_outbox", "revoke_grant", "revoke_sessions", "send_password_reset",
    "set_control", "set_staff_role", "suspend", "unsuspend", "withdraw_announcement",
]


class AdminActionRefused(Exception):
    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code, self.status, self.message = code, status, message


def _require(staff: StaffScope, permission: str) -> None:
    if permission not in staff.permissions:
        raise AdminActionRefused("PERMISSION_DENIED", 403, "Your staff role does not allow this.")


def _audit(conn, staff: StaffScope, action: str, *, account_id: str | None, now: datetime, settings: Any,
           target_type: str | None = None, target_id: str | None = None, detail: dict | None = None,
           request_id: str | None = None) -> None:
    audit(conn, actor_type="STAFF", actor_id=staff.user_id, account_id=account_id, action=action, now=now,
          target_type=target_type, target_id=target_id, request_id=request_id, detail=detail,
          secret=settings.secret_key)


def _owner(conn, account_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT u.* FROM account_memberships m JOIN users u ON u.id = m.user_id "
                       "JOIN accounts a ON a.id = m.account_id WHERE m.account_id = ? AND m.role = 'OWNER' "
                       "AND m.revoked_at IS NULL AND a.kind = 'candidate'", (account_id,)).fetchone()
    if row is None:
        raise AdminActionRefused("NOT_FOUND", 404, "No such customer account.")
    return dict(row)


def _account_status(conn, account_id: str) -> str:
    return conn.execute("SELECT status FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]


# ---- accounts.resend -------------------------------------------------------------------------

def resend_verification(conn, staff: StaffScope, account_id: str, *, settings: Any, now: datetime) -> None:
    _require(staff, "accounts.resend")
    from webapp.services.auth import AuthService
    owner = _owner(conn, account_id)
    AuthService(settings, clock=lambda: now).resend_verification(conn, email=owner["email_normalized"])
    _audit(conn, staff, "VERIFICATION_RESENT", account_id=account_id, now=now, settings=settings,
           target_type="user", target_id=owner["id"])
    conn.commit()


def send_password_reset(conn, staff: StaffScope, account_id: str, *, settings: Any, now: datetime) -> None:
    _require(staff, "accounts.resend")
    from webapp.services.auth import AuthService
    owner = _owner(conn, account_id)
    AuthService(settings, clock=lambda: now).request_password_reset(conn, email=owner["email_normalized"])
    _audit(conn, staff, "PASSWORD_RESET_SENT", account_id=account_id, now=now, settings=settings,
           target_type="user", target_id=owner["id"])
    conn.commit()


# ---- accounts.suspend ------------------------------------------------------------------------

def _revoke_credentials(conn, user_id: str, *, reason: str, now: datetime, settings: Any) -> None:
    from webapp.services.auth import CREDENTIAL_REVOCATION_HOOKS
    from webapp.services.sessions import SessionService
    SessionService(settings.secret_key).revoke_all_for_user(conn, user_id, reason=reason)
    for hook in CREDENTIAL_REVOCATION_HOOKS:
        hook(conn, user_id=user_id, reason=reason, now=now)


def suspend(conn, staff: StaffScope, account_id: str, *, reason: str, settings: Any, now: datetime) -> None:
    """ACTIVE → SUSPENDED: sessions and devices revoked, schedules disabled, the
    6C queue paused, the user told. Export and deletion keep working through
    the restricted page (§11.5)."""
    _require(staff, "accounts.suspend")
    from webapp.services.notifications import notify
    from webapp.services.search_schedules import on_account_suspended
    owner = _owner(conn, account_id)
    if _account_status(conn, account_id) != "ACTIVE":
        raise AdminActionRefused("INVALID_STATE", 409, "Only an active account can be suspended.")
    with dbapi.account_transaction(conn, account_id):
        conn.execute("UPDATE accounts SET status = 'SUSPENDED', updated_at = ? WHERE id = ?", (now.isoformat(),
                                                                                               account_id))
        _revoke_credentials(conn, owner["id"], reason="ACCOUNT_SUSPENDED", now=now, settings=settings)
        on_account_suspended(conn, account_id, now=now)
        notify(conn, account_id=account_id, kind="account.suspended", subject_type="account", subject_id=account_id,
               dedupe_key=f"account.suspended:{account_id}:{now.isoformat()}", detail={"reason": reason}, now=now)
        _audit(conn, staff, "ACCOUNT_SUSPENDED", account_id=account_id, now=now, settings=settings,
               target_type="account", target_id=account_id, detail={"reason": reason})


def unsuspend(conn, staff: StaffScope, account_id: str, *, reason: str, settings: Any, now: datetime) -> None:
    """SUSPENDED → ACTIVE. Schedules and the paused queue stay off until the user turns them on."""
    _require(staff, "accounts.suspend")
    _owner(conn, account_id)
    if _account_status(conn, account_id) != "SUSPENDED":
        raise AdminActionRefused("INVALID_STATE", 409, "The account is not suspended.")
    with dbapi.account_transaction(conn, account_id):
        conn.execute("UPDATE accounts SET status = 'ACTIVE', updated_at = ? WHERE id = ?", (now.isoformat(),
                                                                                            account_id))
        _audit(conn, staff, "ACCOUNT_UNSUSPENDED", account_id=account_id, now=now, settings=settings,
               target_type="account", target_id=account_id, detail={"reason": reason})


def revoke_sessions(conn, staff: StaffScope, account_id: str, *, settings: Any, now: datetime) -> None:
    _require(staff, "accounts.suspend")
    owner = _owner(conn, account_id)
    _revoke_credentials(conn, owner["id"], reason="STAFF_REVOKED", now=now, settings=settings)
    _audit(conn, staff, "SESSIONS_REVOKED", account_id=account_id, now=now, settings=settings,
           target_type="user", target_id=owner["id"])
    conn.commit()


# ---- accounts.kill_switch ---------------------------------------------------------------------

def kill_switch(conn, staff: StaffScope, account_id: str, *, engage: bool, reason: str, settings: Any,
                now: datetime) -> None:
    _require(staff, "accounts.kill_switch")
    from webapp.services.autonomy_controls import engage_kill_switch, release_kill_switch
    _owner(conn, account_id)
    actor = f"staff:{staff.user_id}"
    if engage:
        engage_kill_switch(conn, account_id=account_id, actor=actor, reason=reason, now=now)
    else:
        release_kill_switch(conn, account_id=account_id, actor=actor, reason=reason, now=now)
    _audit(conn, staff, "AUTONOMY_KILL_SWITCH_SET", account_id=account_id, now=now, settings=settings,
           target_type="account", target_id=account_id, detail={"engaged": engage, "reason": reason})
    conn.commit()


# ---- grants.manage ----------------------------------------------------------------------------

def create_grant(conn, staff: StaffScope, account_id: str, *, kind: str, plan_id: str | None, allowance: str | None,
                 amount: int | None, expires_at: datetime | None, reason: str, settings: Any, now: datetime) -> str:
    _require(staff, "grants.manage")
    from webapp.services.entitlements import add_grant
    _owner(conn, account_id)
    try:
        grant_id = add_grant(conn, account_id=account_id, kind=kind, reason=reason, actor_user_id=staff.user_id,
                             starts_at=now, expires_at=expires_at, now=now, plan_id=plan_id, allowance=allowance,
                             amount=amount)
    except dbapi.IntegrityError as exc:
        conn.rollback()
        raise AdminActionRefused("INVALID_GRANT", 400, "A plan override needs a plan and an expiry; a bonus needs "
                                                       "an allowance and a positive amount.") from exc
    _audit(conn, staff, "ENTITLEMENT_GRANT_CREATED", account_id=account_id, now=now, settings=settings,
           target_type="grant", target_id=grant_id,
           detail={"kind": kind, "plan_id": plan_id, "allowance": allowance, "amount": amount, "reason": reason})
    conn.commit()
    return grant_id


def revoke_grant(conn, staff: StaffScope, grant_id: str, *, settings: Any, now: datetime) -> None:
    _require(staff, "grants.manage")
    from webapp.services.entitlements import revoke_grant as revoke
    row = conn.execute("SELECT account_id FROM entitlement_grants WHERE id = ?", (grant_id,)).fetchone()
    if row is None:
        raise AdminActionRefused("NOT_FOUND", 404, "No such grant.")
    revoke(conn, grant_id, now=now)
    _audit(conn, staff, "ENTITLEMENT_GRANT_REVOKED", account_id=row[0], now=now, settings=settings,
           target_type="grant", target_id=grant_id)
    conn.commit()


# ---- ops.retry --------------------------------------------------------------------------------

def retry_job(conn, staff: StaffScope, job_id: str, *, settings: Any, now: datetime) -> None:
    _require(staff, "ops.retry")
    row = conn.execute("SELECT account_id, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None or row[1] != "DEAD":
        raise AdminActionRefused("INVALID_STATE", 409, "Only a dead job can be retried.")
    conn.execute("UPDATE jobs SET status = 'QUEUED', attempts = 0, run_at = ?, lease_holder = NULL, "
                 "lease_expires_at = NULL, finished_at = NULL, updated_at = ? WHERE id = ?",
                 (now.isoformat(), now.isoformat(), job_id))
    _audit(conn, staff, "DEAD_LETTER_RETRIED", account_id=row[0], now=now, settings=settings, target_type="job",
           target_id=job_id)
    conn.commit()


def retry_outbox(conn, staff: StaffScope, message_id: str, *, settings: Any, now: datetime) -> None:
    _require(staff, "ops.retry")
    row = conn.execute("SELECT account_id, status FROM outbound_messages WHERE id = ?", (message_id,)).fetchone()
    if row is None or row[1] != "FAILED":
        raise AdminActionRefused("INVALID_STATE", 409, "Only a failed message can be retried.")
    conn.execute("UPDATE outbound_messages SET status = 'QUEUED', attempts = 0, next_attempt_at = ?, last_error = NULL, "
                 "updated_at = ? WHERE id = ?", (now.isoformat(), now.isoformat(), message_id))
    _audit(conn, staff, "DEAD_LETTER_RETRIED", account_id=row[0], now=now, settings=settings,
           target_type="outbound_message", target_id=message_id)
    conn.commit()


# ---- announcements.manage ---------------------------------------------------------------------

def create_announcement(conn, staff: StaffScope, *, title: str, body_markdown: str, audience: str, severity: str,
                        expires_at: datetime | None, settings: Any, now: datetime) -> str:
    _require(staff, "announcements.manage")
    from webapp.services import announcements
    try:
        announcement_id = announcements.create(conn, title=title, body_markdown=body_markdown, audience=audience,
                                               severity=severity, expires_at=expires_at, created_by=staff.user_id,
                                               now=now)
    except ValueError as exc:
        raise AdminActionRefused("INVALID_ANNOUNCEMENT", 400, str(exc)) from exc
    conn.commit()
    return announcement_id


def publish_announcement(conn, staff: StaffScope, announcement_id: str, *, email: bool, settings: Any,
                         now: datetime) -> int:
    _require(staff, "announcements.manage")
    from webapp.services import announcements
    try:
        reached = announcements.publish(conn, announcement_id, email=email, settings=settings, now=now)
    except ValueError as exc:
        conn.rollback()
        raise AdminActionRefused("NOT_FOUND", 404, str(exc)) from exc
    _audit(conn, staff, "ANNOUNCEMENT_PUBLISHED", account_id=None, now=now, settings=settings,
           target_type="announcement", target_id=announcement_id, detail={"reached": reached, "email": email})
    conn.commit()
    return reached


def withdraw_announcement(conn, staff: StaffScope, announcement_id: str, *, settings: Any, now: datetime) -> None:
    _require(staff, "announcements.manage")
    from webapp.services import announcements
    announcements.withdraw(conn, announcement_id, now=now)
    _audit(conn, staff, "ANNOUNCEMENT_WITHDRAWN", account_id=None, now=now, settings=settings,
           target_type="announcement", target_id=announcement_id)
    conn.commit()


# ---- controls.manage --------------------------------------------------------------------------

def set_control(conn, staff: StaffScope, key: str, value: bool, *, reason: str, settings: Any,
                now: datetime) -> None:
    _require(staff, "controls.manage")
    from webapp.services.entitlements import PLATFORM_CONTROL_KEYS, set_platform_control
    if key not in PLATFORM_CONTROL_KEYS:
        raise AdminActionRefused("INVALID_CONTROL", 400, "Unknown platform control.")
    if not reason.strip():
        raise AdminActionRefused("INVALID_CONTROL", 400, "Say why the control changes.")
    set_platform_control(conn, key, value, actor_user_id=staff.user_id, reason=reason, now=now)
    _audit(conn, staff, "PLATFORM_CONTROL_SET", account_id=None, now=now, settings=settings,
           target_type="platform_control", target_id=key, detail={"value": value, "reason": reason})
    conn.commit()


# ---- staff.manage -----------------------------------------------------------------------------

def set_staff_role(conn, staff: StaffScope, user_id: str, role: str, *, grant: bool, reason: str, settings: Any,
                   now: datetime) -> None:
    _require(staff, "staff.manage")
    from webapp.services.staff_auth import ROLES, grant_role, revoke_role, staff_roles
    if role not in ROLES:
        raise AdminActionRefused("INVALID_ROLE", 400, "Unknown staff role.")
    if conn.execute("SELECT 1 FROM account_memberships WHERE user_id = ? AND revoked_at IS NULL",
                    (user_id,)).fetchone() is not None:
        raise AdminActionRefused("INVALID_ROLE", 400, "Staff users own no customer account.")
    if not grant and role == "ADMIN":
        admins = [u[0] for u in conn.execute("SELECT DISTINCT user_id FROM platform_role_assignments").fetchall()
                  if "ADMIN" in staff_roles(conn, u[0])]
        if admins == [user_id]:
            raise AdminActionRefused("INVALID_ROLE", 409, "The last admin cannot be removed.")
    (grant_role if grant else revoke_role)(conn, user_id=user_id, role=role, actor_user_id=staff.user_id,
                                           reason=reason, now=now)
    if not grant:  # a role change is a privilege change: the user's staff sessions end
        conn.execute("UPDATE web_sessions SET revoked_at = ?, revoke_reason = 'ROLE_CHANGED' WHERE user_id = ? "
                     "AND kind = 'STAFF' AND revoked_at IS NULL", (now.isoformat(), user_id))
    _audit(conn, staff, "STAFF_ROLE_CHANGED", account_id=None, now=now, settings=settings, target_type="user",
           target_id=user_id, detail={"role": role, "action": "GRANT" if grant else "REVOKE", "reason": reason})
    conn.commit()
