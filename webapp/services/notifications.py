"""The unified notification log (Bundle 7 spec §17).

``notify`` writes a notification (deduplicated per account) and, where the
category's email mode says so, enqueues the email in the same transaction; it
never commits. SECURITY, BILLING and ACCOUNT mail is always immediate; the
configurable categories default per §17.1; DAILY_DIGEST notifications are
collected by the worker's ``notify.digest`` job at 07:00 in the account's
timezone; a challenge handoff never emails. Kinds whose producer already
sends its own dedicated service mail (password and email changes) are in-app
only here, so nothing is sent twice.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from webapp.persistence import dbapi
from webapp.persistence.bundle7_migrations import SIXC_KIND_MAP

__all__ = [
    "CONFIGURABLE", "DEFAULT_MODES", "NOTIFICATION_KINDS", "archive", "email_modes", "list_notifications",
    "mark_read", "notify", "scan_expiring_approvals", "send_digests", "set_email_mode",
]

logger = logging.getLogger("webapp.notifications")

NOTIFICATION_KINDS: dict[str, str] = {
    "application.prepared": "OUTCOME",
    "application.review_required": "ACTION_REQUIRED",
    "application.approval_expiring": "ACTION_REQUIRED",
    "application.blocker_needs_answer": "ACTION_REQUIRED",
    "application.automation_blocked": "OUTCOME",
    "fill.completed_awaiting_submit": "ACTION_REQUIRED",
    "fill.failed": "OUTCOME",
    "submit.confirmed": "OUTCOME",
    "submit.ambiguous": "ACTION_REQUIRED",
    "submit.failed": "OUTCOME",
    "submit.challenge_handoff": "ACTION_REQUIRED",
    "discovery.new_matches": "DISCOVERY",
    "usage.limit_near": "USAGE",
    "usage.limit_reached": "USAGE",
    "billing.payment_failed": "BILLING",
    "billing.subscription_changed": "BILLING",
    "billing.subscription_canceled": "BILLING",
    "security.new_device_paired": "SECURITY",
    "security.token_reuse_detected": "SECURITY",
    "security.password_changed": "SECURITY",
    "security.email_changed": "SECURITY",
    "account.suspended": "ACCOUNT",
    "account.deletion_requested": "ACCOUNT",
    "account.data_export_ready": "ACCOUNT",
    "announcement.published": "ANNOUNCEMENT",
}
SEVERITY: dict[str, str] = {
    "security.token_reuse_detected": "CRITICAL", "account.suspended": "CRITICAL",
    "billing.payment_failed": "WARNING", "submit.ambiguous": "WARNING", "submit.failed": "WARNING",
    "fill.failed": "WARNING", "usage.limit_reached": "WARNING", "application.approval_expiring": "WARNING",
    "application.automation_blocked": "WARNING", "account.deletion_requested": "WARNING",
    "security.new_device_paired": "WARNING", "submit.challenge_handoff": "WARNING",
}
TITLES: dict[str, str] = {
    "application.prepared": "Your application is prepared",
    "application.review_required": "An application is ready for your review",
    "application.approval_expiring": "An approval expires soon",
    "application.blocker_needs_answer": "An application needs your answer",
    "application.automation_blocked": "Automation could not finish an application",
    "fill.completed_awaiting_submit": "The form is filled and waiting for you to submit",
    "fill.failed": "Filling the form stopped",
    "submit.confirmed": "Your application was submitted",
    "submit.ambiguous": "Tell us whether your application went through",
    "submit.failed": "Your application was not submitted",
    "submit.challenge_handoff": "Verification needed on the employer's page",
    "discovery.new_matches": "New jobs match your search",
    "usage.limit_near": "You've used most of your allowance",
    "usage.limit_reached": "You've used all of your allowance",
    "billing.payment_failed": "Your payment didn't go through",
    "billing.subscription_changed": "Your plan changed",
    "billing.subscription_canceled": "Your subscription was canceled",
    "security.new_device_paired": "A browser extension was connected",
    "security.token_reuse_detected": "We signed out a browser extension",
    "security.password_changed": "Your password was changed",
    "security.email_changed": "Your account email changed",
    "account.suspended": "Your account was suspended",
    "account.deletion_requested": "Your account is scheduled for deletion",
    "account.data_export_ready": "Your data export is ready",
    "announcement.published": "A new announcement",
}
CONFIGURABLE = ("ACTION_REQUIRED", "OUTCOME", "USAGE", "DISCOVERY")
DEFAULT_MODES = {"ACTION_REQUIRED": "IMMEDIATE", "OUTCOME": "IMMEDIATE", "USAGE": "IMMEDIATE",
                 "DISCOVERY": "DAILY_DIGEST"}
ALWAYS_IMMEDIATE = frozenset({"SECURITY", "BILLING", "ACCOUNT"})
MODES = ("IMMEDIATE", "DAILY_DIGEST", "OFF")
# In-app only: time-critical (no email), or the producer sends its own service mail.
NO_EMAIL_KINDS = frozenset({"submit.challenge_handoff", "security.password_changed", "security.email_changed"})
# Kinds with a dedicated template of the same id; everything else uses notify.immediate.
DEDICATED_TEMPLATES = frozenset({
    "billing.payment_failed", "billing.subscription_changed", "billing.subscription_canceled",
    "security.new_device_paired", "security.token_reuse_detected", "account.suspended",
    "account.deletion_requested", "account.data_export_ready", "usage.limit_near", "usage.limit_reached",
})
DIGEST_HOUR = 7
ALLOWANCE_LABELS = {"applications.prepare": "prepares", "discovery.on_demand_runs": "discovery searches",
                    "profile.cv_import": "CV imports", "cv.tailor": "tailored CVs"}


def ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _href(subject_type: str, subject_id: str, kind: str) -> str:
    if subject_type == "workspace":
        return f"/workspaces/{subject_id}"
    if subject_type == "subscription":
        return "/settings/billing"
    if subject_type in ("extension_device", "user"):
        return "/settings/security"
    if subject_type == "search_workspace":
        return f"/search-workspaces/{subject_id}/discover"
    if kind.startswith("usage."):
        return "/settings/usage"
    return "/inbox"


def _owner_email(conn: dbapi.Connection, account_id: str) -> str | None:
    row = conn.execute("SELECT u.email_normalized FROM account_memberships m JOIN users u ON u.id = m.user_id "
                       "WHERE m.account_id = ? AND m.role = 'OWNER' AND u.status NOT IN ('PURGED', 'DELETED') "
                       "ORDER BY m.created_at LIMIT 1", (account_id,)).fetchone()
    return None if row is None else row[0]


def email_modes(conn: dbapi.Connection, account_id: str) -> dict[str, str]:
    modes = dict(DEFAULT_MODES)
    for row in conn.execute("SELECT category, email_mode FROM notification_preferences WHERE account_id = ?",
                            (account_id,)):
        modes[row[0]] = row[1]
    return modes


def set_email_mode(conn: dbapi.Connection, *, account_id: str, category: str, mode: str, now: datetime) -> None:
    """No commit. SECURITY, BILLING, ACCOUNT (always immediate) and
    ANNOUNCEMENT are not configurable."""
    if category not in CONFIGURABLE:
        raise ValueError(f"{category} notifications are not configurable")
    if mode not in MODES:
        raise ValueError(f"unknown email mode {mode}")
    conn.execute("INSERT INTO notification_preferences (account_id, category, email_mode, updated_at) "
                 "VALUES (?, ?, ?, ?) ON CONFLICT (account_id, category) DO UPDATE SET "
                 "email_mode = excluded.email_mode, updated_at = excluded.updated_at",
                 (account_id, category, mode, ts(now)))


def _payload(kind: str, detail: Mapping[str, Any], href: str, app_origin: str) -> dict[str, Any]:
    plan = detail.get("plan_name") or str(detail.get("to_plan") or detail.get("plan_id") or "your").title()
    allowance = detail.get("allowance")
    return {
        "title": TITLES[kind], "body": detail.get("message") or TITLES[kind] + ".",
        "action_url": f"{app_origin}{href}", "plan_name": plan, "device_label": detail.get("device_label", "extension"),
        "reason": detail.get("reason", "see your account"), "allowance_label": ALLOWANCE_LABELS.get(allowance, allowance),
        **{k: v for k, v in detail.items() if isinstance(v, (str, int, float, bool)) or v is None},
    }


def _fan_out(conn: dbapi.Connection, *, notification_id: str, account_id: str, kind: str, category: str,
             href: str, detail: Mapping[str, Any], now: datetime) -> None:
    if kind in NO_EMAIL_KINDS or category == "ANNOUNCEMENT":
        return
    mode = "IMMEDIATE" if category in ALWAYS_IMMEDIATE else email_modes(conn, account_id)[category]
    if mode != "IMMEDIATE":
        return
    to = _owner_email(conn, account_id)
    if to is None:  # the local operator account has no mailbox
        return
    from webapp import comms
    template = kind if kind in DEDICATED_TEMPLATES else "notify.immediate"
    comms.enqueue(conn, category="SERVICE" if category in ALWAYS_IMMEDIATE else "PRODUCT", template_id=template,
                  to_address=to, payload=_payload(kind, detail, href, _origin()), account_id=account_id,
                  idempotency_key=f"notification:{notification_id}", now=now)


_ORIGIN: dict[str, str] = {}


def configure_origin(app_origin: str) -> None:
    """The app's public origin for links in fanned-out mail (set at startup)."""
    _ORIGIN["value"] = app_origin.rstrip("/")


