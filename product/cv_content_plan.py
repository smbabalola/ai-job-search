"""Pure CV Generation Quality v2 evidence planning.

This module deliberately stops before prose generation.  It answers the first
CV-native question: which supported evidence belongs in a tailored CV, and
where should that evidence be placed?

The implementation is intentionally deterministic and inspectable.  It does
not collapse relevance, specificity, provenance, recency, and record integrity
into one opaque score; instead every candidate carries explicit ranking
dimensions and the planner uses a stable lexicographic ordering over those
dimensions.
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from typing import Any


CV_CONTENT_PLAN_VERSION = "cv-content-plan.v0"
CV_EVIDENCE_CANDIDATE_POOL_VERSION = "cv-evidence-candidate-pool.v0"

DEFAULT_PLANNING_BUDGETS: dict[str, Any] = {
    # Pure-planner defaults, not production rendering limits.  These keep the
    # first v2 layer honest: it must choose evidence instead of dumping the
    # whole profile.  Callers/tests may override any value.
    "max_planned_evidence": 12,
    "max_role_bullet_plans": 6,
    "max_bullet_plans_per_role": 2,
    "max_summary_evidence": 3,
    "max_skills": 6,
    "max_qualifications": 4,
}

MATCH_TYPE_PRECEDENCE = {
    "direct": 4,
    "functionally_equivalent": 3,
    "transferable": 2,
    "gate": 1,
    "none": 0,
}

REQUIREMENT_KIND_PRECEDENCE = {
    "required": 3,
    "preferred": 2,
    "informational": 1,
    "unknown": 0,
    None: 0,
}

MATCH_COLLECTIONS: tuple[tuple[str, str], ...] = (
    ("direct_matches", "direct"),
    ("functionally_equivalent_matches", "functionally_equivalent"),
    ("transferable_matches", "transferable"),
)

EMPLOYMENT_FIELDS_FOR_ROLE_CONTEXT = {"job_title", "employer", "date_range", "location"}
EMPLOYMENT_FIELDS_FOR_BULLET_PLANS = {"responsibility_or_achievement"}
QUALIFICATION_CATEGORIES = {"education", "certifications"}


def build_cv_evidence_candidate_pool(
    profile_snapshot: dict[str, Any],
    job_fit_result: dict[str, Any],
    resolved_job_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Return profile claims enriched for downstream CV planning.

    The pool retains unsafe claims with flags so audits can see why the planner
    omitted them.  Planning functions must not select placeholder or conflicted
    claims.
    """

    profile_claims = [
        copy.deepcopy(claim)
        for claim in profile_snapshot.get("claims", [])
        if isinstance(claim, dict) and claim.get("id")
    ]
    profile_by_id = {claim["id"]: claim for claim in profile_claims}
    conflict_concepts = {
        item.get("concept_id")
        for item in profile_snapshot.get("conflicts", [])
        if isinstance(item, dict) and item.get("concept_id")
    }
    job_by_id = {
        item.get("id"): item
        for item in resolved_job_evidence.get("evidence", [])
        if isinstance(item, dict) and item.get("id")
    }
    match_contexts_by_profile_id = _match_contexts_by_profile_id(job_fit_result, job_by_id)
    record_context_by_id = _record_context_by_id(profile_claims)

    candidates = []
    for claim in profile_claims:
        record_id = claim.get("record_id")
        contexts = match_contexts_by_profile_id.get(claim["id"], [])
        conflicted = claim.get("concept_id") in conflict_concepts
        duplicate_key = _duplicate_key(claim)
        candidate = {
            "profile_evidence_id": claim["id"],
            "category": claim.get("category"),
            "field": claim.get("field"),
            "value": claim.get("value"),
            "record_id": record_id,
            "record_context": copy.deepcopy(record_context_by_id.get(record_id, {})) if record_id else {},
            "source": copy.deepcopy(claim.get("source")),
            "placeholder": bool(claim.get("placeholder")),
            "conflicted": bool(conflicted),
            "match_contexts": copy.deepcopy(contexts),
            "matched_job_requirement_ids": sorted({
                req_id for context in contexts for req_id in context.get("job_requirement_ids", [])
            }),
            "match_type": _best_match_type(contexts),
            "requirement_importance": _best_requirement_importance(contexts),
            "duplicate_key": duplicate_key,
        }
        candidate["ranking_dimensions"] = _ranking_dimensions(candidate, claim, contexts)
        candidates.append(candidate)

    candidates.sort(key=_candidate_sort_key)
    return {
        "schema_version": CV_EVIDENCE_CANDIDATE_POOL_VERSION,
        "candidates": candidates,
        "summary": {
            "candidate_count": len(candidates),
            "safe_candidate_count": sum(
                1 for candidate in candidates
                if not candidate["placeholder"] and not candidate["conflicted"]
            ),
            "placeholder_candidate_count": sum(1 for candidate in candidates if candidate["placeholder"]),
            "conflicted_candidate_count": sum(1 for candidate in candidates if candidate["conflicted"]),
        },
    }


