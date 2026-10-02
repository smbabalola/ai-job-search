from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.persistence import review_approval as ra
from tests.webapp.route_inventory import all_routes
from tests.webapp.services.review_fixtures import NOW, V2_ACCOUNT, docx_bytes, v2_chain  # noqa: F401

COPY = "Filling does not submit. Submission will ask you separately."
ALLOWED_ACTIONS = {"save", "approve", "revoke", "acknowledge", "answer", "omit", "replace", "select",
                   "confirm-classification",
                   "approve-selected", "preview", "use-answer", "use-new-draft"}


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
    # 6E-A: the only submit routes are the Phase 3 confirmation and the human-authorized inventory.
    from tests.webapp.test_submit_structure import CONFIRM_SUBMISSION, SUBMIT_ROUTES_6E_A
    submit_routes = {r.path for r in all_routes(client.app) if "submit" in r.path.lower()}
    assert SUBMIT_ROUTES_6E_A <= submit_routes, "the inventory sees the 6E-A routes (never vacuous)"
    assert submit_routes <= {CONFIRM_SUBMISSION} | SUBMIT_ROUTES_6E_A, submit_routes


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


# ---- final-review corrections: Use this answer, Use the new draft, APPLICATION reach ----

def _regenerate(world):
    """A newer AI generation after the user's selections (the pipeline rerun)."""
    from webapp.services.application_documents import generate_application_documents
    out = generate_application_documents(world.conn, world.ws, documents_root=world.settings.documents_root,
                                         extensions_dir=world.settings.extensions_dir, account_id=V2_ACCOUNT)
    world.conn.commit()
    return {row["document_kind"]: row["id"] for row in out["documents"]}


def test_optional_contact_offers_use_this_answer_and_leave_blank(ui):
    from tests.webapp.services.review_fixtures import add_contact_claim
    client, world = ui
    add_contact_claim(world, "location", "London")
    html = client.get(f"/workspaces/{world.ws}/review").text
    assert 'data-review-action="use-answer" data-key="contact:location"' in html
    assert 'data-review-action="omit" data-key="contact:location"' in html
    assert 'data-review-action="answer" data-key="contact:location"' not in html  # evidence, not typed
    r = client.post(f"/api/workspaces/{world.ws}/review/fields/contact:location/disposition",
                    json={"disposition": "ANSWER"})
    assert r.status_code == 200, r.text
    field = next(f for f in world.reviewable().fields if f.answer_key == "contact:location")
    assert (field.disposition, field.source_kind) == ("ANSWER", "EVIDENCE")
    assert 'data-review-action="use-answer" data-key="contact:location"' not in \
        client.get(f"/workspaces/{world.ws}/review").text


def test_page_answers_are_sent_for_this_application_only(ui):
    client, world = ui
    html = client.get(f"/workspaces/{world.ws}/review").text
    assert 'reach: "APPLICATION"' in html and 'reach: "ACCOUNT"' not in html


def test_newer_ai_draft_offers_use_the_new_draft_and_never_moves_the_selection(ui):
    from tests.webapp.services.review_fixtures import diff_counts, table_counts
    client, world = ui
    selected = world.selection("cv")
    fresh = _regenerate(world)
    assert fresh["cv"] != selected["document_version_id"]
    assert world.selection("cv") == selected  # the rerun never moves the user's selection
    html = client.get(f"/workspaces/{world.ws}/review").text
    assert 'data-newer-draft="cv"' in html and "Use the new draft" in html
    assert re.search(rf'data-review-action="use-new-draft"\s+data-kind="cv"\s+data-version="{fresh["cv"]}"'
                     rf'\s+data-revision="{selected["revision"]}"', html)
    assert world.selection("cv") == selected  # rendering never moves it either
    before = table_counts(world.conn)
    r = client.post(f"/api/workspaces/{world.ws}/review/documents/cv/select",
                    json={"document_version_id": fresh["cv"], "expected_revision": selected["revision"]})
    assert r.status_code == 200, r.text
    # Only the selection moved (in place) with its SELECTION_CHANGED event: no pack artifact, no Save.
    assert diff_counts(before, table_counts(world.conn)) == {"application_review_events": 1}
    assert [e["event"] for e in ra.events(world.conn, world.ws)][-1] == "SELECTION_CHANGED"
    assert world.selection("cv")["document_version_id"] == fresh["cv"]
    assert world.selection("cover_letter")["document_version_id"] != fresh["cover_letter"]  # only the cv moved
    assert world.state().binding_hash is None  # not approvable until Save changes
    assert client.post(f"/api/workspaces/{world.ws}/review/save").status_code == 200
    assert world.state().binding_hash is not None


