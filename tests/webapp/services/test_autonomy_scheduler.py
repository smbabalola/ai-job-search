from __future__ import annotations

import dataclasses
import random
from datetime import timedelta
from decimal import Decimal

import pytest

from webapp.persistence import autonomy_prepare as ap
from webapp.services import autonomy_scheduler as sched
from webapp.services.autonomy_providers import ProviderSet
from tests.webapp.services.autonomy_6c_fixtures import (  # noqa: F401
    ACCOUNT, NOW, enable_prepare, prepared_chain, ready_chain,
)

ENVELOPES = {"EVALUATE": Decimal("0.05"), "UNDERSTAND": Decimal("0.05"), "FIT": Decimal("0.10"),
             "INTELLIGENCE": Decimal("0.20")}


class Counting:
    """Wraps a provider, counts calls, and can inject a failure or a delay."""

    def __init__(self, inner, clock=None):
        self.inner, self.calls, self.fail_with, self.advance, self.clock, self.during = inner, 0, None, None, clock, None

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            self.calls += 1
            if self.during:
                self.during()
            if self.advance is not None:
                self.clock.now += self.advance
            if self.fail_with is not None:
                raise self.fail_with
            return attr(*a, **k)
        return call


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def world(tmp_path):
    """A freshly created job workspace (nothing prepared yet), enrolled, with
    PREPARE authority, an LLM budget and step envelopes."""
    from fastapi.testclient import TestClient
    from tests.webapp.fixtures.acceptance.fixtures import completion_ready_content_units, provider_candidate
    from tests.webapp.test_full_journey_acceptance import (
        AIFake, FakeSemanticProposalAdapter, UnderstandingFake, _close, _create_workspace, _install_extension,
        _install_profile, _settings,
    )
    from webapp.app import create_app
    from webapp.persistence.db import connect
    from webapp.services.autonomy_prepare import enrol
    base = _settings(tmp_path)
    _install_extension(base)
    client = TestClient(create_app(base))
    client.__enter__()
    _install_profile(base)
    ws = _create_workspace(client)
    conn = connect(base.db_path)
    enable_prepare(conn)
    enrol(conn, account_id=ACCOUNT, application_workspace_id=ws, actor="u", now=NOW)
    clock = Clock(NOW)
    providers = ProviderSet(
        Counting(UnderstandingFake(provider_candidate()), clock),
        Counting(FakeSemanticProposalAdapter(canned_response={"matches": [], "gates": []}), clock),
        Counting(AIFake({"content_units": completion_ready_content_units()}), clock))
    settings = dataclasses.replace(base, autonomy_max_capability="PREPARE", autonomy_scheduler_enabled=True,
                                   autonomy_step_cost_max=dict(ENVELOPES))
    yield conn, ws, settings, providers, clock
    conn.close()
    _close(client)


def tick(conn, settings, providers, clock, *, worker="w1", meter=None, rng=None):
    return sched.run_tick(conn, settings=settings, providers=providers, now=clock(), rng=rng or random.Random(1),
                          worker_id=worker, clock=clock, cost_meter=meter)


def attempts(conn, ws):
    return [(r["step_kind"], r["event"]) for r in ap.attempt_rows(conn, "APPLICATION", ws) if r["event"] != "STARTED"]


def eligible(conn, ws):
    row = conn.execute("SELECT next_eligible_at FROM autonomy_queue_items WHERE application_workspace_id = ?",
                       (ws,)).fetchone()
    return row[0] if row else None


def test_paid_steps_run_then_governing_questions_stop_before_more_spend(world):
    conn, ws, settings, providers, clock = world
    for _ in range(6):
        tick(conn, settings, providers, clock)
        clock.now += timedelta(seconds=1)
    assert [k for k, e in attempts(conn, ws) if e == "SUCCEEDED"] == ["UNDERSTAND", "FIT"]
    assert providers.intelligence.calls == 0  # no further spend while questions are open
    notes = ap.open_notifications(conn, ACCOUNT)
    assert [(n["kind"], n["detail"]["reason"]) for n in notes] == [("NEEDS_USER", "require_user")]
    rows = conn.execute("SELECT subject_type, status, settled_amount, settlement_ref FROM limit_reservations "
                        "WHERE subject_id = ?", (ws,)).fetchall()
    assert len(rows) == 4 and all(r["status"] == "CONSUMED" and r["settlement_ref"] for r in rows)
    started = [r for r in ap.attempt_rows(conn, "APPLICATION", ws) if r["event"] == "STARTED"]
    assert all(r["authorization_decision_id"] for r in started)


def _chain_world(conn, ws, settings):
    from webapp.services.autonomy_prepare import enrol
    enable_prepare(conn)
    enrol(conn, account_id=ACCOUNT, application_workspace_id=ws, actor="u", now=NOW)
    return dataclasses.replace(settings, autonomy_max_capability="PREPARE", autonomy_scheduler_enabled=True,
                               autonomy_step_cost_max=dict(ENVELOPES))


