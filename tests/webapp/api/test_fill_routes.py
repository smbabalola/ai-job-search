"""6D-B routes (spec §20): a full run through the extension API, ownership
(404 + zero-row diff), the read-only state GET, stale confirmation, closed
bodies, and the intent response as the only body carrying cleartext."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.webapp.services.fill_fixtures import (  # noqa: F401
    NOW, TARGET, V2_ACCOUNT, fill_world, grant_world, observation_doc, v2_chain,
)
from tests.webapp.services.review_fixtures import diff_counts, table_counts
from webapp.app import create_app
from webapp.persistence import fill as f

SENTINELS = ("ada@example.com", "1 month")
RULESET = "sha256:" + "5" * 64


def _session(conn, account_id, ws):
    from webapp.persistence.handoff import create_handoff_session, create_session_token
    pack = conn.execute("SELECT id FROM artifacts WHERE artifact_type = 'application_pack' ORDER BY rowid DESC "
                        "LIMIT 1").fetchone()[0]
    session = create_handoff_session(conn, account_id=account_id, workspace_id=ws, pack_artifact_id=pack,
                                     target_url=TARGET, target_domain="jobs.example.test", ats_adapter_id="greenhouse",
                                     ats_adapter_version="greenhouse@2")
    return session["id"], create_session_token(conn, handoff_session_id=session["id"])


class Ext:
    def __init__(self, client, sid, token):
        self.client, self.sid, self.headers, self.responses = client, sid, {"X-Handoff-Session-Token": token}, []

    def call(self, method, path, body=None, expect=200, keep=True):
        r = getattr(self.client, method)(f"/api/handoff/sessions/{self.sid}/fill{path}", headers=self.headers,
                                         **({"json": body} if body is not None else {}))
        assert r.status_code == expect, (path, r.status_code, r.text)
        if keep:
            self.responses.append((path, r.text))
        return r.json()


@pytest.fixture
def api(grant_world):
    with TestClient(create_app(grant_world.settings)) as client:
        sid, token = _session(grant_world.conn, V2_ACCOUNT, grant_world.ws)
        yield Ext(client, sid, token), client, grant_world


def _plan(w):
    return f.get_plan_by_hash(w.conn, w.plan_hash)["plan"]


def _page_after(w, done):
    doc = observation_doc()
    for i, action in enumerate(_plan(w)["actions"][:done]):
        if action["rendered_value_hash"]:
            doc["elements"][i]["value_state"] = {"state": "NONBLANK",
                                                 "current_value_hash": action["rendered_value_hash"]}
    return doc


def _to_filling(ext):
    run = ext.call("post", "/runs", {"executor_instance_id": "ex_1", "browser_session_id": "b1",
                                    "execution_tab_id": 7}, expect=201)
    rid = run["id"]
    assert ext.call("post", f"/runs/{rid}/observations", {"phase": "INITIAL", "observation": observation_doc()}
                    )["state"] == "REVALIDATING"
    ext.call("post", f"/runs/{rid}/quarantine", {"phase": "PRELOAD_INSTALLED", "ruleset_hash": "sha256:" + "1" * 64})
    ext.call("post", f"/runs/{rid}/quarantine", {"phase": "RELOADED"})
    assert ext.call("post", f"/runs/{rid}/observations", {"phase": "REVALIDATION", "observation": observation_doc()}
                    )["matched"] is True
    assert ext.call("post", f"/runs/{rid}/quarantine", {"phase": "TOTAL_VERIFIED", "ruleset_hash": RULESET}
                    )["state"] == "QUARANTINE_ACTIVE"
    assert ext.call("post", f"/runs/{rid}/grant")["granted"] is True
    return rid


def test_a_full_run_through_the_extension_api_and_only_intents_carry_cleartext(api):
    ext, client, w = api
    rid = _to_filling(ext)
    binding = f.get_grant_binding(w.conn, rid)
    intents = []
    for i, action in enumerate(_plan(w)["actions"]):
        precheck = {"ruleset_hash": RULESET, "structure_fingerprint": binding["structure_fingerprint"],
                    "field_fingerprint": action["field_fingerprint"], "siblings_contained": True}
        env = ext.call("post", f"/runs/{rid}/actions/{i}/intent", {"precheck": precheck}, keep=False)
        intents.append(json.dumps(env))
        envelope = env["envelope"]
        outcome = {"WRITE": "WRITTEN_VERIFIED", "ATTACH_LOCAL": "ATTACH_LOCAL_VERIFIED",
                   "IGNORE_NON_APPLICATION": "IGNORE_RECORDED"}[action["action_kind"]]
        ext.call("post", f"/runs/{rid}/actions/{i}/outcome", {
            "envelope_id": envelope["envelope_id"], "outcome": outcome, "readback_hash": action["rendered_value_hash"],
            "post_observation": _page_after(w, i + 1)})
        ext.call("post", f"/runs/{rid}/heartbeat")
    assert ext.call("post", f"/runs/{rid}/final", {"observation": _page_after(w, 5)})["state"] == \
        "FILLED_AWAITING_SUBMISSION"
    ext.call("get", f"/runs/{rid}/plan-status")
    ext.call("post", f"/runs/{rid}/detections", {"kind": "POST_FILL_CHANGE_OBSERVED", "detail": {}})
    assert "ada@example.com" in intents[0] and "1 month" in intents[1]
    app_bodies = [client.get(f"/api/workspaces/{w.ws}/fill-plan/state").text,
                  client.get(f"/api/workspaces/{w.ws}/fill-runs").text,
                  client.get(f"/api/workspaces/{w.ws}/fill-runs/{rid}").text]
    for path, text in ext.responses + [("app", t) for t in app_bodies]:
        for sentinel in SENTINELS:
            assert sentinel not in text, f"{sentinel!r} in the {path} response"
    detail = client.get(f"/api/workspaces/{w.ws}/fill-runs/{rid}").json()
    assert detail["result"]["terminal_state"] == "FILLED_AWAITING_SUBMISSION"


def test_the_state_get_is_read_only_and_withholds_rendered_values(api):
    _, client, w = api
    before = table_counts(w.conn)
    body = client.get(f"/api/workspaces/{w.ws}/fill-plan/state").json()
    assert diff_counts(before, table_counts(w.conn)) == {}
    assert body["plan"]["displayed_plan_hash"] == w.plan_hash and body["plan"]["confirmed"] is True
    assert all("rendered_value" not in row for row in body["plan"]["rows"])


def test_the_confirm_route_refuses_a_stale_hash(api):
    _, client, w = api
    before = table_counts(w.conn)
    r = client.post(f"/api/workspaces/{w.ws}/fill-plan/confirm",
                    json={"observation_id": w.observation["id"], "displayed_plan_hash": "sha256:" + "0" * 64})
    assert (r.status_code, r.json()["detail"]) == (409, "stale_plan")
    assert diff_counts(before, table_counts(w.conn)) == {}


def test_bodies_forbid_extra_keys(api):
    ext, client, w = api
    ext.call("post", "/runs", {"executor_instance_id": "e", "browser_session_id": "b", "execution_tab_id": 1,
                               "grant": True}, expect=422)
    r = client.post(f"/api/workspaces/{w.ws}/fill-plan/confirm",
                    json={"observation_id": "x", "displayed_plan_hash": "y", "force": True})
    assert r.status_code == 422


def test_a_refusal_is_409_with_the_reason(api):
    ext, _, _ = api
    ext.call("post", "/runs", {"executor_instance_id": "e", "browser_session_id": "b", "execution_tab_id": 1},
             expect=201)
    out = ext.call("post", "/runs", {"executor_instance_id": "e", "browser_session_id": "b", "execution_tab_id": 2},
                   expect=409)
    assert out["detail"] == "run_active"


def test_an_invalid_observation_is_422(api):
    ext, _, _ = api
    rid = ext.call("post", "/runs", {"executor_instance_id": "e", "browser_session_id": "b", "execution_tab_id": 1},
                   expect=201)["id"]
    ext.call("post", f"/runs/{rid}/observations", {"phase": "INITIAL", "observation": {"x": 1}}, expect=422)


# ---- ownership -----------------------------------------------------------------------------

@pytest.fixture
def foreign(api):
    """Another account's workspace and handoff session; our run exists."""
    from webapp.persistence.accounts import create_account
    from webapp.persistence.workspaces import create_workspace
    ext, client, w = api
    rid = ext.call("post", "/runs", {"executor_instance_id": "e", "browser_session_id": "b", "execution_tab_id": 1},
                   expect=201)["id"]
    create_account(w.conn, account_id="account_other", display_name="Other")
    other_ws = create_workspace(w.conn, company="Other Co", title="Engineer", account_id="account_other")["id"]
    sid, token = _session(w.conn, "account_other", other_ws)
    return client, w, rid, other_ws, Ext(client, sid, token), ext


