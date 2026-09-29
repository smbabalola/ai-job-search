"""6E-A concurrency (spec §14, J8): separate WAL connections released
together by a barrier (the 6D-B race helper). Every scenario runs 20 times."""
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.webapp.services.submit_fixtures import (  # noqa: F401
    NOW, SEC, V2_ACCOUNT, filled_world, grant_world, review_observation, v2_chain,
)
from tests.webapp.services.test_fill_concurrency import race
from tests.webapp.services.test_human_submit_authorize import ready
from tests.webapp.services.test_human_submit_preclick import verification
from tests.webapp.services.test_human_submit_results import evidence
from webapp.persistence import submit as sp
from webapp.persistence.autonomy_ledger import attempt_state, expire_grants, get_grant
from webapp.services import human_submit as hs
from webapp.services.autonomy import mark_stale_dispatches_ambiguous
from webapp.services.autonomy_controls import run_immediate

REPEATS = range(20)
TERMINAL = ("CONFIRMED_SUCCESS", "SUBMISSION_AMBIGUOUS", "SUBMISSION_FAILED", "EXPIRED_UNCLICKED")


def _authorize(w, h, at=NOW):
    return lambda conn: hs.authorize(conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                                     review_hash=h, actor="u", now=at)


def _authorized(w):
    out = hs.authorize(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                       review_hash=ready(w), actor="u", now=NOW)
    out["review"] = sp.get_authorization(w.conn, out["authorization_id"])["review"]
    return out


def _pre_click(w, auth, at):
    return lambda conn: hs.human_pre_click_commit(conn, settings=w.settings, run_id=w.run["id"],
                                                  grant_id=auth["grant_id"], observation=review_observation(w),
                                                  verification=verification(w, auth), now=at)


def _dispatched(w):
    auth = _authorized(w)
    attempt = _pre_click(w, auth, NOW + SEC)(w.conn)["attempt_id"]
    assert hs.human_record_click_dispatched(w.conn, settings=w.settings, attempt_id=attempt, now=NOW + 2 * SEC)
    return auth, attempt


def _terminal_events(w, attempt):
    marks = ",".join("?" * len(TERMINAL))
    return w.conn.execute(f"SELECT state FROM submission_attempt_events WHERE attempt_id = ? AND state IN ({marks})",
                          (attempt, *TERMINAL)).fetchall()


@pytest.mark.parametrize("_", REPEATS)
def test_two_authorizes_create_exactly_one(filled_world, _):
    h = ready(filled_world)
    results = race(filled_world, _authorize(filled_world, h), _authorize(filled_world, h))
    assert sorted(kind for kind, _ in results) == ["error", "ok"]
    assert [r for kind, r in results if kind == "error"][0].reason == "already_authorized"
    assert filled_world.conn.execute("SELECT COUNT(*) FROM human_submit_authorizations").fetchone()[0] == 1


@pytest.mark.parametrize("_", REPEATS)
def test_a_pre_click_racing_grant_expiry_never_authorizes_an_expired_grant(filled_world, _):
    auth = _authorized(filled_world)
    late = NOW + timedelta(seconds=121)
    results = race(filled_world, _pre_click(filled_world, auth, late),
                   lambda conn: run_immediate(conn, lambda: expire_grants(conn, now=late)))
    assert results[0][0] == "error" and results[0][1].reason in ("grant_not_consumable", "lease_expired")
    assert filled_world.conn.execute("SELECT COUNT(*) FROM submission_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("_", REPEATS)
def test_pre_click_racing_cancel_leaves_no_live_attempt_on_a_cancelled_authorization(filled_world, _):
    auth = _authorized(filled_world)
    results = race(filled_world, _pre_click(filled_world, auth, NOW + SEC),
                   lambda conn: hs.cancel_authorization(conn, account_id=V2_ACCOUNT,
                                                        authorization_id=auth["authorization_id"], actor="u",
                                                        now=NOW + SEC))
    cancelled = results[1][0] == "ok"
    attempts = filled_world.conn.execute("SELECT id FROM submission_attempts").fetchall()
    if cancelled:  # the cancel won: either no attempt at all, or the attempt was expired by the user
        assert all(attempt_state(filled_world.conn, a["id"]) == "EXPIRED_UNCLICKED" for a in attempts)
        live = filled_world.conn.execute("SELECT COUNT(*) FROM submission_intents WHERE state = 'CLAIMED'").fetchone()
        assert live[0] == 0
    else:  # the pre-click consumed the grant first and the attempt was already beyond cancellation
        assert results[0][0] == "ok"
    assert get_grant(filled_world.conn, auth["grant_id"])["status"] in ("REVOKED", "CONSUMED")


@pytest.mark.parametrize("_", REPEATS)
def test_a_result_report_racing_the_ambiguity_sweep_records_one_terminal_result(filled_world, _):
    auth, attempt = _dispatched(filled_world)
    results = race(filled_world,
                   lambda conn: hs.report_result(conn, settings=filled_world.settings, attempt_id=attempt,
                                                 evidence=evidence(success_observed=True), now=NOW + 10 * SEC),
                   lambda conn: mark_stale_dispatches_ambiguous(conn, now=NOW + 10 * SEC,
                                                                result_timeout=timedelta(seconds=0)))
    assert len(_terminal_events(filled_world, attempt)) == 1
    if results[0][0] == "error":
        assert results[0][1].reason == "result_already_recorded"


@pytest.mark.parametrize("_", REPEATS)
def test_two_result_reports_record_one(filled_world, _):
    auth, attempt = _dispatched(filled_world)
    report = lambda conn: hs.report_result(conn, settings=filled_world.settings, attempt_id=attempt,  # noqa: E731
                                           evidence=evidence(success_observed=True), now=NOW + 10 * SEC)
    results = race(filled_world, report, report)
    assert sorted(kind for kind, _ in results) == ["error", "ok"]
    assert len(_terminal_events(filled_world, attempt)) == 1
    assert filled_world.conn.execute("SELECT COUNT(*) FROM submission_results").fetchone()[0] == 1
