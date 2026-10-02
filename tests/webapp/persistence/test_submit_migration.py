"""6E-A migration 021_human_submit (spec §17): append-only submit evidence,
closed vocabularies, and submission_intents rebuilt only to accept the
HUMAN_AUTHORIZED source -- rows, the live-intent index and FKs preserved."""
from __future__ import annotations

# Legacy-chain assertions are scoped to 001-021 (id < '022'); Bundle 7
# migrations are covered by test_schema_parity.py and their own tests.

import sqlite3
from datetime import datetime, timezone

import pytest

from webapp.persistence.db import connect, init_db
from webapp.persistence.migrations import (
    FILL_MIGRATION_ID, HUMAN_SUBMIT_MIGRATION_ID, SUBMIT_APPEND_ONLY_TABLES, SUBMIT_EVENTS, SUBMIT_OBSERVATION_PHASES,
)
from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID

NOW = "2026-09-29T10:00:00.000000+00:00"


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "db.sqlite3")
    c = connect(tmp_path / "db.sqlite3")
    yield c
    c.close()


def _ids(c):
    return [r[0] for r in c.execute("SELECT id FROM schema_migrations WHERE id < '022' ORDER BY rowid")]


def test_fresh_database_has_21_migrations_ending_020_021_and_rerun_is_a_noop(tmp_path, conn):
    ids = _ids(conn)
    assert len(ids) == 21 and ids[-2:] == [FILL_MIGRATION_ID, HUMAN_SUBMIT_MIGRATION_ID]
    schema = sorted(tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master"))
    init_db(tmp_path / "db.sqlite3")
    assert _ids(conn) == ids
    assert sorted(tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master")) == schema


def test_the_closed_vocabularies_are_the_spec_lists():
    assert SUBMIT_OBSERVATION_PHASES == ("REVIEW", "PRE_SUBMIT", "CHALLENGE_CLEARED", "POST_SUBMIT")
    assert SUBMIT_EVENTS == (
        "PRE_CLICK_REFUSED", "CHALLENGE_BEFORE_SUBMIT", "CANCELLED_BEFORE_DISPATCH", "EGRESS_INSTALLED",
        "EGRESS_VERIFY_FAILED", "CLICK_PERFORMED", "SUBMIT_CONTROL_MISSING", "CHALLENGE_DETECTED",
        "CHALLENGE_CLEARED", "CONTENT_CHANGED_DURING_ATTEMPT", "SIGNAL_OBSERVED", "TOTAL_RESTORED",
        "TOTAL_RESTORE_FAILED", "EXECUTOR_RESTARTED", "RESULT_REPORTED")
    assert SUBMIT_APPEND_ONLY_TABLES == ("human_submit_authorizations", "submit_reobservation_requests",
                                         "submit_observations", "submit_events", "submission_results")


@pytest.mark.parametrize("table", SUBMIT_APPEND_ONLY_TABLES)
def test_new_tables_reject_update_and_delete(conn, table):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                                        (table,))}
    assert {f"{table}_append_only_update", f"{table}_append_only_delete"} <= names


def _ws(conn):
    from webapp.persistence.workspaces import create_workspace
    row = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
    return row[0] if row else create_workspace(conn, company="Acme", title="Engineer", account_id=DEFAULT_ACCOUNT_ID)["id"]


def _intent(conn, id_, key, *, state="CLAIMED", source="HUMAN_AUTHORIZED"):
    conn.execute("INSERT INTO submission_intents (id, account_id, job_identity_key, application_workspace_id, state, "
                 "source, created_at, updated_at) VALUES (?, 'account_local', ?, ?, ?, ?, ?, ?)",
                 (id_, key, _ws(conn), state, source, NOW, NOW))


def test_intents_accept_human_authorized_and_still_reject_unknown_sources(conn):
    _intent(conn, "i1", "k1")
    with pytest.raises(sqlite3.IntegrityError):
        _intent(conn, "i2", "k2", source="ROBOT")


def test_the_live_intent_index_still_allows_one_live_intent_per_identity(conn):
    _intent(conn, "i1", "k1")
    with pytest.raises(sqlite3.IntegrityError):
        _intent(conn, "i2", "k1", source="HUMAN_APPLIED", state="CONFIRMED")
    _intent(conn, "i3", "k1", state="RELEASED")  # a released one does not count


def test_submit_event_and_phase_checks_reject_unknown_values(conn):
    conn.execute("PRAGMA foreign_keys = OFF")  # isolate the CHECKs from the FKs
    conn.execute("INSERT INTO submit_events (id, authorization_id, event, detail_json, created_at) "
                 "VALUES ('ok', 'a', 'CLICK_PERFORMED', '{}', ?)", (NOW,))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("INSERT INTO submit_events (id, authorization_id, event, detail_json, created_at) "
                     "VALUES ('e', 'a', 'SUBMITTED_BY_MAGIC', '{}', ?)", (NOW,))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("INSERT INTO submit_observations (id, account_id, application_workspace_id, fill_run_id, phase, "
                     "structure_fingerprint, observation_fingerprint, observation_json, created_at) "
                     "VALUES ('o', 'acc', 'ws', 'fr', 'POST_FILL', 's', 'o', '{}', ?)", (NOW,))


def test_foreign_keys_and_integrity_are_clean(conn):
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_datetime_constant_is_aware():
    assert datetime.fromisoformat(NOW).tzinfo == timezone.utc
