from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from product.discovery_search import (
    CliDiscoveryPortalRunner,
    DiscoverySourceError,
    SOURCE_CLI_PATHS,
    SUPPORTED_DISCOVERY_SOURCES,
    available_discovery_source_ids,
)
from product.discovery_sources import portal_result_to_source_record


def test_cli_runner_uses_allowlisted_argv_without_shell_and_bounds_linkedin_detail(tmp_path, monkeypatch):
    for source in ("freehire-search", "linkedin-search"):
        cli = tmp_path / f".agents/skills/{source}/cli/src/cli.ts"
        cli.parent.mkdir(parents=True)
        cli.write_text("// fixture", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "detail" in argv:
            payload = {
                "id": argv[argv.index("detail") + 1], "title": "Planner", "company": "Energy Co",
                "url": "https://linkedin.com/jobs/view/planner-4426311357", "description": "Plan work.",
            }
        else:
            payload = {"meta": {"count": 1}, "results": [{"id": "4426311357"}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("product.discovery_search.subprocess.run", fake_run)
    runner = CliDiscoveryPortalRunner(tmp_path)

    results = runner.search(
        "linkedin-search", queries=["planner"], locations=["London"], recency_days=10, limit=1
    )

    assert results[0]["description"] == "Plan work."
    assert len(calls) == 2
    assert all(call[1]["shell"] is False for call in calls)
    assert calls[0][0][:2] == ["bun", "run"]
    assert calls[0][0][-2:] == ["--jobage", "14"]
    assert calls[1][0][-3:] == ["4426311357", "--format", "json"]


def test_cli_runner_rejects_unknown_source_and_linkedin_without_location(tmp_path):
    runner = CliDiscoveryPortalRunner(tmp_path)
    with pytest.raises(DiscoverySourceError, match="unsupported"):
        runner.search("invented", queries=[], locations=[], recency_days=7, limit=10)
    with pytest.raises(DiscoverySourceError, match="requires at least one location"):
        runner.search("linkedin-search", queries=["planner"], locations=[], recency_days=7, limit=10)


def test_cli_runner_rejects_energy_jobline_without_location(tmp_path):
    runner = CliDiscoveryPortalRunner(tmp_path)
    with pytest.raises(DiscoverySourceError, match="Energy Jobline search requires at least one location"):
        runner.search(
            "energy-jobline-search", queries=["drilling engineer"], locations=[],
            recency_days=7, limit=10,
        )


def test_cli_runner_energy_jobline_fetches_detail_per_result_and_omits_unsupported_flags(tmp_path, monkeypatch):
    cli = tmp_path / ".agents/skills/energy-jobline-search/cli/src/cli.ts"
    cli.parent.mkdir(parents=True)
    cli.write_text("// fixture", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "detail" in argv:
            payload = {
                "id": argv[argv.index("detail") + 1], "title": "Senior Drilling Engineer",
                "company": "Cammach", "url": "https://www.energyjobline.com/job/x-31512381",
                "description": "Lead well planning.", "jsonLdFound": True,
            }
        else:
            payload = {"meta": {"count": 1}, "results": [{"id": "31512381"}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("product.discovery_search.subprocess.run", fake_run)
    runner = CliDiscoveryPortalRunner(tmp_path)

    results = runner.search(
        "energy-jobline-search", queries=["drilling engineer"], locations=["Aberdeen"],
        recency_days=7, limit=1,
    )

    assert results[0]["description"] == "Lead well planning."
    assert len(calls) == 2
    search_argv = calls[0][0]
    detail_argv = calls[1][0]
    assert "--location" in search_argv and "Aberdeen" in search_argv
    # No --jobage or --description-format for this source: its CLI accepts neither.
    assert "--jobage" not in search_argv
    assert "--description-format" not in search_argv
    assert detail_argv[-3:] == ["31512381", "--format", "json"]


def test_cli_runner_energy_jobline_omits_remote_flag_even_when_requested(tmp_path, monkeypatch):
    cli = tmp_path / ".agents/skills/energy-jobline-search/cli/src/cli.ts"
    cli.parent.mkdir(parents=True)
    cli.write_text("// fixture", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "detail" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "id": "1", "title": "Engineer", "company": "Co",
                    "url": "https://www.energyjobline.com/job/x-1",
                }),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout=json.dumps({"results": [{"id": "1"}]}), stderr="")

    monkeypatch.setattr("product.discovery_search.subprocess.run", fake_run)
    runner = CliDiscoveryPortalRunner(tmp_path)

    runner.search(
        "energy-jobline-search", queries=["engineer"], locations=["Aberdeen"],
        recency_days=7, limit=1, remote_mode="remote",
    )

    assert "--remote" not in calls[0]


def test_cli_runner_airswift_search_does_not_require_a_location(tmp_path, monkeypatch):
    # Unlike linkedin-search and energy-jobline-search, airswift-search's
    # /jobs listing returns every job when location is omitted -- an empty
    # locations list must not raise, and no --location flag should be sent.
    cli = tmp_path / ".agents/skills/airswift-search/cli/src/cli.ts"
    cli.parent.mkdir(parents=True)
    cli.write_text("// fixture", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "detail" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "id": "1280556", "title": "FPSO Piping Integration Coordinator",
                    "company": "Airswift",
                    "url": "https://www.airswift.com/jobs/fpso-piping-integration-coordinator-1280556",
                    "description": "Coordinate piping integration.",
                }),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "results": [{"id": "https://www.airswift.com/jobs/fpso-piping-integration-coordinator-1280556"}],
            }),
            stderr="",
        )

    monkeypatch.setattr("product.discovery_search.subprocess.run", fake_run)
    runner = CliDiscoveryPortalRunner(tmp_path)

    results = runner.search(
        "airswift-search", queries=["piping"], locations=[], recency_days=7, limit=1,
    )

    assert results[0]["description"] == "Coordinate piping integration."
    search_argv = calls[0]
    assert "--location" not in search_argv


