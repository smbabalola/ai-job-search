"""Bundle 6E-A browser acceptance, part 1 (spec §22 items 1-5, 13, 14, 16):
the human-authorized submission end to end in real Chrome -- the Submit
Review page, the one "Submit application" act, the extension's proofs,
durable dispatch, the certified egress over TOTAL, the one click, the
server-determined result -- plus containment during the egress window and
the live-certification and deployment gates."""
from __future__ import annotations

import dataclasses

from tests.webapp.fixtures.fill.submit_harness import (  # noqa: F401
    APP, SUBMIT_POST, hook_build, recorder, submit_harness, third_party,
)
from tests.webapp.services.fill_fixtures import v2_chain  # noqa: F401
from webapp.persistence import submit as sp

RESULT = {"click_performed": True}


def rules(h):
    return sorted(h.worker.evaluate("async () => (await chrome.declarativeNetRequest.getSessionRules()).map(r => r.id)"))


def test_happy_path_through_the_submit_review_page_with_a_double_click(submit_harness):
    h = submit_harness("happy")
    page, tab = h.filled()
    review = h.ctx.new_page()
    review.goto(f"{APP}/workspaces/{h.w.ws}/submit")
    review.wait_for_selector('[data-submit-state="READY"]', timeout=45_000)
    assert "1 month" in review.content() and "greenhouse@2/submit@1" in review.content()
    review.dblclick('[data-submit-action="authorize"]')
    review.wait_for_selector('[data-submission-status="SUBMITTED"]', timeout=120_000)
    assert len(sp.authorizations_for_application(h.w.conn, h.w.ws)) == 1  # the double click made one
    assert h.submit_posts() == [SUBMIT_POST]
    assert ("GET", "confirm:acme/123") in h.recorder.requests
    assert page.url.endswith("/acme/jobs/123/confirmation")
    assert rules(h) == [9111, 9121]  # TOTAL restored: no allow rule left
    assert h.recorder.probes() == {"submit:acme/123", "confirm:acme/123"}
    [attempt] = h.attempts()
    result = sp.get_submission_result(h.w.conn, attempt["id"])["result"]
    assert result["state"] == "CONFIRMED_SUCCESS" and 9201 in result["matched_rule_ids"]
    events = [e["event"] for e in sp.submit_events(h.w.conn, sp.authorizations_for_application(h.w.conn, h.w.ws)[0]["id"])]
    assert events[:2] == ["EGRESS_INSTALLED", "CLICK_PERFORMED"] and "TOTAL_RESTORED" in events
    # a second authorization of the same filled run is refused (duplicate prevention)
    h.request_review()
    state = h.state()
    assert state["review"]["review_hash"] is None
    h.authorize("sha256:" + "0" * 64, expect=409)


def test_in_page_xhr_success(submit_harness):
    h = submit_harness("xhr")
    page, _ = h.filled()
    h.authorize(h.ready())
    assert h.wait_status({"SUBMITTED", "SUBMISSION_UNCLEAR", "SUBMISSION_FAILED"})["status"] == "SUBMITTED"
    assert page.query_selector("#application_confirmation") is not None
    assert h.submit_posts() == [SUBMIT_POST] and rules(h) == [9111, 9121]


def test_an_employer_validation_error_is_a_proven_failure(submit_harness):
    h = submit_harness("validation_error")
    page, _ = h.filled()
    h.authorize(h.ready())
    status = h.wait_status({"SUBMITTED", "SUBMISSION_UNCLEAR", "SUBMISSION_FAILED"})
    assert status["status"] == "SUBMISSION_FAILED"
    [attempt] = h.attempts()
    result = sp.get_submission_result(h.w.conn, attempt["id"])["result"]
    assert (result["proven_not_submitted"], result["reason"]) == (True, "EMPLOYER_VALIDATION_ERROR")
    released = h.w.conn.execute("SELECT state FROM submission_intents").fetchall()
    assert [r[0] for r in released] == ["RELEASED"]


def test_a_client_side_block_sends_nothing_and_stays_unclear_without_proof(submit_harness):
    # Nothing left the browser (the recording server proves it), but the
    # extension cannot prove that: matched-rule feedback is not reliable
    # evidence of absence, so the honest outcome is SUBMISSION_AMBIGUOUS.
    h = submit_harness("client_block")
    h.filled()
    h.authorize(h.ready())
    status = h.wait_status({"SUBMITTED", "SUBMISSION_UNCLEAR", "SUBMISSION_FAILED"}, timeout=150)
    assert status["status"] == "SUBMISSION_UNCLEAR"
    [attempt] = h.attempts()
    result = sp.get_submission_result(h.w.conn, attempt["id"])["result"]
    assert (result["proven_not_submitted"], result["matched_rules_available"]) == (False, False)
    assert h.submit_posts() == []


def test_no_signal_is_ambiguous_and_the_user_resolves_it(submit_harness):
    h = submit_harness("no_signal")
    h.filled()
    h.authorize(h.ready())
    status = h.wait_status({"SUBMITTED", "SUBMISSION_UNCLEAR", "SUBMISSION_FAILED"}, timeout=150)
    [attempt] = h.attempts()
    result = sp.get_submission_result(h.w.conn, attempt["id"])
    r = result["result"] if result else {}
    assert status["status"] == "SUBMISSION_UNCLEAR", (r.get("reason"), r.get("matched_rule_ids"), r.get("matched_rules_available"), h.recorder.requests)
    assert h.submit_posts() == [SUBMIT_POST]  # it left; nothing proved the outcome
    r = h.http.post(f"/api/workspaces/{h.w.ws}/submit/attempts/{status['attempt_id']}/resolve",
                    json={"submitted": False})
    assert r.status_code == 200 and r.json()["state"] == "SUBMISSION_FAILED"
    assert h.state()["status"]["status"] == "SUBMISSION_FAILED"


def test_nothing_else_leaves_while_the_submit_egress_is_open(submit_harness):
    h = submit_harness("egress_exfil")
    h.filled()
    h.authorize(h.ready())
    assert h.wait_status({"SUBMITTED", "SUBMISSION_UNCLEAR", "SUBMISSION_FAILED"})["status"] == "SUBMITTED"
    assert h.submit_posts() == [SUBMIT_POST]
    probes = h.recorder.probes()
    assert "exfil_control.png" in probes  # the probes really ran while the egress was open
    assert not {"exfil_same_origin_post", "exfil_same_origin_fetch"} & probes, probes
    from tests.webapp.fixtures.fill.submit_harness import _ThirdParty
    assert _ThirdParty.hits == []  # fetch, beacon and image to the third party never arrived
    assert ("connect", "exfil_submit") not in h.recorder.ws_events


def test_the_deployment_switch_and_the_live_certification_gate(submit_harness):
    h = submit_harness("happy")
    h.filled()
    review_hash = h.ready()
    settings = h.app.state.settings
    h.app.state.settings = dataclasses.replace(settings, human_submit_enabled=False)
    state = h.state()
    assert state["review"]["state"] == "BLOCKED" and "human_submit_disabled" in state["review"]["reasons"]
    assert h.authorize(review_hash, expect=409)["detail"] == "human_submit_disabled"
    h.app.state.settings = dataclasses.replace(settings, submit_fixture_origins_enabled=False)
    state = h.state()
    assert state["review"]["reasons"] == ["adapter_not_live_certified"]
    assert h.authorize(review_hash, expect=409)["detail"] == "adapter_not_live_certified"
    assert h.attempts() == [] and h.submit_posts() == []
