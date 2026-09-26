from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from webapp.persistence import autonomy_prepare as ap
from webapp.services import autonomy_candidates as ac
from webapp.services.autonomy_providers import ProviderSet
from tests.webapp.services.autonomy_6c_fixtures import (  # noqa: F401
    ACCOUNT, NOW, add_fit, conn, discover, enable_prepare, fresh_fits, portal_job, settings_6c,
)

SW = "search_default"


def _ctx(conn, settings, cid, now=NOW):
    return ac.build_candidate_context(conn, settings=settings, account_id=ACCOUNT, search_workspace_id=SW,
                                      candidate_id=cid, now=now)


def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
        "workspaces", "autonomy_candidate_promotions", "autonomy_enrolments", "limit_reservations",
        "autonomy_candidate_screenings", "autonomy_prepare_steps")}


@pytest.fixture
def world(conn, tmp_path, monkeypatch):
    fresh_fits(monkeypatch)
    enable_prepare(conn)
    return settings_6c(tmp_path)


def test_discovery_enqueues_finished_run_candidates_only_with_prepare_authority(conn):
    result = discover(conn, [portal_job("a1")])  # no authority yet
    assert conn.execute("SELECT COUNT(*) FROM autonomy_candidate_queue").fetchone()[0] == 0
    enable_prepare(conn)
    assert ac.enqueue_run_candidates(conn, run_id=result["run"]["id"], account_id=ACCOUNT, search_workspace_id=SW,
                                     now=NOW) == 1
    result2 = discover(conn, [portal_job("a2")])
    queued = {r[0] for r in conn.execute("SELECT candidate_id FROM autonomy_candidate_queue")}
    assert set(result2["candidate_ids"]) <= queued  # enqueued in the same transaction as run completion


def test_dismissed_and_failed_run_candidates_are_not_enqueued(conn):
    from webapp.persistence.discovery import set_discovery_candidate_status
    enable_prepare(conn)
    result = discover(conn, [portal_job("d1")])
    cid = result["candidate_ids"][0]
    conn.execute("DELETE FROM autonomy_candidate_queue")
    set_discovery_candidate_status(conn, cid, "dismissed")
    assert ac.enqueue_run_candidates(conn, run_id=result["run"]["id"], account_id=ACCOUNT, search_workspace_id=SW,
                                     now=NOW) == 0
    conn.execute("UPDATE discovery_runs SET status = 'failed' WHERE id = ?", (result["run"]["id"],))
    set_discovery_candidate_status(conn, cid, "new")
    assert ac.enqueue_run_candidates(conn, run_id=result["run"]["id"], account_id=ACCOUNT, search_workspace_id=SW,
                                     now=NOW) == 0


def test_context_budget_and_envelope_fail_closed(conn, tmp_path, monkeypatch):
    fresh_fits(monkeypatch)
    cid = discover(conn, [portal_job("b1")])["candidate_ids"][0]
    enable_prepare(conn, llm_per_day=None)
    ctx = _ctx(conn, settings_6c(tmp_path), cid)
    assert ctx.llm_budget_configured is False and ac.admit_candidate_evaluation(ctx).admitted is False
    enable_prepare(conn, llm_per_day="0.04")  # smaller than the 0.05 EVALUATE envelope
    ctx = _ctx(conn, settings_6c(tmp_path), cid)
    assert ctx.llm_budget_configured and not ctx.budget_available
    ctx = _ctx(conn, settings_6c(tmp_path, autonomy_step_cost_max={}), cid)
    assert ctx.evaluate_envelope_present is False


def test_candidate_evaluation_call_never_reserves_or_records_attempts(conn, world, monkeypatch):
    cid = discover(conn, [portal_job("e1")])["candidate_ids"][0]
    calls = []
    monkeypatch.setattr("webapp.services.discovery.evaluate_discovery_candidate",
                        lambda *a, **k: calls.append(k) or {"id": "dsfit_x"})
    before = _counts(conn)
    ac.run_candidate_evaluation(conn, settings=world, providers=ProviderSet("U", "S", "I"),
                                ctx=_ctx(conn, world, cid), request_id="req")
    assert calls and calls[0]["active_extensions"] == [] and _counts(conn) == before


def test_screening_persists_and_promote_is_the_next_action(conn, world):
    cid = discover(conn, [portal_job("s1")])["candidate_ids"][0]
    assert ac.candidate_next_action(conn, _ctx(conn, world, cid)) == "EVALUATE"
    add_fit(conn, cid)
    ctx = _ctx(conn, world, cid)
    assert ac.candidate_next_action(conn, ctx) == "SCREEN"
    row, next_at = ac.screen_candidate(conn, ctx=ctx, now=NOW)
    conn.commit()  # screen_candidate never commits; the scheduler owns the transaction
    assert row["outcome"] == "PROMOTE" and next_at == NOW
    assert ac.candidate_next_action(conn, _ctx(conn, world, cid)) == "PROMOTE"