EXT_ROUTES = [
    ("post", "/runs/{rid}/observations", {"phase": "INITIAL", "observation": {}}),
    ("get", "/runs/{rid}/plan-status", None),
    ("post", "/runs/{rid}/quarantine", {"phase": "LOST"}),
    ("post", "/runs/{rid}/grant", None),
    ("post", "/runs/{rid}/actions/0/intent", {"precheck": {}}),
    ("post", "/runs/{rid}/actions/0/outcome", {"outcome": "WRITTEN_VERIFIED", "post_observation": {}}),
    ("post", "/runs/{rid}/actions/0/unknown", None),
    ("post", "/runs/{rid}/heartbeat", None),
    ("post", "/runs/{rid}/detections", {"kind": "SUBMIT_ATTEMPT_OBSERVED"}),
    ("post", "/runs/{rid}/final", {"observation": {}}),
]


@pytest.mark.parametrize("method,path,body", EXT_ROUTES)
def test_a_foreign_session_cannot_touch_our_run(foreign, method, path, body):
    client, w, rid, _, other, ours = foreign
    before = table_counts(w.conn)
    other.call(method, path.format(rid=rid), body, expect=404, keep=False)  # its own session path, our run
    r = getattr(client, method)(f"/api/handoff/sessions/{ours.sid}/fill{path.format(rid=rid)}",
                                headers=other.headers, **({"json": body} if body is not None else {}))
    assert r.status_code == 404  # our session path with the foreign token
    assert diff_counts(before, table_counts(w.conn)) == {}


