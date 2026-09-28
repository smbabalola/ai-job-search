"""6D-B run lifecycle (spec §10.4, §11.1, §11.4): concurrency keys, the
INITIAL routing, revalidation mismatch routing, quarantine events, leases at
their boundary and the reduce-only reaper."""
from __future__ import annotations

import inspect
import sqlite3
from datetime import timedelta

import pytest

from product.fill_constants import FILL_TIMING_VERSION, RUN_LEASE_TTL
from product.fill_hash import fill_value_hash
from tests.product.fill_observation_fixtures import element
from tests.webapp.services.fill_fixtures import (  # noqa: F401
    NOW, V2_ACCOUNT, fill_world, grant_world, observation_doc, v2_chain,
)
from webapp.persistence import fill as f
from webapp.persistence import review_approval as ra
from webapp.services import fill_runs as fr
from webapp.services.autonomy_controls import run_immediate

RULESET = "sha256:" + "5" * 64
MS = timedelta(milliseconds=1)


def start(w, *, tab=1, browser="b1", executor="ex_1", ws=None, now=NOW):
    return fr.start_run(w.conn, settings=w.settings, account_id=V2_ACCOUNT, handoff_session_id="hs_1",
                        application_workspace_id=ws or w.ws, executor_instance_id=executor,
                        browser_session_id=browser, execution_tab_id=tab, now=now)


def observe(w, run, phase, doc=None, now=NOW):
    return fr.record_observation(w.conn, settings=w.settings, run_id=run["id"], phase=phase, action_index=None,
                                 observation=doc or observation_doc(), now=now)


def quarantine(w, run, phase="TOTAL_VERIFIED"):
    return fr.record_quarantine(w.conn, run_id=run["id"], phase=phase, ruleset_hash=RULESET, now=NOW)


def to_active(w):
    run = start(w)
    assert observe(w, run, "INITIAL")["state"] == "REVALIDATING"
    assert observe(w, run, "REVALIDATION")["matched"] is True
    quarantine(w, run, "PRELOAD_INSTALLED")
    quarantine(w, run, "RELOADED")
    assert quarantine(w, run)["state"] == "QUARANTINE_ACTIVE"
    return run


def events(w, run):
    return [(e["event"], e["reason"]) for e in f.run_events(w.conn, run["id"])]


def count(w, table):
    return w.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_start_records_identity_lease_and_observing(grant_world):
    run = start(grant_world)
    assert run["timing_version"] == FILL_TIMING_VERSION and run["execution_tab_id"] == 1
    assert events(grant_world, run) == [("OBSERVING", None)]
    assert f.get_lease(grant_world.conn, run["id"])["expires_at"].startswith("2026")
    assert f.active_run_for(grant_world.conn, grant_world.ws) == run["id"]


def test_one_non_terminal_run_per_application_and_per_context(grant_world):
    from webapp.persistence.workspaces import create_workspace
    start(grant_world)
    runs = count(grant_world, "fill_runs")
    with pytest.raises(fr.FillRefused) as refused:
        start(grant_world, tab=2)
    assert refused.value.reason == "run_active"
    other = create_workspace(grant_world.conn, company="Other", title="Role", account_id=V2_ACCOUNT)["id"]
    with pytest.raises(fr.FillRefused) as refused:
        start(grant_world, ws=other)  # the same executor/browser/tab
    assert refused.value.reason == "context_in_use"
    assert count(grant_world, "fill_runs") == runs  # refusals write nothing
    start(grant_world, ws=other, tab=2)  # another tab is another context


def test_an_unconfirmed_plan_ends_the_run_for_review(fill_world):
    run = start(fill_world)
    out = observe(fill_world, run, "INITIAL")
    assert out["state"] == "PLAN_NEEDS_REVIEW"
    assert events(fill_world, run) == [("OBSERVING", None), ("PLAN_PROPOSED", None), ("PLAN_NEEDS_REVIEW", None)]
    assert f.active_run_for(fill_world.conn, fill_world.ws) is None and f.get_lease(fill_world.conn, run["id"]) is None
    assert f.get_result(fill_world.conn, run["id"])["result"]["terminal_state"] == "PLAN_NEEDS_REVIEW"


