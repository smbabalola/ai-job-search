"""Regressions for the independent review of 294077c (one test group per
finding). Each pins an approved-spec behaviour the earlier suite missed."""
from __future__ import annotations

import dataclasses
import hashlib
import random
import time
from datetime import datetime, timedelta, timezone

import pytest

from product.autonomy_contract import Capability
from webapp.persistence import autonomy_prepare as ap
from webapp.persistence.artifacts import get_current_artifact
from webapp.persistence.db import connect
from webapp.services import autonomy_scheduler as sched
from webapp.services.autonomy_providers import ProviderSet
from tests.webapp.services.autonomy_6c_fixtures import (  # noqa: F401
    ACCOUNT, NOW, add_fit, conn, discover, enable_prepare, fresh_fits, make_workspace, portal_job,
    prepared_chain, ready_chain, settings_6c,
)
from tests.webapp.services.test_autonomy_scheduler import (  # noqa: F401
    Clock, ENVELOPES, _chain_world, attempts, eligible, tick, world,
)

SW = "search_default"


def _rows(conn, ws):
    return [r for r in ap.attempt_rows(conn, "APPLICATION", ws) if r["event"] != "STARTED"]


# ---- 1. lease fencing of every DB-visible result + hard timeout -------------------

def test_a_provider_that_outlives_its_lease_commits_no_result(world):
    conn, ws, settings, providers, clock = world
    providers.understanding.advance = timedelta(seconds=settings.autonomy_step_timeout
                                                + settings.autonomy_lease_margin + 5)
    tick(conn, settings, providers, clock)
    assert get_current_artifact(conn, ws, "job_understanding_result") is None  # the late result never became current
    assert attempts(conn, ws) == []
    providers.understanding.advance = None
    clock.now += timedelta(seconds=1)
    tick(conn, settings, providers, clock)  # recovery abandons, then the step runs afresh under a live lease
    assert ("UNDERSTAND", "ABANDONED") in attempts(conn, ws) and ("UNDERSTAND", "SUCCEEDED") in attempts(conn, ws)
    assert providers.understanding.calls == 2


def test_the_hard_step_timeout_is_enforced_and_a_late_commit_is_refused(world):
    conn, ws, settings, providers, clock = world
    short = dataclasses.replace(settings, autonomy_step_timeout=0.3)
    providers.understanding.during = lambda: time.sleep(1.2)
    started = time.monotonic()
    tick(conn, short, providers, clock)
    assert time.monotonic() - started < 1.0  # the scheduler did not wait for the slow call
    [row] = _rows(conn, ws)
    assert (row["step_kind"], row["event"], row["error_class"]) == ("UNDERSTAND", "FAILED", "TRANSIENT")
    time.sleep(1.5)  # the abandoned call finishes and tries to commit
    conn.rollback()
    assert get_current_artifact(conn, ws, "job_understanding_result") is None


@pytest.fixture
def candidates(conn, tmp_path, monkeypatch):
    fresh_fits(monkeypatch)
    enable_prepare(conn)
    return settings_6c(tmp_path)


def _screened(conn, settings, record_id):
    from webapp.services import autonomy_candidates as ac
    cid = discover(conn, [portal_job(record_id)])["candidate_ids"][0]
    add_fit(conn, cid)
    ctx = ac.build_candidate_context(conn, settings=settings, account_id=ACCOUNT, search_workspace_id=SW,
                                     candidate_id=cid, now=NOW)
    ac.screen_candidate(conn, ctx=ctx, now=NOW)
    ap.wake(conn, queue="CANDIDATE", item_id=cid, now=NOW)
    conn.commit()
    return cid


def test_a_stale_promoter_cannot_promote(conn, candidates, monkeypatch):
    from webapp.services import autonomy_candidates as ac
    cid = _screened(conn, candidates, "stale-1")
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    real = ac.candidate_next_action

    def then_lose_the_lease(c, ctx):
        action = real(c, ctx)
        other = connect(db)  # another worker takes the item over (e.g. after expiry)
        other.execute("UPDATE autonomy_candidate_queue SET lease_holder = 'w2', "
                      "lease_generation = lease_generation + 1, lease_expires_at = '2099-01-01T00:00:00.000000+00:00' "
                      "WHERE candidate_id = ?", (cid,))
        other.commit()
        other.close()
        return action
    monkeypatch.setattr(ac, "candidate_next_action", then_lose_the_lease)
    sched.run_tick(conn, settings=candidates, providers=ProviderSet(None, None, None), now=NOW,
                   rng=random.Random(1), worker_id="w1")
    assert ap.promotion_for_candidate(conn, cid) is None
    assert conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 0 or \
        conn.execute("SELECT COUNT(*) FROM autonomy_enrolments").fetchone()[0] == 0
    assert conn.execute("SELECT lease_holder FROM autonomy_candidate_queue WHERE candidate_id = ?",
                        (cid,)).fetchone()[0] == "w2"


