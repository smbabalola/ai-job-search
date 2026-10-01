"""Bundle 7 spec §11.4, §12.1, §19.3: the entitlement gate, grants, platform
controls and recorded catalog versions."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from product.entitlements import FeatureNotInPlan, SubscriptionView, load_catalog, parse_catalog
from webapp.config import Settings
from webapp.persistence import dbapi, identity
from webapp.persistence.db import connect, init_db
from webapp.services import entitlements as ent
from webapp.services.auth import AuthService, SignupUnavailable
from webapp.services.ownership import AccountScope
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
DEV = Path(__file__).parents[3] / "product" / "plans" / "plan-catalog.dev.json"
CATALOG = load_catalog(DEV)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    conn = connect(path)
    created = identity.create_user_with_account(
        conn, email="ada@example.com", password_hash="h", display_name="Ada", legal_document_ids=[],
        now=NOW, profile_store=DatabaseProfileSourceStore())
    conn.commit()
    settings = Settings(db_path=path)
    scope = AccountScope(account_id=created["account"]["id"], profile_root=tmp_path,
                         user_id=created["user"]["id"])
    yield conn, settings, scope
    conn.close()


def _gate(settings, **kwargs) -> ent.EntitlementGate:
    return ent.EntitlementGate(CATALOG, settings=settings, **kwargs)


# ---- platform controls --------------------------------------------------------

def test_missing_controls_read_fail_closed_with_local_conveniences(world, tmp_path):
    conn, settings, _ = world
    hosted = Settings(db_path=settings.db_path, deployment="hosted")
    local_defaults = {key: ent.platform_control(conn, key, settings=settings) for key in ent.PLATFORM_CONTROL_KEYS}
    assert {k for k, v in local_defaults.items() if v} == {"SIGNUPS_ENABLED", "AI_ENABLED", "DISCOVERY_ENABLED"}
    assert not any(ent.platform_control(conn, key, settings=hosted) for key in ent.PLATFORM_CONTROL_KEYS)


def test_the_latest_control_value_wins_and_history_is_append_only(world):
    conn, settings, scope = world
    ent.set_platform_control(conn, "AI_ENABLED", False, actor_user_id=scope.user_id, reason="provider outage", now=NOW)
    assert ent.platform_control(conn, "AI_ENABLED", settings=settings) is False
    ent.set_platform_control(conn, "AI_ENABLED", True, actor_user_id=scope.user_id, reason="recovered",
                             now=NOW + timedelta(minutes=5))
    assert ent.platform_control(conn, "AI_ENABLED", settings=settings) is True
    with pytest.raises(dbapi.IntegrityError):
        conn.execute("UPDATE platform_controls SET value = 0")
    with pytest.raises(ValueError):
        ent.set_platform_control(conn, "EVERYTHING_ENABLED", True, actor_user_id=None, reason="x", now=NOW)


# ---- catalog versions ---------------------------------------------------------

def test_catalog_versions_are_recorded_once_and_a_silent_edit_is_refused(world):
    conn, _, _ = world
    ent.record_catalog(conn, CATALOG, now=NOW)
    ent.record_catalog(conn, CATALOG, now=NOW + timedelta(days=1))
    rows = conn.execute("SELECT catalog_version, catalog_hash FROM plan_catalog_versions").fetchall()
    assert [tuple(row) for row in rows] == [(CATALOG.catalog_version, CATALOG.catalog_hash)]
    edited = json.loads(CATALOG.catalog_json)
    edited["plans"]["pro"]["display_name"] = "Pro (edited)"
    with pytest.raises(ent.CatalogError):
        ent.record_catalog(conn, parse_catalog(edited), now=NOW)


# ---- grants ---------------------------------------------------------------------

def test_a_grant_is_append_only_except_a_single_revocation(world):
    conn, _, scope = world
    grant_id = ent.add_grant(conn, account_id=scope.account_id, kind="PLAN_OVERRIDE", plan_id="power",
                             reason="beta tester", actor_user_id=scope.user_id, starts_at=NOW,
                             expires_at=NOW + timedelta(days=30), now=NOW)
    with pytest.raises(dbapi.IntegrityError):
        conn.execute("UPDATE entitlement_grants SET plan_id = 'pro' WHERE id = ?", (grant_id,))
    with pytest.raises(dbapi.IntegrityError):
        conn.execute("DELETE FROM entitlement_grants WHERE id = ?", (grant_id,))
    ent.revoke_grant(conn, grant_id, now=NOW + timedelta(days=1))
    with pytest.raises(dbapi.IntegrityError):
        conn.execute("UPDATE entitlement_grants SET revoked_at = ? WHERE id = ?", (NOW.isoformat(), grant_id))


def test_a_malformed_grant_is_refused_by_the_schema(world):
    conn, _, scope = world
    with pytest.raises(dbapi.IntegrityError):
        ent.add_grant(conn, account_id=scope.account_id, kind="ALLOWANCE_BONUS", allowance="cv.tailor", amount=0,
                      reason="x", actor_user_id=None, starts_at=NOW, expires_at=None, now=NOW)
    with pytest.raises(dbapi.IntegrityError):
        ent.add_grant(conn, account_id=scope.account_id, kind="PLAN_OVERRIDE", plan_id="power",
                      reason="x", actor_user_id=None, starts_at=NOW, expires_at=None, now=NOW)


# ---- the gate -----------------------------------------------------------------

def test_the_gate_resolves_free_without_a_subscription_and_names_the_upgrade(world):
    conn, settings, scope = world
    gate = _gate(settings)
    assert gate.entitlements(conn, scope, now=NOW).plan_id == "free"
    assert gate.require_feature(conn, scope, "ai.prepare", now=NOW).plan_id == "free"
    with pytest.raises(FeatureNotInPlan) as exc_info:
        gate.require_feature(conn, scope, "ai.cv_tailor", now=NOW)
    assert (exc_info.value.plan_id, exc_info.value.upgrade_to) == ("free", "pro")
    with pytest.raises(FeatureNotInPlan) as exc_info:
        gate.require_feature(conn, scope, "automation.prepare", now=NOW)
    assert exc_info.value.upgrade_to == "power"


def test_the_gate_applies_grants_from_the_database(world):
    conn, settings, scope = world
    ent.add_grant(conn, account_id=scope.account_id, kind="PLAN_OVERRIDE", plan_id="power", reason="beta",
                  actor_user_id=None, starts_at=NOW - timedelta(hours=1), expires_at=NOW + timedelta(days=1), now=NOW)
    # AUTOMATION_ENABLED reads false until set, even locally (§19.3).
    with pytest.raises(FeatureNotInPlan):
        _gate(settings).require_feature(conn, scope, "automation.prepare", now=NOW)
    ent.set_platform_control(conn, "AUTOMATION_ENABLED", True, actor_user_id=None, reason="on", now=NOW)
    resolved = _gate(settings).require_feature(conn, scope, "automation.prepare", now=NOW)
    assert (resolved.plan_id, resolved.source) == ("power", "grant")


def test_a_platform_control_refusal_offers_no_upgrade(world):
    conn, settings, scope = world
    ent.set_platform_control(conn, "AI_ENABLED", False, actor_user_id=None, reason="outage", now=NOW)
    with pytest.raises(FeatureNotInPlan) as exc_info:
        _gate(settings).require_feature(conn, scope, "ai.prepare", now=NOW)
    assert exc_info.value.upgrade_to is None


def test_the_gate_honours_a_subscription_pinned_to_an_older_recorded_catalog(world):
    conn, settings, scope = world
    older = json.loads(CATALOG.catalog_json)
    older["catalog_version"] = "dev-2026-01.1"
    older["plans"]["pro"]["allowances"]["applications.prepare"]["limit"] = 7
    ent.record_catalog(conn, parse_catalog(older), now=NOW)
    view = SubscriptionView(state="ACTIVE", plan_id="pro", catalog_version="dev-2026-01.1",
                            current_period_start=NOW - timedelta(days=3), current_period_end=NOW + timedelta(days=27),
                            past_due_since=None)
    gate = _gate(settings, subscriptions=lambda _conn, account_id: view if account_id == scope.account_id else None)
    resolved = gate.entitlements(conn, scope, now=NOW)
    assert (resolved.plan_id, resolved.catalog_version, resolved.allowances["applications.prepare"]) == (
        "pro", "dev-2026-01.1", 7)


# ---- sign-ups closed (deferred from Task 8) -------------------------------------

def test_signups_disabled_refuses_signup_before_creating_a_user(world):
    conn, settings, _ = world
    for kind in ("TERMS", "PRIVACY"):
        conn.execute("INSERT INTO legal_documents (id, kind, version, published_at, content_sha256) "
                     "VALUES (?, ?, '1', ?, 'x')", (f"legal_{kind}", kind, (NOW - timedelta(days=1)).isoformat()))
    ent.set_platform_control(conn, "SIGNUPS_ENABLED", False, actor_user_id=None, reason="launch hold", now=NOW)
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    with pytest.raises(SignupUnavailable):
        AuthService(settings).signup(conn, email="new@example.com", password="a sufficiently long passphrase",
                                     display_name="New", accepted_legal_ids=["legal_TERMS", "legal_PRIVACY"])
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == before


# ---- startup ------------------------------------------------------------------

def test_the_app_records_its_catalog_at_startup_and_exposes_the_gate(tmp_path):
    from fastapi.testclient import TestClient

    from webapp.app import create_app

    settings = Settings(db_path=tmp_path / "db.sqlite3")
    app = create_app(settings)
    with TestClient(app):
        assert isinstance(app.state.entitlement_gate, ent.EntitlementGate)
    conn = connect(settings.db_path)
    try:
        versions = [row[0] for row in conn.execute("SELECT catalog_version FROM plan_catalog_versions")]
    finally:
        conn.close()
    assert versions == [CATALOG.catalog_version]


def test_the_app_refuses_to_start_with_an_invalid_catalog(tmp_path):
    from webapp.app import create_app

    broken = json.loads(CATALOG.catalog_json)
    broken["plans"]["pro"]["allowances"]["cv.tailor"]["limit"] = -1
    path = tmp_path / "plan-catalog.broken.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(ent.CatalogError):
        create_app(Settings(db_path=tmp_path / "db.sqlite3", plan_catalog_path=path))


def test_every_gate_applies_the_retention_policy_payment_grace(tmp_path):
    """DP-4: a PAST_DUE payer keeps the plan for the configured grace period, in
    the web app's gate and in the gate the worker and 6C build (gate_for)."""
    from product.retention_policy import load_retention_policy
    from webapp.app import _project_path, create_app
    from webapp.config import Settings
    from webapp.services.entitlements import gate_for
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents")
    grace = load_retention_policy(_project_path(settings.retention_policy_path)).payment_grace
    assert grace > timedelta(0)  # the dev policy has a grace period, so a zero would show
    assert create_app(settings).state.entitlement_gate.grace == grace
    assert gate_for(settings).grace == grace