def test_system_review_then_judgment_items_need_the_user(prepared_chain):
    conn, ws, settings = prepared_chain
    s = _chain_world(conn, ws, settings)
    clock = Clock(NOW)
    for _ in range(3):
        tick(conn, s, ProviderSet(None, None, None), clock)
        clock.now += timedelta(seconds=1)
    assert attempts(conn, ws) == [("SYSTEM_REVIEW", "SUCCEEDED")]
    assert [(n["kind"], n["detail"]["reason"]) for n in ap.open_notifications(conn, ACCOUNT)] == [
        ("NEEDS_USER", "pack_review")]


def test_decided_chain_is_system_confirmed_prepared(ready_chain):
    from webapp.services.application_pack import SYSTEM_GATE4_NOTE
    conn, ws, settings = ready_chain
    s = _chain_world(conn, ws, settings)
    clock = Clock(NOW)
    for _ in range(3):
        tick(conn, s, ProviderSet(None, None, None), clock)
        clock.now += timedelta(seconds=1)
    assert attempts(conn, ws) == [("GATE4", "SUCCEEDED")]
    note = conn.execute("SELECT note FROM workflow_events WHERE workspace_id = ? AND new_status = 'drafted' "
                        "ORDER BY rowid DESC LIMIT 1", (ws,)).fetchone()[0]
    assert note == SYSTEM_GATE4_NOTE
    assert [n["kind"] for n in ap.open_notifications(conn, ACCOUNT)] == ["PREPARED"]
    assert conn.execute("SELECT COUNT(*) FROM autonomy_grants").fetchone()[0] == 0


def test_allow_none_runs_nothing(world):
    from product.autonomy_contract import Capability
    from webapp.services.autonomy_controls import set_capability
    conn, ws, settings, providers, clock = world
    set_capability(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT, capability=Capability.NONE,
                   actor="u", now=NOW)
    tick(conn, settings, providers, clock)
    assert attempts(conn, ws) == [] and providers.understanding.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM limit_reservations").fetchone()[0] == 0


def test_scheduler_gate_off_and_halt_run_no_work_but_sweeps_still_run(world, monkeypatch):
    from webapp.services.autonomy_controls import engage_kill_switch
    conn, ws, settings, providers, clock = world
    swept = []
    monkeypatch.setattr(sched, "expire_grants", lambda c, now: swept.append(now) or 0)
    tick(conn, dataclasses.replace(settings, autonomy_scheduler_enabled=False), providers, clock)
    engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    tick(conn, settings, providers, clock)
    assert len(swept) == 2 and attempts(conn, ws) == [] and providers.understanding.calls == 0


