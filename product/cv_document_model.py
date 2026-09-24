"""Pure CV Generation Quality v2 document planning.

Task 3 consumes the factual statement IR from Task 2 and allocates statements
into a renderer-independent CV document model.  It decides structure only:
sections, role placement, ordering, budgets, and explicit omissions.  It does
not rewrite statement text, render documents, or call providers.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from typing import Any


CV_DOCUMENT_MODEL_VERSION = "cv-document-model.v2"

DEFAULT_DOCUMENT_BUDGETS: dict[str, int] = {
    "max_summary_statements": 3,
    "max_skill_statements": 6,
    "max_roles": 6,
    "max_statements_per_role": 4,
    "max_qualification_statements": 4,
}

SECTION_ORDER = (
    "professional_summary",
    "skills",
    "professional_experience",
    "qualifications",
    "evidence_gaps",
)

SUPPORTED_NON_ROLE_SECTIONS = {
    "professional_summary",
    "skills",
    "qualifications",
}


def build_cv_document_model(
    statement_plan: dict[str, Any],
    *,
    budgets: dict[str, int] | None = None,
    strategy: str = "evidence_first",
) -> dict[str, Any]:
    """Allocate Task 2 statements into a deterministic CV document model."""

    resolved_budgets = _resolve_budgets(budgets)
    statements = [
        copy.deepcopy(statement)
        for statement in statement_plan.get("statements", [])
        if isinstance(statement, dict) and statement.get("statement_id")
    ]
    statements.sort(key=_statement_sort_key)

    selected_statement_ids: set[str] = set()
    suppressed: list[dict[str, Any]] = []

    summary_items = _allocate_flat_section(
        statements=statements,
        target_section="professional_summary",
        budget=resolved_budgets["max_summary_statements"],
        selected_statement_ids=selected_statement_ids,
        suppressed=suppressed,
    )
    skill_items = _allocate_flat_section(
        statements=statements,
        target_section="skills",
        budget=resolved_budgets["max_skill_statements"],
        selected_statement_ids=selected_statement_ids,
        suppressed=suppressed,
    )
    role_items = _allocate_roles(
        statements=statements,
        budgets=resolved_budgets,
        selected_statement_ids=selected_statement_ids,
        suppressed=suppressed,
    )
    qualification_items = _allocate_flat_section(
        statements=statements,
        target_section="qualifications",
        budget=resolved_budgets["max_qualification_statements"],
        selected_statement_ids=selected_statement_ids,
        suppressed=suppressed,
    )

    for statement in statements:
        statement_id = statement["statement_id"]
        if statement_id in selected_statement_ids or any(item["statement_id"] == statement_id for item in suppressed):
            continue
        reason = "UNPLACEABLE"
        if statement.get("target_section") not in SUPPORTED_NON_ROLE_SECTIONS | {"professional_experience"}:
            reason = "UNPLACEABLE_UNSUPPORTED_SECTION"
        suppressed.append(_suppressed(statement, reason, "unallocated_statement"))

    sections = []
    if summary_items:
        sections.append(_section("professional_summary", "Professional Summary", summary_items, resolved_budgets["max_summary_statements"]))
    if skill_items:
        sections.append(_section("skills", "Core Competencies", skill_items, resolved_budgets["max_skill_statements"]))
    if role_items:
        sections.append({
            "section_id": "professional_experience",
            "title": "Professional Experience",
            "order": SECTION_ORDER.index("professional_experience"),
            "budget": {
                "max_roles": resolved_budgets["max_roles"],
                "max_statements_per_role": resolved_budgets["max_statements_per_role"],
            },
            "roles": role_items,
        })
    if qualification_items:
        sections.append(_section("qualifications", "Qualifications", qualification_items, resolved_budgets["max_qualification_statements"]))

    gaps = [
        _gap_record(item)
        for item in statement_plan.get("uncovered_requirements", [])
        if isinstance(item, dict)
    ]
    if gaps:
        sections.append({
            "section_id": "evidence_gaps",
            "title": "Evidence Gaps",
            "order": SECTION_ORDER.index("evidence_gaps"),
            "items": gaps,
        })

    sections.sort(key=lambda section: section["order"])
    model = {
        "schema_version": CV_DOCUMENT_MODEL_VERSION,
        "source_statement_plan_version": statement_plan.get("schema_version"),
        "strategy": strategy,
        "budgets": resolved_budgets,
        "sections": sections,
        "suppressed_statements": sorted(suppressed, key=lambda item: (item["status"], item["statement_id"])),
        "source_omitted_evidence": copy.deepcopy(statement_plan.get("omitted_evidence", [])),
        "source_ungroupable_evidence": copy.deepcopy(statement_plan.get("ungroupable_evidence", [])),
        "provenance": {
            "source_statement_ids": [statement["statement_id"] for statement in statements],
            "selected_statement_ids": sorted(selected_statement_ids),
            "suppressed_statement_ids": sorted(item["statement_id"] for item in suppressed),
            "source_profile_evidence_ids": list(
                statement_plan.get("provenance", {}).get("statement_profile_evidence_ids", [])
            ),
            "source_job_requirement_ids": list(
                statement_plan.get("provenance", {}).get("source_selected_job_requirement_ids", [])
            ),
        },
        "decision_metadata": {
            "section_order": list(SECTION_ORDER),
            "ordering_rule": "target_section_then_record_then_statement_id",
            "omission_policy": "selected_or_explicitly_suppressed",
        },
    }
    model["document_id"] = _stable_id("cvdoc", {
        "schema_version": model["schema_version"],
        "strategy": strategy,
        "budgets": resolved_budgets,
        "sections": sections,
        "suppressed_statement_ids": model["provenance"]["suppressed_statement_ids"],
    })
    return model


def _resolve_budgets(overrides: dict[str, int] | None) -> dict[str, int]:
    budgets = copy.deepcopy(DEFAULT_DOCUMENT_BUDGETS)
    if overrides:
        for key, value in overrides.items():
            if key not in budgets:
                raise ValueError(f"unknown CV document budget {key!r}")
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"CV document budget {key!r} must be a non-negative integer")
            budgets[key] = value
    return budgets


def _allocate_flat_section(
    *,
    statements: list[dict[str, Any]],
    target_section: str,
    budget: int,
    selected_statement_ids: set[str],
    suppressed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates = [
        statement for statement in statements
        if statement.get("target_section") == target_section
    ]
    selected = []
    for statement in candidates:
        if len(selected) >= budget:
            suppressed.append(_suppressed(statement, "OMITTED_BUDGET", target_section))
            continue
        selected.append(_document_item(statement, target_section))
        selected_statement_ids.add(statement["statement_id"])
    return selected


def _allocate_roles(
    *,
    statements: list[dict[str, Any]],
    budgets: dict[str, int],
    selected_statement_ids: set[str],
    suppressed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_record: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for statement in statements:
        if statement.get("target_section") != "professional_experience":
            continue
        record_id = statement.get("target_record_id")
        if not record_id:
            suppressed.append(_suppressed(statement, "UNPLACEABLE_MISSING_RECORD", "professional_experience"))
            continue
        by_record[record_id].append(statement)

    roles = []
    for record_id in sorted(by_record):
        role_statements = sorted(by_record[record_id], key=_statement_sort_key)
        if len(roles) >= budgets["max_roles"]:
            for statement in role_statements:
                suppressed.append(_suppressed(statement, "OMITTED_BUDGET", "professional_experience"))
            continue
        selected_items = []
        omitted_count = 0
        for statement in role_statements:
            if len(selected_items) >= budgets["max_statements_per_role"]:
                suppressed.append(_suppressed(statement, "OMITTED_BUDGET", "professional_experience"))
                omitted_count += 1
                continue
            selected_items.append(_document_item(statement, "professional_experience"))
            selected_statement_ids.add(statement["statement_id"])
        roles.append({
            "role_id": record_id,
            "record_id": record_id,
            "order": len(roles),
            "budget": {
                "max_statements": budgets["max_statements_per_role"],
                "candidate_statement_count": len(role_statements),
                "selected_statement_count": len(selected_items),
                "omitted_statement_count": omitted_count,
            },
            "statements": selected_items,
        })
    return roles


def _document_item(statement: dict[str, Any], section_id: str) -> dict[str, Any]:
    return {
        "status": "SELECTED",
        "section_id": section_id,
        "statement_id": statement["statement_id"],
        "target_record_id": statement.get("target_record_id"),
        "statement_text": statement.get("statement_text"),
        "source_profile_evidence_ids": list(statement.get("source_profile_evidence_ids", [])),
        "source_job_requirement_ids": list(statement.get("source_job_requirement_ids", [])),
        "provenance": copy.deepcopy(statement.get("provenance", {})),
        "structural_decision": {
            "reason": "matches_statement_target_section",
            "source_target_section": statement.get("target_section"),
        },
    }


def _section(section_id: str, title: str, items: list[dict[str, Any]], budget: int) -> dict[str, Any]:
    return {
        "section_id": section_id,
        "title": title,
        "order": SECTION_ORDER.index(section_id),
        "budget": {
            "max_statements": budget,
            "candidate_statement_count": len(items),
            "selected_statement_count": len(items),
        },
        "items": items,
    }


def _suppressed(statement: dict[str, Any], reason: str, source: str) -> dict[str, Any]:
    return {
        "status": reason,
        "statement_id": statement["statement_id"],
        "source": source,
        "target_section": statement.get("target_section"),
        "target_record_id": statement.get("target_record_id"),
        "source_profile_evidence_ids": list(statement.get("source_profile_evidence_ids", [])),
        "source_job_requirement_ids": list(statement.get("source_job_requirement_ids", [])),
    }


def _gap_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "GAP",
        "job_requirement_id": item.get("job_requirement_id"),
        "text": item.get("text"),
        "kind": item.get("kind"),
        "source": item.get("source"),
        "gap": copy.deepcopy(item.get("gap")),
    }


def _statement_sort_key(statement: dict[str, Any]) -> tuple[Any, ...]:
    return (
        SECTION_ORDER.index(statement.get("target_section"))
        if statement.get("target_section") in SECTION_ORDER
        else len(SECTION_ORDER),
        statement.get("target_record_id") or "",
        statement["statement_id"],
    )


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
