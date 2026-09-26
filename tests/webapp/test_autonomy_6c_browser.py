"""Bundle 6C browser regression (spec §15): a live uvicorn server and headless
Chromium drive the inbox, the badge, enrolment, candidate questions and the
explicit pack-review latch."""
from __future__ import annotations

import dataclasses
from decimal import Decimal
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn

from webapp.app import create_app
from webapp.persistence import autonomy_prepare as ap
from webapp.persistence.db import connect
from tests.webapp.services.autonomy_6c_fixtures import (
    ACCOUNT, NOW, add_fit, build_chain, discover, enable_prepare, fresh_fits, portal_job,
)

SW = "search_default"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ask_on_unknown_fit(conn):
    from product.standing_policy import default_policy_document
    from webapp.persistence.autonomy_authority import save_policy_version
    doc = default_policy_document("Europe/London")
    doc["limits"]["budgets"] = {"LLM": {"per_day": "5.00", "per_application": "1.00"}}
    doc["rules"] = [{"id": "fit_unknown", "description": "", "when": {"attr": "fit.overall_score", "op": "lt",
                     "value": 50}, "effect": {"type": "REQUIRE_USER"}, "on_unknown": {"type": "REQUIRE_USER"}}]
    save_policy_version(conn, account_id=ACCOUNT, doc=doc, created_by="u", now=NOW)


def _question(conn, settings, record_id, company):
    from webapp.services import autonomy_candidates as ac
    cid = discover(conn, [portal_job(record_id, company=company)])["candidate_ids"][0]
    add_fit(conn, cid, score=None)
    ctx = ac.build_candidate_context(conn, settings=settings, account_id=ACCOUNT, search_workspace_id=SW,
                                     candidate_id=cid, now=NOW)
    ac.screen_candidate(conn, ctx=ctx, now=NOW)
    conn.commit()
    return cid


@pytest.fixture
def live(tmp_path, monkeypatch):
    from tests.webapp.test_full_journey_acceptance import _close
    fresh_fits(monkeypatch)
    client, _, base, ws = build_chain(tmp_path)  # a workspace with a current pack revision
    port = _free_port()
    settings = dataclasses.replace(base, host="127.0.0.1", port=port, autonomy_max_capability="PREPARE",
                                   autonomy_step_cost_max={"EVALUATE": Decimal("0.05")})
    conn = connect(settings.db_path)
    enable_prepare(conn)
    _ask_on_unknown_fit(conn)
    promote = _question(conn, settings, "b1", "Promote Co")
    dismiss = _question(conn, settings, "b2", "Dismiss Co")
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
    yield SimpleNamespace(base_url=f"http://127.0.0.1:{port}", conn=conn, ws=ws, promote=promote, dismiss=dismiss)
    server.should_exit = True
    thread.join(timeout=10)
    conn.close()
    _close(client)


def _fail_on_dialog(page):
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    return dialogs


def _badge(page):
    badge = page.locator("[data-autonomy-badge]")
    page.wait_for_function("() => { const b = document.querySelector('[data-autonomy-badge]');"
                           " return b && (b.hidden || b.textContent.length > 0); }")
    return None if badge.is_hidden() else badge.inner_text()


def test_inbox_badge_and_candidate_questions(page, live):
    from webapp.services.autonomy_inbox import notify_outcome
    dialogs = _fail_on_dialog(page)
    page.goto(f"{live.base_url}/")
    assert _badge(page) == "2"  # two unresolved candidate questions
    notify_outcome(live.conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=live.ws, kind="BLOCKED",
                   reason="blocked", fingerprint="f", detail={}, now=NOW)
    live.conn.commit()
    page.goto(f"{live.base_url}/")
    assert _badge(page) == "3"  # the new informational notification is unseen

    page.goto(f"{live.base_url}/autonomy/inbox")
    for section in ("needs-answer", "informational", "ready"):
        assert page.locator(f'[data-inbox-section="{section}"]').is_visible()
    assert page.locator('[data-inbox-entry="CANDIDATE_QUESTION"]').count() == 2
    assert page.locator('[data-inbox-entry="BLOCKED"]').count() == 1

    exc = {e["candidate_id"]: e["id"] for e in ap.open_candidate_exceptions(live.conn, ACCOUNT)}
    with page.expect_navigation():
        page.locator(f'[data-candidate-resolve="PROMOTE"][data-exception="{exc[live.promote]}"]').click()
    promotion = ap.promotion_for_candidate(live.conn, live.promote)
    assert promotion is not None and promotion["actor_type"] == "USER"
    with page.expect_navigation():
        page.locator(f'[data-candidate-resolve="DISMISS"][data-exception="{exc[live.dismiss]}"]').click()
    status = live.conn.execute("SELECT lifecycle_status FROM discovery_candidates WHERE id = ?",
                               (live.dismiss,)).fetchone()[0]
    assert status == "dismissed"
    assert page.locator('[data-inbox-entry="CANDIDATE_QUESTION"]').count() == 0
    assert _badge(page) is None  # questions resolved; the BLOCKED entry was seen on the inbox
    assert dialogs == []


def test_dossier_enrol_unenrol_and_explicit_pack_review(page, live):
    dialogs = _fail_on_dialog(page)
    page.goto(f"{live.base_url}/workspaces/{live.ws}/autonomy")
    assert page.locator('[data-dossier-section="current-state"]').is_visible()
    with page.expect_navigation():
        page.locator("[data-autonomy-enrol]").click()
    assert ap.is_enrolled(live.conn, live.ws)
    with page.expect_navigation():
        page.locator("[data-autonomy-unenrol]").click()
    assert not ap.is_enrolled(live.conn, live.ws)
    with page.expect_navigation():
        page.locator("[data-autonomy-review-pack]").click()
    latches = live.conn.execute("SELECT reason FROM autonomy_review_latches WHERE application_workspace_id = ?",
                                (live.ws,)).fetchall()
    assert [r[0] for r in latches] == ["EXPLICIT_REVIEW"]
    assert dialogs == []
