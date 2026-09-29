"""6D-B human-facing pages (spec §8.5, §9, §16): the fill-plan page from one
snapshot, classification confirmation on the review page, fill statuses on
the prepared list and in the dossier. No Submit or release control."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.product.fill_observation_fixtures import element
from tests.webapp.services.fill_fixtures import (  # noqa: F401
    NOW, V2_ACCOUNT, fill_world, grant_world, observation_doc, store_observation, v2_chain,
)
from tests.webapp.services.review_fixtures import diff_counts, table_counts
from webapp.app import create_app
from webapp.persistence import fill as f
from webapp.persistence import review_approval as ra
from webapp.services.fill_results import APPROVED_WORDING, READY_WORDING, fill_status_label

TEMPLATES = Path(__file__).resolve().parents[3] / "webapp" / "templates"
FILL_ACTIONS = {"confirm", "map", "new-question"}


@pytest.fixture
def ui(fill_world):
    with TestClient(create_app(fill_world.settings)) as client:
        yield client, fill_world


def page(client, w):
    r = client.get(f"/workspaces/{w.ws}/fill-plan")
    assert r.status_code == 200, r.text
    return r.text


def shown_hash(html):
    match = re.search(r'data-displayed-plan-hash="([^"]+)"', html)
    return match.group(1) if match else None


def pair(w):
    from webapp.persistence.handoff import create_extension_credential
    create_extension_credential(w.conn, account_id=V2_ACCOUNT, secret_hash="h" * 64)


def test_the_page_shows_every_row_with_the_cleartext_write_value(ui):
    client, w = ui
    html = page(client, w)
    assert 'data-fill-row="gh:email" data-action-kind="WRITE"' in html and "ada@example.com" in html
    assert "1 month" in html and 'data-action-kind="ATTACH_LOCAL"' in html
    assert "not part of the application" in html  # the IGNORE row shows its proof
    assert shown_hash(html) and "Confirm fill plan" in html


def test_the_page_is_read_only(ui):
    client, w = ui
    before = table_counts(w.conn)
    page(client, w)
    assert diff_counts(before, table_counts(w.conn)) == {}


def test_the_page_renders_one_snapshot_and_a_stale_confirm_is_refused(ui, monkeypatch):
    client, w = ui
    from webapp.api import review_pages
    from webapp.services.review_approval import revoke
    real = review_pages.fill_plans.fill_plan_presentation

    def racing(conn, **kw):
        view = real(conn, **kw)  # the snapshot the page renders ...
        revoke(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws, actor="u", now=NOW)
        return view  # ... and the state moves on before the user clicks

    monkeypatch.setattr(review_pages.fill_plans, "fill_plan_presentation", racing)
    html = page(client, w)
    hashed = shown_hash(html)
    rows = re.findall(r'data-fill-row="([^"]+)"', html)
    assert hashed and len(rows) == 5  # the rows and the hash come from the same read
    r = client.post(f"/api/workspaces/{w.ws}/fill-plan/confirm",
                    json={"observation_id": w.observation["id"], "displayed_plan_hash": hashed})
    assert (r.status_code, r.json()["detail"]) == (409, "stale_plan")
    assert w.conn.execute("SELECT COUNT(*) FROM fill_plan_confirmations").fetchone()[0] == 0


def test_no_confirm_until_every_row_is_resolved(ui):
    client, w = ui
    other = element("gh:contact", control_kind="email", type="email", label="Contact address",
                    question="Contact address", name="contact", id="contact", required=True)
    doc = observation_doc(other)
    doc["elements"] = [e for e in doc["elements"] if e["page_field_key"] != "gh:email"]
    obs = store_observation(w, doc)
    html = page(client, w)
    assert 'data-needs-review="gh:contact"' in html and shown_hash(html) is None
    assert "Confirm fill plan" not in html
    r = client.post(f"/api/workspaces/{w.ws}/fill-plan/mappings", json={
        "observation_id": obs["id"], "page_field_key": "gh:contact", "answer_key": "contact:email", "choice": "MAP"})
    assert r.status_code == 200, r.text
    assert shown_hash(page(client, w))


def test_confirming_the_displayed_plan_then_the_page_shows_it_confirmed(ui):
    client, w = ui
    hashed = shown_hash(page(client, w))
    r = client.post(f"/api/workspaces/{w.ws}/fill-plan/confirm",
                    json={"observation_id": w.observation["id"], "displayed_plan_hash": hashed})
    assert r.status_code == 200, r.text
    html = page(client, w)
    assert "data-fill-confirmed" in html and "Confirm fill plan" not in html


def test_status_wording_approved_then_ready(ui):
    client, w = ui
    assert APPROVED_WORDING in page(client, w)
    client.post(f"/api/workspaces/{w.ws}/fill-plan/confirm",
                json={"observation_id": w.observation["id"], "displayed_plan_hash": shown_hash(page(client, w))})
    pair(w)
    html = page(client, w)
    assert READY_WORDING in html and APPROVED_WORDING not in html


@pytest.mark.parametrize("status,effective,expected", [
    ({"status": "NOT_STARTED", "ready_to_fill": False}, True, APPROVED_WORDING),
    ({"status": "NOT_STARTED", "ready_to_fill": False}, False, None),
    ({"status": "NOT_STARTED", "ready_to_fill": True}, True, READY_WORDING),
    ({"status": "FILLING"}, True, "Filling (the page is quarantined)"),
    ({"status": "PLAN_NEEDS_REVIEW", "ready_to_fill": False}, True, "The fill plan needs your review"),
    ({"status": "UNSUPPORTED_FORM", "ready_to_fill": False}, True, "This form is not supported for safe filling"),
    ({"status": "FILL_STOPPED", "stop_reason": "EXECUTOR_LOST", "ready_to_fill": False}, True,
     "Filling stopped (EXECUTOR_LOST)"),
    ({"status": "FILLED_AWAITING_SUBMISSION", "stale": False}, True,
     "Filled and quarantined; submission is a later, separate step"),
    ({"status": "FILLED_AWAITING_SUBMISSION", "stale": True}, True,
     "Filled and quarantined; submission is a later, separate step (stale: the approval or plan changed since)"),
    ({"status": "FILLED_CONTEXT_UNVERIFIED", "stale": False}, True,
     "Filled earlier, but the filled page can no longer be verified"),
])
def test_status_wording_per_state(status, effective, expected):
    assert fill_status_label(status, approval_effective=effective) == expected


def test_the_prepared_list_and_dossier_show_the_fill_status(ui):
    client, w = ui
    html = client.get("/applications/prepared").text
    assert APPROVED_WORDING in html and f'href="/workspaces/{w.ws}/fill-plan"' in html
    from tests.webapp.services.test_fill_runs import start
    run = start(w)
    dossier = client.get(f"/workspaces/{w.ws}/autonomy").text
    assert 'data-dossier-section="fill"' in dossier and f'data-fill-run="{run["id"]}"' in dossier


def test_another_accounts_fill_plan_page_is_404(ui):
    client, w = ui
    from webapp.persistence.accounts import create_account
    from webapp.persistence.workspaces import create_workspace
    create_account(w.conn, account_id="account_other", display_name="Other")
    other = create_workspace(w.conn, company="Other", title="Role", account_id="account_other")["id"]
    assert client.get(f"/workspaces/{other}/fill-plan").status_code == 404


def test_no_submit_or_release_control_on_the_fill_pages(ui):
    client, w = ui
    html = page(client, w)
    assert set(re.findall(r'data-fill-action="([^"]+)"', html)) <= FILL_ACTIONS
    controls = re.findall(r"<button[^>]*>([^<]*)</button>", html)
    assert not [c for c in controls if re.search(r"submit|release|lift|unquarantine", c, re.IGNORECASE)], controls
    assert not re.search(r'<form[^>]*action="', html, re.IGNORECASE)
    # 6E-A: the only submit routes are the Phase 3 confirmation and the human-authorized inventory.
    from tests.webapp.test_submit_structure import CONFIRM_SUBMISSION, SUBMIT_ROUTES_6E_A
    submit_routes = {r.path for r in client.app.routes if "submit" in getattr(r, "path", "").lower()}
    assert submit_routes <= {CONFIRM_SUBMISSION} | SUBMIT_ROUTES_6E_A


def test_new_and_modified_templates_never_use_the_global_data_action_hook():
    for name in ("fill_plan.html", "review_application.html", "prepared_applications.html",
                 "autonomy_dossier.html"):
        assert "data-action=" not in (TEMPLATES / name).read_text(encoding="utf-8"), name


# ---- classification confirmation on the review page (spec §9) ---------------------------

@pytest.fixture
def unclassified(ui):
    client, w = ui
    q = element("gh:q", label="How long is your notice?", question="How long is your notice?", name="q", id="q",
                required=True)
    obs = store_observation(w, observation_doc(q))
    from webapp.services import fill_plans as fp
    fp.propose_plan(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                    observation_id=obs["id"], now=NOW)
    [delta] = ra.open_deltas(w.conn, w.ws)
    return client, w, delta, f.latest_proposal(w.conn, delta["id"])


def test_the_review_page_offers_confirm_meaning_with_the_displayed_proposal(unclassified):
    client, w, delta, proposal = unclassified
    html = client.get(f"/workspaces/{w.ws}/review").text
    assert f'data-classification="{delta["id"]}"' in html
    assert f'data-proposal="{proposal["id"]}"' in html and "Confirm meaning" in html
    assert 'data-review-action="confirm-classification"' in html


def test_confirm_meaning_writes_only_the_classification(unclassified):
    client, w, delta, proposal = unclassified
    before = table_counts(w.conn)
    r = client.post(f"/api/workspaces/{w.ws}/review/deltas/{delta['id']}/classification/confirm",
                    json={"displayed_proposal_id": proposal["id"], "subject": proposal["subject"]})
    assert r.status_code == 200, r.text
    changed = diff_counts(before, table_counts(w.conn))
    assert set(changed) == {"review_deltas", "application_review_events", "delta_classification_confirmations"}
    assert not [e for e in ra.events(w.conn, w.ws) if e["event"] == "ANSWER_EDITED"
                and e["created_at"] > r.json()["created_at"]]
