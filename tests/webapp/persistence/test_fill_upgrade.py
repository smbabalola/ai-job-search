"""Bundle 6D-B migration validation (spec §19, acceptance 18): a fresh
database gets all 20 migrations, and a database built by the real
master@8b28c57 (6D-A merged) code, holding 6D-A approvals, deltas, review
events, dispositions and approved answers, upgrades to 020_fill with every
6D-A row and trigger byte-identical, FK/integrity clean and a re-run a no-op.

The upgrade test skips only when 8b28c57 is genuinely absent from the clone
(a shallow CI checkout); any other git failure, or a present commit whose
archive or upgrade fails, still fails the test."""
from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

from webapp.persistence.db import connect, init_db
from webapp.persistence.migrations import FILL_APPEND_ONLY_TABLES, FILL_MIGRATION_ID, REVIEW_APPROVAL_MIGRATION_ID

BASE = "8b28c57"
REPO = Path(__file__).resolve().parents[3]
SIX_D_A_TABLES = ("application_approvals", "application_review_events", "review_deltas",
                  "application_field_dispositions", "approved_answers", "answer_confirmations")

SEED = textwrap.dedent("""
    import sys
    from datetime import datetime, timezone
    from pathlib import Path
    from product.autonomy_contract import Reach
    from webapp.persistence import review_approval as ra
    from webapp.persistence.autonomy_answers import approve_answer, confirm_answer
    from webapp.persistence.db import connect, init_db
    from webapp.persistence.workspaces import create_workspace
    from webapp.services.review_approval import open_review_delta

    db = Path(sys.argv[1])
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    init_db(db)
    conn = connect(db)
    ws = create_workspace(conn, company="Acme", title="Engineer")["id"]
    answer = approve_answer(conn, account_id="account_local", subject="employment.notice_period", value="1 month",
                            reach=Reach.APPLICATION, scope_id=ws, context={}, basis={"kind": "USER_ASSERTION"},
                            approved_by="u", now=now)
    confirm_answer(conn, approved_answer_id=answer["id"], confirmed_by="u", now=now, commit=False)
    ra.set_disposition(conn, account_id="account_local", application_workspace_id=ws,
                       answer_key="subject:employment.notice_period", disposition="ANSWER", actor="u", now=now)
    binding = {"account_id": "account_local", "application_workspace_id": ws, "fields": [], "documents": []}
    first = ra.insert_approval(conn, account_id="account_local", application_workspace_id=ws, binding=binding,
                               binding_hash="sha256:" + "1" * 64, supersedes_id=None, batch_id=None,
                               resolved_delta_ids=[], actor="u", now=now)
    ra.record_event(conn, account_id="account_local", application_workspace_id=ws, event="APPROVED",
                    binding_hash=first["binding_hash"], detail={"approval_id": first["id"]}, actor="u", now=now)
    conn.commit()
    delta = open_review_delta(conn, account_id="account_local", application_workspace_id=ws, kind="NEW_QUESTION",
                              answer_key=None, subject=None, required=True, question="How did you hear about us?",
                              observed={"field_key": "q1"}, source="FILL_SESSION:s1", now=now)
    ra.insert_approval(conn, account_id="account_local", application_workspace_id=ws, binding=binding,
                       binding_hash="sha256:" + "2" * 64, supersedes_id=first["id"], batch_id="b1",
                       resolved_delta_ids=[delta["id"]], actor="u", now=now)
    conn.commit()
    conn.close()
    print(ws)
""")


def _migrations(c):
    return [r[0] for r in c.execute("SELECT id FROM schema_migrations ORDER BY rowid")]


def _schema(c):
    return sorted(tuple(r) for r in c.execute("SELECT type, name, sql FROM sqlite_master"))


def _objects_on(c, table):
    return sorted(tuple(r) for r in c.execute("SELECT type, name, sql FROM sqlite_master WHERE tbl_name = ? "
                                              "AND type IN ('trigger', 'index')", (table,)))


def _rows(c, table):
    return [tuple(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def test_fresh_database_gets_twenty_migrations_and_a_rerun_is_a_noop(tmp_path):
    db = tmp_path / "fresh.sqlite3"
    init_db(db)
    c = connect(db)
    ids, schema = _migrations(c), _schema(c)
    assert len(ids) == 20 and ids[-1] == FILL_MIGRATION_ID
    init_db(db)
    assert _migrations(c) == ids and _schema(c) == schema
    c.close()


@pytest.fixture
def base_db(tmp_path):
    # --verify --quiet exits 1 (no output) only when the object is missing;
    # anything else non-zero (not a repo, git broken) is a real failure.
    present = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{BASE}^{{commit}}"], cwd=REPO,
                             capture_output=True, text=True)
    if present.returncode == 1:
        pytest.skip(f"{BASE} is not in this clone (shallow checkout); the upgrade proof needs full history")
    assert present.returncode == 0, f"git rev-parse failed: {present.stderr[:200]}"
    archive =subprocess.run(["git", "archive", BASE, "webapp", "product"], cwd=REPO, capture_output=True)
    assert archive.returncode == 0, f"{BASE} must be in this clone: {archive.stderr.decode()[:200]}"
    root = tmp_path / "base"
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(root, filter="data")
    db = tmp_path / "base.sqlite3"
    env = {**os.environ, "PYTHONPATH": str(root)}
    seeded = subprocess.run([sys.executable, "-c", SEED, str(db)], cwd=root, env=env, capture_output=True, text=True)
    assert seeded.returncode == 0, seeded.stderr
    return db, seeded.stdout.strip()


def test_a_6da_database_upgrades_to_020_with_6da_rows_and_triggers_unchanged(base_db):
    db, _ = base_db
    c = sqlite3.connect(db)
    before_ids = _migrations(c)
    assert before_ids[-1] == REVIEW_APPROVAL_MIGRATION_ID and FILL_MIGRATION_ID not in before_ids
    rows_before = {t: _rows(c, t) for t in SIX_D_A_TABLES}
    objects_before = {t: _objects_on(c, t) for t in SIX_D_A_TABLES}
    tables_before = {r[0]: r[1] for r in c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
    assert len(rows_before["application_approvals"]) == 2 and len(rows_before["review_deltas"]) == 1
    c.close()

    init_db(db)
    c = connect(db)
    assert _migrations(c) == [*before_ids, FILL_MIGRATION_ID]
    assert {t: _rows(c, t) for t in SIX_D_A_TABLES} == rows_before
    assert {t: _objects_on(c, t) for t in SIX_D_A_TABLES} == objects_before
    tables_after = {r[0]: r[1] for r in c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
    assert {k: v for k, v in tables_after.items() if k in tables_before} == tables_before  # no table DDL changed
    new_tables = set(tables_after) - set(tables_before)
    assert set(FILL_APPEND_ONLY_TABLES) <= new_tables
    for table in FILL_APPEND_ONLY_TABLES:  # every fill evidence table is append-only from the start
        triggers = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                                            (table,))}
        assert triggers, table
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    ids, schema = _migrations(c), _schema(c)
    init_db(db)
    assert _migrations(c) == ids and _schema(c) == schema
    c.close()
