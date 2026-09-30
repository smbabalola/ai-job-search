"""6E-A spec §11, §15, §16.2: server-determined results, submission-result.v1,
ambiguity resolution by the user, the submission status, and the sweeps."""
from __future__ import annotations

import pytest

from tests.webapp.services.submit_fixtures import (  # noqa: F401
    NOW, SEC, V2_ACCOUNT, filled_world, grant_world, review_observation, v2_chain,
)
from tests.webapp.services.test_human_submit_authorize import authorize, ready
from tests.webapp.services.test_human_submit_preclick import authorized, pre_click
from webapp.persistence import submit as sp
from webapp.persistence.autonomy_ledger import attempt_state
from webapp.services import human_submit as hs
from webapp.services.autonomy import expire_unclicked, mark_stale_dispatches_ambiguous
from product.submit_constants import DISPATCH_RESULT_TIMEOUT

E1 = 9201


def dispatched(w, at=NOW):
    auth = authorized(w, at)
    attempt = pre_click(w, auth, at=at + SEC)["attempt_id"]
    assert hs.human_record_click_dispatched(w.conn, settings=w.settings, attempt_id=attempt, now=at + 2 * SEC)
    return auth, attempt


def evidence(**overrides):
    base = {"click_performed": True, "egress_ever_installed": True, "total_restored_verified": True,
            "success_observed": False, "failure_observed": False, "content_changed": False,
            "matched_rule_ids": [E1], "matched_rules_available": True, "cause": None}
    base.update(overrides)
    return base


def report(w, attempt, at=NOW + 10 * SEC, **overrides):
    return hs.report_result(w.conn, settings=w.settings, attempt_id=attempt, evidence=evidence(**overrides), now=at)


def intents(w):
    return [tuple(r) for r in w.conn.execute("SELECT source, state FROM submission_intents ORDER BY seq")]


def status(w, at=NOW + 20 * SEC):
    return hs.submission_status(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                                now=at)


def test_success_confirms_the_intent_and_writes_submission_result(filled_world):
    auth, attempt = dispatched(filled_world)
    out = report(filled_world, attempt, success_observed=True)
    assert out["state"] == "CONFIRMED_SUCCESS" and out["result_hash"].startswith("sha256:")
    assert attempt_state(filled_world.conn, attempt) == "CONFIRMED_SUCCESS"
    assert intents(filled_world) == [("HUMAN_AUTHORIZED", "CONFIRMED")]
    stored = sp.get_submission_result(filled_world.conn, attempt)["result"]
    authorization = sp.get_authorization(filled_world.conn, auth["authorization_id"])
    assert stored["schema"] == "submission-result" and stored["review_hash"] == authorization["review_hash"]
    assert stored["authorization_id"] == auth["authorization_id"] and stored["state"] == "CONFIRMED_SUCCESS"
    assert stored["non_claims"]["employer_accepted"] is False
    assert [e["event"] for e in sp.submit_events(filled_world.conn, auth["authorization_id"])][-1] == "RESULT_REPORTED"
    assert status(filled_world)["status"] == "SUBMITTED"


def test_success_records_applied_in_the_tracker_when_the_pack_is_the_confirmed_one(filled_world, monkeypatch):
    from webapp.services import autonomy_context
    real = filled_world.conn.execute("SELECT id FROM artifacts WHERE workspace_id = ? AND artifact_type = "
                                     "'application_pack' ORDER BY rowid DESC LIMIT 1", (filled_world.ws,)).fetchone()[0]
    monkeypatch.setattr(autonomy_context, "pack_readiness", lambda c, **kw: (real, True))
    auth, attempt = dispatched(filled_world)
    report(filled_world, attempt, success_observed=True)
    ws = filled_world.conn.execute("SELECT workflow_status FROM workspaces WHERE id = ?", (filled_world.ws,)).fetchone()
    assert ws[0] == "applied"
    row = filled_world.conn.execute("SELECT source, state, workflow_event_id FROM submission_intents").fetchall()
    assert len(row) == 1 and row[0]["source"] == "HUMAN_AUTHORIZED" and row[0]["workflow_event_id"]
    last = filled_world.conn.execute("SELECT evidence_json FROM submission_attempt_events WHERE attempt_id = ? "
                                     "ORDER BY seq DESC LIMIT 1", (attempt,)).fetchone()[0]
    assert '"workflow_applied":true' in last.replace(" ", "")


def test_success_is_recorded_even_when_the_tracker_cannot_mark_applied(filled_world):
    _, attempt = dispatched(filled_world)  # the fixture's pack id is a placeholder: the tracker refuses
    assert report(filled_world, attempt, success_observed=True)["state"] == "CONFIRMED_SUCCESS"
    last = filled_world.conn.execute("SELECT evidence_json FROM submission_attempt_events WHERE attempt_id = ? "
                                     "ORDER BY seq DESC LIMIT 1", (attempt,)).fetchone()[0]
    assert '"workflow_applied":false' in last.replace(" ", "")
    assert intents(filled_world) == [("HUMAN_AUTHORIZED", "CONFIRMED")]


