from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


SOURCE_CLI_PATHS = {
    "freehire-search": Path(".agents/skills/freehire-search/cli/src/cli.ts"),
    "linkedin-search": Path(".agents/skills/linkedin-search/cli/src/cli.ts"),
    "energy-jobline-search": Path(".agents/skills/energy-jobline-search/cli/src/cli.ts"),
    "airswift-search": Path(".agents/skills/airswift-search/cli/src/cli.ts"),
}
# Every source this process can actually invoke -- derived from
# SOURCE_CLI_PATHS rather than listed by hand a second time, so the two can
# never drift. This is the code-implemented side of availability; whether a
# source is *offered* to users is the separate, backend-registry-controlled
# concern handled by available_discovery_source_ids below.
SUPPORTED_DISCOVERY_SOURCES = tuple(SOURCE_CLI_PATHS.keys())
# Sources whose search listing carries only a preview (no full description) —
# each accepted search hit is followed by a `detail` call to fetch the full
# posting before it reaches discovery persistence. Freehire's search endpoint
# already hydrates the full description inline, so it is not in this set.
SOURCES_REQUIRING_DETAIL_FETCH = frozenset(
    {"linkedin-search", "energy-jobline-search", "airswift-search"}
)


def available_discovery_source_ids(enabled_source_ids: list[str] | frozenset[str] | set[str]) -> list[str]:
    """Runtime-available discovery sources: the intersection of what is
    actually implemented in this process (SOURCE_CLI_PATHS.keys()) and
    what the backend registry marks enabled. A registry row for a source
    with no matching code-level adapter (e.g. someone inserts
    "rigzone-search" before that adapter exists) is deliberately inert here
    — this function is the one place that intersection is enforced, so no
    caller can make an unimplemented source runnable by database edit
    alone. Order follows SOURCE_CLI_PATHS's own (insertion) order, not the
    caller's enabled-list order, for deterministic output regardless of how
    the registry query happened to sort.
    """
    enabled = set(enabled_source_ids)
    return [source_id for source_id in SOURCE_CLI_PATHS if source_id in enabled]


class DiscoverySourceError(RuntimeError):
    pass


class DiscoveryPortalRunner(Protocol):
    def search(
        self,
        source: str,
        *,
        queries: list[str],
        locations: list[str],
        recency_days: int,
        limit: int,
        remote_mode: str | None = None,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class CliDiscoveryPortalRunner:
    root: Path
    timeout_seconds: int = 45

    def search(
        self,
        source: str,
        *,
        queries: list[str],
        locations: list[str],
        recency_days: int,
        limit: int,
        remote_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        if source not in SUPPORTED_DISCOVERY_SOURCES:
            raise DiscoverySourceError(f"unsupported discovery source {source!r}")
        if source == "linkedin-search" and not locations:
            raise DiscoverySourceError("LinkedIn search requires at least one location")
        if source == "energy-jobline-search" and not locations:
            raise DiscoverySourceError("Energy Jobline search requires at least one location")
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        query_values = queries or [""]
        # airswift-search accepts --location like linkedin-search and
        # energy-jobline-search, but (unlike those two) does not require it --
        # its /jobs listing returns every job when location is omitted, so an
        # empty locations list still yields one location_values entry ("")
        # rather than looping per requested location.
        location_values = locations if source in ("linkedin-search", "energy-jobline-search") else (
            locations if source == "airswift-search" and locations else [""]
        )
        for query in query_values:
            for location in location_values:
                remaining = limit - len(results)
                if remaining <= 0:
                    return results
                payload = self._search_once(
                    source,
                    query=query,
                    location=location,
                    recency_days=recency_days,
                    limit=remaining,
                    remote_mode=remote_mode,
                )
                for item in payload:
                    identity = str(item.get("id", ""))
                    if not identity or identity in seen:
                        continue
                    seen.add(identity)
                    if source in SOURCES_REQUIRING_DETAIL_FETCH:
                        item = self._detail(source, identity)
                    results.append(item)
                    if len(results) >= limit:
                        return results
        return results

    def _search_once(
        self, source: str, *, query: str, location: str, recency_days: int, limit: int,
        remote_mode: str | None,
    ) -> list[dict[str, Any]]:
        args = ["search", "--format", "json", "--limit", str(limit)]
        if query:
            args.extend(["--query", query])
        if remote_mode is not None:
            if remote_mode not in {"remote", "hybrid", "onsite"}:
                raise DiscoverySourceError("remote mode must be remote, hybrid, or onsite")
            # Only freehire-search and linkedin-search expose a --remote/work-mode
            # filter; energy-jobline-search's and airswift-search's CLIs have no
            # such flag (their listing pages carry no work-mode facet), so a
            # caller-requested remote_mode is silently inapplicable there rather
            # than passed to an unknown flag.
            if source not in ("energy-jobline-search", "airswift-search"):
                args.extend(["--remote", remote_mode])
        if source == "linkedin-search":
            args.extend(["--location", location])
            # LinkedIn exposes a controlled recency set; round outward so the
            # source never silently excludes jobs within the requested window.
            allowed = next((days for days in (1, 7, 14, 30) if days >= recency_days), None)
            if allowed is not None:
                args.extend(["--jobage", str(allowed)])
        elif source == "energy-jobline-search":
            args.extend(["--location", location])
            # No recency filter exists in energy-jobline-search's CLI (the
            # listing page carries no posted-date facet) — recency_days is
            # inapplicable here, matching the remote_mode omission above.
        elif source == "airswift-search":
            # Both --query and --location are optional for airswift-search;
            # only pass --location when one was actually supplied (an empty
            # string would be a no-op for the CLI's own optional-flag parsing,
            # but omitting it entirely keeps the invoked args self-documenting).
            # No recency filter exists in its CLI either, matching
            # energy-jobline-search's omission above.
            if location:
                args.extend(["--location", location])
        else:
            args.extend(["--jobage", str(recency_days), "--description-format", "markdown"])
        payload = self._run(source, args)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise DiscoverySourceError(f"{source} returned an invalid search envelope")
        return results

    def _detail(self, source: str, record_id: str) -> dict[str, Any]:
        payload = self._run(source, ["detail", record_id, "--format", "json"])
        if not isinstance(payload, dict):
            raise DiscoverySourceError(f"{source} returned an invalid detail record")
        return payload

    def _run(self, source: str, args: list[str]) -> Any:
        cli = self.root / SOURCE_CLI_PATHS[source]
        if not cli.is_file():
            raise DiscoverySourceError(f"installed CLI for {source} was not found")
        try:
            completed = subprocess.run(
                ["bun", "run", str(cli), *args],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DiscoverySourceError(f"{source} could not run: {exc}") from exc
        if completed.returncode != 0:
            message = completed.stderr.strip() or "portal CLI failed"
            raise DiscoverySourceError(f"{source} failed: {message[:500]}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DiscoverySourceError(f"{source} returned invalid JSON") from exc