def _ask_on_unknown_fit(conn):
    from product.standing_policy import default_policy_document
    from webapp.persistence.autonomy_authority import save_policy_version
    doc = default_policy_document("Europe/London")
    doc["limits"]["budgets"] = {"LLM": {"per_day": "5.00", "per_application": "1.00"}}
    doc["rules"] = [{"id": "fit_unknown", "description": "", "when": {"attr": "fit.overall_score", "op": "lt",
                     "value": 50}, "effect": {"type": "REQUIRE_USER"}, "on_unknown": {"type": "REQUIRE_USER"}}]
    save_policy_version(conn, account_id=ACCOUNT, doc=doc, created_by="u", now=NOW)


def test_require_user_opens_one_exception_and_notification_only_when_it_could_unlock(conn, world):
    _ask_on_unknown_fit(conn)
    cid = discover(conn, [portal_job("q1")])["candidate_ids"][0]
    add_fit(conn, cid, score=None)
    row, next_at = ac.screen_candidate(conn, ctx=_ctx(conn, world, cid), now=NOW)
    conn.commit()  # screen_candidate never commits; the scheduler owns the transaction
    assert row["outcome"] == "REQUIRE_USER" and row["could_unlock"] and next_at is None
    ac.screen_candidate(conn, ctx=_ctx(conn, world, cid), now=NOW + timedelta(minutes=1))
    conn.commit()  # screen_candidate never commits; the scheduler owns the transaction
    assert len(ap.open_candidate_exceptions(conn, ACCOUNT)) == 1
    assert [n["kind"] for n in ap.open_notifications(conn, ACCOUNT)] == ["CANDIDATE_QUESTION"]


def test_structural_obstacle_with_require_user_opens_no_question(conn, world, monkeypatch):
    from product.autonomy_contract import IdentityStrength
    _ask_on_unknown_fit(conn)
    cid = discover(conn, [portal_job("q2")])["candidate_ids"][0]
    add_fit(conn, cid, score=None)
    weak = dataclasses.replace(_ctx(conn, world, cid), identity_key=None, identity_strength=IdentityStrength.WEAK)
    row, _ = ac.screen_candidate(conn, ctx=weak, now=NOW)
    conn.commit()  # screen_candidate never commits; the scheduler owns the transaction
    assert row["outcome"] == "NOT_ELIGIBLE" and not row["could_unlock"]
    assert ap.open_candidate_exceptions(conn, ACCOUNT) == [] and ap.open_notifications(conn, ACCOUNT) == []


def _screened(conn, world, record_id):
    cid = discover(conn, [portal_job(record_id)])["candidate_ids"][0]
    add_fit(conn, cid)
    row, _ = ac.screen_candidate(conn, ctx=_ctx(conn, world, cid), now=NOW)
    conn.commit()  # screen_candidate never commits; the scheduler owns the transaction
    assert row["outcome"] == "PROMOTE"
    return cid, row


def _promote(conn, settings, cid, row, now=NOW):
    return ac.promote_candidate(conn, settings=settings, account_id=ACCOUNT, search_workspace_id=SW,
                                candidate_id=cid, screening_id=row["id"], actor_type="SCHEDULER",
                                actor="scheduler", now=now)


def test_scheduler_promotion_creates_enrols_enqueues_and_consumes_the_cap(conn, world):
    cid, row = _screened(conn, world, "p1")
    out = _promote(conn, world, cid, row)
    ws = out["application_workspace_id"]
    assert ap.is_enrolled(conn, ws) and ap.promotion_for_candidate(conn, cid)["actor_type"] == "SCHEDULER"
    assert conn.execute("SELECT COUNT(*) FROM autonomy_queue_items WHERE application_workspace_id = ?",
                        (ws,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM limit_reservations WHERE counter_name = 'promotions:day' "
                        "AND status = 'CONSUMED'").fetchone()[0] == 1


def test_rediscovered_identity_is_not_promoted_twice(conn, world):
    from webapp.persistence.discovery import get_discovery_candidate
    from webapp.services.pipeline import create_job_from_source_record
    cid, row = _screened(conn, world, "r1")
    _promote(conn, world, cid, row)
    # Rediscovering the same job in the same search workspace resolves to the
    # same (now promoted) candidate: nothing further to do.
    again = discover(conn, [portal_job("r1")])["candidate_ids"]
    assert again == [cid] and ac.candidate_next_action(conn, _ctx(conn, world, cid)) == "DONE"
    # A different candidate whose identity already has an application (created
    # by another path) is never promoted into a second one.
    other = discover(conn, [portal_job("r2", company="Second Co")])["candidate_ids"][0]
    cand = get_discovery_candidate(conn, other)
    create_job_from_source_record(conn, company=cand["company"], title=cand["title"],
                                  source_record=cand["canonical_source_record"])
    add_fit(conn, other)
    row2, _ = ac.screen_candidate(conn, ctx=_ctx(conn, world, other), now=NOW)
    conn.commit()  # screen_candidate never commits; the scheduler owns the transaction
    assert (row2["outcome"], row2["reason_code"]) == ("NOT_ELIGIBLE", "existing_application")
    assert conn.execute("SELECT COUNT(*) FROM autonomy_candidate_promotions").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["policy", "ceiling", "kill_switch", "pause", "dedupe", "state", "cap"])
