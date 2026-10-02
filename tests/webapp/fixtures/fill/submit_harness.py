"""The 6E-A browser acceptance harness: the 6D-B acceptance world (real
Chrome, the FILL_TEST_HOOKS extension build, the real webapp on
127.0.0.1:8420, the recording employer server on localhost:8430), with the
application page at a Greenhouse-shaped URL (/acme/jobs/123) so the certified
egress resolves, human submission enabled for the loopback fixture origin,
and a third-party listener on 127.0.0.1:8431 that must never be reached."""
from __future__ import annotations

import dataclasses
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.webapp.services.fill_fixtures import _prepare, patch_6b_external_reads
from tests.webapp.services.review_fixtures import V2_ACCOUNT, add_contact_claim
from tests.webapp.test_fill_acceptance_browser import (  # noqa: F401  (fixtures re-exported)
    APP, EMPLOYER, HOOK_BUILD, Harness, LiveApp, hook_build, recorder,
)
from webapp.persistence import submit as sp

THIRD_PARTY_PORT = 8431
SUBMIT_POST = ("POST", "submit:acme/123")


def submit_url(scenario: str) -> str:
    return f"{EMPLOYER}/acme/jobs/123?s={scenario}"


class _ThirdParty(BaseHTTPRequestHandler):
    hits: list[str] = []

    def _hit(self):
        _ThirdParty.hits.append(f"{self.command} {self.path}")
        self.send_response(200)
        self.end_headers()

    do_GET = do_POST = _hit  # noqa: N815

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def third_party():
    server = ThreadingHTTPServer(("127.0.0.1", THIRD_PARTY_PORT), _ThirdParty)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield _ThirdParty.hits
    server.shutdown()
    server.server_close()  # shutdown() stops serving; this closes the listening socket


class SubmitHarness(Harness):
    http: httpx.Client
    app = None

    def filled(self, timeout: float = 120.0):
        page = self.open()
        tab, view = self.fill(page, timeout)
        assert view["phase"] == "FILLED", view
        return page, tab

    def state(self):
        r = self.http.get(f"/api/workspaces/{self.w.ws}/submit/state")
        assert r.status_code == 200, r.text
        return r.json()

    def request_review(self):
        assert self.http.get(f"/workspaces/{self.w.ws}/submit").status_code == 200

    def wait_review(self, timeout: float = 45.0):
        """The heartbeat (<= 10 s) carries the request; the extension answers."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if state["review"]["state"] != "PENDING_OBSERVATION":
                return state
            time.sleep(0.5)
        raise AssertionError(f"no REVIEW observation arrived: {self.state()}")

    def ready(self) -> str:
        self.request_review()
        state = self.wait_review()
        assert state["review"]["state"] == "READY", state
        return state["review"]["review_hash"]

    def authorize(self, review_hash: str, expect: int = 201):
        r = self.http.post(f"/api/workspaces/{self.w.ws}/submit/authorize", json={"review_hash": review_hash})
        assert r.status_code == expect, r.text
        return r.json()

    def wait_status(self, statuses, timeout: float = 120.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if state["status"]["status"] in statuses:
                return state["status"]
            time.sleep(0.5)
        raise AssertionError(f"status never reached {statuses}: {self.state()['status']}")

    def wait_authorization_used(self, timeout: float = 60.0):
        """The heartbeat delivered the authorization and the pre-click was refused or completed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            auth = sp.authorizations_for_application(self.w.conn, self.w.ws)
            if auth:
                events = [e["event"] for e in sp.submit_events(self.w.conn, auth[-1]["id"])]
                grant = self.w.conn.execute("SELECT status FROM autonomy_grants WHERE id = ?",
                                            (auth[-1]["grant_id"],)).fetchone()[0]
                if events or grant != "ISSUED":
                    return events, grant
            time.sleep(0.5)
        raise AssertionError("the authorization was never used")

    def submit_posts(self):
        return [r for r in self.recorder.requests if r == SUBMIT_POST]

    def attempts(self):
        return sp.attempts_for_application(self.w.conn, self.w.ws)


@pytest.fixture
def submit_harness(v2_chain, monkeypatch, playwright, tmp_path, recorder, third_party):
    contexts, apps, clients = [], [], []

    def setup(scenario: str) -> SubmitHarness:
        url = submit_url(scenario)
        w = v2_chain
        w.set_target(url=url)
        add_contact_claim(w, "phone", "+44 20 7946 0000")
        patch_6b_external_reads(monkeypatch, target=url)
        w.settings = dataclasses.replace(w.settings, autonomy_max_capability="SUBMIT",
                                         autonomy_submit_capable_adapters=("greenhouse",),
                                         human_submit_enabled=True, submit_fixture_origins_enabled=True)
        world = _prepare(w, gate_ready=True, target=url, store=False)
        from tests.webapp.api.test_fill_routes import _session
        session_id, token = _session(world.conn, V2_ACCOUNT, world.ws)
        app = LiveApp(world.settings).__enter__()
        apps.append(app)
        path = str(HOOK_BUILD.resolve())
        ctx = playwright.chromium.launch_persistent_context(
            str(tmp_path / f"profile-{scenario}"), headless=False,
            ignore_default_args=["--disable-popup-blocking"],
            args=["--headless=new", f"--disable-extensions-except={path}", f"--load-extension={path}"])
        contexts.append(ctx)
        recorder.clear()
        third_party.clear()
        h = SubmitHarness(ctx, world, recorder, scenario, session_id, token)
        h.url = url
        h.http = httpx.Client(base_url=APP, timeout=30)
        clients.append(h.http)
        h.app = app.server.config.app
        return h

    yield setup
    for client in clients:
        client.close()
    for ctx in contexts:
        ctx.close()
    for app in apps:
        app.__exit__()
