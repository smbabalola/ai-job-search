from __future__ import annotations

from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.db import connect
from webapp.persistence.discovery_sources import set_discovery_source_enabled


class Runner:
    def search(self, source, **kwargs):
        return [{
            "id": "planner-1", "title": "Project Planner", "company": "Energy Co",
            "location": "Aberdeen", "date": "2026-08-20",
            "url": "https://freehire.me/jobs/planner-1", "description": "Plan work.",
            "work_mode": "hybrid", "regions": ["eu"], "countries": ["GB"], "skills": [],
        }]


def test_discovery_search_lifecycle_and_idempotent_promotion(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "discovery.sqlite3"))
    app.state.discovery_portal_runner = Runner()
    with TestClient(app) as client:
        base = "/api/search-workspaces/search_default"
        client.put(f"{base}/user-profile", headers={"If-Match": "0"}, json={
            "target_roles": ["Project Planner"], "locations": ["Aberdeen"],
            "source_preferences": ["freehire-search"], "recency_days": 14,
        })
        searched = client.post(f"{base}/discovery/search", json={"limit_per_source": 5})
        assert searched.status_code == 200, searched.text
        candidate_id = searched.json()["candidate_ids"][0]
        page = client.get("/discover")
        assert page.status_code == 200
        assert "Project Planner" in page.text
        assert "No invented score" in page.text

        saved = client.patch(f"{base}/discovery/candidates/{candidate_id}", json={"status": "saved"})
        assert saved.json()["candidate"]["lifecycle_status"] == "saved"
        first = client.post(f"{base}/discovery/candidates/{candidate_id}/promote")
        second = client.post(f"{base}/discovery/candidates/{candidate_id}/promote")
        assert first.status_code == 200
        assert first.json()["created"] is True
        assert second.json()["created"] is False
        assert first.json()["workspace"]["id"] == second.json()["workspace"]["id"]
        assert len(client.get("/api/workspaces").json()["workspaces"]) == 1


def test_discovery_requires_profile_and_rejects_unknown_source(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "discovery.sqlite3"))
    app.state.discovery_portal_runner = Runner()
    with TestClient(app) as client:
        base = "/api/search-workspaces/search_default"
        assert client.post(f"{base}/discovery/search", json={}).status_code == 400
        client.put(f"{base}/user-profile", headers={"If-Match": "0"}, json={})
        invalid = client.post(f"{base}/discovery/search", json={"sources": ["made-up"]})
        assert invalid.status_code == 400
        assert "unsupported discovery sources" in invalid.json()["detail"]


def test_get_sources_returns_display_names_and_reflects_registry_disable(tmp_path):
    db_path = tmp_path / "discovery.sqlite3"
    app = create_app(Settings(db_path=db_path))
    app.state.discovery_portal_runner = Runner()
    with TestClient(app) as client:
        base = "/api/search-workspaces/search_default"

        response = client.get(f"{base}/discovery/sources")
        assert response.status_code == 200
        sources = response.json()["sources"]
        assert {s["source_id"]: s["display_name"] for s in sources} == {
            "freehire-search": "Freehire",
            "linkedin-search": "LinkedIn",
            "energy-jobline-search": "Energy Jobline",
        }

        conn = connect(db_path)
        set_discovery_source_enabled(conn, "energy-jobline-search", False)
        conn.close()

        response = client.get(f"{base}/discovery/sources")
        source_ids = {s["source_id"] for s in response.json()["sources"]}
        assert source_ids == {"freehire-search", "linkedin-search"}


def test_discovery_page_renders_display_names_not_raw_source_ids(tmp_path):
    db_path = tmp_path / "discovery.sqlite3"
    app = create_app(Settings(db_path=db_path))
    app.state.discovery_portal_runner = Runner()
    with TestClient(app) as client:
        base = "/api/search-workspaces/search_default"
        client.put(f"{base}/user-profile", headers={"If-Match": "0"}, json={
            "target_roles": ["Project Planner"], "locations": ["Aberdeen"],
        })

        page = client.get("/search-workspaces/search_default/discover")
        assert page.status_code == 200
        assert "Freehire" in page.text
        assert "LinkedIn" in page.text
        assert "Energy Jobline" in page.text
        assert 'value="freehire-search"' in page.text
        assert 'value="energy-jobline-search"' in page.text

        conn = connect(db_path)
        set_discovery_source_enabled(conn, "energy-jobline-search", False)
        conn.close()

        page = client.get("/search-workspaces/search_default/discover")
        assert "Energy Jobline" not in page.text
        assert 'value="energy-jobline-search"' not in page.text
        assert "Freehire" in page.text
