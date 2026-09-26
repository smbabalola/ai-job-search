"""Bundle 6C races (spec §15): real threads, one connection each, WAL.
Every race asserts exactly one winner and no partial state."""
from __future__ import annotations

import dataclasses
import random
import threading
from datetime import timedelta
from decimal import Decimal

import pytest

from webapp.persistence import autonomy_prepare as ap
from webapp.persistence.db import connect
from webapp.services import autonomy_candidates as ac
from webapp.services.autonomy_controls import run_immediate
from tests.webapp.services.autonomy_6c_fixtures import (  # noqa: F401
    ACCOUNT, NOW, add_fit, conn, discover, enable_prepare, fresh_fits, make_workspace, portal_job, settings_6c,
)
from tests.webapp.services.test_autonomy_scheduler import world  # noqa: F401

SW = "search_default"


def race(db_path, *work):
    """Run each callable(conn) on its own thread and connection, released
    together by a barrier. Returns their results in order; re-raises."""
    barrier = threading.Barrier(len(work))
    results, errors = [None] * len(work), []

    def run(i, fn):
        c = connect(db_path)
        try:
            barrier.wait()
            results[i] = fn(c)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
        finally:
            c.close()
    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(work)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    if errors:
        raise errors[0]
    return results


def _db(conn):
    return conn.execute("PRAGMA database_list").fetchone()["file"]


def _lease(queue, item_id, worker):
    return lambda c: run_immediate(c, lambda: ap.acquire_lease(c, queue=queue, item_id=item_id, worker_id=worker,
                                                              now=NOW, ttl=timedelta(minutes=5)))


def test_two_workers_one_application_lease(conn):
    ws = make_workspace(conn)
    ap.enqueue_application(conn, application_workspace_id=ws, account_id=ACCOUNT, now=NOW)
    conn.commit()
    got = race(_db(conn), _lease("APPLICATION", ws, "w1"), _lease("APPLICATION", ws, "w2"))
    assert sorted(g is not None for g in got) == [False, True]
    row = conn.execute("SELECT lease_holder, lease_generation FROM autonomy_queue_items "
                       "WHERE application_workspace_id = ?", (ws,)).fetchone()
    winner = "w1" if got[0] is not None else "w2"
    assert row["lease_holder"] == winner and row["lease_generation"] == 1


def test_two_workers_one_candidate_lease(conn):
    ap.enqueue_candidate(conn, candidate_id="cand_race", account_id=ACCOUNT, search_workspace_id=SW, now=NOW)
    conn.commit()
    got = race(_db(conn), _lease("CANDIDATE", "cand_race", "w1"), _lease("CANDIDATE", "cand_race", "w2"))
    assert sorted(g is not None for g in got) == [False, True]
    row = conn.execute("SELECT lease_generation FROM autonomy_candidate_queue WHERE candidate_id = 'cand_race'"
                       ).fetchone()
    assert row[0] == 1


def test_the_last_budget_unit_has_one_winner(conn):
    from webapp.persistence.autonomy_ledger import budget_usage, reserve_within_cap
    ws = make_workspace(conn)
    conn.commit()

    def reserve(c):
        return run_immediate(c, lambda: reserve_within_cap(
            c, account_id=ACCOUNT, counter_name="LLM:day", window_key="2026-09-24", cap=Decimal("0.10"),
            amount=Decimal("0.10"), subject_type="APPLICATION", subject_id=ws, now=NOW))
    got = race(_db(conn), reserve, reserve, reserve)
    assert sum(g is not None for g in got) == 1
    assert budget_usage(conn, account_id=ACCOUNT, counter_name="LLM:day", window_key="2026-09-24") == Decimal("0.10")
    assert conn.execute("SELECT COUNT(*) FROM limit_reservations").fetchone()[0] == 1


@pytest.fixture
def candidates(conn, tmp_path, monkeypatch):
    fresh_fits(monkeypatch)
    enable_prepare(conn)
    return settings_6c(tmp_path)


def _screened(conn, settings, record_id, company):
    cid = discover(conn, [portal_job(record_id, company=company)])["candidate_ids"][0]
    add_fit(conn, cid)
    ctx = ac.build_candidate_context(conn, settings=settings, account_id=ACCOUNT, search_workspace_id=SW,
                                     candidate_id=cid, now=NOW)
    row, _ = ac.screen_candidate(conn, ctx=ctx, now=NOW)
    conn.commit()
    assert row["outcome"] == "PROMOTE"
    return cid, row


def _promote(settings, cid, row):
    return lambda c: ac.promote_candidate(c, settings=settings, account_id=ACCOUNT, search_workspace_id=SW,
                                          candidate_id=cid, screening_id=row["id"], actor_type="SCHEDULER",
                                          actor="scheduler", now=NOW)


def _promotion_state(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
        "workspaces", "autonomy_candidate_promotions", "autonomy_enrolments", "autonomy_queue_items")}


def test_the_promotions_cap_has_one_winner(conn, candidates):
    settings = dataclasses.replace(candidates, autonomy_max_promotions_per_day=1)
    a = _screened(conn, settings, "cap1", "Acme Drilling")
    b = _screened(conn, settings, "cap2", "Brine Works")
    before = _promotion_state(conn)
    got = race(_db(conn), _promote(settings, *a), _promote(settings, *b))
    assert sum(g is not None for g in got) == 1
    after = _promotion_state(conn)
    assert {k: after[k] - before[k] for k in after} == {
        "workspaces": 1, "autonomy_candidate_promotions": 1, "autonomy_enrolments": 1, "autonomy_queue_items": 1}
    assert conn.execute("SELECT COUNT(*) FROM limit_reservations WHERE counter_name = 'promotions:day'"
                        ).fetchone()[0] == 1


def test_two_promotions_of_one_candidate_have_one_winner(conn, candidates):
    cid, row = _screened(conn, candidates, "same1", "Acme Drilling")
    before = _promotion_state(conn)
    got = race(_db(conn), _promote(candidates, cid, row), _promote(candidates, cid, row))
    assert sum(g is not None for g in got) == 1
    after = _promotion_state(conn)
    assert {k: after[k] - before[k] for k in after} == {
        "workspaces": 1, "autonomy_candidate_promotions": 1, "autonomy_enrolments": 1, "autonomy_queue_items": 1}


def test_the_kill_switch_racing_a_tick_leaves_no_partial_state(world):  # noqa: F811
    from webapp.services import autonomy_scheduler as sched
    from webapp.services.autonomy_controls import engage_kill_switch
    conn, ws, settings, providers, clock = world
    conn.commit()
    db = settings.db_path

    def tick(c):
        return sched.run_tick(c, settings=settings, providers=providers, now=clock(), rng=random.Random(1),
                              worker_id="w1", clock=clock)

    def halt(c):
        return engage_kill_switch(c, account_id=ACCOUNT, actor="u", reason="race", now=NOW)
    race(db, tick, halt)
    rows = ap.attempt_rows(conn, "APPLICATION", ws)
    started = {r["attempt_id"] for r in rows if r["event"] == "STARTED"}
    finished = {r["attempt_id"] for r in rows if r["event"] != "STARTED"}
    assert started == finished  # every started step finished truthfully
    assert conn.execute("SELECT COUNT(*) FROM limit_reservations WHERE status = 'RESERVED'").fetchone()[0] == 0
    # once halted, nothing new starts
    clock.now += timedelta(hours=1)
    tick(conn)
    assert len(ap.attempt_rows(conn, "APPLICATION", ws)) == len(rows)
    assert conn.execute("SELECT COUNT(*) FROM autonomy_grants").fetchone()[0] == 0
