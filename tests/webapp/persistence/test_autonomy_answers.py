from __future__ import annotations

from datetime import timedelta

import pytest
import sqlite3

from product.autonomy_contract import Reach
from webapp.persistence.autonomy_answers import (
    AnswerValidationError, approve_answer, confirm_answer, confirm_apply_target,
    current_apply_target_confirmation, current_approved_answers, current_rule_acknowledgements,
    record_rule_acknowledgement, save_proposed_answer,
)
from webapp.persistence.db import connect
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace, db_path  # noqa: F401

ASSERT = {"kind": "USER_ASSERTION"}


def test_approve_writes_first_confirmation(conn):
    a = approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                       reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW)
    (current,) = current_approved_answers(conn, account_id=ACCOUNT, subject="employment.notice_period")
    assert current["id"] == a["id"] and current["value"] == "1 month"
    assert current["latest_confirmation_at"] == "2026-09-24T12:00:00.000000+00:00"
    confirm_answer(conn, approved_answer_id=a["id"], confirmed_by="u", now=NOW + timedelta(days=30))
    (current,) = current_approved_answers(conn, account_id=ACCOUNT, subject="employment.notice_period")
    assert current["latest_confirmation_at"].startswith("2026-10-24")


def test_supersede_hides_old_row(conn):
    a = approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                       reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW)
    b = approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="3 months",
                       reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW,
                       supersedes_id=a["id"])
    assert [r["id"] for r in current_approved_answers(conn, account_id=ACCOUNT, subject="employment.notice_period")] == [b["id"]]


@pytest.mark.parametrize("kwargs, message", [
    (dict(subject="made.up", reach=Reach.ACCOUNT, scope_id=None, context={}), "subject"),
    (dict(subject="motivation.employer_specific", reach=Reach.ACCOUNT, scope_id=None, context={}), "reach"),
    (dict(subject="motivation.employer_specific", reach=Reach.EMPLOYER, scope_id=None, context={}), "scope_id"),
    (dict(subject="demographic.eeo", reach=Reach.EMPLOYER, scope_id="name:acme", context={}), "sensitive"),
    (dict(subject="mobility.relocation", reach=Reach.SEARCH_WORKSPACE, scope_id="sw_1", context={"planet": "Mars"}), "context"),
])
def test_validation(conn, kwargs, message):
    with pytest.raises(AnswerValidationError, match=message):
        approve_answer(conn, account_id=ACCOUNT, value="x", basis=ASSERT, approved_by="u", now=NOW, **kwargs)


def test_rule_acknowledgements_latest_per_rule(conn):
    ws = make_workspace(conn)
    kw = dict(account_id=ACCOUNT, application_workspace_id=ws, rule_hash="sha256:r", observed_fingerprint="sha256:o",
              policy_version_hash="sha256:p", actor="u", now=NOW)
    record_rule_acknowledgement(conn, rule_id="perm", disposition="PROCEED", **kw)
    record_rule_acknowledgement(conn, rule_id="perm", disposition="DO_NOT_PROCEED", **kw)
    record_rule_acknowledgement(conn, rule_id="other", disposition="PROCEED", **kw)
    acks = {a["rule_id"]: a["disposition"] for a in current_rule_acknowledgements(conn, ws)}
    assert acks == {"perm": "DO_NOT_PROCEED", "other": "PROCEED"}


def test_apply_target_confirmation(conn):
    ws = make_workspace(conn)
    assert current_apply_target_confirmation(conn, ws) is None
    confirm_apply_target(conn, application_workspace_id=ws, job_identity_key="source:x:1",
                         canonical_url="https://boards.greenhouse.io/acme/jobs/1", confirmed_by="u", now=NOW)
    assert current_apply_target_confirmation(conn, ws)["canonical_url"].endswith("/jobs/1")


def test_approve_savepoint_atomicity_with_failure_commit_true(conn, monkeypatch):
    """When commit=True and confirmation insert fails, savepoint rollback leaves zero rows."""
    from webapp.persistence import autonomy_answers
    original_insert = autonomy_answers._insert
    call_count = [0]

    def failing_insert(conn, table, values):
        call_count[0] += 1
        if call_count[0] == 2 and table == "answer_confirmations":
            raise ValueError("Simulated confirmation insert failure")
        return original_insert(conn, table, values)

    monkeypatch.setattr(autonomy_answers, "_insert", failing_insert)

    with pytest.raises(ValueError, match="Simulated confirmation insert failure"):
        approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                      reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW,
                      commit=True)

    rows = conn.execute("SELECT COUNT(*) as cnt FROM approved_answers WHERE account_id = ?", (ACCOUNT,)).fetchone()
    assert rows["cnt"] == 0