# ---- 2. the enabled document path --------------------------------------------------

def test_cv_v2_enabled_needs_document_selection_and_never_confirms_v1(ready_chain):
    conn, ws, settings = ready_chain
    s = dataclasses.replace(_chain_world(conn, ws, settings), cv_quality_v2_enabled=True)
    clock = Clock(NOW)
    for _ in range(3):
        tick(conn, s, ProviderSet(None, None, None), clock)
        clock.now += timedelta(seconds=1)
    assert [r["step_kind"] for r in ap.attempt_rows(conn, "APPLICATION", ws)] == []
    assert [(n["kind"], n["detail"]["reason"]) for n in ap.open_notifications(conn, ACCOUNT)] == [
        ("NEEDS_USER", "document_selection_required")]
    assert conn.execute("SELECT COUNT(*) FROM workflow_events WHERE workspace_id = ? AND new_status = 'drafted'",
                        (ws,)).fetchone()[0] == 0


def test_system_gate4_refuses_when_cv_v2_is_enabled(ready_chain):
    from webapp.services.autonomy_prepare import prepare_snapshot, system_gate4
    from webapp.services.autonomy_prepare_auth import authorize_prepare
    from webapp.services.pipeline import PipelineError
    conn, ws, settings = ready_chain
    s = dataclasses.replace(_chain_world(conn, ws, settings), cv_quality_v2_enabled=True)
    auth = authorize_prepare(conn, settings=s, account_id=ACCOUNT, application_workspace_id=ws, now=NOW)
    _, detail = prepare_snapshot(conn, settings=s, account_id=ACCOUNT, application_workspace_id=ws)
    with pytest.raises(PipelineError):
        system_gate4(conn, settings=s, account_id=ACCOUNT, application_workspace_id=ws, authorization=auth,
                     expected_revision=detail["pack_revision"], now=NOW)
    assert get_current_artifact(conn, ws, "application_pack") is None


def test_pack_revision_understands_the_v2_pack_shape():
    from product.prepare_steps import pack_revision
    from webapp.services.autonomy_prepare import _revision_of_pack
    sources = {"profile_snapshot": {"content_id": "p"}, "job_fit_result": {"content_id": "f"},
               "application_intelligence_result": {"content_id": "i"}}
    v2 = {"schema_version": "application-pack.v2",
          "generation_basis": {"reviewed_application_pack": {"source_artifacts": sources}}}
    assert _revision_of_pack(v2) == pack_revision("p", "f", "i") == _revision_of_pack({"source_artifacts": sources})


# ---- 3. local-step refusals are not invented provider timeouts ---------------------

@pytest.mark.parametrize("kind", ["state_changed", "integrity", "plain"])
def test_gate4_refusals_are_classified_truthfully(ready_chain, monkeypatch, kind):
    from webapp.services import autonomy_prepare as svc
    from webapp.services.autonomy_inbox import RetryNotEligible, retry_failure
    from webapp.services.pipeline import PipelineError
    conn, ws, settings = ready_chain
    s = _chain_world(conn, ws, settings)
    errors = {"state_changed": lambda: svc.StepStateChanged("revision_changed"),
              "integrity": lambda: svc.StepIntegrityError("system_basis:u1"),
              "plain": lambda: PipelineError("refused")}

    def refuse(*a, **k):
        raise errors[kind]()
    monkeypatch.setattr(sched, "system_gate4", refuse)
    clock = Clock(NOW)
    tick(conn, s, ProviderSet(None, None, None), clock)
    [row] = _rows(conn, ws)
    assert row["event"] == "FAILED" and row["error_class"] != "TRANSIENT"
    if kind == "state_changed":
        assert (row["error_class"], row["error_code"]) == (None, "state_changed")
        assert eligible(conn, ws) is not None and ap.open_notifications(conn, ACCOUNT) == []
    else:
        assert row["error_class"] == "INTERNAL" and eligible(conn, ws) is None
        assert [n["kind"] for n in ap.open_notifications(conn, ACCOUNT)] == ["OPERATIONAL_ERROR"]
        with pytest.raises(RetryNotEligible):
            retry_failure(conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=ws, step_kind="GATE4",
                          actor="u", now=NOW)