def test_an_employer_validation_error_is_a_proven_failure_and_releases_the_intent(filled_world):
    _, attempt = dispatched(filled_world)
    out = report(filled_world, attempt, failure_observed=True)
    assert (out["state"], out["proven_not_submitted"], out["reason"]) == \
        ("SUBMISSION_FAILED", True, "EMPLOYER_VALIDATION_ERROR")
    assert intents(filled_world) == [("HUMAN_AUTHORIZED", "RELEASED")]
    assert status(filled_world)["status"] == "SUBMISSION_FAILED"


def test_nothing_observed_is_ambiguous_and_the_user_resolves_it_both_ways(filled_world):
    _, attempt = dispatched(filled_world)
    assert report(filled_world, attempt)["state"] == "SUBMISSION_AMBIGUOUS"
    assert intents(filled_world) == [("HUMAN_AUTHORIZED", "CLAIMED")]  # still blocks duplicates
    assert status(filled_world)["status"] == "SUBMISSION_UNCLEAR"
    assert hs.resolve(filled_world.conn, account_id=V2_ACCOUNT, attempt_id=attempt, submitted=True, actor="u",
                      now=NOW + 30 * SEC) == "CONFIRMED_SUCCESS"
    assert intents(filled_world) == [("HUMAN_AUTHORIZED", "CONFIRMED")]
    with pytest.raises(hs.SubmitRefused, match="not_ambiguous"):
        hs.resolve(filled_world.conn, account_id=V2_ACCOUNT, attempt_id=attempt, submitted=False, actor="u",
                   now=NOW + 31 * SEC)


def test_resolving_not_submitted_releases_the_intent(filled_world):
    _, attempt = dispatched(filled_world)
    report(filled_world, attempt)
    assert hs.resolve(filled_world.conn, account_id=V2_ACCOUNT, attempt_id=attempt, submitted=False, actor="u",
                      now=NOW + 30 * SEC) == "SUBMISSION_FAILED"
    assert intents(filled_world) == [("HUMAN_AUTHORIZED", "RELEASED")]


def test_resolve_by_another_account_is_not_found(filled_world):
    _, attempt = dispatched(filled_world)
    report(filled_world, attempt)
    with pytest.raises(hs.SubmitRefused, match="not_found"):
        hs.resolve(filled_world.conn, account_id="other", attempt_id=attempt, submitted=True, actor="u", now=NOW)


def test_a_content_change_is_ambiguous_even_with_success(filled_world):
    auth, attempt = dispatched(filled_world)
    hs.record_submit_event(filled_world.conn, attempt_id=attempt, event="CONTENT_CHANGED_DURING_ATTEMPT",
                           detail={}, now=NOW + 5 * SEC)
    out = report(filled_world, attempt, success_observed=True)  # the extension's flag is ORed with the events
    assert (out["state"], out["reason"]) == ("SUBMISSION_AMBIGUOUS", "CONTENT_CHANGED")


def test_a_second_report_is_refused(filled_world):
    _, attempt = dispatched(filled_world)
    report(filled_world, attempt, success_observed=True)
    with pytest.raises(hs.SubmitRefused, match="result_already_recorded"):
        report(filled_world, attempt, failure_observed=True)


def test_events_use_the_closed_vocabulary(filled_world):
    _, attempt = dispatched(filled_world)
    with pytest.raises(hs.SubmitRefused, match="unknown_event"):
        hs.record_submit_event(filled_world.conn, attempt_id=attempt, event="SUBMITTED", detail={}, now=NOW)


def test_the_sweep_marks_a_silent_dispatch_ambiguous_and_expires_an_unclicked_attempt(filled_world):
    _, attempt = dispatched(filled_world)
    assert mark_stale_dispatches_ambiguous(filled_world.conn, now=NOW + DISPATCH_RESULT_TIMEOUT + 3 * SEC,
                                           result_timeout=DISPATCH_RESULT_TIMEOUT) == 1
    assert attempt_state(filled_world.conn, attempt) == "SUBMISSION_AMBIGUOUS"


def test_the_sweep_expires_an_authorized_attempt_after_the_dispatch_ttl(filled_world):
    attempt = pre_click(filled_world, authorized(filled_world))["attempt_id"]
    assert expire_unclicked(filled_world.conn, now=NOW + 62 * SEC) == 1
    assert attempt_state(filled_world.conn, attempt) == "EXPIRED_UNCLICKED"
    assert intents(filled_world) == [("HUMAN_AUTHORIZED", "RELEASED")]


def test_the_scheduler_dispatch_timeout_outlasts_the_challenge_window(filled_world):
    from product.submit_constants import CHALLENGE_HANDOFF_WINDOW
    assert filled_world.settings.autonomy_dispatch_result_timeout >= DISPATCH_RESULT_TIMEOUT.total_seconds()
    assert DISPATCH_RESULT_TIMEOUT > CHALLENGE_HANDOFF_WINDOW


