"""6E-A spec §7 and §16.1: the Submit Review service — re-observation
requests, REVIEW intake, snapshot assembly (preconditions §7.2) and the
human-gate readiness that enables the one "Submit application" act."""
from __future__ import annotations

import dataclasses

import pytest

from tests.webapp.services.submit_fixtures import (  # noqa: F401
    NOW, SEC, V2_ACCOUNT, filled_world, grant_world, review_observation, v2_chain,
)
from webapp.persistence import submit as sp
from webapp.services import submit_review as sr


def reobserve(w, doc=None, *, at=NOW):
    sr.request_reobservation(w.conn, account_id=V2_ACCOUNT, application_workspace_id=w.ws, now=at)
    return sr.record_submit_observation(w.conn, settings=w.settings, run_id=w.run["id"], phase="REVIEW",
                                        attempt_id=None, observation=doc or review_observation(w), now=at)


def review(w, *, at=NOW):
    return sr.current_review(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws, now=at)


def test_a_request_is_pending_until_a_review_observation_arrives(filled_world):
    req = sr.request_reobservation(filled_world.conn, account_id=V2_ACCOUNT,
                                   application_workspace_id=filled_world.ws, now=NOW)
    assert sr.pending_reobservation(filled_world.conn, filled_world.run["id"])["id"] == req["id"]
    assert review(filled_world)["state"] == "PENDING_OBSERVATION"
    sr.record_submit_observation(filled_world.conn, settings=filled_world.settings, run_id=filled_world.run["id"],
                                 phase="REVIEW", attempt_id=None, observation=review_observation(filled_world),
                                 now=NOW + SEC)
    assert sr.pending_reobservation(filled_world.conn, filled_world.run["id"]) is None


def test_an_unchanged_page_is_ready_and_a_fresh_identical_observation_reproduces_the_hash(filled_world):
    reobserve(filled_world)
    first = review(filled_world)
    assert first["state"] == "READY" and first["reasons"] == [] and first["review_hash"].startswith("sha256:")
    snap = first["snapshot"]
    assert snap["fill"]["fill_run_id"] == filled_world.run["id"]
    assert snap["certification"] == {"certification_id": "greenhouse@2/submit@1", "status": "FIXTURE_CERTIFIED"}
    assert snap["context"] == {"executor_instance_id": "ex_1", "browser_session_id": "b1", "execution_tab_id": 1}
    assert len(snap["answers"]) == 2 and len(snap["documents"]) == 2
    reobserve(filled_world, at=NOW + 5 * SEC)
    assert review(filled_world, at=NOW + 5 * SEC)["review_hash"] == first["review_hash"]  # E18


def _changed(w):
    doc = review_observation(w)
    for e in doc["elements"]:
        if e["page_field_key"] == "gh:notice":
            e["value_state"] = {"state": "NONBLANK", "current_value_hash": "sha256:" + "9" * 64}
    return doc


def test_a_field_edited_after_the_fill_makes_the_review_unavailable(filled_world):  # Review Focus 2
    reobserve(filled_world, _changed(filled_world))
    out = review(filled_world)
    assert out["state"] == "UNAVAILABLE" and out["reasons"] == ["page_changed_since_fill"]
    assert out["review_hash"] is None


def test_a_review_observation_older_than_the_max_age_is_stale_while_the_lease_is_live(filled_world):
    from webapp.services import fill_runs as fr
    reobserve(filled_world, at=NOW)
    assert review(filled_world, at=NOW + 30 * SEC)["state"] == "READY"
    fr.heartbeat(filled_world.conn, run_id=filled_world.run["id"], now=NOW + 40 * SEC)
    assert review(filled_world, at=NOW + 61 * SEC)["reasons"] == ["observation_stale"]


def test_two_submit_controls_are_refused(filled_world):
    doc = review_observation(filled_world)
    doc["submit_controls"].append({"control_fingerprint": "sha256:" + "b" * 64})
    reobserve(filled_world, doc)
    assert review(filled_world)["reasons"] == ["submit_control_not_unique"]


def test_a_post_fill_change_detection_blocks_the_review(filled_world):
    from webapp.services import fill_actions as fa
    fa.record_detection(filled_world.conn, run_id=filled_world.run["id"], kind="POST_FILL_CHANGE_OBSERVED",
                        detail={}, now=NOW)
    reobserve(filled_world)
    assert review(filled_world)["reasons"] == ["post_fill_change"]


def test_an_expired_lease_blocks_the_review(filled_world):
    reobserve(filled_world, at=NOW)
    assert review(filled_world, at=NOW + 46 * SEC)["reasons"] == ["lease_expired"]


def test_the_deployment_switch_off_blocks_with_human_submit_disabled(filled_world):
    reobserve(filled_world)
    filled_world.settings = dataclasses.replace(filled_world.settings, human_submit_enabled=False)
    out = review(filled_world)
    assert out["state"] == "BLOCKED" and "human_submit_disabled" in out["reasons"]
    assert out["review_hash"] is not None  # the snapshot is shown; only the act is disabled


def test_a_fixture_certified_adapter_on_a_real_origin_is_not_live_certified(filled_world, monkeypatch):
    from product import submit_certification
    import re
    monkeypatch.setattr(submit_certification, "_LOOPBACK", re.compile(r"^http://127\.0\.0\.1(:\d{1,5})?$"))
    reobserve(filled_world)
    assert review(filled_world)["reasons"] == ["adapter_not_live_certified"]


def test_no_filled_run_means_nothing_to_review(grant_world):  # noqa: F811
    assert sr.request_reobservation(grant_world.conn, account_id=V2_ACCOUNT, application_workspace_id=grant_world.ws,
                                    now=NOW) is None
    assert sr.current_review(grant_world.conn, settings=grant_world.settings, account_id=V2_ACCOUNT,
                             application_workspace_id=grant_world.ws, now=NOW)["reasons"] == ["no_filled_run"]


@pytest.mark.sqlite_only  # turns SQLite foreign keys off to insert a placeholder grant
def test_review_observations_are_refused_once_the_run_is_authorized(filled_world):
    reobserve(filled_world)
    filled_world.conn.execute("PRAGMA foreign_keys = OFF")
    sp.insert_authorization(filled_world.conn, account_id=V2_ACCOUNT, application_workspace_id=filled_world.ws,
                            fill_run_id=filled_world.run["id"], review_hash="h", review={}, grant_id="g", actor="u",
                            now=NOW)
    filled_world.conn.commit()
    filled_world.conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sr.SubmitRefused, match="already_authorized"):
        reobserve(filled_world)
    assert review(filled_world)["reasons"] == ["already_authorized"]


def test_the_stored_review_observation_has_no_cleartext(filled_world):
    reobserve(filled_world)
    row = filled_world.conn.execute("SELECT observation_json FROM submit_observations").fetchone()[0]
    assert "1 month" not in row and "ada@example.com" not in row
