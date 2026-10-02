"""The admin console's only window onto the data (Bundle 7 spec §19.2, §19.4).

Every function here returns counts, statuses and account metadata. None
selects a candidate content column: profile sources, document bytes or names,
artifact payloads, answer values, observations or job text. Admin routes call
nothing else. A privacy-canary test plants a marker in every content table and
checks no admin response carries it."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from webapp.persistence import dbapi

__all__ = [
    "account_detail", "announcements", "audit_entries", "billing_events", "controls", "dashboard", "jobs",
    "outbox", "search_accounts", "staff_members",
]

LIST_LIMIT = 100


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _count(conn: dbapi.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def _plan_id(conn: dbapi.Connection, settings: Any, account_id: str, now: datetime) -> str:
    from webapp.services.entitlements import gate_for
    try:
        return gate_for(settings).entitlements(conn, SimpleNamespace(account_id=account_id), now=now).plan_id
    except Exception:  # noqa: BLE001 - a read model never fails on one account
        return "unknown"


def _customer_accounts(conn: dbapi.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT a.id, a.status, a.created_at, u.id AS user_id, u.email_display, u.status AS user_status, "
        "u.email_verified_at, u.last_login_at FROM accounts a "
        "JOIN account_memberships m ON m.account_id = a.id AND m.role = 'OWNER' AND m.revoked_at IS NULL "
        "JOIN users u ON u.id = m.user_id WHERE a.kind = 'candidate' ORDER BY a.created_at DESC").fetchall()]


def dashboard(conn: dbapi.Connection, *, settings: Any, now: datetime) -> dict[str, Any]:
    accounts = _customer_accounts(conn)
    by_status: dict[str, int] = {}
    by_plan: dict[str, int] = {}
    for account in accounts:
        by_status[account["status"]] = by_status.get(account["status"], 0) + 1
        plan = _plan_id(conn, settings, account["id"], now)
        by_plan[plan] = by_plan.get(plan, 0) + 1
    week, month = _iso(now - timedelta(days=7)), _iso(now - timedelta(days=30))
    total = len(accounts)
    verified = sum(1 for a in accounts if a["email_verified_at"])
    onboarded = 0
    for row in conn.execute("SELECT state_json FROM account_onboarding WHERE account_id IN "
                            "(SELECT id FROM accounts WHERE kind = 'candidate')").fetchall():
        steps = json.loads(row[0]).get("steps", {})
        onboarded += bool(steps) and all(status in ("DONE", "SKIPPED") for status in steps.values())
    return {
        "accounts_by_status": by_status, "accounts_by_plan": by_plan,
        "signups_7d": sum(1 for a in accounts if a["created_at"] >= week),
        "signups_30d": sum(1 for a in accounts if a["created_at"] >= month),
        "verified_pct": round(100 * verified / total) if total else 0,
        "onboarding_pct": round(100 * onboarded / total) if total else 0,
        "prepares_7d": _count(conn, "SELECT COUNT(*) FROM usage_reservations WHERE allowance = 'applications.prepare' "
                                    "AND status = 'CONSUMED' AND settled_at >= ?", (week,)),
        "fills_7d": _count(conn, "SELECT COUNT(*) FROM fill_runs WHERE created_at >= ?", (week,)),
        "submits_7d": _count(conn, "SELECT COUNT(*) FROM submission_attempts WHERE created_at >= ?", (week,)),
        "outbox": {s: _count(conn, "SELECT COUNT(*) FROM outbound_messages WHERE status = ?", (s,))
                   for s in ("QUEUED", "FAILED")},
        "jobs": {s: _count(conn, "SELECT COUNT(*) FROM jobs WHERE status = ?", (s,)) for s in ("QUEUED", "DEAD")},
        "webhooks": {
            "unprocessed": _count(conn, "SELECT COUNT(*) FROM billing_webhook_events WHERE processed_at IS NULL"),
            "failed": _count(conn, "SELECT COUNT(*) FROM billing_webhook_events WHERE process_error IS NOT NULL"),
        },
        "ai_cost_usd_7d": _count(conn, "SELECT SUM(cost_micro_usd) FROM ai_cost_events WHERE created_at >= ?",
                                 (week,)) / 1_000_000,
        "ai_cost_usd_30d": _count(conn, "SELECT SUM(cost_micro_usd) FROM ai_cost_events WHERE created_at >= ?",
                                  (month,)) / 1_000_000,
        "unresolved_decisions": None,  # the release-readiness tool (Task 32) fills this in
        "writer_lock": _writer_lock(),
    }


def _writer_lock() -> dict[str, Any]:
    from webapp.api.ops import writer_lock_summary
    return writer_lock_summary()


def search_accounts(conn: dbapi.Connection, *, settings: Any, now: datetime, query: str = "",
                    status: str = "", plan: str = "") -> list[dict[str, Any]]:
    needle = query.strip().casefold()
    out = []
    for account in _customer_accounts(conn):
        if needle and needle not in (account["email_display"] or "").casefold() and needle != account["id"]:
            continue
        if status and account["status"] != status:
            continue
        plan_id = _plan_id(conn, settings, account["id"], now)
        if plan and plan_id != plan:
            continue
        out.append({"id": account["id"], "email": account["email_display"], "status": account["status"],
                    "plan": plan_id, "created_at": account["created_at"]})
        if len(out) >= LIST_LIMIT:
            break
    return out


def account_detail(conn: dbapi.Connection, account_id: str, *, settings: Any, now: datetime) -> dict[str, Any] | None:
    account = next((a for a in _customer_accounts(conn) if a["id"] == account_id), None)
    if account is None:
        return None
    from webapp.services.entitlements import gate_for
    from webapp.services.usage import UsageService
    scope = SimpleNamespace(account_id=account_id)
    usage = [{"allowance": r["allowance"], "used": r["used"], "limit": r["limit"]}
             for r in UsageService(gate_for(settings)).summary(conn, scope, now=now)]
    onboarding = conn.execute("SELECT state_json FROM account_onboarding WHERE account_id = ?",
                              (account_id,)).fetchone()
    steps = json.loads(onboarding[0]).get("steps", {}) if onboarding else {}
    from webapp.persistence.autonomy_authority import kill_switch_state
    return {
        "id": account_id, "email": account["email_display"], "status": account["status"],
        "user_status": account["user_status"], "created_at": account["created_at"],
        "email_verified": bool(account["email_verified_at"]), "last_login_at": account["last_login_at"],
        "plan": _plan_id(conn, settings, account_id, now), "usage": usage,
        "onboarding": {step: status for step, status in steps.items()},
        "devices": _count(conn, "SELECT COUNT(*) FROM extension_devices WHERE account_id = ? AND revoked_at IS NULL",
                          (account_id,)),
        "notifications": _count(conn, "SELECT COUNT(*) FROM notifications WHERE account_id = ?", (account_id,)),
        "sessions": _count(conn, "SELECT COUNT(*) FROM web_sessions WHERE user_id = ? AND revoked_at IS NULL "
                                 "AND absolute_expires_at > ?", (account["user_id"], _iso(now))),
        "kill_switch_engaged": bool(kill_switch_state(conn, account_id)["halted"]),
        "grants": [dict(r) for r in conn.execute(
            "SELECT id, kind, plan_id, allowance, amount, starts_at, expires_at, revoked_at FROM entitlement_grants "
            "WHERE account_id = ? ORDER BY seq DESC", (account_id,)).fetchall()],
        "audit": audit_entries(conn, account_id=account_id, limit=50),
    }


def audit_entries(conn: dbapi.Connection, *, account_id: str | None = None, limit: int = LIST_LIMIT) -> list[dict]:
    """Who did what, when. ``detail_json`` is not shown: actions and targets only."""
    where, params = ("WHERE account_id = ?", (account_id,)) if account_id else ("", ())
    return [dict(r) for r in conn.execute(
        f"SELECT occurred_at, actor_type, actor_id, account_id, action, target_type, target_id FROM audit_log {where} "
        f"ORDER BY seq DESC LIMIT {int(limit)}", params).fetchall()]


def billing_events(conn: dbapi.Connection) -> dict[str, list[dict]]:
    return {
        "subscription_events": [dict(r) for r in conn.execute(
            "SELECT created_at, account_id, from_state, to_state, cause FROM subscription_events "
            f"ORDER BY seq DESC LIMIT {LIST_LIMIT}").fetchall()],
        "webhook_events": [dict(r) for r in conn.execute(
            "SELECT id, provider, provider_event_id, received_at, signature_verified, processed_at, process_error, "
            f"attempts FROM billing_webhook_events ORDER BY received_at DESC LIMIT {LIST_LIMIT}").fetchall()],
    }


def jobs(conn: dbapi.Connection, *, status: str = "") -> list[dict]:
    where, params = ("WHERE status = ?", (status,)) if status else ("", ())
    rows = conn.execute(f"SELECT id, kind, account_id, status, attempts, max_attempts, run_at, last_error, updated_at "
                        f"FROM jobs {where} ORDER BY updated_at DESC LIMIT {LIST_LIMIT}", params).fetchall()
    return [{**dict(r), "last_error": (r["last_error"] or "")[:200]} for r in rows]


def outbox(conn: dbapi.Connection, *, status: str = "") -> list[dict]:
    where, params = ("WHERE status = ?", (status,)) if status else ("", ())
    rows = conn.execute(f"SELECT id, account_id, template_id, category, status, attempts, last_error, created_at "
                        f"FROM outbound_messages {where} ORDER BY created_at DESC LIMIT {LIST_LIMIT}",
                        params).fetchall()
    return [{**dict(r), "last_error": (r["last_error"] or "")[:200]} for r in rows]


def announcements(conn: dbapi.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, title, body_markdown, audience, severity, published_at, expires_at, withdrawn_at, created_at "
        "FROM announcements ORDER BY created_at DESC").fetchall()]


def controls(conn: dbapi.Connection, *, settings: Any) -> dict[str, Any]:
    from webapp.services.entitlements import PLATFORM_CONTROL_KEYS, platform_control
    return {
        "current": {key: platform_control(conn, key, settings=settings) for key in PLATFORM_CONTROL_KEYS},
        "history": [dict(r) for r in conn.execute(
            "SELECT key, value, actor_user_id, reason, created_at FROM platform_controls "
            f"ORDER BY seq DESC LIMIT {LIST_LIMIT}").fetchall()],
    }


def staff_members(conn: dbapi.Connection) -> list[dict]:
    from webapp.services.staff_auth import has_confirmed_totp, staff_roles
    users = conn.execute("SELECT DISTINCT u.id, u.email_display, u.status FROM users u "
                         "JOIN platform_role_assignments p ON p.user_id = u.id ORDER BY u.email_display").fetchall()
    return [{"id": u[0], "email": u[1], "status": u[2], "roles": sorted(staff_roles(conn, u[0])),
             "totp": has_confirmed_totp(conn, u[0])} for u in users]