def _origin() -> str:
    if "value" not in _ORIGIN:
        from webapp.config import Settings
        configure_origin(Settings().app_origin)
    return _ORIGIN["value"]


def notify(conn: dbapi.Connection, *, account_id: str, kind: str, subject_type: str, subject_id: str,
           dedupe_key: str, detail: Mapping[str, Any], now: datetime) -> bool:
    """No commit. False when this account already has ``dedupe_key``."""
    if kind not in NOTIFICATION_KINDS:
        raise ValueError(f"unknown notification kind {kind}")
    category = NOTIFICATION_KINDS[kind]
    notification_id = f"ntf_{uuid.uuid4().hex[:20]}"
    cursor = conn.execute(
        "INSERT INTO notifications (id, account_id, kind, category, severity, subject_type, subject_id, dedupe_key, "
        "detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (notification_id, account_id, kind, category, SEVERITY.get(kind, "INFO"), subject_type, subject_id, dedupe_key,
         json.dumps(dict(detail), sort_keys=True, default=str), ts(now)))
    if cursor.rowcount != 1:
        return False
    _fan_out(conn, notification_id=notification_id, account_id=account_id, kind=kind, category=category,
             href=_href(subject_type, subject_id, kind), detail=detail, now=now)
    return True


def notify_6c(conn: dbapi.Connection, *, account_id: str, key: str, kind: str, subject_type: str, subject_id: str,
              detail: Mapping[str, Any], now: datetime) -> bool:
    """The 6C ``create_notification`` mirror (§17.2 mapping)."""
    mapped = SIXC_KIND_MAP.get(kind)
    if mapped is None:
        return False
    return notify(conn, account_id=account_id, kind=mapped, subject_type=subject_type, subject_id=subject_id,
                  dedupe_key=f"6c:{key}", detail={"source": "6c", "6c_kind": kind, **dict(detail)}, now=now)


