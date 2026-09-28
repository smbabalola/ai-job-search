"""6D-B fill-plan services (spec §8.4-8.5, §16.1): proposal against the
effective 6D-A approval, 6D-A delta intake for new content, compatible-only
mapping choices, the one-snapshot presentation (cleartext only there) and the
stale-view-refusing confirmation."""
from __future__ import annotations

import json

import pytest

from product.fill_hash import fill_value_hash
from tests.product.fill_observation_fixtures import element
from tests.webapp.services.fill_fixtures import (  # noqa: F401
    NOW, V2_ACCOUNT, fill_world, observation_doc, store_observation, v2_chain,
)
from webapp.persistence import fill as f
from webapp.persistence import review_approval as ra
from webapp.services import fill_plans as fp
from webapp.services.review_application import ReviewRefused


def _propose(w, obs=None):
    return fp.propose_plan(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                           observation_id=(obs or w.observation)["id"], now=NOW)


def _presentation(w, obs=None):
    return fp.fill_plan_presentation(w.conn, settings=w.settings, account_id=V2_ACCOUNT,
                                     application_workspace_id=w.ws, observation_id=(obs or w.observation)["id"],
                                     now=NOW)


def test_a_fully_deterministic_page_proposes_a_plan_that_still_needs_confirmation(fill_world):
    outcome = _propose(fill_world)
    assert outcome.state == "PLAN_PROPOSED" and outcome.plan_hash and outcome.confirmed is False
    stored = f.get_plan_by_hash(fill_world.conn, outcome.plan_hash)
    assert {a["page_field_key"]: a["action_kind"] for a in stored["plan"]["actions"]} == {
        "gh:email": "WRITE", "gh:notice": "WRITE", "gh:resume": "ATTACH_LOCAL", "gh:cover": "ATTACH_LOCAL",
        "gh:csrf": "IGNORE_NON_APPLICATION"}


def test_the_stored_plan_holds_no_cleartext(fill_world):
    outcome = _propose(fill_world)
    text = json.dumps(f.get_plan_by_hash(fill_world.conn, outcome.plan_hash)["plan"])
    assert "ada@example.com" not in text and "1 month" not in text


def test_new_content_is_opened_as_6da_deltas_once(fill_world):
    hear = element("gh:hear", label="How did you hear about us?", question="How did you hear about us?",
                   name="hear", id="hear", required=True)
    obs = store_observation(fill_world, observation_doc(hear))
    outcome = _propose(fill_world, obs)
    assert outcome.state == "DELTAS_OPENED"
    deltas = ra.open_deltas(fill_world.conn, fill_world.ws)
    assert [(d["kind"], d["observed"]["field_key"]) for d in deltas] == [("NEW_QUESTION", "gh:hear")]
    assert deltas[0]["source"].startswith("FILL_OBSERVATION:")
    assert not fill_world.world.state().approval_effective  # 6D-A: an open delta means NEEDS_REVIEW
    again = _propose(fill_world, obs)  # a retry never duplicates the delta
    assert again.state == "APPROVAL_NOT_EFFECTIVE" and len(ra.open_deltas(fill_world.conn, fill_world.ws)) == 1


def test_a_declaration_delta_gets_a_non_authoritative_proposal(fill_world):
    decl = element("gh:certify", label="I certify the information is true", question="I certify the information is true",
                   name="certify", id="certify", required=True)
    _propose(fill_world, store_observation(fill_world, observation_doc(decl)))
    [delta] = ra.open_deltas(fill_world.conn, fill_world.ws)
    assert (delta["kind"], delta["subject"]) == ("DECLARATION", None)  # unclassified until the user confirms
    assert f.latest_proposal(fill_world.conn, delta["id"])["subject"] == "legal.attestation"


def test_a_wrong_target_opens_a_target_change_delta(fill_world):
    obs = store_observation(fill_world, observation_doc(url="https://jobs.example.test/acme/999"))
    assert _propose(fill_world, obs).state == "DELTAS_OPENED"
    assert [d["kind"] for d in ra.open_deltas(fill_world.conn, fill_world.ws)] == ["TARGET_CHANGE"]


def test_an_uncertified_or_wizard_page_is_unsupported_and_writes_nothing(fill_world):
    doc = observation_doc()
    doc["context"]["multi_step_indicators"] = ["NEXT_BUTTON"]
    before = fill_world.conn.execute("SELECT COUNT(*) FROM review_deltas").fetchone()[0]
    outcome = _propose(fill_world, store_observation(fill_world, doc))
    assert (outcome.state, outcome.details["causes"]) == ("UNSUPPORTED_FORM", ["MULTI_STEP"])
    assert fill_world.conn.execute("SELECT COUNT(*) FROM review_deltas").fetchone()[0] == before


