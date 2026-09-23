from __future__ import annotations

import copy
from typing import Any

from product.job_ingestion import (
    JOB_SOURCE_RECORD_VERSION,
    JobIngestionValidationError,
    freehire_detail_to_source_record,
    validate_job_source_record,
)


LINKEDIN_ADAPTER_VERSION = "linkedin-detail.v0"
LINKEDIN_DESCRIPTION_PROVENANCE = (
    "LinkedIn public job-detail text preserved as emitted by the installed portal CLI."
)

ENERGY_JOBLINE_ADAPTER_VERSION = "energy-jobline-detail.v0"
ENERGY_JOBLINE_DESCRIPTION_PROVENANCE = (
    "Energy Jobline public job-detail text (from the page's schema.org/JobPosting "
    "JSON-LD when present, else absent) preserved as emitted by the installed portal CLI."
)

AIRSWIFT_ADAPTER_VERSION = "airswift-detail.v0"
AIRSWIFT_DESCRIPTION_PROVENANCE = (
    "Airswift public job-detail text (from the page's schema.org/JobPosting "
    "JSON-LD when present, else absent) preserved as emitted by the installed portal CLI."
)


def portal_result_to_source_record(
    source: str, result: dict[str, Any], captured_at: str
) -> dict[str, Any]:
    if source == "freehire-search":
        return freehire_detail_to_source_record(result, captured_at)
    if source == "linkedin-search":
        return linkedin_detail_to_source_record(result, captured_at)
    if source == "energy-jobline-search":
        return energy_jobline_detail_to_source_record(result, captured_at)
    if source == "airswift-search":
        return airswift_detail_to_source_record(result, captured_at)
    raise JobIngestionValidationError(f"$.source: unsupported discovery source {source!r}")


def linkedin_detail_to_source_record(
    detail: dict[str, Any], captured_at: str
) -> dict[str, Any]:
    if not isinstance(detail, dict):
        raise JobIngestionValidationError("$.linkedin_detail: must be an object")
    required = ("id", "title", "company", "url")
    for field in required:
        if not isinstance(detail.get(field), str) or not detail[field].strip():
            raise JobIngestionValidationError(
                f"$.linkedin_detail.{field}: must be a non-empty string"
            )
    if not isinstance(captured_at, str) or not captured_at.strip():
        raise JobIngestionValidationError("$.captured_at: must be a non-empty string")
    record: dict[str, Any] = {
        "schema_version": JOB_SOURCE_RECORD_VERSION,
        "source": "linkedin-search",
        "source_record_id": detail["id"],
        "source_url": detail["url"],
        "captured_at": captured_at,
        "company": detail["company"],
        "title": detail["title"],
        "requirements": [],
        "responsibilities": [],
        "language_requirements": [],
        "eligibility_requirements": [],
        "logistics_requirements": [],
        "metadata": {
            "adapter": LINKEDIN_ADAPTER_VERSION,
            "description_provenance": LINKEDIN_DESCRIPTION_PROVENANCE,
            "linkedin": {
                key: copy.deepcopy(detail[key])
                for key in ("companyUrl", "date", "seniority", "jobFunction", "industries", "applyUrl")
                if detail.get(key) is not None
            },
        },
    }
    for incoming, outgoing in (("location", "location"), ("employmentType", "employment_type"), ("description", "description")):
        value = detail.get(incoming)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise JobIngestionValidationError(
                    f"$.linkedin_detail.{incoming}: must be a non-empty string or null"
                )
            record[outgoing] = value
    validate_job_source_record(record)
    return record


