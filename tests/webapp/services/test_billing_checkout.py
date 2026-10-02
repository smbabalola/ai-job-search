"""Bundle 7 spec §12: the provider-neutral billing port, the fake provider and
the account-initiated flows (checkout, portal, change plan, cancel)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from product.entitlements import load_catalog
from tests.webapp.route_inventory import all_routes
from tests.webapp.test_deployment_modes import hosted_settings
from webapp.app import create_app
from webapp.billing.fake import FakeBillingProvider, WebhookSignatureInvalid
from webapp.billing.registry import provider_for
from webapp.config import Settings
from webapp.persistence import billing as billing_rows
from webapp.persistence import identity
from webapp.persistence.db import connect, init_db
from webapp.services.billing import BillingRefused, BillingService
from webapp.services.ownership import AccountScope
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
PLANS = Path(__file__).parents[3] / "product" / "plans"
DEV = load_catalog(PLANS / "plan-catalog.dev.json")
PRODUCTION = load_catalog(PLANS / "plan-catalog.v1.json")
SECRET = b"fake-webhook-secret"


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


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
    scope = AccountScope(account_id=created["account"]["id"], profile_root=tmp_path, user_id=created["user"]["id"])
    clock = Clock(NOW)
    delivered: list[tuple[dict, bytes]] = []
    provider = FakeBillingProvider(None, SECRET, clock, price_map=DEV,
                                   deliver=lambda headers, body: delivered.append((headers, body)))
    service = BillingService(provider, DEV, settings=settings)
    yield conn, scope, provider, service, delivered, clock
    conn.close()


def _subscribe(conn, scope, provider, service, plan_id="pro", interval="month"):
    url = service.start_checkout(conn, scope, plan_id=plan_id, interval=interval, now=NOW)
    session_id = url.rstrip("/").split("/")[-1]
    provider.simulate(session_id, "pay")
    subscription_id = provider.subscription_for_session(session_id)
    # Task 15 derives these rows from the webhook; here they are written directly.
    conn.execute("UPDATE checkout_sessions SET status = 'COMPLETED' WHERE provider_session_id = ?", (session_id,))
    snapshot = provider.fetch_subscription(subscription_id)
    billing_rows.upsert_subscription(conn, account_id=scope.account_id, provider=provider.name, snapshot=snapshot,
                                     state=snapshot.status, catalog_version=DEV.catalog_version, now=NOW)
    return subscription_id


def test_checkout_opens_a_session_and_creates_the_customer_once(world):
    conn, scope, provider, service, _, _ = world
    first = service.start_checkout(conn, scope, plan_id="pro", interval="month", now=NOW)
    second = service.start_checkout(conn, scope, plan_id="pro", interval="month", now=NOW + timedelta(seconds=5))
    assert first == second and "/dev/billing/checkout/" in first
    sessions = conn.execute("SELECT status, plan_id, interval FROM checkout_sessions").fetchall()
    assert [tuple(row) for row in sessions] == [("OPEN", "pro", "month")]
    assert conn.execute("SELECT COUNT(*) FROM billing_customers").fetchone()[0] == 1
    assert provider.customer_count() == 1
    actions = [row[0] for row in conn.execute("SELECT action FROM audit_log WHERE account_id = ?",
                                              (scope.account_id,))]
    assert actions.count("PLAN_CHECKOUT_STARTED") == 1


def test_a_different_plan_opens_a_new_session_for_the_same_customer(world):
    conn, scope, provider, service, _, _ = world
    pro = service.start_checkout(conn, scope, plan_id="pro", interval="month", now=NOW)
    power = service.start_checkout(conn, scope, plan_id="power", interval="year", now=NOW)
    assert pro != power
    assert conn.execute("SELECT COUNT(*) FROM checkout_sessions WHERE status = 'OPEN'").fetchone()[0] == 2
    assert provider.customer_count() == 1


def test_choosing_free_makes_no_provider_call(world):
    conn, scope, provider, service, _, _ = world
    url = service.start_checkout(conn, scope, plan_id="free", interval="month", now=NOW)
    assert url.endswith("/settings/billing")
    assert provider.customer_count() == 0
    assert conn.execute("SELECT COUNT(*) FROM checkout_sessions").fetchone()[0] == 0


def test_a_paid_plan_without_a_provider_price_is_unavailable(world):
    conn, scope, provider, _, _, _ = world
    service = BillingService(provider, PRODUCTION, settings=Settings(db_path=Path("unused")))
    with pytest.raises(BillingRefused) as exc_info:
        service.start_checkout(conn, scope, plan_id="pro", interval="month", now=NOW)
    assert (exc_info.value.code, exc_info.value.status) == ("PLAN_UNAVAILABLE", 409)
    assert provider.customer_count() == 0


def test_no_configured_provider_makes_paid_plans_unavailable(world):
    conn, scope, _, _, _, _ = world
    service = BillingService(None, DEV, settings=Settings(db_path=Path("unused")))
    with pytest.raises(BillingRefused) as exc_info:
        service.start_checkout(conn, scope, plan_id="pro", interval="month", now=NOW)
    assert exc_info.value.code == "PLAN_UNAVAILABLE"


def test_an_unknown_plan_or_interval_is_refused(world):
    conn, scope, _, service, _, _ = world
    for plan_id, interval in (("gold", "month"), ("pro", "week")):
        with pytest.raises(BillingRefused) as exc_info:
            service.start_checkout(conn, scope, plan_id=plan_id, interval=interval, now=NOW)
        assert exc_info.value.code == "INVALID_PLAN"


def test_simulated_payment_sends_a_signed_webhook_the_provider_verifies(world):
    conn, scope, provider, service, delivered, _ = world
    url = service.start_checkout(conn, scope, plan_id="pro", interval="month", now=NOW)
    provider.simulate(url.split("/")[-1], "pay")
    assert delivered, "paying must deliver a webhook"
    headers, body = delivered[-1]
    event = provider.verify_webhook(headers=headers, body=body, now=NOW)
    assert event.subscription_id == provider.subscription_for_session(url.split("/")[-1])
    snapshot = provider.fetch_subscription(event.subscription_id)
    # First subscription: the dev catalog's 14-day trial applies.
    assert (snapshot.status, snapshot.plan_id, snapshot.interval) == ("TRIALING", "pro", "month")
    assert snapshot.current_period_end - snapshot.current_period_start == timedelta(days=14)
    with pytest.raises(WebhookSignatureInvalid):
        provider.verify_webhook(headers=headers, body=body + b" ", now=NOW)
    with pytest.raises(WebhookSignatureInvalid):
        provider.verify_webhook(headers=headers, body=body, now=NOW + timedelta(minutes=10))


def test_a_failed_or_cancelled_checkout_creates_no_active_subscription(world):
    conn, scope, provider, service, _, _ = world
    failed = service.start_checkout(conn, scope, plan_id="pro", interval="month", now=NOW).split("/")[-1]
    provider.simulate(failed, "fail")
    assert provider.fetch_subscription(provider.subscription_for_session(failed)).status == "INCOMPLETE"
    cancelled = service.start_checkout(conn, scope, plan_id="power", interval="month", now=NOW).split("/")[-1]
    provider.simulate(cancelled, "cancel")
    assert provider.subscription_for_session(cancelled) is None


def test_upgrades_apply_now_and_downgrades_at_period_end(world):
    conn, scope, provider, service, _, _ = world
    subscription_id = _subscribe(conn, scope, provider, service, "pro")
    assert service.change_plan(conn, scope, plan_id="power", interval="month", now=NOW)["when"] == "now"
    assert provider.fetch_subscription(subscription_id).plan_id == "power"
    billing_rows.upsert_subscription(conn, account_id=scope.account_id, provider=provider.name,
                                     snapshot=provider.fetch_subscription(subscription_id), state="ACTIVE",
                                     catalog_version=DEV.catalog_version, now=NOW)
    assert service.change_plan(conn, scope, plan_id="pro", interval="month", now=NOW)["when"] == "period_end"
    snapshot = provider.fetch_subscription(subscription_id)
    assert snapshot.plan_id == "power", "a downgrade waits for the period end"
    assert provider.scheduled_change(subscription_id) == "pro"


def test_choosing_free_while_subscribed_cancels_at_period_end(world):
    conn, scope, provider, service, _, _ = world
    subscription_id = _subscribe(conn, scope, provider, service, "pro")
    assert service.change_plan(conn, scope, plan_id="free", interval="month", now=NOW)["when"] == "period_end"
    snapshot = provider.fetch_subscription(subscription_id)
    assert (snapshot.status, snapshot.cancel_at_period_end) == ("CANCEL_SCHEDULED", True)


def test_cancel_takes_effect_at_the_period_end(world):
    conn, scope, provider, service, _, _ = world
    subscription_id = _subscribe(conn, scope, provider, service, "pro")
    service.cancel(conn, scope, now=NOW)
    snapshot = provider.fetch_subscription(subscription_id)
    assert (snapshot.status, snapshot.plan_id, snapshot.cancel_at_period_end) == ("CANCEL_SCHEDULED", "pro", True)


def test_checkout_is_refused_while_a_subscription_is_live(world):
    conn, scope, provider, service, _, _ = world
    _subscribe(conn, scope, provider, service, "pro")
    with pytest.raises(BillingRefused) as exc_info:
        service.start_checkout(conn, scope, plan_id="power", interval="month", now=NOW)
    assert exc_info.value.code == "SUBSCRIPTION_EXISTS"


def test_plan_changes_without_a_subscription_are_refused(world):
    conn, scope, _, service, _, _ = world
    for call in (lambda: service.change_plan(conn, scope, plan_id="power", interval="month", now=NOW),
                 lambda: service.cancel(conn, scope, now=NOW),
                 lambda: service.portal_url(conn, scope, now=NOW)):
        with pytest.raises(BillingRefused) as exc_info:
            call()
        assert exc_info.value.code == "NO_SUBSCRIPTION"


def test_status_and_the_gate_read_the_subscription_row(world):
    conn, scope, provider, service, _, _ = world
    assert service.status(conn, scope)["plan_id"] == "free"
    _subscribe(conn, scope, provider, service, "pro")
    status = service.status(conn, scope)
    assert (status["plan_id"], status["state"], status["interval"]) == ("pro", "TRIALING", "month")
    view = billing_rows.subscription_view(conn, scope.account_id)
    assert (view.plan_id, view.state, view.catalog_version) == ("pro", "TRIALING", DEV.catalog_version)


def test_a_returning_subscriber_gets_no_second_trial(world):
    conn, scope, provider, service, _, _ = world
    first = _subscribe(conn, scope, provider, service, "pro")
    conn.execute("UPDATE subscriptions SET state = 'ENDED' WHERE provider_subscription_id = ?", (first,))
    second = _subscribe(conn, scope, provider, service, "pro")
    assert provider.fetch_subscription(second).status == "ACTIVE"


def test_the_fake_provider_persists_to_its_state_file(tmp_path):
    state = tmp_path / "fake-billing.json"
    clock = Clock(NOW)
    first = FakeBillingProvider(state, SECRET, clock, price_map=DEV)
    customer = first.ensure_customer(account_id="acct_1", email="a@example.com")
    again = FakeBillingProvider(state, SECRET, clock, price_map=DEV)
    assert again.ensure_customer(account_id="acct_1", email="a@example.com") == customer
    assert json.loads(state.read_text(encoding="utf-8"))["customers"]


# ---- registry and routes ------------------------------------------------------

def test_the_fake_provider_is_never_used_in_hosted_mode():
    assert provider_for(hosted_settings(), DEV) is None
    assert provider_for(Settings(), DEV).name == "fake"


def test_dev_billing_pages_exist_only_in_local_mode(tmp_path):
    local = create_app(Settings(db_path=tmp_path / "db.sqlite3"))
    hosted = create_app(hosted_settings())
    local_paths = {route.path for route in all_routes(local)}
    hosted_paths = {route.path for route in all_routes(hosted)}
    assert "/api/billing/checkout" in hosted_paths, "the hosted inventory is the real one (never vacuous)"
    assert "/dev/billing/checkout/{session_id}" in local_paths
    assert not any(path.startswith("/dev/billing") for path in hosted_paths)


def test_the_checkout_route_returns_the_redirect_and_the_dev_page_pays(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3")
    app = create_app(settings)
    with TestClient(app) as client:
        started = client.post("/api/billing/checkout", json={"plan_id": "pro", "interval": "month"})
        assert started.status_code == 201, started.text
        page = client.get(started.json()["url"].replace(settings.app_origin, ""))
        assert page.status_code == 200 and "Pay succeeds" in page.text
        unavailable = client.post("/api/billing/checkout", json={"plan_id": "gold", "interval": "month"})
        assert (unavailable.status_code, unavailable.json()["error"]) == (400, "INVALID_PLAN")
        status = client.get("/api/billing/status")
        assert status.status_code == 200 and status.json()["checkout"]["status"] == "OPEN"
