"""Bundle 6C acceptance (spec §15.4, §16) on the real workflow: fake portal
runner, fake LLM providers, real services, a user-triggered discovery run,
then scheduler ticks until quiescent."""
from __future__ import annotations

import dataclasses
import random
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from webapp.persistence import autonomy_prepare as ap
from webapp.persistence.db import connect
from webapp.services import autonomy_scheduler as sched
from webapp.services.autonomy_providers import ProviderSet
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, NOW, discover, enable_prepare, portal_job

ENVELOPES = {"EVALUATE": "0.05", "UNDERSTAND": "0.05", "FIT": "0.10", "INTELLIGENCE": "0.20"}


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _policy(conn, *, budget=True, ask_company=None):
    from product.autonomy_contract import normalized_employer_key
    from product.standing_policy import default_policy_document
    from webapp.persistence.autonomy_authority import save_policy_version
    doc = default_policy_document("Europe/London")
    if budget:
        doc["limits"]["budgets"] = {"LLM": {"per_day": "5.00", "per_application": "1.00"}}
    if ask_company:
        doc["rules"] = [{"id": "ask_me", "description": "", "when": {
            "attr": "company.key", "op": "eq", "value": normalized_employer_key(ask_company)},
            "effect": {"type": "REQUIRE_USER"}, "on_unknown": {"type": "REQUIRE_USER"}}]
    save_policy_version(conn, account_id=ACCOUNT, doc=doc, created_by="u", now=NOW)


@pytest.fixture
def journey(tmp_path):
    from decimal import Decimal
    from tests.webapp.fixtures.acceptance.fixtures import completion_ready_content_units, provider_candidate
    from tests.webapp.test_full_journey_acceptance import (
        AIFake, FakeSemanticProposalAdapter, UnderstandingFake, _close, _create_workspace, _install_extension,
        _install_profile, _settings,
    )
    from webapp.app import create_app
    base = _settings(tmp_path)
    _install_extension(base)
    client = TestClient(create_app(base))
    client.__enter__()
    _install_profile(base)
    older = _create_workspace(client)  # an application that existed before autonomy was enabled
    conn = connect(base.db_path)
    enable_prepare(conn)
    _policy(conn, ask_company="Ask Me Co")
    settings = dataclasses.replace(
        base, autonomy_max_capability="PREPARE", autonomy_scheduler_enabled=True,
        autonomy_step_cost_max={k: Decimal(v) for k, v in ENVELOPES.items()})
    providers = ProviderSet(UnderstandingFake(provider_candidate()),
                            FakeSemanticProposalAdapter(canned_response={"matches": [], "gates": []}),
                            AIFake({"content_units": completion_ready_content_units()}))
    client.app.state.settings = settings  # the API sees the same deployment settings as the scheduler
    j = SimpleNamespace(client=client, conn=conn, settings=settings, providers=providers, clock=Clock(NOW),
                        older=older)
    yield j
    conn.close()
    _close(client)


def tick(j, settings=None):
    return sched.run_tick(j.conn, settings=settings or j.settings, providers=j.providers, now=j.clock(),
                          rng=random.Random(1), worker_id="w1", clock=j.clock)


def quiesce(j, settings=None, max_ticks=40):
    """Tick until a tick does nothing; returns every entry that did something."""
    seen = []
    for _ in range(max_ticks):
        report = tick(j, settings)
        j.clock.now += timedelta(seconds=1)
        active = [e for e in report.processed if e["action"] != "skipped"]  # skipped = halted, untouched
        if not active:
            return seen
        seen.extend(active)
    raise AssertionError("scheduler did not become quiescent")


def discover_and_wake(j, jobs):
    out = discover(j.conn, jobs)
    for cid in out["candidate_ids"]:
        ap.wake(j.conn, queue="CANDIDATE", item_id=cid, now=j.clock())  # discovery enqueues at wall-clock time
    j.conn.commit()
    return out


def catch_up(j):
    """API routes stamp wakes with wall-clock time; the injected test clock
    moves forward to it so API-woken items are due."""
    from datetime import datetime, timezone
    j.clock.now = max(j.clock.now, datetime.now(timezone.utc) + timedelta(seconds=1))


def zero_fill_submit(conn):
    for table in ("autonomy_grants", "submission_intents", "submission_attempts"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM autonomy_decisions WHERE requested_stage IN ('FILL', 'SUBMIT')"
                        ).fetchone()[0] == 0


def promoted_workspace(conn, cid):
    promotion = ap.promotion_for_candidate(conn, cid)
    return promotion["application_workspace_id"] if promotion else None


def answer_needs_user(j, ws):
    """The user opens the inbox entry and answers each listed judgment item on
    the application's review surface."""
    from tests.webapp.test_full_journey_acceptance import _post_decision, _review
    entry = next(e for e in j.client.get("/api/autonomy/inbox").json()["needs_answer"] if e["subject_id"] == ws)
    fit = _review(j.client, ws)["job_fit_result"]
    for item in entry["detail"]["items"]:
        item_type, item_id = item.split(":", 1)
        _post_decision(j.client, ws, source_artifact_id=fit["id"], item_type=item_type, item_id=item_id)


