from __future__ import annotations

import pytest

from product.discovery_sources import portal_result_to_source_record
from product.job_ingestion import JobIngestionValidationError, normalize_job_source_record


def test_freehire_search_result_adapts_to_source_record_not_workspace_snapshot():
    result = {
        "id": "planner-abc",
        "title": "Project Planner",
        "company": "Example Energy",
        "company_slug": "example-energy",
        "location": "Aberdeen",
        "date": "2026-08-20",
        "url": "https://freehire.me/jobs/planner-abc",
        "work_mode": "hybrid",
        "regions": ["eu"],
        "countries": ["GB"],
        "skills": ["planning"],
        "description": "Coordinate engineering schedules.",
    }

    record = portal_result_to_source_record(
        "freehire-search", result, "2026-08-21T09:00:00+00:00"
    )

    assert record["schema_version"] == "job-source-record.v0"
    assert record["description"] == "Coordinate engineering schedules."
    assert record["metadata"]["freehire"]["work_mode"] == "hybrid"
    assert normalize_job_source_record(record)["company"] == "Example Energy"


def test_linkedin_detail_adapts_exact_posting_text_and_metadata():
    detail = {
        "id": "4426311357",
        "title": "Project Coordinator",
        "company": "Example Energy",
        "companyUrl": "https://linkedin.com/company/example",
        "location": "London, UK",
        "date": "2026-08-20",
        "url": "https://linkedin.com/jobs/view/project-coordinator-4426311357",
        "description": "Coordinate projects & report progress.",
        "seniority": "Mid-Senior level",
        "employmentType": "Full-time",
        "jobFunction": "Project Management",
        "industries": "Energy",
        "applyUrl": "https://example.test/apply",
    }

    record = portal_result_to_source_record(
        "linkedin-search", detail, "2026-08-21T09:00:00+00:00"
    )

    assert record["description"] == detail["description"]
    assert record["employment_type"] == "Full-time"
    assert record["metadata"]["linkedin"]["applyUrl"] == detail["applyUrl"]


def test_unknown_source_and_incomplete_linkedin_results_are_rejected():
    with pytest.raises(JobIngestionValidationError, match="unsupported discovery source"):
        portal_result_to_source_record("unknown", {}, "2026-08-21T09:00:00+00:00")
    with pytest.raises(JobIngestionValidationError, match="company"):
        portal_result_to_source_record(
            "linkedin-search",
            {"id": "1", "title": "Planner", "company": None, "url": "https://example.test/1"},
            "2026-08-21T09:00:00+00:00",
        )


def test_energy_jobline_detail_adapts_exact_posting_text_and_preserves_source_url():
    detail = {
        "id": "31512381",
        "title": "Senior Drilling Engineer in Aberdeen",
        "company": "Cammach Recruitment",
        "location": "Aberdeen, UK",
        "date": "2026-09-03",
        "url": "https://www.energyjobline.com/job/senior-drilling-engineer-in-aberdeen-31512381",
        "terms": ["engineer", "Drilling"],
        "description": "Leading Oil & Gas Operator seeking a Senior Drilling Engineer.",
        "employmentType": "FULL_TIME",
        "validThrough": "2026-10-02",
        "addressLocality": "Aberdeen",
        "addressRegion": "Scotland",
        "addressCountry": "GB",
        "jsonLdFound": True,
    }

    record = portal_result_to_source_record(
        "energy-jobline-search", detail, "2026-09-17T09:00:00+00:00"
    )

    assert record["schema_version"] == "job-source-record.v0"
    assert record["source"] == "energy-jobline-search"
    assert record["source_record_id"] == "31512381"
    # source_url preserved exactly as the provider returned it — no rewriting.
    assert record["source_url"] == detail["url"]
    assert record["company"] == "Cammach Recruitment"
    assert record["title"] == detail["title"]
    assert record["location"] == "Aberdeen, UK"
    assert record["employment_type"] == "FULL_TIME"
    assert record["description"] == detail["description"]
    assert record["metadata"]["adapter"] == "energy-jobline-detail.v0"
    assert record["metadata"]["energy_jobline"]["validThrough"] == "2026-10-02"
    assert record["metadata"]["energy_jobline"]["addressLocality"] == "Aberdeen"
    assert record["metadata"]["energy_jobline"]["jsonLdFound"] is True
    assert normalize_job_source_record(record)["company"] == "Cammach Recruitment"


