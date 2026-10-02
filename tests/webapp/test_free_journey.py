"""Bundle 7 spec §22.2 / §25.4: the Free-user journey at the API level.

Sign up -> Free (no payment) -> onboard -> prepare up to the dev Free
allowance -> the next prepare is refused ALLOWANCE_EXHAUSTED with an upgrade
path to Pro -> the applications already prepared stay reviewable and
fillable (the fill start needs the assisted-fill feature only, never an
allowance)."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from product.cv_extraction_providers import FakeCvExtractionProvider
from product.job_understanding_providers import DeterministicFakeProvider as UnderstandingFake
from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
from tests.webapp.fixtures.acceptance.fixtures import provider_candidate, source_record
from tests.webapp.services.review_fixtures import docx_bytes
from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.db import connect

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SKILL = {"target": "PROFILE_ENTRY", "kind": "technical_skill", "confidence": 0.8, "source_excerpt": "WellPlan",
         "fields": {"subsection": "Software", "value": "WellPlan"}}
FREE_PREPARES = 3  # product/plans/plan-catalog.dev.json: free applications.prepare


def _post(client, url, **kwargs):
    return client.post(url, headers={"X-CSRF-Token": csrf_token(client)}, follow_redirects=False, **kwargs)


def onboard(client) -> str:
    """The guided onboarding walk (spec §15.2), as a person submits it. Returns the CV item id."""
    assert _post(client, "/onboarding/about", data={"display_name": "Ada Lovelace", "phone": "+44 7700 900000",
                                                     "city": "Aberdeen", "country": "gb"}).status_code == 303
    assert _post(client, "/onboarding/cv", data={"title": "My CV"},
                 files={"file": ("cv.docx", docx_bytes("Drilling Engineer, WellPlan"), DOCX)}).status_code == 303
    item_id = client.get("/api/cvs").json()["items"][0]["id"]
    assert _post(client, "/onboarding/import", data={"item_id": item_id}).status_code == 303
    proposal_ids = sorted(set(re.findall(r'name="resolution_([^"]+)"', client.get("/onboarding/import").text)))
    assert proposal_ids
    assert _post(client, "/onboarding/import/resolve",
                 data={f"resolution_{pid}": "ACCEPTED" for pid in proposal_ids}).status_code == 303
    assert _post(client, "/onboarding/eligibility", data={"country": "GB", "right_to_work": "yes", "sponsorship": "no",
                                                          "notice_period": "1 month"}).status_code == 303
    assert _post(client, "/onboarding/preferences", data={"target_roles": "Drilling Engineer",
                                                          "locations": "Aberdeen"}).status_code == 303
    assert _post(client, "/onboarding/families", data={
        "default_cv": f"latest:{item_id}", "family_name_0": "Drilling Engineer",
        "family_words_0": "drilling engineer", "family_cv_0": f"latest:{item_id}"}).status_code == 303
    assert _post(client, "/onboarding/rules/skip").status_code == 303
    assert client.get("/api/onboarding").json()["readiness"]["prepare_ok"] is True
    return item_id


def capture(client, n: int) -> str:
    record = {**source_record(), "title": f"Data Engineer {n}"}
    response = client.post("/api/workspaces", headers={"X-CSRF-Token": csrf_token(client)}, json={
        "company": "München Evidence Labs", "title": record["title"], "source_record": record,
        "source_record_origin": "manual_entry"})
    assert response.status_code == 201, response.text
    return response.json()["workspace"]["id"]


class CountingUnderstanding(UnderstandingFake):
    def __init__(self, payload):
        super().__init__(payload)
        self.calls = []

    def extract(self, request):
        self.calls.append(1)
        return super().extract(request)


@pytest.fixture
def world(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        auth_required_in_local=True)
    app = create_app(settings)
    app.state.cv_extraction_provider = FakeCvExtractionProvider([SKILL])
    app.state.job_understanding_provider = CountingUnderstanding(provider_candidate())
    with TestClient(app) as client:
        publish_legal_documents(settings)
        sign_up_and_verify(client)
        sign_in(client)
        yield SimpleNamespace(app=app, client=client, settings=settings)


def test_free_user_prepares_to_the_allowance_then_gets_an_upgrade_path(world):
    client = world.client
    # Free: no checkout, straight back to the product
    chosen = _post(client, "/plans/choose", data={"plan_id": "free"})
    assert (chosen.status_code, chosen.headers["location"]) == (303, "/")
    onboard(client)
    me = client.get("/auth/me").json()

    prepared = []
    for n in range(FREE_PREPARES):
        ws = capture(client, n)
        response = _post(client, f"/api/workspaces/{ws}/understand", json={"request_id": f"r{n}"})
        assert response.status_code == 200, response.text
        prepared.append(ws)
    usage = {row["allowance"]: row for row in client.get("/api/usage").json()["usage"]}
    assert (usage["applications.prepare"]["used"], usage["applications.prepare"]["limit"]) == (FREE_PREPARES,
                                                                                              FREE_PREPARES)

    # one more application: refused before any AI call, with the way up
    calls_before = len(world.app.state.job_understanding_provider.calls)
    extra = capture(client, FREE_PREPARES)
    refused = _post(client, f"/api/workspaces/{extra}/understand", json={"request_id": "over"})
    assert refused.status_code == 402, refused.text
    error = refused.json()
    assert error["error"] == "ALLOWANCE_EXHAUSTED"
    assert error["detail"]["allowance"] == "applications.prepare" and error["detail"]["upgrade_to"] == "pro"
    assert len(world.app.state.job_understanding_provider.calls) == calls_before
    usage = {row["allowance"]: row for row in client.get("/api/usage").json()["usage"]}
    assert usage["applications.prepare"]["used"] == FREE_PREPARES  # the refusal consumed nothing

    # re-running a stage of an application already prepared this period is not a new prepare
    again = _post(client, f"/api/workspaces/{prepared[0]}/understand", json={"request_id": "again"})
    assert again.status_code == 200, again.text

    # what was prepared keeps working: the review and the fill start (feature only, never an allowance)
    assert client.get(f"/workspaces/{prepared[0]}").status_code == 200
    assert client.get(f"/api/workspaces/{prepared[0]}/review").status_code == 200
    from webapp.api import fill_extension
    from webapp.persistence.handoff import create_handoff_session
    from webapp.services import fill_runs
    from webapp.services.handoff import SessionScope
    conn = connect(world.settings)
    try:
        pack = conn.execute("SELECT id FROM artifacts WHERE workspace_id = ? ORDER BY rowid LIMIT 1",
                            (prepared[0],)).fetchone()[0]
        session = create_handoff_session(conn, account_id=me["account_id"], workspace_id=prepared[0],
                                         pack_artifact_id=pack, target_url="https://jobs.example.test/1",
                                         target_domain="jobs.example.test", ats_adapter_id="greenhouse",
                                         ats_adapter_version="2")
        conn.commit()
        started = []
        original = fill_runs.start_run
        fill_runs.start_run = lambda *a, **k: started.append(1) or {"id": "run"}
        try:
            scope = SessionScope(account_id=me["account_id"], handoff_session_id=session["id"],
                                 workspace_id=prepared[0], pack_artifact_id=pack)
            body = fill_extension.StartBody(executor_instance_id="e", browser_session_id="b", execution_tab_id=1)
            assert fill_extension.post_run(session["id"], body, SimpleNamespace(app=world.app), conn, scope) \
                == {"id": "run"}
        finally:
            fill_runs.start_run = original
        assert started == [1]
    finally:
        conn.close()
