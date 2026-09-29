"""6D-B concurrency (spec §11.3, §11.4): separate WAL connections released
together by a barrier. Every scenario runs 20 times."""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from product.fill_constants import RUN_LEASE_TTL
from tests.webapp.services.fill_fixtures import NOW, V2_ACCOUNT, grant_world, v2_chain  # noqa: F401
from tests.webapp.services.test_fill_actions import filling, page_after, precheck
from tests.webapp.services.test_fill_runs import to_active
from webapp.persistence import fill as f
from webapp.persistence.db import connect
from webapp.services import fill_actions as fa
from webapp.services import fill_runs as fr

REPEATS = range(20)
MS = timedelta(milliseconds=1)


def race(w, *jobs):
    """Run each job(conn) on its own connection, released together."""
    barrier, results = threading.Barrier(len(jobs)), [None] * len(jobs)

    def runner(i, job):
        conn = connect(w.settings.db_path)
        try:
            barrier.wait()
            results[i] = ("ok", job(conn))
        except Exception as exc:  # recorded, asserted by the caller
            results[i] = ("error", exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=runner, args=(i, job)) for i, job in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.mark.parametrize("attempt", REPEATS)
def test_two_starts_for_one_application_make_one_run(grant_world, attempt):
    w = grant_world

    def start(tab):
        return lambda conn: fr.start_run(conn, settings=w.settings, account_id=V2_ACCOUNT, handoff_session_id="hs",
                                         application_workspace_id=w.ws, executor_instance_id="ex",
                                         browser_session_id="bs", execution_tab_id=tab, now=NOW)

    results = race(w, start(1), start(2))
    assert sorted(r[0] for r in results) == ["error", "ok"], results
    refused = next(r[1] for r in results if r[0] == "error")
    assert isinstance(refused, fr.FillRefused) and refused.reason == "run_active"
    assert w.conn.execute("SELECT COUNT(*) FROM fill_runs").fetchone()[0] == 1
    assert w.conn.execute("SELECT COUNT(*) FROM active_fill_runs").fetchone()[0] == 1


@pytest.mark.parametrize("attempt", REPEATS)
def test_duplicate_intents_issue_one_envelope(grant_world, attempt):
    w = grant_world
    run = filling(w)
    check = precheck(w, run, 0)
    job = lambda conn: fa.request_intent(conn, settings=w.settings, run_id=run["id"], action_index=0,  # noqa: E731
                                         precheck=check, now=NOW)
    results = race(w, job, job)
    assert [r[0] for r in results] == ["ok", "ok"], results
    ids = {r[1].envelope.envelope_id for r in results}
    assert len(ids) == 1 and None not in ids
    assert w.conn.execute("SELECT COUNT(*) FROM fill_action_events WHERE event = 'ENVELOPE_ISSUED'").fetchone()[0] == 1
    assert w.conn.execute("SELECT COUNT(*) FROM fill_action_events WHERE event = 'WRITE_INTENT'").fetchone()[0] == 1


@pytest.mark.parametrize("attempt", REPEATS)
def test_concurrent_outcomes_for_one_action_accept_one(grant_world, attempt):
    w = grant_world
    run = filling(w)
    env = fa.request_intent(w.conn, settings=w.settings, run_id=run["id"], action_index=0,
                            precheck=precheck(w, run, 0), now=NOW).envelope
    post = page_after(w, 1)
    plan = f.get_plan_by_hash(w.conn, w.plan_hash)["plan"]

    def report(outcome):
        return lambda conn: fa.record_outcome(conn, run_id=run["id"], action_index=0, envelope_id=env.envelope_id,
                                              outcome=outcome, readback_hash=plan["actions"][0]["rendered_value_hash"],
                                              post_observation=post, now=NOW)

    results = race(w, report("WRITTEN_VERIFIED"), report("NOOP_ALREADY_EQUAL"))
    assert sorted(r[0] for r in results) == ["error", "ok"], results
    assert isinstance(next(r[1] for r in results if r[0] == "error"), fr.FillRefused)
    assert w.conn.execute("SELECT COUNT(*) FROM fill_action_events WHERE event = 'OUTCOME'").fetchone()[0] == 1


@pytest.mark.parametrize("attempt", REPEATS)
def test_a_lease_reap_racing_a_heartbeat_is_consistent(grant_world, attempt):
    w = grant_world
    run = to_active(w)
    late = NOW + RUN_LEASE_TTL + MS
    results = race(w, lambda conn: fr.reap_expired_leases(conn, now=late),
                   lambda conn: fr.heartbeat(conn, run_id=run["id"], now=late))
    assert all(r[0] == "ok" or isinstance(r[1], fr.FillRefused) for r in results), results
    # Exactly one reaping happened, the run is stopped, and nothing revived it.
    stops = [e for e in f.run_events(w.conn, run["id"]) if e["event"] == "FILL_STOPPED"]
    assert [(e["reason"]) for e in stops] == ["EXECUTOR_LOST"]
    assert f.get_lease(w.conn, run["id"]) is None and f.active_run_for(w.conn, w.ws) is None
