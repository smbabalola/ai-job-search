from __future__ import annotations

from datetime import timedelta

import pytest

from webapp.persistence import review_approval as ra
from webapp.services import review_approval as svc
from webapp.services.review_application import ReviewRefused
from tests.webapp.services.review_fixtures import (  # noqa: F401
    ACCOUNT, NOW, V2_ACCOUNT, diff_counts, docx_bytes, table_counts, v2_chain,
)


def _events(world, name):
    return [e for e in ra.events(world.conn, world.ws) if e["event"] == name]


def _refused(world, reason, **kw):
    before = table_counts(world.conn)
    with pytest.raises(ReviewRefused) as caught:
        world.approve(**kw)
    assert caught.value.reason == reason
    assert diff_counts(before, table_counts(world.conn)) == {}


def test_normal_approval_write_contract(v2_chain):
    v2_chain.make_approvable()
    before = table_counts(v2_chain.conn)
    out = v2_chain.approve()
    assert diff_counts(before, table_counts(v2_chain.conn)) == {"application_approvals": 1,
                                                               "application_review_events": 1,
                                                               "document_version_references": 2}  # Bundle 7 L3: cv + cover letter
    [event] = _events(v2_chain, "APPROVED")
    assert event["detail"]["approval_id"] == out["approval_id"]
    state = v2_chain.state()
    assert state.state == "APPROVED_FOR_FILL" and state.approval_effective


def test_approval_never_acknowledges(v2_chain):
    v2_chain.make_approvable()
    v2_chain.set_target(url="https://jobs.example.test/acme/other")  # a new unacknowledged ATTENTION warning
    _refused(v2_chain, "unacknowledged_attention")
    assert len(_events(v2_chain, "WARNING_ACKNOWLEDGED")) == len({e["detail"]["warning_key"]
                                                                  for e in _events(v2_chain, "WARNING_ACKNOWLEDGED")})


def test_stale_blocking_and_no_pack_refusals_write_nothing(v2_chain):
    v2_chain.make_approvable()
    _refused(v2_chain, "stale", displayed="sha256:not-what-was-shown")
    from webapp.services import review_documents as rd
    rd.replace_document(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                        application_workspace_id=v2_chain.ws, kind="cv", filename="m.docx", content=docx_bytes("x"),
                        expected_revision=v2_chain.selection("cv")["revision"], actor="u", now=NOW)
    _refused(v2_chain, "no_pack", displayed="sha256:anything")
    rd.save_changes(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                    application_workspace_id=v2_chain.ws, actor="u", now=NOW)
    _refused(v2_chain, "blocking" if v2_chain.state().blocking else "unacknowledged_attention")


def test_approve_while_paused_and_halted_succeeds(v2_chain):
    from webapp.services.autonomy_controls import engage_kill_switch, pause
    v2_chain.make_approvable()
    pause(v2_chain.conn, account_id=V2_ACCOUNT, scope_type="APPLICATION", scope_id=v2_chain.ws, actor="u",
          reason="r", now=NOW)
    engage_kill_switch(v2_chain.conn, account_id=V2_ACCOUNT, actor="u", reason="stop", now=NOW)
    assert v2_chain.approve()["approval_id"]


def test_second_approval_at_the_same_hash_is_already_approved_and_writes_nothing(v2_chain):
    v2_chain.make_approvable()
    v2_chain.approve()
    _refused(v2_chain, "already_approved")


def _delta(world, **kw):
    values = dict(account_id=V2_ACCOUNT, application_workspace_id=world.ws, kind="NEW_QUESTION", answer_key=None,
                  subject=None, required=False, question="Anything else?", observed={"field_key": "f"},
                  source="FILL_SESSION:s1", now=NOW)
    values.update(kw)
    d = ra.insert_delta(world.conn, **values)
    ra.record_event(world.conn, account_id=V2_ACCOUNT, application_workspace_id=world.ws, event="DELTA_OPENED",
                    binding_hash=None, detail={"delta_id": d["id"]}, actor="system", now=NOW)
    world.conn.commit()
    return d