def test_initial_new_content_opens_deltas_and_ends_for_review(grant_world):
    hear = element("gh:hear", label="How did you hear about us?", question="How did you hear about us?",
                   name="hear", id="hear", required=True)
    run = start(grant_world)
    assert observe(grant_world, run, "INITIAL", observation_doc(hear))["state"] == "PLAN_NEEDS_REVIEW"
    [delta] = ra.open_deltas(grant_world.conn, grant_world.ws)
    assert delta["source"] == f"FILL_RUN:{run['id']}"


def test_revalidation_with_changed_value_states_is_observation_mismatch(grant_world):
    run = start(grant_world)
    observe(grant_world, run, "INITIAL")
    doc = observation_doc()
    doc["elements"][1]["value_state"] = {"state": "NONBLANK", "current_value_hash": fill_value_hash("3 months")}
    out = observe(grant_world, run, "REVALIDATION", doc)
    assert (out["state"], out["reason"]) == ("FILL_STOPPED", "OBSERVATION_MISMATCH")
    assert f.run_state(grant_world.conn, run["id"])["detail"] == {"cause": "VALUE_STATES_DIFFER"}


def test_revalidation_with_a_new_question_opens_the_delta_and_stops(grant_world):
    run = start(grant_world)
    observe(grant_world, run, "INITIAL")
    hear = element("gh:hear", label="How did you hear about us?", question="How did you hear about us?",
                   name="hear", id="hear", required=True)
    out = observe(grant_world, run, "REVALIDATION", observation_doc(hear))
    assert (out["state"], out["reason"]) == ("FILL_STOPPED", "DELTA_OPENED")
    [delta] = ra.open_deltas(grant_world.conn, grant_world.ws)
    assert delta["kind"] == "NEW_QUESTION"
    assert f.run_state(grant_world.conn, run["id"])["detail"]["delta_ids"] == [delta["id"]]


def test_revalidation_with_a_removed_field_is_structure_changed(grant_world):
    run = start(grant_world)
    observe(grant_world, run, "INITIAL")
    doc = observation_doc()
    doc["elements"] = [e for e in doc["elements"] if e["page_field_key"] != "gh:cover"]
    assert observe(grant_world, run, "REVALIDATION", doc)["reason"] == "STRUCTURE_CHANGED"


def test_total_quarantine_needs_a_matching_revalidation_first(grant_world):
    run = start(grant_world)
    observe(grant_world, run, "INITIAL")
    with pytest.raises(fr.FillRefused) as refused:
        quarantine(grant_world, run)
    assert refused.value.reason == "revalidation_required"
    observe(grant_world, run, "REVALIDATION")
    assert quarantine(grant_world, run)["state"] == "QUARANTINE_ACTIVE"
    assert [q["phase"] for q in f.quarantine_events(grant_world.conn, run["id"])] == ["TOTAL_VERIFIED"]


def test_a_lost_quarantine_stops_the_run(grant_world):
    run = to_active(grant_world)
    assert quarantine(grant_world, run, "LOST")["reason"] == "QUARANTINE_LOST"
    assert events(grant_world, run)[-1] == ("FILL_STOPPED", "QUARANTINE_LOST")


def test_observations_out_of_phase_are_refused(grant_world):
    run = start(grant_world)
    with pytest.raises(fr.FillRefused):
        observe(grant_world, run, "REVALIDATION")


def test_lease_boundary(grant_world):
    run = start(grant_world)
    edge = NOW + RUN_LEASE_TTL
    assert fr.reap_expired_leases(grant_world.conn, now=edge - MS) == 0
    assert fr.reap_expired_leases(grant_world.conn, now=edge) == 0  # expiry is strictly after the TTL
    assert fr.heartbeat(grant_world.conn, run_id=run["id"], now=edge - MS)["lease_expired"] is False
    renewed = edge - MS + RUN_LEASE_TTL
    assert fr.reap_expired_leases(grant_world.conn, now=renewed) == 0
    assert fr.reap_expired_leases(grant_world.conn, now=renewed + MS) == 1
    assert events(grant_world, run)[-1] == ("FILL_STOPPED", "EXECUTOR_LOST")


