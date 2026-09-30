"""Bundle 7 Task 24 (spec §15.2): the guided onboarding walk — each step writes
through the real services, and the account ends ready to prepare."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from product.cv_extraction_providers import FakeCvExtractionProvider
from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
from tests.webapp.services.review_fixtures import docx_bytes
from webapp.app import create_app
from webapp.config import Settings

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SKILL = {"target": "PROFILE_ENTRY", "kind": "technical_skill", "confidence": 0.8, "source_excerpt": "WellPlan",
         "fields": {"subsection": "Software", "value": "WellPlan"}}


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        auth_required_in_local=True)
    app = create_app(settings)
    app.state.cv_extraction_provider = FakeCvExtractionProvider([SKILL])
    with TestClient(app) as c:
        publish_legal_documents(settings)
        sign_up_and_verify(c)
        sign_in(c)
        yield c


def _post(client, url, **kwargs):
    return client.post(url, headers={"X-CSRF-Token": csrf_token(client)}, follow_redirects=False, **kwargs)


def test_every_step_page_renders_and_the_dashboard_shows_the_checklist(client):
    for step in ("about", "cv", "import", "eligibility", "preferences", "families", "rules", "extension"):
        page = client.get(f"/onboarding/{step}")
        assert page.status_code == 200, step
    dashboard = client.get("/")
    assert "data-onboarding-checklist" in dashboard.text
    assert client.get("/onboarding", follow_redirects=False).headers["location"] == "/onboarding/about"


def test_the_walk_leaves_the_account_ready_to_prepare(client):
    assert _post(client, "/onboarding/about", data={"display_name": "Ada Lovelace", "phone": "+44 7700 900000",
                                                     "city": "Aberdeen", "country": "gb"}).headers["location"] \
        == "/onboarding/cv"
    assert _post(client, "/onboarding/cv", data={"title": "My CV"},
                 files={"file": ("cv.docx", docx_bytes("Drilling Engineer, WellPlan"), DOCX)}).status_code == 303
    item_id = client.get("/api/cvs").json()["items"][0]["id"]
    assert client.get("/api/cv-strategy").json()["doc"]["default"] == {"mode": "LATEST_VERSION", "item_id": item_id}
    assert _post(client, "/onboarding/import", data={"item_id": item_id}).status_code == 303
    page = client.get("/onboarding/import")
    assert "WellPlan" in page.text
    proposals = client.get("/api/onboarding").json()  # nothing accepted yet
    assert proposals["state"]["steps"]["import"] == "TODO"
    import re
    proposal_id = re.search(r'name="resolution_([^"]+)"', page.text).group(1)
    assert _post(client, "/onboarding/import/resolve", data={f"resolution_{proposal_id}": "ACCEPTED"}).status_code == 303
    assert _post(client, "/onboarding/eligibility", data={"country": "GB", "right_to_work": "yes", "sponsorship": "no",
                                                          "notice_period": "1 month"}).status_code == 303
    assert _post(client, "/onboarding/preferences", data={"target_roles": "Drilling Engineer",
                                                          "locations": "Aberdeen"}).status_code == 303
    assert _post(client, "/onboarding/families", data={
        "default_cv": f"latest:{item_id}", "family_name_0": "Drilling Engineer",
        "family_words_0": "drilling engineer", "family_cv_0": f"latest:{item_id}"}).status_code == 303
    assert _post(client, "/onboarding/rules/skip").status_code == 303
    body = client.get("/api/onboarding").json()
    assert body["readiness"]["prepare_ok"] is True and body["readiness"]["prepare_missing"] == []
    assert body["readiness"]["fill_missing"] == ["extension_device"]
    assert _post(client, "/onboarding/extension/done").status_code == 303
    assert client.get("/api/onboarding").json()["state"]["complete"] is True
    assert "data-onboarding-checklist" not in client.get("/").text


def test_the_first_step_cannot_be_skipped_over_http(client):
    assert _post(client, "/onboarding/about/skip").status_code == 400