def plan_cv_content(
    profile_snapshot: dict[str, Any],
    job_fit_result: dict[str, Any],
    resolved_job_evidence: dict[str, Any],
    *,
    budgets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic CV content plan without generating prose."""

    resolved_budgets = _resolve_budgets(budgets)
    pool = build_cv_evidence_candidate_pool(profile_snapshot, job_fit_result, resolved_job_evidence)
    candidates = pool["candidates"]
    candidates_by_id = {candidate["profile_evidence_id"]: candidate for candidate in candidates}
    safe_candidates = [
        candidate for candidate in candidates
        if not candidate["placeholder"] and not candidate["conflicted"]
    ]
    duplicate_winners = _duplicate_winners(safe_candidates)

    selected_ids: list[str] = []
    omitted_reasons: dict[str, set[str]] = defaultdict(set)

    for candidate in safe_candidates:
        if duplicate_winners.get(candidate["duplicate_key"]) != candidate["profile_evidence_id"]:
            omitted_reasons[candidate["profile_evidence_id"]].add("duplicate_or_near_duplicate")

    for req_id in _important_requirement_ids(resolved_job_evidence):
        if len(selected_ids) >= resolved_budgets["max_planned_evidence"]:
            break
        best = _best_candidate_for_requirement(
            req_id,
            safe_candidates,
            selected_ids,
            duplicate_winners,
        )
        if best is not None:
            selected_ids.append(best["profile_evidence_id"])

    for candidate in safe_candidates:
        if len(selected_ids) >= resolved_budgets["max_planned_evidence"]:
            break
        candidate_id = candidate["profile_evidence_id"]
        if candidate_id in selected_ids:
            continue
        if duplicate_winners.get(candidate["duplicate_key"]) != candidate_id:
            continue
        if candidate["match_type"] == "none":
            continue
        selected_ids.append(candidate_id)

    selected_candidates = [candidates_by_id[candidate_id] for candidate_id in selected_ids]
    selected_set = set(selected_ids)

    for candidate in candidates:
        candidate_id = candidate["profile_evidence_id"]
        if candidate_id in selected_set:
            continue
        if candidate["placeholder"]:
            omitted_reasons[candidate_id].add("placeholder")
        if candidate["conflicted"]:
            omitted_reasons[candidate_id].add("conflicted")
        if not omitted_reasons[candidate_id]:
            if candidate["match_type"] == "none":
                omitted_reasons[candidate_id].add("no_supported_job_match")
            else:
                omitted_reasons[candidate_id].add("budget_or_lower_priority")

    role_bullet_plans = _role_bullet_plans(selected_candidates, resolved_budgets)
    role_plan_evidence = {
        evidence_id
        for plan in role_bullet_plans
        for evidence_id in plan["supporting_profile_evidence_ids"]
    }
    skills_to_surface = _surface_items(
        selected_candidates,
        category="skills",
        limit=resolved_budgets["max_skills"],
        exclude_ids=role_plan_evidence,
    )
    qualifications_to_surface = [
        _surface_item(candidate)
        for candidate in selected_candidates
        if candidate["category"] in QUALIFICATION_CATEGORIES
    ][: resolved_budgets["max_qualifications"]]

    covered_requirement_ids = {
        req_id for candidate in selected_candidates for req_id in candidate["matched_job_requirement_ids"]
    }
    important_requirements = _important_requirements(resolved_job_evidence)
    explicit_gap_ids = {
        req_id
        for gap in job_fit_result.get("gaps", [])
        if isinstance(gap, dict)
        for req_id in gap.get("job_requirement_ids", [])
    }
    uncovered_requirements = [
        _requirement_gap_record(requirement, job_fit_result)
        for requirement in important_requirements
        if requirement["id"] not in covered_requirement_ids
    ]
    for gap in job_fit_result.get("gaps", []):
        if not isinstance(gap, dict):
            continue
        for req_id in gap.get("job_requirement_ids", []):
            if req_id in covered_requirement_ids:
                continue
            if not any(item["job_requirement_id"] == req_id for item in uncovered_requirements):
                uncovered_requirements.append({
                    "job_requirement_id": req_id,
                    "text": None,
                    "kind": "unknown",
                    "source": "job_fit_gap",
                    "gap": copy.deepcopy(gap),
                })

    plan = {
        "schema_version": CV_CONTENT_PLAN_VERSION,
        "budgets": resolved_budgets,
        "candidate_pool": pool,
        "summary_themes": [
            _summary_theme(candidate)
            for candidate in _summary_theme_candidates(selected_candidates, resolved_budgets["max_summary_evidence"])
        ],
        "must_cover_requirements": [
            {
                "job_requirement_id": requirement["id"],
                "kind": requirement.get("kind", "unknown"),
                "text": requirement.get("text"),
                "covered": requirement["id"] in covered_requirement_ids,
                "supporting_profile_evidence_ids": [
                    candidate["profile_evidence_id"]
                    for candidate in selected_candidates
                    if requirement["id"] in candidate["matched_job_requirement_ids"]
                ],
            }
            for requirement in important_requirements
        ],
        "role_bullet_plans": role_bullet_plans,
        "skills_to_surface": skills_to_surface,
        "qualifications_to_surface": qualifications_to_surface,
        "omitted_evidence": [
            {
                "profile_evidence_id": candidate_id,
                "reasons": sorted(reasons),
            }
            for candidate_id, reasons in sorted(omitted_reasons.items())
            if candidate_id not in selected_set
        ],
        "uncovered_requirements": uncovered_requirements,
        "section_priorities": _section_priorities(role_bullet_plans, skills_to_surface, qualifications_to_surface),
        "provenance": {
            "selected_profile_evidence_ids": selected_ids,
            "selected_job_requirement_ids": sorted(covered_requirement_ids),
            "explicit_gap_job_requirement_ids": sorted(explicit_gap_ids),
        },
    }
    return plan


def _resolve_budgets(overrides: dict[str, Any] | None) -> dict[str, Any]:
    budgets = copy.deepcopy(DEFAULT_PLANNING_BUDGETS)
    if overrides:
        for key, value in overrides.items():
            if key not in budgets:
                raise ValueError(f"unknown CV planning budget {key!r}")
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"CV planning budget {key!r} must be a non-negative integer")
            budgets[key] = value
    return budgets


def _match_contexts_by_profile_id(
    job_fit_result: dict[str, Any],
    job_by_id: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    contexts_by_profile_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for collection, match_type in MATCH_COLLECTIONS:
        for match in job_fit_result.get(collection, []):
            if not isinstance(match, dict) or match.get("status") not in {None, "READY", "NEEDS_REVIEW"}:
                continue
            job_requirement_ids = [
                req_id for req_id in match.get("job_requirement_ids", [])
                if isinstance(req_id, str)
            ]
            job_refs = [_job_ref(job_by_id.get(req_id)) for req_id in job_requirement_ids]
            context = {
                "match_id": match.get("match_id"),
                "match_type": match_type,
                "classification": match.get("classification", match_type),
                "job_requirement_ids": job_requirement_ids,
                "job_evidence": [ref for ref in job_refs if ref is not None],
                "requirement_importance": _best_kind(ref.get("kind") for ref in job_refs if ref),
                "status": match.get("status"),
            }
            for claim_id in match.get("profile_evidence_ids", []):
                if isinstance(claim_id, str):
                    contexts_by_profile_id[claim_id].append(copy.deepcopy(context))

    for gate in job_fit_result.get("gate_assessments", []):
        if not isinstance(gate, dict) or gate.get("status") not in {"PASS", "REVIEW"}:
            continue
        job_requirement_ids = [
            req_id for req_id in gate.get("job_evidence_ids", [])
            if isinstance(req_id, str)
        ]
        job_refs = [_job_ref(job_by_id.get(req_id)) for req_id in job_requirement_ids]
        context = {
            "match_id": gate.get("gate_id"),
            "match_type": "gate",
            "classification": "gate",
            "job_requirement_ids": job_requirement_ids,
            "job_evidence": [ref for ref in job_refs if ref is not None],
            "requirement_importance": _best_kind(ref.get("kind") for ref in job_refs if ref),
            "status": gate.get("status"),
        }
        for claim_id in gate.get("profile_evidence_ids", []):
            if isinstance(claim_id, str):
                contexts_by_profile_id[claim_id].append(copy.deepcopy(context))
    return contexts_by_profile_id


def _job_ref(job_item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not job_item:
        return None
    return {
        "id": job_item.get("id"),
        "category": job_item.get("category"),
        "kind": job_item.get("kind", "unknown"),
        "text": job_item.get("text"),
    }


def _record_context_by_id(claims: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = defaultdict(dict)
    for claim in claims:
        record_id = claim.get("record_id")
        if not record_id:
            continue
        if claim.get("category") == "employment" and claim.get("field") in EMPLOYMENT_FIELDS_FOR_ROLE_CONTEXT:
            contexts[record_id][claim["field"]] = {
                "profile_evidence_id": claim.get("id"),
                "value": claim.get("value"),
            }
        elif claim.get("category") == "education" and claim.get("field") in {"qualification", "institution", "date_range"}:
            contexts[record_id][claim["field"]] = {
                "profile_evidence_id": claim.get("id"),
                "value": claim.get("value"),
            }
    return dict(contexts)


def _best_match_type(contexts: list[dict[str, Any]]) -> str:
    if not contexts:
        return "none"
    return max(
        (context.get("match_type", "none") for context in contexts),
        key=lambda item: MATCH_TYPE_PRECEDENCE.get(item, 0),
    )


def _best_requirement_importance(contexts: list[dict[str, Any]]) -> str:
    return _best_kind(context.get("requirement_importance") for context in contexts)


def _best_kind(kinds: Any) -> str:
    best = "unknown"
    for kind in kinds:
        candidate = kind or "unknown"
        if REQUIREMENT_KIND_PRECEDENCE.get(candidate, 0) > REQUIREMENT_KIND_PRECEDENCE.get(best, 0):
            best = candidate
    return best


def _ranking_dimensions(
    candidate: dict[str, Any],
    claim: dict[str, Any],
    contexts: list[dict[str, Any]],
) -> dict[str, Any]:
    value = str(claim.get("value", ""))
    source = claim.get("source")
    return {
        "job_relevance": {
            "match_type": candidate["match_type"],
            "match_type_rank": MATCH_TYPE_PRECEDENCE.get(candidate["match_type"], 0),
            "requirement_importance": candidate["requirement_importance"],
            "requirement_importance_rank": REQUIREMENT_KIND_PRECEDENCE.get(candidate["requirement_importance"], 0),
            "matched_requirement_count": len(candidate["matched_job_requirement_ids"]),
        },
        "evidence_strength": {
            "specificity": _specificity(value),
            "measurable_impact": _has_measurable_impact(value),
            "concrete_technical_scope": _has_concrete_technical_scope(value),
            "leadership_or_ownership_scope": _has_leadership_or_ownership(value),
            "seniority_relevance": _seniority_relevance(value, candidate),
            "recency_year": _recency_year(value, candidate),
            "uniqueness_key": candidate["duplicate_key"],
            "record_integrity": _record_integrity(candidate),
            "provenance_strength": _provenance_strength(source, claim),
        },
        "selection_context": {
            "match_count": len(contexts),
            "has_record_id": bool(claim.get("record_id")),
            "has_source": bool(source),
        },
    }


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    relevance = candidate["ranking_dimensions"]["job_relevance"]
    strength = candidate["ranking_dimensions"]["evidence_strength"]
    return (
        -relevance["match_type_rank"],
        -relevance["requirement_importance_rank"],
        -relevance["matched_requirement_count"],
        -strength["specificity"],
        -int(strength["measurable_impact"]),
        -int(strength["concrete_technical_scope"]),
        -int(strength["leadership_or_ownership_scope"]),
        -int(strength["seniority_relevance"]),
        -(strength["recency_year"] or 0),
        -int(strength["record_integrity"]),
        -int(strength["provenance_strength"]),
        candidate["profile_evidence_id"],
    )


def _specificity(value: str) -> int:
    words = re.findall(r"[A-Za-z0-9+#.-]+", value)
    score = min(len(words), 12)
    if _has_measurable_impact(value):
        score += 2
    if _has_concrete_technical_scope(value):
        score += 1
    return score


def _has_measurable_impact(value: str) -> bool:
    return bool(re.search(r"(\d|%|\$|£|€|\bmillion\b|\bthousand\b|\b\d+x\b)", value, re.IGNORECASE))


def _has_concrete_technical_scope(value: str) -> bool:
    return bool(re.search(
        r"\b(api|apis|python|sql|aws|azure|gcp|kubernetes|pipeline|pipelines|hpht|north sea|sap|etl|ml|model|models)\b",
        value,
        re.IGNORECASE,
    ))


def _has_leadership_or_ownership(value: str) -> bool:
    return bool(re.search(r"\b(led|lead|owned|owner|managed|headed|directed|accountable|responsible for)\b", value, re.IGNORECASE))


def _seniority_relevance(value: str, candidate: dict[str, Any]) -> bool:
    role_values = " ".join(
        str(item.get("value", ""))
        for item in candidate.get("record_context", {}).values()
        if isinstance(item, dict)
    )
    combined = f"{value} {role_values}"
    return bool(re.search(r"\b(senior|lead|principal|manager|head|director|chief|vp)\b", combined, re.IGNORECASE))


def _recency_year(value: str, candidate: dict[str, Any]) -> int | None:
    text = value
    date_range = candidate.get("record_context", {}).get("date_range")
    if isinstance(date_range, dict):
        text = f"{text} {date_range.get('value', '')}"
    years = [int(item) for item in re.findall(r"\b(19\d{2}|20\d{2})\b", text)]
    return max(years) if years else None


def _record_integrity(candidate: dict[str, Any]) -> bool:
    if candidate.get("category") != "employment":
        return True
    if not candidate.get("record_id"):
        return False
    if candidate.get("field") in EMPLOYMENT_FIELDS_FOR_ROLE_CONTEXT:
        return True
    return bool(candidate.get("record_context"))


def _provenance_strength(source: Any, claim: dict[str, Any]) -> bool:
    return bool(source) and claim.get("confidence") in {None, "high", "medium"} and claim.get("extraction_status") in {None, "explicit"}


def _duplicate_key(claim: dict[str, Any]) -> str:
    value = str(claim.get("value", "")).lower()
    normalized = " ".join(re.findall(r"[a-z0-9+#.-]+", value))
    return f"{claim.get('category')}:{claim.get('field')}:{normalized}"


def _duplicate_winners(candidates: list[dict[str, Any]]) -> dict[str, str]:
    winners: dict[str, str] = {}
    for candidate in candidates:
        key = candidate["duplicate_key"]
        current = winners.get(key)
        if current is None:
            winners[key] = candidate["profile_evidence_id"]
            continue
        current_candidate = next(item for item in candidates if item["profile_evidence_id"] == current)
        if _candidate_sort_key(candidate) < _candidate_sort_key(current_candidate):
            winners[key] = candidate["profile_evidence_id"]
    return winners


def _important_requirements(resolved_job_evidence: dict[str, Any]) -> list[dict[str, Any]]:
    requirements = [
        item for item in resolved_job_evidence.get("evidence", [])
        if isinstance(item, dict) and item.get("id") and item.get("kind", "unknown") in {"required", "preferred"}
    ]
    return sorted(
        requirements,
        key=lambda item: (-REQUIREMENT_KIND_PRECEDENCE.get(item.get("kind"), 0), item["id"]),
    )


def _important_requirement_ids(resolved_job_evidence: dict[str, Any]) -> list[str]:
    return [item["id"] for item in _important_requirements(resolved_job_evidence)]


def _best_candidate_for_requirement(
    req_id: str,
    candidates: list[dict[str, Any]],
    selected_ids: list[str],
    duplicate_winners: dict[str, str],
) -> dict[str, Any] | None:
    selected = set(selected_ids)
    options = [
        candidate for candidate in candidates
        if req_id in candidate["matched_job_requirement_ids"]
        and candidate["profile_evidence_id"] not in selected
        and duplicate_winners.get(candidate["duplicate_key"]) == candidate["profile_evidence_id"]
    ]
    if not options:
        return None
    return sorted(options, key=_candidate_sort_key)[0]


def _role_bullet_plans(
    selected_candidates: list[dict[str, Any]],
    budgets: dict[str, Any],
) -> list[dict[str, Any]]:
    by_record: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in selected_candidates:
        if candidate["category"] != "employment":
            continue
        if candidate["field"] not in EMPLOYMENT_FIELDS_FOR_BULLET_PLANS:
            continue
        record_id = candidate.get("record_id")
        if not record_id:
            continue
        by_record[record_id].append(candidate)

    plans = []
    for record_id, record_candidates in sorted(
        by_record.items(),
        key=lambda item: _candidate_sort_key(sorted(item[1], key=_candidate_sort_key)[0]),
    ):
        for candidate in sorted(record_candidates, key=_candidate_sort_key)[: budgets["max_bullet_plans_per_role"]]:
            if len(plans) >= budgets["max_role_bullet_plans"]:
                return plans
            plans.append({
                "plan_id": f"role-bullet-{len(plans) + 1}",
                "target_section": "professional_experience",
                "target_record_id": record_id,
                "record_context": copy.deepcopy(candidate.get("record_context", {})),
                "supporting_profile_evidence_ids": [candidate["profile_evidence_id"]],
                "matched_job_requirement_ids": list(candidate["matched_job_requirement_ids"]),
                "intended_emphasis": _intended_emphasis(candidate),
                "ranking_rationale": _ranking_rationale(candidate),
                "prose": None,
            })
    return plans


def _surface_items(
    selected_candidates: list[dict[str, Any]],
    *,
    category: str,
    limit: int,
    exclude_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    excluded = exclude_ids or set()
    return [
        _surface_item(candidate)
        for candidate in selected_candidates
        if candidate["category"] == category and candidate["profile_evidence_id"] not in excluded
    ][:limit]


def _surface_item(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile_evidence_id": candidate["profile_evidence_id"],
        "category": candidate["category"],
        "field": candidate["field"],
        "value": candidate["value"],
        "matched_job_requirement_ids": list(candidate["matched_job_requirement_ids"]),
        "ranking_rationale": _ranking_rationale(candidate),
    }


def _summary_theme(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile_evidence_id": candidate["profile_evidence_id"],
        "theme": _intended_emphasis(candidate),
        "matched_job_requirement_ids": list(candidate["matched_job_requirement_ids"]),
        "ranking_rationale": _ranking_rationale(candidate),
    }


def _summary_theme_candidates(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Return selected evidence that is suitable for professional-summary planning.

    Gate-only evidence remains available for audit/coverage, but it is not
    automatically CV summary material.  Mixed evidence is preserved because a
    substantive match outranks gate in ``match_type``.
    """

    return [
        candidate for candidate in candidates
        if candidate.get("match_type") != "gate"
    ][:limit]


def _intended_emphasis(candidate: dict[str, Any]) -> str:
    if candidate["matched_job_requirement_ids"]:
        return f"{candidate['match_type']} evidence for job requirement coverage"
    if candidate["category"] == "skills":
        return "skill signal"
    if candidate["category"] in QUALIFICATION_CATEGORIES:
        return "qualification signal"
    return "profile evidence"


def _ranking_rationale(candidate: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(candidate["ranking_dimensions"])


def _requirement_gap_record(requirement: dict[str, Any], job_fit_result: dict[str, Any]) -> dict[str, Any]:
    matching_gaps = [
        copy.deepcopy(gap)
        for gap in job_fit_result.get("gaps", [])
        if isinstance(gap, dict) and requirement["id"] in gap.get("job_requirement_ids", [])
    ]
    return {
        "job_requirement_id": requirement["id"],
        "text": requirement.get("text"),
        "kind": requirement.get("kind", "unknown"),
        "source": "resolved_job_evidence",
        "gap": matching_gaps[0] if matching_gaps else None,
    }


def _section_priorities(
    role_bullet_plans: list[dict[str, Any]],
    skills_to_surface: list[dict[str, Any]],
    qualifications_to_surface: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    priorities = [{"section": "professional_summary", "reason": "summarize highest-ranked selected evidence"}]
    if role_bullet_plans:
        priorities.append({"section": "professional_experience", "reason": "place employment-specific evidence under its source record"})
    if skills_to_surface:
        priorities.append({"section": "skills", "reason": "surface generic or cross-role skills without assigning them to an employer"})
    if qualifications_to_surface:
        priorities.append({"section": "qualifications", "reason": "surface selected education/certification evidence separately"})
    return priorities