def test_delta_reapproval_writes_one_delta_resolved_per_delta(v2_chain):
    v2_chain.make_approvable()
    v2_chain.approve()
    deltas = [_delta(v2_chain, observed={"field_key": f"f{i}"}) for i in range(2)]
    assert v2_chain.state().state == "NEEDS_REVIEW"
    v2_chain.make_approvable()  # the user leaves both new optional questions blank
    before = table_counts(v2_chain.conn)
    out = v2_chain.approve()
    assert diff_counts(before, table_counts(v2_chain.conn)) == {"application_approvals": 1,
                                                               "application_review_events": 3,
                                                               "document_version_references": 2}  # Bundle 7 L3
    resolved = {e["detail"]["delta_id"] for e in _events(v2_chain, "DELTA_RESOLVED")}
    assert resolved == {d["id"] for d in deltas} and set(out["resolved_delta_ids"]) == resolved
    assert ra.open_deltas(v2_chain.conn, v2_chain.ws) == [] and v2_chain.state().approval_effective


def _invalidations(world):
    return _events(world, "APPROVAL_INVALIDATED")


def _record(world, now=NOW):
    return svc.record_invalidation_if_needed(world.conn, settings=world.settings, account_id=V2_ACCOUNT,
                                             application_workspace_id=world.ws, now=now)


def test_invalidation_recorded_once_per_reason_set_for_every_cause(v2_chain):
    v2_chain.make_approvable()
    v2_chain.approve()
    assert not _record(v2_chain)  # effective: nothing to record
    v2_chain.set_target(url="https://jobs.example.test/acme/changed")  # binding changes (target + warning)
    assert _record(v2_chain) and not _record(v2_chain)
    [event] = _invalidations(v2_chain)
    assert "binding_changed" in event["detail"]["reasons"] and "apply_target" in event["detail"]["reasons"]
    _delta(v2_chain, required=True, observed={"field_key": "g"})  # a new reason set: open delta
    assert _record(v2_chain) and not _record(v2_chain) and len(_invalidations(v2_chain)) == 2


def test_same_reason_set_with_a_new_provisional_hash_records_nothing(v2_chain):
    v2_chain.make_approvable()
    v2_chain.approve()
    v2_chain.set_target(url="https://jobs.example.test/acme/one")
    _record(v2_chain)
    v2_chain.set_target(url="https://jobs.example.test/acme/two")  # a new hash, the same reason set
    assert not _record(v2_chain)
    assert len(_invalidations(v2_chain)) == 1


def test_revoke_and_expiry(v2_chain):
    v2_chain.make_approvable()
    approval = v2_chain.approve()
    svc.revoke(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
               application_workspace_id=v2_chain.ws, actor="u", now=NOW)
    state = v2_chain.state()
    assert state.state == "NEEDS_REVIEW" and "revoked" in state.reasons
    assert _events(v2_chain, "REVOKED")[0]["detail"]["approval_id"] == approval["approval_id"]
    with pytest.raises(ReviewRefused):
        svc.revoke(v2_chain.conn, settings=v2_chain.settings, account_id=V2_ACCOUNT,
                   application_workspace_id=v2_chain.ws, actor="u", now=NOW)
    later = NOW + timedelta(days=v2_chain.settings.review_approval_ttl_days)
    _record(v2_chain, now=later)
    assert _events(v2_chain, "EXPIRED") and not _record(v2_chain, now=later)


def test_reconcile_runs_in_the_6c_sweep_and_is_reduce_only(v2_chain):
    import dataclasses
    import random
    from webapp.services import autonomy_scheduler as sched
    from webapp.services.autonomy_providers import ProviderSet
    v2_chain.make_approvable()
    v2_chain.approve()
    v2_chain.set_target(url="https://jobs.example.test/acme/swept")
    before = table_counts(v2_chain.conn)
    report = sched.run_tick(v2_chain.conn, settings=dataclasses.replace(v2_chain.settings,
                                                                       autonomy_scheduler_enabled=False),
                            providers=ProviderSet(None, None, None), now=NOW, rng=random.Random(1), worker_id="w")
    assert report.sweeps["review_invalidations"] == 1
    delta = diff_counts(before, table_counts(v2_chain.conn))
    assert delta == {"application_review_events": 1}
