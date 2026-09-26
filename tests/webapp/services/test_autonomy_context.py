from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from product.autonomy_contract import (
    UNKNOWN, Capability, EmployerKeyStrength, IdentityStrength, Mode, ProvenanceTier, Reach,
)
from webapp.config import Settings
from webapp.persistence.application_identity import save_application_identity
from webapp.persistence.artifacts import save_artifact
from webapp.persistence.autonomy_answers import approve_answer, confirm_apply_target
from webapp.persistence.autonomy_ledger import claim_intent, try_reserve
from webapp.services import autonomy_context
from webapp.services.autonomy_context import (
    RequirementSpec, build_context, canonical_target_url, day_window, evidence_values_hash,
)
from webapp.services.autonomy_controls import enable_autonomous_preparation, set_capability
from webapp.services.workspace_view import ApplyTarget
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401

URL = "https://boards.greenhouse.io/acme/jobs/123"


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "autonomy.sqlite3", autonomy_max_capability="SUBMIT",
                    autonomy_submit_capable_adapters=("greenhouse",))


def seed_workspace(conn, *, record_id="123", company="Acme"):
    ws = make_workspace(conn, company=company)
    save_artifact(conn, workspace_id=ws, artifact_type="job_posting_snapshot", payload={
        "schema_version": "job-posting-snapshot.v0", "job_id": f"jobsrc_{record_id}", "source": "greenhouse",
        "captured_at": "2026-09-20T00:00:00Z", "company": company, "title": "Drilling Fluids Engineer",
        "location": "Aberdeen, UK", "employment_type": "Permanent", "source_url": URL,
        "requirements": [], "responsibilities": [],
    })
    save_artifact(conn, workspace_id=ws, artifact_type="job_fit_result", payload={
        "overall_score": 82.5, "verdict": {"id": "strong", "display_name": "Strong", "score": 82.5},
    })
    save_application_identity(conn, application_workspace_id=ws, source_record={
        "source": "greenhouse", "source_record_id": record_id, "source_url": URL,
        "company": company, "title": "Drilling Fluids Engineer", "location": "Aberdeen, UK",
    })
    conn.commit()
    return ws


def patch_external_reads(monkeypatch):
    """Discovery origins, ATS URL provenance and pack staleness have their own
    suites; here they are fixed so these tests exercise assembly only."""
    monkeypatch.setattr(autonomy_context, "get_search_workspace_for_application", lambda c, w: "sw_1")
    monkeypatch.setattr(autonomy_context, "resolve_apply_target",
                        lambda c, workspace_id, account_id: ApplyTarget(url=URL, provenance="discovery_verified"))
    monkeypatch.setattr(autonomy_context, "pack_readiness", lambda c, **kw: ("art_pack", True))


@pytest.fixture
def seeded(conn, monkeypatch):
    patch_external_reads(monkeypatch)
    return seed_workspace(conn)


def _ctx(conn, settings, ws, **kw):
    params = dict(settings=settings, account_id=ACCOUNT, application_workspace_id=ws,
                  requested_stage=Capability.SUBMIT, mode=Mode.LIVE, now=NOW, sentinel_present=False)
    params.update(kw)
    return build_context(conn, **params)


def test_no_configuration_means_none_and_no_policy(conn, settings, seeded):
    ctx = _ctx(conn, settings, seeded)
    assert (ctx.account_max, ctx.workspace_ceiling, ctx.standing_policy) == (Capability.NONE, Capability.NONE, None)
    assert ctx.deployment_ceiling == Capability.SUBMIT


def test_attributes_identity_and_target(conn, settings, seeded):
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    ctx = _ctx(conn, settings, seeded)
    assert ctx.attributes["fit.overall_score"] == Decimal("82.5")
    assert ctx.attributes["fit.verdict"] == "strong"
    assert ctx.attributes["job.employment_type"] == "PERMANENT"
    assert ctx.attributes["company.key"] == "name:acme" and ctx.attributes["workspace.id"] == "sw_1"
    assert ctx.identity_strength is IdentityStrength.SOURCE_RECORD and ctx.identity_key.startswith("source:")
    assert ctx.employer_key_strength is EmployerKeyStrength.NORMALIZED_NAME
    assert ctx.apply_target.provenance is ProvenanceTier.DISCOVERY_VERIFIED
    assert ctx.apply_target.adapter_submit_capable is False  # no executor observation in 6B
    assert ctx.pack_auto_confirmable is True


