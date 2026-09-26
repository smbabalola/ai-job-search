from __future__ import annotations

import dataclasses

import pytest

from product.prepare_steps import StepKind, next_prepare_step
from webapp.persistence import autonomy_prepare as ap
from webapp.persistence.review import SYSTEM_AUTO_CONFIRMED, save_review_decision
from webapp.services import autonomy_prepare as svc
from webapp.services.application_pack import SYSTEM_GATE4_NOTE
from webapp.services.pipeline import PipelineError
from tests.webapp.services.autonomy_6c_fixtures import (  # noqa: F401
    ACCOUNT, NOW, enable_prepare, prepared_chain, ready_chain,
)


def _authorized(conn, settings, ws):
    enable_prepare(conn)
    s = dataclasses.replace(settings, autonomy_max_capability="PREPARE", autonomy_scheduler_enabled=True)
    svc.enrol(conn, account_id=ACCOUNT, application_workspace_id=ws, actor="u", now=NOW)
    return s


def _snap(conn, settings, ws):
    return svc.prepare_snapshot(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=ws)


def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("workflow_events", "artifacts", "review_decisions")}


def _gate4(conn, settings, ws):
    from webapp.services.autonomy_prepare_auth import authorize_prepare
    auth = authorize_prepare(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=ws, now=NOW)
    _, detail = _snap(conn, settings, ws)
    return svc.system_gate4(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=ws,
                            authorization=auth, expected_revision=detail["pack_revision"], now=NOW)


def test_fresh_chain_offers_system_review_of_grounded_units_only(prepared_chain):
    conn, ws, settings = prepared_chain
    snap, detail = _snap(conn, settings, ws)
    assert snap.understanding_current and snap.fit_current and snap.intelligence_current
    assert snap.mechanically_acceptable >= 1
    assert all(i["item_type"] == "content_unit" for i in detail["mechanical"])
    assert next_prepare_step(snap).step is StepKind.SYSTEM_REVIEW


def test_system_review_writes_system_decisions_for_mechanical_items_only(prepared_chain):
    conn, ws, settings = prepared_chain
    _, detail = _snap(conn, settings, ws)
    written = svc.run_system_review(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=ws, now=NOW)
    assert written == len(detail["mechanical"])
    rows = conn.execute("SELECT review_item_type, system_basis_json FROM review_decisions WHERE workspace_id = ? "
                        "AND decision_provenance = ?", (ws, SYSTEM_AUTO_CONFIRMED)).fetchall()
    assert len(rows) == written and all(r["review_item_type"] == "content_unit" for r in rows)
    judgment = {(i["item_type"], i["item_id"]) for i in detail["judgment"]}
    decided = {(r["review_item_type"], r["domain_item_id"]) for r in conn.execute(
        "SELECT review_item_type, domain_item_id FROM review_decisions WHERE workspace_id = ?", (ws,))}
    assert not judgment & decided  # judgment items stay genuinely undecided
    snap, _ = _snap(conn, settings, ws)
    if snap.judgment_outstanding:
        assert next_prepare_step(snap).reason == "pack_review"


def test_system_review_never_overrides_or_duplicates_a_user_decision(prepared_chain):
    conn, ws, settings = prepared_chain
    _, detail = _snap(conn, settings, ws)
    target = detail["mechanical"][0]
    save_review_decision(conn, workspace_id=ws, review_item_type=target["item_type"],
                         source_artifact_id=target["source_artifact_id"], domain_item_id=target["item_id"],
                         disposition="omit_from_positioning")  # the user decided between snapshot and write
    svc.run_system_review(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=ws, now=NOW)
    rows = conn.execute("SELECT decision_provenance FROM review_decisions WHERE workspace_id = ? AND "
                        "domain_item_id = ?", (ws, target["item_id"])).fetchall()
    assert [r["decision_provenance"] for r in rows] == ["USER"]


def test_decided_chain_goes_to_gate4_then_prepared_with_system_note(ready_chain):
    conn, ws, settings = ready_chain
    s = _authorized(conn, settings, ws)
    snap, _ = _snap(conn, s, ws)
    assert next_prepare_step(snap).step is StepKind.GATE4
    out = _gate4(conn, s, ws)
    assert out["workflow_event"]["note"] == SYSTEM_GATE4_NOTE
    snap, detail = _snap(conn, s, ws)
    assert next_prepare_step(snap).kind == "PREPARED"
    assert svc.system_confirmed_revision(conn, ws) == detail["pack_revision"]


