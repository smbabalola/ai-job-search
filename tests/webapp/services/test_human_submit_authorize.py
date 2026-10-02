"""6E-A spec §8.1/§8.3: the one human "Submit application" act. It is bound
to the displayed review hash, creates exactly one authorization per fill
run and one ISSUED SUBMIT grant (120 s) through the human entry point; the
autonomous SUBMIT entry points stay closed."""
from __future__ import annotations

import dataclasses
import threading

import pytest

from product.autonomy_contract import SUBMIT_GRANT_TTL, Capability, parse_utc
from tests.webapp.services.submit_fixtures import (  # noqa: F401
    NOW, SEC, V2_ACCOUNT, filled_world, grant_world, review_observation, v2_chain,
)
from webapp.persistence import submit as sp
from webapp.persistence.autonomy_ledger import claim_intent, get_grant, workspace_identity
from webapp.persistence.db import connect
from webapp.services import human_submit as hs
from webapp.services import submit_review as sr
from webapp.services.autonomy import SubmissionNotAvailable, request_grant
from webapp.services.autonomy_controls import engage_kill_switch


def ready(w, at=NOW):
    sr.request_reobservation(w.conn, account_id=V2_ACCOUNT, application_workspace_id=w.ws, now=at)
    sr.record_submit_observation(w.conn, settings=w.settings, run_id=w.run["id"], phase="REVIEW", attempt_id=None,
                                 observation=review_observation(w), now=at)
    out = sr.current_review(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws, now=at)
    assert out["state"] == "READY", out
    return out["review_hash"]


def authorize(w, review_hash, *, conn=None, at=NOW):
    return hs.authorize(conn or w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                        review_hash=review_hash, actor="u", now=at)


def test_authorize_creates_one_authorization_and_one_issued_submit_grant(filled_world):
    h = ready(filled_world)
    out = authorize(filled_world, h)
    grant = get_grant(filled_world.conn, out["grant_id"])
    assert grant["stage"] == "SUBMIT" and grant["status"] == "ISSUED"
    assert parse_utc(grant["expires_at"]) - parse_utc(grant["issued_at"]) == SUBMIT_GRANT_TTL
    assert grant["binding"]["authority"] == "HUMAN_SUBMIT"
    assert grant["binding"]["review_hash"] == h
    assert grant["binding"]["human_authorization_id"] == out["authorization_id"]
    auth = sp.authorization_for_run(filled_world.conn, filled_world.run["id"])
    assert auth["id"] == out["authorization_id"] and auth["review_hash"] == h and auth["grant_id"] == grant["id"]
    assert auth["review"]["schema"] == "submission-review"


def test_a_second_authorize_on_the_same_run_is_refused(filled_world):  # Review Focus 1
    h = ready(filled_world)
    authorize(filled_world, h)
    with pytest.raises(hs.SubmitRefused, match="already_authorized"):
        authorize(filled_world, h)


def test_two_concurrent_authorizes_create_exactly_one(filled_world):  # Review Focus 1
    h = ready(filled_world)
    outcomes = []

    def go():
        c = connect(filled_world.settings.db_path)
        try:
            outcomes.append(authorize(filled_world, h, conn=c))
        except hs.SubmitRefused as refusal:
            outcomes.append(refusal.reason)
        finally:
            c.close()

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(o if isinstance(o, str) else "ok" for o in outcomes) == ["already_authorized", "ok"]
    assert filled_world.conn.execute("SELECT COUNT(*) FROM human_submit_authorizations").fetchone()[0] == 1
    assert filled_world.conn.execute("SELECT COUNT(*) FROM autonomy_grants WHERE stage = 'SUBMIT'").fetchone()[0] == 1


def test_a_hash_that_is_not_the_current_review_is_stale(filled_world):  # Review Focus 2
    ready(filled_world)
    with pytest.raises(hs.SubmitRefused, match="stale_review"):
        authorize(filled_world, "sha256:" + "0" * 64)
    assert sp.authorization_for_run(filled_world.conn, filled_world.run["id"]) is None


