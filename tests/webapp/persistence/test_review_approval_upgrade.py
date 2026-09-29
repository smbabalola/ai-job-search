"""Bundle 6D-A migration validation (spec §19): a fresh database gets all 19
migrations, and a pre-6D database built by the real master@fc316ee code, with
6C data, v2 document data, approved answers, a confirmation and a superseding
answer, upgrades through 019 with every approved answer byte-identical."""
from __future__ import annotations

# Legacy-chain assertions are scoped to 001-021 (id < '022'); Bundle 7
# migrations are covered by test_schema_parity.py and their own tests.

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
from webapp.persistence.migrations import REVIEW_APPROVAL_MIGRATION_ID

PRE_6D = "fc316ee"
REPO = Path(__file__).resolve().parents[3]

SEED = textwrap.dedent("""
    import io, sys
    from datetime import datetime, timezone
    from pathlib import Path
    from docx import Document
    from product.autonomy_contract import Reach
    from webapp.persistence import autonomy_prepare as ap
    from webapp.persistence.autonomy_answers import approve_answer, confirm_answer
    from webapp.persistence.db import connect, init_db
    from webapp.persistence.workspaces import create_workspace
    from webapp.services.application_documents import select_application_document, upload_application_document

    db, docs = Path(sys.argv[1]), Path(sys.argv[2])
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    init_db(db)
    conn = connect(db)
    ws = create_workspace(conn, company="Acme", title="Engineer")["id"]
    for kind in ("cv", "cover_letter"):
        stream = io.BytesIO()
        document = Document()
        document.add_paragraph(kind)
        document.save(stream)
        version = upload_application_document(conn, ws, kind=kind, filename=kind + ".docx",
                                              content=stream.getvalue(), documents_root=docs,
                                              account_id="account_local")
        select_application_document(conn, ws, kind=kind, document_version_id=version["id"], expected_revision=0,
                                    account_id="account_local")
    ap.enqueue_application(conn, application_workspace_id=ws, account_id="account_local", now=now)
    first = approve_answer(conn, account_id="account_local", subject="employment.notice_period", value="1 month",
                           reach=Reach.ACCOUNT, scope_id=None, context={}, basis={"kind": "USER_ASSERTION"},
                           approved_by="u", now=now)
    confirm_answer(conn, approved_answer_id=first["id"], confirmed_by="u", now=now)
    approve_answer(conn, account_id="account_local", subject="employment.notice_period", value="2 months",
                   reach=Reach.ACCOUNT, scope_id=None, context={}, basis={"kind": "USER_ASSERTION"},
                   approved_by="u", now=now, supersedes_id=first["id"])
    conn.commit()
    conn.close()
    print(ws)
""")


def _migrations(c):
    return [r[0] for r in c.execute("SELECT id FROM schema_migrations WHERE id < '022' ORDER BY rowid")]


def _schema(c):
    return sorted(tuple(r) for r in c.execute("SELECT type, name, sql FROM sqlite_master"))


def _objects_on(c, table, kind):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = ? AND tbl_name = ?", (kind, table))}


def test_fresh_database_gets_all_migrations_through_020_and_a_rerun_is_a_noop(tmp_path):
    db = tmp_path / "fresh.sqlite3"
    init_db(db)
    c = connect(db)
    ids, schema = _migrations(c), _schema(c)
    assert len(ids) == 21 and ids[-3:] == [REVIEW_APPROVAL_MIGRATION_ID, "020_fill", "021_human_submit"]
    init_db(db)
    assert _migrations(c) == ids and _schema(c) == schema
    c.close()


@pytest.fixture
def pre_6d_db(tmp_path):
    archive = subprocess.run(["git", "archive", PRE_6D, "webapp", "product"], cwd=REPO, capture_output=True)
    if archive.returncode != 0:
        pytest.skip(f"{PRE_6D} is not in this clone")
    root = tmp_path / "pre6d"
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(root, filter="data")
    db = tmp_path / "pre6d.sqlite3"
    env = {**os.environ, "PYTHONPATH": str(root)}
    seeded = subprocess.run([sys.executable, "-c", SEED, str(db), str(tmp_path / "docs")], cwd=root, env=env,
                            capture_output=True, text=True)
    assert seeded.returncode == 0, seeded.stderr
    return db, seeded.stdout.strip()


def test_pre_6d_database_upgrades_through_019(pre_6d_db):
    db, ws = pre_6d_db
    c = sqlite3.connect(db)
    migrations_before = [r[0] for r in c.execute("SELECT id FROM schema_migrations WHERE id < '022' ORDER BY rowid")]
    answers_before = [tuple(r) for r in c.execute("SELECT * FROM approved_answers ORDER BY seq")]
    triggers_before = _objects_on(c, "approved_answers", "trigger")
    indexes_before = _objects_on(c, "approved_answers", "index")
    counts_before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
        "answer_confirmations", "application_document_versions", "application_document_selections",
        "autonomy_queue_items", "workspaces")}
    c.close()
    assert REVIEW_APPROVAL_MIGRATION_ID not in migrations_before and len(answers_before) == 2

    init_db(db)
    c = connect(db)
    assert _migrations(c) == [*migrations_before, REVIEW_APPROVAL_MIGRATION_ID, "020_fill", "021_human_submit"]
    assert [tuple(r) for r in c.execute("SELECT * FROM approved_answers ORDER BY seq")] == answers_before
    assert _objects_on(c, "approved_answers", "trigger") >= triggers_before
    assert _objects_on(c, "approved_answers", "index") >= indexes_before
    assert {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts_before} == counts_before

    # APPLICATION reach is accepted; the append-only guard still holds.
    c.execute("INSERT INTO approved_answers (id, account_id, subject, answer_kind, value_json, reach, scope_id, "
              "context_json, provenance, basis_json, approved_by, created_at) VALUES ('ans_app', 'account_local', "
              "'demographic.eeo', 'STRUCTURED', '\"x\"', 'APPLICATION', ?, '{}', 'USER', "
              "'{\"kind\": \"USER_ASSERTION\"}', 'u', '2026-09-27T12:00:00.000000+00:00')", (ws,))
    c.commit()
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE approved_answers SET value_json = '\"y\"' WHERE id = 'ans_app'")
    c.rollback()

    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    ids, schema = _migrations(c), _schema(c)
    init_db(db)
    assert _migrations(c) == ids and _schema(c) == schema
    c.close()
