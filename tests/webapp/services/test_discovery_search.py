from __future__ import annotations

import pytest

from webapp.persistence.accounts import create_account
from webapp.persistence.db import connect, init_db
from webapp.persistence.discovery_sources import set_discovery_source_enabled
from webapp.persistence.user_profile import get_current_user_profile, save_user_profile
from webapp.persistence.search_workspaces import create_search_workspace
from webapp.services.discovery import DiscoveryServiceError, discovery_run_is_stale, run_discovery_search


class FakeRunner:
    def __init__(self):
        self.calls = []

    def search(self, source, **kwargs):
        self.calls.append((source, kwargs))
        if source == "linkedin-search":
            raise RuntimeError("rate limited")
        return [{
            "id": "planner-1", "title": "Project Planner", "company": "Energy Co",
            "location": "Aberdeen", "date": "2026-08-20",
            "url": "https://freehire.me/jobs/planner-1", "description": "Plan work.",
            "work_mode": "hybrid", "regions": ["eu"], "countries": ["GB"], "skills": [],
        }]


class FakeRunnerThatSucceedsForAnySource:
    """Records which sources were actually invoked, without ever failing
    one. Used to prove which sources a call to run_discovery_search
    actually reached, independent of any per-source content."""

    def __init__(self):
        self.calls = []

    def search(self, source, **kwargs):
        self.calls.append((source, kwargs))
        return [{
            "id": f"{source}-1", "title": "Project Planner", "company": "Energy Co",
            "location": "Aberdeen", "date": "2026-08-20",
            "url": f"https://example.test/{source}/1", "description": "Plan work.",
            "work_mode": "hybrid", "regions": ["eu"], "countries": ["GB"], "skills": [],
        }]


class FakeRunnerWithFailingEnergyJobline:
    """Proves a failure isolated to energy-jobline-search never blocks the
    other configured sources from completing in the same run — the same
    per-source isolation contract already proven for linkedin-search above,
    now proven for the new source specifically."""

    def __init__(self):
        self.calls = []

    def search(self, source, **kwargs):
        self.calls.append((source, kwargs))
        if source == "energy-jobline-search":
            raise RuntimeError("energy jobline unreachable")
        return [{
            "id": "planner-1", "title": "Project Planner", "company": "Energy Co",
            "location": "Aberdeen", "date": "2026-08-20",
            "url": "https://freehire.me/jobs/planner-1", "description": "Plan work.",
            "work_mode": "hybrid", "regions": ["eu"], "countries": ["GB"], "skills": [],
        }]


def _connection(tmp_path):
    path = tmp_path / "search.db"
    init_db(path)
    return connect(path)


def test_search_requires_user_profile(tmp_path):
    with pytest.raises(DiscoveryServiceError, match="User Profile"):
        run_discovery_search(_connection(tmp_path), FakeRunner())


def test_search_fingerprints_preferences_and_isolates_source_failure(tmp_path):
    conn = _connection(tmp_path)
    profile = save_user_profile(conn, {
        "target_roles": ["Project Planner"], "locations": ["Aberdeen"],
        "search_terms": ["project controls"], "source_preferences": ["freehire-search", "linkedin-search"],
        "recency_days": 7,
    })
    runner = FakeRunner()

    result = run_discovery_search(conn, runner, limit_per_source=10)

    assert result["run"]["status"] == "partial"
    assert result["run"]["user_profile_content_id"] == profile["content_id"]
    assert result["run"]["request"]["queries"] == ["project controls"]
    assert result["run"]["source_status"]["freehire-search"]["accepted"] == 1
    assert result["run"]["source_status"]["linkedin-search"]["status"] == "failed"
    assert len(result["candidate_ids"]) == 1
    assert conn.execute("select count(*) from workspaces").fetchone()[0] == 0
    assert discovery_run_is_stale(conn, result["run"]) is False
    save_user_profile(conn, {"target_roles": ["Changed role"]})
    assert discovery_run_is_stale(conn, result["run"]) is True


def test_energy_jobline_source_failure_is_isolated_from_other_sources(tmp_path):
    conn = _connection(tmp_path)
    save_user_profile(conn, {
        "target_roles": ["Drilling Engineer"], "locations": ["Aberdeen"],
        "search_terms": ["drilling"],
        "source_preferences": ["freehire-search", "energy-jobline-search"],
        "recency_days": 7,
    })
    runner = FakeRunnerWithFailingEnergyJobline()

    result = run_discovery_search(conn, runner, limit_per_source=10)

    assert result["run"]["status"] == "partial"
    assert result["run"]["source_status"]["freehire-search"]["accepted"] == 1
    assert result["run"]["source_status"]["energy-jobline-search"]["status"] == "failed"
    assert len(result["candidate_ids"]) == 1


