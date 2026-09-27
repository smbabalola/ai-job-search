"""Bundle 6D-A browser regression (spec §19): a live uvicorn server and headless
Chromium drive the review page, preview, replace + Save, answers, Leave blank,
acknowledgement, approval, the stale-tab refusal, the delta-only banner and
bulk approval from the Prepared list. No Submit control is ever rendered."""
from __future__ import annotations

import dataclasses
import re
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn

from webapp.app import create_app
from webapp.persistence import review_approval as ra
from webapp.persistence.db import connect
from tests.webapp.services.review_fixtures import NOW, V2_ACCOUNT, blocker, docx_bytes, v2_chain  # noqa: F401
from tests.webapp.services.test_review_bulk import _second

NOTICE = "employment.notice_period"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live(v2_chain, tmp_path):
    other = _second(v2_chain)
    v2_chain.conn.commit()
    port = _free_port()
    settings = dataclasses.replace(v2_chain.settings, host="127.0.0.1", port=port)
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port,
                                           log_level="warning", access_log=False))
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
        raise RuntimeError("uvicorn did not start")
    conn = connect(settings.db_path)  # a fresh connection sees the server's commits
    yield SimpleNamespace(base_url=f"http://127.0.0.1:{port}", world=v2_chain, other=other, conn=conn,
                          tmp_path=tmp_path)
    server.should_exit = True
    thread.join(timeout=10)
    conn.close()


def _dialogs(page):
    seen = []
    page.on("dialog", lambda d: (seen.append(d.message), d.dismiss()))
    return seen


def _open(page, live, ws):
    page.goto(f"{live.base_url}/workspaces/{ws}/review")
    return page


def _click_and_reload(page, selector):
    with page.expect_navigation():
        page.locator(selector).first.click()


def _state(page):
    return page.locator("[data-review-state]").inner_text()


def _no_submit_control(page):
    controls = page.locator("button, a, input[type=submit]")
    labels = [controls.nth(i).inner_text() for i in range(controls.count())]
    assert not [label for label in labels if re.search(r"\bsubmit\b", label, re.IGNORECASE)], labels
    assert page.locator("[data-copy]").inner_text() == "Filling does not submit. Submission will ask you separately."


def _approval_count(live, ws):
    return live.conn.execute("SELECT COUNT(*) FROM application_approvals WHERE application_workspace_id = ?",
                             (ws,)).fetchone()[0]


def test_review_page_edit_answer_acknowledge_and_approve(page, live):
    world = live.world
    blocker(world.conn, world.ws, NOTICE, "What is your notice period?")
    world.conn.commit()
    dialogs = _dialogs(page)
    _open(page, live, world.ws)
    _no_submit_control(page)
    assert _state(page) == "READY_FOR_REVIEW"

    # Preview renders the exact selected bytes read-only.
    page.locator('[data-review-action="preview"][data-kind="cv"]').click()
    page.wait_for_selector('[data-preview-for="cv"]:not([hidden])')

    # Replace the CV, then Save changes.
    upload = live.tmp_path / "edited_cv.docx"
    upload.write_bytes(docx_bytes("My edited CV"))
    page.locator('form[data-review-action="replace"][data-kind="cv"] input[type=file]').set_input_files(str(upload))
    _click_and_reload(page, 'form[data-review-action="replace"][data-kind="cv"] button')
    assert page.locator("[data-save-guidance]").is_visible()
    assert page.locator('[data-review-action="approve"]').is_disabled()
    _click_and_reload(page, '[data-review-action="save"]')
    assert page.locator('[data-document="cv"]').inner_text().count("Your document, not content-verified") == 1
    page.locator('[data-review-action="preview"][data-kind="cv"]').click()
    page.wait_for_selector('[data-preview-for="cv"]:not([hidden])')
    assert page.locator('[data-preview-for="cv"]').inner_text() == "My edited CV"

    # Answer the required question; Leave blank every optional one.
    field = page.locator(f'form[data-review-action="answer"][data-key="subject:{NOTICE}"]')
    field.locator("input[name=value]").fill("1 month")
    _click_and_reload(page, f'form[data-review-action="answer"][data-key="subject:{NOTICE}"] button')
    assert "1 month" in page.locator(f'[data-field="subject:{NOTICE}"]').inner_text()
    optional = page.locator('[data-review-action="omit"]')
    for key in [optional.nth(i).get_attribute("data-key") for i in range(optional.count())]:
        _click_and_reload(page, f'[data-review-action="omit"][data-key="{key}"]')

    # Acknowledge each ATTENTION warning, then approve.
    while page.locator('[data-review-action="acknowledge"]').count():
        _click_and_reload(page, '[data-review-action="acknowledge"]')
    _click_and_reload(page, '[data-review-action="approve"]')
    assert _state(page) == "APPROVED_FOR_FILL"
    assert dialogs == [] and _approval_count(live, world.ws) == 1
    _no_submit_control(page)