def test_discovery_to_system_confirmed_prepared_on_the_real_workflow(journey):
    from webapp.services.application_pack import SYSTEM_GATE4_NOTE, USER_GATE4_NOTE
    from webapp.services.pipeline import create_job_from_source_record
    from webapp.persistence.discovery import get_discovery_candidate
    j = journey
    weak = dict(portal_job("weak-1", company="Weak Co"))
    weak.pop("id")
    weak["url"] = ""
    out = discover_and_wake(j, [portal_job("g1", company="Grounded Co"), portal_job("q1", company="Ask Me Co"),
                                portal_job("d1", company="Dup Co"), weak])
    by_company = {get_discovery_candidate(j.conn, c)["company"]: c for c in out["candidate_ids"]}
    # A record with no strong identity never even becomes a candidate.
    assert set(by_company) == {"Grounded Co", "Ask Me Co", "Dup Co"}
    dup = get_discovery_candidate(j.conn, by_company["Dup Co"])
    create_job_from_source_record(j.conn, company=dup["company"], title=dup["title"],  # already applied manually
                                  source_record=dup["canonical_source_record"])
    workspaces_before = j.conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]

    quiesce(j)

    ws = promoted_workspace(j.conn, by_company["Grounded Co"])
    assert ws is not None and ap.is_enrolled(j.conn, ws)
    assert promoted_workspace(j.conn, by_company["Ask Me Co"]) is None
    assert promoted_workspace(j.conn, by_company["Dup Co"]) is None
    screening = j.conn.execute("SELECT outcome, reason_code FROM autonomy_candidate_screenings WHERE candidate_id = ? "
                               "ORDER BY seq DESC LIMIT 1", (by_company["Dup Co"],)).fetchone()
    assert tuple(screening) == ("NOT_ELIGIBLE", "existing_application")
    assert j.conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == workspaces_before + 1
    # Mechanical pack items were system-confirmed; judgment items wait for the user.
    assert j.conn.execute("SELECT COUNT(*) FROM review_decisions WHERE workspace_id = ? AND decision_provenance = "
                          "'SYSTEM_AUTO_CONFIRMED'", (ws,)).fetchone()[0] > 0
    kinds = sorted((n["kind"], n["subject_type"]) for n in ap.open_notifications(j.conn, ACCOUNT))
    assert kinds == [("CANDIDATE_QUESTION", "CANDIDATE"), ("NEEDS_USER", "APPLICATION")]
    assert j.client.get("/api/autonomy/inbox/summary").json() == {"badge": 2, "actionable": 2,
                                                                  "informational_unseen": 0}
    # The older application is untouched until enrolled.
    assert ap.attempt_rows(j.conn, "APPLICATION", j.older) == []
    assert j.conn.execute("SELECT COUNT(*) FROM autonomy_decisions WHERE application_workspace_id = ?",
                          (j.older,)).fetchone()[0] == 0

    # Judgment: NEEDS_USER -> answered from the inbox -> woken -> PREPARED.
    answer_needs_user(j, ws)
    catch_up(j)
    quiesce(j)
    note = j.conn.execute("SELECT note FROM workflow_events WHERE workspace_id = ? AND new_status = 'drafted' "
                          "ORDER BY rowid DESC LIMIT 1", (ws,)).fetchone()[0]
    assert note == SYSTEM_GATE4_NOTE and note != USER_GATE4_NOTE
    open_kinds = {(n["kind"], n["subject_id"]) for n in ap.open_notifications(j.conn, ACCOUNT)}
    assert ("PREPARED", ws) in open_kinds and ("NEEDS_USER", ws) not in open_kinds
    dossier = j.client.get(f"/api/workspaces/{ws}/autonomy/dossier").json()
    assert dossier["current_state_derived"]["next"] == "PREPARED" and dossier["pack"]["system_confirmed"] is True
    assert dossier["origin"]["promotion"]["actor_type"] == "SCHEDULER"
    assert dossier["system_review"] and dossier["attempt_history_observational"]
    page = j.client.get(f"/workspaces/{ws}/autonomy").text
    assert "Current state (derived)" in page and "Attempt history (observational)" in page
    assert j.client.get("/api/autonomy/inbox/summary").json()["actionable"] == 1  # the candidate question

    # The candidate question: the user promotes it; its own rule still asks
    # at the application level, so autonomy stops there truthfully.
    question = ap.open_candidate_exceptions(j.conn, ACCOUNT)[0]
    r = j.client.post(f"/api/autonomy/candidate-exceptions/{question['id']}/resolve", json={"resolution": "PROMOTE"})
    assert r.status_code == 200, r.text
    asked = r.json()["application_workspace_id"]
    catch_up(j)
    quiesce(j)
    assert ap.attempt_rows(j.conn, "APPLICATION", asked) == []
    assert ("NEEDS_USER", asked) in {(n["kind"], n["subject_id"]) for n in ap.open_notifications(j.conn, ACCOUNT)}

    # Enrolling the older application is what starts work on it.
    assert j.client.post(f"/api/workspaces/{j.older}/autonomy/enrol").status_code == 200
    catch_up(j)
    quiesce(j)
    assert ("UNDERSTAND", "SUCCEEDED") in [(a["step_kind"], a["event"])
                                          for a in ap.attempt_rows(j.conn, "APPLICATION", j.older)]
    zero_fill_submit(j.conn)