def test_a_persistent_state_change_escalates_instead_of_looping(ready_chain, monkeypatch):
    from webapp.services import autonomy_prepare as svc
    conn, ws, settings = ready_chain
    s = _chain_world(conn, ws, settings)

    def refuse(*a, **k):
        raise svc.StepStateChanged("prepare_authority")
    monkeypatch.setattr(sched, "system_gate4", refuse)
    clock = Clock(NOW)
    for _ in range(8):
        tick(conn, s, ProviderSet(None, None, None), clock)
        clock.now += timedelta(seconds=1)
    assert len(_rows(conn, ws)) == 4
    assert [(n["kind"], n["detail"]["reason"]) for n in ap.open_notifications(conn, ACCOUNT)] == [
        ("OPERATIONAL_ERROR", "state_unsettled")]


# ---- 4. budget in promotion screening; deployment ceiling at enqueue ----------------

def test_screening_denies_temporarily_when_the_budget_is_exhausted():
    from product.candidate_promotion import ScreeningOutcome, evaluate_candidate_promotion
    from tests.product.test_candidate_promotion import ctx
    retry = datetime(2026, 9, 25, tzinfo=timezone.utc)
    assert evaluate_candidate_promotion(ctx()).outcome is ScreeningOutcome.PROMOTE
    result = evaluate_candidate_promotion(ctx(budget_available=False, budget_retry_at=retry))
    assert (result.outcome, result.reason_code, result.retry_at) == (ScreeningOutcome.DENY_TEMPORARY, "budget", retry)


@pytest.mark.parametrize("ceiling,expected", [(Capability.NONE, 0), (Capability.PREPARE, 1)])
def test_candidate_enqueue_respects_the_deployment_ceiling(conn, ceiling, expected):
    enable_prepare(conn)
    discover(conn, [portal_job("dc-1")], deployment_ceiling=ceiling)
    assert conn.execute("SELECT COUNT(*) FROM autonomy_candidate_queue").fetchone()[0] == expected


# ---- 5. monotonic review ordering; reconciliation only when the condition is gone ---

def test_the_latest_review_decision_is_the_last_inserted_not_the_latest_timestamp(conn):
    from webapp.persistence.artifacts import save_artifact
    from webapp.persistence.review import list_review_decisions
    ws = make_workspace(conn)
    art = save_artifact(conn, workspace_id=ws, artifact_type="job_fit_result", payload={"x": 1})
    for decision_id, created in (("rev_first", "2026-09-24T12:00:05.000000+00:00"),
                                 ("rev_second", "2026-09-24T12:00:00.000000+00:00")):  # a clock stepped back
        conn.execute("INSERT INTO review_decisions (id, workspace_id, review_item_type, source_artifact_id, "
                     "domain_item_id, disposition, note, created_at) VALUES (?, ?, 'gate_flag', ?, 'gate:x', "
                     "'acknowledged_and_proceed', NULL, ?)", (decision_id, ws, art["id"], created))
    conn.commit()
    assert list_review_decisions(conn, ws)[0]["id"] == "rev_second"
    assert list_review_decisions(conn, ws, art["id"])[0]["id"] == "rev_second"


def test_a_wake_alone_does_not_resolve_a_still_valid_notification(world):
    conn, ws, settings, providers, clock = world
    for _ in range(4):
        tick(conn, settings, providers, clock)
        clock.now += timedelta(seconds=1)
    [note] = ap.open_notifications(conn, ACCOUNT)
    assert note["kind"] == "NEEDS_USER"
    ap.wake(conn, queue="APPLICATION", item_id=ws, now=clock())  # e.g. an unrelated policy/profile wake
    conn.commit()
    sched.run_tick(conn, settings=dataclasses.replace(settings, autonomy_scheduler_enabled=False),
                   providers=providers, now=clock(), rng=random.Random(1), worker_id="w1")  # sweeps only
    assert [n["key"] for n in ap.open_notifications(conn, ACCOUNT)] == [note["key"]]
    tick(conn, settings, providers, clock)  # re-derived: the same condition still holds
    assert [n["key"] for n in ap.open_notifications(conn, ACCOUNT)] == [note["key"]]