def test_a_stale_tab_cannot_approve(page, live):
    world = live.world
    world.make_approvable()
    dialogs = _dialogs(page)
    stale = _open(page, live, world.ws)
    fresh = _open(page.context.new_page(), live, world.ws)
    upload = live.tmp_path / "letter.docx"
    upload.write_bytes(docx_bytes("A revised letter"))
    fresh.locator('form[data-review-action="replace"][data-kind="cover_letter"] input[type=file]').set_input_files(str(upload))
    with fresh.expect_navigation():
        fresh.locator('form[data-review-action="replace"][data-kind="cover_letter"] button').click()
    with fresh.expect_navigation():
        fresh.locator('[data-review-action="save"]').click()
    stale.locator('[data-review-action="approve"]').click()
    deadline = time.monotonic() + 5
    while not dialogs and time.monotonic() < deadline:
        stale.wait_for_timeout(50)
    assert dialogs == ["stale"]
    assert _approval_count(live, world.ws) == 0


def test_delta_only_banner(page, live):
    from webapp.services.review_approval import open_review_delta
    world = live.world
    world.make_approvable()
    world.approve()
    delta = open_review_delta(world.conn, account_id=V2_ACCOUNT, application_workspace_id=world.ws,
                              kind="NEW_QUESTION", answer_key=None, subject="employment.availability_start",
                              required=True, question="When could you start?", observed={"field_key": "q_start"},
                              source="FILL_SESSION:s1", now=NOW)
    world.conn.commit()
    _open(page, live, world.ws)
    banner = page.locator("[data-delta-only-banner]")
    assert banner.is_visible() and delta["answer_key"] in banner.inner_text()
    assert page.locator(f'[data-field="{delta["answer_key"]}"][data-changed]').count() == 1
    assert _state(page) == "NEEDS_REVIEW"
    _no_submit_control(page)


def test_bulk_approval_from_the_prepared_list(page, live):
    dialogs = _dialogs(page)
    worlds = (live.world, live.other)
    for world in worlds:
        world.make_approvable()
    page.goto(f"{live.base_url}/applications/prepared")
    for world in worlds:  # not yet opened at the current version: not selectable
        assert page.locator(f'[data-select-application="{world.ws}"]').is_disabled()
    for world in worlds:
        _open(page, live, world.ws)
    page.goto(f"{live.base_url}/applications/prepared")
    for world in worlds:
        page.locator(f'[data-select-application="{world.ws}"]').check()
    assert page.locator("[data-selected-count]").inner_text() == "2"
    assert " ".join(page.locator('[data-review-action="approve-selected"]').text_content().split()) == "Approve selected (2)"
    with page.expect_navigation():
        page.locator('[data-review-action="approve-selected"]').click()
    assert dialogs == []
    batches = {ra.latest_approval(live.conn, w.ws)["batch_id"] for w in worlds}
    assert len(batches) == 1 and None not in batches
    for world in worlds:
        assert page.locator(f'[data-select-application="{world.ws}"]').is_disabled()