def test_energy_jobline_detail_without_jsonld_still_produces_a_valid_thinner_record():
    # jsonLdFound: False mirrors the CLI's documented fallback behavior when
    # a detail page has no schema.org/JobPosting block — required fields
    # (id/title/company/url) still come from the listing card.
    detail = {
        "id": "999",
        "title": "Well Engineer",
        "company": "North Sea Recruiters",
        "url": "https://www.energyjobline.com/job/well-engineer-999",
        "jsonLdFound": False,
    }

    record = portal_result_to_source_record(
        "energy-jobline-search", detail, "2026-09-17T09:00:00+00:00"
    )

    assert record["source_url"] == detail["url"]
    assert "description" not in record
    assert "employment_type" not in record
    assert record["metadata"]["energy_jobline"]["jsonLdFound"] is False


def test_energy_jobline_missing_required_field_is_rejected():
    with pytest.raises(JobIngestionValidationError, match="company"):
        portal_result_to_source_record(
            "energy-jobline-search",
            {"id": "1", "title": "Engineer", "company": None, "url": "https://example.test/1"},
            "2026-09-17T09:00:00+00:00",
        )


def test_airswift_detail_adapts_to_source_record():
    detail = {
        # The CLI re-derives this numeric reference from the detail URL --
        # it is never the URL itself, unlike the search-result "id" that the
        # discovery dispatcher threads through as the detail command's
        # argument (see product/discovery_search.py's SOURCES_REQUIRING_DETAIL_FETCH).
        "id": "1280556",
        "title": "FPSO Piping Integration Coordinator",
        "company": "Airswift",
        "location": "Qidong, China",
        "date": "2026-09-18",
        "employmentType": "Contract",
        "summary": None,
        "url": "https://www.airswift.com/jobs/fpso-piping-integration-coordinator-1280556",
        "description": "We are seeking an experienced Piping Integration Coordinator.",
        "validThrough": "2026-11-17",
        "industry": "Oil & Gas - Offshore Oil",
        "addressLocality": "Qidong",
        "addressRegion": "",
        "addressCountry": "China",
        "jsonLdFound": True,
    }

    record = portal_result_to_source_record(
        "airswift-search", detail, "2026-09-18T10:00:00+00:00"
    )

    assert record["schema_version"] == "job-source-record.v0"
    assert record["source"] == "airswift-search"
    assert record["source_record_id"] == "1280556"
    # source_url preserved exactly as the provider returned it — no rewriting.
    assert record["source_url"] == detail["url"]
    assert record["company"] == "Airswift"
    assert record["title"] == detail["title"]
    assert record["location"] == "Qidong, China"
    assert record["employment_type"] == "Contract"
    assert record["description"] == detail["description"]
    assert record["metadata"]["adapter"] == "airswift-detail.v0"
    assert record["metadata"]["airswift"]["validThrough"] == "2026-11-17"
    assert record["metadata"]["airswift"]["industry"] == "Oil & Gas - Offshore Oil"
    assert record["metadata"]["airswift"]["addressLocality"] == "Qidong"
    assert record["metadata"]["airswift"]["jsonLdFound"] is True
    assert normalize_job_source_record(record)["company"] == "Airswift"


def test_airswift_detail_without_jsonld_still_produces_a_valid_thinner_record():
    # jsonLdFound: False mirrors the CLI's documented fallback behavior when
    # a detail page has no schema.org/JobPosting block — required fields
    # (id/title/company/url) still come from the listing card.
    detail = {
        "id": "999999",
        "title": "Well Engineer",
        "company": "Airswift",
        "url": "https://www.airswift.com/jobs/well-engineer-999999",
        "jsonLdFound": False,
    }

    record = portal_result_to_source_record(
        "airswift-search", detail, "2026-09-18T10:00:00+00:00"
    )

    assert record["source_url"] == detail["url"]
    assert "description" not in record
    assert "employment_type" not in record
    assert record["metadata"]["airswift"]["jsonLdFound"] is False


def test_airswift_missing_required_field_is_rejected():
    with pytest.raises(JobIngestionValidationError, match="company"):
        portal_result_to_source_record(
            "airswift-search",
            {"id": "1", "title": "Engineer", "company": None, "url": "https://example.test/1"},
            "2026-09-18T10:00:00+00:00",
        )
