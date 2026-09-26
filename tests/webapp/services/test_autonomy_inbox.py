from __future__ import annotations

from datetime import timedelta

import pytest

from webapp.persistence import autonomy_prepare as ap
from webapp.services import autonomy_inbox as inbox
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, NOW, conn, enable_prepare, make_workspace  # noqa: F401


def _enrolled(conn, company="Acme"):
    ws = make_workspace(conn, company=company)
    ap.record_enrolment(conn, account_id=ACCOUNT, application_workspace_id=ws, action="ENROL", actor_type="USER",
                        actor="u", reason=None, now=NOW)
    ap.enqueue_application(conn, application_workspace_id=ws, account_id=ACCOUNT, now=NOW)
    ap.set_dormant(conn, queue="APPLICATION", item_id=ws, now=NOW)
    conn.commit()
    return ws


def _eligible(conn, ws):
    return conn.execute("SELECT next_eligible_at FROM autonomy_queue_items WHERE application_workspace_id = ?",
                        (ws,)).fetchone()[0]


def test_notify_outcome_dedupes_by_occurrence_key(conn):
    kw = dict(account_id=ACCOUNT, subject_type="APPLICATION", subject_id="ws1", kind="NEEDS_USER",
              reason="pack_review", fingerprint="fp1", detail={}, now=NOW)
    assert inbox.notify_outcome(conn, **kw) is True
    assert inbox.notify_outcome(conn, **kw) is False
    assert inbox.notify_outcome(conn, **{**kw, "fingerprint": "fp2"}) is True
    assert inbox.notification_key("NEEDS_USER", "APPLICATION", "ws1", "pack_review", "fp1") == \
        "NEEDS_USER:APPLICATION:ws1:pack_review:fp1"


def test_badge_counts_actionable_until_resolved_and_informational_until_seen(conn):
    a, b = _enrolled(conn, "A Co"), _enrolled(conn, "B Co")
    inbox.notify_outcome(conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=a, kind="NEEDS_USER",
                         reason="pack_review", fingerprint="f", detail={}, now=NOW)
    inbox.notify_outcome(conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=b, kind="PREPARED",
                         reason="prepared", fingerprint="f", detail={}, now=NOW)
    inbox.notify_outcome(conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=b,
                         kind="OPERATIONAL_ERROR", reason="internal", fingerprint="f", detail={}, now=NOW)
    conn.commit()
    assert inbox.inbox_summary(conn, ACCOUNT) == {"badge": 3, "actionable": 1, "informational_unseen": 2}
    assert inbox.mark_inbox_seen(conn, account_id=ACCOUNT, now=NOW) == 3
    assert inbox.inbox_summary(conn, ACCOUNT) == {"badge": 1, "actionable": 1, "informational_unseen": 0}


def test_inbox_groups_entries(conn):
    a = _enrolled(conn)
    inbox.notify_outcome(conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=a, kind="NEEDS_USER",
                         reason="pack_review", fingerprint="f", detail={"items": ["content_unit:u1"]}, now=NOW)
    inbox.notify_outcome(conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=a, kind="BLOCKED",
                         reason="rule_block", fingerprint="f", detail={}, now=NOW)
    conn.commit()
    view = inbox.build_inbox(conn, account_id=ACCOUNT)
    assert [e["kind"] for e in view["needs_answer"]] == ["NEEDS_USER"]
    assert view["needs_answer"][0]["detail"]["items"] == ["content_unit:u1"]
    assert [e["kind"] for e in view["informational"]] == ["BLOCKED"] and view["ready"] == []


def test_reconcile_resolves_only_vanished_conditions(conn):
    a, b = _enrolled(conn, "A Co"), _enrolled(conn, "B Co")
    for ws in (a, b):
        inbox.notify_outcome(conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=ws, kind="NEEDS_USER",
                             reason="pack_review", fingerprint="f", detail={}, now=NOW)
    ap.wake(conn, queue="APPLICATION", item_id=a, now=NOW)  # a wake alone proves nothing about the condition
    conn.commit()
    assert inbox.reconcile_notifications(conn, account_id=ACCOUNT, now=NOW) == 0
    assert len(ap.open_notifications(conn, ACCOUNT)) == 2
    # the scheduler re-derived a: its new outcome supersedes the old one, b is untouched
    kept = inbox.notification_key("BLOCKED", "APPLICATION", a, "blocked", "g")
    inbox.notify_outcome(conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=a, kind="BLOCKED",
                         reason="blocked", fingerprint="g", detail={}, now=NOW)
    assert inbox.supersede_outcomes(conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=a, now=NOW,
                                    keep_key=kept) == 1
    assert sorted((n["subject_id"], n["kind"]) for n in ap.open_notifications(conn, ACCOUNT)) == sorted(
        [(a, "BLOCKED"), (b, "NEEDS_USER")])


