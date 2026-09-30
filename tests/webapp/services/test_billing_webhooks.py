"""Bundle 7 spec §12.1-§12.3: verified, idempotent webhook ingestion and
snapshot-derived subscription processing."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from product.entitlements import load_catalog
from webapp.app import create_app
from webapp.billing.fake import FakeBillingProvider
from webapp.config import Settings
from webapp.persistence import identity
from webapp.persistence.db import connect, init_db
from webapp.services import billing_webhooks as hooks
from webapp.services.billing import BillingService
from webapp.services.ownership import AccountScope
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
DEV = load_catalog(Path(__file__).parents[3] / "product" / "plans" / "plan-catalog.dev.json")
SECRET = b"fake-webhook-secret"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
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
    scope = AccountScope(account_id=created["account"]["id"], profile_root=tmp_path, user_id=created["user"]["id"])
    clock = Clock(NOW)
    inbox: list[tuple[dict, bytes]] = []
    provider = FakeBillingProvider(None, SECRET, clock, price_map=DEV,
                                   deliver=lambda headers, body: inbox.append((headers, body)))
    service = BillingService(provider, DEV, settings=Settings(db_path=path))
    webhooks = hooks.BillingWebhooks(provider, catalog_version=DEV.catalog_version)
    yield conn, scope, provider, service, webhooks, inbox, clock
    conn.close()


def _deliver_all(conn, webhooks, inbox, clock):
    ids = [webhooks.ingest(conn, headers=h, body=b, now=clock()) for h, b in inbox]
    inbox.clear()
    webhooks.process_pending(conn, now=clock())
    return ids


def _subscription(conn, account_id):
    row = conn.execute("SELECT * FROM subscriptions WHERE account_id = ? ORDER BY updated_at DESC",
                       (account_id,)).fetchone()
    return None if row is None else dict(row)


def _pay(conn, scope, provider, service, webhooks, inbox, clock, plan_id="pro"):
    session_id = service.start_checkout(conn, scope, plan_id=plan_id, interval="month", now=clock()).split("/")[-1]
    provider.simulate(session_id, "pay")
    _deliver_all(conn, webhooks, inbox, clock)
    return provider.subscription_for_session(session_id)


def test_a_paid_checkout_becomes_a_live_subscription_and_completes_the_checkout(world):
    conn, scope, provider, service, webhooks, inbox, clock = world
    _pay(conn, scope, provider, service, webhooks, inbox, clock)
    row = _subscription(conn, scope.account_id)
    assert (row["plan_id"], row["state"], row["catalog_version"]) == ("pro", "TRIALING", DEV.catalog_version)
    assert conn.execute("SELECT status FROM checkout_sessions").fetchone()[0] == "COMPLETED"
    events = conn.execute("SELECT from_state, to_state FROM subscription_events").fetchall()
    assert [tuple(e) for e in events] == [(None, "TRIALING")]
    assert "SUBSCRIPTION_STATE_CHANGED" in [r[0] for r in conn.execute("SELECT action FROM audit_log")]


def test_the_same_event_delivered_twice_is_stored_once(world):
    conn, scope, provider, service, webhooks, inbox, clock = world
    session_id = service.start_checkout(conn, scope, plan_id="pro", interval="month", now=NOW).split("/")[-1]
    provider.simulate(session_id, "pay")
    headers, body = inbox[0]
    first = webhooks.ingest(conn, headers=headers, body=body, now=NOW)
    second = webhooks.ingest(conn, headers=headers, body=body, now=NOW)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM billing_webhook_events").fetchone()[0] == 1
    webhooks.process_pending(conn, now=NOW)
    webhooks.process_event(conn, first, now=NOW)  # reprocessing a processed row does nothing
    assert conn.execute("SELECT COUNT(*) FROM subscription_events").fetchone()[0] == 1


def test_a_bad_signature_is_recorded_unverified_and_never_processed(world):
    conn, scope, provider, service, webhooks, inbox, clock = world
    session_id = service.start_checkout(conn, scope, plan_id="pro", interval="month", now=NOW).split("/")[-1]
    provider.simulate(session_id, "pay")
    headers, body = inbox[0]
    huge = body + b" " * 10_000
    with pytest.raises(hooks.WebhookRejected):
        webhooks.ingest(conn, headers=headers, body=huge, now=NOW)
    row = dict(conn.execute("SELECT * FROM billing_webhook_events").fetchone())
    assert (row["signature_verified"], row["provider_event_id"], row["processed_at"]) == (0, None, None)
    assert len(row["payload_json"].encode()) <= 4096
    webhooks.process_pending(conn, now=NOW)
    assert _subscription(conn, scope.account_id) is None


def test_an_event_for_an_unknown_customer_waits_and_succeeds_once_the_customer_exists(world):
    """Review Focus 1."""
    conn, scope, provider, service, webhooks, inbox, clock = world
    session_id = service.start_checkout(conn, scope, plan_id="pro", interval="month", now=NOW).split("/")[-1]
    provider.simulate(session_id, "pay")
    # The provider's webhook outruns our own commit of the customer row.
    conn.execute("DELETE FROM checkout_sessions")
    conn.execute("DELETE FROM billing_customers")
    event_ids = [webhooks.ingest(conn, headers=h, body=b, now=NOW) for h, b in inbox]
    webhooks.process_pending(conn, now=NOW)
    row = dict(conn.execute("SELECT * FROM billing_webhook_events WHERE id = ?", (event_ids[0],)).fetchone())
    assert (row["processed_at"], row["attempts"]) == (None, 1)
    assert "no account" in row["process_error"]
    customer_id = provider.fetch_subscription(provider.subscription_for_session(session_id)).provider_customer_id
    conn.execute("INSERT INTO billing_customers (account_id, provider, provider_customer_id, created_at) "
                 "VALUES (?, 'fake', ?, ?)", (scope.account_id, customer_id, NOW.isoformat()))
    webhooks.process_pending(conn, now=NOW + timedelta(minutes=1))
    row = dict(conn.execute("SELECT * FROM billing_webhook_events WHERE id = ?", (event_ids[0],)).fetchone())
    assert row["processed_at"] is not None and row["attempts"] == 2
    assert _subscription(conn, scope.account_id)["state"] == "TRIALING"


def test_payment_failure_plan_change_and_cancellation_notify_the_account(world):
    conn, scope, provider, service, webhooks, inbox, clock = world
    subscription_id = _pay(conn, scope, provider, service, webhooks, inbox, clock)

    provider.fail_renewal(subscription_id)
    _deliver_all(conn, webhooks, inbox, clock)
    row = _subscription(conn, scope.account_id)
    assert (row["state"], row["past_due_since"]) == ("PAST_DUE", NOW.isoformat())

    clock.now = NOW + timedelta(hours=2)
    service.change_plan(conn, scope, plan_id="power", interval="month", now=clock())
    _deliver_all(conn, webhooks, inbox, clock)
    row = _subscription(conn, scope.account_id)
    assert (row["plan_id"], row["state"], row["past_due_since"]) == ("power", "PAST_DUE", NOW.isoformat())

    service.cancel(conn, scope, now=clock())
    provider.advance_period(subscription_id)
    _deliver_all(conn, webhooks, inbox, clock)
    assert _subscription(conn, scope.account_id)["state"] == "ENDED"

    kinds = sorted(r[0] for r in conn.execute("SELECT kind FROM notifications"))  # change and cancel share a clock tick
    assert kinds == ["billing.payment_failed", "billing.subscription_canceled", "billing.subscription_changed"]
    templates = sorted(r[0] for r in conn.execute("SELECT template_id FROM outbound_messages"))
    assert templates == ["billing.payment_failed", "billing.subscription_canceled", "billing.subscription_changed"]


def test_out_of_order_delivery_ends_at_the_provider_state(world):
    conn, scope, provider, service, webhooks, inbox, clock = world
    subscription_id = _pay(conn, scope, provider, service, webhooks, inbox, clock)
    provider.fail_renewal(subscription_id)
    failed = list(inbox)
    inbox.clear()
    provider.advance_period(subscription_id)  # the renewal then succeeds
    succeeded = list(inbox)
    inbox.clear()
    for headers, body in succeeded + failed:  # the success arrives first
        webhooks.ingest(conn, headers=headers, body=body, now=NOW)
    webhooks.process_pending(conn, now=NOW)
    assert _subscription(conn, scope.account_id)["state"] == "ACTIVE"


def test_a_new_subscription_supersedes_an_incomplete_one(world):
    conn, scope, provider, service, webhooks, inbox, clock = world
    failed = service.start_checkout(conn, scope, plan_id="pro", interval="month", now=NOW).split("/")[-1]
    provider.simulate(failed, "fail")
    _deliver_all(conn, webhooks, inbox, clock)
    assert _subscription(conn, scope.account_id)["state"] == "INCOMPLETE"
    assert conn.execute("SELECT status FROM checkout_sessions").fetchone()[0] == "EXPIRED"
    # An incomplete attempt does not block paying again.
    _pay(conn, scope, provider, service, webhooks, inbox, clock)
    states = sorted(r[0] for r in conn.execute("SELECT state FROM subscriptions WHERE account_id = ?",
                                               (scope.account_id,)))
    assert states == ["ACTIVE", "INCOMPLETE_EXPIRED"]  # the failed attempt used up the trial


def test_the_webhook_route_verifies_and_processes(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3")
    app = create_app(settings)
    with TestClient(app) as client:
        provider = app.state.billing_service.provider
        started = client.post("/api/billing/checkout", json={"plan_id": "pro", "interval": "month"})
        session_id = started.json()["url"].split("/")[-1]
        # The dev checkout page's "Pay succeeds" delivers the webhook in-process.
        paid = client.post(f"/dev/billing/checkout/{session_id}/pay", follow_redirects=False)
        assert paid.status_code == 303
        assert client.get("/api/billing/status").json()["plan_id"] == "pro"
        forged = client.post("/webhooks/billing/fake", content=b'{"id":"evt_x"}',
                             headers={"Fake-Signature": "t=1,v1=00"})
        assert forged.status_code == 400
        assert client.post("/webhooks/billing/stripe", content=b"{}").status_code == 404
        assert provider.outbox == []


def test_the_worker_job_retries_an_early_event_until_it_applies(world, tmp_path):
    """Review Focus 1 through the Task 18 worker: ingest enqueues one durable
    job; the job is requeued while the customer is unknown and succeeds once
    it exists; a duplicate delivery adds no second job."""
    from webapp.worker.handlers import default_handlers
    from webapp.worker.runner import Worker
    conn, scope, provider, service, webhooks, inbox, clock = world
    session_id = service.start_checkout(conn, scope, plan_id="pro", interval="month", now=NOW).split("/")[-1]
    provider.simulate(session_id, "pay")
    conn.execute("DELETE FROM checkout_sessions")
    conn.execute("DELETE FROM billing_customers")
    delivered = list(inbox)
    for headers, body in delivered + delivered:  # the provider redelivers
        webhooks.ingest(conn, headers=headers, body=body, now=NOW)
    conn.commit()
    jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'billing.webhook.process'").fetchone()[0]
    assert jobs == len(delivered)
    db_path = tmp_path / "db.sqlite3"  # the world fixture's database (redirected under --db postgres)
    worker = Worker(Settings(db_path=db_path), default_handlers(Settings(db_path=db_path), billing_webhooks=webhooks,
                                                                providers_factory=lambda: None),
                    clock=clock, worker_id="w1")
    worker.run_once()
    conn.rollback()
    statuses = {r[0] for r in conn.execute("SELECT status FROM jobs WHERE kind = 'billing.webhook.process'")}
    assert statuses == {"QUEUED"}  # retried later, not failed
    customer_id = provider.fetch_subscription(provider.subscription_for_session(session_id)).provider_customer_id
    conn.execute("INSERT INTO billing_customers (account_id, provider, provider_customer_id, created_at) "
                 "VALUES (?, 'fake', ?, ?)", (scope.account_id, customer_id, NOW.isoformat()))
    conn.commit()
    clock.now = NOW + timedelta(hours=7)  # past every backoff
    worker.run_once()
    conn.rollback()
    statuses = {r[0] for r in conn.execute("SELECT status FROM jobs WHERE kind = 'billing.webhook.process'")}
    assert statuses == {"SUCCEEDED"}
    assert _subscription(conn, scope.account_id)["state"] == "TRIALING"