def test_cli_runner_airswift_search_fetches_detail_per_result_using_the_search_result_url(tmp_path, monkeypatch):
    # airswift-search's detail command requires the full URL, not a bare
    # numeric reference -- Airswift's /jobs/<slug>-<id> route 404s on a
    # wrong/placeholder slug even with the correct numeric ID, so the search
    # result's "id" field must already be the URL for the dispatcher's
    # identity -> detail round-trip to work at all.
    cli = tmp_path / ".agents/skills/airswift-search/cli/src/cli.ts"
    cli.parent.mkdir(parents=True)
    cli.write_text("// fixture", encoding="utf-8")
    calls = []
    detail_url = "https://www.airswift.com/jobs/fpso-piping-integration-coordinator-1280556"

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "detail" in argv:
            payload = {
                "id": "1280556", "title": "FPSO Piping Integration Coordinator",
                "company": "Airswift", "url": argv[argv.index("detail") + 1],
                "description": "Coordinate piping integration.", "jsonLdFound": True,
            }
        else:
            payload = {"results": [{"id": detail_url}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("product.discovery_search.subprocess.run", fake_run)
    runner = CliDiscoveryPortalRunner(tmp_path)

    results = runner.search(
        "airswift-search", queries=["piping"], locations=["Aberdeen"], recency_days=7, limit=1,
    )

    assert len(calls) == 2
    search_argv, detail_argv = calls
    assert "--location" in search_argv and "Aberdeen" in search_argv
    assert "--jobage" not in search_argv
    assert "--description-format" not in search_argv
    # The bare positional argument passed to `detail` is exactly the search
    # result's "id" value -- the full URL, not a derived/re-parsed reference.
    assert detail_argv[detail_argv.index("detail") + 1] == detail_url
    assert results[0]["url"] == detail_url
    assert results[0]["id"] == "1280556"


def test_airswift_url_as_id_bridge_survives_the_full_search_to_persisted_record_chain(tmp_path, monkeypatch):
    # Pins the entire Airswift identity-bridge contract in one test so a
    # later refactor of any single link cannot silently break the chain:
    #
    #   listing card's "id" == the full detail URL (not a bare reference)
    #   -> dispatcher passes that exact string as detail's bare argument
    #      (CliDiscoveryPortalRunner._detail, product/discovery_search.py)
    #   -> the (simulated) CLI's detail command re-derives the numeric
    #      job reference from the URL/page and returns it as "id"
    #   -> portal_result_to_source_record persists that numeric value as
    #      source_record_id, and preserves the exact detail URL as
    #      source_url unchanged.
    #
    # This is the one place that overloads "id" between search (a URL) and
    # detail (a number) is verified end-to-end, not just at each boundary
    # separately -- see product/discovery_search.py's
    # SOURCES_REQUIRING_DETAIL_FETCH and helpers.ts's parseJobCards for why
    # this overload exists at all (Airswift's /jobs/<slug>-<id> route 404s
    # on a wrong/placeholder slug, so a bare ID alone is not resolvable).
    cli = tmp_path / ".agents/skills/airswift-search/cli/src/cli.ts"
    cli.parent.mkdir(parents=True)
    cli.write_text("// fixture", encoding="utf-8")

    detail_url = "https://www.airswift.com/jobs/fpso-piping-integration-coordinator-1280556"
    captured_detail_argv: list[str] = []

    def fake_run(argv, **kwargs):
        if "detail" in argv:
            # Simulates the real CLI's detail.ts: normalizeInput() only
            # accepts a full URL (rejecting a bare numeric id with BAD_ID),
            # then parseJobDetail() re-derives the numeric reference from
            # that URL via referenceFromUrl() and returns it as "id",
            # while "url" is echoed back exactly as received.
            received = argv[argv.index("detail") + 1]
            captured_detail_argv.append(received)
            assert received.startswith("http"), (
                "detail must receive the full URL, never a bare numeric id"
            )
            payload = {
                "id": "1280556",  # re-derived numeric reference, not the URL
                "title": "FPSO Piping Integration Coordinator",
                "company": "Airswift",
                "location": "Qidong, China",
                "url": received,  # echoed back exactly, unchanged
                "description": "Coordinate piping integration.",
                "employmentType": "Contract",
                "validThrough": "2026-11-17",
                "industry": "Oil & Gas - Offshore Oil",
                "jsonLdFound": True,
            }
        else:
            # Simulates search.ts's parseJobCards: the listing card's "id"
            # field is the full detail URL, not the bare numeric reference.
            payload = {"results": [{"id": detail_url}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("product.discovery_search.subprocess.run", fake_run)
    runner = CliDiscoveryPortalRunner(tmp_path)

    results = runner.search(
        "airswift-search", queries=["piping"], locations=["Aberdeen"], recency_days=7, limit=1,
    )

    # The dispatcher received the search result's "id" (the URL) and passed
    # that exact value, unmodified, as detail's bare positional argument.
    assert captured_detail_argv == [detail_url]

    assert len(results) == 1
    detail_result = results[0]
    assert detail_result["id"] == "1280556"
    assert detail_result["url"] == detail_url

    record = portal_result_to_source_record(
        "airswift-search", detail_result, "2026-09-18T12:00:00+00:00"
    )
    assert record["source_record_id"] == "1280556"
    assert record["source_url"] == detail_url
    assert record["source"] == "airswift-search"


def test_cli_runner_airswift_search_omits_remote_flag_even_when_requested(tmp_path, monkeypatch):
    cli = tmp_path / ".agents/skills/airswift-search/cli/src/cli.ts"
    cli.parent.mkdir(parents=True)
    cli.write_text("// fixture", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "detail" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "id": "1", "title": "Engineer", "company": "Airswift",
                    "url": "https://www.airswift.com/jobs/engineer-1",
                }),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"results": [{"id": "https://www.airswift.com/jobs/engineer-1"}]}),
            stderr="",
        )

    monkeypatch.setattr("product.discovery_search.subprocess.run", fake_run)
    runner = CliDiscoveryPortalRunner(tmp_path)

    runner.search(
        "airswift-search", queries=["engineer"], locations=[],
        recency_days=7, limit=1, remote_mode="remote",
    )

    assert "--remote" not in calls[0]


