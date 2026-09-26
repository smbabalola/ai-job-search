from __future__ import annotations

import pytest

from product.autonomy_contract import Reach
from webapp.persistence import review_approval as ra
from webapp.persistence.autonomy_answers import current_approved_answers, save_proposed_answer
from webapp.services import review_answers as rv
from webapp.services.review_application import ReviewRefused
from tests.webapp.services.review_fixtures import (  # noqa: F401
    NOW, V2_ACCOUNT, blocker, diff_counts, table_counts, v2_chain,
)
from tests.webapp.services.test_review_bulk import _second

NOTICE = "employment.notice_period"
EEO = "demographic.eeo"
KEY = f"subject:{NOTICE}"


def _answer(world, value, *, key=KEY, reach=Reach.ACCOUNT):
    return rv.answer_field(world.conn, settings=world.settings, account_id=V2_ACCOUNT,
                           application_workspace_id=world.ws, answer_key=key, value=value, reach=reach, actor="u",
                           now=NOW)


def _field(world, key=KEY):
    return next(f for f in world.reviewable().fields if f.answer_key == key)


def _events(world, name):
    return [e for e in ra.events(world.conn, world.ws) if e["event"] == name]


def test_answer_supersedes_and_invalidates(v2_chain):
    blocker(v2_chain.conn, v2_chain.ws, NOTICE)
    first = _answer(v2_chain, "1 month")
    v2_chain.make_approvable()
    v2_chain.approve()
    second = _answer(v2_chain, "2 months")
    assert _field(v2_chain).source_ref == second["approved_answer_id"] != first["approved_answer_id"]
    assert [a["id"] for a in current_approved_answers(v2_chain.conn, account_id=V2_ACCOUNT, subject=NOTICE)] == \
        [second["approved_answer_id"]]
    state = v2_chain.state()
    assert not state.approval_effective and "binding_changed" in state.reasons and _events(v2_chain, "ANSWER_EDITED")


def test_editing_an_account_answer_invalidates_every_approval_that_bound_it(v2_chain):
    other = _second(v2_chain)
    for world in (v2_chain, other):
        blocker(world.conn, world.ws, NOTICE)
    _answer(v2_chain, "1 month")
    for world in (v2_chain, other):
        world.make_approvable()
        world.approve()
        assert world.state().approval_effective
    _answer(v2_chain, "3 months")  # edited while reviewing A: the answer is account-wide
    for world in (v2_chain, other):
        state = world.state()
        assert not state.approval_effective and "binding_changed" in state.reasons


def test_omit_refused_for_required(v2_chain):
    blocker(v2_chain.conn, v2_chain.ws, NOTICE)
    with pytest.raises(ReviewRefused) as caught:
        rv.set_field_disposition(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                                 application_workspace_id=v2_chain.ws, answer_key=KEY, disposition="OMIT", actor="u",
                                 now=NOW)
    assert caught.value.reason == "required_field"


def test_acknowledge_only_attention_and_it_changes_the_binding(v2_chain):
    before = v2_chain.state().binding_hash
    attention = next(w for w in v2_chain.reviewable().warnings if w.key.startswith("target_user_supplied"))
    rv.acknowledge_warning(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                           application_workspace_id=v2_chain.ws, warning_key=attention.key, actor="u", now=NOW)
    assert v2_chain.state().binding_hash != before
    with pytest.raises(ReviewRefused) as caught:
        rv.acknowledge_warning(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                               application_workspace_id=v2_chain.ws, warning_key="fit_score", actor="u", now=NOW)
    assert caught.value.reason == "not_attention"


def test_system_proposal_never_supersedes_a_user_answer(v2_chain):
    b = blocker(v2_chain.conn, v2_chain.ws, NOTICE)
    mine = _answer(v2_chain, "1 month")
    save_proposed_answer(v2_chain.conn, blocker_id=b["id"], subject=NOTICE, value="6 months", now=NOW)
    assert _field(v2_chain).source_ref == mine["approved_answer_id"]