def test_submission_status_through_the_lifecycle(filled_world):
    assert status(filled_world, at=NOW)["status"] == "SUBMIT_READY"
    auth = authorize(filled_world, ready(filled_world))
    assert status(filled_world, at=NOW + SEC)["status"] == "SUBMITTING"
    attempt = pre_click(filled_world, {**auth, "review": sp.get_authorization(filled_world.conn,
                                                                            auth["authorization_id"])["review"]},
                        at=NOW + SEC)["attempt_id"]
    hs.human_record_click_dispatched(filled_world.conn, settings=filled_world.settings, attempt_id=attempt,
                                     now=NOW + 2 * SEC)
    hs.record_submit_event(filled_world.conn, attempt_id=attempt, event="CHALLENGE_DETECTED", detail={},
                           now=NOW + 3 * SEC)
    assert status(filled_world, at=NOW + 4 * SEC)["status"] == "CHALLENGE_WAITING"
    hs.record_submit_event(filled_world.conn, attempt_id=attempt, event="CHALLENGE_CLEARED", detail={},
                           now=NOW + 5 * SEC)
    assert status(filled_world, at=NOW + 6 * SEC)["status"] == "SUBMITTING"


def test_no_filled_run_is_not_ready(grant_world):  # noqa: F811
    assert hs.submission_status(grant_world.conn, settings=grant_world.settings, account_id=V2_ACCOUNT,
                                application_workspace_id=grant_world.ws, now=NOW)["status"] == "NOT_READY"


def test_the_stored_result_has_no_cleartext(filled_world):
    _, attempt = dispatched(filled_world)
    report(filled_world, attempt, success_observed=True)
    raw = filled_world.conn.execute("SELECT result_json FROM submission_results").fetchone()[0]
    assert "1 month" not in raw and "ada@example.com" not in raw


def test_matched_rule_feedback_is_never_proof_that_nothing_left(filled_world):
    """Browser finding (6E-A Task 16): Chrome drops a tab's matched-rule record
    when a main-frame form POST does not commit a navigation (e.g. a 204), so
    an empty match list does not prove the submit request stayed in the
    browser. Such an outcome is SUBMISSION_AMBIGUOUS, never a proven failure."""
    _, attempt = dispatched(filled_world)
    out = report(filled_world, attempt, matched_rule_ids=[], matched_rules_available=True)
    assert (out["state"], out["proven_not_submitted"]) == ("SUBMISSION_AMBIGUOUS", False)


def test_no_click_after_the_egress_opened_is_not_proven_by_matched_rules(filled_world):
    _, attempt = dispatched(filled_world)
    out = report(filled_world, attempt, click_performed=False, matched_rule_ids=[], matched_rules_available=True,
                 cause="SUBMIT_CONTROL_MISSING")
    assert (out["state"], out["proven_not_submitted"]) == ("SUBMISSION_AMBIGUOUS", False)


def test_any_tracker_failure_never_discards_the_submission_result(filled_world, monkeypatch):
    """User condition: submission result persistence is authoritative even if
    the tracker update fails -- for ANY failure, not only its precondition
    ValueErrors."""
    from webapp.services import human_submit
    def broken(*args, **kwargs):
        raise RuntimeError("tracker down")
    monkeypatch.setattr(human_submit, "record_status_change", broken)
    _, attempt = dispatched(filled_world)
    assert report(filled_world, attempt, success_observed=True)["state"] == "CONFIRMED_SUCCESS"
    from webapp.persistence import submit as sp_
    assert sp_.get_submission_result(filled_world.conn, attempt)["result"]["state"] == "CONFIRMED_SUCCESS"
    last = filled_world.conn.execute("SELECT evidence_json FROM submission_attempt_events WHERE attempt_id = ? "
                                     "ORDER BY seq DESC LIMIT 1", (attempt,)).fetchone()[0]
    assert '"workflow_applied":false' in last.replace(" ", "") and "tracker down" in last
    assert intents(filled_world) == [("HUMAN_AUTHORIZED", "CONFIRMED")]


def _kinds(w):
    return [r[0] for r in w.conn.execute("SELECT kind FROM notifications ORDER BY created_at, id")]


def test_results_and_challenges_notify(filled_world):
    """Bundle 7 §17.2 producers: confirmed / ambiguous / failed and the challenge handoff (in-app only)."""
    _, attempt = dispatched(filled_world)
    hs.record_submit_event(filled_world.conn, attempt_id=attempt, event="CHALLENGE_DETECTED", detail={},
                           now=NOW + 3 * SEC)
    assert "submit.challenge_handoff" in _kinds(filled_world)
    report(filled_world, attempt)
    assert "submit.ambiguous" in _kinds(filled_world)
    templates = [r[0] for r in filled_world.conn.execute("SELECT template_id FROM outbound_messages")]
    assert templates.count("notify.immediate") <= 1  # the challenge never emailed


@pytest.mark.parametrize("overrides,kind", [({"success_observed": True}, "submit.confirmed"),
                                            ({"failure_observed": True}, "submit.failed")])
def test_a_decided_result_notifies_its_outcome(filled_world, overrides, kind):
    _, attempt = dispatched(filled_world)
    assert kind not in _kinds(filled_world)
    report(filled_world, attempt, **overrides)
    assert kind in _kinds(filled_world)
