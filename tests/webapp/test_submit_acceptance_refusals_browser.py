"""Bundle 6E-A browser acceptance, part 2 (spec §22 items 6-12): the
challenge handoff (completed by the person, and interrupted by a person's
edit), and every refusal before the click -- a challenge already showing, a
page changed after review or after authorization, cancel before dispatch,
and the kill switch. None of the refusals ever lets the submission leave."""
from __future__ import annotations

import time

from tests.webapp.fixtures.fill.submit_harness import (  # noqa: F401
    SUBMIT_POST, hook_build, recorder, submit_harness, third_party,
)
from tests.webapp.services.fill_fixtures import v2_chain  # noqa: F401
from tests.webapp.services.review_fixtures import V2_ACCOUNT
from webapp.persistence import submit as sp


def test_a_challenge_after_the_click_is_completed_by_the_person_then_submitted(submit_harness):
    h = submit_harness("challenge")
    page, _ = h.filled()
    h.authorize(h.ready())
    assert h.wait_status({"CHALLENGE_WAITING"})["status"] == "CHALLENGE_WAITING"
    assert h.submit_posts() == []  # nothing left yet: the page is waiting for the person
    page.click("#challenge_done")  # the person completes the check (a trusted click)
    status = h.wait_status({"SUBMITTED", "SUBMISSION_UNCLEAR", "SUBMISSION_FAILED"})
    auth_id = sp.authorizations_for_application(h.w.conn, h.w.ws)[0]["id"]
    trail = [(e["event"], e["detail"]) for e in sp.submit_events(h.w.conn, auth_id)]
    assert status["status"] == "SUBMITTED", (status, trail, h.recorder.requests)
    assert h.submit_posts() == [SUBMIT_POST]
    events = [e["event"] for e in sp.submit_events(h.w.conn, sp.authorizations_for_application(h.w.conn, h.w.ws)[0]["id"])]
    assert "CHALLENGE_DETECTED" in events and "CHALLENGE_CLEARED" in events
    assert not any(e == "CONTENT_CHANGED_DURING_ATTEMPT" for e in events)


def test_a_person_editing_during_the_challenge_closes_the_egress_immediately(submit_harness):
    h = submit_harness("challenge")
    page, _ = h.filled()
    h.authorize(h.ready())
    h.wait_status({"CHALLENGE_WAITING"})
    page.fill("#notice", "3 months")  # a trusted edit of an application field
    deadline = time.monotonic() + 20
    auth_id = sp.authorizations_for_application(h.w.conn, h.w.ws)[0]["id"]
    while time.monotonic() < deadline and "CONTENT_CHANGED_DURING_ATTEMPT" not in \
            [e["event"] for e in sp.submit_events(h.w.conn, auth_id)]:
        time.sleep(0.25)
    page.click("#challenge_done")  # completing the check now cannot send: TOTAL is back
    status = h.wait_status({"SUBMITTED", "SUBMISSION_UNCLEAR", "SUBMISSION_FAILED"})
    assert status["status"] == "SUBMISSION_UNCLEAR"
    assert h.submit_posts() == []
    [attempt] = h.attempts()
    assert sp.get_submission_result(h.w.conn, attempt["id"])["result"]["reason"] == "CONTENT_CHANGED"


def test_a_challenge_already_showing_refuses_before_any_click(submit_harness):
    h = submit_harness("challenge_before")
    h.filled()
    h.authorize(h.ready())
    events, grant = h.wait_authorization_used()
    assert events == ["CHALLENGE_BEFORE_SUBMIT"] and grant == "REVOKED"
    assert h.attempts() == [] and h.submit_posts() == []


def test_a_page_changed_after_the_review_refuses_authorization(submit_harness):
    h = submit_harness("happy")
    page, _ = h.filled()
    review_hash = h.ready()
    page.fill("#notice", "2 months")
    h.request_review()  # the next look at the page sees the change
    state = h.wait_review()
    assert state["review"]["state"] == "UNAVAILABLE"
    r = h.http.post(f"/api/workspaces/{h.w.ws}/submit/authorize", json={"review_hash": review_hash})
    assert r.status_code == 409 and r.json()["detail"] in ("stale_review", "page_changed_since_fill")
    assert h.attempts() == [] and h.submit_posts() == []


def test_a_page_changed_after_authorization_refuses_the_pre_click(submit_harness):
    h = submit_harness("happy")
    page, _ = h.filled()
    h.authorize(h.ready())
    page.fill("#notice", "2 months")  # before the heartbeat delivers the authorization
    events, grant = h.wait_authorization_used()
    assert events == ["PRE_CLICK_REFUSED"] and grant == "REVOKED"
    assert h.attempts() == [] and h.submit_posts() == []


def test_cancel_before_dispatch_sends_nothing(submit_harness):
    h = submit_harness("happy")
    h.filled()
    auth = h.authorize(h.ready())
    r = h.http.post(f"/api/workspaces/{h.w.ws}/submit/cancel", json={"authorization_id": auth["authorization_id"]})
    assert r.status_code == 200 and r.json()["cancelled"] is True
    time.sleep(12)  # one heartbeat later: the revoked grant is never delivered
    assert h.attempts() == [] and h.submit_posts() == []
    events = [e["event"] for e in sp.submit_events(h.w.conn, auth["authorization_id"])]
    assert events == ["CANCELLED_BEFORE_DISPATCH"]


def test_the_kill_switch_after_authorization_refuses_the_pre_click(submit_harness):
    from datetime import datetime, timezone
    from webapp.services.autonomy_controls import engage_kill_switch
    h = submit_harness("happy")
    h.filled()
    auth = h.authorize(h.ready())
    # Engaging the kill switch revokes every issued grant at once (6B), the
    # human SUBMIT grant included: the extension is never even offered it.
    engage_kill_switch(h.w.conn, account_id=V2_ACCOUNT, actor="u", reason="stop", now=datetime.now(timezone.utc))
    events, grant = h.wait_authorization_used()
    assert grant == "REVOKED" and events in ([], ["PRE_CLICK_REFUSED"])
    reason = h.w.conn.execute("SELECT revoked_reason FROM autonomy_grants WHERE id = ?",
                              (auth["grant_id"],)).fetchone()[0]
    assert reason in ("kill_switch", "pre_click:kill_switch", "pre_click:review_changed")
    time.sleep(12)  # a heartbeat later: still nothing
    assert h.attempts() == [] and h.submit_posts() == []
