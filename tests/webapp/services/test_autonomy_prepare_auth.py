from __future__ import annotations

from datetime import timedelta

import pytest

from product.standing_policy import default_policy_document
from webapp.persistence.autonomy_authority import save_policy_version
from webapp.persistence.autonomy_ledger import list_decisions
from webapp.services.autonomy import AutonomyPaused
from webapp.services.autonomy_prepare_auth import authorize_prepare, material_fingerprint
from webapp.services.autonomy_providers import NoCostEvidence, ProviderSet, providers_from_app_state
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, NOW, conn  # noqa: F401
from tests.webapp.services.test_autonomy_context import seeded, settings  # noqa: F401
from tests.webapp.services.test_autonomy_decide import authorize_all


def _auth(conn, settings, ws, now=NOW):
    return authorize_prepare(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=ws, now=now)


def test_permitted_decision_is_reused_while_inputs_are_unchanged(conn, settings, seeded):
    authorize_all(conn)
    first = _auth(conn, settings, seeded)
    assert first.permitted and not first.reused
    second = _auth(conn, settings, seeded, now=NOW + timedelta(minutes=10))
    assert second.reused and second.decision_id == first.decision_id
    assert len([d for d in list_decisions(conn, seeded) if d["requested_stage"] == "PREPARE"]) == 1


def test_a_halt_and_resume_all_forces_a_fresh_decision(conn, settings, seeded):
    """Spec §2.8: halt -> explicit resume -> fresh authorization. The halted
    flag is False both before the halt and after the resume, so the material
    inputs alone cannot tell them apart."""
    from webapp.services.autonomy_controls import engage_kill_switch, resume_all
    authorize_all(conn)
    first = _auth(conn, settings, seeded)
    engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW + timedelta(minutes=1))
    resume_all(conn, account_id=ACCOUNT, actor="u", reason="go", now=NOW + timedelta(minutes=2),
               sentinel_path=settings.autonomy_sentinel_path)
    second = _auth(conn, settings, seeded, now=NOW + timedelta(minutes=3))
    assert second.permitted and not second.reused and second.decision_id != first.decision_id
    third = _auth(conn, settings, seeded, now=NOW + timedelta(minutes=4))
    assert third.reused and third.decision_id == second.decision_id  # reuse resumes after the fresh decision


def test_policy_change_invalidates_reuse(conn, settings, seeded):
    authorize_all(conn)
    first = _auth(conn, settings, seeded)
    doc = default_policy_document("Europe/London")
    doc["limits"]["fill_per_day"] = 7
    save_policy_version(conn, account_id=ACCOUNT, doc=doc, created_by="u", now=NOW)
    second = _auth(conn, settings, seeded, now=NOW + timedelta(minutes=1))
    assert not second.reused and second.decision_id != first.decision_id


def test_validity_horizon_forces_fresh_evaluation_after_local_midnight(conn, settings, seeded):
    authorize_all(conn)
    first = _auth(conn, settings, seeded)
    later = _auth(conn, settings, seeded, now=NOW + timedelta(hours=13))  # past Europe/London midnight
    assert not later.reused and later.decision_id != first.decision_id


def test_horizon_is_the_earliest_temporal_boundary_not_just_midnight():
    from datetime import timedelta
    from product.autonomy_contract import AnswerCandidate, Reach, RepresentationRequirement
    from tests.product.autonomy_fixtures import make_ctx
    from webapp.services.autonomy_prepare_auth import validity_horizon
    ctx = make_ctx()
    assert validity_horizon(NOW, ctx) is not None  # next local midnight
    expiring = AnswerCandidate(approved_answer_id="a", subject="employment.availability_start", reach=Reach.ACCOUNT,
                               scope_id=None, context={}, confirmed_at=NOW - timedelta(days=30) + timedelta(hours=1),
                               basis_kind="USER_ASSERTION", basis_hash_at_approval=None, basis_hash_current=None,
                               contradicted=False)
    req = RepresentationRequirement(key="start", subject="employment.availability_start", required=True,
                                    evidence_available=False, candidates=(expiring,))
    assert validity_horizon(NOW, make_ctx(requirements=(req,))) == NOW + timedelta(hours=1)  # freshness beats midnight
    unknown = RepresentationRequirement(key="x", subject="not.a.subject", required=True, evidence_available=False,
                                        candidates=(expiring.__class__(**{**expiring.__dict__, "subject": "not.a.subject"}),))
    assert validity_horizon(NOW, make_ctx(requirements=(unknown,))) is None  # unreliable -> no reuse
    assert validity_horizon(NOW, make_ctx(standing_policy=None)) is None


def test_allow_none_is_never_permission(conn, settings, seeded):
    decision = _auth(conn, settings, seeded)  # no authority configured -> NONE
    assert decision.permitted is False and decision.effective_capability == "NONE"


def test_pause_raises_and_records_nothing(conn, settings, seeded):
    from webapp.services.autonomy_controls import pause
    authorize_all(conn)
    pause(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id=seeded, actor="u", reason="r", now=NOW)
    with pytest.raises(AutonomyPaused):
        _auth(conn, settings, seeded)
    assert list_decisions(conn, seeded) == []


def test_material_fingerprint_ignores_now_and_run_id():
    base = {"now": "2026-09-24T12:00:00.000000+00:00", "run_id": None, "account_id": "a", "x": 1}
    assert material_fingerprint(base) == material_fingerprint({**base, "now": "2027-01-01T00:00:00.000000+00:00",
                                                               "run_id": "run_9"})
    assert material_fingerprint(base) != material_fingerprint({**base, "x": 2})


def test_provider_set_honours_app_state_overrides():
    class State:
        job_understanding_provider = "U"
        semantic_adapter = "S"
        application_intelligence_provider = "I"
    assert providers_from_app_state(State()) == ProviderSet("U", "S", "I")
    from decimal import Decimal
    assert NoCostEvidence().actual_cost("FIT", reserved=Decimal("1")) is None
