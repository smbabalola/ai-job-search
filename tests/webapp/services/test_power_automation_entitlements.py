"""Bundle 7 spec §11.3, §20.3 (Task 26): autonomous prepare is bounded by the
plan. The effective capability is min(the user's authorization, the
entitlement, the deployment ceiling, AUTOMATION_ENABLED), read at every tick;
autonomous prepares consume plan units; a downgrade or suspension never
strands a step but starts nothing new (Review Focus 5)."""
from __future__ import annotations

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest

from product.autonomy_contract import Capability
from product.entitlements import load_catalog
from webapp.billing.fake import FakeBillingProvider
from webapp.persistence.db import connect
from webapp.services import search_schedules as schedules
from webapp.services.autonomy_controls import set_capability
from webapp.services.autonomy_entitlements import automation_capability
from webapp.services.billing import BillingService
from webapp.services.billing_webhooks import BillingWebhooks
from webapp.services.entitlements import gate_for, set_platform_control
from webapp.services.ownership import AccountScope
from webapp.services.usage import Metering, UsageService
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, NOW
from tests.webapp.services.test_autonomy_scheduler import attempts, tick, world  # noqa: F401

DEV = load_catalog(Path(__file__).parents[3] / "product" / "plans" / "plan-catalog.dev.json")
SECRET = b"fake-webhook-secret"


class Billing:
    """The fake provider end to end: checkout → webhook → subscription state."""

    def __init__(self, settings, clock):
        self.settings, self.clock, self.inbox = settings, clock, []
        self.provider = FakeBillingProvider(None, SECRET, clock, price_map=DEV,
                                            deliver=lambda headers, body: self.inbox.append((headers, body)))
        self.service = BillingService(self.provider, DEV, settings=settings)
        self.webhooks = BillingWebhooks(self.provider, catalog_version=DEV.catalog_version, settings=settings)
        self.subscription = None

    def deliver(self):
        conn = connect(self.settings)
        try:
            for headers, body in self.inbox:
                self.webhooks.ingest(conn, headers=headers, body=body, now=self.clock())
            self.inbox.clear()
            self.webhooks.process_pending(conn, now=self.clock())
            conn.commit()
        finally:
            conn.close()

    def subscribe(self, conn, plan_id):
        scope = AccountScope(account_id=ACCOUNT, profile_root=self.settings.profile_root, user_id=None)
        session_id = self.service.start_checkout(conn, scope, plan_id=plan_id, interval="month",
                                                 now=self.clock()).split("/")[-1]
        conn.commit()
        self.provider.simulate(session_id, "pay")
        self.subscription = self.provider.subscription_for_session(session_id)
        self.deliver()

    def change_to(self, plan_id):
        self.provider.change_plan(subscription_id=self.subscription,
                                  price_id=DEV.plan(plan_id).provider_prices["month"], when="now")
        self.deliver()


@pytest.fixture
def power(world):  # noqa: F811
    conn, ws, settings, providers, clock = world
    settings = dataclasses.replace(settings, auth_required_in_local=True)  # a metered account
    set_platform_control(conn, "AUTOMATION_ENABLED", True, actor_user_id=None, reason="test", now=NOW)
    conn.commit()
    billing = Billing(settings, clock)
    billing.subscribe(conn, "power")
    yield conn, ws, settings, providers, clock, billing


def _metering(settings):
    gate = gate_for(settings)
    return Metering(gate, UsageService(gate), enforced=True)


