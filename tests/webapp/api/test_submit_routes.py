"""6E-A routes (spec §18): the extension submit routes under the handoff
session token, the app submit routes under the account, heartbeat
directives, ownership (404), domain refusals (409 + reason) and closed
bodies (422)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.webapp.api.test_fill_routes import Ext, _session
from tests.webapp.services.submit_fixtures import (  # noqa: F401
    NOW, SEC, V2_ACCOUNT, grant_world, review_observation, v2_chain, widen_loopback,
)
from tests.webapp.services.test_fill_actions import page_after, run_all
from tests.webapp.services.test_fill_runs import observe, quarantine
from webapp.app import create_app
from webapp.persistence import submit as sp


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def api(grant_world, monkeypatch):  # noqa: F811
    import dataclasses
    from webapp.api import fill_app, fill_extension, submit_app, submit_extension
    from webapp.services import fill_actions as fa
    from webapp.services import fill_runs as fr
    widen_loopback(monkeypatch)
    grant_world.settings = dataclasses.replace(grant_world.settings, human_submit_enabled=True,
                                               submit_fixture_origins_enabled=True)
    clock = Clock()
    for module in (fill_extension, fill_app, submit_extension, submit_app):
        monkeypatch.setattr(module, "_now", clock)
    with TestClient(create_app(grant_world.settings)) as client:
        sid, token = _session(grant_world.conn, V2_ACCOUNT, grant_world.ws)
        run = fr.start_run(grant_world.conn, settings=grant_world.settings, account_id=V2_ACCOUNT,
                           handoff_session_id=sid, application_workspace_id=grant_world.ws,
                           executor_instance_id="ex_1", browser_session_id="b1", execution_tab_id=1, now=NOW)
        assert observe(grant_world, run, "INITIAL")["state"] == "REVALIDATING"
        assert observe(grant_world, run, "REVALIDATION")["matched"] is True
        quarantine(grant_world, run, "PRELOAD_INSTALLED")
        quarantine(grant_world, run, "RELOADED")
        quarantine(grant_world, run)
        assert fr.request_fill_grant(grant_world.conn, settings=grant_world.settings, run_id=run["id"], now=NOW).granted
        run_all(grant_world, run)
        fa.final_validate(grant_world.conn, run_id=run["id"], observation=page_after(grant_world, 5), now=NOW)
        grant_world.run, grant_world.clock = run, clock
        yield Ext(client, sid, token), client, grant_world


def app_call(client, method, w, path, body=None, expect=200):
    r = getattr(client, method)(f"/api/workspaces/{w.ws}/submit{path}", **({"json": body} if body is not None else {}))
    assert r.status_code == expect, (path, r.status_code, r.text)
    return r.json()


def to_ready(ext, client, w):
    """The app asks for a re-observation; the heartbeat carries it; the
    extension answers with a REVIEW observation; the state becomes READY."""
    from webapp.services import submit_review as sr
    sr.request_reobservation(w.conn, account_id=V2_ACCOUNT, application_workspace_id=w.ws, now=NOW)
    beat = ext.call("post", f"/runs/{w.run['id']}/heartbeat")
    assert beat["reobserve"] is not None and beat["authorization"] is None
    ext.call("post", f"/runs/{w.run['id']}/submit/observations",
             {"phase": "REVIEW", "observation": review_observation(w)})
    assert ext.call("post", f"/runs/{w.run['id']}/heartbeat")["reobserve"] is None
    state = app_call(client, "get", w, "/state")
    assert state["review"]["state"] == "READY" and state["status"]["status"] == "SUBMIT_READY"
    return state["review"]["review_hash"]


def to_dispatched(ext, client, w):
    h = to_ready(ext, client, w)
    auth = app_call(client, "post", w, "/authorize", {"review_hash": h}, expect=201)
    directive = ext.call("post", f"/runs/{w.run['id']}/heartbeat")["authorization"]
    assert directive["grant_id"] == auth["grant_id"] and directive["review_hash"] == h
    assert directive["certification_id"] == "greenhouse@2/submit@1"
    assert [e["id"] for e in directive["egress"]] == ["E1_SUBMIT", "E2_CONFIRM", "E3_RENDER", "C1_RECAPTCHA"]
    expected = directive["expected"]
    verification = {"executor_instance_id": "ex_1", "browser_session_id": "b1", "execution_tab_id": 1,
                    "challenge_visible": False, **expected}
    w.clock.now = NOW + SEC
    attempt = ext.call("post", f"/runs/{w.run['id']}/submit/pre-click",
                       {"grant_id": auth["grant_id"], "observation": review_observation(w),
                        "verification": verification})["attempt_id"]
    assert ext.call("post", f"/runs/{w.run['id']}/heartbeat")["authorization"] is None  # consumed
    w.clock.now = NOW + 2 * SEC
    assert ext.call("post", f"/runs/{w.run['id']}/submit/{attempt}/dispatch")["dispatched"] is True
    return auth, attempt


def test_the_whole_human_submit_flow_through_the_routes(api):
    ext, client, w = api
    auth, attempt = to_dispatched(ext, client, w)
    for event in ("EGRESS_INSTALLED", "CLICK_PERFORMED", "SIGNAL_OBSERVED", "TOTAL_RESTORED"):
        ext.call("post", f"/runs/{w.run['id']}/submit/{attempt}/events", {"event": event, "detail": {}})
    w.clock.now = NOW + 5 * SEC
    out = ext.call("post", f"/runs/{w.run['id']}/submit/{attempt}/result", {"evidence": {
        "click_performed": True, "egress_ever_installed": True, "total_restored_verified": True,
        "success_observed": True, "failure_observed": False, "content_changed": False,
        "matched_rule_ids": [9201, 9202], "matched_rules_available": True, "cause": None}})
    assert out["state"] == "CONFIRMED_SUCCESS"
    assert app_call(client, "get", w, "/state")["status"]["status"] == "SUBMITTED"


def test_authorize_with_a_stale_hash_is_409_and_a_second_authorize_is_409(api):
    ext, client, w = api
    h = to_ready(ext, client, w)
    assert client.post(f"/api/workspaces/{w.ws}/submit/authorize",
                       json={"review_hash": "sha256:" + "0" * 64}).json()["detail"] == "stale_review"
    app_call(client, "post", w, "/authorize", {"review_hash": h}, expect=201)
    r = client.post(f"/api/workspaces/{w.ws}/submit/authorize", json={"review_hash": h})
    assert (r.status_code, r.json()["detail"]) == (409, "already_authorized")


def test_extra_body_keys_are_422(api):
    ext, client, w = api
    app_call(client, "post", w, "/authorize", {"review_hash": "x", "force": True}, expect=422)
    ext.call("post", f"/runs/{w.run['id']}/submit/observations",
             {"phase": "REVIEW", "observation": review_observation(w), "extra": 1}, expect=422)


def test_an_invalid_observation_is_422(api):
    ext, client, w = api
    ext.call("post", f"/runs/{w.run['id']}/submit/observations", {"phase": "REVIEW", "observation": {"x": 1}},
             expect=422)


def test_a_foreign_workspace_is_404(api):
    ext, client, w = api
    app_call(client, "get", type("W", (), {"ws": "ws_nope"})(), "/state", expect=404)


def test_a_foreign_run_and_attempt_are_404(api):
    ext, client, w = api
    ext.call("post", "/runs/fr_nope/submit/observations", {"phase": "REVIEW", "observation": review_observation(w)},
             expect=404)
    auth, attempt = to_dispatched(ext, client, w)
    ext.call("post", f"/runs/{w.run['id']}/submit/att_nope/dispatch", expect=404)


def test_cancel_through_the_app_before_the_pre_click(api):
    ext, client, w = api
    auth = app_call(client, "post", w, "/authorize", {"review_hash": to_ready(ext, client, w)}, expect=201)
    out = app_call(client, "post", w, "/cancel", {"authorization_id": auth["authorization_id"]})
    assert out["cancelled"] is True
    assert ext.call("post", f"/runs/{w.run['id']}/heartbeat")["authorization"] is None


def test_cancel_through_the_extension_after_pre_click_and_refused_after_dispatch(api):
    ext, client, w = api
    auth, attempt = to_dispatched(ext, client, w)
    r = ext.call("post", f"/runs/{w.run['id']}/submit/{attempt}/cancel", expect=409)
    assert r["detail"] == "already_dispatched"


def test_an_ambiguous_result_is_resolved_through_the_app(api):
    ext, client, w = api
    auth, attempt = to_dispatched(ext, client, w)
    ext.call("post", f"/runs/{w.run['id']}/submit/{attempt}/result", {"evidence": {
        "click_performed": True, "egress_ever_installed": True, "total_restored_verified": True,
        "success_observed": False, "failure_observed": False, "content_changed": False,
        "matched_rule_ids": [9201], "matched_rules_available": True, "cause": None}})
    assert app_call(client, "get", w, "/state")["status"]["status"] == "SUBMISSION_UNCLEAR"
    out = app_call(client, "post", w, f"/attempts/{attempt}/resolve", {"submitted": False})
    assert out["state"] == "SUBMISSION_FAILED"
    app_call(client, "post", w, "/attempts/att_nope/resolve", {"submitted": True}, expect=404)


def test_unknown_event_is_409(api):
    ext, client, w = api
    auth, attempt = to_dispatched(ext, client, w)
    assert ext.call("post", f"/runs/{w.run['id']}/submit/{attempt}/events", {"event": "NOPE", "detail": {}},
                    expect=409)["detail"] == "unknown_event"


def test_the_heartbeat_offers_an_authorization_only_while_its_grant_is_issued(api):
    ext, client, w = api
    h = to_ready(ext, client, w)
    app_call(client, "post", w, "/authorize", {"review_hash": h}, expect=201)
    assert ext.call("post", f"/runs/{w.run['id']}/heartbeat")["authorization"] is not None
    for seconds in (40, 80, 119):  # the fill lease stays alive; only the 120 s grant runs out
        w.clock.now = NOW + timedelta(seconds=seconds)
        assert ext.call("post", f"/runs/{w.run['id']}/heartbeat")["authorization"] is not None
    w.clock.now = NOW + timedelta(seconds=121)
    beat = ext.call("post", f"/runs/{w.run['id']}/heartbeat")
    assert beat["lease_expired"] is False and beat["authorization"] is None


def test_no_route_body_carries_cleartext(api):
    ext, client, w = api
    to_dispatched(ext, client, w)
    for path, text in ext.responses:
        assert "ada@example.com" not in text and "1 month" not in text, path
    assert "1 month" not in client.get(f"/api/workspaces/{w.ws}/submit/state").text
