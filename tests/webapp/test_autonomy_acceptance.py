"""Bundle 6B acceptance: on the real workflow, with shadow mode on and no
autonomy granted, the product behaves exactly as before, records shadow
decisions, and never creates executable authority."""
from __future__ import annotations

from webapp.persistence.autonomy_ledger import list_decisions
from webapp.persistence.db import connect
from tests.webapp.fixtures.acceptance.fixtures import completion_ready_content_units
from tests.webapp.test_full_journey_acceptance import _build_chain, _close, _decide_current_review_surface

AUTHORITY_TABLES = ("autonomy_grants", "limit_reservations", "submission_intents", "submission_attempts")


def _journey(tmp_path):
    client, app, settings, workspace_id = _build_chain(tmp_path, ai_units=completion_ready_content_units())
    _decide_current_review_surface(client, workspace_id)
    pack = client.post(f"/api/workspaces/{workspace_id}/application-pack",
                       json={"confirmed": True, "effective_date": "2026-08-20"})
    assert pack.status_code == 201, pack.text
    return client, settings, workspace_id


def _counts(settings):
    conn = connect(settings.db_path)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("autonomy_decisions",) + AUTHORITY_TABLES}
    finally:
        conn.close()


def test_shadow_mode_on_real_workflow_is_non_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSEARCH_AUTONOMY_SHADOW", "1")
    client, settings, workspace_id = _journey(tmp_path)
    try:
        conn = connect(settings.db_path)
        try:
            decisions = list_decisions(conn, workspace_id)
        finally:
            conn.close()
        stages = [d["requested_stage"] for d in decisions]
        assert stages.count("PREPARE") == 2 and stages[-1] == "FILL", stages  # two fits, then pack confirmation
        assert all(d["mode"] == "SHADOW" and d["grantable"] == 0 for d in decisions)
        assert all(d["effective_capability"] == "NONE" for d in decisions)  # nothing authorized
        counts = _counts(settings)
        assert all(counts[t] == 0 for t in AUTHORITY_TABLES), counts
        dossier = client.get(f"/api/workspaces/{workspace_id}/autonomy/dossier").json()
        assert len(dossier["decisions"]) == len(decisions)
    finally:
        _close(client)


def test_shadow_off_records_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSEARCH_AUTONOMY_SHADOW", raising=False)
    client, settings, _ = _journey(tmp_path)
    try:
        assert all(n == 0 for n in _counts(settings).values())
    finally:
        _close(client)