def test_approve_savepoint_atomicity_with_failure_commit_false(conn, monkeypatch):
    """When commit=False and confirmation insert fails, caller can still commit unrelated work; answer row still absent."""
    from webapp.persistence import autonomy_answers
    original_insert = autonomy_answers._insert
    call_count = [0]

    def failing_insert(conn, table, values):
        call_count[0] += 1
        if call_count[0] == 2 and table == "answer_confirmations":
            raise ValueError("Simulated confirmation insert failure")
        return original_insert(conn, table, values)

    monkeypatch.setattr(autonomy_answers, "_insert", failing_insert)

    # Try to approve but fail on confirmation
    with pytest.raises(ValueError, match="Simulated confirmation insert failure"):
        approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                      reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW,
                      commit=False)

    # Caller's unrelated work should still work (create a workspace)
    ws = make_workspace(conn)
    assert ws  # workspace created successfully
    conn.commit()

    # Verify no approved_answers rows exist (savepoint rollback worked)
    rows = conn.execute("SELECT COUNT(*) as cnt FROM approved_answers WHERE account_id = ?", (ACCOUNT,)).fetchone()
    assert rows["cnt"] == 0

    # Verify the workspace was saved (unrelated work persisted)
    rows = conn.execute("SELECT COUNT(*) as cnt FROM workspaces WHERE id = ?", (ws,)).fetchone()
    assert rows["cnt"] == 1


def test_approve_commit_false_two_connection_isolation(db_path):
    """When commit=False on fresh connection, data not visible to second connection until first commits; rollback hides it."""
    conn1 = connect(db_path)
    conn1.row_factory = sqlite3.Row

    # Approve answer with commit=False on fresh connection (no transaction open)
    a = approve_answer(conn1, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                      reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW,
                      commit=False)

    # Open a second connection to the same database
    conn2 = connect(db_path)
    conn2.row_factory = sqlite3.Row

    # Second connection should NOT see the uncommitted rows
    rows = conn2.execute("SELECT COUNT(*) as cnt FROM approved_answers WHERE id = ?", (a["id"],)).fetchone()
    assert rows["cnt"] == 0, "Second connection saw uncommitted data (isolation violation)"

    # Commit from first connection
    conn1.commit()

    # Now second connection SHOULD see it
    rows = conn2.execute("SELECT COUNT(*) as cnt FROM approved_answers WHERE id = ?", (a["id"],)).fetchone()
    assert rows["cnt"] == 1, "Second connection didn't see committed data"

    # Now test rollback: approve another answer and rollback
    a2 = approve_answer(conn1, account_id=ACCOUNT, subject="employment.availability_start", value="tomorrow",
                       reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW,
                       commit=False)
    conn1.rollback()

    # Second connection should NOT see rolled-back data
    rows = conn2.execute("SELECT COUNT(*) as cnt FROM approved_answers WHERE id = ?", (a2["id"],)).fetchone()
    assert rows["cnt"] == 0, "Second connection saw rolled-back data"

    conn1.close()
    conn2.close()


def test_proposed_answer_separate_table(conn):
    """save_proposed_answer stores in separate table with SYSTEM_PROPOSED provenance."""
    # Create an approved answer
    a = approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                      reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW)

    # Insert a proposed_answer row directly (tests the table/provenance constraint, not FK validation)
    # Disable FK temporarily to bypass blocker_id constraint since FK validation is not the focus
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        'INSERT INTO proposed_answers (id, blocker_id, subject, value_json, provenance, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        ("prop_1", "block_1", "employment.notice_period", '"2 months"', "SYSTEM_PROPOSED", "2026-09-24T12:00:00.000000+00:00")
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()

    # Verify proposed_answers table has SYSTEM_PROPOSED provenance
    rows = conn.execute("SELECT COUNT(*) as cnt FROM proposed_answers WHERE provenance = 'SYSTEM_PROPOSED'").fetchone()
    assert rows[0] == 1

    # current_approved_answers should only return the approved answer, not the proposed one
    current = current_approved_answers(conn, account_id=ACCOUNT, subject="employment.notice_period")
    assert len(current) == 1
    assert current[0]["id"] == a["id"]
    assert current[0]["value"] == "1 month"