# ---- consent snapshot: the window INSIDE review_payload ----------------------------

def _change_after_first_assembly(monkeypatch, world):
    """Commit a binding change from a second connection (world.conn) right
    after the payload's Reviewable has been assembled, before the payload's
    hash is derived."""
    from webapp.services import review_application
    real, calls = review_application._assemble, []

    def assemble_then_change(*a, **k):
        out = real(*a, **k)
        if not calls:
            world.set_target(url="https://jobs.example.test/acme/moved")
            world.conn.commit()
        calls.append(1)
        return out
    monkeypatch.setattr(review_application, "_assemble", assemble_then_change)


def test_payload_content_and_hash_come_from_one_snapshot(ui, monkeypatch):
    from webapp.api.review_approval import review_payload
    from webapp.persistence.db import connect
    _, world = ui
    before = world.state().binding_hash
    reader = connect(world.settings.db_path)
    try:
        _change_after_first_assembly(monkeypatch, world)
        payload = review_payload(reader, settings=world.settings, account_id=V2_ACCOUNT, workspace_id=world.ws)
        assert not reader.in_transaction  # the read transaction was ended
    finally:
        reader.close()
    assert payload["reviewable"]["target_url"].endswith("acme/123")  # content A
    assert payload["binding_hash"] == before                      # hash A, never B
    assert world.state().binding_hash != before                    # B exists, but only after the snapshot


def test_a_change_inside_payload_derivation_offers_no_approval(ui, monkeypatch):
    """The page can never pair displayed content A with approvable hash B."""
    client, world = ui
    _change_after_first_assembly(monkeypatch, world)
    html = client.get(f"/workspaces/{world.ws}/review").text
    assert "acme/123" in html and "acme/moved" not in html  # the rendered content is A
    assert "data-displayed-binding-hash" not in html
    assert re.search(r'data-review-action="approve"[^>]*disabled', html)
    assert _presented(world) == []


def test_document_controls_carry_the_rendered_snapshots_revision(ui, monkeypatch):
    """A selection change landing after the page is presented must not leak a
    newer expected_revision into the rendered controls: the stale page's
    Replace / Use the new draft are then refused, never applied."""
    from tests.webapp.services.review_fixtures import diff_counts, table_counts
    from webapp.services import review_approval as svc
    from webapp.services import review_documents as rd
    client, world = ui
    _regenerate(world)  # so Use the new draft is offered for the cv
    shown = world.selection("cv")
    real = svc.record_presented

    def presented_then_other_tab_moves_the_cv(*a, **k):
        out = real(*a, **k)
        rd.select_document(world.conn, settings=world.settings, account_id=V2_ACCOUNT,
                           application_workspace_id=world.ws, kind="cv",
                           document_version_id=shown["document_version_id"], expected_revision=shown["revision"],
                           actor="u", now=NOW)  # another tab: a new selection revision
        return out
    monkeypatch.setattr(svc, "record_presented", presented_then_other_tab_moves_the_cv)
    html = client.get(f"/workspaces/{world.ws}/review").text
    moved = world.selection("cv")
    assert moved["revision"] == shown["revision"] + 1
    replace_rev = re.search(r'data-review-action="replace" data-kind="cv" data-revision="(\d+)"', html).group(1)
    draft = re.search(r'data-review-action="use-new-draft"\s+data-kind="cv"\s+data-version="([^"]+)"'
                      r'\s+data-revision="(\d+)"', html)
    assert int(replace_rev) == int(draft.group(2)) == shown["revision"]  # the rendered snapshot's revision

    before = table_counts(world.conn)
    replaced = client.post(f"/api/workspaces/{world.ws}/review/documents/cv", data={"expected_revision": replace_rev},
                           files={"file": ("cv.docx", docx_bytes("stale"), "application/octet-stream")})
    selected = client.post(f"/api/workspaces/{world.ws}/review/documents/cv/select",
                           json={"document_version_id": draft.group(1), "expected_revision": int(draft.group(2))})
    assert (replaced.status_code, replaced.json()["detail"]) == (409, "stale_selection")
    assert (selected.status_code, selected.json()["detail"]) == (409, "stale_selection")
    assert diff_counts(before, table_counts(world.conn)) == {} and world.selection("cv") == moved


def test_the_state_api_exposes_the_snapshot_revisions(ui):
    client, world = ui
    body = client.get(f"/api/workspaces/{world.ws}/review/state").json()
    assert body["selection_revisions"] == {k: world.selection(k)["revision"] for k in ("cv", "cover_letter")}
