"""6E-A spec §16.1/§16.2: the Submit Review page (one snapshot, one
"Submit application" act carrying the displayed review hash), progress and
ambiguity resolution, and the submission status on the Prepared list and in
the dossier. Cleartext answers appear on the Submit Review page only."""
from __future__ import annotations

import html
import re

import pytest

from tests.webapp.api.test_submit_routes import (  # noqa: F401
    Clock, api, app_call, to_dispatched, to_ready,
)
from tests.webapp.services.submit_fixtures import NOW, grant_world, v2_chain  # noqa: F401
from tests.webapp.test_submit_structure import SUBMIT_ROUTES_6E_A

SENTINELS = ("ada@example.com", "1 month")


@pytest.fixture
def ui(api, monkeypatch):
    from webapp.api import review_pages
    ext, client, w = api
    monkeypatch.setattr(review_pages, "_now", w.clock)
    return ext, client, w


def page(client, w, query=""):
    r = client.get(f"/workspaces/{w.ws}/submit{query}")
    assert r.status_code == 200, r.text
    return html.unescape(r.text)


def test_first_load_requests_a_fresh_look_and_waits_for_it(ui):
    ext, client, w = ui
    html = page(client, w)
    assert "data-submit-state=\"PENDING_OBSERVATION\"" in html and "Checking the employer page" in html
    assert f"/api/workspaces/{w.ws}/submit/state" in html  # it polls
    assert ext.call("post", f"/runs/{w.run['id']}/heartbeat")["reobserve"] is not None


def test_ready_shows_every_bound_item_and_one_submit_act_carrying_the_hash(ui):
    ext, client, w = ui
    h = to_ready(ext, client, w)
    html = page(client, w, "?observed=1")
    assert 'data-submit-state="READY"' in html
    for text in ("greenhouse@2/submit@1", "FIXTURE_CERTIFIED", "https://jobs.example.test/acme/123", h,
                 "What is your notice period?", "1 month", "ada@example.com"):
        assert text in html, text
    for key in ("plan_hash", "fill_result_hash", "observation_fingerprint", "approval_binding_hash",
                "control_epoch", "sha256"):
        assert f'data-bound="{key}"' in html, key
    buttons = re.findall(r'<button[^>]*data-submit-action="([^"]+)"[^>]*>([^<]*)</button>', html)
    assert buttons == [("authorize", "Submit application")]
    assert f'data-review-hash="{h}"' in html
    assert "disabled" not in re.search(r'<button[^>]*data-submit-action="authorize"[^>]*>', html).group(0)


def test_blocked_lists_plain_reasons_and_disables_the_act(ui):
    import dataclasses
    ext, client, w = ui
    to_ready(ext, client, w)
    client.app.state.settings = dataclasses.replace(client.app.state.settings, human_submit_enabled=False)
    html = page(client, w, "?observed=1")
    assert 'data-submit-state="BLOCKED"' in html
    assert "Submission is turned off for this deployment." in html
    assert re.search(r'<button[^>]*data-submit-action="authorize"[^>]*disabled', html)


def test_unavailable_explains_the_reason(ui):
    ext, client, w = ui
    html = page(client, w, "?observed=1")  # no REVIEW observation yet
    assert 'data-submit-state="UNAVAILABLE"' in html
    assert "Couldn't read the employer tab" in html
    assert not re.search(r'data-submit-action="authorize"', html)


def test_in_progress_and_ambiguous_states_render_progress_and_the_two_resolve_buttons(ui):
    ext, client, w = ui
    auth, attempt = to_dispatched(ext, client, w)
    html = page(client, w, "?observed=1")
    assert 'data-submission-status="SUBMITTING"' in html and "Submitting" in html
    ext.call("post", f"/runs/{w.run['id']}/submit/{attempt}/result", {"evidence": {
        "click_performed": True, "egress_ever_installed": True, "total_restored_verified": True,
        "success_observed": False, "failure_observed": False, "content_changed": False,
        "matched_rule_ids": [9201], "matched_rules_available": True, "cause": None}})
    html = page(client, w, "?observed=1")
    assert 'data-submission-status="SUBMISSION_UNCLEAR"' in html
    buttons = re.findall(r'data-submit-action="(resolve-[a-z-]+)"', html)
    assert buttons == ["resolve-submitted", "resolve-not-submitted"]
    assert f'data-attempt="{attempt}"' in html


def test_prepared_list_and_dossier_show_the_status_without_cleartext(ui):
    ext, client, w = ui
    auth, attempt = to_dispatched(ext, client, w)
    ext.call("post", f"/runs/{w.run['id']}/submit/{attempt}/result", {"evidence": {
        "click_performed": True, "egress_ever_installed": True, "total_restored_verified": True,
        "success_observed": True, "failure_observed": False, "content_changed": False,
        "matched_rule_ids": [9201], "matched_rules_available": True, "cause": None}})
    prepared = client.get("/applications/prepared").text
    assert 'data-submission-status="SUBMITTED"' in prepared
    dossier = client.get(f"/workspaces/{w.ws}/autonomy").text
    assert 'data-dossier-section="submission"' in dossier and f'data-submit-attempt="{attempt}"' in dossier
    assert "CONFIRMED_SUCCESS" in dossier
    for text in SENTINELS:
        assert text not in prepared and text not in dossier


def test_the_submit_route_inventory_is_exact(ui):
    ext, client, w = ui
    registered = {r.path for r in client.app.routes if "submit" in getattr(r, "path", "").lower()}
    assert registered - {"/api/handoff/sessions/{session_id}/confirm-submission"} == SUBMIT_ROUTES_6E_A


def test_an_unknown_workspace_is_404(ui):
    ext, client, w = ui
    assert client.get("/workspaces/ws_nope/submit").status_code == 404
