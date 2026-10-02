"""Bundle 7 release journey (spec §22.1 steps 1-15, §25.4), in real Chrome with
the extension test-hook build against the live webapp on 127.0.0.1:8420.

A new customer signs up from the pricing page, verifies the email from the
link in the delivered mail, buys Pro through the (fake) provider checkout,
onboards (CV upload, CV import, eligibility, preferences, a job family bound
to the CV, a salary-floor rule), pairs the extension with the code the web app
shows, pastes a posting, prepares it, reviews and approves it, fills the
fixture Greenhouse form through the extension and submits it -- then sees the
application in the tracker with the CV it used. There are no backdoors: mail
comes from the console provider's delivered messages, the pairing code from
the page, and every write goes through the product's own pages or the JSON
API those pages call (with the page's CSRF token, in the page's session).

Automation stands in for exactly two gestures a real browser needs: the
toolbar click that starts a fill (__fillTest.startSafeFill sends what the
popup's "Fill this page" sends) and Chrome's optional-permission prompt (the
test-hook build already holds tabs/webNavigation)."""
from __future__ import annotations

import re
import time

from playwright.sync_api import expect

from tests.webapp.auth_helpers import PASSWORD
from tests.webapp.fixtures.fill.extension_worker import extension_worker
from tests.webapp.fixtures.journey import FAMILY, TARGET_URL, cv_docx, posting
from tests.webapp.test_fill_acceptance_browser import HOOK_BUILD, hook_build, recorder  # noqa: F401
from webapp.persistence import submit as sp
from webapp.persistence.db import connect

EMAIL = "ada.release@example.com"
SUBMIT_POST = ("POST", "submit:acme/123")


