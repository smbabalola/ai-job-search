"""Deterministic, fixture-backed browser acceptance coverage for the
multi-provider discovery journey: Profile -> Discover -> Job Fit ->
save/dismiss/promote -> Application Workspace.

Two scenarios are kept separate deliberately:

- the golden path, where all four live discovery sources return results
  (including a duplicate vacancy that must collapse via the existing
  identity-key mechanism), and
- the resilience path, where one source fails and another returns no
  results, proving that a single bad provider does not take discovery down.

No live network calls are made -- every provider is a local fake injected
via ``app.state.discovery_portal_runner``, the same seam production uses to
fall back to ``CliDiscoveryPortalRunner``. This reuses the same real
Chromium + Uvicorn harness as ``test_browser_smoke.py`` (profile writer,
job-understanding/semantic fakes, extension registry); only the discovery
runner varies per test, so a small local fixture factory builds the app
with a scripted runner instead of the shared ``live_server`` fixture's
fixed one.
"""
from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from docx import Document

from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.artifacts import get_current_artifact
from webapp.persistence.db import connect

from tests.webapp.fixtures.acceptance.fixtures import extension
from tests.webapp.test_browser_smoke import (
    POSTING_TEXT,
    _ApplicationIntelligenceProvider,
    _SemanticAdapter,
    _UnderstandingProvider,
    _click_reload,
    _confirm_pack,
    _free_port,
    _refresh_profile,
    _resolve_all_pending_reviews,
    _write_profile_root,
)


FREEHIRE = "freehire-search"
LINKEDIN = "linkedin-search"
ENERGY_JOBLINE = "energy-jobline-search"
AIRSWIFT = "airswift-search"
ALL_SOURCES = (FREEHIRE, LINKEDIN, ENERGY_JOBLINE, AIRSWIFT)


class _ScriptedDiscoveryRunner:
    """Fake ``DiscoveryPortalRunner`` returning scripted, per-source
    results instead of shelling out to the real CLI adapters. A source
    absent from both ``results`` and ``failures`` returns an empty list,
    the same as a live provider finding nothing that day.
    """

    def __init__(
        self,
        results: dict[str, list[dict[str, Any]]] | None = None,
        failures: dict[str, Exception] | None = None,
    ) -> None:
        self.results = results or {}
        self.failures = failures or {}

    def search(self, source, **kwargs):
        if source in self.failures:
            raise self.failures[source]
        return self.results.get(source, [])


@pytest.fixture
def discovery_server(tmp_path, monkeypatch):
    """Same real Chromium + Uvicorn harness as ``live_server`` in
    test_browser_smoke.py, parametrized so each test supplies its own
    scripted discovery runner instead of the shared fixture's fixed one.
    """

    def _start(runner: _ScriptedDiscoveryRunner) -> SimpleNamespace:
        profile_root = tmp_path / "profile"
        _write_profile_root(profile_root)
        extensions_dir = tmp_path / "private-extension-registry"
        extension_dir = extensions_dir / "data-transfer"
        extension_dir.mkdir(parents=True)
        (extension_dir / "extension.json").write_text(
            json.dumps(extension(conditional=True)), encoding="utf-8"
        )
        monkeypatch.setenv("OPENAI_API_KEY", "sk-golden-path-must-never-render")
        port = _free_port()
        settings = Settings(
            db_path=tmp_path / "browser.sqlite3", host="127.0.0.1", port=port,
            profile_root=str(profile_root), extensions_dir=extensions_dir,
            documents_root=tmp_path / "documents",
        )
        app = create_app(settings)
        app.state.job_understanding_provider = _UnderstandingProvider()
        app.state.semantic_adapter = _SemanticAdapter()
        app.state.application_intelligence_provider = _ApplicationIntelligenceProvider()
        app.state.discovery_portal_runner = runner
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", access_log=False,
        ))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            server.should_exit = True
            thread.join(timeout=5)
            raise RuntimeError("Uvicorn browser fixture did not start on 127.0.0.1")
        started.append((server, thread))
        return SimpleNamespace(base_url=f"http://127.0.0.1:{port}", db_path=settings.db_path)

    started: list[tuple[uvicorn.Server, threading.Thread]] = []
    yield _start
    for server, thread in started:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "Uvicorn browser fixture did not stop cleanly"


