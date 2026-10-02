"""6D-B persistence (webapp/persistence/fill.py): append-only writes with no
commit (callers own transactions), current-state reads by seq, the run
concurrency keys, leases, and the no-cleartext guard (spec I9)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from webapp.persistence import fill as f
from webapp.persistence.db import connect, init_db
from webapp.persistence.workspaces import create_workspace
from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
ACCOUNT = "account_local"


@pytest.fixture
def world(tmp_path):
    init_db(tmp_path / "db.sqlite3")
    conn = connect(tmp_path / "db.sqlite3")
    ws = create_workspace(conn, company="Acme", title="Engineer", account_id=DEFAULT_ACCOUNT_ID)["id"]
    conn.execute("INSERT INTO application_approvals (id, account_id, application_workspace_id, scope, binding_json, "
                 "binding_hash, actor, created_at) VALUES ('apr_1', ?, ?, 'FILL', '{}', 'sha256:b', 'u', 't')",
                 (ACCOUNT, ws))
    conn.commit()
    yield conn, ws
    conn.close()


def _run(conn, ws, tab=7):
    return f.insert_run(conn, account_id=ACCOUNT, application_workspace_id=ws, handoff_session_id="hs",
                        executor_instance_id="exe", browser_session_id="bs", execution_tab_id=tab,
                        timing_version="fill-timing.v1", now=NOW)


def _observation(conn, ws, run_id=None, phase="INITIAL", payload=None):
    return f.insert_observation(conn, account_id=ACCOUNT, application_workspace_id=ws, fill_run_id=run_id,
                                phase=phase, action_index=None, structure_fingerprint="sha256:s",
                                observation_fingerprint="sha256:o", observation=payload or {"elements": []}, now=NOW)


def test_functions_never_commit(world):
    conn, ws = world
    _run(conn, ws)
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM fill_runs").fetchone()[0] == 0


def test_run_events_derive_state_by_seq(world):
    conn, ws = world
    run = _run(conn, ws)
    f.append_run_event(conn, fill_run_id=run["id"], event="OBSERVING", now=NOW)
    f.append_run_event(conn, fill_run_id=run["id"], event="FILL_STOPPED", reason="DELTA_OPENED",
                       detail={"delta_kind": "NEW_QUESTION"}, now=NOW)
    state = f.run_state(conn, run["id"])
    assert (state["event"], state["reason"], state["detail"]) == ("FILL_STOPPED", "DELTA_OPENED",
                                                                   {"delta_kind": "NEW_QUESTION"})
    assert [e["event"] for e in f.run_events(conn, run["id"])] == ["OBSERVING", "FILL_STOPPED"]


def test_plans_are_stored_once_per_hash_and_confirmations_bind_the_exact_tuple(world):
    conn, ws = world
    obs = _observation(conn, ws)
    plan = {"schema_version": "fill-plan.v1", "actions": []}
    first = f.insert_plan(conn, account_id=ACCOUNT, application_workspace_id=ws, plan=plan, plan_hash="sha256:p1",
                          approval_id="apr_1", approval_binding_hash="sha256:b", observation_id=obs["id"], now=NOW)
    again = f.insert_plan(conn, account_id=ACCOUNT, application_workspace_id=ws, plan=plan, plan_hash="sha256:p1",
                          approval_id="apr_1", approval_binding_hash="sha256:b", observation_id=obs["id"], now=NOW)
    assert first["id"] == again["id"] and f.get_plan_by_hash(conn, "sha256:p1")["plan"] == plan
    assert not f.plan_confirmed(conn, "sha256:p1", "apr_1", "sha256:b")
    f.insert_plan_confirmation(conn, account_id=ACCOUNT, application_workspace_id=ws, plan_hash="sha256:p1",
                               approval_id="apr_1", approval_binding_hash="sha256:b", actor="u", now=NOW)
    assert f.plan_confirmed(conn, "sha256:p1", "apr_1", "sha256:b")
    assert not f.plan_confirmed(conn, "sha256:p1", "apr_1", "sha256:OTHER")  # a different binding is not confirmed


def test_latest_mapping_choice_per_field_wins(world):
    conn, ws = world
    obs = _observation(conn, ws)
    common = dict(account_id=ACCOUNT, application_workspace_id=ws, observation_id=obs["id"], field_fingerprint="sha256:f",
                  actor="u", now=NOW)
    f.insert_mapping_choice(conn, page_field_key="q1", answer_key="subject:a", choice="MAP", **common)
    f.insert_mapping_choice(conn, page_field_key="q1", answer_key=None, choice="NEW_QUESTION", **common)
    f.insert_mapping_choice(conn, page_field_key="q2", answer_key="subject:b", choice="MAP", **common)
    choices = f.current_mapping_choices(conn, obs["id"])
    assert {k: (c["choice"], c["answer_key"]) for k, c in choices.items()} == {
        "q1": ("NEW_QUESTION", None), "q2": ("MAP", "subject:b")}


def test_action_events_allow_one_envelope_and_one_outcome(world):
    conn, ws = world
    run = _run(conn, ws)
    f.append_action_event(conn, fill_run_id=run["id"], action_index=0, event="WRITE_INTENT", now=NOW)
    f.append_action_event(conn, fill_run_id=run["id"], action_index=0, event="ENVELOPE_ISSUED", envelope_id="env_1",
                          now=NOW)
    assert f.issued_envelope(conn, run["id"], 0)["envelope_id"] == "env_1"
    with pytest.raises(sqlite3.IntegrityError):
        f.append_action_event(conn, fill_run_id=run["id"], action_index=0, event="ENVELOPE_ISSUED",
                              envelope_id="env_2", now=NOW)
    f.append_action_event(conn, fill_run_id=run["id"], action_index=0, event="OUTCOME", outcome="WRITTEN_VERIFIED",
                          readback_hash="sha256:r", now=NOW)
    assert f.action_outcome(conn, run["id"], 0)["outcome"] == "WRITTEN_VERIFIED"
    with pytest.raises(sqlite3.IntegrityError):
        f.append_action_event(conn, fill_run_id=run["id"], action_index=0, event="OUTCOME",
                              outcome="READBACK_MISMATCH", now=NOW)
    assert f.action_outcome(conn, run["id"], 1) is None


def test_active_run_claim_and_release(world):
    conn, ws = world
    run = _run(conn, ws)
    f.claim_active_run(conn, application_workspace_id=ws, fill_run_id=run["id"], context_key="exe|bs|7")
    assert f.active_run_for(conn, ws) == run["id"]
    other = _run(conn, ws, tab=8)
    with pytest.raises(sqlite3.IntegrityError):
        f.claim_active_run(conn, application_workspace_id=ws, fill_run_id=other["id"], context_key="exe|bs|8")
    f.release_active_run(conn, run["id"])
    assert f.active_run_for(conn, ws) is None
    f.claim_active_run(conn, application_workspace_id=ws, fill_run_id=other["id"], context_key="exe|bs|8")


def test_leases_expire_strictly_after_their_time(world):
    conn, ws = world
    run = _run(conn, ws)
    f.touch_lease(conn, fill_run_id=run["id"], expires_at=NOW + timedelta(seconds=45), now=NOW)
    assert f.expired_leases(conn, NOW + timedelta(seconds=45)) == []
    assert f.expired_leases(conn, NOW + timedelta(seconds=45, microseconds=1)) == [run["id"]]
    f.touch_lease(conn, fill_run_id=run["id"], expires_at=NOW + timedelta(seconds=90), now=NOW)  # upsert
    assert f.expired_leases(conn, NOW + timedelta(seconds=46)) == []


@pytest.mark.parametrize("payload", [
    {"value": "1 month"},
    {"elements": [{"page_field_key": "q", "rendered_value": "1 month"}]},
    {"rows": [{"display_value": "London"}]},
    {"nested": {"deep": [{"cleartext": "x"}]}},
])
def test_cleartext_bearing_keys_are_refused_at_rest(world, payload):
    conn, ws = world
    with pytest.raises(f.CleartextAtRestError):
        _observation(conn, ws, payload=payload)
    run = _run(conn, ws)
    with pytest.raises(f.CleartextAtRestError):
        f.append_run_event(conn, fill_run_id=run["id"], event="FILL_STOPPED", reason="READBACK_MISMATCH",
                           detail=payload, now=NOW)


def test_hash_named_keys_are_allowed(world):
    conn, ws = world
    _observation(conn, ws, payload={"elements": [{"value_state": {"state": "NONBLANK",
                                                                  "current_value_hash": "sha256:x"}}]})