def test_promotion_revalidates_every_input(conn, world, change):
    from product.autonomy_contract import Capability
    from webapp.persistence.discovery import get_discovery_candidate, set_discovery_candidate_status
    from webapp.services.autonomy_controls import engage_kill_switch, pause, set_capability
    from webapp.services.pipeline import create_job_from_source_record
    cid, row = _screened(conn, world, f"v-{change}")
    settings = world
    if change == "policy":
        _ask_on_unknown_fit(conn)
    elif change == "ceiling":
        set_capability(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT,
                       capability=Capability.NONE, actor="u", now=NOW)
    elif change == "kill_switch":
        engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    elif change == "pause":
        pause(conn, account_id=ACCOUNT, scope_type="SEARCH_WORKSPACE", scope_id=SW, actor="u", reason="r", now=NOW)
    elif change == "dedupe":
        cand = get_discovery_candidate(conn, cid)
        create_job_from_source_record(conn, company=cand["company"], title=cand["title"],
                                      source_record=cand["canonical_source_record"])
    elif change == "state":
        set_discovery_candidate_status(conn, cid, "dismissed")
    elif change == "cap":
        settings = dataclasses.replace(world, autonomy_max_promotions_per_day=0)
    before = _counts(conn)
    assert _promote(conn, settings, cid, row) is None
    after = _counts(conn)
    for table in ("autonomy_candidate_promotions", "autonomy_enrolments", "limit_reservations", "workspaces"):
        assert after[table] == before[table], (change, table)


def test_user_promote_enrols_wakes_and_does_not_consume_the_cap(conn, world):
    _ask_on_unknown_fit(conn)
    cid = discover(conn, [portal_job("u1")])["candidate_ids"][0]
    add_fit(conn, cid, score=None)
    ac.screen_candidate(conn, ctx=_ctx(conn, world, cid), now=NOW)
    conn.commit()  # screen_candidate never commits; the scheduler owns the transaction
    (exc,) = ap.open_candidate_exceptions(conn, ACCOUNT)
    out = ac.resolve_candidate_question(conn, settings=dataclasses.replace(world, autonomy_max_promotions_per_day=0),
                                        exception_id=exc["id"], resolution="PROMOTE", actor="u", reason=None, now=NOW)
    ws = out["application_workspace_id"]
    enrol = conn.execute("SELECT actor_type, action FROM autonomy_enrolments WHERE application_workspace_id = ?",
                         (ws,)).fetchone()
    assert tuple(enrol) == ("USER", "ENROL")
    assert conn.execute("SELECT next_eligible_at FROM autonomy_queue_items WHERE application_workspace_id = ?",
                        (ws,)).fetchone()[0] is not None
    assert conn.execute("SELECT COUNT(*) FROM limit_reservations WHERE counter_name = 'promotions:day'"
                        ).fetchone()[0] == 0
    assert ap.open_candidate_exceptions(conn, ACCOUNT) == []
    with pytest.raises(ac.CandidatePromotionRefused):
        ac.resolve_candidate_question(conn, settings=world, exception_id=exc["id"], resolution="DISMISS",
                                      actor="u", reason=None, now=NOW)


def test_user_promote_refused_when_a_structural_obstacle_appeared_and_dismiss_works(conn, world):
    from product.autonomy_contract import Capability
    from webapp.services.autonomy_controls import set_capability
    _ask_on_unknown_fit(conn)
    ids = discover(conn, [portal_job("x1"), portal_job("x2", company="Other Co")])["candidate_ids"]
    for cid in ids:
        add_fit(conn, cid, score=None)
        ac.screen_candidate(conn, ctx=_ctx(conn, world, cid), now=NOW)
        conn.commit()  # screen_candidate never commits; the scheduler owns the transaction
    first, second = ap.open_candidate_exceptions(conn, ACCOUNT)
    ac.resolve_candidate_question(conn, settings=world, exception_id=second["id"], resolution="DISMISS", actor="u",
                                  reason="not for me", now=NOW)
    status = conn.execute("SELECT lifecycle_status FROM discovery_candidates WHERE id = ?",
                          (second["candidate_id"],)).fetchone()[0]
    assert status == "dismissed"
    set_capability(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT, capability=Capability.NONE,
                   actor="u", now=NOW)
    before = _counts(conn)
    with pytest.raises(ac.CandidatePromotionRefused):
        ac.resolve_candidate_question(conn, settings=world, exception_id=first["id"], resolution="PROMOTE",
                                      actor="u", reason=None, now=NOW)
    assert _counts(conn) == before and ap.get_candidate_exception(conn, first["id"])["resolution"] is None
