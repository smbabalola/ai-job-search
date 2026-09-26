from __future__ import annotations

import pytest

from product.autonomy_contract import Capability
from webapp.config import Settings
from webapp.persistence.autonomy_authority import (
    current_policy, is_paused, kill_switch_state, resolve_authority,
)
from webapp.persistence.autonomy_ledger import get_grant, insert_decision, insert_grant
from webapp.services.autonomy_controls import (
    AutonomyHalted, enable_autonomous_preparation, engage_kill_switch, engage_kill_switch_in_transaction,
    observe_sentinel, pause, release_kill_switch, resume, resume_all, set_capability,
)
from product.autonomy_gate import evaluate_authorization
from tests.product.autonomy_fixtures import make_ctx
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401


def test_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("JOBSEARCH_AUTONOMY_MAX_CAPABILITY", raising=False)
    monkeypatch.delenv("JOBSEARCH_AUTONOMY_SUBMIT_ADAPTERS", raising=False)
    monkeypatch.delenv("JOBSEARCH_AUTONOMY_SHADOW", raising=False)
    s = Settings(db_path=tmp_path / "x" / "db.sqlite3")
    assert s.autonomy_deployment_ceiling() == Capability.NONE
    assert s.autonomy_sentinel_path == tmp_path / "x" / "AUTONOMY_HALT"
    assert s.autonomy_live_submit_daily_cap == 1
    assert s.autonomy_submit_capable_adapters == ()
    assert s.autonomy_shadow_enabled is False
    monkeypatch.setenv("JOBSEARCH_AUTONOMY_MAX_CAPABILITY", "FILL")
    monkeypatch.setenv("JOBSEARCH_AUTONOMY_SUBMIT_ADAPTERS", "greenhouse,lever")
    s = Settings(db_path=tmp_path / "db.sqlite3")
    assert s.autonomy_deployment_ceiling() == Capability.FILL
    assert s.autonomy_submit_capable_adapters == ("greenhouse", "lever")
    monkeypatch.setenv("JOBSEARCH_AUTONOMY_MAX_CAPABILITY", "EVERYTHING")
    assert Settings(db_path=tmp_path / "db.sqlite3").autonomy_deployment_ceiling() == Capability.NONE


def _issued_grant(conn, ws):
    ctx = make_ctx(account_id=ACCOUNT, application_workspace_id=ws)
    d = insert_decision(conn, ctx=ctx, decision=evaluate_authorization(ctx))
    return insert_grant(conn, decision_id=d["id"], account_id=ACCOUNT, application_workspace_id=ws,
                        stage=Capability.FILL, binding={}, issued_at=NOW, expires_at=NOW.replace(year=2027))


def test_engage_revokes_issued_grants_and_is_idempotent(conn):
    ws = make_workspace(conn)
    g = _issued_grant(conn, ws)
    first = engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    assert first == {"engaged": True, "revoked": 1, "recorded": True}
    assert get_grant(conn, g["id"])["status"] == "REVOKED"
    second = engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop again", now=NOW)
    assert second["recorded"] is False and second["revoked"] == 0


def test_release_never_resumes_and_release_when_not_engaged_is_noop(conn):
    assert release_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="x", now=NOW) == {"recorded": False}
    engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    assert release_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="x", now=NOW) == {"recorded": True}
    assert kill_switch_state(conn, ACCOUNT)["halted"] is True


def test_sentinel_engages_and_blocks_resume_all_until_removed(conn, tmp_path):
    sentinel = tmp_path / "AUTONOMY_HALT"
    assert observe_sentinel(conn, account_id=ACCOUNT, sentinel_path=sentinel, now=NOW) is False
    sentinel.write_text("halt")
    assert observe_sentinel(conn, account_id=ACCOUNT, sentinel_path=sentinel, now=NOW) is True
    assert kill_switch_state(conn, ACCOUNT)["engaged"] is True
    with pytest.raises(AutonomyHalted):
        resume_all(conn, account_id=ACCOUNT, actor="u", reason="go", now=NOW, sentinel_path=sentinel)
    assert kill_switch_state(conn, ACCOUNT)["engaged"] is True  # a refused resume_all records nothing
    sentinel.unlink()
    assert kill_switch_state(conn, ACCOUNT)["halted"] is True  # removal alone never resumes
    resume_all(conn, account_id=ACCOUNT, actor="u", reason="go", now=NOW, sentinel_path=sentinel)
    assert kill_switch_state(conn, ACCOUNT)["halted"] is False


def test_pause_and_resume_are_recorded(conn):
    pause(conn, account_id=ACCOUNT, scope_type="SEARCH_WORKSPACE", scope_id="sw_1", actor="u", reason="r", now=NOW)
    assert is_paused(conn, account_id=ACCOUNT, scope_type="SEARCH_WORKSPACE", scope_id="sw_1")
    resume(conn, account_id=ACCOUNT, scope_type="SEARCH_WORKSPACE", scope_id="sw_1", actor="u", reason="r", now=NOW)
    assert not is_paused(conn, account_id=ACCOUNT, scope_type="SEARCH_WORKSPACE", scope_id="sw_1")


def test_enable_preparation_writes_explicit_records_and_never_lowers(conn):
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id="sw_new") == (Capability.PREPARE, Capability.PREPARE)
    assert current_policy(conn, ACCOUNT)["doc"]["timezone"] == "Europe/London"
    set_capability(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT,
                   capability=Capability.SUBMIT, actor="u", now=NOW)
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id=None)[0] == Capability.SUBMIT


def test_committing_controls_refuse_to_run_inside_a_caller_transaction(conn):
    # They would otherwise commit (or fail on) the caller's half-done work.
    ws = make_workspace(conn)
    conn.execute("UPDATE workspaces SET title = 'x' WHERE id = ?", (ws,))
    assert conn.in_transaction
    with pytest.raises(RuntimeError):
        engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    assert conn.in_transaction  # the caller's transaction is untouched
    conn.rollback()


def test_engage_in_transaction_leaves_commit_to_caller(conn):
    ws = make_workspace(conn)
    g = _issued_grant(conn, ws)
    conn.execute("BEGIN IMMEDIATE")
    result = engage_kill_switch_in_transaction(conn, account_id=ACCOUNT, actor="sentinel", reason="r", now=NOW)
    assert result == {"engaged": True, "revoked": 1, "recorded": True}
    conn.rollback()
    assert kill_switch_state(conn, ACCOUNT)["engaged"] is False
    assert get_grant(conn, g["id"])["status"] == "ISSUED"