def test_a_prefilled_conflict_is_a_stop_not_an_overwrite(fill_world):
    doc = observation_doc()
    doc["elements"][0]["value_state"] = {"state": "NONBLANK", "current_value_hash": fill_value_hash("x@y.test")}
    outcome = _propose(fill_world, store_observation(fill_world, doc))
    assert (outcome.state, outcome.details["reason"]) == ("STOPPED", "PREFILLED_VALUE_CONFLICT")


def test_non_deterministic_approved_content_needs_review_then_a_mapping_choice_resolves_it(fill_world):
    other = element("gh:contact", control_kind="email", type="email", label="Contact address",
                    question="Contact address", name="contact", id="contact", required=True)
    doc = observation_doc(other)
    doc["elements"] = [e for e in doc["elements"] if e["page_field_key"] != "gh:email"]
    obs = store_observation(fill_world, doc)
    outcome = _propose(fill_world, obs)
    assert outcome.state == "PLAN_NEEDS_REVIEW"
    [row] = outcome.details["needs_review"]
    assert row["page_field_key"] == "gh:contact" and "contact:email" in row["candidates"]
    with pytest.raises(ReviewRefused) as refused:
        fp.record_mapping_choice(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT,
                                 application_workspace_id=fill_world.ws, observation_id=obs["id"],
                                 page_field_key="gh:contact", answer_key="document:cv", choice="MAP", actor="u", now=NOW)
    assert refused.value.reason == "incompatible_mapping"
    fp.record_mapping_choice(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT,
                             application_workspace_id=fill_world.ws, observation_id=obs["id"],
                             page_field_key="gh:contact", answer_key="contact:email", choice="MAP", actor="u", now=NOW)
    assert _propose(fill_world, obs).state == "PLAN_PROPOSED"


def test_presentation_shows_cleartext_that_matches_the_plan_hashes(fill_world):
    view = _presentation(fill_world)
    rows = {r["page_field_key"]: r for r in view["rows"]}
    assert rows["gh:notice"]["rendered_value"] == "1 month"
    assert rows["gh:email"]["rendered_value"] == "ada@example.com"
    assert rows["gh:resume"]["document"]["filename"].endswith(".docx")
    assert view["displayed_plan_hash"] and view["confirmed"] is False
    plan = f.get_plan_by_hash(fill_world.conn, view["displayed_plan_hash"])
    assert plan is None  # the presentation is read-only: nothing stored by viewing
    for key in ("gh:notice", "gh:email"):
        assert fill_value_hash(rows[key]["rendered_value"]) == rows[key]["rendered_value_hash"]


def test_confirmation_binds_the_displayed_hash_and_refuses_a_stale_view(fill_world):
    shown = _presentation(fill_world)["displayed_plan_hash"]
    with pytest.raises(ReviewRefused) as stale:
        fp.confirm_plan(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT,
                        application_workspace_id=fill_world.ws, observation_id=fill_world.observation["id"],
                        displayed_plan_hash="sha256:" + "0" * 64, actor="u", now=NOW)
    assert stale.value.reason == "stale_plan"
    assert fill_world.conn.execute("SELECT COUNT(*) FROM fill_plan_confirmations").fetchone()[0] == 0
    fp.confirm_plan(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT,
                    application_workspace_id=fill_world.ws, observation_id=fill_world.observation["id"],
                    displayed_plan_hash=shown, actor="u", now=NOW)
    assert _propose(fill_world).confirmed is True
    fp.confirm_plan(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT,  # idempotent
                    application_workspace_id=fill_world.ws, observation_id=fill_world.observation["id"],
                    displayed_plan_hash=shown, actor="u", now=NOW)
    assert fill_world.conn.execute("SELECT COUNT(*) FROM fill_plan_confirmations").fetchone()[0] == 1


def test_a_new_6da_approval_invalidates_the_confirmation(fill_world):
    shown = _presentation(fill_world)["displayed_plan_hash"]
    fp.confirm_plan(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT,
                    application_workspace_id=fill_world.ws, observation_id=fill_world.observation["id"],
                    displayed_plan_hash=shown, actor="u", now=NOW)
    from product.autonomy_contract import Reach
    from webapp.services import review_answers as rv
    rv.answer_field(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT,
                    application_workspace_id=fill_world.ws, answer_key="subject:employment.notice_period",
                    value="2 months", reach=Reach.APPLICATION, actor="u", now=NOW)
    fill_world.world.make_approvable()
    fill_world.world.approve()
    outcome = _propose(fill_world)
    assert outcome.state == "PLAN_PROPOSED" and outcome.confirmed is False and outcome.plan_hash != shown


def test_a_not_effective_approval_never_yields_a_plan(fill_world):
    from webapp.services.review_approval import revoke
    revoke(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT, application_workspace_id=fill_world.ws,
           actor="u", now=NOW)
    assert _propose(fill_world).state == "APPROVAL_NOT_EFFECTIVE"
