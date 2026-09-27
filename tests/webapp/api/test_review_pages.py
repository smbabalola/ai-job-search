from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.persistence import review_approval as ra
from tests.webapp.services.review_fixtures import NOW, V2_ACCOUNT, docx_bytes, v2_chain  # noqa: F401

COPY = "Filling does not submit. Submission will ask you separately."
ALLOWED_ACTIONS = {"save", "approve", "revoke", "acknowledge", "answer", "omit", "replace", "select",
                   "approve-selected", "preview"}


@pytest.fixture
def ui(v2_chain):
    with TestClient(create_app(v2_chain.settings)) as client:
        yield client, v2_chain


def _presented(world):
    return [e for e in ra.events(world.conn, world.ws) if e["event"] == "REVIEW_PRESENTED"]


def test_review_page_renders_every_section(ui):
    client, world = ui
    html = client.get(f"/workspaces/{world.ws}/review").text
    for section in ("job", "target", "documents", "claims", "fields", "warnings", "actions"):
        assert f'data-review-section="{section}"' in html, section
    assert COPY in html and "Approve for filling" in html
    assert f'data-displayed-binding-hash="{world.state().binding_hash}"' in html


def test_page_route_records_presented_and_api_does_not(ui):
    client, world = ui
    client.get(f"/api/workspaces/{world.ws}/review/state")
    assert _presented(world) == []
    client.get(f"/workspaces/{world.ws}/review")
    [event] = _presented(world)
    assert event["binding_hash"] == world.state().binding_hash


def test_without_an_exact_pack_approve_is_disabled_and_no_hash_is_emitted(ui):
    client, world = ui
    from webapp.services import review_documents as rd
    rd.replace_document(world.conn, settings=world.settings, account_id=V2_ACCOUNT, application_workspace_id=world.ws,
                        kind="cv", filename="x.docx", content=docx_bytes("x"),
                        expected_revision=world.selection("cv")["revision"], actor="u", now=NOW)
    html = client.get(f"/workspaces/{world.ws}/review").text
    assert "data-displayed-binding-hash" not in html and "Save your document changes" in html
    assert re.search(r'data-review-action="approve"[^>]*disabled', html)
    assert _presented(world) == []


def test_delta_only_banner_only_in_delta_only_mode(ui):
    client, world = ui
    from webapp.services import review_approval as svc
    assert "data-delta-only-banner" not in client.get(f"/workspaces/{world.ws}/review").text
    world.make_approvable()
    world.approve()
    svc.open_review_delta(world.conn, account_id=V2_ACCOUNT, application_workspace_id=world.ws, kind="NEW_QUESTION",
                          answer_key=None, subject=None, required=False, question="Anything else?",
                          observed={"field_key": "f"}, source="s", now=NOW)
    html = client.get(f"/workspaces/{world.ws}/review").text
    assert "data-delta-only-banner" in html and "Everything else is unchanged" in html


def test_no_submission_action_or_route_exists(ui):
    client, world = ui
    for path in (f"/workspaces/{world.ws}/review", "/applications/prepared"):
        html = client.get(path).text
        actions = set(re.findall(r'data-review-action="([^"]+)"', html))
        assert actions <= ALLOWED_ACTIONS, actions
        assert not re.search(r'<form[^>]*action="[^"]*submit', html, re.IGNORECASE)
        assert COPY in html or path.startswith("/applications")
    submit_routes = {r.path for r in client.app.routes if "submit" in getattr(r, "path", "").lower()}
    assert submit_routes <= {"/api/handoff/sessions/{session_id}/confirm-submission"}, submit_routes


def test_prepared_page_lists_applications_with_bulk_control(ui):
    client, world = ui
    html = client.get("/applications/prepared").text
    assert f'data-application="{world.ws}"' in html and 'data-review-action="approve-selected"' in html
    assert "Approve selected" in html and "Approve all" not in html


def test_document_preview_route(ui):
    client, world = ui
    r = client.get(f"/api/workspaces/{world.ws}/review/documents/cv/preview")
    assert r.status_code == 200 and r.json()["paragraphs"]


def test_inbox_prepared_entry_links_to_the_review_page(ui):
    client, world = ui
    from webapp.services.autonomy_inbox import notify_outcome
    notify_outcome(world.conn, account_id=V2_ACCOUNT, subject_type="APPLICATION", subject_id=world.ws,
                   kind="PREPARED", reason="prepared", fingerprint="f", detail={}, now=NOW)
    world.conn.commit()
    html = client.get("/autonomy/inbox").text
    assert "Ready for review" in html and f'href="/workspaces/{world.ws}/review"' in html


def test_dossier_has_an_approvals_section(ui):
    client, world = ui
    world.make_approvable()
    approval = world.approve()
    html = client.get(f"/workspaces/{world.ws}/autonomy").text
    assert 'data-dossier-section="approvals"' in html and approval["approval_id"] in html


def test_review_pages_do_not_use_the_global_data_action_hook():
    """app.js reloads the page for any button[data-action] it does not know;
    the 6D-A pages use their own data-review-action so clicks are not raced."""
    from pathlib import Path
    templates = Path(__file__).resolve().parents[3] / "webapp" / "templates"
    for name in ("review_application.html", "prepared_applications.html"):
        assert "data-action=" not in (templates / name).read_text(encoding="utf-8"), name


def test_a_change_between_rendering_and_presentation_offers_no_approval(ui, monkeypatch):
    """What the user approves is exactly what they saw: if the binding moves
    after the page payload is built, the page renders but cannot approve and
    no presentation is recorded for the unseen hash."""
    from webapp.api import review_pages
    client, world = ui
    real = review_pages.review_payload

    def payload_then_change(*a, **k):
        shown = real(*a, **k)
        world.set_target(url="https://jobs.example.test/acme/moved")  # lands before the presentation transaction
        world.conn.commit()
        return shown
    monkeypatch.setattr(review_pages, "review_payload", payload_then_change)
    html = client.get(f"/workspaces/{world.ws}/review").text
    assert 'data-review-section="documents"' in html  # the content still renders
    assert "data-displayed-binding-hash" not in html
    assert re.search(r'data-review-action="approve"[^>]*disabled', html)
    assert _presented(world) == []
    assert world.state().binding_hash is not None  # the new hash exists but was never presented


def test_record_presented_writes_only_at_the_expected_hash(ui):
    from webapp.services import review_approval as svc
    _, world = ui

    def present(expected):
        return svc.record_presented(world.conn, settings=world.settings, account_id=V2_ACCOUNT,
                                    application_workspace_id=world.ws, expected_binding_hash=expected, actor="u",
                                    now=NOW)
    current = world.state().binding_hash
    assert present("sha256:" + "0" * 64) is None and _presented(world) == []
    assert present(None) is None and _presented(world) == []
    assert present(current) == current
    assert [e["binding_hash"] for e in _presented(world)] == [current]
