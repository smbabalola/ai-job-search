"""Phase 2B-5B: CV Quality v2 reachable from the real product UI.

One real Chromium + Uvicorn journey: promoted application -> Start CV Quality
v2 review -> Use/Leave out each statement -> build basis -> generate from that
exact basis -> existing selection -> existing confirmation -> download.

Grounding, immutability and pinning depth are covered at the service level by
tests/webapp/test_cv_generation_v2_acceptance.py and are not repeated here.
"""
from __future__ import annotations

import hashlib
import re
from io import BytesIO
from pathlib import Path

from docx import Document

from webapp.persistence.artifacts import get_current_artifact
from webapp.persistence.db import connect

from tests.webapp.test_browser_smoke import (
    _click_reload,
    _refresh_profile,
    _resolve_all_pending_reviews,
    _run_to_intelligence,
    live_server,
)


def _docx_text(content: bytes) -> str:
    document = Document(BytesIO(content))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def test_cv_v2_review_generate_select_confirm_and_download_exact_cv(page, live_server):
    _refresh_profile(page, live_server)
    workspace_url = _run_to_intelligence(page, live_server)
    workspace_id = workspace_url.rsplit("/", 1)[-1]
    _resolve_all_pending_reviews(page, "acknowledged_and_proceed")

    page.goto(workspace_url, wait_until="networkidle")
    _click_reload(page, page.get_by_role("button", name="Start CV Quality v2 review"))
    match = re.search(rf"/workspaces/{workspace_id}/cv-v2/(art_[0-9a-f]+)$", page.url)
    assert match, page.url
    plan_id = match.group(1)
    assert page.locator(".cv-v2-review").get_attribute("data-plan-id") == plan_id

    statements = page.locator("article.cv-v2-statement")
    statement_ids = [statements.nth(i).get_attribute("data-statement-id") for i in range(statements.count())]
    assert len(statement_ids) >= 2, "fixture must yield a statement to omit and one to use"
    texts = {
        sid: page.locator(f'article.cv-v2-statement[data-statement-id="{sid}"] p').inner_text()
        for sid in statement_ids
    }
    assert page.get_by_role("button", name="Build reviewed CV basis").is_disabled()

    omitted_id, used_ids = statement_ids[0], statement_ids[1:]
    for sid, label in [(omitted_id, "Leave out"), *[(used, "Use") for used in used_ids]]:
        article = page.locator(f'article.cv-v2-statement[data-statement-id="{sid}"]')
        _click_reload(page, article.get_by_role("button", name=label, exact=True))
    assert page.get_by_text("0 pending").is_visible()

    _click_reload(page, page.get_by_role("button", name="Build reviewed CV basis"))
    generate = page.locator("button.cv-v2-generate")
    assert generate.count() == 1
    basis_id = generate.get_attribute("data-basis-id")
    assert generate.is_enabled()

    with page.expect_navigation(wait_until="networkidle"):
        generate.click()
    assert page.url.endswith(f"/workspaces/{workspace_id}#documents")

    for kind in ("cv", "cover_letter"):
        panel = page.locator(f'[data-document-kind="{kind}"]')
        _click_reload(page, panel.get_by_role("button", name="Use this version").first)
    page.once("dialog", lambda dialog: dialog.accept())
    _click_reload(page, page.get_by_role("button", name="Confirm selected files — does not submit"))

    conn = connect(live_server.db_path)
    try:
        generation = get_current_artifact(conn, workspace_id, "application_document_generation")["payload"]
        pack = get_current_artifact(conn, workspace_id, "application_pack")["payload"]
    finally:
        conn.close()
    assert generation["schema_version"] == "application-document-generation.v2"
    assert generation["cv_generation_basis"]["artifact_id"] == basis_id
    generated_cv = generation["documents"]["cv"]
    assert pack["schema_version"] == "application-pack.v2"
    assert pack["final_documents"]["cv"]["document_version_id"] == generated_cv["document_version_id"]

    cv_panel = page.locator('[data-document-kind="cv"]')
    with page.expect_download() as download_info:
        cv_panel.get_by_role("link", name="Download").first.click()
    content = Path(download_info.value.path()).read_bytes()
    assert hashlib.sha256(content).hexdigest() == generated_cv["sha256"]

    cv_text = _docx_text(content)
    assert texts[omitted_id] not in cv_text
    assert any(texts[sid] in cv_text for sid in used_ids)
