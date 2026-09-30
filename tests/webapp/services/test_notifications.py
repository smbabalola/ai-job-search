"""Bundle 7 Task 20 (spec §17): the unified notification log, email fan-out
by category and mode, digests in the account's timezone, the 6C mirror and
usage thresholds."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from product.entitlements import parse_catalog
from webapp.config import Settings
from webapp.persistence import dbapi, identity
from webapp.persistence.db import connect, init_db
from webapp.services import notifications as n
from webapp.services.entitlements import EntitlementGate
from webapp.services.ownership import AccountScope
from webapp.services.usage import Metering, UsageService
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
PLANS = Path(__file__).parents[3] / "product" / "plans"


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    conn = connect(path)
    accounts = []
    for email in ("ada@example.com", "bob@example.com"):
        created = identity.create_user_with_account(
            conn, email=email, password_hash="h", display_name=email.split("@")[0], legal_document_ids=[], now=NOW,
            profile_store=DatabaseProfileSourceStore())
        accounts.append(created["account"]["id"])
    conn.commit()
    yield conn, accounts, path
    conn.close()


def _notify(conn, account_id, kind, key="k1", detail=None, now=NOW, subject=("workspace", "ws_1")):
    return n.notify(conn, account_id=account_id, kind=kind, subject_type=subject[0], subject_id=subject[1],
                    dedupe_key=key, detail=detail or {}, now=now)


def _messages(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT template_id, category, to_address FROM outbound_messages ORDER BY created_at, id")]


def test_the_kinds_are_the_spec_vocabulary():
    assert n.NOTIFICATION_KINDS["submit.challenge_handoff"] == "ACTION_REQUIRED"
    assert n.NOTIFICATION_KINDS["application.automation_blocked"] == "OUTCOME"
    assert n.NOTIFICATION_KINDS["discovery.new_matches"] == "DISCOVERY"
    assert n.NOTIFICATION_KINDS["announcement.published"] == "ANNOUNCEMENT"
    assert len(n.NOTIFICATION_KINDS) == 25


def test_notify_rolled_back_leaves_no_notification_and_no_mail(world):
    conn, (ada, _), _ = world
    assert _notify(conn, ada, "fill.failed") is True
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    assert _messages(conn) == []


def test_notify_dedupes_per_account(world):
    conn, (ada, bob), _ = world
    assert _notify(conn, ada, "fill.failed") is True
    assert _notify(conn, ada, "fill.failed") is False
    assert _notify(conn, bob, "fill.failed") is True  # the same key in another account is its own
    conn.commit()
    assert len(_messages(conn)) == 2


def test_unknown_kinds_are_refused(world):
    conn, (ada, _), _ = world
    with pytest.raises(ValueError):
        _notify(conn, ada, "made.up")


def test_immediate_categories_email_through_the_outbox(world):
    conn, (ada, _), _ = world
    _notify(conn, ada, "fill.failed", key="a")                                           # OUTCOME, default IMMEDIATE
    _notify(conn, ada, "billing.payment_failed", key="b", subject=("subscription", "s"))  # BILLING, dedicated template
    conn.commit()
    assert sorted(_messages(conn)) == [("billing.payment_failed", "SERVICE", "ada@example.com"),
                                       ("notify.immediate", "PRODUCT", "ada@example.com")]


def test_an_off_category_sends_no_mail_and_a_digest_category_waits(world):
    conn, (ada, _), _ = world
    n.set_email_mode(conn, account_id=ada, category="OUTCOME", mode="OFF", now=NOW)
    _notify(conn, ada, "fill.failed", key="a")
    _notify(conn, ada, "discovery.new_matches", key="b", subject=("search_workspace", "sw"))  # DISCOVERY: DAILY_DIGEST
    conn.commit()
    assert _messages(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2


def test_security_billing_and_account_are_not_configurable(world):
    conn, (ada, _), _ = world
    for category in ("SECURITY", "BILLING", "ACCOUNT", "ANNOUNCEMENT"):
        with pytest.raises(ValueError):
            n.set_email_mode(conn, account_id=ada, category=category, mode="OFF", now=NOW)
    with pytest.raises(ValueError):
        n.set_email_mode(conn, account_id=ada, category="OUTCOME", mode="SOMETIMES", now=NOW)


def test_email_modes_default_and_persist(world):
    conn, (ada, _), _ = world
    assert n.email_modes(conn, ada) == {"ACTION_REQUIRED": "IMMEDIATE", "OUTCOME": "IMMEDIATE", "USAGE": "IMMEDIATE",
                                        "DISCOVERY": "DAILY_DIGEST"}
    n.set_email_mode(conn, account_id=ada, category="USAGE", mode="DAILY_DIGEST", now=NOW)
    n.set_email_mode(conn, account_id=ada, category="USAGE", mode="OFF", now=NOW)
    assert n.email_modes(conn, ada)["USAGE"] == "OFF"


def test_a_challenge_handoff_never_emails(world):
    conn, (ada, _), _ = world
    _notify(conn, ada, "submit.challenge_handoff")
    conn.commit()
    assert _messages(conn) == []


def test_the_6c_notification_is_mirrored_with_the_mapped_kind(world):
    from webapp.persistence import autonomy_prepare as ap
    conn, (ada, _), _ = world
    for kind in ("PREPARED", "CANDIDATE_QUESTION", "BLOCKED"):
        ap.create_notification(conn, account_id=ada, key=f"6c:{kind}", kind=kind, subject_type="workspace",
                               subject_id="ws_1", detail={"reason": "x"}, now=NOW)
    conn.commit()
    kinds = sorted(r[0] for r in conn.execute("SELECT kind FROM notifications WHERE account_id = ?", (ada,)))
    assert kinds == ["application.automation_blocked", "application.blocker_needs_answer", "application.prepared"]


def _set_timezone(conn, account_id, tz):
    conn.execute("INSERT INTO standing_policy_versions (id, account_id, policy_json, policy_hash, created_by, "
                 "created_at) VALUES (?, ?, ?, 'h', 'test', ?)",
                 (f"pol_{account_id}", account_id, json.dumps({"timezone": tz}), NOW.isoformat()))


def test_the_digest_goes_out_at_seven_local_time_per_account(world):
    conn, (ada, bob), _ = world
    _set_timezone(conn, ada, "Europe/London")     # BST in October: 07:00 local = 06:00Z
    _set_timezone(conn, bob, "America/New_York")  # EDT: 07:00 local = 11:00Z
    for account in (ada, bob):
        _notify(conn, account, "discovery.new_matches", key="m", subject=("search_workspace", "sw"),
                now=datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc))
    conn.commit()
    assert n.send_digests(conn, now=datetime(2026, 10, 15, 6, 5, tzinfo=timezone.utc)) == 1
    conn.commit()
    assert _messages(conn) == [("notify.digest", "PRODUCT", "ada@example.com")]
    assert n.send_digests(conn, now=datetime(2026, 10, 15, 6, 40, tzinfo=timezone.utc)) == 0  # once per day
    assert n.send_digests(conn, now=datetime(2026, 10, 15, 11, 5, tzinfo=timezone.utc)) == 1
    conn.commit()
    assert [m[2] for m in _messages(conn)] == ["ada@example.com", "bob@example.com"]


def test_a_digest_is_not_sent_when_nothing_is_waiting(world):
    conn, (ada, _), _ = world
    assert n.send_digests(conn, now=datetime(2026, 10, 15, 7, 5, tzinfo=timezone.utc)) == 0  # UTC default


def _catalog(limit):
    doc = json.loads((PLANS / "plan-catalog.dev.json").read_text(encoding="utf-8"))
    doc["catalog_version"] = "test-notify"
    doc["plans"]["free"]["allowances"]["applications.prepare"]["limit"] = limit
    return parse_catalog(doc)


def test_usage_limit_near_fires_once_at_eighty_percent_then_reached(world):
    conn, (ada, _), path = world
    gate = EntitlementGate(_catalog(10), settings=Settings(db_path=path))
    metering = Metering(gate, UsageService(gate), enforced=True, clock=lambda: NOW)
    scope = AccountScope(account_id=ada, profile_root=path.parent, user_id=None)
    for i in range(10):
        metering.prepare(conn, scope, f"ws_{i}", lambda: None, stage="understand")
        kinds = [r[0] for r in conn.execute("SELECT kind FROM notifications ORDER BY created_at, id")]
        if i + 1 < 8:
            assert kinds == []
        elif i + 1 < 10:
            assert kinds == ["usage.limit_near"]
    assert kinds == ["usage.limit_near", "usage.limit_reached"]


def test_a_notification_can_be_read_and_archived_by_its_account_only(world):
    conn, (ada, bob), _ = world
    _notify(conn, ada, "fill.failed")
    conn.commit()
    note = n.list_notifications(conn, ada)[0]
    assert n.mark_read(conn, account_id=bob, notification_id=note["id"], now=NOW) is False
    assert n.mark_read(conn, account_id=ada, notification_id=note["id"], now=NOW) is True
    assert n.archive(conn, account_id=ada, notification_id=note["id"], now=NOW) is True
    assert n.list_notifications(conn, ada) == []
    assert n.list_notifications(conn, ada, include_archived=True)[0]["read_at"] is not None


def test_an_effective_approval_expiring_within_48_hours_is_notified_once(world, monkeypatch):
    from types import SimpleNamespace
    from webapp.persistence.workspaces import create_workspace
    from webapp.services import review_application
    conn, (ada, _), path = world
    settings = Settings(db_path=path)
    ttl = timedelta(days=settings.review_approval_ttl_days)
    workspaces = {}
    for approval_id, created in (("apr_soon", NOW - ttl + timedelta(hours=24)),   # expires in 24 h: notify
                                 ("apr_fresh", NOW - timedelta(hours=1)),          # expires in ~ttl: not yet
                                 ("apr_revoked", NOW - ttl + timedelta(hours=12))):  # in window but not effective
        ws = create_workspace(conn, company=approval_id, title="Engineer", account_id=ada)["id"]
        workspaces[ws] = approval_id
        conn.execute("INSERT INTO application_approvals (id, account_id, application_workspace_id, scope, binding_json, "
                     "binding_hash, actor, created_at) VALUES (?, ?, ?, 'FILL', '{}', 'h', 'u', ?)",
                     (approval_id, ada, ws, created.isoformat()))
    conn.commit()
    monkeypatch.setattr(review_application, "review_state", lambda conn, **kw: SimpleNamespace(
        approval_effective=workspaces[kw["application_workspace_id"]] != "apr_revoked"))
    assert n.scan_expiring_approvals(conn, settings=settings, now=NOW) == 1
    assert n.scan_expiring_approvals(conn, settings=settings, now=NOW + timedelta(hours=1)) == 0
    assert [r[0] for r in conn.execute("SELECT kind FROM notifications")] == ["application.approval_expiring"]


def test_the_worker_runs_digests_and_the_approval_scan(world):
    from webapp.worker.handlers import default_handlers
    _, _, path = world
    handlers = default_handlers(Settings(db_path=path), providers_factory=lambda: None)
    assert {"notify.digest", "notify.approval_expiry_scan"} <= set(handlers)


def test_the_migration_back_projects_existing_6c_notifications_once(world):
    from webapp.persistence.bundle7_migrations import _back_project_6c
    conn, (ada, _), _ = world
    conn.execute("INSERT INTO autonomy_notification_events (id, account_id, notification_key, kind, subject_type, "
                 "subject_id, event, detail_json, created_at) VALUES ('ane_1', ?, 'old-key', 'PREPARED', 'workspace', "
                 "'ws_9', 'CREATED', '{}', ?)", (ada, NOW.isoformat()))
    conn.commit()
    for _ in range(2):  # re-running is a no-op (dedupe by key)
        _back_project_6c(conn, conn.dialect)
    conn.commit()
    rows = [tuple(r) for r in conn.execute("SELECT kind, dedupe_key FROM notifications WHERE account_id = ?", (ada,))]
    assert rows == [("application.prepared", "6c:old-key")]