def _record(
    *,
    source: str,
    source_record_id: str,
    source_url: str,
    company: str,
    title: str,
    location: str = "London, UK",
) -> dict[str, Any]:
    """Build the *native portal detail* shape ``runner.search()`` returns
    for a given source -- i.e. what portal_result_to_source_record
    (product/discovery_sources.py) expects to adapt, not an
    already-normalized job-source-record.v0. Every source requires
    id/title/company/url; Freehire alone uses employment_type (snake_case)
    and adds work_mode/regions/countries/skills, while LinkedIn, Energy
    Jobline and Airswift use employmentType (camelCase).
    """
    if source == FREEHIRE:
        return {
            "id": source_record_id,
            "url": source_url,
            "company": company,
            "title": title,
            "location": location,
            "date": "2026-08-20",
            "work_mode": "hybrid",
            "regions": ["eu"],
            "countries": ["GB"],
            "skills": ["Python"],
            "description": POSTING_TEXT,
        }
    return {
        "id": source_record_id,
        "url": source_url,
        "company": company,
        "title": title,
        "location": location,
        "description": POSTING_TEXT,
    }


def _select_all_sources(page, server) -> None:
    # Confirmed product behaviour, not incidental setup: Job Fit evaluation
    # rejects every discovery candidate with "refresh Evidence Profile
    # before evaluating jobs" until the Evidence Profile has been refreshed
    # at least once in this session. Skipping this step doesn't surface as
    # an obvious error in the browser -- the evaluate click's JS handler
    # catches the resulting failure and never reaches window.location.reload(),
    # which looks like a Playwright navigation timeout instead.
    _refresh_profile(page, server)
    page.goto(f"{server.base_url}/user-profile", wait_until="networkidle")
    page.locator('textarea[name="target_roles"]').fill("Data Engineer")
    page.locator('textarea[name="locations"]').fill("London, UK")
    page.locator('textarea[name="search_terms"]').fill("Python data pipelines")
    page.locator('textarea[name="source_preferences"]').fill("\n".join(ALL_SOURCES))
    _click_reload(page, page.get_by_role("button", name="Save search preferences"))


