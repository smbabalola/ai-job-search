"""6D-B migration 020_fill (spec §19): append-only evidence, closed
vocabularies, run concurrency keys, and 6D-A left byte-identical."""
from __future__ import annotations

# Legacy-chain assertions are scoped to 001-021 (id < '022'); Bundle 7
# migrations are covered by test_schema_parity.py and their own tests.

import sqlite3

import pytest

from product import fill_vocab as v
from webapp.persistence.db import connect, init_db
from webapp.persistence.migrations import FILL_APPEND_ONLY_TABLES, FILL_MIGRATION_ID, REVIEW_APPROVAL_MIGRATION_ID

SIXDA_TABLES = ("application_approvals", "application_review_events", "review_deltas",
                "application_field_dispositions", "approved_answers")


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "db.sqlite3")
    c = connect(tmp_path / "db.sqlite3")
    yield c
    c.close()


def _ids(c):
    return [r[0] for r in c.execute("SELECT id FROM schema_migrations WHERE id < '022' ORDER BY rowid")]


def _sixda_schema(c):
    marks = ",".join("?" for _ in SIXDA_TABLES)
    return sorted(tuple(r) for r in c.execute(
        f"SELECT type, name, tbl_name, sql FROM sqlite_master WHERE tbl_name IN ({marks})", SIXDA_TABLES))


def test_fresh_database_has_twenty_migrations_ending_019_020_and_rerun_is_a_noop(tmp_path, conn):
    ids = _ids(conn)
    assert len(ids) == 21 and ids[-3:-1] == [REVIEW_APPROVAL_MIGRATION_ID, FILL_MIGRATION_ID]  # 021 (6E-A) follows
    init_db(tmp_path / "db.sqlite3")
    assert _ids(conn) == ids


def test_sixda_tables_triggers_and_indexes_are_byte_identical_with_and_without_020(tmp_path, monkeypatch):
    from webapp.persistence import migrations
    without = tmp_path / "without.sqlite3"
    monkeypatch.setattr(migrations, "_migrate_fill", lambda c: None)
    init_db(without)
    monkeypatch.undo()
    with_020 = tmp_path / "with.sqlite3"
    init_db(with_020)
    a, b = connect(without), connect(with_020)
    try:
        assert _sixda_schema(a) == _sixda_schema(b)
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("table", FILL_APPEND_ONLY_TABLES)
def test_append_only_tables_reject_update_and_delete(conn, table):
    # Row-level BEFORE triggers only fire on existing rows: insert one first.
    from tests.webapp.persistence.fill_fixtures import insert_one_row
    insert_one_row(conn, table)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute(f"UPDATE {table} SET created_at = created_at")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute(f"DELETE FROM {table}")


@pytest.mark.parametrize("table,column,bad", [
    ("fill_observations", "phase", "SOMETIME"),
    ("fill_run_events", "event", "RUNNING_FREE"),
    ("fill_run_events", "reason", "BECAUSE"),
    ("fill_action_events", "event", "CLICK"),
    ("fill_action_events", "outcome", "SUBMITTED"),
    ("fill_quarantine_events", "phase", "RELEASED"),
    ("fill_detection_events", "kind", "SUBMITTED_OK"),
    ("fill_plan_mapping_choices", "choice", "GUESS"),
    ("delta_classification_proposals", "basis", "HUNCH"),
])
def test_closed_vocabularies_reject_unknown_values(conn, table, column, bad):
    from tests.webapp.persistence.fill_fixtures import insert_one_row
    extra = {"event": "OUTCOME"} if (table, column) == ("fill_action_events", "outcome") else {}
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_one_row(conn, table, **{column: bad}, **extra)
    good = {"fill_action_events": {"event": "OUTCOME", "outcome": "WRITTEN_VERIFIED"}}.get(table, {})
    if column == "outcome":
        insert_one_row(conn, table, **good)  # the same row with an in-vocabulary value is accepted


def test_vocabularies_have_no_release_or_submit_values():
    everything = {*v.RUN_EVENTS, *v.STOP_REASONS, *v.ACTION_EVENTS, *v.ACTION_OUTCOMES, *v.QUARANTINE_PHASES,
                  *v.DETECTION_KINDS}
    assert not {x for x in everything if "RELEASE" in x or x in ("SUBMITTED", "SUBMIT")}


def test_at_most_one_terminal_outcome_per_action(conn):
    from tests.webapp.persistence.fill_fixtures import insert_one_row
    insert_one_row(conn, "fill_action_events", event="OUTCOME", outcome="WRITTEN_VERIFIED", action_index=3)
    with pytest.raises(sqlite3.IntegrityError):
        insert_one_row(conn, "fill_action_events", event="OUTCOME", outcome="READBACK_MISMATCH", action_index=3)
    insert_one_row(conn, "fill_action_events", event="WRITE_INTENT", outcome=None, action_index=3)  # non-terminal fine


def test_one_active_run_per_application_and_per_execution_context(conn):
    conn.execute("INSERT INTO active_fill_runs (application_workspace_id, fill_run_id, context_key) "
                 "VALUES ('ws_a', 'run_1', 'exe|bs|7')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO active_fill_runs (application_workspace_id, fill_run_id, context_key) "
                     "VALUES ('ws_a', 'run_2', 'exe|bs|8')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO active_fill_runs (application_workspace_id, fill_run_id, context_key) "
                     "VALUES ('ws_b', 'run_3', 'exe|bs|7')")


def test_foreign_keys_and_integrity_are_clean(conn):
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
