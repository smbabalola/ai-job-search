from __future__ import annotations

import sqlite3

import pytest

from webapp.persistence.db import connect, init_db
from webapp.persistence.migrations import AUTONOMY_APPEND_ONLY_TABLES

EXPECTED = {
    "autonomy_authorizations", "autonomy_kill_switch", "autonomy_control_events", "autonomy_runs",
    "autonomy_run_ends", "standing_policy_versions", "approved_answers", "answer_confirmations",
    "proposed_answers", "rule_acknowledgements", "apply_target_confirmations", "autonomy_decisions",
    "autonomy_grants", "autonomy_grant_events", "limit_reservations", "submission_intents",
    "intent_overrides", "submission_attempts", "submission_attempt_events",
    "dry_run_submission_cases", "dry_run_case_agreements", "autonomy_queue_items",
}

DECISION_COLUMNS = (
    "id", "account_id", "application_workspace_id", "run_id", "mode", "requested_stage", "result",
    "deny_reason", "effective_capability", "grantable", "reasons_json", "require_user_json",
    "completion_blockers_json", "retry_at", "retryable", "inputs_json", "input_fingerprint",
    "decision_fingerprint", "engine_version", "policy_version_hash", "subject_policy_hash",
    "grant_id", "created_at",
)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "t.sqlite3"
    init_db(db)
    c = connect(db)
    yield c
    c.close()


def test_tables_exist_and_migration_is_idempotent(conn, tmp_path):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED <= names
    init_db(tmp_path / "t.sqlite3")  # second run is a no-op
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE id='016_autonomy_contract'").fetchone()[0] == 1


def test_append_only_tables_have_seq_and_reject_update_delete(conn):
    for table in AUTONOMY_APPEND_ONLY_TABLES:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert cols[0] == "seq", table
    conn.execute(
        "INSERT INTO autonomy_kill_switch (id, account_id, engaged, reason, actor, created_at) "
        "VALUES ('k1', 'account_local', 1, 'test', 'me', '2026-09-24T12:00:00.000000+00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE autonomy_kill_switch SET engaged = 0")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM autonomy_kill_switch")


def test_live_intent_unique_index_ignores_released_and_overridden(conn):
    conn.execute(
        "INSERT INTO workspaces (id, kind, company, title, workflow_status, created_at, updated_at, account_id) "
        "VALUES ('ws1', 'job', 'Acme', 'Eng', NULL, 'x', 'x', 'account_local')"
    )
    row = ("account_local", "source:x:1", "ws1", "AUTONOMOUS", "t", "t")
    sql = ("INSERT INTO submission_intents (id, account_id, job_identity_key, application_workspace_id, state, "
           "source, overridden, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
    conn.execute(sql, ("i1", row[0], row[1], row[2], "CONFIRMED", row[3], 0, row[4], row[5]))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("i2", row[0], row[1], row[2], "CLAIMED", row[3], 0, row[4], row[5]))
    conn.execute("UPDATE submission_intents SET overridden = 1 WHERE id = 'i1'")
    conn.execute(sql, ("i3", row[0], row[1], row[2], "CLAIMED", row[3], 0, row[4], row[5]))
    conn.execute(sql, ("i4", row[0], row[1], row[2], "RELEASED", row[3], 0, row[4], row[5]))


def _insert_decision(conn, decision_id, *, mode, requested_stage, deny_reason):
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, kind, company, title, workflow_status, created_at, updated_at, account_id) "
        "VALUES ('ws_dec', 'job', 'Acme', 'Eng', NULL, 'x', 'x', 'account_local')"
    )
    sql = (
        "INSERT INTO autonomy_decisions ("
        "id, account_id, application_workspace_id, run_id, mode, requested_stage, result, "
        "deny_reason, effective_capability, grantable, reasons_json, require_user_json, "
        "completion_blockers_json, retry_at, retryable, inputs_json, input_fingerprint, "
        "decision_fingerprint, engine_version, policy_version_hash, subject_policy_hash, "
        "grant_id, created_at"
        ") VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)"
    )
    conn.execute(
        sql,
        (
            decision_id, "account_local", "ws_dec", mode, requested_stage, "DENY", deny_reason,
            "NONE", 0, "[]", "[]", "[]", 0, "{}", "fp_input", "fp_decision", "v1",
            "2026-09-24T12:00:00.000000+00:00",
        ),
    )


def test_decision_with_null_mode_and_stage_requires_invalid_input_deny_reason(conn):
    # Ruling: mode/requested_stage may be NULL only alongside deny_reason='invalid_input'.
    _insert_decision(conn, "dec_ok", mode=None, requested_stage=None, deny_reason="invalid_input")
    row = conn.execute(
        "SELECT mode, requested_stage, deny_reason FROM autonomy_decisions WHERE id='dec_ok'"
    ).fetchone()
    assert (row["mode"], row["requested_stage"], row["deny_reason"]) == (None, None, "invalid_input")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_decision(conn, "dec_bad", mode=None, requested_stage="FILL", deny_reason="kill_switch_engaged")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_decision(conn, "dec_bad2", mode="LIVE", requested_stage=None, deny_reason="kill_switch_engaged")


def test_decision_new_columns_exist_and_are_not_null(conn):
    cols = {r["name"]: r["notnull"] for r in conn.execute("PRAGMA table_info(autonomy_decisions)")}
    for column in DECISION_COLUMNS:
        assert column in cols, column
    for column in ("completion_blockers_json", "decision_fingerprint", "retryable"):
        assert cols[column] == 1, column
    for column in ("mode", "requested_stage"):
        assert cols[column] == 0, column
