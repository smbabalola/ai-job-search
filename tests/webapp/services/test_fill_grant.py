"""6D-B FILL grant (spec §11.2): one BEGIN IMMEDIATE over the 6D-A approval,
the plan confirmation, the revalidation, QUARANTINE_ACTIVE and G4 coverage,
then the unchanged public 6B request_grant. Every refusal is a stop with no
grant, and the page's quarantine is left exactly as it was."""
from __future__ import annotations

import pytest

from product.fill_manifest import manifest_hash
from product.fill_plan import derive_manifest
from tests.webapp.services.fill_fixtures import (  # noqa: F401
    NOW, SEARCH_WS, V2_ACCOUNT, grant_world, observation_doc, v2_chain,
)
from tests.webapp.services.test_fill_runs import RULESET, events, observe, start, to_active
from webapp.persistence import fill as f
from webapp.persistence.autonomy_ledger import count_usage, get_grant, try_reserve
from webapp.services import fill_runs as fr
from webapp.services.autonomy_context import day_window
from webapp.services.autonomy_controls import engage_kill_switch, pause


def grant(w, run):
    return fr.request_fill_grant(w.conn, settings=w.settings, run_id=run["id"], now=NOW)


def fill_grants(w):
    return w.conn.execute("SELECT COUNT(*) FROM autonomy_grants WHERE stage = 'FILL'").fetchone()[0]


def test_the_grant_binds_every_hash(grant_world):
    run = to_active(grant_world)
    out = grant(grant_world, run)
    assert out.granted and out.grant_id
    binding = f.get_grant_binding(grant_world.conn, run["id"])
    plan = f.get_plan_by_hash(grant_world.conn, grant_world.plan_hash)["plan"]
    revalidated = f.latest_observation(grant_world.conn, run["id"], "REVALIDATION")
    approval = grant_world.world.state()
    assert (binding["grant_id"], binding["plan_hash"], binding["ruleset_hash"]) == (out.grant_id, plan["plan_hash"], RULESET)
    assert binding["approval_binding_hash"] == approval.binding_hash == plan["approval_binding_hash"]
    assert binding["approval_id"] == plan["approval_id"]
    assert (binding["structure_fingerprint"], binding["observation_fingerprint"]) == (
        revalidated["structure_fingerprint"], revalidated["observation_fingerprint"])
    issued = get_grant(grant_world.conn, out.grant_id)
    manifest = issued["binding"]["fill_manifest"]
    assert issued["stage"] == "FILL" and issued["binding"]["fill_manifest_hash"] == manifest_hash(manifest)
    assert {e["page_field_key"] for e in manifest["pages"][0]["entries"]} == {
        "gh:email", "gh:notice", "gh:resume", "gh:cover"}
    notice = next(e for e in manifest["pages"][0]["entries"] if e["page_field_key"] == "gh:notice")
    assert notice["source"]["kind"] == "APPROVED_ANSWER" and notice["source"]["confirmation_id"]
    assert events(grant_world, run)[-1] == ("FILLING", None)
    day, _ = day_window(NOW, "Europe/London")
    assert count_usage(grant_world.conn, account_id=V2_ACCOUNT, counter_name="fill_per_day", window_key=day) == 1


def test_a_retried_grant_request_returns_the_bound_grant(grant_world):
    run = to_active(grant_world)
    first = grant(grant_world, run)
    assert grant(grant_world, run).grant_id == first.grant_id and fill_grants(grant_world) == 1


def _revoke_approval(w, run):
    from webapp.services.review_approval import revoke
    revoke(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws, actor="u", now=NOW)


def _stale_confirmation(w, run, monkeypatch):
    monkeypatch.setattr(fr.f, "plan_confirmed", lambda *a, **k: False)


def _g4_violation(w, run, monkeypatch):
    def dropping(plan, **kw):
        manifest = derive_manifest(plan, **kw)
        manifest["pages"][0]["entries"] = manifest["pages"][0]["entries"][:-1]
        return manifest
    monkeypatch.setattr(fr, "derive_manifest", dropping)


def _pause(w, run):
    pause(w.conn, account_id=V2_ACCOUNT, scope_type="APPLICATION", scope_id=w.ws, actor="u", reason="hold", now=NOW)


def _kill_switch(w, run):
    engage_kill_switch(w.conn, account_id=V2_ACCOUNT, actor="u", reason="stop", now=NOW)


