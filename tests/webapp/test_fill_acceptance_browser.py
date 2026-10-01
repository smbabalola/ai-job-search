"""Bundle 6D-B adversarial browser acceptance (spec §23, §24).

Real Chrome, the real extension (the FILL_TEST_HOOKS build), the real
webapp on 127.0.0.1:8420 (the extension's server) and a separate employer
origin, http://localhost:8430, served by the recording server: every
exfiltration probe targets /record/<channel>, so containment is proved by
absence. The test-hooks build differs from production only by its hook and
by granting what automated Chrome cannot: the optional tabs/webNavigation
permissions and host access to the employer fixture origin (the activeTab
grant a real toolbar click gives). Popup blocking is left ON, as in a real
browser.

A run starts through __fillTest.startFill without awaiting it, and the test
polls the controller's view, so mid-run events (approval revocation, kill
switch, executor loss) can be timed from in-process server hooks."""
from __future__ import annotations

import dataclasses
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.webapp.fixtures.fill.extension_worker import extension_worker
from tests.webapp.fixtures.fill.recording_server import Recorder, RunningServer
from tests.webapp.services.fill_fixtures import _prepare, patch_6b_external_reads, v2_chain  # noqa: F401
from tests.webapp.services.review_fixtures import V2_ACCOUNT, add_contact_claim
from webapp.persistence import fill as f


REPO = Path(__file__).resolve().parents[2]
EXTENSION_ROOT = REPO / "extension"
HOOK_BUILD = EXTENSION_ROOT / "dist" / "extension-test-hooks"
EMPLOYER = "http://localhost:8430"
APP = "http://127.0.0.1:8420"
TERMINAL = {"NEEDS_REVIEW", "UNSUPPORTED", "FILLED", "STOPPED"}


def page_url(scenario: str) -> str:
    return f"{EMPLOYER}/adversarial/page.html?s={scenario}"


@pytest.fixture(scope="module", autouse=True)
def hook_build():
    npm = shutil.which("npm")
    assert npm is not None, "npm is required to build the extension"
    subprocess.run([npm, "run", "build"], cwd=EXTENSION_ROOT, check=True, env={**os.environ, "FILL_TEST_HOOKS": "1"})


@pytest.fixture(scope="module")
def recorder():
    rec = Recorder()
    with RunningServer(rec, port=8430):
        yield rec


class LiveApp:
    def __init__(self, settings):
        import uvicorn
        from webapp.app import create_app
        self.server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=8420,
                                                    log_level="warning", access_log=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", 8420), timeout=0.25):
                    return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("the webapp did not start on 127.0.0.1:8420")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _now():
    return datetime.now(timezone.utc)