def test_preference_staleness_is_derived_and_isolated_by_search_workspace(tmp_path):
    conn = _connection(tmp_path)
    other = create_search_workspace(conn, name="Project Manager")
    save_user_profile(conn, {"target_roles": ["Planner"]})
    save_user_profile(
        conn,
        {"target_roles": ["Project Manager"]},
        search_workspace_id=other["id"],
    )
    default_run = run_discovery_search(
        conn, FakeRunner(), sources=["freehire-search"]
    )["run"]
    other_run = run_discovery_search(
        conn,
        FakeRunner(),
        sources=["freehire-search"],
        search_workspace_id=other["id"],
    )["run"]

    other_current = get_current_user_profile(conn, other["id"])
    save_user_profile(
        conn,
        {"target_roles": ["Programme Manager"]},
        search_workspace_id=other["id"],
        expected_revision=other_current["profile_revision"],
    )

    assert discovery_run_is_stale(
        conn, default_run, search_workspace_id="search_default"
    ) is False
    assert discovery_run_is_stale(
        conn, other_run, search_workspace_id=other["id"]
    ) is True
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(discovery_runs)")
    }
    assert "stale" not in columns


def test_search_and_staleness_use_the_explicit_nondefault_account(tmp_path):
    conn = _connection(tmp_path)
    create_account(conn, account_id="account_search_b", display_name="Search B")
    search = create_search_workspace(
        conn, name="Search B", account_id="account_search_b"
    )
    profile = save_user_profile(
        conn,
        {"target_roles": ["Project Planner"]},
        search_workspace_id=search["id"],
        account_id="account_search_b",
    )

    run = run_discovery_search(
        conn,
        FakeRunner(),
        search_workspace_id=search["id"],
        sources=["freehire-search"],
        account_id="account_search_b",
    )["run"]

    assert run["user_profile_content_id"] == profile["content_id"]
    assert discovery_run_is_stale(
        conn,
        run,
        search_workspace_id=search["id"],
        account_id="account_search_b",
    ) is False


def test_empty_preferences_default_to_all_enabled_sources_only(tmp_path):
    conn = _connection(tmp_path)
    save_user_profile(conn, {
        "target_roles": ["Planner"], "locations": ["Aberdeen"],
        "source_preferences": [],  # empty -> default to every available source
    })
    runner = FakeRunnerThatSucceedsForAnySource()

    run_discovery_search(conn, runner, limit_per_source=10)

    called_sources = {source for source, _ in runner.calls}
    assert called_sources == {"freehire-search", "linkedin-search", "energy-jobline-search"}


def test_disabling_energy_jobline_removes_it_from_default_discovery(tmp_path):
    conn = _connection(tmp_path)
    set_discovery_source_enabled(conn, "energy-jobline-search", False)
    save_user_profile(conn, {
        "target_roles": ["Planner"], "locations": ["Aberdeen"],
        "source_preferences": [],  # empty -> defaults to available sources
    })
    runner = FakeRunnerThatSucceedsForAnySource()

    run_discovery_search(conn, runner, limit_per_source=10)

    called_sources = {source for source, _ in runner.calls}
    assert called_sources == {"freehire-search", "linkedin-search"}
    assert "energy-jobline-search" not in called_sources


def test_explicitly_requesting_a_disabled_provider_is_rejected(tmp_path):
    conn = _connection(tmp_path)
    set_discovery_source_enabled(conn, "energy-jobline-search", False)
    save_user_profile(conn, {"target_roles": ["Planner"], "locations": ["Aberdeen"]})
    runner = FakeRunnerThatSucceedsForAnySource()

    with pytest.raises(DiscoveryServiceError, match="unsupported discovery sources"):
        run_discovery_search(
            conn, runner, sources=["energy-jobline-search"], limit_per_source=10,
        )
    assert runner.calls == []


def test_partially_stale_saved_preference_keeps_the_remaining_valid_selection(tmp_path):
    # The user saved this preference list back when all three sources were
    # enabled. Disabling one afterward narrows the explicit selection down
    # to what's still available -- it does not broaden back out to sources
    # the user never selected, and it does not hard-fail as long as at
    # least one of the originally-selected sources remains available.
    conn = _connection(tmp_path)
    save_user_profile(conn, {
        "target_roles": ["Planner"], "locations": ["Aberdeen"],
        "source_preferences": ["freehire-search", "energy-jobline-search"],
    })
    set_discovery_source_enabled(conn, "energy-jobline-search", False)
    runner = FakeRunnerThatSucceedsForAnySource()

    result = run_discovery_search(conn, runner, limit_per_source=10)

    called_sources = {source for source, _ in runner.calls}
    assert called_sources == {"freehire-search"}
    assert "linkedin-search" not in called_sources  # never implicitly added
    assert result["run"]["request"]["sources"] == ["freehire-search"]