def test_user_confirmed_target_upgrades_only_exact_url_and_identity(conn, settings, seeded, monkeypatch):
    monkeypatch.setattr(autonomy_context, "resolve_apply_target",
                        lambda c, workspace_id, account_id: ApplyTarget(url=URL, provenance="user_supplied"))
    assert _ctx(conn, settings, seeded).apply_target.provenance is ProvenanceTier.USER_SUPPLIED
    ident = _ctx(conn, settings, seeded).identity_key
    confirm_apply_target(conn, application_workspace_id=seeded, job_identity_key=ident,
                         canonical_url=canonical_target_url(URL), confirmed_by="u", now=NOW)
    assert _ctx(conn, settings, seeded).apply_target.provenance is ProvenanceTier.USER_CONFIRMED_APPLY_TARGET
    confirm_apply_target(conn, application_workspace_id=seeded, job_identity_key=ident,
                         canonical_url=canonical_target_url(URL + "0"), confirmed_by="u", now=NOW)
    assert _ctx(conn, settings, seeded).apply_target.provenance is ProvenanceTier.USER_SUPPLIED


def test_duplicate_intent_visible(conn, settings, seeded):
    ident = _ctx(conn, settings, seeded).identity_key
    claim_intent(conn, account_id=ACCOUNT, job_identity_key=ident, application_workspace_id=seeded,
                 source="HUMAN_APPLIED", state="CONFIRMED", now=NOW)
    conn.commit()
    assert _ctx(conn, settings, seeded).existing_intent_state == "CONFIRMED"


def test_counters_use_account_timezone_day_and_canary_cap(conn, settings, seeded):
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    day, _ = day_window(NOW, "Europe/London")
    try_reserve(conn, account_id=ACCOUNT, counter_name="submit_per_day", window_key=day, limit=9, now=NOW)
    conn.commit()
    counters = {c.name: c for c in _ctx(conn, settings, seeded).counters}
    assert counters["submit_per_day"].used == 1 and counters["submit_per_day"].limit == 1  # canary cap wins
    assert counters["submit_per_employer_30d"].limit == 2


@pytest.mark.parametrize("now, day, midnight_utc", [
    # BST: local midnight is 23:00 UTC the previous day.
    (datetime(2026, 9, 24, 22, 30, tzinfo=timezone.utc), "2026-09-24", datetime(2026, 9, 24, 23, 0, tzinfo=timezone.utc)),
    (datetime(2026, 9, 24, 23, 30, tzinfo=timezone.utc), "2026-09-25", datetime(2026, 9, 25, 23, 0, tzinfo=timezone.utc)),
    # Clocks go back 2026-10-25 01:00 UTC: next midnight after that is 00:00 UTC.
    (datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc), "2026-10-25", datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)),
    # Clocks go forward 2026-03-29 01:00 UTC.
    (datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc), "2026-03-28", datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)),
    (datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc), "2026-03-29", datetime(2026, 3, 29, 23, 0, tzinfo=timezone.utc)),
])
def test_day_window_across_dst(now, day, midnight_utc):
    assert day_window(now, "Europe/London") == (day, midnight_utc)


def test_requirements_get_candidates_with_basis_and_contradiction(conn, settings, seeded):
    approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                   reach=Reach.ACCOUNT, scope_id=None, context={}, basis={"kind": "USER_ASSERTION"},
                   approved_by="u", now=NOW)
    ctx = _ctx(conn, settings, seeded, requirements=(
        RequirementSpec(key="notice", subject="employment.notice_period", required=True, evidence_available=False),))
    (req,) = ctx.requirements
    (cand,) = req.candidates
    assert cand.basis_kind == "USER_ASSERTION" and cand.contradicted is False
    assert cand.confirmed_at == NOW


def test_evidence_values_hash():
    payload = {"claims": [{"id": "ev_1", "text": "Right to work: UK"}, {"id": "ev_2", "years": 2.5}]}
    h = evidence_values_hash(payload, ["ev_1"])
    assert h and h == evidence_values_hash({"x": [{"id": "ev_1", "text": "Right to work: UK"}]}, ["ev_1"])
    assert evidence_values_hash(payload, ["ev_2"])  # floats tolerated (converted to Decimal)
    assert evidence_values_hash(payload, ["missing"]) is None
    assert evidence_values_hash(payload, ["ev_1", "missing"]) != h


# ---- fail-closed hardening ---------------------------------------------------

@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf"), "82", True, None, [82]])
def test_non_finite_or_non_numeric_score_is_unknown(raw):
    assert autonomy_context._score(raw) is UNKNOWN


def test_numeric_score_kept():
    assert autonomy_context._score(82) == 82 and autonomy_context._score(82.5) == Decimal("82.5")


def test_released_but_not_resumed_kill_switch_still_halts(conn, settings, seeded):
    from webapp.services.autonomy_controls import engage_kill_switch, release_kill_switch
    engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    release_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="x", now=NOW)
    assert _ctx(conn, settings, seeded).kill_switch_engaged is True