def test_candidate_question_resolves_when_the_question_is_answered(conn):
    from tests.webapp.services.autonomy_6c_fixtures import make_screening_row
    screening = make_screening_row(conn, candidate_id="cand_q", outcome="REQUIRE_USER", could_unlock=True)
    exc = ap.open_candidate_exception(conn, account_id=ACCOUNT, search_workspace_id="search_default",
                                      candidate_id="cand_q", screening_id=screening["id"], items=[], now=NOW)
    inbox.notify_outcome(conn, account_id=ACCOUNT, subject_type="CANDIDATE", subject_id="cand_q",
                         kind="CANDIDATE_QUESTION", reason="rule_require_user", fingerprint="fp", detail={}, now=NOW)
    conn.commit()
    assert inbox.reconcile_notifications(conn, account_id=ACCOUNT, now=NOW) == 0
    assert [e["kind"] for e in inbox.build_inbox(conn, account_id=ACCOUNT)["needs_answer"]] == ["CANDIDATE_QUESTION"]
    ap.resolve_candidate_exception(conn, exception_id=exc["id"], resolution="DISMISS", actor="u", reason=None, now=NOW)
    conn.commit()
    assert inbox.reconcile_notifications(conn, account_id=ACCOUNT, now=NOW) == 1


# ---- propagation --------------------------------------------------------------

def _blocker(conn, ws, subject="employment.notice_period"):
    from webapp.persistence.application_blockers import save_application_blocker
    from webapp.persistence.artifacts import save_artifact
    from webapp.persistence.policy_decisions import save_policy_decision
    fit = save_artifact(conn, workspace_id=ws, artifact_type="job_fit_result", payload={"x": ws})
    decision = save_policy_decision(
        conn, workspace_id=ws, stage="fit", source_artifact_id=fit["id"], review_item_type="question",
        subject_key="notice", outcome="REQUIRE_USER", policy_version="v1", policy_fingerprint="fp",
        reason_code="needs_user", reason="notice period", blocking=True)
    return save_application_blocker(
        conn, workspace_id=ws, policy_decision_id=decision["id"], source_artifact_id=fit["id"], stage="fit",
        blocker_type="question", subject_key="notice", question="Notice period?", resume_stage="fit",
        allowed_scopes=["APPLICATION_ONLY"], semantic_subject_key=subject)


def _writes(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("application_blockers", "blocker_resolutions", "review_decisions", "approved_answers")}


def test_propagation_wakes_waiting_siblings_in_reach_and_writes_nothing_else(conn):
    waiting, other_subject, not_waiting = _enrolled(conn, "A Co"), _enrolled(conn, "B Co"), _enrolled(conn, "C Co")
    _blocker(conn, waiting)
    _blocker(conn, other_subject, subject="licence.driving")
    conn.commit()
    before = _writes(conn)
    woken = inbox.propagate_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", reach="ACCOUNT",
                                   scope_id=None, now=NOW)
    conn.commit()
    assert woken == 1 and _eligible(conn, waiting) is not None
    assert _eligible(conn, other_subject) is None and _eligible(conn, not_waiting) is None
    assert _writes(conn) == before


def test_employer_reach_only_wakes_that_employer(conn):
    from product.autonomy_contract import normalized_employer_key
    acme, other = _enrolled(conn, "Acme"), _enrolled(conn, "Globex")
    _blocker(conn, acme)
    _blocker(conn, other)
    conn.commit()
    assert inbox.propagate_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", reach="EMPLOYER",
                                  scope_id=normalized_employer_key("Acme"), now=NOW) == 1
    assert _eligible(conn, acme) is not None and _eligible(conn, other) is None


def test_answer_blocker_is_atomic(conn, monkeypatch):
    ws = _enrolled(conn)
    blocker = _blocker(conn, ws)
    conn.commit()
    before = _writes(conn)
    monkeypatch.setattr(inbox, "propagate_answer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        inbox.answer_blocker(conn, account_id=ACCOUNT, workspace_id=ws, blocker_id=blocker["id"], request_id="r1",
                             answer_value={"type": "text", "value": "1 month"}, answer_scope="APPLICATION_ONLY",
                             resolved_by="u", reusable=True, subject="employment.notice_period", reach="ACCOUNT",
                             scope_id=None, context={}, now=NOW)
    assert _writes(conn) == before and _eligible(conn, ws) is None