def test_submitted_application_is_done(ready_chain):
    conn, ws, settings = ready_chain
    conn.execute("UPDATE workspaces SET workflow_status = 'applied' WHERE id = ?", (ws,))
    conn.commit()
    assert next_prepare_step(_snap(conn, settings, ws)[0]).kind == "DONE"


@pytest.mark.parametrize("fault", ["authority", "paused", "halted", "unenrolled", "scheduler_off", "latch",
                                   "revision", "unresolved", "system_basis", "unsupported"])
def test_gate4_refuses_on_every_recheck_and_writes_nothing(ready_chain, monkeypatch, fault):
    from product.autonomy_contract import Capability
    from webapp.services.autonomy_controls import engage_kill_switch, pause, set_capability
    from webapp.services.autonomy_prepare_auth import authorize_prepare
    conn, ws, settings = ready_chain
    s = _authorized(conn, settings, ws)
    auth = authorize_prepare(conn, settings=s, account_id=ACCOUNT, application_workspace_id=ws, now=NOW)
    _, detail = _snap(conn, s, ws)
    revision = detail["pack_revision"]
    if fault == "authority":
        set_capability(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT,
                       capability=Capability.NONE, actor="u", now=NOW)
    elif fault == "paused":
        pause(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id=ws, actor="u", reason="r", now=NOW)
    elif fault == "halted":
        engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    elif fault == "unenrolled":
        svc.unenrol(conn, account_id=ACCOUNT, application_workspace_id=ws, actor="u", now=NOW)
    elif fault == "scheduler_off":
        s = dataclasses.replace(s, autonomy_scheduler_enabled=False)
    elif fault == "latch":
        ap.record_latch(conn, application_workspace_id=ws, pack_revision=revision, reason="EXPLICIT_REVIEW",
                        actor="u", now=NOW)
        conn.commit()
    elif fault == "revision":
        revision = "sha256:not-the-current-revision"
    elif fault == "unresolved":
        unit = next(i for i in _content_units(conn, ws))
        save_review_decision(conn, workspace_id=ws, review_item_type="content_unit",
                             source_artifact_id=unit["source_artifact_id"], domain_item_id=unit["unit_id"],
                             disposition="requires_upstream_change")
    elif fault == "system_basis":
        unit = next(i for i in _content_units(conn, ws))
        save_review_decision(conn, workspace_id=ws, review_item_type="content_unit",
                             source_artifact_id=unit["source_artifact_id"], domain_item_id=unit["unit_id"],
                             disposition="acknowledged_and_proceed", decision_provenance=SYSTEM_AUTO_CONFIRMED,
                             system_basis={"reason": "grounded_ready_unit", "item_content_hash": "sha256:wrong",
                                           "pack_revision": revision})
    elif fault == "unsupported":
        monkeypatch.setattr(svc, "grounded_claim_ids", lambda profile: frozenset())
    before = _counts(conn)
    with pytest.raises(PipelineError):
        svc.system_gate4(conn, settings=s, account_id=ACCOUNT, application_workspace_id=ws, authorization=auth,
                         expected_revision=revision, now=NOW)
    after = _counts(conn)
    assert (after["workflow_events"], after["artifacts"]) == (before["workflow_events"], before["artifacts"]), fault


def _content_units(conn, ws):
    from webapp.persistence.artifacts import get_current_artifact
    art = get_current_artifact(conn, ws, "application_intelligence_result")
    for collection in ("cv_content", "cover_letter_content"):
        for unit in art["payload"].get(collection, []):
            if unit.get("text"):
                yield {**unit, "source_artifact_id": art["id"]}


def test_explicit_review_latches_the_current_revision(prepared_chain):
    conn, ws, settings = prepared_chain
    svc.request_pack_review(conn, account_id=ACCOUNT, application_workspace_id=ws, actor="u", now=NOW)
    snap, detail = _snap(conn, settings, ws)
    assert snap.latched and snap.mechanically_acceptable == 0
    assert next_prepare_step(snap).reason == "human_review_latched"