def test_the_kill_switch_refuses_authorization(filled_world):
    h = ready(filled_world)
    engage_kill_switch(filled_world.conn, account_id=V2_ACCOUNT, actor="u", reason="stop", now=NOW)
    # the control epoch is part of the review: the old hash is stale; the fresh one is denied by the kill switch
    fresh = sr.current_review(filled_world.conn, settings=filled_world.settings, account_id=V2_ACCOUNT,
                              application_workspace_id=filled_world.ws, now=NOW)
    assert fresh["review_hash"] != h and fresh["reasons"] == ["kill_switch"]
    with pytest.raises(hs.SubmitRefused, match="kill_switch"):
        authorize(filled_world, fresh["review_hash"])


def test_a_job_already_applied_elsewhere_is_a_duplicate(filled_world):  # Review Focus 4
    from webapp.persistence.workspaces import create_workspace
    key, _, _ = workspace_identity(filled_world.conn, filled_world.ws)
    other = create_workspace(filled_world.conn, company="Acme", title="Engineer", account_id=V2_ACCOUNT)["id"]
    claim_intent(filled_world.conn, account_id=V2_ACCOUNT, job_identity_key=key, application_workspace_id=other,
                 source="HUMAN_APPLIED", state="CONFIRMED", now=NOW)
    filled_world.conn.commit()
    sr.request_reobservation(filled_world.conn, account_id=V2_ACCOUNT, application_workspace_id=filled_world.ws,
                             now=NOW)
    sr.record_submit_observation(filled_world.conn, settings=filled_world.settings, run_id=filled_world.run["id"],
                                 phase="REVIEW", attempt_id=None, observation=review_observation(filled_world), now=NOW)
    out = sr.current_review(filled_world.conn, settings=filled_world.settings, account_id=V2_ACCOUNT,
                            application_workspace_id=filled_world.ws, now=NOW)
    assert out["state"] == "BLOCKED" and out["reasons"] == ["duplicate"]
    with pytest.raises(hs.SubmitRefused, match="duplicate"):
        authorize(filled_world, out["review_hash"])


def test_the_deployment_switch_off_refuses_with_human_submit_disabled(filled_world):
    h = ready(filled_world)
    filled_world.settings = dataclasses.replace(filled_world.settings, human_submit_enabled=False)
    with pytest.raises(hs.SubmitRefused, match="human_submit_disabled"):
        authorize(filled_world, h)


def test_cancel_before_pre_click_revokes_the_grant_and_records_the_event(filled_world):
    out = authorize(filled_world, ready(filled_world))
    hs.cancel_authorization(filled_world.conn, account_id=V2_ACCOUNT, authorization_id=out["authorization_id"],
                            actor="u", now=NOW + SEC)
    grant = get_grant(filled_world.conn, out["grant_id"])
    assert grant["status"] == "REVOKED" and grant["revoked_reason"] == "user_cancelled"
    assert [e["event"] for e in sp.submit_events(filled_world.conn, out["authorization_id"])] == \
        ["CANCELLED_BEFORE_DISPATCH"]
    with pytest.raises(hs.SubmitRefused, match="not_cancellable"):
        hs.cancel_authorization(filled_world.conn, account_id=V2_ACCOUNT, authorization_id=out["authorization_id"],
                                actor="u", now=NOW + 2 * SEC)


def test_cancel_by_another_account_is_not_found(filled_world):
    out = authorize(filled_world, ready(filled_world))
    with pytest.raises(hs.SubmitRefused, match="not_found"):
        hs.cancel_authorization(filled_world.conn, account_id="someone_else", authorization_id=out["authorization_id"],
                                actor="u", now=NOW)


def test_the_autonomous_submit_grant_stays_closed(filled_world):
    with pytest.raises(SubmissionNotAvailable):
        request_grant(filled_world.conn, settings=filled_world.settings, account_id=V2_ACCOUNT,
                      application_workspace_id=filled_world.ws, stage=Capability.SUBMIT, now=NOW, fill_manifest={})
