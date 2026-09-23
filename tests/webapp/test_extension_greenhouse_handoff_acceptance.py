"""Deterministic browser E2E: a real discovery-origin, confirmed
Application Pack workspace drives the real production extension all the
way through Greenhouse-adapter probing, session association, candidate
snapshot projection, autofill, and CV/cover-letter attachment -- with NO
automatic form submission.

Everything runs through production code, unmodified:
  - the real pairing flow (chrome.storage-persisted durable credential);
  - the real content-bridge script (.apply-with-extension -> pending
    context -> cross-origin navigation);
  - the real background service worker's runAutofillOnTab, reached only
    by sending the same `popup_run_autofill` message the popup's own
    button sends -- never called directly;
  - the real Greenhouse adapter (detect/scan/classify/map/
    findAttachmentTarget);
  - the real attachment runner (MAIN-world DataTransfer file write).

The one thing this test deliberately does NOT need `activeTab` for: the
fixture lives at http://127.0.0.1:8420/test-fixtures/handoff/..., which is
already in the built extension's host_permissions (manifest.json) for the
content-bridge script's own sake. That is a separate, independent grant
from activeTab -- confirmed empirically (not merely inferred) against the
real production-built extension: chrome.tabs.get()/.query() reveal a
loopback-origin tab's .url with zero activeTab grant, while the same call
against an external ATS domain not in host_permissions correctly reveals
nothing, exactly as intended by Chrome's permission model. This test
exploits nothing new -- it exercises the same standing grant the
content-bridge already depends on to run at all.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from playwright.sync_api import expect

from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.artifacts import get_current_artifact
from webapp.persistence.db import connect
from webapp.persistence.discovery import ingest_discovery_record
from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID
from webapp.persistence.handoff import (
    find_in_progress_handoff_sessions,
    get_handoff_session,
    list_handoff_events,
)
from webapp.services.discovery import promote_discovery_candidate
from webapp.services.pipeline import refresh_profile

ROOT = Path(__file__).parents[2]
EXTENSION_ROOT = ROOT / "extension"
BUILD_ROOT = EXTENSION_ROOT / "dist" / "extension"
_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "handoff"

# Same fixed port every other extension-acceptance test binds to: the
# built extension's ServerClient hardcodes this address (see
# test_extension_pairing_acceptance.py's own comment on this point), and
# it is also the one loopback origin in manifest.json's host_permissions.
HANDOFF_PORT = 8420

TARGET_URL = (
    f"http://127.0.0.1:{HANDOFF_PORT}/test-fixtures/handoff/"
    "greenhouse_fixture.html?ref=discovery-e2e#apply"
)

CANDIDATE_NAME = "Ada Lovelace"
CANDIDATE_EMAIL = "ada.lovelace@example.com"
CANDIDATE_EMPLOYER = "Evidence Works"

# Same job-posting text test_browser_smoke.py's shared browser fixtures use
# (POSTING_TEXT) -- copied rather than imported (a private module-level
# constant in another test file would be a brittle cross-test-module
# dependency), but kept byte-for-byte identical because _UnderstandingProvider
# below extracts evidence by exact quote match against this text, and any
# drift would silently produce a different (or empty) evidence set.
POSTING_TEXT = (
    "Python is required.\n"
    "Cloud certification is required.\n"
    "Build reliable data pipelines.\n"
    "Applicants must already have the right to work in the UK.\n"
    "German would be an advantage.\n"
    "Hybrid role: two days per week in London.\n"
)


@pytest.fixture(scope="module", autouse=True)
def production_extension_build():
    npm = shutil.which("npm")
    assert npm is not None, "npm is required to build the production extension"
    subprocess.run([npm, "run", "build"], cwd=EXTENSION_ROOT, check=True)


def _write_profile_root(root: Path) -> None:
    """Evidence Profile fixture local to this test module (not imported
    from test_browser_smoke.py, to avoid cross-test-module coupling on a
    private helper). Matches the shared fixture's markdown content exactly
    -- the German-language and Python-skill claims it produces are what
    the local _SemanticAdapter below looks up by exact claim text, so
    simplifying this content would silently break that lookup. The one
    addition is \\email{} in the LaTeX CV: contact.email/phone/linkedin/
    github are parsed exclusively from LaTeX commands
    (product/profile_snapshot.py's _parse_latex_source), never from the
    markdown profile, so this is the only way to get a real,
    non-fabricated contact.email claim into build_candidate_snapshot's
    output."""
    candidate = root / ".claude/skills/job-application-assistant"
    candidate.mkdir(parents=True)
    (root / "cv").mkdir(parents=True)
    (root / "CLAUDE.md").write_text(
        """# Job Application Assistant for Ada Lovelace

## Candidate Profile

### Identity
- **Name:** Ada Lovelace
- **Location:** London hybrid
- **Languages:**

| Language | Level |
|----------|-------|
| German | Professional |

- **Status:** Employed

### Professional Experience
- **Data Engineer** (2020-01 - Present) - **Evidence Works** (London)

### Technical Skills
- **Primary:** Python
""",
        encoding="utf-8",
    )
    (candidate / "01-candidate-profile.md").write_text(
        """# Candidate Profile

## Identity
- **Name:** Ada Lovelace
- **Location:** London hybrid
- **Status:** Employed
- **Constraints:** Right to work in the UK

### Languages

| Language | Level | Notes |
|----------|-------|-------|
| German | Professional | professional use |

## Professional Experience

### Data Engineer - Evidence Works (2020-01 - Present)
London
- Built production data pipelines
- Coordinated complex engineering schedules resources risks milestones recovery actions reporting delivery planning controls stakeholder communication governance assurance oversight
- I bring evidence backed project planning experience across complex engineering operations coordinating schedules resources risks milestones recovery actions field teams leadership reporting data analysis delivery governance quality controls stakeholder communication continuous improvement operational readiness tender planning lessons learned and critical path protection

## Technical Skills

### Programming & ML
- Python
""",
        encoding="utf-8",
    )
    (root / "cv/main_example.tex").write_text(
        "\\documentclass{moderncv}"
        "\\name{Ada}{Lovelace}"
        f"\\email{{{CANDIDATE_EMAIL}}}"
        "\\begin{document}\\end{document}\n",
        encoding="utf-8",
    )


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "handoff-acceptance-secret-sentinel")
    settings = Settings(
        db_path=tmp_path / "jobsearch.sqlite3", host="127.0.0.1", port=HANDOFF_PORT,
        documents_root=tmp_path / "documents", handoff_fixtures_dir=_FIXTURES_DIR,
    )
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=HANDOFF_PORT, log_level="warning", access_log=False,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", HANDOFF_PORT), timeout=0.25):
                break
        except OSError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError(
            f"Uvicorn handoff-acceptance fixture did not start on 127.0.0.1:{HANDOFF_PORT} "
            "(stop any other process holding this port first)"
        )
    yield SimpleNamespace(base_url=f"http://127.0.0.1:{HANDOFF_PORT}", db_path=settings.db_path)
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive(), "Uvicorn handoff-acceptance fixture did not stop cleanly"


@pytest.fixture
def extension_context(playwright, tmp_path):
    extension_path = str(BUILD_ROOT.resolve())
    context = playwright.chromium.launch_persistent_context(
        str(tmp_path / "chromium-profile"),
        headless=False,
        args=[
            "--headless=new",
            f"--disable-extensions-except={extension_path}",
            f"--load-extension={extension_path}",
        ],
    )
    try:
        yield context
    finally:
        context.close()


def _service_worker(context):
    return (
        context.service_workers[0]
        if context.service_workers
        else context.wait_for_event("serviceworker", timeout=5_000)
    )


def _pair_extension(context, base_url: str) -> None:
    import re
    import urllib.request

    html = urllib.request.urlopen(f"{base_url}/pairing").read().decode()
    match = re.search(r"[A-Za-z0-9_-]{40,}", html)
    assert match, "no pairing code found on /pairing page"
    code = match.group(0)

    extension_id = _service_worker(context).url.split("/")[2]
    page = context.new_page()
    page.goto(f"chrome-extension://{extension_id}/popup.html")
    page.wait_for_selector("#app *", timeout=5_000)
    page.fill("#pairing-code", code)
    page.click("#pair-button")
    expect(page.locator("#app")).to_contain_text("Paired", timeout=5_000)
    page.close()


class _UnderstandingProvider:
    """Local equivalent of test_browser_smoke.py's private
    _UnderstandingProvider (not imported, to avoid cross-test-module
    coupling) -- extracts the same six quotes from POSTING_TEXT."""

    provider_id = "handoff-e2e-fake"
    model_id = "handoff-e2e-fixture"
    model_version = "v0"

    def extract(self, request):
        from product.job_understanding_providers import ProviderResponse as UnderstandingResponse

        quotes = (
            ("python", "requirements", "required", "Python is required."),
            ("cloud", "requirements", "required", "Cloud certification is required."),
            ("pipelines", "responsibilities", "required", "Build reliable data pipelines."),
            ("rights", "eligibility_requirements", "required", "Applicants must already have the right to work in the UK."),
            ("german", "language_requirements", "preferred", "German would be an advantage."),
            ("hybrid", "logistics_requirements", "required", "Hybrid role: two days per week in London."),
        )
        return UnderstandingResponse(payload={
            "schema_version": "job-understanding-candidate.v0",
            "items": [
                {
                    "proposal_id": f"handoff-e2e-{name}", "category": category,
                    "kind": kind, "quote": quote, "certainty": "explicit",
                }
                for name, category, kind, quote in quotes
            ],
            "suggestions": [], "ambiguous_statements": [], "warnings": [],
        })


def _claim_id(claims: list[dict], needle: str) -> str:
    return next(
        claim["id"] for claim in claims
        if needle.casefold() in str(claim.get("value", "")).casefold()
    )


def _job_id(evidence: list[dict], exact_text: str) -> str:
    return next(item["id"] for item in evidence if item["text"] == exact_text)


class _SemanticAdapter:
    """Local equivalent of test_browser_smoke.py's private
    _SemanticAdapter -- direct/functional/transferable proposals plus the
    three gates, referencing the real claim/evidence IDs the profile and
    job text above produce."""

    def propose(self, *, profile_evidence, resolved_job_evidence, active_extensions):
        evidence = resolved_job_evidence["evidence"]
        python_claim = _claim_id(profile_evidence, "Python")
        pipeline_claim = _claim_id(profile_evidence, "Built production data pipelines")
        rights_claim = _claim_id(profile_evidence, "Right to work")
        german_claim = _claim_id(profile_evidence, "German")
        location_claim = _claim_id(profile_evidence, "London hybrid")
        return {
            "matches": [
                {
                    "proposal_id": "handoff-e2e-direct",
                    "job_evidence_id": _job_id(evidence, "Python is required."),
                    "profile_evidence_ids": [python_claim],
                    "classification": "direct",
                    "rationale": "Explicit Python evidence on both sides.",
                    "confidence": "high",
                },
                {
                    "proposal_id": "handoff-e2e-functional",
                    "job_evidence_id": _job_id(evidence, "Build reliable data pipelines."),
                    "profile_evidence_ids": [pipeline_claim],
                    "classification": "functionally_equivalent",
                    "rationale": "The responsibilities align by function.",
                    "confidence": "high",
                    "functional_basis": {
                        "responsibility_alignment": [
                            "Build reliable data pipelines",
                            "Built production data pipelines",
                        ],
                        "competency_alignment": [],
                        "title_similarity_only": False,
                    },
                },
            ],
            "gates": [
                {
                    "gate_id": "eligibility", "status": "PASS",
                    "reason": "Affirmative candidate and job evidence.",
                    "job_evidence_ids": [_job_id(evidence, "Applicants must already have the right to work in the UK.")],
                    "profile_evidence_ids": [rights_claim],
                },
                {
                    "gate_id": "language", "status": "PASS",
                    "reason": "Affirmative candidate and job evidence.",
                    "job_evidence_ids": [_job_id(evidence, "German would be an advantage.")],
                    "profile_evidence_ids": [german_claim],
                },
                {
                    "gate_id": "location_logistics", "status": "PASS",
                    "reason": "Affirmative candidate and job evidence.",
                    "job_evidence_ids": [_job_id(evidence, "Hybrid role: two days per week in London.")],
                    "profile_evidence_ids": [location_claim],
                },
            ],
        }


class _ApplicationIntelligenceProvider:
    """Local equivalent of test_browser_smoke.py's private
    _ApplicationIntelligenceProvider -- produces one READY cv_bullet,
    cv_summary_line, and cover_letter_paragraph, each backed by a real
    profile claim, which is what confirm_application_pack requires for
    completion_status to reach READY."""

    provider_id = "handoff-e2e-fake"
    model_id = "handoff-e2e-fixture"
    model_version = "v0"

    def propose(self, request):
        from product.application_intelligence_providers import ProviderResponse as AIResponse

        python_claim = _claim_id(request["profile_snapshot"]["claims"], "Python")
        summary_claim = _claim_id(
            request["profile_snapshot"]["claims"], "Coordinated complex engineering schedules"
        )
        cover_claim = _claim_id(
            request["profile_snapshot"]["claims"], "I bring evidence backed project planning"
        )
        valid_atom = {
            "atom_id": "handoff-e2e-valid", "atom_kind": "candidate_fact",
            "assertion_type": "technical_skill",
            "profile_evidence_ids": [python_claim], "rendering_variant": "PLAIN",
        }
        return AIResponse(payload={"content_units": [
            {
                "unit_id": "cv-ready", "unit_type": "cv_bullet",
                "atoms": [valid_atom], "connectives": [],
            },
            {
                "unit_id": "cv-summary-ready", "unit_type": "cv_summary_line",
                "atoms": [{
                    "atom_id": "handoff-e2e-summary", "atom_kind": "candidate_fact",
                    "assertion_type": "responsibility",
                    "profile_evidence_ids": [summary_claim], "rendering_variant": "PLAIN",
                }],
                "connectives": [],
            },
            {
                "unit_id": "cv-cover-basis-ready", "unit_type": "cv_bullet",
                "atoms": [{
                    "atom_id": "handoff-e2e-cv-cover-basis", "atom_kind": "candidate_fact",
                    "assertion_type": "responsibility",
                    "profile_evidence_ids": [cover_claim], "rendering_variant": "PLAIN",
                }],
                "connectives": [],
            },
            {
                "unit_id": "cover-ready", "unit_type": "cover_letter_paragraph",
                "atoms": [{
                    "atom_id": "handoff-e2e-cover", "atom_kind": "candidate_fact",
                    "assertion_type": "responsibility",
                    "profile_evidence_ids": [cover_claim], "rendering_variant": "PLAIN",
                }],
                "connectives": [],
            },
        ]})


def _build_confirmed_pack_workspace(tmp_path, db_path) -> tuple[str, str]:
    """Real discovery -> promote -> profile-refresh -> Understanding ->
    Job Fit -> Application Intelligence -> resolve reviews -> confirm-pack
    path, driven entirely through the real service layer (webapp.services.
    pipeline / application_pack) -- the same production functions the
    workspace UI's own buttons call, just invoked directly rather than via
    page.click(), since establishing this precondition is not itself the
    subject of this test (the browser is reserved for the extension
    handoff that follows). Returns (workspace_id, pack_artifact_id)."""
    from webapp.persistence.workspaces import ensure_profile_workspace
    from webapp.services.application_pack import confirm_application_pack
    from webapp.services.http_api import record_review_decisions
    from webapp.services.pipeline import run_application_intelligence, run_job_fit, run_job_understanding

    profile_root = tmp_path / "profile"
    _write_profile_root(profile_root)

    conn = connect(db_path)
    ensure_profile_workspace(conn)
    refresh_profile(conn, root=str(profile_root))

    record = {
        "schema_version": "job-source-record.v0", "source": "airswift-search",
        "source_record_id": "handoff-e2e-1", "source_url": TARGET_URL,
        "captured_at": "2026-08-21T09:00:00+00:00", "company": "Acme Energy",
        "title": "Senior Drilling Engineer", "location": "Remote",
        "description": POSTING_TEXT,
        "requirements": [], "responsibilities": [], "language_requirements": [],
        "eligibility_requirements": [], "logistics_requirements": [],
    }
    candidate = ingest_discovery_record(conn, record)["candidate"]
    workspace = promote_discovery_candidate(conn, candidate["id"])["workspace"]
    workspace_id = workspace["id"]

    documents_root = db_path.parent / "documents"
    extensions_dir = db_path.parent / "extensions"
    extensions_dir.mkdir(exist_ok=True)

    run_job_understanding(
        conn, workspace_id, _UnderstandingProvider(), request_id="handoff-e2e-understanding",
    )
    run_job_fit(
        conn, workspace_id, _SemanticAdapter(),
        request_id="handoff-e2e-fit", active_extensions=[],
    )
    run_application_intelligence(
        conn, workspace_id, _ApplicationIntelligenceProvider(),
        request_id="handoff-e2e-intelligence",
    )

    # Enumerates pending review items the same way the workspace page
    # itself does (build_workspace_view_model's own review_items list),
    # rather than guessing the result shape -- job_fit_result's
    # functionally_equivalent/transferable matches need review decisions
    # too, not just application_intelligence_result's content units.
    from webapp.services.workspace_view import build_workspace_view_model

    view = build_workspace_view_model(conn, workspace_id, extensions_dir=extensions_dir)
    pending = [item for item in view["review_items"] if item["decision"] is None]
    decisions = [
        {
            "review_item_type": item["review_item_type"],
            "source_artifact_id": item["source_artifact_id"],
            "domain_item_id": item["domain_item_id"],
            "disposition": "acknowledged_and_proceed",
            "note": None,
        }
        for item in pending
    ]
    if decisions:
        record_review_decisions(conn, workspace_id, decisions)

    confirmed = confirm_application_pack(
        conn, workspace_id, effective_date="2026-09-18",
        documents_root=documents_root, extensions_dir=extensions_dir,
    )
    conn.close()
    return workspace_id, confirmed["artifact"]["id"]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_confirmed_pack_reaches_greenhouse_fixture_via_real_extension_with_no_submission(
    extension_context, live_server, tmp_path,
):
    workspace_id, pack_artifact_id = _build_confirmed_pack_workspace(
        tmp_path, live_server.db_path,
    )
    _pair_extension(extension_context, live_server.base_url)

    page = extension_context.new_page()
    page.goto(f"{live_server.base_url}/workspaces/{workspace_id}")
    button = page.locator(".apply-with-extension")
    expect(button).to_be_visible()
    assert button.get_attribute("disabled") is None
    assert button.get_attribute("data-target-url") == TARGET_URL

    button.click()
    page.wait_for_url(TARGET_URL, timeout=5_000)
    assert page.url == TARGET_URL

    worker = _service_worker(extension_context)
    stored = worker.evaluate("() => chrome.storage.session.get('handoff_pending_context')")
    pending_context = stored["handoff_pending_context"]
    assert pending_context["workspaceId"] == workspace_id
    assert pending_context["packArtifactId"] == pack_artifact_id
    assert pending_context["targetUrl"] == TARGET_URL
    assert set(pending_context.keys()) == {"workspaceId", "packArtifactId", "targetUrl", "requestedAt"}

    # --- pre-write baseline ---
    for input_id in [
        "job_application_first_name", "job_application_email",
        "job_application_most_recent_employer", "job_application_years_experience",
    ]:
        assert page.locator(f"#{input_id}").input_value() == ""
    assert page.locator("#job_application_disability_status").input_value() in ("", "Yes")
    assert page.locator("#job_application_resume").evaluate("el => el.files.length") == 0
    assert page.locator("#job_application_cover_letter").evaluate("el => el.files.length") == 0

    page.evaluate(
        "() => { window.__submitClicks = 0; window.__submitEvents = 0; "
        "document.getElementById('submit_app')"
        ".addEventListener('click', () => { window.__submitClicks += 1; }); "
        "document.getElementById('application_form')"
        ".addEventListener('submit', (e) => { window.__submitEvents += 1; e.preventDefault(); }); }"
    )

    # --- trigger the real production path: the same message the popup's
    # own "Run autofill on this tab" button sends, crossing the same
    # chrome.runtime.onMessage boundary. runAutofillOnTab is fire-and-
    # forget from this message handler's perspective, so sendMessage
    # completing proves nothing about autofill having finished -- poll
    # observable DOM state instead.
    #
    # The tab ID is resolved from the worker's own chrome.tabs.query({}) --
    # proven (see module docstring) to reveal the loopback fixture tab's
    # .url with zero activeTab grant. The message itself must be sent from
    # a genuine popup-page extension context, not from the worker's own
    # context: chrome.runtime.sendMessage called from the background
    # script's own execution context never reaches its own
    # onMessage listener ("Receiving end does not exist", confirmed
    # empirically) -- exactly like a real popup, which is a separate
    # context from the background service worker. Opening popup.html via
    # page.goto() (the same _open_popup pattern test_extension_pairing_
    # acceptance.py already uses) is a real, different extension context,
    # so sending from there crosses the identical message boundary the
    # popup's own click handler does. What is NOT replicated is the
    # popup's own chrome.tabs.query({active:true, currentWindow:true})
    # tab-discovery convenience -- confirmed empirically that a page
    # opened via page.goto() is itself treated as the "active tab",
    # unlike a real toolbar popup, which is not a tab at all. Supplying
    # the already-resolved tabId directly still exercises the identical
    # popup_run_autofill message and the entire unmodified
    # runAutofillOnTab path; only the popup UI's own tab-lookup
    # convenience (irrelevant to what the extension actually does with
    # the tabId once given) is bypassed.
    tab_id = worker.evaluate(
        """
        async () => {
            const tabs = await chrome.tabs.query({});
            const match = tabs.find(t => t.url && t.url.includes('greenhouse_fixture.html'));
            return match ? match.id : null;
        }
        """
    )
    assert tab_id is not None, (
        "the fixture tab's URL must be visible via chrome.tabs.query without any "
        "activeTab grant -- it is in host_permissions"
    )
    extension_id = worker.url.split("/")[2]
    popup_page = extension_context.new_page()
    popup_page.goto(f"chrome-extension://{extension_id}/popup.html")
    popup_page.wait_for_selector("#app *", timeout=5_000)
    popup_page.evaluate(
        "(tabId) => chrome.runtime.sendMessage({ type: 'popup_run_autofill', tabId })",
        tab_id,
    )
    popup_page.close()

    expect(page.locator("#job_application_first_name")).to_have_value(CANDIDATE_NAME, timeout=10_000)

    # --- field assertions: expected filled ---
    assert page.locator("#job_application_email").input_value() == CANDIDATE_EMAIL
    assert page.locator("#job_application_most_recent_employer").input_value() == CANDIDATE_EMPLOYER

    # --- field assertions: expected untouched (existing, unmodified adapter policy) ---
    assert page.locator("#job_application_years_experience").input_value() == ""
    assert page.locator("#job_application_disability_status").input_value() in ("", "Yes")

    # --- attachments: wait for both file inputs to be populated by the
    # real attachment runner, then verify against independently fetched
    # exact pinned pack documents (never the session-token-authenticated
    # endpoint -- the ordinary workspace document-render route requires
    # no session token at all) ---
    expect(page.locator("#job_application_resume")).to_have_js_property(
        "files.length", 1, timeout=10_000,
    )
    expect(page.locator("#job_application_cover_letter")).to_have_js_property(
        "files.length", 1, timeout=10_000,
    )

    cv_response = page.request.get(
        f"{live_server.base_url}/api/workspaces/{workspace_id}/application-pack/render/cv"
        f"?pack_artifact_id={pack_artifact_id}"
    )
    cover_response = page.request.get(
        f"{live_server.base_url}/api/workspaces/{workspace_id}/application-pack/render/cover_letter"
        f"?pack_artifact_id={pack_artifact_id}"
    )
    assert cv_response.status == 200
    assert cover_response.status == 200
    expected_cv_sha = _sha256(cv_response.body())
    expected_cover_sha = _sha256(cover_response.body())

    resume_file = page.locator("#job_application_resume").evaluate(
        "el => ({ name: el.files[0].name, size: el.files[0].size, type: el.files[0].type })"
    )
    cover_file = page.locator("#job_application_cover_letter").evaluate(
        "el => ({ name: el.files[0].name, size: el.files[0].size, type: el.files[0].type })"
    )
    assert resume_file["size"] > 0
    assert cover_file["size"] > 0
    assert resume_file["size"] == len(cv_response.body())
    assert cover_file["size"] == len(cover_response.body())

    resume_bytes = page.locator("#job_application_resume").evaluate(
        "async el => Array.from(new Uint8Array(await el.files[0].arrayBuffer()))"
    )
    cover_bytes = page.locator("#job_application_cover_letter").evaluate(
        "async el => Array.from(new Uint8Array(await el.files[0].arrayBuffer()))"
    )
    assert _sha256(bytes(resume_bytes)) == expected_cv_sha
    assert _sha256(bytes(cover_bytes)) == expected_cover_sha

    # --- post-handoff identity, read directly from the database via
    # existing persistence helpers -- never via the session token ---
    conn = connect(live_server.db_path)
    sessions = find_in_progress_handoff_sessions(
        conn, account_id=DEFAULT_ACCOUNT_ID, workspace_id=workspace_id, target_domain="127.0.0.1",
    )
    assert len(sessions) == 1, "exactly one handoff session for this workspace/domain"
    session = get_handoff_session(conn, sessions[0]["id"])
    assert session["workspace_id"] == workspace_id
    assert session["pack_artifact_id"] == pack_artifact_id
    assert session["target_url"] == TARGET_URL
    assert session["target_domain"] == "127.0.0.1"
    assert session["ats_adapter_id"] == "greenhouse"
    assert session["ats_adapter_version"] == "greenhouse@1"
    assert session["status"] == "in_progress"

    # Pending context is cleared only after successful session
    # association -- by this point in the flow it must be gone.
    cleared = worker.evaluate("() => chrome.storage.session.get('handoff_pending_context')")
    assert "handoff_pending_context" not in cleared or cleared.get("handoff_pending_context") is None

    # --- probe-before-write / attachment-honesty: proves the real
    # delivery pipeline (persist locally -> flush to server -> remove
    # only on acknowledgement), fixed in the Phase 1 corrective slice
    # (ChromeEventStore's serialized mutations + DurableEventQueue's
    # per-turn-fresh-snapshot flush chain + real flush triggers wired into
    # background/index.ts after routing a content-script event, after
    # routing an attachment event, and after session association). Reads
    # the server-side handoff_events table via the existing,
    # unmodified list_handoff_events persistence helper -- never via the
    # session token -- polling briefly since flush is asynchronous
    # relative to the DOM writes the earlier to_have_value()/
    # to_have_js_property() assertions already waited for.
    expected_field_keys = {
        "greenhouse:application:job_application_first_name",
        "greenhouse:application:job_application_email",
        "greenhouse:application:job_application_most_recent_employer",
    }

    def _fetch_server_events() -> list[dict]:
        return list_handoff_events(conn, session["id"])

    server_events: list[dict] = []
    for _ in range(40):
        server_events = _fetch_server_events()
        inserted_keys = {
            event["page_field_key"] for event in server_events
            if event["event_type"] == "value_inserted"
        }
        attachment_kinds = {
            json.loads(event["event_json"]).get("kind") for event in server_events
            if event["event_type"].startswith("attachment_")
        }
        if expected_field_keys <= inserted_keys and {"cv", "cover_letter"} <= attachment_kinds:
            break
        time.sleep(0.25)

    detected_sequence: dict[str, int] = {}
    inserted_sequence: dict[str, int] = {}
    attachment_by_kind: dict[str, str] = {}
    for event in server_events:
        if event["event_type"] == "field_detected":
            detected_sequence.setdefault(event["page_field_key"], event["server_sequence"])
        elif event["event_type"] == "value_inserted":
            inserted_sequence.setdefault(event["page_field_key"], event["server_sequence"])
        elif event["event_type"].startswith("attachment_"):
            kind = json.loads(event["event_json"]).get("kind")
            if kind:
                attachment_by_kind[kind] = event["event_type"]

    for page_field_key in expected_field_keys:
        assert page_field_key in detected_sequence, (
            f"{page_field_key}: expected a field_detected event on the server"
        )
        assert page_field_key in inserted_sequence, (
            f"{page_field_key}: expected a value_inserted event on the server"
        )
        assert detected_sequence[page_field_key] < inserted_sequence[page_field_key], (
            f"{page_field_key}: field_detected must precede value_inserted"
        )

    assert attachment_by_kind.get("cv") == "attachment_selected"
    assert attachment_by_kind.get("cover_letter") == "attachment_selected"

    # Local queue assertion: once every event the server needed to
    # acknowledge has actually been acknowledged, the local durable queue
    # must eventually be empty -- proving the "remove only after
    # acknowledgement" half of the pipeline, not just "delivery happened."
    local_queue: list[dict] = []
    for _ in range(40):
        local_queue = worker.evaluate(
            "async () => (await chrome.storage.local.get('handoff_event_queue'))['handoff_event_queue'] ?? []"
        )
        if not local_queue:
            break
        time.sleep(0.25)
    assert local_queue == [], f"expected the local event queue to drain after acknowledgement, found {local_queue}"

    # --- zero automatic submission ---
    assert page.evaluate("() => window.__submitClicks") == 0
    assert page.evaluate("() => window.__submitEvents") == 0
    assert page.url == TARGET_URL
    session_after = get_handoff_session(conn, session["id"])
    assert session_after["status"] == "in_progress"
    conn.close()