def test_answer_blocker_resolves_saves_reusable_answer_and_wakes(conn):
    ws = _enrolled(conn)
    blocker = _blocker(conn, ws)
    conn.commit()
    out = inbox.answer_blocker(conn, account_id=ACCOUNT, workspace_id=ws, blocker_id=blocker["id"], request_id="r1",
                               answer_value={"type": "text", "value": "1 month"}, answer_scope="APPLICATION_ONLY",
                               resolved_by="u", reusable=True, subject="employment.notice_period", reach="ACCOUNT",
                               scope_id=None, context={}, now=NOW)
    assert out["resolution"] and out["approved_answer"] and _eligible(conn, ws) is not None


# ---- retry eligibility --------------------------------------------------------

def _fail(conn, ws, *, error_class="TRANSIENT", retry_request_id=None, n=1, fingerprint="fp"):
    from tests.webapp.persistence.test_autonomy_prepare_history import _decision
    for _ in range(n):
        attempt = ap.start_attempt(conn, subject_type="APPLICATION", subject_id=ws, step_kind="FIT",
                                   attempt_no=ap.next_attempt_no(conn, "APPLICATION", ws, "FIT"),
                                   input_fingerprint=fingerprint, authorization_decision_id=_decision(conn, ws),
                                   retry_request_id=retry_request_id, lease_generation=1, worker_id="w",
                                   reservation_ids=[], now=NOW)
        ap.finish_attempt(conn, attempt_id=attempt, event="FAILED", error_class=error_class, now=NOW)
    conn.commit()


def test_retry_only_for_an_exhausted_transient_cycle_and_one_shot(conn):
    ws = _enrolled(conn)
    kw = dict(account_id=ACCOUNT, subject_type="APPLICATION", subject_id=ws, step_kind="FIT", actor="u", now=NOW)
    _fail(conn, ws, n=3)
    with pytest.raises(inbox.RetryNotEligible):
        inbox.retry_failure(conn, **kw)  # cycle not exhausted yet
    _fail(conn, ws, n=1)
    request = inbox.retry_failure(conn, **kw)
    assert request["input_fingerprint"] == "fp" and _eligible(conn, ws) is not None
    with pytest.raises(inbox.RetryNotEligible):
        inbox.retry_failure(conn, **kw)  # a cycle is already open
    _fail(conn, ws, n=4, retry_request_id=request["id"])
    second = inbox.retry_failure(conn, **kw)  # the used request cannot be reused; a new one opens a new cycle
    assert second["id"] != request["id"]


def test_retry_refused_for_internal_failures(conn):
    ws = _enrolled(conn)
    _fail(conn, ws, error_class="INTERNAL")
    with pytest.raises(inbox.RetryNotEligible):
        inbox.retry_failure(conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=ws, step_kind="FIT",
                            actor="u", now=NOW)


# ---- wake hooks ------------------------------------------------------------------

@pytest.mark.parametrize("hook", ["resume", "resume_all", "capability", "policy", "profile", "resolve_blocker"])
def test_wake_hooks_reactivate_dormant_items_in_both_queues(conn, tmp_path, hook):
    from product.autonomy_contract import Capability
    from product.standing_policy import default_policy_document
    from webapp.services import autonomy_controls as controls
    enable_prepare(conn)
    ws = _enrolled(conn)
    ap.enqueue_candidate(conn, candidate_id="cand_w", account_id=ACCOUNT, search_workspace_id="search_default",
                         now=NOW)
    ap.set_dormant(conn, queue="CANDIDATE", item_id="cand_w", now=NOW)
    conn.commit()
    later = NOW + timedelta(minutes=5)
    if hook == "resume":
        controls.resume(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id=ws, actor="u", reason="r",
                        now=later)
    elif hook == "resume_all":
        controls.resume_all(conn, account_id=ACCOUNT, actor="u", reason="r", now=later,
                            sentinel_path=tmp_path / "AUTONOMY_HALT")
    elif hook == "capability":
        controls.set_capability(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT,
                                capability=Capability.FILL, actor="u", now=later)
    elif hook == "policy":
        controls.save_standing_policy(conn, account_id=ACCOUNT, doc=default_policy_document("Europe/London"),
                                      actor="u", now=later)
    elif hook == "profile":
        from webapp.services.pipeline import wake_after_profile_refresh
        wake_after_profile_refresh(conn, account_id=ACCOUNT, now=later)
        conn.commit()
    elif hook == "resolve_blocker":
        from webapp.services.decision_policy import resolve_blocker
        blocker = _blocker(conn, ws)
        conn.commit()
        resolve_blocker(conn, workspace_id=ws, blocker_id=blocker["id"], request_id="r9",
                        answer_value={"type": "text", "value": "x"}, answer_scope="APPLICATION_ONLY",
                        resolved_by="u")
    assert _eligible(conn, ws) is not None, hook
    if hook != "resolve_blocker":
        assert conn.execute("SELECT next_eligible_at FROM autonomy_candidate_queue WHERE candidate_id = 'cand_w'"
                            ).fetchone()[0] is not None, hook