class Harness:
    def __init__(self, ctx, world, recorder, scenario, session_id, token):
        self.ctx, self.w, self.recorder, self.scenario = ctx, world, recorder, scenario
        self.url = page_url(scenario)
        self.session_id, self.token = session_id, token

    @property
    def worker(self):
        return extension_worker(self.ctx, HOOK_BUILD)

    def open(self, url=None):
        page = self.ctx.new_page()
        page.goto(url or self.url)
        page.wait_for_load_state("load")
        return page

    def tab_id(self, url=None) -> int:
        return self.worker.evaluate(
            "async (u) => (await chrome.tabs.query({})).find((t) => t.url === u).id", url or self.url)

    def install_device(self) -> None:
        """Bundle 7 spec X2: runs need a paired device. Seed the extension's
        storage with a device and a live access token for the session's
        account (paired server-side by tests.webapp.extension_helpers)."""
        bearer = getattr(self.token, "bearer", None)
        if bearer:
            self.worker.evaluate(
                "async (b) => { await chrome.storage.local.set({handoff_device: {deviceId: 'dev_test', "
                "refreshToken: 'unused', accountLabel: 'test'}}); await chrome.storage.session.set("
                "{handoff_access: {token: b, expiresAt: Date.now() + 9 * 60 * 1000}}); return true; }", bearer)

    def start(self, tab: int) -> None:
        self.install_device()
        assert self.worker.evaluate("([t, s, k]) => globalThis.__fillTest.startFill(t, s, k, 'greenhouse')",
                                    [tab, self.session_id, str(self.token)]) is True

    def view(self, tab: int):
        return self.worker.evaluate("(t) => globalThis.__fillTest.fillView(t)", tab)

    def run_id(self, tab: int) -> str | None:
        view = self.view(tab)
        return view.get("runId") if view else None

    def wait(self, tab: int, timeout: float = 90.0, *, after: str | None = None):
        """The tab's next terminal view. startFill returns before the new run
        replaces the tab's view, so a view still showing run ``after`` (the run
        before start()) is the previous run's ending, not this one's."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = self.view(tab)
            if view and view["phase"] in TERMINAL and (after is None or view.get("runId") != after):
                return view
            time.sleep(0.2)
        raise AssertionError(f"run did not end: {self.view(tab)}")

    def run(self, tab: int, timeout: float = 90.0):
        previous = self.run_id(tab)
        self.start(tab)
        return self.wait(tab, timeout, after=previous)

    def confirm_plan(self):
        from webapp.services import fill_plans as fp
        observation = f.latest_workspace_observation(self.w.conn, self.w.ws, "INITIAL")
        view = fp.fill_plan_presentation(self.w.conn, settings=self.w.settings, account_id=V2_ACCOUNT,
                                         application_workspace_id=self.w.ws, observation_id=observation["id"],
                                         now=_now())
        assert view["displayed_plan_hash"], view
        fp.confirm_plan(self.w.conn, settings=self.w.settings, account_id=V2_ACCOUNT, application_workspace_id=self.w.ws,
                        observation_id=observation["id"], displayed_plan_hash=view["displayed_plan_hash"], actor="u",
                        now=_now())
        return view

    def fill(self, page, timeout: float = 90.0):
        """Observe -> plan review (run 1 ends for review) -> confirm -> the full run."""
        tab = self.tab_id()
        first = self.run(tab)
        assert first["phase"] == "NEEDS_REVIEW", first
        self.confirm_plan()
        return tab, self.run(tab, timeout)

    def runs(self):
        return f.runs_for_application(self.w.conn, self.w.ws)

    def last_run_events(self):
        return [(e["event"], e["reason"]) for e in f.run_events(self.w.conn, self.runs()[-1]["id"])]

    def value(self, page, element_id):
        return page.evaluate("(i) => document.getElementById(i).value", element_id)

    def files(self, page, element_id):
        return page.evaluate("(i) => Array.from(document.getElementById(i).files).map((x) => x.name)", element_id)


@pytest.fixture
def harness(v2_chain, monkeypatch, playwright, tmp_path, recorder):
    contexts, apps = [], []

    def setup(scenario: str) -> Harness:
        url = page_url(scenario)
        w = v2_chain
        w.set_target(url=url)
        add_contact_claim(w, "phone", "+44 20 7946 0000")  # optional: make_approvable binds it OMIT
        patch_6b_external_reads(monkeypatch, target=url)
        w.settings = dataclasses.replace(w.settings, autonomy_max_capability="SUBMIT",
                                         autonomy_submit_capable_adapters=("greenhouse",))
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
        return Harness(ctx, world, recorder, scenario, session_id, token)

    yield setup
    for ctx in contexts:
        ctx.close()
    for app in apps:
        app.__exit__()


def assert_nothing_left(h, *channels):
    """Containment: no /record probe arrived (for the named channels, or at all)."""
    probes = h.recorder.probes()
    if channels:
        assert not {p for p in probes if any(p.startswith(c) for c in channels)}, probes
    else:
        assert probes == set(), probes


# ---- the happy path --------------------------------------------------------------------------

def test_happy_path_reaches_filled_awaiting_submission_under_quarantine(harness):
    h = harness("happy")
    page = h.open()
    tab, view = h.fill(page)
    assert view["phase"] == "FILLED", view
    assert h.value(page, "email") == "ada@example.com" and h.value(page, "notice") == "1 month"
    assert len(h.files(page, "resume")) == 1 and len(h.files(page, "cover_letter")) == 1
    events = h.last_run_events()
    assert [e for e, _ in events] == ["OBSERVING", "PLAN_PROPOSED", "REVALIDATING", "QUARANTINE_ACTIVE", "FILLING",
                                      "FINAL_VALIDATING", "FILLED_AWAITING_SUBMISSION"]
    result = f.get_result(h.w.conn, h.runs()[-1]["id"])["result"]
    assert result["terminal_state"] == "FILLED_AWAITING_SUBMISSION"
    assert result["non_claims"] and all(v is False for v in result["non_claims"].values())
    assert result["quarantine_status"] == "TOTAL_VERIFIED"
    rules = h.worker.evaluate("async () => (await chrome.declarativeNetRequest.getSessionRules()).map((r) => r.id)")
    assert sorted(rules) == [9111, 9121]  # TOTAL stays: no release
    badge = h.worker.evaluate("(t) => chrome.action.getBadgeText({tabId: t})", tab)
    assert badge == "Q"
    assert_nothing_left(h)


# ---- submission attempts -----------------------------------------------------------------------

def test_enter_submits_after_filled_is_cancelled_and_recorded_as_evidence(harness):
    h = harness("enter_submits")
    page = h.open()
    _, view = h.fill(page)
    assert view["phase"] == "FILLED"
    page.focus("#email")
    page.keyboard.press("Enter")
    page.wait_for_timeout(1500)
    assert h.last_run_events()[-1][0] == "FILLED_AWAITING_SUBMISSION"  # an event only after FILLED
    kinds = [d["kind"] for d in f.detection_events(h.w.conn, h.runs()[-1]["id"])]
    assert "SUBMIT_ATTEMPT_OBSERVED" in kinds
    assert page.url == h.url  # no navigation happened
    assert_nothing_left(h)


@pytest.mark.parametrize("scenario", ["auto_submit", "hidden_submit"])
def test_a_page_submitting_during_filling_stops_the_run(harness, scenario):
    h = harness(scenario)
    page = h.open()
    _, view = h.fill(page)
    assert (view["phase"], view["reason"]) == ("STOPPED", "SUBMIT_ATTEMPT_OBSERVED"), view
    assert h.value(page, "notice") == ""  # nothing after the stop
    assert_nothing_left(h)


def test_js_submit_interception_cannot_send(harness):
    h = harness("js_intercept")
    page = h.open()
    _, view = h.fill(page)
    assert view["phase"] == "FILLED"
    page.focus("#email")
    page.keyboard.press("Enter")
    page.wait_for_timeout(1500)
    assert_nothing_left(h)


# ---- exfiltration channels -----------------------------------------------------------------------

def test_fetch_beacon_image_websocket_and_webtransport_send_nothing(harness):
    h = harness("exfil")
    page = h.open()
    _, view = h.fill(page)
    assert view["phase"] == "FILLED", view
    page.wait_for_timeout(1000)
    assert_nothing_left(h)
    assert not [e for e in h.recorder.ws_events if e[1] == "exfil"]


def test_a_service_worker_proxy_sends_nothing(harness):
    h = harness("sw_proxy")
    page = h.open()
    page.wait_for_function("navigator.serviceWorker && navigator.serviceWorker.controller !== undefined")
    _, view = h.fill(page)
    assert view["phase"] == "FILLED", view
    page.wait_for_timeout(1000)
    assert_nothing_left(h, "sw_proxy_fetch")


def test_a_pre_existing_socket_is_closed_by_the_reset(harness):
    h = harness("pre_socket")
    page = h.open()
    page.wait_for_timeout(500)
    assert ("connect", "pre_socket") in h.recorder.ws_events
    _, view = h.fill(page)
    assert view["phase"] == "FILLED", view
    page.wait_for_timeout(1000)
    events = [e for e, name in h.recorder.ws_events if name == "pre_socket"]
    assert events.count("connect") == 1 and "close" in events  # the reload closed it; the reopen was blocked
    assert "message" not in events


# ---- navigation --------------------------------------------------------------------------------

def test_push_state_navigation_stops_the_run(harness):
    h = harness("push_state")
    page = h.open()
    _, view = h.fill(page)
    assert view["phase"] == "STOPPED" and view["reason"] in ("NAVIGATION_ATTEMPT_OBSERVED", "DELTA_OPENED"), view
    assert h.value(page, "notice") == ""


def test_window_open_is_blocked_without_user_activation(harness):
    h = harness("window_open")
    page = h.open()
    _, view = h.fill(page)
    page.wait_for_timeout(1000)
    assert_nothing_left(h, "window_open")
    assert not [p for p in h.ctx.pages if "/record/window_open" in p.url]  # the popup blocker held
    assert view["phase"] == "FILLED", view


# ---- the form changing -------------------------------------------------------------------------

def test_a_required_field_appearing_mid_fill_opens_a_delta_and_stops(harness):
    h = harness("dynamic_required")
    page = h.open()
    _, view = h.fill(page)
    assert (view["phase"], view["reason"]) == ("STOPPED", "DELTA_OPENED"), view
    from webapp.persistence import review_approval as ra
    assert [d["observed"]["field_key"] for d in ra.open_deltas(h.w.conn, h.w.ws)] == ["gh:hear"]
    assert h.value(page, "notice") == ""


def test_a_spa_re_render_is_a_target_change(harness):
    h = harness("spa_rerender")
    page = h.open()
    _, view = h.fill(page)
    assert (view["phase"], view["reason"]) == ("STOPPED", "TARGET_CHANGED"), view


@pytest.mark.parametrize("scenario,phase,reason", [
    ("duplicate_labels", "UNSUPPORTED", None),
    ("prefilled", "STOPPED", "PREFILLED_VALUE_CONFLICT"),
    ("omit_prefill", "STOPPED", "OMIT_FIELD_NOT_BLANK"),
    ("wizard", "UNSUPPORTED", None),
    ("cross_frame", "UNSUPPORTED", None),
])
def test_forms_that_cannot_be_filled_end_before_any_quarantine_or_write(harness, scenario, phase, reason):
    h = harness(scenario)
    page = h.open()
    view = h.run(h.tab_id())
    assert view["phase"] == phase and (reason is None or view["reason"] == reason), view
    assert "QUARANTINE_ACTIVE" not in [e for e, _ in h.last_run_events()]
    assert h.value(page, "email") == ""
    assert h.worker.evaluate("async () => (await chrome.declarativeNetRequest.getSessionRules()).length") == 0


def test_unsupported_causes_are_exact(harness):
    h = harness("wizard")
    h.open()
    h.run(h.tab_id())
    assert f.run_state(h.w.conn, h.runs()[-1]["id"])["detail"]["causes"] == ["MULTI_STEP"]


@pytest.mark.parametrize("scenario", ["custom_widget", "click_checkbox"])
def test_widgets_that_need_a_click_or_custom_ui_are_never_filled(harness, scenario):
    h = harness(scenario)
    page = h.open()
    view = h.run(h.tab_id())
    assert view["phase"] in ("NEEDS_REVIEW", "UNSUPPORTED"), view
    assert "QUARANTINE_ACTIVE" not in [e for e, _ in h.last_run_events()]
    assert h.value(page, "email") == ""


# ---- sibling contexts and concurrency -------------------------------------------------------------

def test_a_sibling_tab_on_the_employer_origin_refuses_the_run(harness):
    h = harness("happy")
    h.open()
    h.open(f"{EMPLOYER}/employer_plain.html")
    view = h.run(h.tab_id())
    assert (view["phase"], view["reason"]) == ("STOPPED", "SIBLING_EMPLOYER_CONTEXT_OPEN"), view


def test_a_sibling_frame_in_another_tab_refuses_the_run(harness):
    h = harness("happy")
    h.open()
    other = h.open("http://127.0.0.1:8430/adversarial/sibling_frame_host.html")
    other.wait_for_timeout(500)
    view = h.run(h.tab_id())
    assert (view["phase"], view["reason"]) == ("STOPPED", "SIBLING_EMPLOYER_CONTEXT_OPEN"), view


def test_two_concurrent_runs_never_both_fill(harness):
    h = harness("happy")
    h.open()
    h.run(h.tab_id())  # the plan, for review
    h.confirm_plan()
    second = h.open()
    tabs = [t["id"] for t in h.worker.evaluate("async (u) => (await chrome.tabs.query({url: u + '*'}))",
                                                f"{EMPLOYER}/adversarial/page.html")]
    assert len(tabs) == 2 and second
    previous = {tab: h.run_id(tab) for tab in tabs}
    for tab in tabs:
        h.start(tab)
    views = [h.wait(tab, after=previous[tab]) for tab in tabs]
    assert not [v for v in views if v["phase"] == "FILLED"], views
    assert {v["reason"] for v in views} <= {"SIBLING_EMPLOYER_CONTEXT_OPEN", "run_active"}, views


def test_a_stale_app_tab_cannot_confirm_an_old_plan(harness):
    h = harness("happy")
    h.open()
    h.run(h.tab_id())
    app = h.open(f"{APP}/workspaces/{h.w.ws}/fill-plan")
    assert app.locator('[data-fill-action="confirm"]').count() == 1
    from webapp.services.review_approval import revoke
    revoke(h.w.conn, settings=h.w.settings, account_id=V2_ACCOUNT, application_workspace_id=h.w.ws, actor="u",
           now=_now())
    app.click('[data-fill-action="confirm"]')
    app.wait_for_selector("[data-fill-refusal]:not([hidden])")
    assert "stale_plan" in app.inner_text("[data-fill-refusal]")
    assert h.w.conn.execute("SELECT COUNT(*) FROM fill_plan_confirmations").fetchone()[0] == 0


# ---- authority changing mid-fill, executor loss ---------------------------------------------------

def _after_email_written(monkeypatch, action):
    """Runs `action` in the server right after action 1's outcome (the email
    WRITE; action 0 is the hidden token's IGNORE) commits."""
    from webapp.services import fill_actions
    real = fill_actions.record_outcome
    fired = threading.Event()

    def hooked(conn, **kw):
        out = real(conn, **kw)
        if kw["action_index"] == 1 and not fired.is_set():
            fired.set()
            action()
        return out

    monkeypatch.setattr(fill_actions, "record_outcome", hooked)
    return fired


