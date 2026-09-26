from __future__ import annotations

from datetime import timedelta

import pytest

from product.autonomy_contract import Capability
from product.standing_policy import StandingPolicyError, default_policy_document, policy_hash
from webapp.persistence.autonomy_authority import (
    current_capability, current_policy, end_run, get_run, is_paused, kill_switch_state,
    record_authorization, record_control_event, record_kill_switch, resolve_authority,
    save_policy_version, start_run,
)
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn  # noqa: F401


def test_no_records_means_none(conn):
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id="sw_1") == (Capability.NONE, Capability.NONE)
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id=None) == (Capability.NONE, Capability.NONE)


def test_latest_by_seq_even_with_identical_timestamps(conn):
    for cap in (Capability.SUBMIT, Capability.PREPARE):
        record_authorization(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT,
                             capability=cap, set_by="user", now=NOW)
    assert current_capability(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT) == Capability.PREPARE


def test_workspace_ceiling_falls_back_to_explicit_default(conn):
    record_authorization(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT, capability=Capability.SUBMIT, set_by="u", now=NOW)
    record_authorization(conn, account_id=ACCOUNT, scope_type="DEFAULT_WORKSPACE_CEILING", scope_id=ACCOUNT, capability=Capability.PREPARE, set_by="u", now=NOW)
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id="sw_1") == (Capability.SUBMIT, Capability.PREPARE)
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id=None) == (Capability.SUBMIT, Capability.PREPARE)
    record_authorization(conn, account_id=ACCOUNT, scope_type="WORKSPACE_CEILING", scope_id="sw_1", capability=Capability.SUBMIT, set_by="u", now=NOW)
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id="sw_1") == (Capability.SUBMIT, Capability.SUBMIT)


def test_kill_switch_release_does_not_resume_without_resume_all(conn):
    assert kill_switch_state(conn, ACCOUNT)["halted"] is False
    engaged = record_kill_switch(conn, account_id=ACCOUNT, engaged=True, reason="r", actor="u", now=NOW)
    record_kill_switch(conn, account_id=ACCOUNT, engaged=False, reason="r", actor="u", now=NOW)
    state = kill_switch_state(conn, ACCOUNT)
    assert state["engaged"] is False and state["halted"] is True
    record_control_event(conn, account_id=ACCOUNT, scope_type="ACCOUNT", scope_id=ACCOUNT, action="RESUME_ALL",
                         actor="u", reason="ok", now=NOW, kill_switch_seq_acknowledged=engaged["seq"])
    assert kill_switch_state(conn, ACCOUNT)["halted"] is False


def test_pause_resume_by_seq_and_resume_all(conn):
    kw = dict(account_id=ACCOUNT, scope_type="APPLICATION", scope_id="ws_1", actor="u", reason="r", now=NOW)
    assert not is_paused(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id="ws_1")
    record_control_event(conn, action="PAUSE", **kw)
    assert is_paused(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id="ws_1")
    record_control_event(conn, action="RESUME", **kw)
    assert not is_paused(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id="ws_1")
    record_control_event(conn, action="PAUSE", **kw)
    record_control_event(conn, account_id=ACCOUNT, scope_type="ACCOUNT", scope_id=ACCOUNT, action="RESUME_ALL",
                         actor="u", reason="r", now=NOW)
    assert not is_paused(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id="ws_1")


def test_policy_versions(conn):
    assert current_policy(conn, ACCOUNT) is None
    doc = default_policy_document("Europe/London")
    saved = save_policy_version(conn, account_id=ACCOUNT, doc=doc, created_by="u", now=NOW)
    current = current_policy(conn, ACCOUNT)
    assert current["doc"] == doc and current["policy_hash"] == policy_hash(doc) == saved["policy_hash"]
    with pytest.raises(StandingPolicyError):
        save_policy_version(conn, account_id=ACCOUNT, doc={**doc, "timezone": "Nowhere/Void"}, created_by="u", now=NOW)
    assert current_policy(conn, ACCOUNT)["id"] == saved["id"]


def test_runs(conn):
    run = start_run(conn, account_id=ACCOUNT, started_by="SCHEDULER", now=NOW)
    assert get_run(conn, run["run_id"])["ended_at"] is None
    end_run(conn, run_id=run["run_id"], end_reason="done", now=NOW + timedelta(minutes=5))
    assert get_run(conn, run["run_id"])["ended_at"] is not None
