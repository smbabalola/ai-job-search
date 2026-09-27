"""Bundle 6D-A races (spec §19, criterion 16): real threads, one connection
each, WAL. Every race ends in exactly one approval record or a clean
refusal, never a partial state."""
from __future__ import annotations

from product.autonomy_contract import Reach
from webapp.persistence import review_approval as ra
from webapp.services import review_answers as rv
from webapp.services import review_approval as svc
from webapp.services import review_documents as rd
from webapp.services.review_application import ReviewRefused, review_state
from tests.webapp.services.review_fixtures import NOW, V2_ACCOUNT, blocker, v2_chain  # noqa: F401
from tests.webapp.services.test_autonomy_6c_concurrency import race

NOTICE = "employment.notice_period"


def _db(world):
    return world.settings.db_path


def _outcome(fn):
    """Wrap a service call so a ReviewRefused is a result, not an error."""
    def run(c):
        try:
            return fn(c)
        except ReviewRefused as exc:
            return ("refused", exc.reason)
    return run


def _approve(world, shown):
    return _outcome(lambda c: svc.approve(c, settings=world.settings, account_id=V2_ACCOUNT,
                                          application_workspace_id=world.ws, displayed_binding_hash=shown,
                                          actor="u", now=NOW))


def _consistent(world):
    """No partial state: one APPROVED event per approval row."""
    approvals = world.conn.execute("SELECT COUNT(*) FROM application_approvals WHERE application_workspace_id = ?",
                                   (world.ws,)).fetchone()[0]
    approved = [e for e in ra.events(world.conn, world.ws) if e["event"] == "APPROVED"]
    assert len(approved) == approvals
    return approvals


def _state(world):
    return review_state(world.conn, settings=world.settings, account_id=V2_ACCOUNT, application_workspace_id=world.ws,
                        now=NOW)


def test_two_approvals_at_the_same_hash_make_one_record(v2_chain):
    shown = v2_chain.make_approvable().binding_hash
    got = race(_db(v2_chain), _approve(v2_chain, shown), _approve(v2_chain, shown))
    assert sorted(isinstance(g, dict) for g in got) == [False, True]
    assert ("refused", "already_approved") in got
    assert _consistent(v2_chain) == 1 and _state(v2_chain).approval_effective


def test_approve_versus_save_changes(v2_chain):
    shown = v2_chain.make_approvable().binding_hash
    save = _outcome(lambda c: rd.save_changes(c, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                                              application_workspace_id=v2_chain.ws, actor="u", now=NOW))
    approve_result, saved = race(_db(v2_chain), _approve(v2_chain, shown), save)
    assert isinstance(saved, dict)  # Save changes is never refused; it re-confirms a new pack
    count = _consistent(v2_chain)
    state = _state(v2_chain)
    if isinstance(approve_result, dict):
        assert count == 1 and approve_result["binding_hash"] == shown != state.binding_hash
        assert not state.approval_effective
    else:
        assert approve_result[0] == "refused" and approve_result[1] in ("stale", "no_pack") and count == 0


def test_approve_versus_answer_supersede(v2_chain):
    blocker(v2_chain.conn, v2_chain.ws, NOTICE)
    rv.answer_field(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                    application_workspace_id=v2_chain.ws, answer_key=f"subject:{NOTICE}", value="1 month",
                    reach=Reach.ACCOUNT, actor="u", now=NOW)
    shown = v2_chain.make_approvable().binding_hash
    edit = _outcome(lambda c: rv.answer_field(c, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                                              application_workspace_id=v2_chain.ws, answer_key=f"subject:{NOTICE}",
                                              value="2 months", reach=Reach.ACCOUNT, actor="u", now=NOW))
    approve_result, edited = race(_db(v2_chain), _approve(v2_chain, shown), edit)
    assert isinstance(edited, dict)
    count = _consistent(v2_chain)
    state = _state(v2_chain)
    assert state.binding_hash != shown and not state.approval_effective
    if isinstance(approve_result, dict):
        assert count == 1 and "binding_changed" in state.reasons
    else:
        assert approve_result == ("refused", "stale") and count == 0


def test_bulk_versus_single_approve_make_one_record(v2_chain):
    shown = v2_chain.make_approvable().binding_hash
    assert svc.record_presented(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                                application_workspace_id=v2_chain.ws, actor="u", now=NOW) == shown
    bulk = _outcome(lambda c: svc.approve_selected(c, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                                                   items=[{"workspace_id": v2_chain.ws,
                                                           "displayed_binding_hash": shown}],
                                                   actor="u", now=NOW))
    bulk_result, single = race(_db(v2_chain), bulk, _approve(v2_chain, shown))
    outcomes = [bulk_result[0]["outcome"], "approved" if isinstance(single, dict) else single[1]]
    assert sorted(outcomes) == ["already_approved", "approved"]
    assert _consistent(v2_chain) == 1
