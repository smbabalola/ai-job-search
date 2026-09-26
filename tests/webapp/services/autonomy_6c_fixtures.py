"""Shared world-building for Bundle 6C tests. Later tasks APPEND helpers here;
never rewrite existing ones."""
from __future__ import annotations

import pytest

from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, db_path, make_workspace  # noqa: F401


def make_screening_row(conn, *, candidate_id, outcome="PROMOTE", could_unlock=False, fingerprint="fp",
                       search_workspace_id="search_default", now=NOW):
    from webapp.persistence.autonomy_prepare import insert_screening
    return insert_screening(
        conn, account_id=ACCOUNT, search_workspace_id=search_workspace_id, candidate_id=candidate_id,
        discovery_run_id=None, discovery_fit_id=None, outcome=outcome, reason_code=outcome.lower(),
        reasons=[], require_user=[], could_unlock=could_unlock, retry_at=None, input_fingerprint=fingerprint,
        authority={"deployment": "PREPARE", "account": "PREPARE", "workspace": "PREPARE"},
        policy_version_hash=None, subject_policy_hash=None, engine_version="candidate-promotion.v1", now=now)


def build_chain(tmp_path, *, ai_units=None, decide=False):
    """A real application workspace driven through understand -> fit ->
    intelligence with the acceptance-suite fakes, returning (client, app,
    settings, workspace_id). decide=True also records USER review decisions
    for every item (the full-journey helper)."""
    from tests.webapp.fixtures.acceptance.fixtures import completion_ready_content_units
    from tests.webapp.test_full_journey_acceptance import _build_chain, _decide_current_review_surface
    client, app, settings, ws = _build_chain(tmp_path, ai_units=ai_units or completion_ready_content_units())
    if decide:
        _decide_current_review_surface(client, ws)
    return client, app, settings, ws


@pytest.fixture
def prepared_chain(tmp_path):
    from webapp.persistence.db import connect
    from tests.webapp.test_full_journey_acceptance import _close
    client, _, settings, ws = build_chain(tmp_path)
    conn = connect(settings.db_path)
    yield conn, ws, settings
    conn.close()
    _close(client)


@pytest.fixture
def ready_chain(tmp_path):
    from webapp.persistence.db import connect
    from tests.webapp.test_full_journey_acceptance import _close
    client, _, settings, ws = build_chain(tmp_path, decide=True)
    conn = connect(settings.db_path)
    yield conn, ws, settings
    conn.close()
    _close(client)


# ---- Task 10: discovery worlds -------------------------------------------------

class JobsRunner:
    """Fake portal runner: serves the given jobs once, then nothing."""

    def __init__(self, jobs):
        self.jobs, self.served = list(jobs), False

    def search(self, source, **kwargs):
        if self.served:
            return []
        self.served = True
        return self.jobs


def portal_job(record_id, *, company="Acme Drilling", title="Drilling Fluids Engineer", url=None):
    return {"id": record_id, "title": title, "company": company, "location": "Aberdeen", "date": "2026-09-20",
            "url": url or f"https://example.test/jobs/{record_id}", "description": "Mud engineering.",
            "work_mode": "onsite", "regions": ["eu"], "countries": ["GB"], "skills": []}


def discover(conn, jobs, *, deployment_ceiling=None):
    """A real, user-triggered discovery run (freehire only) with a fake runner,
    under a PREPARE deployment ceiling unless one is given."""
    from product.autonomy_contract import Capability
    from webapp.persistence.user_profile import get_current_user_profile, save_user_profile
    from webapp.services.discovery import run_discovery_search
    if get_current_user_profile(conn, "search_default", account_id=ACCOUNT) is None:
        save_user_profile(conn, {"target_roles": ["Drilling Fluids Engineer"], "locations": ["Aberdeen"],
                                 "search_terms": ["drilling fluids"], "source_preferences": ["freehire-search"],
                                 "recency_days": 7})
    return run_discovery_search(conn, JobsRunner(jobs), limit_per_source=10,
                                deployment_ceiling=Capability.PREPARE if deployment_ceiling is None
                                else deployment_ceiling)


def enable_prepare(conn, *, llm_per_day="5.00", llm_per_application="1.00", now=NOW):
    """Explicit PREPARE authority + a standing policy with an LLM budget."""
    from product.standing_policy import default_policy_document
    from webapp.persistence.autonomy_authority import save_policy_version
    from webapp.services.autonomy_controls import enable_autonomous_preparation
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=now)
    doc = default_policy_document("Europe/London")
    if llm_per_day is not None:
        doc["limits"]["budgets"] = {"LLM": {"per_day": llm_per_day, "per_application": llm_per_application}}
    save_policy_version(conn, account_id=ACCOUNT, doc=doc, created_by="u", now=now)
    return doc


def settings_6c(tmp_path, **kw):
    from decimal import Decimal
    from webapp.config import Settings
    values = dict(db_path=tmp_path / "settings.sqlite3", autonomy_max_capability="PREPARE",
                  autonomy_scheduler_enabled=True,
                  autonomy_step_cost_max={"EVALUATE": Decimal("0.05"), "UNDERSTAND": Decimal("0.05"),
                                          "FIT": Decimal("0.10"), "INTELLIGENCE": Decimal("0.20")})
    values.update(kw)
    return Settings(**values)


def add_fit(conn, candidate_id, *, score=82, verdict="strong"):
    """A stored discovery fit for the candidate (content is what screening reads)."""
    from webapp.persistence.discovery import get_discovery_candidate, save_discovery_fit
    candidate = get_discovery_candidate(conn, candidate_id)
    return save_discovery_fit(conn, candidate_id=candidate_id, occurrence_id=candidate["canonical_occurrence_id"],
                              request={"active_extensions": []},
                              result={"overall_score": score, "verdict": {"id": verdict} if verdict else None},
                              fingerprints={})


def fresh_fits(monkeypatch):
    """Discovery-fit staleness has its own suite; here a stored fit is fresh."""
    from webapp.persistence.discovery import get_current_discovery_fit
    from webapp.services import autonomy_candidates

    def state(conn, *, candidate_id, search_workspace_id, account_id):
        fit = get_current_discovery_fit(conn, candidate_id, search_workspace_id=search_workspace_id)
        return fit, fit is not None
    monkeypatch.setattr(autonomy_candidates, "_fit_state", state)
