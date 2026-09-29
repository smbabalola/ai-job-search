"""6D-B R7 classification (spec §9): a proposal never classifies; the user's
confirmation performs the atomic R7 successor transition exactly once, and is
provable separately from answer approval."""
from __future__ import annotations

import pytest

from product.autonomy_contract import Reach
from tests.product.fill_observation_fixtures import element
from tests.webapp.services.fill_fixtures import (  # noqa: F401
    NOW, V2_ACCOUNT, fill_world, observation_doc, store_observation, v2_chain,
)
from webapp.persistence import fill as f
from webapp.persistence import review_approval as ra
from webapp.services import fill_classification as fc
from webapp.services import fill_plans as fp
from webapp.services import review_answers as rv
from webapp.services.review_application import ReviewRefused


def _open_unclassified(w, question="How long is your notice?", required=True):
    q = element("gh:q", label=question, question=question, name="q", id="q", required=required)
    obs = store_observation(w, observation_doc(q))
    fp.propose_plan(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                    observation_id=obs["id"], now=NOW)
    [delta] = ra.open_deltas(w.conn, w.ws)
    return delta


def _confirm(w, delta, proposal_id, subject):
    return fc.confirm_classification(w.conn, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                                     delta_id=delta["id"], displayed_proposal_id=proposal_id, subject=subject,
                                     actor="u", now=NOW)


def test_a_heuristic_proposal_is_stored_but_the_delta_stays_unclassified(fill_world):
    delta = _open_unclassified(fill_world)
    assert delta["subject"] is None
    proposal = f.latest_proposal(fill_world.conn, delta["id"])
    assert proposal["subject"] == "employment.notice_period" and proposal["basis"] == "HEURISTIC"
    field = next(x for x in fill_world.world.reviewable().fields if x.answer_key == f"delta:{delta['id']}")
    with pytest.raises(ReviewRefused) as refused:  # 6D-A R7: an unclassified required question can't be answered
        rv.answer_field(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT,
                        application_workspace_id=fill_world.ws, answer_key=field.answer_key, value="1 month",
                        reach=Reach.APPLICATION, actor="u", now=NOW)
    assert refused.value.reason == "unclassified_subject"


def test_confirming_performs_the_r7_transition_atomically_and_once(fill_world):
    delta = _open_unclassified(fill_world)
    proposal = f.latest_proposal(fill_world.conn, delta["id"])
    successor = _confirm(fill_world, delta, proposal["id"], "employment.notice_period")
    assert successor["subject"] == "employment.notice_period"
    events = [e for e in ra.events(fill_world.conn, fill_world.ws) if e["event"] == "DELTA_RESOLVED"]
    assert [(e["detail"]["delta_id"], e["detail"]["reason"], e["detail"]["successor_delta_id"]) for e in events] == [
        (delta["id"], "classified", successor["id"])]
    confirmation = f.classification_confirmation(fill_world.conn, delta["id"])
    assert (confirmation["subject"], confirmation["successor_delta_id"]) == ("employment.notice_period", successor["id"])
    counts = {t: fill_world.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("review_deltas", "application_review_events", "delta_classification_confirmations")}
    again = _confirm(fill_world, delta, proposal["id"], "employment.notice_period")  # retry writes nothing
    assert again["id"] == successor["id"]
    assert counts == {t: fill_world.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts}


def test_classification_and_answer_approval_are_separately_provable(fill_world):
    delta = _open_unclassified(fill_world)
    proposal = f.latest_proposal(fill_world.conn, delta["id"])

    def edits():
        return len([e for e in ra.events(fill_world.conn, fill_world.ws) if e["event"] == "ANSWER_EDITED"])
    before = edits()
    successor = _confirm(fill_world, delta, proposal["id"], "employment.notice_period")
    assert edits() == before  # classifying answered nothing
    rv.answer_field(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT,
                    application_workspace_id=fill_world.ws, answer_key=successor["answer_key"], value="1 month",
                    reach=Reach.APPLICATION, actor="u", now=NOW)
    assert f.classification_confirmation(fill_world.conn, delta["id"]) is not None
    assert edits() == before + 1


@pytest.mark.parametrize("subject,proposal_ok,reason", [
    ("employment.availability_start", True, "subject_mismatch"),  # not the displayed proposal's subject
    ("employment.notice_period", False, "stale_proposal"),        # not the displayed proposal
    ("made.up_subject", True, "subject_mismatch"),
])
def test_confirmation_refusals_write_nothing(fill_world, subject, proposal_ok, reason):
    delta = _open_unclassified(fill_world)
    proposal = f.latest_proposal(fill_world.conn, delta["id"])
    before = fill_world.conn.execute("SELECT COUNT(*) FROM review_deltas").fetchone()[0]
    with pytest.raises(ReviewRefused) as refused:
        _confirm(fill_world, delta, proposal["id"] if proposal_ok else "fprop_other", subject)
    assert refused.value.reason == reason
    assert fill_world.conn.execute("SELECT COUNT(*) FROM review_deltas").fetchone()[0] == before


def test_a_deterministic_rule_opens_a_classified_delta_directly(fill_world):
    start = element("gh:start", label="When can you start?", question="When can you start?", name="s", id="s")
    obs = store_observation(fill_world, observation_doc(start))
    fp.propose_plan(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT,
                    application_workspace_id=fill_world.ws, observation_id=obs["id"], now=NOW)
    [delta] = ra.open_deltas(fill_world.conn, fill_world.ws)
    assert delta["subject"] == "employment.availability_start"
    assert f.latest_proposal(fill_world.conn, delta["id"]) is None  # no proposal: the rule classified it


def test_a_heuristic_tie_proposes_nothing():
    from webapp.services.fill_plans import heuristic_subject
    # "notice" points at the notice period, "employer" at employer motivation: a tie is no proposal.
    assert heuristic_subject("How much notice must you give your employer?") is None
    assert heuristic_subject("How long is your notice?") == "employment.notice_period"
    assert heuristic_subject("Favourite colour?") is None
