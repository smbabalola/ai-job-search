"""Bundle 6E-A spikes (spec §21), on fixtures, in a real Chrome with the
packed FILL_TEST_HOOKS build. The extension's own service worker installs
the real TOTAL ruleset and the candidate allow rules and injects ISOLATED
scripts, exactly as production would. Blocking is proved by absence at the
recording servers.

- S-E1 (gate): a priority-2000 tab-scoped allow rule overrides TOTAL for
  exactly its method/type/URL; nothing else gets through.
- S-E2: getMatchedRules reports the allow rule's match for the tab.
- S-E3 (gate): an ISOLATED-world element.click() triggers (a) a native form
  POST navigation and (b) a React onSubmit XHR.
- S-E4: after the allowed POST navigation, tabs.onUpdated reports the
  confirmation page complete and a re-injected script reads its marker."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.webapp.fixtures.fill.extension_worker import extension_worker
from tests.webapp.fixtures.fill.recording_server import PORT, Recorder, RunningServer

ROOT = Path(__file__).parents[3]
EXTENSION_ROOT = ROOT / "extension"
HOOK_BUILD = EXTENSION_ROOT / "dist" / "extension-test-hooks"
BASE = f"http://127.0.0.1:{PORT}"
HOST = "127.0.0.1"
THIRD_PARTY_PORT = 8431
E1_REGEX = r"^http://127\.0\.0\.1:8420/acme/jobs/123$"
E2_REGEX = r"^http://127\.0\.0\.1:8420/acme/jobs/123/confirmation(\?.*)?$"


def _allow(rule_id, tab, methods, types, regex):
    return {"id": rule_id, "priority": 2000, "action": {"type": "allow"},
            "condition": {"tabIds": [tab], "requestMethods": methods, "resourceTypes": types, "regexFilter": regex}}


class _ThirdParty(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        _ThirdParty.hits.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module", autouse=True)
def hook_build():
    npm = shutil.which("npm")
    assert npm is not None, "npm is required to build the extension"
    subprocess.run([npm, "run", "build"], cwd=EXTENSION_ROOT, check=True,
                   env={**os.environ, "FILL_TEST_HOOKS": "1"})


@pytest.fixture(scope="module")
def recorder():
    rec = Recorder()
    third = ThreadingHTTPServer(("127.0.0.1", THIRD_PARTY_PORT), _ThirdParty)
    thread = threading.Thread(target=third.serve_forever, daemon=True)
    thread.start()
    with RunningServer(rec):
        yield rec
    third.shutdown()


@pytest.fixture
def context(playwright, tmp_path, recorder):
    path = str(HOOK_BUILD.resolve())
    ctx = playwright.chromium.launch_persistent_context(
        str(tmp_path / "profile"), headless=False,
        args=["--headless=new", f"--disable-extensions-except={path}", f"--load-extension={path}"])
    recorder.clear()
    _ThirdParty.hits.clear()
    try:
        yield ctx
    finally:
        ctx.close()


def _tab(worker, part):
    tab = worker.evaluate(
        "async (p) => (await chrome.tabs.query({})).find(t => t.url && t.url.includes(p))?.id ?? null", part)
    assert tab is not None
    return tab


def _total_plus(worker, tab, allows):
    return worker.evaluate(
        """async ({tab, host, allows}) => {
             await globalThis.__fillTest.installRuleset("TOTAL", tab, host);
             await chrome.declarativeNetRequest.updateSessionRules({ addRules: allows });
             return (await chrome.declarativeNetRequest.getSessionRules()).map(r => r.id).sort();
           }""", {"tab": tab, "host": HOST, "allows": allows})


def _isolated_click(worker, tab):
    return worker.evaluate(
        """async (tab) => (await chrome.scripting.executeScript({ target: { tabId: tab }, world: "ISOLATED",
             func: () => { const b = document.querySelector("#submit_app"); b.click(); return true; } }))[0].result""",
        tab)


def _open(ctx, name):
    page = ctx.new_page()
    page.goto(f"{BASE}/submit/{name}")
    page.wait_for_selector("#submit_app")
    return page


def test_s_e1_allow_overrides_total_only_for_the_exact_request(context, recorder):
    page = _open(context, "react_xhr.html")
    worker = extension_worker(context, HOOK_BUILD)
    tab = _tab(worker, "react_xhr.html")
    ids = _total_plus(worker, tab, [_allow(9201, tab, ["post"], ["main_frame", "xmlhttprequest"], E1_REGEX)])
    assert ids == [9111, 9121, 9201], ids
    recorder.clear()
    page.evaluate("() => { window.__probe.other_post(); window.__probe.third_party_image(); }")
    page.click("#submit_app")  # the page's own submit (the allowed request)
    page.wait_for_selector("#application_confirmation", timeout=10_000)
    page.wait_for_timeout(1_000)
    assert recorder.requests == [("POST", "submit:acme/123")], recorder.requests
    assert _ThirdParty.hits == []


def test_s_e1_allow_is_tab_scoped_so_the_service_worker_stays_blocked(context, recorder):
    page = context.new_page()
    page.goto(f"{BASE}/quarantine_probe.html")
    page.wait_for_function("window.__swReady !== undefined")
    page.evaluate("() => window.__swReady")
    page.reload()
    page.wait_for_function("navigator.serviceWorker.controller !== null", timeout=10_000)
    worker = extension_worker(context, HOOK_BUILD)
    tab = _tab(worker, "quarantine_probe.html")
    _total_plus(worker, tab, [_allow(9201, tab, ["get"], ["xmlhttprequest", "other"],
                                     r"^http://127\.0\.0\.1:8420/record/sw_fetch$")])
    recorder.clear()
    page.evaluate("() => fetch('/record/sw_fetch').catch(() => {})")  # tab request: allowed
    page.wait_for_timeout(800)
    page_hits = list(recorder.requests)
    recorder.clear()
    page.evaluate("() => window.__probe.sw_fetch()")  # service-worker fetch: TAB_ID_NONE, Q2 blocks
    page.wait_for_timeout(1_500)
    assert page_hits == [("GET", "sw_fetch")], page_hits
    assert recorder.requests == [], recorder.requests


def test_s_e2_matched_rules_report_the_allow_rule(context, recorder):
    page = _open(context, "react_xhr.html")
    worker = extension_worker(context, HOOK_BUILD)
    tab = _tab(worker, "react_xhr.html")
    _total_plus(worker, tab, [_allow(9201, tab, ["post"], ["main_frame", "xmlhttprequest"], E1_REGEX)])
    page.click("#submit_app")
    page.wait_for_selector("#application_confirmation", timeout=10_000)
    matched = worker.evaluate(
        """async (tab) => { try {
             const r = await chrome.declarativeNetRequest.getMatchedRules({ tabId: tab });
             return { ok: true, ids: r.rulesMatchedInfo.map(i => i.rule.ruleId) };
           } catch (e) { return { ok: false, error: String(e) }; } }""", tab)
    print(f"[S-E2] getMatchedRules -> {matched}")
    assert matched["ok"], matched
    assert 9201 in matched["ids"], matched


def test_s_e3b_isolated_click_triggers_react_onsubmit_xhr(context, recorder):
    page = _open(context, "react_xhr.html")
    worker = extension_worker(context, HOOK_BUILD)
    tab = _tab(worker, "react_xhr.html")
    _total_plus(worker, tab, [_allow(9201, tab, ["post"], ["main_frame", "xmlhttprequest"], E1_REGEX)])
    recorder.clear()
    assert _isolated_click(worker, tab) is True
    page.wait_for_selector("#application_confirmation", timeout=10_000)
    assert recorder.requests == [("POST", "submit:acme/123")], recorder.requests


def test_s_e3a_and_s_e4_isolated_click_posts_the_form_and_the_confirmation_is_observable(context, recorder):
    _open(context, "form_post.html")
    worker = extension_worker(context, HOOK_BUILD)
    tab = _tab(worker, "form_post.html")
    _total_plus(worker, tab, [
        _allow(9201, tab, ["post"], ["main_frame", "xmlhttprequest"], E1_REGEX),
        _allow(9202, tab, ["get"], ["main_frame", "xmlhttprequest"], E2_REGEX),
    ])
    worker.evaluate(
        """(tab) => { globalThis.__navDone = new Promise((resolve) => {
             const listener = (id, info, t) => {
               if (id === tab && info.status === "complete" && t.url && t.url.includes("/confirmation")) {
                 chrome.tabs.onUpdated.removeListener(listener); resolve(t.url);
               } };
             chrome.tabs.onUpdated.addListener(listener); }); return true; }""", tab)
    recorder.clear()
    assert _isolated_click(worker, tab) is True
    url = worker.evaluate(
        "async () => await Promise.race([globalThis.__navDone, new Promise(r => setTimeout(() => r(null), 10000))])")
    assert url == f"{BASE}/acme/jobs/123/confirmation", url
    assert recorder.requests == [("POST", "submit:acme/123"), ("GET", "confirm:acme/123")], recorder.requests
    marker = worker.evaluate(
        """async (tab) => (await chrome.scripting.executeScript({ target: { tabId: tab }, world: "ISOLATED",
             func: () => document.querySelector("#application_confirmation") !== null }))[0].result""", tab)
    assert marker is True
