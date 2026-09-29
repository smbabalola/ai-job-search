"""6E-A spec §8.3/§10: the human pre-click commit (the exact review, the
live context and the certified control proved again from a fresh
PRE_SUBMIT observation), the halt-checked CLICK_DISPATCHED acknowledgement,
and cancel before dispatch. Refusals revoke the grant and never click."""
from __future__ import annotations

import pytest

from tests.webapp.services.submit_fixtures import (  # noqa: F401
    NOW, SEC, V2_ACCOUNT, filled_world, grant_world, review_observation, v2_chain,
)
from tests.webapp.services.test_human_submit_authorize import authorize, ready
from webapp.persistence import submit as sp
from webapp.persistence.autonomy_ledger import attempt_state, get_grant
from webapp.services import human_submit as hs
from webapp.services.autonomy import SubmissionNotAvailable, pre_click_commit
from webapp.services.autonomy_controls import engage_kill_switch


def authorized(w, at=NOW):
    out = authorize(w, ready(w, at), at=at)
    out["review"] = sp.get_authorization(w.conn, out["authorization_id"])["review"]
    return out


def verification(w, auth, **overrides):
    snap = auth["review"]
    out = {"executor_instance_id": "ex_1", "browser_session_id": "b1", "execution_tab_id": 1,
           "canonical_url": snap["target"]["canonical_url"],
           "observation_fingerprint": snap["observation"]["observation_fingerprint"],
           "submit_control_fingerprint": snap["submit_control"]["control_fingerprint"],
           "ruleset_hash": snap["fill"]["ruleset_hash_total"], "challenge_visible": False}
    out.update(overrides)
    return out


def pre_click(w, auth, *, doc=None, at=NOW + SEC, **overrides):
    return hs.human_pre_click_commit(w.conn, settings=w.settings, run_id=w.run["id"], grant_id=auth["grant_id"],
                                     observation=doc or review_observation(w), verification=verification(w, auth,
                                                                                                        **overrides),
                                     now=at)


def intents(w):
    return [tuple(r) for r in w.conn.execute("SELECT source, state FROM submission_intents ORDER BY seq")]


def test_a_matching_pre_click_authorizes_one_attempt_under_human_authority(filled_world):
    auth = authorized(filled_world)
    out = pre_click(filled_world, auth)
    assert attempt_state(filled_world.conn, out["attempt_id"]) == "AUTHORIZED"
    assert get_grant(filled_world.conn, auth["grant_id"])["status"] == "CONSUMED"
    assert intents(filled_world) == [("HUMAN_AUTHORIZED", "CLAIMED")]
    assert filled_world.conn.execute("SELECT COUNT(*) FROM limit_reservations WHERE grant_id = ?",
                                     (auth["grant_id"],)).fetchone()[0] == 0  # no autonomous caps
    assert sp.latest_submit_observation(filled_world.conn, filled_world.run["id"], "PRE_SUBMIT") is not None


@pytest.mark.parametrize("key,value,reason", [
    ("execution_tab_id", 2, "context_mismatch"),
    ("browser_session_id", "b2", "context_mismatch"),
    ("executor_instance_id", "ex_2", "context_mismatch"),
    ("canonical_url", "https://jobs.example.test/other", "verification_mismatch"),
    ("observation_fingerprint", "sha256:" + "0" * 64, "verification_mismatch"),
    ("submit_control_fingerprint", "sha256:" + "0" * 64, "verification_mismatch"),
    ("ruleset_hash", "sha256:" + "0" * 64, "verification_mismatch"),
])
def test_each_proof_mismatch_refuses_and_revokes_the_grant(filled_world, key, value, reason):
    auth = authorized(filled_world)
    with pytest.raises(hs.SubmitRefused, match=reason):
        pre_click(filled_world, auth, **{key: value})
    grant = get_grant(filled_world.conn, auth["grant_id"])
    assert grant["status"] == "REVOKED" and grant["revoked_reason"] == f"pre_click:{reason}"
    assert [e["event"] for e in sp.submit_events(filled_world.conn, auth["authorization_id"])] == ["PRE_CLICK_REFUSED"]
    assert intents(filled_world) == []


def test_a_page_changed_after_authorize_is_refused(filled_world):
    auth = authorized(filled_world)
    doc = review_observation(filled_world)
    for e in doc["elements"]:
        if e["page_field_key"] == "gh:notice":
            e["value_state"] = {"state": "NONBLANK", "current_value_hash": "sha256:" + "9" * 64}
    with pytest.raises(hs.SubmitRefused, match="page_changed_since_fill"):
        pre_click(filled_world, auth, doc=doc)
    assert get_grant(filled_world.conn, auth["grant_id"])["status"] == "REVOKED"