class Journey:
    def __init__(self, server, ctx):
        self.server, self.ctx = server, ctx
        self.origin = server.origin
        self.page = ctx.new_page()

    # ---- plumbing ------------------------------------------------------------------------------
    def db(self):
        return connect(self.server.settings)

    def one(self, sql, *params):
        conn = self.db()
        try:
            row = conn.execute(sql, params).fetchone()
            return row[0] if row is not None and len(row) == 1 else row
        finally:
            conn.close()

    def go(self, path):
        self.page.goto(f"{self.origin}{path}")
        self.page.wait_for_load_state("load")
        return self.page

    def csrf(self) -> str:
        for _ in range(20):  # a page script may still be navigating (a redirect, a reload after an action)
            try:
                self.page.wait_for_load_state("load")
                return self.page.evaluate("() => document.querySelector('meta[name=csrf-token]')?.content || ''")
            except Exception:  # noqa: BLE001 - "execution context was destroyed": try again once it settles
                time.sleep(0.25)
        raise AssertionError("the page never settled")

    def api(self, method, path, body=None, expect_status=None):
        """The JSON API the product's pages call, in this page's session."""
        response = self.page.request.fetch(f"{self.origin}{path}", method=method, data=body,
                                           headers={"X-CSRF-Token": self.csrf(), "Accept": "application/json"})
        if expect_status is not None:
            assert response.status == expect_status, (method, path, response.status, response.text())
        return response.json() if "json" in (response.headers.get("content-type") or "") else response.text()

    def submit_form(self, action):
        with self.page.expect_navigation():
            self.page.locator(f'form[action="{action}"] button[type=submit]').first.click()

    def mail(self, to, subject_contains, timeout=75.0):
        """A delivered message: the outbox kick runs within its 10 s slot, the
        periodic dispatch within 60 s."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in list(self.server.mailbox):
                if message["to"] == to and subject_contains.lower() in message["subject"].lower():
                    return message
            time.sleep(0.25)
        conn = self.db()
        try:
            queued = [tuple(r) for r in conn.execute("SELECT template_id, status, last_error FROM outbound_messages")]
        finally:
            conn.close()
        raise AssertionError(f"no '{subject_contains}' mail to {to}: {[m['subject'] for m in self.server.mailbox]} "
                             f"outbox={queued}")

    def account_id(self):
        return self.one("SELECT m.account_id FROM account_memberships m JOIN users u ON u.id = m.user_id "
                        "WHERE u.email_normalized = ?", EMAIL)

    def notifications(self):
        conn = self.db()
        try:
            return [r[0] for r in conn.execute("SELECT kind FROM notifications WHERE account_id = ? ORDER BY "
                                               "created_at, id", (self.account_id(),)).fetchall()]
        finally:
            conn.close()

    @property
    def worker(self):
        return extension_worker(self.ctx, HOOK_BUILD)


def test_release_journey_sign_up_to_submitted(journey_server, playwright, tmp_path, recorder):  # noqa: F811
    path = str(HOOK_BUILD.resolve())
    ctx = playwright.chromium.launch_persistent_context(
        str(tmp_path / "chrome-profile"), headless=False, ignore_default_args=["--disable-popup-blocking"],
        args=["--headless=new", f"--disable-extensions-except={path}", f"--load-extension={path}"])
    try:
        recorder.clear()
        j = Journey(journey_server, ctx)
        _walk(j, recorder)
    finally:
        ctx.close()


def _walk(j: Journey, recorder) -> None:  # noqa: C901 - the journey is one story
    page = j.page
    # 1. pricing -> Get started
    j.go("/pricing")
    with page.expect_navigation():
        page.click("text=Get started")
    assert page.url.endswith("/signup")

    # 2. sign up
    page.fill('input[name="display_name"]', "Ada Lovelace")
    page.fill('input[name="email"]', EMAIL)
    page.fill('input[name="password"]', PASSWORD)
    page.check('input[name="accept_terms"]')
    page.check('input[name="accept_privacy"]')
    j.submit_form("/auth/signup")
    assert j.one("SELECT status FROM users WHERE email_normalized = ?", EMAIL) == "PENDING_VERIFICATION"
    account_id = j.account_id()
    assert j.one("SELECT COUNT(*) FROM search_workspaces WHERE account_id = ?", account_id) >= 1

    # 3. the verification link from the delivered mail
    verify = j.mail(EMAIL, "confirm your email")
    link = re.search(r"https?://\S+/verify-email\S*", verify["text"]).group(0).rstrip(".>)\"'")
    page.goto(link)
    page.wait_for_load_state("load")
    j.submit_form("/auth/verify-email")  # the link opens a confirm page (mail scanners must not verify)
    assert j.one("SELECT status FROM users WHERE email_normalized = ?", EMAIL) == "ACTIVE"
    if "/login" in page.url:
        page.fill('input[name="email"]', EMAIL)
        page.fill('input[name="password"]', PASSWORD)
        j.submit_form("/auth/login")
    assert "/login" not in page.url and "/check-email" not in page.url, page.url

    # 4. Pro through the provider's checkout -> webhook -> an entitled subscription
    j.go("/plans")
    with page.expect_navigation():
        page.click('[data-choose-plan="pro"]')
    with page.expect_navigation():
        page.locator('form[action*="/pay"] button[type=submit]').first.click()
    deadline = time.monotonic() + 30
    # Pro has a trial in the dev catalog: TRIALING is the paid, entitled state until the trial ends
    while j.one("SELECT state FROM subscriptions WHERE account_id = ?", account_id) not in ("ACTIVE", "TRIALING"):
        assert time.monotonic() < deadline, j.one("SELECT state FROM subscriptions WHERE account_id = ?", account_id)
        time.sleep(0.25)
    assert j.api("GET", "/api/usage")["plan_id"] == "pro"

    # 5. onboarding 1-2: basics, the CV upload ("My CV" v1, the default strategy rule)
    j.go("/onboarding/about")
    page.fill('input[name="display_name"]', "Ada Lovelace")
    page.fill('input[name="contact_email"]', EMAIL)
    page.fill('input[name="phone"]', "+44 7700 900000")
    page.fill('input[name="city"]', "Aberdeen")
    page.fill('input[name="country"]', "gb")
    j.submit_form("/onboarding/about")
    j.go("/onboarding/cv")
    page.fill('input[name="title"]', "My CV")
    cv_file = j.server.settings.documents_root.parent / "my-cv.docx"
    cv_file.write_bytes(cv_docx())
    page.set_input_files('input[name="file"]', str(cv_file))
    j.submit_form("/onboarding/cv")
    item_id, version_id, cv_document = j.one(
        "SELECT i.id, v.id, v.document_version_id FROM cv_library_items i JOIN cv_library_versions v "
        "ON v.item_id = i.id WHERE i.account_id = ? AND i.title = 'My CV'", account_id)
    assert j.api("GET", "/api/cv-strategy")["doc"]["default"] == {"mode": "LATEST_VERSION", "item_id": item_id}

    # 6. import from the CV and accept the proposals (one profile.cv_import)
    j.go("/onboarding/import")
    j.submit_form("/onboarding/import")
    proposals = sorted(set(re.findall(r'name="resolution_([^"]+)"', page.content())))
    assert len(proposals) >= 4, proposals
    for proposal in proposals:
        page.check(f'input[name="resolution_{proposal}"][value="ACCEPTED"]')
    j.submit_form("/onboarding/import/resolve")
    assert j.one("SELECT COUNT(*) FROM profile_import_runs WHERE account_id = ? AND status = 'PROPOSED'",
                 account_id) == 1

    # 7. eligibility, preferences, a job family bound to the CV
    j.go("/onboarding/eligibility")
    page.fill('input[name="country"]', "GB")
    page.select_option('select[name="right_to_work"]', "yes")
    page.select_option('select[name="sponsorship"]', "no")
    page.fill('input[name="notice_period"]', "1 month")
    j.submit_form("/onboarding/eligibility")
    j.go("/onboarding/preferences")
    page.fill('[name="target_roles"]', FAMILY)
    page.fill('[name="locations"]', "Aberdeen")
    j.submit_form("/onboarding/preferences")
    j.go("/onboarding/families")
    page.fill('input[name="family_words_0"]', "data engineer")
    page.select_option('select[name="family_cv_0"]', f"latest:{item_id}")
    j.submit_form("/onboarding/families")
    readiness = j.api("GET", "/api/onboarding")["readiness"]
    assert readiness["prepare_ok"] is True, readiness

    # 8. a salary-floor rule (standing-policy.v2)
    j.go("/preferences?tab=rules")
    page.fill('input[name="salary_amount"]', "60000")
    with page.expect_navigation():
        page.locator('form[action="/preferences/rules"] button[type=submit]').first.click()
    j.go("/onboarding/rules")
    j.submit_form("/onboarding/rules/done")
    rules = j.api("GET", "/api/rules")
    assert "pref.salary_floor" in str(rules)

    # 9. install the extension, enter the pairing code the web app shows
    j.go("/pairing")
    code = page.inner_text("#pairing-code").strip()
    assert re.fullmatch(r"[0-9A-Z]{10}", code), code
    extension_id = j.worker.url.split("/")[2]
    popup = j.ctx.new_page()
    popup.goto(f"chrome-extension://{extension_id}/popup.html")
    popup.fill("#pairing-code", code)
    popup.click("#pair-button")
    expect(popup.locator("#app")).to_contain_text("Signed in as", timeout=10_000)
    popup.close()
    assert j.one("SELECT COUNT(*) FROM extension_devices WHERE account_id = ? AND revoked_at IS NULL", account_id) == 1
    j.mail(EMAIL, "extension was connected")  # the security email
    j.go("/onboarding/extension")
    j.submit_form("/onboarding/extension/done")

    # 10. capture a job: paste the posting
    job = posting()
    j.go("/new-job")
    page.fill('input[name="company"]', job["company"])
    page.fill('input[name="title"]', job["title"])
    page.fill('input[name="source_url"]', TARGET_URL)
    page.fill('textarea[name="posting_text"]', job["text"])
    with page.expect_navigation(url=re.compile(r"/workspaces/ws_")):
        page.click('#new-job-form button[type=submit]')
    ws = re.search(r"/workspaces/(ws_[0-9a-f]+)", page.url).group(1)
    assert j.one("SELECT workflow_status FROM workspaces WHERE id = ?", ws) is None

    # 11. Prepare: the stage buttons ("uses 1 of N" shown), then review the material and confirm the pack
    assert "1 of" in page.inner_text("[data-prepare-cost]")
    for stage in ("understand", "fit", "application-intelligence"):
        button = page.locator(f'button[data-stage="{stage}"]')
        if button.count():
            with page.expect_navigation():
                button.first.click()
        else:  # the page offers the next stage only once the previous one is current
            j.api("POST", f"/api/workspaces/{ws}/{stage}", {"request_id": f"journey-{stage}"}, 200)
            j.go(f"/workspaces/{ws}")
    usage = {r["allowance"]: r for r in j.api("GET", "/api/usage")["usage"]}
    assert usage["applications.prepare"]["used"] == 1
    review = j.api("GET", f"/api/workspaces/{ws}/review")
    _decide_review_surface(j, ws, review)
    generated = j.api("POST", f"/api/workspaces/{ws}/application-documents/generate", None, 201)
    cover = next(d for d in generated["documents"] if d["document_kind"] == "cover_letter")
    j.api("PUT", f"/api/workspaces/{ws}/application-documents/selection/cover_letter",
          {"document_version_id": cover["id"], "expected_revision": 0}, 200)
    selections = j.api("GET", f"/api/workspaces/{ws}/application-documents")["selections"]
    assert selections["cv"]["document_version_id"] == cv_document  # the family rule chose "My CV" v1
    j.api("POST", f"/api/workspaces/{ws}/application-pack",
          {"confirmed": True, "effective_date": time.strftime("%Y-%m-%d"),
           "document_selection_revisions": {k: v["revision"] for k, v in selections.items()}}, 201)
    for kind in ("application.prepared", "application.review_required"):
        assert kind in j.notifications(), j.notifications()

    # 12. review and approve (6D-A), acknowledging the rule conflict and the attention items
    R = f"/api/workspaces/{ws}/review"
    j.go(f"/workspaces/{ws}/review")
    # the apply target is part of what is approved: confirm it first
    j.api("POST", f"/api/workspaces/{ws}/autonomy/apply-target/confirm", {"url": TARGET_URL}, 200)
    _review_and_approve(j, ws)
    approval = j.one("SELECT binding_json FROM application_approvals WHERE application_workspace_id = ? "
                     "ORDER BY seq DESC LIMIT 1", ws)
    assert f'"document_version_id": "{cv_document}"' in approval.replace('":"', '": "'), approval[:400]

    # 13. fill authority (the user's autonomy settings), then the extension fills
    j.api("POST", "/api/autonomy/enable-preparation", {"timezone": "Europe/London"}, 200)
    for scope_type in ("ACCOUNT_MAX", "DEFAULT_WORKSPACE_CEILING"):
        j.api("POST", "/api/autonomy/capability", {"scope_type": scope_type, "scope_id": account_id,
                                                   "capability": "FILL"}, 200)
    employer_tab = _launch_from_jobsearch(j, ws)
    view = _fill(j, employer_tab)
    assert view["phase"] == "NEEDS_REVIEW", view  # run 1 ends at the plan review
    employer_tab.close()  # run 1 is over; the next launch opens the form afresh
    j.go(f"/workspaces/{ws}/fill-plan")
    # the observed form adds what the approval did not cover (6D-B deltas): review it and approve again
    if j.api("GET", f"{R}/state")["state"]["approval_effective"] is False:
        _review_and_approve(j, ws)
    plan = j.api("GET", f"/api/workspaces/{ws}/fill-plan/state")
    assert plan["plan"]["displayed_plan_hash"], plan
    j.api("POST", f"/api/workspaces/{ws}/fill-plan/confirm", {"observation_id": plan["observation_id"],
          "displayed_plan_hash": plan["plan"]["displayed_plan_hash"]}, 200)
    employer_tab = _launch_from_jobsearch(j, ws)
    view = _fill(j, employer_tab)
    assert view["phase"] == "FILLED", (view, _fill_diagnostics(j, ws))
    deadline = time.monotonic() + 30
    while "fill.completed_awaiting_submit" not in j.notifications():
        assert time.monotonic() < deadline, j.notifications()
        time.sleep(0.25)

    # 14. Submit Review -> "Submit application" (6E-A)
    review_page = j.ctx.new_page()
    # opening it asks the extension for a REVIEW observation; the page reloads itself once it lands
    review_page.goto(f"{j.origin}/workspaces/{ws}/submit")
    review_page.wait_for_selector('[data-submit-state="READY"]', state="attached", timeout=90_000)
    review_page.click('[data-submit-action="authorize"]')
    review_page.wait_for_selector('[data-submission-status="SUBMITTED"]', timeout=120_000)
    assert [r for r in recorder.requests if r == SUBMIT_POST] == [SUBMIT_POST]
    conn = j.db()
    try:
        [attempt] = sp.attempts_for_application(conn, ws)
        result = sp.get_submission_result(conn, attempt["id"])["result"]
    finally:
        conn.close()
    assert result["state"] == "CONFIRMED_SUCCESS"
    deadline = time.monotonic() + 30
    while "submit.confirmed" not in j.notifications():
        assert time.monotonic() < deadline, j.notifications()
        time.sleep(0.25)

    # 15. the tracker shows the application with the CV it used
    j.go(f"/workspaces/{ws}")
    assert "My CV v1" in page.inner_text("[data-cv-used]")
    assert j.one("SELECT workflow_status FROM workspaces WHERE id = ?", ws) == "applied"
    # exactly one prepare was consumed for the whole journey
    assert j.one("SELECT COUNT(*) FROM usage_reservations WHERE account_id = ? AND allowance = "
                 "'applications.prepare' AND status = 'CONSUMED'", account_id) == 1
    assert version_id


def _fill_diagnostics(j: Journey, ws: str) -> dict:
    """What a stopped fill leaves behind: the run's events and the gate's last decision."""
    conn = j.db()
    try:
        run = conn.execute("SELECT id FROM fill_runs WHERE application_workspace_id = ? ORDER BY created_at DESC "
                           "LIMIT 1", (ws,)).fetchone()
        events = [tuple(r) for r in conn.execute("SELECT event, reason, detail_json FROM fill_run_events WHERE "
                                                 "fill_run_id = ? ORDER BY seq", (run[0],))] if run else []
        decision = conn.execute("SELECT result, reasons_json FROM autonomy_decisions WHERE application_workspace_id "
                                "= ? ORDER BY seq DESC LIMIT 1", (ws,)).fetchone()
    finally:
        conn.close()
    return {"events": events, "decision": tuple(decision) if decision else None}


def _review_and_approve(j: Journey, ws: str) -> None:
    """The review page: answer what is required, decide each field, acknowledge
    rule conflicts and attention items, then click Approve."""
    R = f"/api/workspaces/{ws}/review"
    # the job check's open questions, answered in the review page's own forms
    j.go(f"/workspaces/{ws}/review")
    while j.page.locator("form[data-blocker]").count():
        form = j.page.locator("form[data-blocker]").first
        form.locator("textarea[name=answer]").fill("yes")
        with j.page.expect_navigation():
            form.locator("button[type=submit]").click()
    assert all(b["status"] != "open" for b in j.api("GET", f"{R}/blockers")["blockers"])
    state = j.api("GET", f"{R}/state")
    for field in state["reviewable"]["fields"]:
        if field["required"] and field["display_value"] is None:
            j.api("POST", f"{R}/answers", {"answer_key": field["answer_key"], "value": "yes", "reach": "APPLICATION"},
                  200)
        elif field["disposition"] is None and field["display_value"] is not None:
            j.api("POST", f"{R}/fields/{field['answer_key']}/disposition", {"disposition": "ANSWER"}, 200)
    for advisory in j.api("GET", f"{R}/rule-advisories")["advisories"]:  # "proceed despite my rule"
        if advisory["effect"] != "NO_EFFECT":
            j.api("POST", f"{R}/rule-acknowledgements", {"rule_id": advisory["rule_id"]}, 200)
    for warning in j.api("GET", f"{R}/state")["reviewable"]["warnings"]:
        if warning["level"] == "ATTENTION" and not warning["acknowledged"]:
            j.api("POST", f"{R}/warnings/ack", {"warning_key": warning["key"]}, 200)
    j.go(f"/workspaces/{ws}/review")
    j.page.wait_for_selector('[data-review-action="approve"]:not([disabled])', timeout=15_000)
    j.page.click('[data-review-action="approve"]')
    deadline = time.monotonic() + 15
    while not j.api("GET", f"{R}/state")["state"]["approval_effective"]:
        assert time.monotonic() < deadline, j.api("GET", f"{R}/state")["state"]
        time.sleep(0.25)


def _decide_review_surface(j: Journey, ws: str, view: dict) -> None:
    """The review page's per-item decisions (spec 6D-A predecessors): every
    gate flag, judgment question, match and content unit acknowledged."""
    def decide(source, item_type, item_id):
        j.api("POST", f"/api/workspaces/{ws}/review-decisions", {
            "review_item_type": item_type, "source_artifact_id": source, "domain_item_id": item_id,
            "disposition": "acknowledged_and_proceed", "note": "Reviewed"}, 201)
    fit_artifact = view["job_fit_result"]
    fit = fit_artifact["payload"]
    for gate in fit.get("gate_assessments", []):
        if gate.get("status") in {"FLAG", "UNVERIFIED"}:
            decide(fit_artifact["id"], "gate_flag", f"gate:{gate['gate_id']}")
    for question in fit.get("human_judgment_questions", []):
        decide(fit_artifact["id"], "human_judgment_question", question["question_id"])
    for collection, item_type in (("functionally_equivalent_matches", "functionally_equivalent_match"),
                                  ("transferable_matches", "transferable_match")):
        for match in fit.get(collection, []):
            decide(fit_artifact["id"], item_type, match["match_id"])
    intelligence = view["application_intelligence_result"]
    for collection in ("cv_content", "cover_letter_content"):
        for unit in intelligence["payload"].get(collection, []):
            decide(intelligence["id"], "content_unit", unit["unit_id"])


def _launch_from_jobsearch(j: Journey, ws: str):
    """'Apply with extension' on the workspace page: the extension records the
    pending context and the tab navigates to the employer form."""
    tab = j.ctx.new_page()
    tab.goto(f"{j.origin}/workspaces/{ws}")
    tab.wait_for_selector(".apply-with-extension", timeout=15_000)
    with tab.expect_navigation(url=re.compile(r"localhost:8430/acme/jobs/123"), timeout=30_000):
        tab.click(".apply-with-extension")
    tab.wait_for_load_state("load")
    return tab


def _fill(j: Journey, tab, timeout: float = 120.0):
    tab_id = j.worker.evaluate("async (u) => (await chrome.tabs.query({})).find((t) => t.url === u).id", tab.url)
    assert j.worker.evaluate("(t) => globalThis.__fillTest.startSafeFill(t)", tab_id) is True
    deadline = time.monotonic() + timeout
    view = None
    while time.monotonic() < deadline:
        view = j.worker.evaluate("(t) => globalThis.__fillTest.fillView(t)", tab_id)
        if view and view["phase"] in {"NEEDS_REVIEW", "UNSUPPORTED", "FILLED", "STOPPED"}:
            return view
        time.sleep(0.25)
    raise AssertionError(f"the fill never ended: {view}")