def test_approval_invalidation_mid_fill_stops_before_the_next_write(harness, monkeypatch):
    h = harness("happy")
    page = h.open()
    from webapp.persistence.db import connect
    from webapp.services.review_approval import revoke

    def invalidate():
        conn = connect(h.w.settings.db_path)
        revoke(conn, settings=h.w.settings, account_id=V2_ACCOUNT, application_workspace_id=h.w.ws, actor="u",
               now=_now())
        conn.close()

    fired = _after_email_written(monkeypatch, invalidate)
    _, view = h.fill(page)
    assert fired.is_set()
    assert (view["phase"], view["reason"]) == ("STOPPED", "APPROVAL_NOT_EFFECTIVE"), view
    assert h.value(page, "email") == "ada@example.com" and h.value(page, "notice") == ""


def test_the_kill_switch_mid_fill_stops_before_the_next_write(harness, monkeypatch):
    h = harness("happy")
    page = h.open()
    from webapp.persistence.db import connect
    from webapp.services.autonomy_controls import engage_kill_switch

    def kill():
        conn = connect(h.w.settings.db_path)
        engage_kill_switch(conn, account_id=V2_ACCOUNT, actor="u", reason="stop", now=_now())
        conn.close()

    _after_email_written(monkeypatch, kill)
    _, view = h.fill(page)
    assert (view["phase"], view["reason"]) == ("STOPPED", "AUTHORITY_REDUCED"), view
    assert h.value(page, "email") == "ada@example.com" and h.value(page, "notice") == ""


