from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
EXTENSION_ROOT = ROOT / "extension"
BUILD_ROOT = EXTENSION_ROOT / "dist" / "extension"


def _wait_for_extension_apis(worker, timeout_s: float = 5.0) -> None:
    """A just-started service worker can be reached before Chromium has bound
    the chrome.* extension APIs into it; wait for the binding, not a sleep."""
    deadline = time.monotonic() + timeout_s
    while not worker.evaluate("typeof chrome !== 'undefined' && !!chrome.runtime"):
        assert time.monotonic() < deadline, "extension APIs never bound in the service worker"
        time.sleep(0.05)


@pytest.fixture(scope="module", autouse=True)
def production_extension_build():
    npm = shutil.which("npm")
    assert npm is not None, "npm is required to build the production extension"
    subprocess.run([npm, "run", "build"], cwd=EXTENSION_ROOT, check=True)


def test_unpacked_production_extension_loads_with_active_service_worker(
    playwright, tmp_path
):
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
        worker = (
            context.service_workers[0]
            if context.service_workers
            else context.wait_for_event("serviceworker", timeout=5_000)
        )
        assert worker.url.endswith("/background/index.js")
        _wait_for_extension_apis(worker)
        assert worker.evaluate("chrome.runtime.getManifest().name") == (
            "JobSearch Application Handoff"
        )
        assert worker.evaluate(
            "chrome.runtime.getManifest().background.service_worker"
        ) == "background/index.js"
    finally:
        context.close()
