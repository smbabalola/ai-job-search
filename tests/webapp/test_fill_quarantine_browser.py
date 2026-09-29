"""Bundle 6D-B S2/S4 proof suite (spec §10, §22). The packed extension
(the FILL_TEST_HOOKS build: the production modules exposed on the service
worker) installs the real PRELOAD and TOTAL rulesets on a real Chrome tab.
Every probe tries to reach the recording server; blocking is proved by the
request never arriving there. PRELOAD and TOTAL are tested separately
against their own expected outcomes."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.webapp.fixtures.fill.certify import uncontained_channels
from tests.webapp.fixtures.fill.extension_worker import extension_worker
from tests.webapp.fixtures.fill.recording_server import PORT, Recorder, RunningServer

ROOT = Path(__file__).parents[2]
EXTENSION_ROOT = ROOT / "extension"
HOOK_BUILD = EXTENSION_ROOT / "dist" / "extension-test-hooks"
BASE = f"http://127.0.0.1:{PORT}"
EMPLOYER_HOST = "127.0.0.1"
EMPLOYER_ORIGIN = BASE

PRELOAD_BLOCKED = ["form_post", "fetch_post", "xhr_put", "beacon", "websocket", "sw_fetch"]
PRELOAD_ALLOWED = ["fetch_get", "image_get", "script_get", "style_get", "frame_get", "push_state_then_frame_get"]
RECORDED_AS = {  # probe -> the /record/<name> it would hit
    "form_post": "form_post", "fetch_post": "fetch_post", "xhr_put": "xhr_put", "beacon": "beacon",
    "sw_fetch": "sw_fetch", "fetch_get": "fetch_get", "image_get": "image_get", "script_get": "script_get.js",
    "style_get": "style_get", "frame_get": "frame_get", "push_state_then_frame_get": "frame_get_after_pushstate",
}


@pytest.fixture(scope="module", autouse=True)
def hook_build():
    npm = shutil.which("npm")
    assert npm is not None, "npm is required to build the extension"
    subprocess.run([npm, "run", "build"], cwd=EXTENSION_ROOT, check=True,
                   env={**os.environ, "FILL_TEST_HOOKS": "1"})


@pytest.fixture(scope="module")
def recorder():
    rec = Recorder()
    with RunningServer(rec):
        yield rec


@pytest.fixture
def context(playwright, tmp_path, recorder):
    path = str(HOOK_BUILD.resolve())
    ctx = playwright.chromium.launch_persistent_context(
        str(tmp_path / "profile"), headless=False,
        args=["--headless=new", f"--disable-extensions-except={path}", f"--load-extension={path}"])
    recorder.clear()
    try:
        yield ctx
    finally:
        ctx.close()


def _worker(ctx):
    """The EXTENSION's service worker (the probe page registers its own)."""
    return extension_worker(ctx, HOOK_BUILD)


def _tab_id(worker, url_part: str) -> int:
    tab = worker.evaluate(
        "async (part) => (await chrome.tabs.query({})).find(t => t.url && t.url.includes(part))?.id ?? null", url_part)
    assert tab is not None
    return tab


def _install(worker, kind: str, tab: int) -> str:
    return worker.evaluate(
        "async ({kind, tab, host}) => (await globalThis.__fillTest.installRuleset(kind, tab, host)).rulesetHash",
        {"kind": kind, "tab": tab, "host": EMPLOYER_HOST})


def _verify(worker, ruleset_hash: str) -> bool:
    return worker.evaluate("async (h) => globalThis.__fillTest.verifyRuleset(h)", ruleset_hash)


def _open_probe_page(ctx):
    page = ctx.new_page()
    page.goto(f"{BASE}/quarantine_probe.html")
    page.wait_for_function("window.__swReady !== undefined")
    page.evaluate("() => window.__swReady")
    page.reload()  # so the service worker controls the page
    page.wait_for_function("navigator.serviceWorker.controller !== null", timeout=10_000)
    return page


def _run(page, probes):
    for name in probes:
        page.evaluate(f"() => window.__probe[{name!r}]()")
    page.wait_for_timeout(1_000)


def test_s2_preload_blocks_state_changing_and_sockets_but_allows_get(context, recorder):
    page = _open_probe_page(context)
    worker = _worker(context)
    tab = _tab_id(worker, "quarantine_probe.html")
    # Positive control: without a quarantine the WebSocket probe DOES connect,
    # so its absence below is caused by the rules, not the environment.
    recorder.clear()
    _run(page, ["websocket"])
    assert ("connect", "probe") in recorder.ws_events, recorder.ws_events
    ruleset = _install(worker, "PRELOAD", tab)
    assert _verify(worker, ruleset)
    recorder.clear()
    _run(page, PRELOAD_BLOCKED + PRELOAD_ALLOWED)
    arrived = recorder.probes()
    assert not {RECORDED_AS[p] for p in PRELOAD_BLOCKED if p in RECORDED_AS} & arrived, arrived
    assert not [e for e in recorder.ws_events if e == ("connect", "probe")]
    # PRELOAD deliberately allows GET/HEAD (spec §10.3): the page can rebuild.
    assert {RECORDED_AS[p] for p in PRELOAD_ALLOWED} <= arrived, arrived