def test_a_challenge_visible_before_the_click_is_refused(filled_world):
    auth = authorized(filled_world)
    with pytest.raises(hs.SubmitRefused, match="challenge_before_submit"):
        pre_click(filled_world, auth, challenge_visible=True)
    events = [e["event"] for e in sp.submit_events(filled_world.conn, auth["authorization_id"])]
    assert events == ["CHALLENGE_BEFORE_SUBMIT"]
    assert get_grant(filled_world.conn, auth["grant_id"])["revoked_reason"] == "pre_click:challenge_before_submit"


def test_the_kill_switch_after_authorize_refuses_the_pre_click(filled_world):
    auth = authorized(filled_world)
    engage_kill_switch(filled_world.conn, account_id=V2_ACCOUNT, actor="u", reason="stop", now=NOW)
    with pytest.raises(hs.SubmitRefused):
        pre_click(filled_world, auth)
    assert intents(filled_world) == []


def test_a_consumed_grant_cannot_be_pre_clicked_twice(filled_world):
    auth = authorized(filled_world)
    pre_click(filled_world, auth)
    with pytest.raises(hs.SubmitRefused, match="grant_not_consumable"):
        pre_click(filled_world, auth, at=NOW + 2 * SEC)


def test_dispatch_acknowledges_once(filled_world):
    attempt = pre_click(filled_world, authorized(filled_world))["attempt_id"]
    assert hs.human_record_click_dispatched(filled_world.conn, settings=filled_world.settings, attempt_id=attempt,
                                            now=NOW + 2 * SEC) is True
    assert attempt_state(filled_world.conn, attempt) == "CLICK_DISPATCHED"
    assert hs.human_record_click_dispatched(filled_world.conn, settings=filled_world.settings, attempt_id=attempt,
                                            now=NOW + 3 * SEC) is False


def test_dispatch_after_the_kill_switch_expires_the_attempt_and_releases_the_intent(filled_world):
    attempt = pre_click(filled_world, authorized(filled_world))["attempt_id"]
    engage_kill_switch(filled_world.conn, account_id=V2_ACCOUNT, actor="u", reason="stop", now=NOW + SEC)
    assert hs.human_record_click_dispatched(filled_world.conn, settings=filled_world.settings, attempt_id=attempt,
                                            now=NOW + 2 * SEC) is False
    assert attempt_state(filled_world.conn, attempt) == "EXPIRED_UNCLICKED"
    last = filled_world.conn.execute("SELECT source, evidence_json FROM submission_attempt_events "
                                     "WHERE attempt_id = ? ORDER BY seq DESC LIMIT 1", (attempt,)).fetchone()
    assert last["source"] == "SERVER" and '"halted":"kill_switch"' in last["evidence_json"].replace(" ", "")
    assert intents(filled_world) == [("HUMAN_AUTHORIZED", "RELEASED")]


def test_dispatch_after_the_ttl_is_refused(filled_world):
    attempt = pre_click(filled_world, authorized(filled_world))["attempt_id"]
    assert hs.human_record_click_dispatched(filled_world.conn, settings=filled_world.settings, attempt_id=attempt,
                                            now=NOW + 62 * SEC) is False
    assert attempt_state(filled_world.conn, attempt) == "EXPIRED_UNCLICKED"


def test_cancel_before_dispatch_expires_the_attempt_as_user_and_releases(filled_world):
    auth = authorized(filled_world)
    attempt = pre_click(filled_world, auth)["attempt_id"]
    hs.cancel_authorization(filled_world.conn, account_id=V2_ACCOUNT, authorization_id=auth["authorization_id"],
                            actor="u", now=NOW + 2 * SEC)
    assert attempt_state(filled_world.conn, attempt) == "EXPIRED_UNCLICKED"
    assert intents(filled_world) == [("HUMAN_AUTHORIZED", "RELEASED")]
    source = filled_world.conn.execute("SELECT source FROM submission_attempt_events WHERE attempt_id = ? "
                                       "ORDER BY seq DESC LIMIT 1", (attempt,)).fetchone()[0]
    assert source == "USER"


def test_cancel_after_dispatch_is_refused(filled_world):
    auth = authorized(filled_world)
    attempt = pre_click(filled_world, auth)["attempt_id"]
    hs.human_record_click_dispatched(filled_world.conn, settings=filled_world.settings, attempt_id=attempt,
                                     now=NOW + 2 * SEC)
    with pytest.raises(hs.SubmitRefused, match="already_dispatched"):
        hs.cancel_attempt(filled_world.conn, account_id=V2_ACCOUNT, attempt_id=attempt, actor="u", now=NOW + 3 * SEC)


def test_the_autonomous_pre_click_stays_closed(filled_world):
    with pytest.raises(SubmissionNotAvailable):
        pre_click_commit(filled_world.conn, settings=filled_world.settings, grant_id="any", verification={}, now=NOW)