def test_user_answer_on_an_unconfirmed_revision_does_not_latch(prepared_chain):
    conn, ws, settings = prepared_chain
    _, detail = _snap(conn, settings, ws)
    item = (detail["judgment"] or detail["mechanical"])[0]
    save_review_decision(conn, workspace_id=ws, review_item_type=item["item_type"],
                         source_artifact_id=item["source_artifact_id"], domain_item_id=item["item_id"],
                         disposition="acknowledged_and_proceed")
    svc.on_user_review_decision(conn, workspace_id=ws, account_id=ACCOUNT, now=NOW)
    assert not ap.has_latch(conn, ws, detail["pack_revision"])


def test_user_decision_after_system_confirmation_latches(ready_chain):
    conn2, ws2, settings2 = ready_chain  # system-confirmed: the user reopening it latches
    s = _authorized(conn2, settings2, ws2)
    _gate4(conn2, s, ws2)
    revision = svc.system_confirmed_revision(conn2, ws2)
    unit = next(_content_units(conn2, ws2))
    save_review_decision(conn2, workspace_id=ws2, review_item_type="content_unit",
                         source_artifact_id=unit["source_artifact_id"], domain_item_id=unit["unit_id"],
                         disposition="omit_from_positioning")
    svc.on_user_review_decision(conn2, workspace_id=ws2, account_id=ACCOUNT, now=NOW)
    assert ap.has_latch(conn2, ws2, revision)


@pytest.mark.parametrize("exc, expected", [
    (TimeoutError("slow"), "TRANSIENT"), (ConnectionError("reset"), "TRANSIENT"),
    (type("RateLimitError", (Exception,), {})("429"), "TRANSIENT"),
    (type("AuthenticationError", (Exception,), {})("no key"), "HUMAN_FIXABLE"),
    (PipelineError("refresh Evidence Profile before evaluating jobs"), "HUMAN_FIXABLE"),
    (ValueError("contract violation"), "INTERNAL"),
])
def test_classify_error(exc, expected):
    assert svc.classify_error(exc)[0].value == expected


def test_review_decisions_through_the_service_latch_a_system_confirmed_revision(ready_chain):
    from webapp.services.http_api import record_review_decisions
    conn, ws, settings = ready_chain
    s = _authorized(conn, settings, ws)
    _gate4(conn, s, ws)
    revision = svc.system_confirmed_revision(conn, ws)
    unit = next(_content_units(conn, ws))
    record_review_decisions(conn, ws, [{"review_item_type": "content_unit", "source_artifact_id":
                                        unit["source_artifact_id"], "domain_item_id": unit["unit_id"],
                                        "disposition": "omit_from_positioning"}], account_id=ACCOUNT)
    assert ap.has_latch(conn, ws, revision)


def test_manual_rerun_wakes_an_enrolled_dormant_application(ready_chain):
    import copy
    from webapp.services.http_api import generate_application_intelligence
    from tests.webapp.fixtures.acceptance.fixtures import completion_ready_content_units
    from tests.webapp.test_full_journey_acceptance import AIFake
    conn, ws, settings = ready_chain
    svc.enrol(conn, account_id=ACCOUNT, application_workspace_id=ws, actor="u", now=NOW)
    ap.set_dormant(conn, queue="APPLICATION", item_id=ws, now=NOW)
    conn.commit()
    eligible = lambda: conn.execute("SELECT next_eligible_at FROM autonomy_queue_items "  # noqa: E731
                                    "WHERE application_workspace_id = ?", (ws,)).fetchone()[0]
    assert eligible() is None
    generate_application_intelligence(conn, ws, AIFake({"content_units": copy.deepcopy(completion_ready_content_units())}),
                                      request_id="manual-rerun", account_id=ACCOUNT)
    assert eligible() is not None


def test_classify_error_follows_the_cause_chain():
    try:
        try:
            raise TimeoutError("provider timed out")
        except TimeoutError as inner:
            raise PipelineError("job understanding failed") from inner
    except PipelineError as wrapped:
        assert svc.classify_error(wrapped)[0].value == "TRANSIENT"