def energy_jobline_detail_to_source_record(
    detail: dict[str, Any], captured_at: str
) -> dict[str, Any]:
    if not isinstance(detail, dict):
        raise JobIngestionValidationError("$.energy_jobline_detail: must be an object")
    required = ("id", "title", "company", "url")
    for field in required:
        if not isinstance(detail.get(field), str) or not detail[field].strip():
            raise JobIngestionValidationError(
                f"$.energy_jobline_detail.{field}: must be a non-empty string"
            )
    if not isinstance(captured_at, str) or not captured_at.strip():
        raise JobIngestionValidationError("$.captured_at: must be a non-empty string")
    record: dict[str, Any] = {
        "schema_version": JOB_SOURCE_RECORD_VERSION,
        "source": "energy-jobline-search",
        "source_record_id": detail["id"],
        # Preserved exactly as returned by the installed portal CLI — never
        # rewritten, canonicalized, or substituted (design requirement).
        "source_url": detail["url"],
        "captured_at": captured_at,
        "company": detail["company"],
        "title": detail["title"],
        "requirements": [],
        "responsibilities": [],
        "language_requirements": [],
        "eligibility_requirements": [],
        "logistics_requirements": [],
        "metadata": {
            "adapter": ENERGY_JOBLINE_ADAPTER_VERSION,
            "description_provenance": ENERGY_JOBLINE_DESCRIPTION_PROVENANCE,
            "energy_jobline": {
                key: copy.deepcopy(detail[key])
                for key in (
                    "date", "terms", "validThrough",
                    "addressLocality", "addressRegion", "addressCountry", "jsonLdFound",
                )
                if detail.get(key) is not None
            },
        },
    }
    for incoming, outgoing in (
        ("location", "location"),
        ("employmentType", "employment_type"),
        ("description", "description"),
    ):
        value = detail.get(incoming)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise JobIngestionValidationError(
                    f"$.energy_jobline_detail.{incoming}: must be a non-empty string or null"
                )
            record[outgoing] = value
    validate_job_source_record(record)
    return record


def airswift_detail_to_source_record(
    detail: dict[str, Any], captured_at: str
) -> dict[str, Any]:
    if not isinstance(detail, dict):
        raise JobIngestionValidationError("$.airswift_detail: must be an object")
    required = ("id", "title", "company", "url")
    for field in required:
        if not isinstance(detail.get(field), str) or not detail[field].strip():
            raise JobIngestionValidationError(
                f"$.airswift_detail.{field}: must be a non-empty string"
            )
    if not isinstance(captured_at, str) or not captured_at.strip():
        raise JobIngestionValidationError("$.captured_at: must be a non-empty string")
    record: dict[str, Any] = {
        "schema_version": JOB_SOURCE_RECORD_VERSION,
        "source": "airswift-search",
        # detail["id"] is the numeric Airswift job reference re-derived by
        # the CLI from the detail URL, not the bare value originally
        # threaded through by the discovery dispatcher (which is the URL
        # itself, since Airswift's /jobs/<slug>-<id> route requires the
        # exact slug and cannot be resolved from a bare ID alone).
        "source_record_id": detail["id"],
        # Preserved exactly as returned by the installed portal CLI — never
        # rewritten, canonicalized, or substituted (design requirement).
        "source_url": detail["url"],
        "captured_at": captured_at,
        "company": detail["company"],
        "title": detail["title"],
        "requirements": [],
        "responsibilities": [],
        "language_requirements": [],
        "eligibility_requirements": [],
        "logistics_requirements": [],
        "metadata": {
            "adapter": AIRSWIFT_ADAPTER_VERSION,
            "description_provenance": AIRSWIFT_DESCRIPTION_PROVENANCE,
            "airswift": {
                key: copy.deepcopy(detail[key])
                for key in (
                    "date", "validThrough", "industry",
                    "addressLocality", "addressRegion", "addressCountry", "jsonLdFound",
                )
                if detail.get(key) is not None
            },
        },
    }
    for incoming, outgoing in (
        ("location", "location"),
        ("employmentType", "employment_type"),
        ("description", "description"),
    ):
        value = detail.get(incoming)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise JobIngestionValidationError(
                    f"$.airswift_detail.{incoming}: must be a non-empty string or null"
                )
            record[outgoing] = value
    validate_job_source_record(record)
    return record