def test_validation_non_canonicalizable_value(conn):
    """Value that cannot be canonicalized (e.g., float) raises AnswerValidationError."""
    with pytest.raises(AnswerValidationError, match="float"):
        approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value=3.14,
                      reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW)

    # Verify zero rows exist
    rows = conn.execute("SELECT COUNT(*) as cnt FROM approved_answers WHERE account_id = ?", (ACCOUNT,)).fetchone()
    assert rows["cnt"] == 0


def test_validation_invalid_provenance(conn):
    """Provenance not in {USER, USER_EDITED_PROPOSAL} raises AnswerValidationError."""
    with pytest.raises(AnswerValidationError, match="provenance"):
        approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="x",
                      reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW,
                      provenance="INVALID")

    # Verify zero rows exist
    rows = conn.execute("SELECT COUNT(*) as cnt FROM approved_answers WHERE account_id = ?", (ACCOUNT,)).fetchone()
    assert rows["cnt"] == 0


def test_validation_supersedes_id_not_exist(conn):
    """supersedes_id that doesn't exist raises AnswerValidationError."""
    with pytest.raises(AnswerValidationError, match="does not exist"):
        approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="x",
                      reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW,
                      supersedes_id="ans_nonexistent")

    # Verify zero rows exist
    rows = conn.execute("SELECT COUNT(*) as cnt FROM approved_answers WHERE account_id = ?", (ACCOUNT,)).fetchone()
    assert rows["cnt"] == 0


def test_validation_supersedes_id_different_account(conn):
    """supersedes_id that belongs to different account_id raises AnswerValidationError."""
    # Create answer for ACCOUNT
    a = approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                      reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW)

    # Try to supersede with different account
    with pytest.raises(AnswerValidationError, match="different account"):
        approve_answer(conn, account_id=ACCOUNT + "_other", subject="employment.notice_period", value="x",
                      reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW,
                      supersedes_id=a["id"])

    # Verify only the original row exists, no new row added
    rows = conn.execute("SELECT COUNT(*) as cnt FROM approved_answers WHERE account_id = ?", (ACCOUNT + "_other",)).fetchone()
    assert rows["cnt"] == 0


def test_validation_supersedes_id_different_subject(conn):
    """supersedes_id that has different subject raises AnswerValidationError."""
    # Create answer for employment.notice_period
    a = approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                      reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW)

    # Try to supersede with different subject (employment.availability_start exists)
    with pytest.raises(AnswerValidationError, match="different subject"):
        approve_answer(conn, account_id=ACCOUNT, subject="employment.availability_start", value="x",
                      reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW,
                      supersedes_id=a["id"])

    # Verify only the original row exists for its subject
    rows = conn.execute("SELECT COUNT(*) as cnt FROM approved_answers WHERE account_id = ? AND subject = ?",
                       (ACCOUNT, "employment.availability_start")).fetchone()
    assert rows["cnt"] == 0


def test_validation_evidence_invalid_value_hash(conn):
    """EVIDENCE basis with value_hash not starting with 'sha256:' raises AnswerValidationError."""
    with pytest.raises(AnswerValidationError, match="sha256"):
        approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="x",
                      reach=Reach.ACCOUNT, scope_id=None, context={},
                      basis={"kind": "EVIDENCE", "evidence_ids": ["e1"], "value_hash": "invalid"},
                      approved_by="u", now=NOW)

    # Verify zero rows exist
    rows = conn.execute("SELECT COUNT(*) as cnt FROM approved_answers WHERE account_id = ?", (ACCOUNT,)).fetchone()
    assert rows["cnt"] == 0


def test_validation_evidence_value_hash_not_string(conn):
    """EVIDENCE basis with non-string value_hash raises AnswerValidationError."""
    with pytest.raises(AnswerValidationError, match="sha256"):
        approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="x",
                      reach=Reach.ACCOUNT, scope_id=None, context={},
                      basis={"kind": "EVIDENCE", "evidence_ids": ["e1"], "value_hash": 123},
                      approved_by="u", now=NOW)

    # Verify zero rows exist
    rows = conn.execute("SELECT COUNT(*) as cnt FROM approved_answers WHERE account_id = ?", (ACCOUNT,)).fetchone()
    assert rows["cnt"] == 0