def test_supported_discovery_sources_is_derived_from_source_cli_paths():
    # SUPPORTED_DISCOVERY_SOURCES must never be hand-listed a second time
    # separately from SOURCE_CLI_PATHS -- that duplication is exactly what
    # let the two silently drift before. This pins the derivation.
    assert set(SUPPORTED_DISCOVERY_SOURCES) == set(SOURCE_CLI_PATHS.keys())


def test_available_discovery_source_ids_is_the_intersection():
    available = available_discovery_source_ids(
        ["freehire-search", "energy-jobline-search"]
    )
    assert set(available) == {"freehire-search", "energy-jobline-search"}
    assert "linkedin-search" not in available


def test_available_discovery_source_ids_ignores_all_currently_enabled():
    available = available_discovery_source_ids(list(SOURCE_CLI_PATHS.keys()))
    assert set(available) == set(SOURCE_CLI_PATHS.keys())


def test_available_discovery_source_ids_rejects_unknown_source_even_when_enabled():
    # The core registry invariant: a database row for a source with no
    # matching code-level adapter must never become runnable. Simulates a
    # registry that (incorrectly, or ahead of the adapter landing) marks
    # "rigzone-search" enabled -- it must not appear in the result because
    # SOURCE_CLI_PATHS has no entry for it.
    available = available_discovery_source_ids(
        ["freehire-search", "rigzone-search"]
    )
    assert set(available) == {"freehire-search"}
    assert "rigzone-search" not in available


def test_available_discovery_source_ids_empty_enabled_set_yields_nothing():
    assert available_discovery_source_ids([]) == []


def test_available_discovery_source_ids_accepts_set_or_frozenset_input():
    assert set(available_discovery_source_ids({"linkedin-search"})) == {"linkedin-search"}
    assert set(available_discovery_source_ids(frozenset({"linkedin-search"}))) == {"linkedin-search"}