def _units(conn, status=None):
    rows = conn.execute("SELECT allowance, status FROM usage_reservations WHERE account_id = ? ORDER BY allowance",
                        (ACCOUNT,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def _scope(settings):
    return AccountScope(account_id=ACCOUNT, profile_root=settings.profile_root, user_id=None)


def _schedule(conn, ws, settings):
    from webapp.persistence.search_workspaces import create_search_workspace
    sws = create_search_workspace(conn, name="Saved search", account_id=ACCOUNT)["id"]
    schedules.enable_schedule(conn, _scope(settings), search_workspace_id=sws, cadence="DAILY", now=NOW,
                              metering=_metering(settings))
    return sws


def test_the_capability_is_the_minimum_of_authorization_entitlement_ceiling_and_control(power):
    conn, ws, settings, providers, clock, billing = power
    assert automation_capability(conn, ACCOUNT, settings=settings, now=clock()) is Capability.PREPARE
    no_ceiling = dataclasses.replace(settings, autonomy_max_capability="NONE")
    assert automation_capability(conn, ACCOUNT, settings=no_ceiling, now=clock()) is Capability.NONE
    set_platform_control(conn, "AUTOMATION_ENABLED", False, actor_user_id=None, reason="ops", now=NOW)
    conn.commit()
    assert automation_capability(conn, ACCOUNT, settings=settings, now=clock()) is Capability.NONE
    set_platform_control(conn, "AUTOMATION_ENABLED", True, actor_user_id=None, reason="ops", now=NOW)
    conn.commit()
    set_capability(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT, capability=Capability.NONE,
                   actor="u", now=NOW)
    assert automation_capability(conn, ACCOUNT, settings=settings, now=clock()) is Capability.NONE  # no user authorization


def test_pro_has_no_automation_capability(power):
    conn, ws, settings, providers, clock, billing = power
    billing.change_to("pro")
    assert automation_capability(conn, ACCOUNT, settings=settings, now=clock()) is Capability.NONE
    tick(conn, settings, providers, clock)
    assert attempts(conn, ws) == [] and providers.understanding.calls == 0


def test_an_autonomous_prepare_consumes_a_prepare_and_an_automation_unit(power):
    conn, ws, settings, providers, clock, billing = power
    tick(conn, settings, providers, clock)
    assert attempts(conn, ws) == [("UNDERSTAND", "SUCCEEDED")]
    assert _units(conn) == [("applications.prepare", "CONSUMED"), ("automation.prepare", "CONSUMED")]
    clock.now += timedelta(seconds=1)
    tick(conn, settings, providers, clock)  # the next stage reuses the window's units at no charge
    assert _units(conn) == [("applications.prepare", "CONSUMED"), ("automation.prepare", "CONSUMED")]


def test_a_downgrade_mid_step_completes_and_consumes_then_starts_nothing_new(power):
    conn, ws, settings, providers, clock, billing = power
    sws = _schedule(conn, ws, settings)
    providers.understanding.during = lambda: billing.change_to("pro")  # the webhook lands while the step runs
    tick(conn, settings, providers, clock)
    providers.understanding.during = None
    assert attempts(conn, ws) == [("UNDERSTAND", "SUCCEEDED")]  # the step finished under its entitlement
    assert _units(conn) == [("applications.prepare", "CONSUMED"), ("automation.prepare", "CONSUMED")]
    schedule = schedules.get_schedule(conn, sws)
    assert (schedule["enabled"], schedule["disabled_reason"]) == (0, "ENTITLEMENT")  # disabled, not deleted
    for _ in range(3):
        clock.now += timedelta(seconds=1)
        tick(conn, settings, providers, clock)
    assert attempts(conn, ws) == [("UNDERSTAND", "SUCCEEDED")] and providers.semantic_adapter.calls == 0
    billing.change_to("power")  # re-upgrading alone restores nothing
    assert schedules.get_schedule(conn, sws)["enabled"] == 0
    schedules.enable_schedule(conn, _scope(settings), search_workspace_id=sws, cadence="DAILY", now=clock(),
                              metering=_metering(settings))
    assert schedules.get_schedule(conn, sws)["enabled"] == 1  # the user's enable does


def test_a_downgrade_mid_step_that_then_fails_releases_the_units(power):
    conn, ws, settings, providers, clock, billing = power
    providers.understanding.during = lambda: billing.change_to("pro")
    providers.understanding.fail_with = TimeoutError("provider timed out")
    tick(conn, settings, providers, clock)
    assert [e for _, e in attempts(conn, ws)] and "SUCCEEDED" not in [e for _, e in attempts(conn, ws)]
    assert _units(conn) == [("applications.prepare", "RELEASED"), ("automation.prepare", "RELEASED")]


def test_an_exhausted_automation_allowance_waits_for_the_next_window(power, monkeypatch):
    conn, ws, settings, providers, clock, billing = power
    gate = gate_for(settings)
    real = gate.entitlements

    def exhausted(*a, **k):
        resolved = real(*a, **k)
        return dataclasses.replace(resolved, allowances={**resolved.allowances, "automation.prepare": 0})
    monkeypatch.setattr(gate, "entitlements", exhausted)
    tick(conn, settings, providers, clock)
    assert attempts(conn, ws) == [] and providers.understanding.calls == 0 and _units(conn) == []
    row = conn.execute("SELECT next_eligible_at FROM autonomy_queue_items WHERE application_workspace_id = ?",
                       (ws,)).fetchone()
    assert row[0] is not None and row[0] > clock().isoformat()


def test_suspension_disables_schedules_pauses_the_queue_and_starts_nothing(power):
    conn, ws, settings, providers, clock, billing = power
    sws = _schedule(conn, ws, settings)
    conn.execute("UPDATE accounts SET status = 'SUSPENDED' WHERE id = ?", (ACCOUNT,))
    schedules.on_account_suspended(conn, ACCOUNT, now=clock())
    conn.commit()
    schedule = schedules.get_schedule(conn, sws)
    assert (schedule["enabled"], schedule["disabled_reason"]) == (0, "SUSPENDED")
    assert conn.execute("SELECT paused FROM autonomy_queue_items WHERE application_workspace_id = ?",
                        (ws,)).fetchone()[0] == 1
    assert automation_capability(conn, ACCOUNT, settings=settings, now=clock()) is Capability.NONE
    tick(conn, settings, providers, clock)
    assert attempts(conn, ws) == [] and providers.understanding.calls == 0


def test_the_local_operator_is_not_bounded_by_a_plan(world):  # noqa: F811
    conn, ws, settings, providers, clock = world  # unmetered: no subscription, no platform control
    assert automation_capability(conn, ACCOUNT, settings=settings, now=clock()) is Capability.PREPARE
    tick(conn, settings, providers, clock)
    assert attempts(conn, ws) == [("UNDERSTAND", "SUCCEEDED")]
    assert _units(conn) == []



@pytest.mark.parametrize("plan_id,evaluated", [("power", True), ("pro", False)])
def test_candidate_screening_needs_automation_screening(power, monkeypatch, plan_id, evaluated):
    from tests.webapp.services.autonomy_6c_fixtures import discover, portal_job
    from webapp.persistence import autonomy_prepare as ap
    from webapp.services import autonomy_candidates
    from webapp.services.autonomy_prepare import unenrol
    conn, ws, settings, providers, clock, billing = power
    unenrol(conn, account_id=ACCOUNT, application_workspace_id=ws, actor="u", now=NOW)
    cid = discover(conn, [portal_job("sched-1")])["candidate_ids"][0]
    ap.wake(conn, queue="CANDIDATE", item_id=cid, now=NOW)
    conn.commit()
    if plan_id != "power":
        billing.change_to(plan_id)
    monkeypatch.setattr(autonomy_candidates, "_fit_state", lambda c, **k: (None, False))
    monkeypatch.setattr(autonomy_candidates, "run_candidate_evaluation", lambda c, **k: {"id": "dsfit_fake"})
    report = tick(conn, settings, providers, clock)
    rows = ap.attempt_rows(conn, "CANDIDATE", cid)
    assert bool(rows) is evaluated
    if not evaluated:
        assert [p["action"] for p in report.processed if p["subject_id"] == cid] == ["not_entitled"]