def test_halt_and_recovery_sequence(journey):
    from webapp.services.autonomy_controls import resume_all
    j = journey
    sentinel = j.settings.autonomy_sentinel_path
    out = discover_and_wake(j, [portal_job("h1", company="Grounded Co")])
    cid = out["candidate_ids"][0]
    tick(j)  # EVALUATE
    tick(j)  # screen
    tick(j)  # promote
    ws = promoted_workspace(j.conn, cid)
    assert ws is not None

    # Halt arrives while a step is in flight: that step finishes truthfully.
    real = j.providers.understanding
    j.providers = dataclasses.replace(j.providers, understanding=_during(real, lambda: sentinel.write_text("halt")))
    tick(j)
    steps = [(a["step_kind"], a["event"]) for a in ap.attempt_rows(j.conn, "APPLICATION", ws)]
    assert steps == [("UNDERSTAND", "STARTED"), ("UNDERSTAND", "SUCCEEDED")]
    j.providers = dataclasses.replace(j.providers, understanding=real)
    # Halt active: no new work starts.
    quiesce(j)
    assert len(ap.attempt_rows(j.conn, "APPLICATION", ws)) == 2
    # Removing the halt condition never resumes anything.
    sentinel.unlink()
    j.clock.now += timedelta(minutes=5)
    quiesce(j)
    assert len(ap.attempt_rows(j.conn, "APPLICATION", ws)) == 2
    decisions = j.conn.execute("SELECT COUNT(*) FROM autonomy_decisions WHERE application_workspace_id = ?",
                               (ws,)).fetchone()[0]
    # Explicit resume-all, then a fresh PREPARE decision, then work resumes.
    resume_all(j.conn, account_id=ACCOUNT, actor="u", reason="resume", now=j.clock(), sentinel_path=sentinel)
    quiesce(j)
    assert j.conn.execute("SELECT COUNT(*) FROM autonomy_decisions WHERE application_workspace_id = ?",
                          (ws,)).fetchone()[0] > decisions
    assert ("FIT", "SUCCEEDED") in [(a["step_kind"], a["event"]) for a in ap.attempt_rows(j.conn, "APPLICATION", ws)]
    zero_fill_submit(j.conn)


class _during:
    """Wraps a provider and runs a side effect inside each call."""

    def __init__(self, inner, effect):
        self.inner, self.effect = inner, effect

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            self.effect()
            return attr(*a, **k)
        return call


@pytest.mark.parametrize("control", ["gate_off", "ceiling_none", "kill_switch", "no_budget"])
def test_negative_controls_run_sweeps_but_no_preparation_promotion_fill_or_submit(journey, monkeypatch, control):
    from webapp.services.autonomy_controls import engage_kill_switch
    from webapp.services.autonomy_prepare import enrol
    j = journey
    settings = j.settings
    if control == "gate_off":
        settings = dataclasses.replace(settings, autonomy_scheduler_enabled=False)
    elif control == "ceiling_none":
        settings = dataclasses.replace(settings, autonomy_max_capability="NONE")
    elif control == "kill_switch":
        engage_kill_switch(j.conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    else:
        _policy(j.conn, budget=False)
    j.client.app.state.settings = settings
    enrol(j.conn, account_id=ACCOUNT, application_workspace_id=j.older, actor="u", now=NOW)
    discover_and_wake(j, [portal_job("n1", company="Grounded Co")])
    swept = []
    monkeypatch.setattr(sched, "expire_grants", lambda c, now: swept.append(now) or 0)
    for _ in range(5):
        tick(j, settings)
        j.clock.now += timedelta(seconds=1)
    assert len(swept) == 5  # reduce-only sweeps still run every tick
    assert j.conn.execute("SELECT COUNT(*) FROM autonomy_prepare_steps WHERE event = 'SUCCEEDED'").fetchone()[0] == 0
    assert j.conn.execute("SELECT COUNT(*) FROM limit_reservations").fetchone()[0] == 0
    assert j.conn.execute("SELECT COUNT(*) FROM autonomy_candidate_promotions").fetchone()[0] == 0
    zero_fill_submit(j.conn)
