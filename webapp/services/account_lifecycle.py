"""Account deletion requests (Bundle 7 spec O2, §20.4).

Requesting deletion ends access at once: every session and device is revoked
(the caller gets a fresh deletion-restricted session), the 6C queue is
paused, schedules are disabled, the automation kill switch is engaged and the
subscription is canceled at the provider immediately (no refund, DP-7). The
``account.purge`` job is queued at ``now + cooling_off`` (DP-4). During
cooling-off the user can keep the account: it returns to ACTIVE with the kill
switch still engaged (the user releases it)."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from webapp.persistence import dbapi, identity
from webapp.persistence.audit import audit

__all__ = ["DeletionRefused", "cancel_deletion", "request_deletion"]

logger = logging.getLogger("webapp.account_lifecycle")


class DeletionRefused(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def request_deletion(conn: dbapi.Connection, scope: Any, *, password: str, typed_email: str, now: datetime,
                     settings: Any, policy: Any, billing_provider: Any = None, ip: str | None = None,
                     user_agent: str | None = None) -> tuple[str, str]:
    """Commits. Returns the new deletion-restricted session (raw id, csrf token)."""
    from webapp.services.auth import CREDENTIAL_REVOCATION_HOOKS
    from webapp.services.autonomy_controls import engage_kill_switch_in_transaction
    from webapp.services.notifications import notify
    from webapp.services.passwords import verify_password
    from webapp.services.search_schedules import disable_for_account
    from webapp.services.sessions import SessionService
    from webapp.worker.runner import enqueue

    user = identity.get_user(conn, scope.user_id)
    stored = identity.get_password_hash(conn, scope.user_id)
    if user is None or not stored or not verify_password(stored, password or ""):
        raise DeletionRefused("That password is not right.")
    if " ".join((typed_email or "").split()).casefold() != user["email_normalized"]:
        raise DeletionRefused("Type your account email exactly to confirm.")
    account = conn.execute("SELECT status FROM accounts WHERE id = ?", (scope.account_id,)).fetchone()
    if account[0] not in ("ACTIVE", "SUSPENDED"):
        raise DeletionRefused("This account is already being deleted.")
    purge_after = now + policy.deletion_cooling_off
    live = conn.execute("SELECT provider_subscription_id FROM subscriptions WHERE account_id = ? "
                        "AND state NOT IN ('ENDED', 'INCOMPLETE_EXPIRED') ORDER BY updated_at DESC",
                        (scope.account_id,)).fetchone()
    sessions = SessionService(settings.secret_key)
    with dbapi.account_transaction(conn, scope.account_id):
        conn.execute("UPDATE accounts SET status = 'DELETION_REQUESTED', updated_at = ? WHERE id = ?",
                     (_iso(now), scope.account_id))
        conn.execute("UPDATE users SET status = 'DELETION_REQUESTED', updated_at = ? WHERE id = ?",
                     (_iso(now), scope.user_id))
        conn.execute("INSERT INTO account_deletions (account_id, requested_by, requested_at, purge_after) "
                     "VALUES (?, ?, ?, ?) ON CONFLICT (account_id) DO UPDATE SET requested_by = excluded.requested_by, "
                     "requested_at = excluded.requested_at, purge_after = excluded.purge_after, canceled_at = NULL",
                     (scope.account_id, scope.user_id, _iso(now), _iso(purge_after)))
        sessions.revoke_all_for_user(conn, scope.user_id, reason="DELETION_REQUESTED")
        for hook in CREDENTIAL_REVOCATION_HOOKS:
            hook(conn, user_id=scope.user_id, reason="DELETION_REQUESTED", now=now)
        session = sessions.create(conn, user_id=scope.user_id, kind="CUSTOMER", now=now, ip=ip,
                                  user_agent=user_agent)
        conn.execute("UPDATE autonomy_queue_items SET paused = 1, updated_at = ? WHERE account_id = ?",
                     (_iso(now), scope.account_id))
        disable_for_account(conn, scope.account_id, reason="USER", now=now)
        engage_kill_switch_in_transaction(conn, account_id=scope.account_id, actor=f"user:{scope.user_id}",
                                          reason="account deletion requested", now=now)
        notify(conn, account_id=scope.account_id, kind="account.deletion_requested", subject_type="account",
               subject_id=scope.account_id, dedupe_key=f"account.deletion_requested:{_iso(now)}",
               detail={"purge_after": purge_after.date().isoformat()}, now=now)
        enqueue(conn, kind="account.purge", account_id=scope.account_id, payload={"account_id": scope.account_id},
                run_at=purge_after, dedupe_key=f"purge:{scope.account_id}:{_iso(purge_after)}", now=now)
        audit(conn, actor_type="USER", actor_id=scope.user_id, account_id=scope.account_id,
              action="ACCOUNT_DELETION_REQUESTED", now=now, target_type="account", target_id=scope.account_id,
              detail={"purge_after": _iso(purge_after)}, ip=ip, secret=settings.secret_key)
    if live is not None and billing_provider is not None:  # DP-7: canceled at once, no refund
        try:
            billing_provider.cancel(subscription_id=live[0], immediately=True)
        except Exception:  # noqa: BLE001 - access has already ended; ops must finish the cancellation
            logger.error("billing_cancel_on_deletion_failed", extra={"account_id": scope.account_id,
                                                                     "subscription": live[0]}, exc_info=True)
    return session


def cancel_deletion(conn: dbapi.Connection, scope: Any, *, now: datetime, settings: Any) -> None:
    """Commits. Only during cooling-off (before the purge ran)."""
    row = conn.execute("SELECT a.status, d.completed_at FROM accounts a LEFT JOIN account_deletions d "
                       "ON d.account_id = a.id WHERE a.id = ?", (scope.account_id,)).fetchone()
    if row is None or row[0] != "DELETION_REQUESTED" or row[1] is not None:
        raise DeletionRefused("There is no deletion to cancel.")
    # A suspended user may ask for deletion; cancelling it restores the suspension, never lifts it.
    # Suspension and unsuspension are audited (append-only): the latest of the two decides.
    last = conn.execute("SELECT action FROM audit_log WHERE account_id = ? AND action IN "
                        "('ACCOUNT_SUSPENDED', 'ACCOUNT_UNSUSPENDED') ORDER BY seq DESC LIMIT 1",
                        (scope.account_id,)).fetchone()
    restored = "SUSPENDED" if last is not None and last[0] == "ACCOUNT_SUSPENDED" else "ACTIVE"
    with dbapi.account_transaction(conn, scope.account_id):
        conn.execute("UPDATE accounts SET status = ?, updated_at = ? WHERE id = ?",
                     (restored, _iso(now), scope.account_id))
        conn.execute("UPDATE users SET status = 'ACTIVE', updated_at = ? WHERE id = ?", (_iso(now), scope.user_id))
        conn.execute("UPDATE account_deletions SET canceled_at = ? WHERE account_id = ?", (_iso(now), scope.account_id))
        conn.execute("DELETE FROM jobs WHERE kind = 'account.purge' AND account_id = ? AND status = 'QUEUED'",
                     (scope.account_id,))
        audit(conn, actor_type="USER", actor_id=scope.user_id, account_id=scope.account_id,
              action="ACCOUNT_DELETION_CANCELED", now=now, target_type="account", target_id=scope.account_id,
              secret=settings.secret_key)
