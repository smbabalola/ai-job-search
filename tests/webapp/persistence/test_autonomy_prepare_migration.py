from __future__ import annotations

import sqlite3

import pytest

from webapp.persistence.db import connect, init_db
from webapp.persistence.migrations import AUTONOMY_6C_APPEND_ONLY_TABLES

HISTORY = {
    "autonomy_candidate_screenings", "autonomy_candidate_promotions", "autonomy_candidate_exceptions",
    "autonomy_candidate_exception_resolutions", "autonomy_prepare_steps", "autonomy_review_latches",
    "autonomy_enrolments", "autonomy_retry_requests", "autonomy_notification_events",
}


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "t.sqlite3"
    init_db(db)
    c = connect(db)
    yield c
    c.close()


def _cols(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def test_tables_exist_and_history_tables_are_append_only(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert HISTORY | {"autonomy_candidate_queue"} <= names
    assert set(AUTONOMY_6C_APPEND_ONLY_TABLES) == HISTORY
    for table in HISTORY:
        assert _cols(conn, table)[0] == "seq", table
    triggers = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    for table in HISTORY:
        assert {f"{table}_append_only_update", f"{table}_append_only_delete"} <= triggers, table
    assert not any(t.startswith("autonomy_candidate_queue_append_only") for t in triggers)
    conn.execute("INSERT INTO autonomy_retry_requests (id, account_id, subject_type, subject_id, step_kind, "
                 "input_fingerprint, actor, created_at) VALUES ('rr1', 'account_local', 'APPLICATION', 'w', 'FIT', "
                 "'fp', 'u', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE autonomy_retry_requests SET actor = 'v'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM autonomy_retry_requests")


def test_additive_columns(conn):
    assert "lease_generation" in _cols(conn, "autonomy_queue_items")
    assert "lease_generation" in _cols(conn, "autonomy_candidate_queue")
    assert {"decision_provenance", "system_basis_json"} <= set(_cols(conn, "review_decisions"))
    assert {"subject_type", "subject_id", "settled_amount", "settlement_ref"} <= set(_cols(conn, "limit_reservations"))
    assert {"retry_request_id", "authorization_decision_id", "input_fingerprint"} <= set(_cols(conn, "autonomy_prepare_steps"))


def test_review_provenance_defaults_to_user_and_is_checked(conn):
    from tests.webapp.persistence.autonomy_db import make_workspace
    from webapp.persistence.artifacts import save_artifact
    ws = make_workspace(conn)
    art = save_artifact(conn, workspace_id=ws, artifact_type="job_fit_result", payload={"x": 1})
    insert = ("INSERT INTO review_decisions (id, workspace_id, review_item_type, source_artifact_id, "
              "domain_item_id, disposition, note, created_at{extra}) VALUES (?, ?, 't', ?, NULL, "
              "'acknowledged_and_proceed', NULL, 'x'{marks})")
    conn.execute(insert.format(extra="", marks=""), ("r0", ws, art["id"]))
    assert conn.execute("SELECT decision_provenance FROM review_decisions WHERE id='r0'").fetchone()[0] == "USER"
    with pytest.raises(sqlite3.IntegrityError):  # valid FKs; only the CHECK can fail
        conn.execute(insert.format(extra=", decision_provenance", marks=", 'ROBOT'"), ("r1", ws, art["id"]))


def test_settlement_ref_is_unique_only_when_set(conn):
    base = ("INSERT INTO limit_reservations (id, account_id, counter_name, window_key, amount, status, created_at, "
            "updated_at, settlement_ref) VALUES (?, 'account_local', 'c', 'w', '1', 'RESERVED', 'x', 'x', ?)")
    conn.execute(base, ("r1", None))
    conn.execute(base, ("r2", None))
    conn.execute(base, ("r3", "s1"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(base, ("r4", "s1"))


def test_subject_columns_are_both_or_neither_on_insert_and_update(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO limit_reservations (id, account_id, counter_name, window_key, amount, status, "
                     "created_at, updated_at, subject_type) VALUES ('r9', 'account_local', 'c', 'w', '1', "
                     "'RESERVED', 'x', 'x', 'CANDIDATE')")
    conn.execute("INSERT INTO limit_reservations (id, account_id, counter_name, window_key, amount, status, "
                 "created_at, updated_at, subject_type, subject_id) VALUES ('r8', 'account_local', 'c', 'w', '1', "
                 "'RESERVED', 'x', 'x', 'CANDIDATE', 'cand_1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE limit_reservations SET subject_id = NULL WHERE id = 'r8'")


def test_system_basis_is_required_exactly_for_system_decisions(conn):
    from tests.webapp.persistence.autonomy_db import make_workspace
    from webapp.persistence.artifacts import save_artifact
    ws = make_workspace(conn)
    art = save_artifact(conn, workspace_id=ws, artifact_type="job_fit_result", payload={"x": 1})
    insert = ("INSERT INTO review_decisions (id, workspace_id, review_item_type, source_artifact_id, domain_item_id, "
              "disposition, note, created_at, decision_provenance, system_basis_json) "
              "VALUES (?, ?, 't', ?, NULL, 'acknowledged_and_proceed', NULL, 'x', ?, ?)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("s1", ws, art["id"], "SYSTEM_AUTO_CONFIRMED", None))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("s2", ws, art["id"], "USER", '{"reason": "x"}'))
    conn.execute(insert, ("s3", ws, art["id"], "SYSTEM_AUTO_CONFIRMED", '{"reason": "x"}'))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE review_decisions SET system_basis_json = NULL WHERE id = 's3'")


def test_rerun_is_a_noop(conn, tmp_path):
    init_db(tmp_path / "t.sqlite3")
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE id='018_autonomy_prepare'").fetchone()[0] == 1