def test_a_foreign_token_cannot_start_a_run_in_our_session(foreign):
    client, w, _, _, other, ours = foreign
    before = table_counts(w.conn)
    r = client.post(f"/api/handoff/sessions/{ours.sid}/fill/runs", headers=other.headers,
                    json={"executor_instance_id": "e", "browser_session_id": "b", "execution_tab_id": 9})
    assert r.status_code == 404 and diff_counts(before, table_counts(w.conn)) == {}


APP_ROUTES = [
    ("get", "/fill-plan/state", None),
    ("post", "/fill-plan/mappings", {"observation_id": "x", "page_field_key": "k", "choice": "NEW_QUESTION"}),
    ("post", "/fill-plan/confirm", {"observation_id": "x", "displayed_plan_hash": "y"}),
    ("post", "/review/deltas/d1/classification/confirm", {"displayed_proposal_id": "p", "subject": "s"}),
    ("get", "/fill-runs", None),
    ("get", "/fill-runs/{rid}", None),
]


@pytest.mark.parametrize("method,path,body", APP_ROUTES)
def test_another_accounts_workspace_is_404_and_unchanged(foreign, method, path, body):
    client, w, rid, other_ws, _, _ = foreign
    before = table_counts(w.conn)
    r = getattr(client, method)(f"/api/workspaces/{other_ws}{path.format(rid=rid)}",
                                **({"json": body} if body is not None else {}))
    assert r.status_code == 404, (path, r.status_code, r.text)
    assert diff_counts(before, table_counts(w.conn)) == {}


def test_our_run_is_not_visible_under_another_workspace(foreign):
    client, w, rid, _, _, _ = foreign
    from webapp.persistence.workspaces import create_workspace
    mine = create_workspace(w.conn, company="Mine", title="Role", account_id=V2_ACCOUNT)["id"]
    assert client.get(f"/api/workspaces/{mine}/fill-runs/{rid}").status_code == 404
