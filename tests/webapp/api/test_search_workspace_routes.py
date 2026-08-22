from __future__ import annotations

from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.config import Settings


def _client(tmp_path):
    return TestClient(create_app(Settings(db_path=tmp_path / "search-workspaces.sqlite3")))


class _Runner:
    def search(self, source, **kwargs):
        return [{
            "id": "planner-1", "title": "Project Planner", "company": "Energy Co",
            "location": "London", "date": "2026-08-20",
            "url": "https://jobs.example/planner-1", "description": "Plan work.",
            "work_mode": "hybrid", "regions": ["eu"], "countries": ["GB"],
            "skills": [],
        }]


def test_workspace_crud_and_optimistic_concurrency(tmp_path):
    with _client(tmp_path) as client:
        listed = client.get("/api/search-workspaces")
        assert [item["id"] for item in listed.json()["search_workspaces"]] == [
            "search_default"
        ]
        created = client.post(
            "/api/search-workspaces", json={"name": "Project Manager"}
        ).json()["search_workspace"]

        renamed = client.patch(
            f"/api/search-workspaces/{created['id']}",
            json={"name": "Programme Manager", "expected_revision": 1},
        )
        assert renamed.status_code == 200
        assert renamed.json()["search_workspace"]["revision"] == 2
        stale = client.patch(
            f"/api/search-workspaces/{created['id']}",
            json={"name": "Stale", "expected_revision": 1},
        )
        assert stale.status_code == 409

        archived = client.post(
            f"/api/search-workspaces/{created['id']}/archive",
            json={"expected_revision": 2},
        )
        assert archived.json()["search_workspace"]["status"] == "archived"
        restored = client.post(
            f"/api/search-workspaces/{created['id']}/restore",
            json={"expected_revision": 3},
        )
        assert restored.json()["search_workspace"]["status"] == "active"


def test_scoped_preferences_require_revision_and_remain_isolated(tmp_path):
    with _client(tmp_path) as client:
        other = client.post(
            "/api/search-workspaces", json={"name": "Project Manager"}
        ).json()["search_workspace"]
        payload = {"target_roles": ["Planner"]}

        missing = client.put(
            "/api/search-workspaces/search_default/user-profile", json=payload
        )
        assert missing.status_code == 428
        default_saved = client.put(
            "/api/search-workspaces/search_default/user-profile",
            headers={"If-Match": "0"},
            json=payload,
        )
        assert default_saved.status_code == 200
        other_saved = client.put(
            f"/api/search-workspaces/{other['id']}/user-profile",
            headers={"If-Match": "0"},
            json={"target_roles": ["Project Manager"]},
        )
        assert other_saved.status_code == 200

        assert client.get(
            "/api/search-workspaces/search_default/user-profile"
        ).json()["user_profile"]["payload"]["target_roles"] == ["Planner"]
        assert client.get(
            f"/api/search-workspaces/{other['id']}/user-profile"
        ).json()["user_profile"]["payload"]["target_roles"] == ["Project Manager"]
        stale = client.put(
            f"/api/search-workspaces/{other['id']}/user-profile",
            headers={"If-Match": "0"},
            json={"target_roles": ["Stale"]},
        )
        assert stale.status_code == 409


def test_explicit_workspace_pages_and_legacy_routes_select_context(tmp_path):
    with _client(tmp_path) as client:
        other = client.post(
            "/api/search-workspaces", json={"name": "Project Manager"}
        ).json()["search_workspace"]

        page = client.get(f"/search-workspaces/{other['id']}/preferences")
        assert page.status_code == 200
        assert "Project Manager" in page.text
        assert "Search preferences" in page.text
        assert client.get("/user-profile", follow_redirects=False).status_code == 307
        assert client.get("/discover", follow_redirects=False).status_code == 307


def test_scoped_discovery_is_isolated_but_promotion_is_globally_deduplicated(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "search-workspaces.sqlite3"))
    app.state.discovery_portal_runner = _Runner()
    with TestClient(app) as client:
        other = client.post(
            "/api/search-workspaces", json={"name": "Project Manager"}
        ).json()["search_workspace"]
        for workspace_id, role in (
            ("search_default", "Planner"), (other["id"], "Project Manager")
        ):
            saved = client.put(
                f"/api/search-workspaces/{workspace_id}/user-profile",
                headers={"If-Match": "0"},
                json={"target_roles": [role], "source_preferences": ["freehire-search"]},
            )
            assert saved.status_code == 200

        first = client.post(
            "/api/search-workspaces/search_default/discovery/search",
            json={"limit_per_source": 5},
        ).json()["candidate_ids"][0]
        second = client.post(
            f"/api/search-workspaces/{other['id']}/discovery/search",
            json={"limit_per_source": 5},
        ).json()["candidate_ids"][0]
        assert first != second

        first_promotion = client.post(
            f"/api/search-workspaces/search_default/discovery/candidates/{first}/promote"
        ).json()
        second_promotion = client.post(
            f"/api/search-workspaces/{other['id']}/discovery/candidates/{second}/promote"
        ).json()
        assert first_promotion["created"] is True
        assert second_promotion["created"] is False
        assert first_promotion["workspace"]["id"] == second_promotion["workspace"]["id"]


def test_archived_workspace_is_readable_but_rejects_preference_and_discovery_writes(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "search-workspaces.sqlite3"))
    app.state.discovery_portal_runner = _Runner()
    with TestClient(app) as client:
        other = client.post(
            "/api/search-workspaces", json={"name": "Archived track"}
        ).json()["search_workspace"]
        endpoint = f"/api/search-workspaces/{other['id']}"
        saved = client.put(
            f"{endpoint}/user-profile",
            headers={"If-Match": "0"},
            json={"target_roles": ["Planner"]},
        ).json()["user_profile"]
        client.post(
            f"{endpoint}/archive", json={"expected_revision": other["revision"]}
        )

        assert client.get(f"{endpoint}/user-profile").json()["user_profile"]["id"] == saved["id"]
        assert client.put(
            f"{endpoint}/user-profile",
            headers={"If-Match": str(saved["profile_revision"])},
            json={"target_roles": ["Changed"]},
        ).status_code == 400
        assert client.post(
            f"{endpoint}/discovery/search", json={"limit_per_source": 5}
        ).status_code == 400