def test_a_heartbeat_after_expiry_never_resumes(grant_world):
    run = start(grant_world)
    out = fr.heartbeat(grant_world.conn, run_id=run["id"], now=NOW + RUN_LEASE_TTL + MS)
    assert out == {"state": "FILL_STOPPED", "lease_expired": True}
    with pytest.raises(fr.FillRefused):
        fr.heartbeat(grant_world.conn, run_id=run["id"], now=NOW + RUN_LEASE_TTL + 2 * MS)


def test_the_reaper_is_reduce_only(grant_world):
    run = to_active(grant_world)
    assert fr.request_fill_grant(grant_world.conn, settings=grant_world.settings, run_id=run["id"], now=NOW).granted
    grant_id = f.get_grant_binding(grant_world.conn, run["id"])["grant_id"]
    before = {t: count(grant_world, t) for t in ("autonomy_grants", "autonomy_decisions", "fill_plan_confirmations",
                                                  "application_approvals", "fill_run_grant_bindings")}
    assert fr.reap_expired_leases(grant_world.conn, now=NOW + RUN_LEASE_TTL + MS) == 1
    assert {t: count(grant_world, t) for t in before} == before  # nothing created
    status = grant_world.conn.execute("SELECT status FROM autonomy_grants WHERE id = ?", (grant_id,)).fetchone()[0]
    assert status == "REVOKED" and events(grant_world, run)[-1] == ("FILL_STOPPED", "EXECUTOR_LOST")


def test_a_filled_run_whose_lease_expires_becomes_context_unverified(grant_world):
    run = to_active(grant_world)
    run_immediate(grant_world.conn, lambda: fr.finish_in_transaction(
        grant_world.conn, run_id=run["id"], event="FILLED_AWAITING_SUBMISSION", now=NOW))
    assert f.get_lease(grant_world.conn, run["id"]) is not None  # FILLED keeps continuity
    assert fr.reap_expired_leases(grant_world.conn, now=NOW + RUN_LEASE_TTL + MS) == 1
    assert events(grant_world, run)[-2:] == [("FILLED_AWAITING_SUBMISSION", None), ("FILLED_CONTEXT_UNVERIFIED", None)]
    assert f.get_result(grant_world.conn, run["id"])["result"]["terminal_state"] == "FILLED_AWAITING_SUBMISSION"


def test_an_expired_run_does_not_block_a_fresh_start(grant_world):
    start(grant_world)
    fresh = start(grant_world, now=NOW + RUN_LEASE_TTL + MS)
    assert f.active_run_for(grant_world.conn, grant_world.ws) == fresh["id"]


def test_the_scheduler_sweeps_leases_reduce_only():
    from webapp.services import autonomy_scheduler
    assert "reap_expired_leases" in inspect.getsource(autonomy_scheduler._sweeps)


def test_no_quarantine_phase_lifts_the_quarantine():
    from product.fill_vocab import QUARANTINE_PHASES
    assert not [p for p in QUARANTINE_PHASES if "RELEASE" in p or "LIFT" in p or "REMOVE" in p]


def test_a_stop_after_the_run_ended_is_refused(grant_world):
    run = to_active(grant_world)
    fr.stop_run(grant_world.conn, run_id=run["id"], reason="EXECUTION_CONTEXT_CLOSED", detail={}, now=NOW)
    with pytest.raises(fr.FillRefused):
        fr.stop_run(grant_world.conn, run_id=run["id"], reason="EXECUTOR_LOST", detail={}, now=NOW)
    with pytest.raises(sqlite3.IntegrityError):  # one result per run
        f.insert_result(grant_world.conn, fill_run_id=run["id"], result={}, result_hash="x", now=NOW)
