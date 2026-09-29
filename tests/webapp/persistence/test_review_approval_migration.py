# tests/webapp/persistence/test_review_approval_migration.py
from __future__ import annotations

import sqlite3

import pytest

from webapp.persistence.db import connect, init_db
from webapp.persistence.migrations import REVIEW_APPROVAL_APPEND_ONLY_TABLES, REVIEW_APPROVAL_MIGRATION_ID
from webapp.persistence.workspaces import create_workspace


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "db.sqlite3")
    c = connect(tmp_path / "db.sqlite3")
    yield c
    c.close()


def test_migration_is_recorded_and_rerun_is_a_noop(tmp_path, conn):
    ids = [r[0] for r in conn.execute("SELECT id FROM schema_migrations ORDER BY rowid")]
    # 019 is applied, immediately followed by 6D-B's 020_fill and 6E-A's 021.
    assert ids[ids.index(REVIEW_APPROVAL_MIGRATION_ID) + 1:] == ["020_fill", "021_human_submit"]
    init_db(tmp_path / "db.sqlite3")
    assert [r[0] for r in conn.execute("SELECT id FROM schema_migrations ORDER BY rowid")] == ids


def _approval(conn, ws, scope="FILL"):
    conn.execute("INSERT INTO application_approvals (id, account_id, application_workspace_id, scope, binding_json, "
                 "binding_hash, actor, created_at) VALUES ('apr_1', 'account_local', ?, ?, '{}', 'sha256:x', 'u', 't')",
                 (ws, scope))


def test_scope_can_only_be_fill(conn):
    ws = create_workspace(conn, company="A", title="B")["id"]
    with pytest.raises(sqlite3.IntegrityError):
        _approval(conn, ws, scope="SUBMIT")
    _approval(conn, ws)


@pytest.mark.parametrize("table", REVIEW_APPROVAL_APPEND_ONLY_TABLES)
def test_history_tables_are_append_only(conn, table):
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                                        (table,))]
    for action in ("update", "delete"):
        assert f"{table}_append_only_{action}" in names


def test_disposition_and_event_vocabularies_are_closed(conn):
    ws = create_workspace(conn, company="A", title="B")["id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO application_field_dispositions (id, account_id, application_workspace_id, answer_key, "
                     "disposition, actor, created_at) VALUES ('d', 'account_local', ?, 'k', 'MAYBE', 'u', 't')", (ws,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO application_review_events (id, account_id, application_workspace_id, event, actor, "
                     "created_at) VALUES ('e', 'account_local', ?, 'REVIEW_OPENED', 'u', 't')", (ws,))
    conn.execute("INSERT INTO application_review_events (id, account_id, application_workspace_id, event, actor, "
                 "created_at) VALUES ('e2', 'account_local', ?, 'DOCUMENT_EDITED', 'u', 't')", (ws,))  # reserved for D3


def test_approved_answers_accept_application_reach_and_keep_their_guards(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'approved_answers'")}
    assert {"approved_answers_append_only_update", "approved_answers_append_only_delete"} <= names
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'approved_answers'").fetchone()[0]
    assert "'APPLICATION'" in sql and "'EMPLOYER'" in sql
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_rebuild_preserves_existing_answers(tmp_path):
    # Build a 018-level DB with an approved answer and a confirmation, then upgrade.
    from tests.webapp.persistence.review_migration_fixtures import pre_019_db_with_answers
    db, before = pre_019_db_with_answers(tmp_path)
    init_db(db)
    c = connect(db)
    after = [tuple(r) for r in c.execute("SELECT * FROM approved_answers ORDER BY seq")]
    assert after == before
    assert c.execute("SELECT COUNT(*) FROM answer_confirmations").fetchone()[0] == 1
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()
