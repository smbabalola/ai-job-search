from __future__ import annotations

from datetime import timedelta

import pytest

from product.autonomy_contract import Reach
from webapp.persistence.autonomy_answers import (
    AnswerValidationError, approve_answer, confirm_answer, confirm_apply_target,
    current_apply_target_confirmation, current_approved_answers, current_rule_acknowledgements,
    record_rule_acknowledgement,
)
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401

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