def test_fully_stale_explicit_preference_does_not_broaden_to_other_providers(tmp_path):
    # The user deliberately selected only Energy Jobline. An admin later
    # disables it. The old behavior silently fell back to "every available
    # source" here, which would have quietly started searching
    # Freehire/LinkedIn the user never chose -- broadening their search
    # without consent. The corrected behavior must refuse instead, leaving
    # the user's explicit choice intact and surfacing that it is currently
    # unavailable rather than substituting a different choice for them.
    conn = _connection(tmp_path)
    save_user_profile(conn, {
        "target_roles": ["Planner"], "locations": ["Aberdeen"],
        "source_preferences": ["energy-jobline-search"],
    })
    set_discovery_source_enabled(conn, "energy-jobline-search", False)
    runner = FakeRunnerThatSucceedsForAnySource()

    with pytest.raises(DiscoveryServiceError, match="saved discovery sources are currently unavailable"):
        run_discovery_search(conn, runner, limit_per_source=10)

    assert runner.calls == []  # nothing was searched -- no implicit broadening


def test_unknown_db_source_cannot_become_runnable_via_explicit_request(tmp_path):
    # Simulates a stray/ahead-of-schedule registry row for a source with no
    # code-level adapter -- even if such a row existed and were enabled,
    # run_discovery_search must never invoke it, because
    # available_discovery_source_ids intersects against SOURCE_CLI_PATHS.
    conn = _connection(tmp_path)
    conn.execute(
        "INSERT INTO discovery_source_settings "
        "(source_id, display_name, enabled, updated_at) "
        "VALUES ('rigzone-search', 'Rigzone', 1, 'now')"
    )
    conn.commit()
    save_user_profile(conn, {"target_roles": ["Planner"], "locations": ["Aberdeen"]})
    runner = FakeRunnerThatSucceedsForAnySource()

    with pytest.raises(DiscoveryServiceError, match="unsupported discovery sources"):
        run_discovery_search(conn, runner, sources=["rigzone-search"], limit_per_source=10)
    assert runner.calls == []


def test_freehire_linkedin_energy_jobline_execution_is_unchanged_when_all_enabled(tmp_path):
    # Confirms the registry addition is a pure filtering layer -- when
    # nothing is disabled, run_discovery_search's request/execution shape
    # for the three existing sources is identical to before the registry.
    conn = _connection(tmp_path)
    save_user_profile(conn, {
        "target_roles": ["Planner"], "locations": ["Aberdeen"],
        "source_preferences": ["freehire-search", "linkedin-search", "energy-jobline-search"],
    })
    runner = FakeRunnerThatSucceedsForAnySource()

    result = run_discovery_search(conn, runner, limit_per_source=10)

    assert result["run"]["request"]["sources"] == [
        "freehire-search", "linkedin-search", "energy-jobline-search",
    ]
    called_sources = {source for source, _ in runner.calls}
    assert called_sources == {"freehire-search", "linkedin-search", "energy-jobline-search"}


def test_account_and_workspace_scoping_is_unaffected_by_the_registry(tmp_path):
    # The discovery_source_settings table has no account_id/workspace_id
    # column -- it is a single global registry, not scoped per account. This
    # pins that a disabled source is disabled for every account/workspace,
    # and that existing account isolation for everything else is untouched.
    conn = _connection(tmp_path)
    create_account(conn, account_id="account_b", display_name="B")
    other = create_search_workspace(conn, name="Other", account_id="account_b")
    save_user_profile(conn, {"target_roles": ["Planner"], "locations": ["Aberdeen"]})
    save_user_profile(
        conn, {"target_roles": ["Planner"], "locations": ["Aberdeen"]},
        search_workspace_id=other["id"], account_id="account_b",
    )
    set_discovery_source_enabled(conn, "energy-jobline-search", False)

    runner_default = FakeRunnerThatSucceedsForAnySource()
    run_discovery_search(conn, runner_default, limit_per_source=10)
    runner_other = FakeRunnerThatSucceedsForAnySource()
    run_discovery_search(
        conn, runner_other, search_workspace_id=other["id"],
        account_id="account_b", limit_per_source=10,
    )

    for runner in (runner_default, runner_other):
        called_sources = {source for source, _ in runner.calls}
        assert "energy-jobline-search" not in called_sources
        assert called_sources == {"freehire-search", "linkedin-search"}