def test_s2_total_blocks_every_probe_and_leaves_the_extension_unaffected(context, recorder):
    page = _open_probe_page(context)
    worker = _worker(context)
    tab = _tab_id(worker, "quarantine_probe.html")
    _install(worker, "PRELOAD", tab)
    ruleset = _install(worker, "TOTAL", tab)
    assert _verify(worker, ruleset)
    recorder.clear()
    _run(page, PRELOAD_BLOCKED + PRELOAD_ALLOWED)
    assert recorder.probes() == set(), recorder.requests
    assert not [e for e in recorder.ws_events if e[0] == "connect"]
    # The extension's own server calls come from its service worker, not the tab.
    assert worker.evaluate(f"() => fetch('{BASE}/record/extension_own').then(r => r.status)") == 200
    assert recorder.probes() == {"extension_own"}
    # A top-level GET navigation is blocked too (main_frame).
    page.evaluate("() => { location.href = '/record/nav_get'; }")
    page.wait_for_timeout(1_000)
    assert "nav_get" not in recorder.probes()


def test_s2_preload_allows_top_level_get_navigation(context, recorder):
    page = _open_probe_page(context)
    worker = _worker(context)
    _install(worker, "PRELOAD", _tab_id(worker, "quarantine_probe.html"))
    recorder.clear()
    page.evaluate("() => { location.href = '/record/nav_get'; }")
    page.wait_for_timeout(1_000)
    assert "nav_get" in recorder.probes()


def test_s2_reset_reload_closes_a_document_owned_socket(context, recorder):
    page = context.new_page()
    page.goto(f"{BASE}/ws_page.html")
    page.wait_for_timeout(600)
    assert ("connect", "doc_socket") in recorder.ws_events and ("message", "doc_socket") in recorder.ws_events
    worker = _worker(context)
    _install(worker, "PRELOAD", _tab_id(worker, "ws_page.html"))
    page.reload()
    page.wait_for_timeout(1_000)
    events = list(recorder.ws_events)
    close_at = events.index(("close", "doc_socket"))
    after_close = events[close_at + 1:]
    assert ("message", "doc_socket") not in after_close  # nothing transmitted after the reset
    assert ("connect", "doc_socket") not in after_close  # the reloaded page's new socket is blocked by PRELOAD


def test_s2_window_open_is_blocked_or_detected_as_a_sibling(context, recorder):
    page = _open_probe_page(context)
    worker = _worker(context)
    tab = _tab_id(worker, "quarantine_probe.html")
    _install(worker, "TOTAL", tab)
    recorder.clear()
    popup_blocked = page.evaluate("() => window.open('/record/popup_get') === null")
    page.wait_for_timeout(800)
    if popup_blocked:
        assert "popup_get" not in recorder.probes()
    else:
        result = worker.evaluate("async ({tab, origin}) => globalThis.__fillTest.checkSiblingContainment(tab, origin)",
                                 {"tab": tab, "origin": EMPLOYER_ORIGIN})
        assert result["ok"] is False and result["reason"] == "SIBLING_EMPLOYER_CONTEXT_OPEN"


def test_s2_webtransport_handshake_under_total(context, recorder):
    """Recorded, not asserted as blocked: there is no local HTTP/3 server, so
    a blocked handshake and a refused one may be indistinguishable. The rule
    shape (webtransport in both rulesets) is asserted in the unit suite."""
    page = _open_probe_page(context)
    page.evaluate("() => window.__probe.webtransport()")
    page.wait_for_function("window.__wtResult !== 'pending'", timeout=10_000)
    unquarantined = page.evaluate("() => window.__wtResult")
    worker = _worker(context)
    _install(worker, "TOTAL", _tab_id(worker, "quarantine_probe.html"))
    page.evaluate("() => window.__probe.webtransport()")
    page.wait_for_function("window.__wtResult !== 'pending'", timeout=10_000)
    quarantined = page.evaluate("() => window.__wtResult")
    print(f"WEBTRANSPORT unquarantined={unquarantined!r} quarantined={quarantined!r}")
    assert quarantined != "ready"


def test_certification_refuses_a_service_worker_held_socket(context):
    assert uncontained_channels(context, f"{BASE}/sw_socket_page.html") == ["SW_PERSISTENT_CHANNEL"]
    assert uncontained_channels(context, f"{BASE}/quarantine_probe.html") == []
    assert uncontained_channels(context, f"{BASE}/ws_page.html") == []  # document-owned: the reset closes it


def test_s4_sibling_tab_and_cross_tab_frame_are_detected(context, recorder):
    execution = context.new_page()
    execution.goto(f"{BASE}/employer_plain.html")
    worker = _worker(context)
    tab = _tab_id(worker, "employer_plain.html")
    check = "async ({tab, origin}) => globalThis.__fillTest.checkSiblingContainment(tab, origin)"
    args = {"tab": tab, "origin": EMPLOYER_ORIGIN}
    assert worker.evaluate(check, args) == {"ok": True}

    sibling = context.new_page()
    sibling.goto(f"{BASE}/ws_page.html")
    result = worker.evaluate(check, args)
    assert result["ok"] is False and result["reason"] == "SIBLING_EMPLOYER_CONTEXT_OPEN"
    sibling.close()
    assert worker.evaluate(check, args) == {"ok": True}

    other_origin = context.new_page()  # a different origin embedding the employer origin in a frame
    other_origin.goto(f"http://localhost:{PORT}/embed_other_origin.html")
    other_origin.wait_for_selector("#employer_frame")
    other_origin.frame_locator("#employer_frame").locator("#q").wait_for()
    result = worker.evaluate(check, args)
    assert result["ok"] is False and result["reason"] == "SIBLING_EMPLOYER_CONTEXT_OPEN"
