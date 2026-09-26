from __future__ import annotations

from datetime import timedelta

from webapp.persistence import autonomy_prepare as ap
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, NOW, conn, make_workspace  # noqa: F401


def test_enrolment_is_derived_from_latest_event(conn):
    ws = make_workspace(conn)
    assert ap.current_enrolment(conn, ws) is None and not ap.is_enrolled(conn, ws)
    ap.record_enrolment(conn, account_id=ACCOUNT, application_workspace_id=ws, action="ENROL",
                        actor_type="USER", actor="u", reason=None, now=NOW)
    assert ap.is_enrolled(conn, ws) and ap.enrolled_applications(conn, ACCOUNT) == [ws]
    ap.record_enrolment(conn, account_id=ACCOUNT, application_workspace_id=ws, action="UNENROL",
                        actor_type="USER", actor="u", reason="stop", now=NOW)
    assert not ap.is_enrolled(conn, ws) and ap.enrolled_applications(conn, ACCOUNT) == []


def test_retry_request_lookup_is_exact(conn):
    ws = make_workspace(conn)
    kw = dict(subject_type="APPLICATION", subject_id=ws, step_kind="FIT", input_fingerprint="fp1")
    assert ap.latest_retry_request(conn, **kw) is None
    first = ap.record_retry_request(conn, account_id=ACCOUNT, actor="u", now=NOW, **kw)
    second = ap.record_retry_request(conn, account_id=ACCOUNT, actor="u", now=NOW, **kw)
    assert ap.latest_retry_request(conn, **kw)["id"] == second["id"] != first["id"]
    assert ap.latest_retry_request(conn, **{**kw, "input_fingerprint": "fp2"}) is None


def test_latch_is_per_revision(conn):
    ws = make_workspace(conn)
    ap.record_latch(conn, application_workspace_id=ws, pack_revision="rev1", reason="EXPLICIT_REVIEW",
                    actor="u", now=NOW)
    assert ap.has_latch(conn, ws, "rev1") and not ap.has_latch(conn, ws, "rev2")


def test_notifications_dedupe_by_occurrence_and_seen_is_not_resolved(conn):
    kw = dict(account_id=ACCOUNT, key="needs_user:ws1:fp1", kind="NEEDS_USER", subject_type="APPLICATION",
              subject_id="ws1", detail={"n": 1})
    assert ap.create_notification(conn, now=NOW, **kw) is True
    assert ap.create_notification(conn, now=NOW, **kw) is False  # still open: no spam
    assert ap.mark_seen(conn, account_id=ACCOUNT, key=kw["key"], now=NOW) is True
    (item,) = ap.open_notifications(conn, ACCOUNT)
    assert item["seen"] is True  # seen, but still open
    assert ap.resolve_notification(conn, account_id=ACCOUNT, key=kw["key"], now=NOW) is True
    assert ap.open_notifications(conn, ACCOUNT) == []
    assert ap.create_notification(conn, now=NOW, **kw) is True  # recurrence after resolution notifies again
    assert ap.open_notifications(conn, ACCOUNT)[0]["seen"] is False


def test_candidate_exception_resolution_is_separate_and_current_by_seq(conn):
    from tests.webapp.services.autonomy_6c_fixtures import make_screening_row
    screening = make_screening_row(conn, candidate_id="cand_1", outcome="REQUIRE_USER", could_unlock=True)
    exc = ap.open_candidate_exception(conn, account_id=ACCOUNT, search_workspace_id="search_default",
                                      candidate_id="cand_1", screening_id=screening["id"],
                                      items=[{"kind": "rule", "ref": "fit_unknown"}], now=NOW)
    assert [e["id"] for e in ap.open_candidate_exceptions(conn, ACCOUNT)] == [exc["id"]]
    ap.resolve_candidate_exception(conn, exception_id=exc["id"], resolution="DISMISS", actor="u", reason=None, now=NOW)
    assert ap.open_candidate_exceptions(conn, ACCOUNT) == []
    assert ap.get_candidate_exception(conn, exc["id"])["resolution"]["resolution"] == "DISMISS"


def test_attempt_log_and_cycle_failure_counting(conn):
    ws = make_workspace(conn)
    kw = dict(subject_type="APPLICATION", subject_id=ws, step_kind="FIT", input_fingerprint="fp1")

    def attempt(event, error_class=None, retry_request_id=None, fingerprint="fp1"):
        attempt_id = ap.start_attempt(
            conn, subject_type="APPLICATION", subject_id=ws, step_kind="FIT",
            attempt_no=ap.next_attempt_no(conn, "APPLICATION", ws, "FIT"), input_fingerprint=fingerprint,
            authorization_decision_id=_decision(conn, ws), retry_request_id=retry_request_id,
            lease_generation=1, worker_id="w", reservation_ids=[], now=NOW)
        ap.finish_attempt(conn, attempt_id=attempt_id, event=event, error_class=error_class, now=NOW)
        return attempt_id

    attempt("FAILED", "TRANSIENT")
    attempt("FAILED", "TRANSIENT")
    assert ap.cycle_failures(conn, retry_request_id=None, **kw) == 2
    attempt("FAILED", "TRANSIENT", fingerprint="fp2")  # a different fingerprint is a different cycle
    assert ap.cycle_failures(conn, retry_request_id=None, **kw) == 2
    attempt("SUCCEEDED")
    assert ap.cycle_failures(conn, retry_request_id=None, **kw) == 0
    attempt("FAILED", "TRANSIENT", retry_request_id="rr_1")
    assert ap.cycle_failures(conn, retry_request_id="rr_1", **kw) == 1
    assert ap.cycle_failures(conn, retry_request_id=None, **kw) == 0


def test_orphaned_started_attempt_is_detected_and_terminal_rows_copy_subject(conn):
    ws = make_workspace(conn)
    attempt_id = ap.start_attempt(conn, subject_type="APPLICATION", subject_id=ws, step_kind="UNDERSTAND",
                                  attempt_no=1, input_fingerprint="fp", authorization_decision_id=_decision(conn, ws),
                                  retry_request_id=None, lease_generation=3, worker_id="w1",
                                  reservation_ids=["res_1"], now=NOW)
    (orphan,) = ap.orphaned_attempts(conn, "APPLICATION", ws)
    assert orphan["attempt_id"] == attempt_id and orphan["reservation_ids"] == ["res_1"]
    row = ap.finish_attempt(conn, attempt_id=attempt_id, event="ABANDONED", now=NOW + timedelta(minutes=20))
    assert (row["subject_id"], row["lease_generation"], row["event"]) == (ws, 3, "ABANDONED")
    assert ap.orphaned_attempts(conn, "APPLICATION", ws) == []


def _decision(conn, ws):
    from product.autonomy_gate import evaluate_authorization
    from tests.product.autonomy_fixtures import make_ctx
    from webapp.persistence.autonomy_ledger import insert_decision
    ctx = make_ctx(account_id=ACCOUNT, application_workspace_id=ws)
    return insert_decision(conn, ctx=ctx, decision=evaluate_authorization(ctx), commit=False)["id"]