@pytest.mark.parametrize("control", ["pause", "unenrol", "halt"])
def test_control_change_between_lease_and_step_starts_nothing(world, monkeypatch, control):
    from webapp.services import autonomy_prepare_auth
    from webapp.services.autonomy_controls import engage_kill_switch_in_transaction, pause
    from webapp.services.autonomy_prepare import unenrol
    conn, ws, settings, providers, clock = world
    real = autonomy_prepare_auth.authorize_prepare

    def authorize_then_change(conn_, **kw):
        result = real(conn_, **kw)
        if control == "pause":
            pause(conn_, account_id=ACCOUNT, scope_type="APPLICATION", scope_id=ws, actor="u", reason="r", now=NOW)
        elif control == "unenrol":
            unenrol(conn_, account_id=ACCOUNT, application_workspace_id=ws, actor="u", now=NOW)
        else:
            from webapp.services.autonomy_controls import engage_kill_switch
            engage_kill_switch(conn_, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
        return result
    monkeypatch.setattr(sched, "authorize_prepare", authorize_then_change)
    tick(conn, settings, providers, clock)
    assert attempts(conn, ws) == [] and providers.understanding.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM limit_reservations").fetchone()[0] == 0


def test_halt_during_a_step_finishes_truthfully_and_schedules_nothing_further(world):
    from webapp.services.autonomy_controls import engage_kill_switch
    conn2 = None
    conn, ws, settings, providers, clock = world
    from webapp.persistence.db import connect
    conn2 = connect(settings.db_path)
    providers.understanding.during = lambda: engage_kill_switch(conn2, account_id=ACCOUNT, actor="u",
                                                                reason="stop", now=NOW)
    tick(conn, settings, providers, clock)
    conn2.close()
    assert attempts(conn, ws) == [("UNDERSTAND", "SUCCEEDED")]
    clock.now += timedelta(minutes=1)
    tick(conn, settings, providers, clock)
    assert attempts(conn, ws) == [("UNDERSTAND", "SUCCEEDED")] and providers.semantic_adapter.calls == 0


def test_sentinel_halt_latches_and_only_resume_all_then_fresh_authorization_resumes(world):
    from webapp.services.autonomy_controls import resume_all
    from webapp.services.autonomy_prepare_auth import authorize_prepare
    conn, ws, settings, providers, clock = world
    auth = lambda: authorize_prepare(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=ws,
                                     now=clock())
    before = auth()
    assert before.permitted
    sentinel = settings.autonomy_sentinel_path
    sentinel.write_text("halt")
    tick(conn, settings, providers, clock)  # the scheduler observes and latches the halt
    assert attempts(conn, ws) == [] and providers.understanding.calls == 0
    sentinel.unlink()
    clock.now += timedelta(minutes=5)
    tick(conn, settings, providers, clock)  # deleting the file alone resumes nothing
    assert attempts(conn, ws) == [] and providers.understanding.calls == 0
    resume_all(conn, account_id=ACCOUNT, actor="u", reason="go", now=clock(), sentinel_path=sentinel)
    fresh = auth()
    assert fresh.permitted and not fresh.reused and fresh.decision_id != before.decision_id
    again = auth()
    assert again.reused and again.decision_id == fresh.decision_id
    tick(conn, settings, providers, clock)
    assert attempts(conn, ws) == [("UNDERSTAND", "SUCCEEDED")]


def test_expired_lease_cannot_finalize_even_if_not_retaken(world):
    conn, ws, settings, providers, clock = world
    providers.understanding.advance = timedelta(seconds=settings.autonomy_step_timeout + settings.autonomy_lease_margin
                                                + 5)
    tick(conn, settings, providers, clock)  # the call outlives the lease
    assert attempts(conn, ws) == []  # the late worker wrote no terminal row
    providers.understanding.advance = None
    clock.now += timedelta(seconds=1)
    tick(conn, settings, providers, clock)  # recovery: abandon + settle at the hard maximum
    rows = ap.attempt_rows(conn, "APPLICATION", ws)
    assert ("UNDERSTAND", "ABANDONED") in [(r["step_kind"], r["event"]) for r in rows]
    settled = conn.execute("SELECT settled_amount FROM limit_reservations WHERE counter_name = 'budget:LLM:day' "
                           "ORDER BY created_at LIMIT 1").fetchone()[0]
    assert Decimal(settled) == ENVELOPES["UNDERSTAND"]
    # The late call's result was fenced out (never current), so the step runs
    # again under a live lease.
    assert providers.understanding.calls == 2


def test_transient_failures_retry_60_300_900_then_escalate_and_one_shot_retry(world):
    from webapp.services.autonomy_inbox import retry_failure
    conn, ws, settings, providers, clock = world
    providers.understanding.fail_with = TimeoutError("slow provider")
    delays = []
    for _ in range(4):
        tick(conn, settings, providers, clock)
        nxt = eligible(conn, ws)
        if nxt is None:
            break
        from product.autonomy_contract import parse_utc
        delays.append((parse_utc(nxt) - clock.now).total_seconds())
        clock.now = parse_utc(nxt)
    assert len(delays) == 3
    for got, base in zip(delays, (60, 300, 900)):
        assert 0.8 * base <= got <= 1.2 * base
    assert eligible(conn, ws) is None  # escalated after the 4th failure
    kinds = [n["kind"] for n in ap.open_notifications(conn, ACCOUNT)]
    assert "OPERATIONAL_ERROR" in kinds
    request = retry_failure(conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=ws,
                            step_kind="UNDERSTAND", actor="u", now=clock.now)
    tick(conn, settings, providers, clock)
    last = ap.attempt_rows(conn, "APPLICATION", ws)[-1]
    assert last["retry_request_id"] == request["id"]


def test_human_fixable_failure_needs_user_immediately(world):
    conn, ws, settings, providers, clock = world
    providers.understanding.fail_with = type("AuthenticationError", (Exception,), {})("no key")
    tick(conn, settings, providers, clock)
    assert eligible(conn, ws) is None
    assert [n["kind"] for n in ap.open_notifications(conn, ACCOUNT)] == ["NEEDS_USER"]


def test_cost_overage_is_charged_truthfully_and_blocks_the_step_kind(world):
    conn, ws, settings, providers, clock = world

    class Overage:
        def actual_cost(self, step_kind, *, reserved):
            return reserved * 3
    tick(conn, settings, providers, clock, meter=Overage())
    settled = [Decimal(r[0]) for r in conn.execute(
        "SELECT settled_amount FROM limit_reservations WHERE counter_name = 'budget:LLM:day'")]
    assert settled == [ENVELOPES["UNDERSTAND"] * 3]
    assert ("UNDERSTAND", "FAILED") in attempts(conn, ws)
    assert "OPERATIONAL_ERROR" in [n["kind"] for n in ap.open_notifications(conn, ACCOUNT)]
    ap.wake(conn, queue="APPLICATION", item_id=ws, now=clock.now)
    conn.commit()
    tick(conn, settings, providers, clock, meter=Overage())
    assert providers.understanding.calls == 1  # refused until the envelope changes


@pytest.mark.parametrize("missing", ["budget", "envelope"])
def test_missing_budget_or_envelope_fails_closed(world, missing):
    conn, ws, settings, providers, clock = world
    if missing == "budget":
        enable_prepare(conn, llm_per_day=None)
    else:
        settings = dataclasses.replace(settings, autonomy_step_cost_max={})
    tick(conn, settings, providers, clock)
    assert providers.understanding.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM limit_reservations").fetchone()[0] == 0
    assert [n["kind"] for n in ap.open_notifications(conn, ACCOUNT)] == ["NEEDS_USER"]


def test_fairness_processes_applications_despite_a_candidate_backlog(world):
    conn, ws, settings, providers, clock = world
    for i in range(20):
        conn.execute("INSERT INTO autonomy_candidate_queue (candidate_id, account_id, search_workspace_id, "
                     "next_eligible_at, updated_at) VALUES (?, ?, 'search_default', ?, ?)",
                     (f"cand_{i:02d}", ACCOUNT, "2026-01-01T00:00:00.000000+00:00", "x"))
    conn.commit()
    report = tick(conn, settings, providers, clock)
    assert report.processed and report.processed[0]["subject_type"] == "APPLICATION"
    assert providers.understanding.calls == 1


def test_driver_start_wakes_all_enrolled_and_candidates(world):
    conn, ws, settings, providers, clock = world
    ap.set_dormant(conn, queue="APPLICATION", item_id=ws, now=NOW)
    ap.enqueue_candidate(conn, candidate_id="cand_x", account_id=ACCOUNT, search_workspace_id="search_default",
                         now=NOW)
    ap.set_dormant(conn, queue="CANDIDATE", item_id="cand_x", now=NOW)
    conn.commit()
    assert sched.wake_all_on_start(conn, now=NOW) == 2
    assert eligible(conn, ws) is not None


def test_driver_never_overlaps_its_own_ticks(world, monkeypatch):
    import threading
    conn, ws, settings, providers, clock = world
    active, calls, stop = {"n": 0, "max": 0}, [], threading.Event()

    def fake_tick(c, **kw):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        calls.append(kw["now"])
        active["n"] -= 1
        if len(calls) == 3:
            stop.set()
        return sched.TickReport({}, [])
    monkeypatch.setattr(sched, "run_tick", fake_tick)
    sched.run_driver(dataclasses.replace(settings, autonomy_tick_interval=0.01), providers, stop=stop, clock=clock,
                     rng=random.Random(1), worker_id="driver")
    assert len(calls) == 3 and active["max"] == 1


def test_candidate_evaluation_attempt_has_no_decision_id_and_a_candidate_reservation(world, monkeypatch):
    from tests.webapp.services.autonomy_6c_fixtures import discover, portal_job
    from webapp.services import autonomy_candidates
    conn, ws, settings, providers, clock = world
    from webapp.services.autonomy_prepare import unenrol
    unenrol(conn, account_id=ACCOUNT, application_workspace_id=ws, actor="u", now=NOW)
    cid = discover(conn, [portal_job("sched-1")])["candidate_ids"][0]
    ap.wake(conn, queue="CANDIDATE", item_id=cid, now=NOW)  # discovery enqueued at wall-clock time
    conn.commit()
    monkeypatch.setattr(autonomy_candidates, "_fit_state", lambda c, **k: (None, False))
    monkeypatch.setattr(autonomy_candidates, "run_candidate_evaluation", lambda c, **k: {"id": "dsfit_fake"})
    tick(conn, settings, providers, clock)
    rows = ap.attempt_rows(conn, "CANDIDATE", cid)
    assert [r["event"] for r in rows] == ["STARTED", "SUCCEEDED"]
    assert all(r["authorization_decision_id"] is None for r in rows)
    res = conn.execute("SELECT subject_type, status FROM limit_reservations WHERE subject_id = ?", (cid,)).fetchone()
    assert tuple(res) == ("CANDIDATE", "CONSUMED")


def test_cli_once_does_nothing_with_the_gate_off(tmp_path, monkeypatch, capsys):
    from webapp import autonomy_worker
    monkeypatch.delenv("JOBSEARCH_AUTONOMY_SCHEDULER", raising=False)
    assert autonomy_worker.main(["--once", "--db", str(tmp_path / "db.sqlite3")]) == 0
    assert "scheduler is disabled" in capsys.readouterr().out
