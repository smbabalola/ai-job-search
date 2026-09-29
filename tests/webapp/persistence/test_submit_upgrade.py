"""Bundle 6E-A migration validation (spec §17, acceptance 19): a database
built by the real master@efc6577 (6D-B merged) code -- holding submission
intents of every pre-6E-A source, an intent override referencing one of
them, and a 6D-B fill run -- upgrades to 021_human_submit with every intent
row, the override and the fill run unchanged, FK/integrity clean, and a
re-run a no-op.

The upgrade test skips only when efc6577 is genuinely absent from the clone
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
from webapp.persistence.migrations import FILL_MIGRATION_ID, HUMAN_SUBMIT_MIGRATION_ID, SUBMIT_APPEND_ONLY_TABLES

BASE = "efc6577"
REPO = Path(__file__).resolve().parents[3]

SEED = textwrap.dedent("""
    import sys
    from datetime import datetime, timezone
    from pathlib import Path
    from webapp.persistence.autonomy_ledger import claim_intent
    from webapp.persistence.db import connect, init_db
    from webapp.persistence.workspaces import create_workspace

    db = Path(sys.argv[1])
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    init_db(db)
    conn = connect(db)
    ws = create_workspace(conn, company="Acme", title="Engineer")["id"]
    other = create_workspace(conn, company="Beta", title="Engineer", commit=False)["id"]
    a = claim_intent(conn, account_id="account_local", job_identity_key="k:auto", application_workspace_id=ws,
                     source="AUTONOMOUS", now=now)
    claim_intent(conn, account_id="account_local", job_identity_key="k:handoff", application_workspace_id=ws,
                 source="HUMAN_HANDOFF", state="CONFIRMED", now=now)
    claim_intent(conn, account_id="account_local", job_identity_key="k:applied", application_workspace_id=other,
                 source="HUMAN_APPLIED", state="CONFIRMED", now=now)
    conn.execute("INSERT INTO intent_overrides (id, intent_id, actor, reason, created_at) "
                 "VALUES ('ov_1', ?, 'u', 'reapply', '2026-09-29T12:00:00.000000+00:00')", (a["id"],))
    conn.execute("INSERT INTO fill_runs (id, account_id, application_workspace_id, handoff_session_id, "
                 "executor_instance_id, browser_session_id, execution_tab_id, timing_version, created_at) "
                 "VALUES ('fr_1', 'account_local', ?, 'hs_1', 'ex_1', 'b1', 7, 'fill-timing.v1', "
                 "'2026-09-29T12:00:00.000000+00:00')", (ws,))
    conn.execute("INSERT INTO fill_run_events (id, fill_run_id, event, reason, detail_json, created_at) "
                 "VALUES ('fre_1', 'fr_1', 'FILLED_AWAITING_SUBMISSION', NULL, '{}', "
                 "'2026-09-29T12:00:00.000000+00:00')")
    conn.commit()
    conn.close()
""")


def _migrations(c):
    return [r[0] for r in c.execute("SELECT id FROM schema_migrations ORDER BY rowid")]


def _rows(c, table):
    return [tuple(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]


@pytest.fixture
def base_db(tmp_path):
    # --verify --quiet exits 1 (no output) only when the object is missing;
    # anything else non-zero (not a repo, git broken) is a real failure.
    present = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{BASE}^{{commit}}"], cwd=REPO,
                             capture_output=True, text=True)
    if present.returncode == 1:
        pytest.skip(f"{BASE} is not in this clone (shallow checkout); the upgrade proof needs full history")
    assert present.returncode == 0, f"git rev-parse failed: {present.stderr[:200]}"
    archive = subprocess.run(["git", "archive", BASE, "webapp", "product"], cwd=REPO, capture_output=True)
    assert archive.returncode == 0, f"{BASE} must be in this clone: {archive.stderr.decode()[:200]}"
    root = tmp_path / "base"
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(root, filter="data")
    db = tmp_path / "base.sqlite3"
    env = {**os.environ, "PYTHONPATH": str(root)}
    seeded = subprocess.run([sys.executable, "-c", SEED, str(db)], cwd=root, env=env, capture_output=True, text=True)
    assert seeded.returncode == 0, seeded.stderr
    return db


def test_a_6d_b_database_upgrades_to_021_with_intents_overrides_and_runs_unchanged(base_db):
    c = sqlite3.connect(base_db)
    before_ids = _migrations(c)
    assert before_ids[-1] == FILL_MIGRATION_ID and HUMAN_SUBMIT_MIGRATION_ID not in before_ids
    kept = ("submission_intents", "intent_overrides", "fill_runs", "fill_run_events")
    rows_before = {t: _rows(c, t) for t in kept}
    assert len(rows_before["submission_intents"]) == 3 and len(rows_before["intent_overrides"]) == 1
    index_before = c.execute("SELECT sql FROM sqlite_master WHERE name = 'idx_submission_intents_live'").fetchone()
    c.close()

    init_db(base_db)
    c = connect(base_db)
    assert _migrations(c) == [*before_ids, HUMAN_SUBMIT_MIGRATION_ID]
    assert {t: _rows(c, t) for t in kept} == rows_before
    assert tuple(c.execute("SELECT sql FROM sqlite_master WHERE name = 'idx_submission_intents_live'").fetchone()) \
        == tuple(index_before)
    for table in SUBMIT_APPEND_ONLY_TABLES:
        assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                         (table,)).fetchone()[0] == 2, table
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # the upgraded table accepts the new source and still enforces one live intent per identity
    c.execute("INSERT INTO submission_intents (id, account_id, job_identity_key, application_workspace_id, state, "
              "source, created_at, updated_at) SELECT 'i_new', account_id, 'k:new', application_workspace_id, "
              "'CLAIMED', 'HUMAN_AUTHORIZED', created_at, updated_at FROM submission_intents LIMIT 1")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO submission_intents (id, account_id, job_identity_key, application_workspace_id, "
                  "state, source, created_at, updated_at) SELECT 'i_dup', account_id, 'k:handoff', "
                  "application_workspace_id, 'CLAIMED', 'HUMAN_AUTHORIZED', created_at, updated_at "
                  "FROM submission_intents LIMIT 1")
    c.rollback()
    schema = sorted(tuple(r) for r in c.execute("SELECT type, name, sql FROM sqlite_master"))
    c.close()
    init_db(base_db)  # a re-run is a no-op
    c = connect(base_db)
    assert _migrations(c) == [*before_ids, HUMAN_SUBMIT_MIGRATION_ID]
    assert sorted(tuple(r) for r in c.execute("SELECT type, name, sql FROM sqlite_master")) == schema
    c.close()