def test_executor_loss_is_never_resumed(harness, monkeypatch):
    h = harness("happy")
    page = h.open()
    from webapp.services import fill_runs
    real = fill_runs.record_observation
    reached, release = threading.Event(), threading.Event()

    def hooked(conn, **kw):
        if kw["phase"] == "PRE_ACTION" and kw["action_index"] == 2:
            reached.set()
            release.wait(20)
        return real(conn, **kw)

    monkeypatch.setattr(fill_runs, "record_observation", hooked)
    tab = h.tab_id()
    assert h.run(tab)["phase"] == "NEEDS_REVIEW"
    h.confirm_plan()
    h.start(tab)
    assert reached.wait(60), "the run never reached action 2"
    try:
        h.worker.evaluate("() => { chrome.runtime.reload(); }")
    except Exception:
        pass  # the worker goes away mid-call
    release.set()
    time.sleep(2)
    run = h.runs()[-1]
    fill_runs.reap_expired_leases(h.w.conn, now=_now() + timedelta(seconds=46))
    events = [(e["event"], e["reason"]) for e in f.run_events(h.w.conn, run["id"])]
    assert events[-1] in (("FILL_STOPPED", "EXECUTOR_LOST"), ("FILL_STOPPED", "WRITE_OUTCOME_UNKNOWN")), events
    assert h.value(page, "email") == "ada@example.com"
    assert h.value(page, "notice") == ""  # action 2 never ran
    assert SimpleNamespace  # noqa: B018