# ---- the Updates list ----------------------------------------------------------------

def _decoded(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["detail"] = json.loads(item.pop("detail_json") or "{}")
    item["title"] = TITLES.get(item["kind"], item["kind"])
    item["href"] = _href(item["subject_type"], item["subject_id"], item["kind"])
    return item


def list_notifications(conn: dbapi.Connection, account_id: str, *, include_archived: bool = False,
                       limit: int = 100) -> list[dict[str, Any]]:
    archived = "" if include_archived else " AND archived_at IS NULL"
    rows = conn.execute(f"SELECT * FROM notifications WHERE account_id = ?{archived} "
                        f"ORDER BY created_at DESC, id DESC LIMIT ?", (account_id, limit)).fetchall()
    return [_decoded(r) for r in rows]


def unread_critical(conn: dbapi.Connection, account_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM notifications WHERE account_id = ? AND severity = 'CRITICAL' "
                       "AND read_at IS NULL AND archived_at IS NULL", (account_id,)).fetchone()
    return int(row[0])


def mark_read(conn: dbapi.Connection, *, account_id: str, notification_id: str, now: datetime) -> bool:
    cursor = conn.execute("UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE id = ? AND account_id = ?",
                          (ts(now), notification_id, account_id))
    return cursor.rowcount == 1


def archive(conn: dbapi.Connection, *, account_id: str, notification_id: str, now: datetime) -> bool:
    cursor = conn.execute("UPDATE notifications SET archived_at = COALESCE(archived_at, ?), "
                          "read_at = COALESCE(read_at, ?) WHERE id = ? AND account_id = ?",
                          (ts(now), ts(now), notification_id, account_id))
    return cursor.rowcount == 1


# ---- digests and scans (worker) ------------------------------------------------------------

def account_timezone(conn: dbapi.Connection, account_id: str) -> ZoneInfo:
    from webapp.persistence.autonomy_authority import current_policy
    policy = current_policy(conn, account_id)
    name = (policy or {}).get("doc", {}).get("timezone") or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def send_digests(conn: dbapi.Connection, *, now: datetime) -> int:
    """No commit. For each account at or past 07:00 local time: one digest per
    local day with its DAILY_DIGEST notifications since the previous 07:00."""
    since = ts(now - timedelta(days=2))
    accounts = [r[0] for r in conn.execute(
        "SELECT DISTINCT account_id FROM notifications WHERE created_at >= ? AND archived_at IS NULL "
        "AND category IN ('ACTION_REQUIRED', 'OUTCOME', 'USAGE', 'DISCOVERY') ORDER BY account_id", (since,))]
    sent = 0
    for account_id in accounts:
        tz = account_timezone(conn, account_id)
        local = now.astimezone(tz)
        if local.hour < DIGEST_HOUR:
            continue
        end = datetime.combine(local.date(), time(DIGEST_HOUR), tzinfo=tz)
        start = end - timedelta(days=1)
        digest_categories = [c for c, mode in email_modes(conn, account_id).items() if mode == "DAILY_DIGEST"]
        if not digest_categories:
            continue
        marks = ", ".join("?" for _ in digest_categories)
        rows = conn.execute(
            f"SELECT * FROM notifications WHERE account_id = ? AND category IN ({marks}) AND created_at >= ? "
            f"AND created_at < ? ORDER BY created_at", (account_id, *digest_categories, ts(start), ts(end))).fetchall()
        to = _owner_email(conn, account_id)
        if not rows or to is None:
            continue
        from webapp import comms
        items = [{"title": TITLES[r["kind"]], "action_url": _origin() + _href(r["subject_type"], r["subject_id"],
                                                                             r["kind"])} for r in rows]
        if comms.enqueue(conn, category="PRODUCT", template_id="notify.digest", to_address=to,
                         payload={"items": items, "period": "since yesterday morning"}, account_id=account_id,
                         idempotency_key=f"digest:{account_id}:{local.date().isoformat()}", now=now):
            sent += 1
    return sent


APPROVAL_WARNING = timedelta(hours=48)


def scan_expiring_approvals(conn: dbapi.Connection, *, settings: Any, now: datetime) -> int:
    """No commit. Notify each effective approval that expires within 48 h (once per approval)."""
    from webapp.services.review_application import review_state
    ttl = timedelta(days=settings.review_approval_ttl_days)
    earliest, latest = ts(now - ttl), ts(now - ttl + APPROVAL_WARNING)
    rows = conn.execute(
        "SELECT a.id, a.account_id, a.application_workspace_id, a.created_at FROM application_approvals a "
        "WHERE a.seq = (SELECT MAX(seq) FROM application_approvals b "
        "WHERE b.application_workspace_id = a.application_workspace_id)").fetchall()
    notified = 0
    for row in rows:
        created = datetime.fromisoformat(row["created_at"])
        if not (earliest <= ts(created) <= latest):
            continue
        try:
            state = review_state(conn, settings=settings, account_id=row["account_id"],
                                 application_workspace_id=row["application_workspace_id"], now=now)
        except Exception:  # noqa: BLE001 - one unreadable workspace never stops the scan
            logger.exception("approval_expiry_scan_skip workspace=%s", row["application_workspace_id"])
            continue
        if not state.approval_effective:
            continue
        if notify(conn, account_id=row["account_id"], kind="application.approval_expiring", subject_type="workspace",
                  subject_id=row["application_workspace_id"], dedupe_key=f"approval_expiring:{row['id']}",
                  detail={"expires_at": (created + ttl).isoformat()}, now=now):
            notified += 1
    return notified
