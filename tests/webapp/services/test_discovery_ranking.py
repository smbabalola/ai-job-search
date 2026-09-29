from __future__ import annotations

from product.job_fit import profile_snapshot_content_id
from product.job_understanding_providers import DeterministicFakeProvider
from webapp.persistence.artifacts import save_artifact
from webapp.persistence.db import connect, init_db
from webapp.persistence.discovery import ingest_discovery_record, save_discovery_fit, set_discovery_candidate_status
from webapp.persistence.user_profile import save_user_profile
from webapp.persistence.workspaces import PROFILE_WORKSPACE_ID, ensure_profile_workspace
from webapp.services.discovery import (
    discovery_fit_is_stale,
    evaluate_discovery_candidate,
    grouped_discovery_candidates,
)
from tests.webapp.fixtures.acceptance.fixtures import (
    full_fit_proposals,
    provider_candidate,
    rich_profile,
    source_record,
)
from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID
from webapp.persistence.search_workspaces import DEFAULT_SEARCH_WORKSPACE_ID


def _record(number, title):
    return {
        "schema_version": "job-source-record.v0", "source": "test-source",
        "source_record_id": str(number), "source_url": f"https://example.test/{number}",
        "captured_at": "2026-08-21T09:00:00+00:00", "company": "Example", "title": title,
        "description": "A role.", "requirements": [], "responsibilities": [],
        "language_requirements": [], "eligibility_requirements": [], "logistics_requirements": [],
    }


def _fit(conn, candidate, *, score=None, blocked=False):
    return save_discovery_fit(
        conn, candidate_id=candidate["id"], occurrence_id=candidate["canonical_occurrence_id"],
        request={"schema_version": "job-fit-request.v1"},
        result={"overall_score": score, "blocked": blocked, "status": "READY" if score is not None else "NEEDS_REVIEW"},
        fingerprints={},
     search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID)


def test_groups_authoritative_ticket7_result_without_fallback_score(tmp_path, monkeypatch):
    path = tmp_path / "rank.db"; init_db(path); conn = connect(path)
    high = ingest_discovery_record(conn, _record(1, "High"), account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID)["candidate"]
    low = ingest_discovery_record(conn, _record(2, "Low"), account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID)["candidate"]
    unresolved = ingest_discovery_record(conn, _record(3, "Unresolved"), account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID)["candidate"]
    blocked = ingest_discovery_record(conn, _record(4, "Blocked"), account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID)["candidate"]
    expired = ingest_discovery_record(conn, _record(5, "Expired"), account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID)["candidate"]
    _fit(conn, high, score=91.0); _fit(conn, low, score=68.0)
    _fit(conn, unresolved); _fit(conn, blocked, blocked=True)
    set_discovery_candidate_status(conn, expired["id"], "expired", account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID)
    monkeypatch.setattr("webapp.services.discovery.discovery_fit_is_stale", lambda *args, **kwargs: False)

    groups = grouped_discovery_candidates(conn, account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID)

    assert [item["title"] for item in groups["scored"]] == ["High", "Low"]
    assert groups["unresolved"][0]["fit"]["result"]["overall_score"] is None
    assert [item["title"] for item in groups["blocked"]] == ["Blocked"]
    assert [item["title"] for item in groups["expired_unavailable"]] == ["Expired"]
    assert all("ranking_score" not in item for values in groups.values() for item in values)


class _DynamicSemanticAdapter:
    def propose(self, **kwargs):
        return full_fit_proposals(kwargs["resolved_job_evidence"])


def test_batch_fit_reuses_ticket7_and_tracks_exact_staleness_without_user_preferences(tmp_path):
    path = tmp_path / "evaluate.db"; init_db(path); conn = connect(path)
    ensure_profile_workspace(conn, account_id=DEFAULT_ACCOUNT_ID)
    profile = rich_profile()
    save_artifact(
        conn, workspace_id=PROFILE_WORKSPACE_ID, artifact_type="profile_snapshot",
        payload=profile, content_id=profile_snapshot_content_id(profile),
    )
    save_user_profile(conn, {"target_roles": ["Unrelated preference"]}, account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID)
    candidate = ingest_discovery_record(conn, source_record(), account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID)["candidate"]

    fit = evaluate_discovery_candidate(
        conn, candidate["id"], _DynamicSemanticAdapter(), request_id="discovery-fit-1",
        understanding_provider=DeterministicFakeProvider(provider_candidate()),
     account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID)

    # Ticket 7 leaves required dimensions unresolved here; discovery must
    # preserve that outcome instead of manufacturing a ranking number.
    assert fit["result"]["status"] == "NEEDS_REVIEW"
    assert fit["result"]["overall_score"] is None
    assert fit["result"]["verdict"] is None
    assert "user_profile" not in fit["fingerprints"]
    assert discovery_fit_is_stale(conn, candidate["id"], account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID) is False
    save_user_profile(conn, {"target_roles": ["A changed preference"]}, account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID)
    assert discovery_fit_is_stale(conn, candidate["id"], account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID) is False

    changed = source_record()
    changed["captured_at"] = "2026-08-22T09:00:00+00:00"
    changed["description"] += " Additional exact source text makes this a new snapshot."
    ingest_discovery_record(conn, changed, account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID)
    assert discovery_fit_is_stale(conn, candidate["id"], account_id=DEFAULT_ACCOUNT_ID, search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID) is True