def test_no_policy_means_no_limits_and_gate_denies_everything(conn, settings, seeded):
    from product.autonomy_gate import evaluate_authorization
    set_capability(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT,
                   capability=Capability.SUBMIT, actor="u", now=NOW)
    set_capability(conn, account_id=ACCOUNT, scope_type="DEFAULT_WORKSPACE_CEILING", scope_id=ACCOUNT,
                   capability=Capability.SUBMIT, actor="u", now=NOW)
    ctx = _ctx(conn, settings, seeded)
    assert ctx.standing_policy is None and ctx.counters == () and ctx.budgets == ()
    assert evaluate_authorization(ctx).effective_capability == Capability.NONE


def test_superseded_answer_is_not_a_candidate(conn, settings, seeded):
    old = approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                         reach=Reach.ACCOUNT, scope_id=None, context={}, basis={"kind": "USER_ASSERTION"},
                         approved_by="u", now=NOW)
    new = approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="3 months",
                         reach=Reach.ACCOUNT, scope_id=None, context={}, basis={"kind": "USER_ASSERTION"},
                         approved_by="u", now=NOW, supersedes_id=old["id"])
    ctx = _ctx(conn, settings, seeded, requirements=(
        RequirementSpec(key="notice", subject="employment.notice_period", required=True, evidence_available=False),))
    assert [c.approved_answer_id for c in ctx.requirements[0].candidates] == [new["id"]]


def test_unknown_target_provenance_fails_rather_than_guessing(conn, settings, seeded, monkeypatch):
    monkeypatch.setattr(autonomy_context, "resolve_apply_target",
                        lambda c, workspace_id, account_id: ApplyTarget(url=URL, provenance="trust_me"))
    with pytest.raises(ValueError):
        _ctx(conn, settings, seeded)


def _notice_blocker(conn, ws):
    from webapp.persistence.application_blockers import save_application_blocker
    from webapp.persistence.artifacts import get_current_artifact
    from webapp.persistence.policy_decisions import save_policy_decision
    fit = get_current_artifact(conn, ws, "job_fit_result")
    decision = save_policy_decision(
        conn, workspace_id=ws, stage="fit", source_artifact_id=fit["id"], review_item_type="question",
        subject_key="notice", outcome="REQUIRE_USER", policy_version="v1", policy_fingerprint="fp",
        reason_code="needs_user", reason="notice period", blocking=True)
    return save_application_blocker(
        conn, workspace_id=ws, policy_decision_id=decision["id"], source_artifact_id=fit["id"], stage="fit",
        blocker_type="question", subject_key="notice", question="Notice period?", resume_stage="fit",
        allowed_scopes=["APPLICATION_ONLY"], semantic_subject_key="employment.notice_period")


def _notice_candidate(conn, settings, ws):
    ctx = _ctx(conn, settings, ws, requirements=(
        RequirementSpec(key="notice", subject="employment.notice_period", required=True, evidence_available=False),))
    (cand,) = ctx.requirements[0].candidates
    return cand


def test_agreeing_resolution_in_blocker_answer_shape_is_not_a_contradiction(conn, settings, seeded):
    from webapp.persistence.application_blockers import resolve_application_blocker
    approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                   reach=Reach.ACCOUNT, scope_id=None, context={}, basis={"kind": "USER_ASSERTION"},
                   approved_by="u", now=NOW)
    blocker = _notice_blocker(conn, seeded)
    resolve_application_blocker(conn, blocker_id=blocker["id"], request_id="r1",
                                answer_value={"type": "text", "value": "1 month"},
                                answer_scope="APPLICATION_ONLY", resolved_by="u")
    assert _notice_candidate(conn, settings, seeded).contradicted is False
    resolve_application_blocker(conn, blocker_id=blocker["id"], request_id="r2",
                                answer_value={"type": "text", "value": "3 months"},
                                answer_scope="APPLICATION_ONLY", resolved_by="u")
    assert _notice_candidate(conn, settings, seeded).contradicted is True


@pytest.mark.parametrize("order", [("1 month", "3 months"), ("3 months", "1 month")])
def test_resolutions_tied_on_timestamp_contradict_if_any_disagrees(conn, settings, seeded, monkeypatch, order):
    # Blocker resolutions pick "latest" by created_at with a random-id
    # tie-break (a pre-6B projection; 6A fixes ordering). The gate's input
    # must not depend on that tie-break: any disagreeing tied answer counts.
    from webapp.persistence import application_blockers
    approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                   reach=Reach.ACCOUNT, scope_id=None, context={}, basis={"kind": "USER_ASSERTION"},
                   approved_by="u", now=NOW)
    blocker = _notice_blocker(conn, seeded)
    monkeypatch.setattr(application_blockers, "_now", lambda: "2026-09-24T12:00:00+00:00")
    for i, value in enumerate(order):
        application_blockers.resolve_application_blocker(
            conn, blocker_id=blocker["id"], request_id=f"r{i}", answer_value={"type": "text", "value": value},
            answer_scope="APPLICATION_ONLY", resolved_by="u")
    assert _notice_candidate(conn, settings, seeded).contradicted is True
