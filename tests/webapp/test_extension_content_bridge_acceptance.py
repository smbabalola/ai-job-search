from __future__ import annotations

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

from tests.webapp.fixtures.application_material import completion_ready_pack_payload
from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.artifacts import save_artifact
from webapp.persistence.db import connect
from webapp.persistence.discovery import ingest_discovery_record
from webapp.persistence.workspaces import ensure_profile_workspace
from webapp.services.discovery import promote_discovery_candidate

ROOT = Path(__file__).parents[2]
EXTENSION_ROOT = ROOT / "extension"
BUILD_ROOT = EXTENSION_ROOT / "dist" / "extension"

# Same fixed port the built extension's manifest content_scripts entry
# and host_permissions are scoped to (see manifest.json) — the loopback
# content-bridge only ever activates on this exact origin, so the test
# server must bind here, not an ephemeral port, for the real bundle to
# actually inject.
CONTENT_BRIDGE_PORT = 8420


@pytest.fixture(scope="module", autouse=True)
def production_extension_build():
    npm = shutil.which("npm")
    assert npm is not None, "npm is required to build the production extension"
    subprocess.run([npm, "run", "build"], cwd=EXTENSION_ROOT, check=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(tmp_path):
    settings = Settings(
        db_path=tmp_path / "jobsearch.sqlite3", host="127.0.0.1", port=CONTENT_BRIDGE_PORT,
        documents_root=tmp_path / "documents",
    )
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=CONTENT_BRIDGE_PORT, log_level="warning", access_log=False,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", CONTENT_BRIDGE_PORT), timeout=0.25):
                break
        except OSError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError(
            f"Uvicorn content-bridge fixture did not start on 127.0.0.1:{CONTENT_BRIDGE_PORT} "
            "(stop any other process holding this port first)"
        )
    yield SimpleNamespace(base_url=f"http://127.0.0.1:{CONTENT_BRIDGE_PORT}", db_path=settings.db_path)
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive(), "Uvicorn content-bridge fixture did not stop cleanly"


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


def _discoverable_confirmed_workspace(db_path, *, source_url: str) -> str:
    """Builds a real discovery-origin workspace with a confirmed,
    completion-ready Application Pack — the exact eligibility state
    Task 5's Apply-with-extension CTA requires — via the real
    ingest/promote/save-artifact path, not hand-crafted rows."""
    conn = connect(db_path)
    ensure_profile_workspace(conn)
    record = {
        "schema_version": "job-source-record.v0", "source": "freehire-search",
        "source_record_id": "bridge-test-1", "source_url": source_url,
        "captured_at": "2026-08-21T09:00:00+00:00", "company": "Acme",
        "title": "Engineer", "location": "Remote", "description": "Plan work.",
        "requirements": [], "responsibilities": [], "language_requirements": [],
        "eligibility_requirements": [], "logistics_requirements": [],
    }
    candidate = ingest_discovery_record(conn, record)["candidate"]
    workspace = promote_discovery_candidate(conn, candidate["id"])["workspace"]
    save_artifact(
        conn, workspace_id=workspace["id"], artifact_type="application_pack",
        payload=completion_ready_pack_payload("bridge-test"),
    )
    conn.close()
    return workspace["id"]


def test_apply_click_stores_pending_context_and_navigates_to_target_url(
    extension_context, live_server,
):
    """The end-to-end proof of this whole task's core invariant: a real
    click on the real rendered CTA, in the real built extension, results
    in real chrome.storage.session content AND a real navigation attempt
    to the trusted target URL — never one without the other. The target
    domain's own request is aborted at the network layer (a real,
    external, third-party site) so this test never actually depends on
    boards.greenhouse.io being reachable or behaving a particular way —
    only that Chrome's navigation layer was asked to go there."""
    target_url = "https://boards.greenhouse.io/acme/jobs/1"
    workspace_id = _discoverable_confirmed_workspace(
        live_server.db_path, source_url=target_url,
    )

    page = extension_context.new_page()
    page.route(
        "https://boards.greenhouse.io/**",
        lambda route: route.fulfill(status=200, content_type="text/html", body="<html></html>"),
    )
    page.goto(f"{live_server.base_url}/workspaces/{workspace_id}")
    button = page.locator(".apply-with-extension")
    expect(button).to_be_visible()
    assert button.get_attribute("disabled") is None

    button.click()

    # The click intercepts default navigation and only navigates after
    # the background worker acknowledges persistence — wait for the
    # (stubbed) cross-origin navigation to complete.
    page.wait_for_url(target_url, timeout=5_000)
    assert page.url == target_url

    worker = (
        extension_context.service_workers[0]
        if extension_context.service_workers
        else extension_context.wait_for_event("serviceworker", timeout=5_000)
    )
    stored = worker.evaluate(
        "() => chrome.storage.session.get('handoff_pending_context')"
    )
    context = stored["handoff_pending_context"]
    assert context["workspaceId"] == workspace_id
    assert context["targetUrl"] == "https://boards.greenhouse.io/acme/jobs/1"
    assert isinstance(context["packArtifactId"], str) and context["packArtifactId"]
    assert isinstance(context["requestedAt"], (int, float))

    # No candidate data, credential, or session token anywhere in the
    # persisted pending context.
    assert set(context.keys()) == {"workspaceId", "packArtifactId", "targetUrl", "requestedAt"}
    page.close()


def test_apply_click_does_not_navigate_when_no_extension_installed(browser, live_server):
    """Without the content-bridge script present at all (an ordinary
    browser with no extension), the button's default click behavior
    must not silently navigate anywhere on its own — confirming the
    button carries no independent href/onclick navigation path outside
    the bridge's own explicit, ack-gated window.location.assign call."""
    workspace_id = _discoverable_confirmed_workspace(
        live_server.db_path, source_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    page = browser.new_page()
    try:
        page.goto(f"{live_server.base_url}/workspaces/{workspace_id}")
        button = page.locator(".apply-with-extension")
        expect(button).to_be_visible()

        button.click()
        page.wait_for_timeout(500)

        assert page.url == f"{live_server.base_url}/workspaces/{workspace_id}"
    finally:
        page.close()