def test_proposal_acceptance_event_carries_proposal_and_answer_ids(v2_chain):
    b = blocker(v2_chain.conn, v2_chain.ws, NOTICE)
    proposal = save_proposed_answer(v2_chain.conn, blocker_id=b["id"], subject=NOTICE, value="1 month", now=NOW)
    out = rv.accept_proposal(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                             application_workspace_id=v2_chain.ws, proposal_id=proposal["id"], edited_value=None,
                             reach=Reach.ACCOUNT, actor="u", now=NOW)
    [event] = _events(v2_chain, "PROPOSAL_ACCEPTED")
    assert event["detail"] == {"proposal_id": proposal["id"], "approved_answer_id": out["approved_answer_id"]}
    assert not any(w.key.startswith("proposal_unaccepted") for w in v2_chain.reviewable().warnings)
    with pytest.raises(ReviewRefused):
        rv.accept_proposal(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                           application_workspace_id=v2_chain.ws, proposal_id=proposal["id"], edited_value=None,
                           reach=Reach.ACCOUNT, actor="u", now=NOW)


def test_sensitive_answer_for_application_a_is_not_available_to_application_b_at_the_same_employer(v2_chain):
    from webapp.persistence.workspaces import get_workspace
    other = _second(v2_chain)
    v2_chain.conn.execute("UPDATE workspaces SET company = ? WHERE id = ?",
                          (get_workspace(v2_chain.conn, v2_chain.ws)["company"], other.ws))  # same employer
    for world in (v2_chain, other):
        blocker(world.conn, world.ws, EEO)
    out = _answer(v2_chain, "prefer not to say", key=f"subject:{EEO}", reach=Reach.ACCOUNT)  # forced per application
    row = next(a for a in current_approved_answers(v2_chain.conn, account_id=V2_ACCOUNT, subject=EEO)
               if a["id"] == out["approved_answer_id"])
    assert (row["reach"], row["scope_id"]) == ("APPLICATION", v2_chain.ws)
    assert _field(v2_chain, f"subject:{EEO}").disposition == "ANSWER"
    assert _field(other, f"subject:{EEO}").disposition is None


def test_unclassified_key_cannot_be_answered(v2_chain):
    d = ra.insert_delta(v2_chain.conn, account_id=V2_ACCOUNT, application_workspace_id=v2_chain.ws,
                        kind="NEW_QUESTION", answer_key=None, subject=None, required=False, question="?",
                        observed={"field_key": "x"}, source="s", now=NOW)
    v2_chain.conn.commit()
    key = f"delta:{d['id']}"
    for call in (lambda: _answer(v2_chain, "x", key=key),
                 lambda: rv.set_field_disposition(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                                                  application_workspace_id=v2_chain.ws, answer_key=key,
                                                  disposition="ANSWER", actor="u", now=NOW)):
        with pytest.raises(ReviewRefused) as caught:
            call()
        assert caught.value.reason == "unclassified_subject"
    rv.set_field_disposition(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                             application_workspace_id=v2_chain.ws, answer_key=key, disposition="OMIT", actor="u",
                             now=NOW)
    assert _field(v2_chain, key).disposition == "OMIT"


def _boom(*a, **k):
    raise RuntimeError("audit failed")


def test_each_action_and_its_event_are_atomic(v2_chain, monkeypatch):
    b = blocker(v2_chain.conn, v2_chain.ws, NOTICE)
    proposal = save_proposed_answer(v2_chain.conn, blocker_id=b["id"], subject=NOTICE, value="1 month", now=NOW)
    ra.insert_delta(v2_chain.conn, account_id=V2_ACCOUNT, application_workspace_id=v2_chain.ws, kind="NEW_QUESTION",
                    answer_key="subject:employment.availability_start", subject="employment.availability_start",
                    required=False, question="Start?", observed={"field_key": "s"}, source="s", now=NOW)
    v2_chain.conn.commit()
    attention = next(w for w in v2_chain.reviewable().warnings if w.key.startswith("target_user_supplied"))
    monkeypatch.setattr(rv.ra, "record_event", _boom)
    actions = [
        lambda: _answer(v2_chain, "1 month"),
        lambda: rv.accept_proposal(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                                   application_workspace_id=v2_chain.ws, proposal_id=proposal["id"], edited_value=None,
                                   reach=Reach.ACCOUNT, actor="u", now=NOW),
        lambda: rv.set_field_disposition(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                                         application_workspace_id=v2_chain.ws,
                                         answer_key="subject:employment.availability_start", disposition="OMIT",
                                         actor="u", now=NOW),
        lambda: rv.acknowledge_warning(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                                       application_workspace_id=v2_chain.ws, warning_key=attention.key, actor="u",
                                       now=NOW),
    ]
    for action in actions:
        before = table_counts(v2_chain.conn)
        with pytest.raises(RuntimeError):
            action()
        assert diff_counts(before, table_counts(v2_chain.conn)) == {}