def _fetch_run_status(server) -> dict[str, Any]:
    conn = connect(server.db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, source_status_json FROM discovery_runs "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "expected a discovery run to have been recorded"
    return {"status": row["status"], "source_status": json.loads(row["source_status_json"])}


def _fetch_application_origin(server, application_workspace_id: str) -> sqlite3.Row:
    conn = connect(server.db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM application_workspace_origins WHERE application_workspace_id = ?",
            (application_workspace_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "promotion must record application_workspace_origins provenance"
    return row


def _fetch_job_posting_source_url(server, workspace_id: str) -> str | None:
    conn = connect(server.db_path)
    try:
        conn.row_factory = sqlite3.Row
        artifact = get_current_artifact(conn, workspace_id, "job_posting_snapshot")
    finally:
        conn.close()
    assert artifact is not None, "promotion must create a job_posting_snapshot artifact"
    return artifact["payload"].get("source_url")


def _run_promoted_candidate_to_intelligence(page, workspace_url: str) -> None:
    """Same post-creation sequence as test_browser_smoke.py's
    _run_to_intelligence (Run Understanding -> Run Job Fit -> Run
    Application Intelligence), but starting from an already-existing
    workspace produced by discovery promotion rather than _create_job's
    manual-paste entry. run_job_understanding and run_job_fit both read the
    workspace's current job_posting_snapshot artifact directly -- the one
    promotion already created from the discovery candidate's
    canonical_source_record -- so no separate "attach job" step exists or
    is needed here.
    """
    page.goto(workspace_url, wait_until="networkidle")
    _click_reload(page, page.get_by_role("button", name="Run Understanding"))
    assert page.get_by_text("Accepted job evidence", exact=True).count() == 6
    page.locator('input[name="extension_ids"][value="data-transfer"]').check()
    _click_reload(page, page.get_by_role("button", name="Run Job Fit"))
    _click_reload(page, page.get_by_role("button", name="Run Application Intelligence"))


def test_promoted_discovery_candidate_reaches_application_pack_with_provenance_intact(
    page, discovery_server,
):
    source_url = "https://airswift.com/jobs/senior-drilling-engineer-98765"
    runner = _ScriptedDiscoveryRunner(
        results={
            AIRSWIFT: [
                _record(
                    source=AIRSWIFT, source_record_id="as-continuity-1", source_url=source_url,
                    company="Provenance Energy Co", title="Senior Drilling Engineer",
                ),
            ],
        }
    )
    server = discovery_server(runner)

    _select_all_sources(page, server)

    page.goto(f"{server.base_url}/discover", wait_until="networkidle")
    _click_reload(page, page.get_by_role("button", name="Search jobs"))

    card = page.locator('[data-candidate-id]').filter(has_text="Provenance Energy Co")
    assert card.count() == 1
    card.locator(".candidate-select").check()
    _click_reload(page, page.get_by_role("button", name="Evaluate selected").first)

    card = page.locator('[data-candidate-id]').filter(has_text="Provenance Energy Co")
    assert card.get_by_text("No invented score").is_visible()
    _click_reload(page, card.get_by_role("button", name="Save"))

    card = page.locator('[data-candidate-id]').filter(has_text="Provenance Energy Co")
    with page.expect_navigation(wait_until="networkidle"):
        card.get_by_role("button", name="Create application").click()
    assert "/workspaces/" in page.url
    workspace_url = page.url
    workspace_id = workspace_url.rsplit("/workspaces/", 1)[1].split("/")[0]
    assert page.get_by_text("Provenance Energy Co", exact=True).is_visible()
    assert page.get_by_role("heading", name="Senior Drilling Engineer").is_visible()

    # --- identity continuity, checkpoint 1: immediately after promotion ---
    origin_after_promotion = _fetch_application_origin(server, workspace_id)
    assert origin_after_promotion["discovery_candidate_id"]
    assert origin_after_promotion["discovery_occurrence_id"]
    assert _fetch_job_posting_source_url(server, workspace_id) == source_url

    _run_promoted_candidate_to_intelligence(page, workspace_url)
    _resolve_all_pending_reviews(page, "acknowledged_and_proceed")
    _confirm_pack(page)

    # --- identity continuity, checkpoint 2: after the full pipeline ---
    assert page.url == workspace_url, "pack confirmation must not navigate away from the promoted workspace"
    assert page.get_by_text("Provenance Energy Co", exact=True).is_visible()
    assert page.get_by_role("heading", name="Senior Drilling Engineer").is_visible()

    origin_after_pack = _fetch_application_origin(server, workspace_id)
    assert origin_after_pack["discovery_candidate_id"] == origin_after_promotion["discovery_candidate_id"]
    assert origin_after_pack["discovery_occurrence_id"] == origin_after_promotion["discovery_occurrence_id"]
    assert origin_after_pack["search_workspace_id"] == origin_after_promotion["search_workspace_id"]

    # The job_posting_snapshot is the same artifact run_job_understanding
    # and run_job_fit read from (webapp/services/pipeline.py) -- so this
    # re-check after Understanding/Fit/Intelligence/Pack all ran proves
    # none of those stages replaced or lost the discovery-sourced snapshot,
    # not merely that promotion once created it correctly.
    assert _fetch_job_posting_source_url(server, workspace_id) == source_url

    applications = page.request.get(f"{server.base_url}/api/workspaces").json()["workspaces"]
    matching = [item for item in applications if item["company"] == "Provenance Energy Co"]
    assert len(matching) == 1, "no second Application Workspace was created for this candidate"
    assert matching[0]["id"] == workspace_id

    cv_link = page.get_by_role("link", name="Download CV")
    cover_link = page.get_by_role("link", name="Download Cover Letter")
    cv_response = page.request.get(f"{server.base_url}{cv_link.get_attribute('href')}")
    cover_response = page.request.get(f"{server.base_url}{cover_link.get_attribute('href')}")
    assert cv_response.status == 200
    assert cover_response.status == 200

    cv_texts = [paragraph.text for paragraph in Document(BytesIO(cv_response.body())).paragraphs]
    cover_text = "\n".join(
        paragraph.text for paragraph in Document(BytesIO(cover_response.body())).paragraphs
    )
    assert cv_texts[0] == "Ada Lovelace"
    assert "Built production data pipelines" in cv_texts
    assert "Ada Lovelace" in cover_text


def test_four_provider_golden_path_dedups_evaluates_and_promotes(page, discovery_server):
    duplicate_url = "https://airswift.com/jobs/12345"
    runner = _ScriptedDiscoveryRunner(
        results={
            FREEHIRE: [
                _record(
                    source=FREEHIRE, source_record_id="fh-1", source_url=duplicate_url,
                    company="Discovery Evidence Co", title="Evidence Data Engineer",
                ),
            ],
            LINKEDIN: [
                _record(
                    source=LINKEDIN, source_record_id="li-1",
                    source_url="https://linkedin.com/jobs/view/9001",
                    company="Unrelated Energy Co", title="Unrelated Analyst",
                ),
            ],
            ENERGY_JOBLINE: [],
            AIRSWIFT: [
                _record(
                    # Same source_url as the Freehire result above: this is
                    # the same vacancy seen through a second channel, and
                    # must collapse to a single discovery candidate via the
                    # canonical_url identity key (webapp/persistence/discovery.py).
                    source=AIRSWIFT, source_record_id="as-1", source_url=duplicate_url,
                    company="Discovery Evidence Co", title="Evidence Data Engineer",
                ),
            ],
        }
    )
    server = discovery_server(runner)

    _select_all_sources(page, server)

    page.goto(f"{server.base_url}/discover", wait_until="networkidle")
    assert page.get_by_role("heading", name="Discover and rank jobs").is_visible()
    _click_reload(page, page.get_by_role("button", name="Search jobs"))

    for source in ALL_SOURCES:
        assert page.get_by_text(f"{source}:").is_visible(), f"{source} did not report a run status"

    duplicate_card = page.locator('[data-candidate-id]').filter(has_text="Discovery Evidence Co")
    assert duplicate_card.count() == 1, "the two same-URL occurrences must collapse to one candidate"
    assert duplicate_card.get_by_text("seen 2 times").is_visible()

    unrelated_card = page.locator('[data-candidate-id]').filter(has_text="Unrelated Energy Co")
    assert unrelated_card.count() == 1

    duplicate_card.locator(".candidate-select").check()
    _click_reload(page, page.get_by_role("button", name="Evaluate selected").first)

    duplicate_card = page.locator('[data-candidate-id]').filter(has_text="Discovery Evidence Co")
    assert duplicate_card.get_by_text("No invented score").is_visible()
    _click_reload(page, duplicate_card.get_by_role("button", name="Save"))

    unrelated_card = page.locator('[data-candidate-id]').filter(has_text="Unrelated Energy Co")
    _click_reload(page, unrelated_card.get_by_role("button", name="Dismiss"))

    duplicate_card = page.locator('[data-candidate-id]').filter(has_text="Discovery Evidence Co")
    with page.expect_navigation(wait_until="networkidle"):
        duplicate_card.get_by_role("button", name="Create application").click()
    assert "/workspaces/" in page.url
    workspace_id = page.url.rsplit("/workspaces/", 1)[1].split("/")[0]
    assert page.get_by_text("Discovery Evidence Co", exact=True).is_visible()
    assert page.get_by_role("heading", name="Evidence Data Engineer").is_visible()

    origin = _fetch_application_origin(server, workspace_id)
    assert origin["search_workspace_id"]
    assert origin["discovery_candidate_id"]
    assert origin["discovery_occurrence_id"]
    assert _fetch_job_posting_source_url(server, workspace_id) == duplicate_url

    page.goto(f"{server.base_url}/discover", wait_until="networkidle")
    unrelated_card = page.locator('[data-candidate-id]').filter(has_text="Unrelated Energy Co")
    assert unrelated_card.count() == 1
    assert unrelated_card.get_by_text("dismissed").is_visible()
    # Confirmed product behaviour: promotion does not remove a candidate
    # from the discovery board's active groups. It stays visible (still
    # "new"/"saved" lifecycle_status) alongside its application workspace --
    # promotion is additive, not a replacement of the discovery record.
    promoted_card = page.locator('[data-candidate-id]').filter(has_text="Discovery Evidence Co")
    assert promoted_card.count() == 1, "promoted candidate stays visible on the discovery board"

    applications = page.request.get(f"{server.base_url}/api/workspaces").json()["workspaces"]
    matching = [item for item in applications if item["company"] == "Discovery Evidence Co"]
    assert len(matching) == 1, "exactly one application workspace for the promoted candidate"
    assert not any(item["company"] == "Unrelated Energy Co" for item in applications), (
        "the dismissed, unrelated candidate must never leak into an application workspace"
    )


def test_partial_provider_failure_does_not_block_successful_candidates(page, discovery_server):
    runner = _ScriptedDiscoveryRunner(
        results={
            FREEHIRE: [
                _record(
                    source=FREEHIRE, source_record_id="fh-good", source_url="https://freehire.me/jobs/good-1",
                    company="Resilient Energy Co", title="Resilient Data Engineer",
                ),
            ],
            LINKEDIN: [
                _record(
                    source=LINKEDIN, source_record_id="li-good",
                    source_url="https://linkedin.com/jobs/view/good-2",
                    company="Second Good Co", title="Second Good Role",
                ),
            ],
            ENERGY_JOBLINE: [],
        },
        failures={AIRSWIFT: RuntimeError("airswift-search: upstream site returned 503")},
    )
    server = discovery_server(runner)

    _select_all_sources(page, server)

    page.goto(f"{server.base_url}/discover", wait_until="networkidle")
    _click_reload(page, page.get_by_role("button", name="Search jobs"))

    assert page.get_by_text("Latest search: partial").is_visible()
    assert page.get_by_text(f"{FREEHIRE}:").is_visible()
    assert page.get_by_text(f"{LINKEDIN}:").is_visible()
    assert page.get_by_text(f"{ENERGY_JOBLINE}:").is_visible()
    assert page.get_by_text(f"{AIRSWIFT}:").first.is_visible()
    assert page.get_by_text("airswift-search: upstream site returned 503").is_visible()

    run = _fetch_run_status(server)
    assert run["status"] == "partial"
    assert run["source_status"][AIRSWIFT]["status"] == "failed"
    assert run["source_status"][ENERGY_JOBLINE]["status"] == "completed"
    assert run["source_status"][ENERGY_JOBLINE]["accepted"] == 0
    assert run["source_status"][FREEHIRE]["status"] == "completed"
    assert run["source_status"][LINKEDIN]["status"] == "completed"

    good_card = page.locator('[data-candidate-id]').filter(has_text="Resilient Energy Co")
    assert good_card.count() == 1
    good_card.locator(".candidate-select").check()
    _click_reload(page, page.get_by_role("button", name="Evaluate selected").first)

    good_card = page.locator('[data-candidate-id]').filter(has_text="Resilient Energy Co")
    assert good_card.get_by_text("No invented score").is_visible()
    _click_reload(page, good_card.get_by_role("button", name="Save"))
    good_card = page.locator('[data-candidate-id]').filter(has_text="Resilient Energy Co")
    with page.expect_navigation(wait_until="networkidle"):
        good_card.get_by_role("button", name="Create application").click()
    assert "/workspaces/" in page.url
    assert page.get_by_text("Resilient Energy Co", exact=True).is_visible()
    assert page.get_by_role("heading", name="Resilient Data Engineer").is_visible()
