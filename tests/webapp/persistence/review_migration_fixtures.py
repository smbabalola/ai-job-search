"""A database at the 018 level (before migration 019) with approved-answer
history, for the 019 approved_answers rebuild tests."""
from __future__ import annotations

from unittest import mock

from webapp.persistence import migrations
from webapp.persistence.db import connect, init_db
from webapp.persistence.migrations import FILL_MIGRATION_ID, REVIEW_APPROVAL_MIGRATION_ID


def pre_019_db_with_answers(tmp_path):
    db = tmp_path / "pre019.sqlite3"
    # A true pre-019 database: 020 (which references 019's tables) is skipped too.
    with (mock.patch.object(migrations, "_migrate_review_approval", lambda conn: None),
          mock.patch.object(migrations, "_migrate_fill", lambda conn: None)):
        init_db(db)
    c = connect(db)
    c.execute("DELETE FROM schema_migrations WHERE id IN (?, ?)", (REVIEW_APPROVAL_MIGRATION_ID, FILL_MIGRATION_ID))
    for answer_id, supersedes in (("ans_a", None), ("ans_b", "ans_a")):
        c.execute(
            "INSERT INTO approved_answers (id, account_id, subject, answer_kind, value_json, reach, scope_id, "
            "context_json, provenance, basis_json, basis_profile_version_id, supersedes_id, "
            "source_blocker_resolution_id, approved_by, created_at) VALUES (?, 'account_local', 'notice_period', "
            "'STRUCTURED', '\"1 month\"', 'ACCOUNT', 'account_local', '{}', 'USER', '{\"kind\": \"USER_ASSERTION\"}', "
            "NULL, ?, NULL, 'u', '2026-09-20T12:00:00.000000+00:00')", (answer_id, supersedes))
    c.execute("INSERT INTO answer_confirmations (id, approved_answer_id, confirmed_by, created_at) "
              "VALUES ('conf_1', 'ans_b', 'u', '2026-09-21T12:00:00.000000+00:00')")
    c.commit()
    before = [tuple(r) for r in c.execute("SELECT * FROM approved_answers ORDER BY seq")]
    c.close()
    return db, before