def _fill_per_day_exhausted(w, run):
    day, _ = day_window(NOW, "Europe/London")
    while try_reserve(w.conn, account_id=V2_ACCOUNT, counter_name="fill_per_day", window_key=day, limit=10,
                      now=NOW) is not None:
        pass
    w.conn.commit()


@pytest.mark.parametrize("breaker,reason,cause", [
    (_revoke_approval, "APPROVAL_NOT_EFFECTIVE", None),
    (_stale_confirmation, "PLAN_CONFIRMATION_STALE", None),
    (_g4_violation, "APPROVAL_NOT_EFFECTIVE", None),
    (_pause, "GRANT_REFUSED", "PAUSED"),
    (_kill_switch, "GRANT_REFUSED", None),
    (_fill_per_day_exhausted, "GRANT_REFUSED", None),
])
def test_each_failing_precondition_is_a_stop_without_a_grant(grant_world, monkeypatch, breaker, reason, cause):
    run = to_active(grant_world)
    quarantine_before = f.quarantine_events(grant_world.conn, run["id"])
    args = (grant_world, run, monkeypatch) if breaker in (_stale_confirmation, _g4_violation) else (grant_world, run)
    breaker(*args)
    out = grant(grant_world, run)
    assert (out.granted, out.stop_reason) == (False, reason)
    if cause:
        assert out.detail["cause"] == cause
    if breaker is _g4_violation:
        assert any(v.startswith("rule5") for v in out.detail["g4"])
    assert fill_grants(grant_world) == 0 and f.get_grant_binding(grant_world.conn, run["id"]) is None
    assert events(grant_world, run)[-1] == ("FILL_STOPPED", reason)
    assert f.quarantine_events(grant_world.conn, run["id"]) == quarantine_before  # the quarantine is untouched
    assert f.get_result(grant_world.conn, run["id"])["result"]["quarantine_status"] == "TOTAL_VERIFIED"


def test_no_grant_without_quarantine_active(grant_world):
    run = start(grant_world)
    observe(grant_world, run, "INITIAL")
    observe(grant_world, run, "REVALIDATION")
    out = grant(grant_world, run)
    assert (out.granted, out.stop_reason, out.detail["cause"]) == (False, "GRANT_REFUSED", "QUARANTINE_NOT_ACTIVE")
    assert fill_grants(grant_world) == 0


def test_a_stopped_run_cannot_request_a_grant(grant_world):
    run = to_active(grant_world)
    fr.stop_run(grant_world.conn, run_id=run["id"], reason="EXECUTION_CONTEXT_CLOSED", detail={}, now=NOW)
    with pytest.raises(fr.FillRefused):
        grant(grant_world, run)


def test_stopping_a_filling_run_revokes_its_unused_grant(grant_world):
    run = to_active(grant_world)
    out = grant(grant_world, run)
    fr.stop_run(grant_world.conn, run_id=run["id"], reason="EXECUTION_CONTEXT_CLOSED", detail={}, now=NOW)
    assert get_grant(grant_world.conn, out.grant_id)["status"] == "REVOKED"


def test_the_public_submit_refusal_is_unchanged_in_transaction(grant_world):
    from product.autonomy_contract import Capability
    from webapp.services.autonomy import SubmissionNotAvailable, request_grant
    grant_world.conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(SubmissionNotAvailable):
            request_grant(grant_world.conn, settings=grant_world.settings, account_id=V2_ACCOUNT,
                          application_workspace_id=grant_world.ws, stage=Capability.SUBMIT, now=NOW,
                          fill_manifest=None, in_transaction=True)
    finally:
        grant_world.conn.rollback()


def test_in_transaction_requires_an_open_transaction(grant_world):
    from product.autonomy_contract import Capability
    from webapp.services.autonomy import request_grant
    plan = f.get_plan_by_hash(grant_world.conn, grant_world.plan_hash)["plan"]
    state = grant_world.world.state()
    manifest = derive_manifest(plan, binding=state.binding, confirmation_ids=fr.confirmation_ids(
        grant_world.conn, state.binding))
    with pytest.raises(RuntimeError):
        request_grant(grant_world.conn, settings=grant_world.settings, account_id=V2_ACCOUNT,
                      application_workspace_id=grant_world.ws, stage=Capability.FILL, now=NOW,
                      fill_manifest=manifest, observation=fr.target_observation(plan), in_transaction=True)
    assert fill_grants(grant_world) == 0
    assert SEARCH_WS