# ---- 6. autonomous FIT keeps the extension context ----------------------------------

def _captured_fit_extensions(conn, ws, settings, monkeypatch):
    from product.prepare_steps import StepKind
    from webapp.services import http_api
    from webapp.services.autonomy_prepare import run_paid_step
    seen = {}

    def capture(c, workspace_id, adapter, **kw):
        seen["extension_ids"] = kw["extension_ids"]
        return {"id": "a", "artifact_type": "job_fit_result", "content_id": "c"}
    monkeypatch.setattr(http_api, "fit_job", capture)
    run_paid_step(conn, settings=settings, providers=ProviderSet(None, None, None), step=StepKind.FIT,
                  account_id=ACCOUNT, application_workspace_id=ws, request_id="r")
    return seen["extension_ids"]


def test_autonomous_fit_reuses_the_current_fit_extensions(prepared_chain, monkeypatch):
    conn, ws, settings = prepared_chain
    assert _captured_fit_extensions(conn, ws, settings, monkeypatch) == ["data-transfer"]


def test_autonomous_fit_without_a_previous_fit_uses_no_extensions(world, monkeypatch):
    conn, ws, settings, _, _ = world
    assert _captured_fit_extensions(conn, ws, settings, monkeypatch) == []


# ---- 7. dossier document hashes -------------------------------------------------------

def test_dossier_carries_the_application_document_hashes(ready_chain):
    from webapp.services.archive_projection import _render_markdown
    from webapp.services.autonomy_dossier import build_dossier
    from webapp.services.autonomy_prepare import prepare_snapshot, system_gate4
    from webapp.services.autonomy_prepare_auth import authorize_prepare
    conn, ws, settings = ready_chain
    s = _chain_world(conn, ws, settings)
    auth = authorize_prepare(conn, settings=s, account_id=ACCOUNT, application_workspace_id=ws, now=NOW)
    _, detail = prepare_snapshot(conn, settings=s, account_id=ACCOUNT, application_workspace_id=ws)
    out = system_gate4(conn, settings=s, account_id=ACCOUNT, application_workspace_id=ws, authorization=auth,
                       expected_revision=detail["pack_revision"], now=NOW)
    rendered = _render_markdown(out["artifact"]["payload"], projection_id=out["artifact"]["id"])
    pack = build_dossier(conn, account_id=ACCOUNT, application_workspace_id=ws, settings=s)["pack"]
    assert pack["document_path"] == "v1"
    assert pack["document_hashes"] == {
        "application_pack_projection": "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()}


def test_v2_document_hashes_are_the_selected_files():
    from webapp.services.autonomy_dossier import document_hashes
    v2 = {"schema_version": "application-pack.v2",
          "final_documents": {"cv": {"sha256": "a" * 64}, "cover_letter": {"sha256": "b" * 64}}}
    assert document_hashes(v2, artifact_id="x") == {"cv": "sha256:" + "a" * 64, "cover_letter": "sha256:" + "b" * 64}


# ---- 8. recovery records REUSED for an exact already-produced artifact ----------------

def test_recovery_records_reused_when_the_crashed_step_already_produced_its_artifact(world, monkeypatch):
    conn, ws, settings, providers, clock = world
    real = sched._finish_paid
    monkeypatch.setattr(sched, "_finish_paid", lambda *a, **k: None)  # the worker dies after the provider commit
    tick(conn, settings, providers, clock)
    produced = get_current_artifact(conn, ws, "job_understanding_result")
    assert produced is not None and attempts(conn, ws) == []
    monkeypatch.setattr(sched, "_finish_paid", real)
    clock.now += timedelta(seconds=settings.autonomy_step_timeout + settings.autonomy_lease_margin + 1)
    tick(conn, settings, providers, clock)
    [reused] = [r for r in ap.attempt_rows(conn, "APPLICATION", ws) if r["event"] == "REUSED"]
    assert reused["step_kind"] == "UNDERSTAND"
    assert [ref["artifact_id"] for ref in reused["artifact_refs"]] == [produced["id"]]
    assert providers.understanding.calls == 1  # never recomputed
    assert conn.execute("SELECT COUNT(*) FROM limit_reservations WHERE subject_id = ? AND status = 'RESERVED'",
                        (ws,)).fetchone()[0] == 0
