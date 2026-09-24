"""Pure CV Generation Quality v2 statement composition.

Task 2 consumes the deterministic Task 1 CV content plan and emits a factual
statement intermediate representation.  It intentionally does not write a CV:
there is no persuasive rewriting, no model call, no rendering, and no attempt
to infer relationships that are absent from planned evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any


CV_STATEMENT_PLAN_VERSION = "cv-statement-plan.v0"

SUBSTANTIVE_MATCH_TYPES = {"direct", "functionally_equivalent", "transferable"}
SAFE_ROLE_COMPOSITION_FIELDS = {"responsibility_or_achievement"}


def build_cv_statement_plan(content_plan: dict[str, Any]) -> dict[str, Any]:
    """Convert a Task 1 content plan into grounded factual statements."""

    candidate_by_id = {
        candidate["profile_evidence_id"]: copy.deepcopy(candidate)
        for candidate in content_plan.get("candidate_pool", {}).get("candidates", [])
        if isinstance(candidate, dict) and candidate.get("profile_evidence_id")
    }
    statements: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    ungroupable: list[dict[str, Any]] = []
    seen_in_plan: set[str] = set()

    for summary in content_plan.get("summary_themes", []):
        if not isinstance(summary, dict):
            continue
        evidence_ids = _evidence_ids(summary.get("profile_evidence_id"))
        seen_in_plan.update(evidence_ids)
        _emit_or_omit_plan_item(
            statements=statements,
            omitted=omitted,
            ungroupable=ungroupable,
            candidate_by_id=candidate_by_id,
            evidence_ids=evidence_ids,
            target_section="professional_summary",
            source_plan_type="summary_theme",
            source_plan_id=summary.get("profile_evidence_id"),
            target_record_id=None,
        )

    for role_plan in content_plan.get("role_bullet_plans", []):
        if not isinstance(role_plan, dict):
            continue
        evidence_ids = _evidence_ids(role_plan.get("supporting_profile_evidence_ids", []))
        seen_in_plan.update(evidence_ids)
        _emit_or_omit_plan_item(
            statements=statements,
            omitted=omitted,
            ungroupable=ungroupable,
            candidate_by_id=candidate_by_id,
            evidence_ids=evidence_ids,
            target_section=role_plan.get("target_section", "professional_experience"),
            source_plan_type="role_bullet_plan",
            source_plan_id=role_plan.get("plan_id"),
            target_record_id=role_plan.get("target_record_id"),
        )

    for skill in content_plan.get("skills_to_surface", []):
        if not isinstance(skill, dict):
            continue
        evidence_ids = _evidence_ids(skill.get("profile_evidence_id"))
        seen_in_plan.update(evidence_ids)
        _emit_or_omit_plan_item(
            statements=statements,
            omitted=omitted,
            ungroupable=ungroupable,
            candidate_by_id=candidate_by_id,
            evidence_ids=evidence_ids,
            target_section="skills",
            source_plan_type="skill",
            source_plan_id=skill.get("profile_evidence_id"),
            target_record_id=None,
        )

    for qualification in content_plan.get("qualifications_to_surface", []):
        if not isinstance(qualification, dict):
            continue
        evidence_ids = _evidence_ids(qualification.get("profile_evidence_id"))
        seen_in_plan.update(evidence_ids)
        _emit_or_omit_plan_item(
            statements=statements,
            omitted=omitted,
            ungroupable=ungroupable,
            candidate_by_id=candidate_by_id,
            evidence_ids=evidence_ids,
            target_section="qualifications",
            source_plan_type="qualification",
            source_plan_id=qualification.get("profile_evidence_id"),
            target_record_id=None,
        )

    statements = _dedupe_statements_by_evidence(statements)
    statement_evidence_ids = {
        evidence_id
        for statement in statements
        for evidence_id in statement["source_profile_evidence_ids"]
    }
    omitted_evidence_ids = {item["profile_evidence_id"] for item in omitted}
    for evidence_id in content_plan.get("provenance", {}).get("selected_profile_evidence_ids", []):
        if evidence_id in statement_evidence_ids or evidence_id in omitted_evidence_ids:
            continue
        candidate = candidate_by_id.get(evidence_id)
        reason = "GATE_ONLY_NOT_SUBSTANTIVE" if _is_gate_only(candidate) else "NO_STATEMENT_PLAN"
        omitted.append(_omission(evidence_id, reason, "selected_evidence", None))

    statements.sort(key=lambda item: item["statement_id"])
    omitted.sort(key=lambda item: (item["profile_evidence_id"], item["reason"], item.get("source_plan_type") or ""))
    ungroupable.sort(key=lambda item: item["group_id"])

    return {
        "schema_version": CV_STATEMENT_PLAN_VERSION,
        "source_plan_version": content_plan.get("schema_version"),
        "statements": statements,
        "omitted_evidence": omitted,
        "ungroupable_evidence": ungroupable,
        "uncovered_requirements": copy.deepcopy(content_plan.get("uncovered_requirements", [])),
        "provenance": {
            "source_selected_profile_evidence_ids": list(
                content_plan.get("provenance", {}).get("selected_profile_evidence_ids", [])
            ),
            "statement_profile_evidence_ids": sorted(statement_evidence_ids),
            "omitted_profile_evidence_ids": sorted({item["profile_evidence_id"] for item in omitted}),
            "source_selected_job_requirement_ids": list(
                content_plan.get("provenance", {}).get("selected_job_requirement_ids", [])
            ),
        },
    }


def _dedupe_statements_by_evidence(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one statement for an identical evidence set.

    Task 1 can surface the same selected evidence in more than one planning
    view, for example summary and skills.  Task 2 represents factual content,
    so emitting the same exact fact twice would add no provenance value.
    """

    seen: set[tuple[str, ...]] = set()
    deduped = []
    for statement in statements:
        key = tuple(statement["source_profile_evidence_ids"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(statement)
    return deduped


def _emit_or_omit_plan_item(
    *,
    statements: list[dict[str, Any]],
    omitted: list[dict[str, Any]],
    ungroupable: list[dict[str, Any]],
    candidate_by_id: dict[str, dict[str, Any]],
    evidence_ids: list[str],
    target_section: str,
    source_plan_type: str,
    source_plan_id: str | None,
    target_record_id: str | None,
) -> None:
    if not evidence_ids:
        return

    candidates = [candidate_by_id.get(evidence_id) for evidence_id in evidence_ids]
    unknown_ids = [
        evidence_id for evidence_id, candidate in zip(evidence_ids, candidates)
        if candidate is None
    ]
    for evidence_id in unknown_ids:
        omitted.append(_omission(evidence_id, "INSUFFICIENT_STATEMENT_CONTEXT", source_plan_type, source_plan_id))
    known_candidates = [candidate for candidate in candidates if candidate is not None]
    substantive = []
    for candidate in known_candidates:
        if _is_gate_only(candidate):
            omitted.append(
                _omission(candidate["profile_evidence_id"], "GATE_ONLY_NOT_SUBSTANTIVE", source_plan_type, source_plan_id)
            )
        else:
            substantive.append(candidate)
    if not substantive:
        return

    if len(substantive) == 1:
        statements.append(_statement(substantive, target_section, source_plan_type, source_plan_id, "single", target_record_id))
        return

    safety = _composition_safety(substantive, target_section, target_record_id)
    if safety["safe"]:
        statements.append(_statement(substantive, target_section, source_plan_type, source_plan_id, "composed", target_record_id))
        return

    ungroupable.append({
        "group_id": _stable_id("ungroupable", {
            "evidence_ids": [candidate["profile_evidence_id"] for candidate in substantive],
            "source_plan_type": source_plan_type,
            "source_plan_id": source_plan_id,
            "reason": safety["reason"],
        }),
        "reason": safety["reason"],
        "source_plan_type": source_plan_type,
        "source_plan_id": source_plan_id,
        "profile_evidence_ids": [candidate["profile_evidence_id"] for candidate in substantive],
        "emitted_as_separate_statements": True,
    })
    for candidate in substantive:
        statements.append(_statement([candidate], target_section, source_plan_type, source_plan_id, "single", target_record_id))


def _composition_safety(
    candidates: list[dict[str, Any]],
    target_section: str,
    target_record_id: str | None,
) -> dict[str, Any]:
    if target_section == "professional_experience":
        record_ids = {candidate.get("record_id") for candidate in candidates}
        if len(record_ids) != 1 or None in record_ids:
            return {"safe": False, "reason": "CROSS_RECORD_GROUPING_FORBIDDEN"}
        if target_record_id is not None and record_ids != {target_record_id}:
            return {"safe": False, "reason": "CROSS_RECORD_GROUPING_FORBIDDEN"}
        if any(candidate.get("category") != "employment" for candidate in candidates):
            return {"safe": False, "reason": "UNSAFE_TO_COMBINE"}
        if any(candidate.get("field") not in SAFE_ROLE_COMPOSITION_FIELDS for candidate in candidates):
            return {"safe": False, "reason": "UNSAFE_TO_COMBINE"}
        return {"safe": True, "reason": None}
    return {"safe": False, "reason": "UNSAFE_TO_COMBINE"}


def _statement(
    candidates: list[dict[str, Any]],
    target_section: str,
    source_plan_type: str,
    source_plan_id: str | None,
    composition_mode: str,
    target_record_id: str | None,
) -> dict[str, Any]:
    fragments = [_fragment(candidate) for candidate in candidates]
    evidence_ids = [fragment["profile_evidence_id"] for fragment in fragments]
    requirement_ids = sorted({
        req_id for candidate in candidates for req_id in candidate.get("matched_job_requirement_ids", [])
    })
    record_ids = sorted({
        candidate.get("record_id") for candidate in candidates if candidate.get("record_id")
    })
    record_id = target_record_id or (record_ids[0] if len(record_ids) == 1 else None)
    statement_text = "; ".join(fragment["text"] for fragment in fragments)
    payload = {
        "target_section": target_section,
        "target_record_id": record_id,
        "source_profile_evidence_ids": evidence_ids,
        "source_job_requirement_ids": requirement_ids,
        "statement_text": statement_text,
        "composition_mode": composition_mode,
        "source_plan_type": source_plan_type,
        "source_plan_id": source_plan_id,
    }
    return {
        "statement_id": _stable_id("stmt", payload),
        **payload,
        "structured_content": {
            "fragments": fragments,
            "joiner": "; " if len(fragments) > 1 else None,
        },
        "composition": {
            "mode": composition_mode,
            "grouping_rule": (
                "same_record_employment_responsibility_fragments"
                if composition_mode == "composed"
                else "single_evidence_exact_value"
            ),
            "source_plan_type": source_plan_type,
            "source_plan_id": source_plan_id,
        },
        "provenance": {
            "profile_evidence_ids": evidence_ids,
            "job_requirement_ids": requirement_ids,
        },
    }


def _fragment(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile_evidence_id": candidate["profile_evidence_id"],
        "category": candidate.get("category"),
        "field": candidate.get("field"),
        "record_id": candidate.get("record_id"),
        "text": _normalize_text(candidate.get("value")),
    }


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _evidence_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _is_gate_only(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    if candidate.get("match_type") != "gate":
        return False
    contexts = candidate.get("match_contexts", [])
    return not any(
        context.get("match_type") in SUBSTANTIVE_MATCH_TYPES
        for context in contexts
        if isinstance(context, dict)
    )


def _omission(
    evidence_id: str,
    reason: str,
    source_plan_type: str | None,
    source_plan_id: str | None,
) -> dict[str, Any]:
    return {
        "profile_evidence_id": evidence_id,
        "reason": reason,
        "source_plan_type": source_plan_type,
        "source_plan_id": source_plan_id,
    }


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
