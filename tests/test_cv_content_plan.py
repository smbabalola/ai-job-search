"""Characterization tests for CV Generation Quality v2 Task 1.

These tests pin the first pure-product planning layer.  They intentionally do
not change CV generation v1, Application Intelligence, rendering, persistence,
or provider behavior.
"""

import copy
import unittest

from product.cv_content_plan import (
    CV_CONTENT_PLAN_VERSION,
    build_cv_evidence_candidate_pool,
    plan_cv_content,
)


def source(line: int = 1) -> dict:
    return {"file": "PROFILE.md", "section": "Profile", "line_start": line, "line_end": line}


def claim(
    claim_id: str,
    category: str,
    field: str,
    value: str,
    *,
    record_id: str | None = None,
    concept_id: str | None = None,
    placeholder: bool = False,
) -> dict:
    item = {
        "id": claim_id,
        "concept_id": concept_id or f"cpt_{claim_id}",
        "category": category,
        "field": field,
        "value": value,
        "source": source(1),
        "placeholder": placeholder,
        "confidence": "high",
        "extraction_status": "explicit",
    }
    if record_id is not None:
        item["record_id"] = record_id
    return item


def employment_record(record_id: str, title: str, employer: str, date_range: str) -> list[dict]:
    suffix = record_id.replace("rec_", "")
    return [
        claim(f"clm_{suffix}_title", "employment", "job_title", title, record_id=record_id),
        claim(f"clm_{suffix}_employer", "employment", "employer", employer, record_id=record_id),
        claim(f"clm_{suffix}_dates", "employment", "date_range", date_range, record_id=record_id),
    ]


def profile_snapshot(claims: list[dict], conflicts: list[dict] | None = None) -> dict:
    return {
        "schema_version": "candidate-profile-evidence-snapshot.v0",
        "claims": copy.deepcopy(claims),
        "corroborations": [],
        "conflicts": conflicts or [],
        "summary": {
            "claim_count": len(claims),
            "placeholder_claim_count": sum(1 for item in claims if item.get("placeholder")),
            "conflict_count": len(conflicts or []),
        },
    }


def resolved_job_evidence() -> dict:
    return {
        "schema_version": "resolved-job-evidence-bundle.v0",
        "evidence": [
            {"id": "job_hpht", "category": "requirements", "text": "Requires North Sea HPHT drilling delivery.", "kind": "required"},
            {"id": "job_python", "category": "requirements", "text": "Requires production Python experience.", "kind": "required"},
            {"id": "job_leadership", "category": "responsibilities", "text": "Lead cross-functional technical delivery.", "kind": "preferred"},
            {"id": "job_uncovered", "category": "requirements", "text": "Requires fluent German.", "kind": "required"},
        ],
        "aliases": [],
        "excluded": {},
        "summary": {"evidence_count": 4, "alias_count": 0},
    }


def match(
    match_id: str,
    classification: str,
    job_requirement_ids: list[str],
    profile_evidence_ids: list[str],
) -> dict:
    return {
        "match_id": match_id,
        "classification": classification,
        "job_requirement_ids": job_requirement_ids,
        "profile_evidence_ids": profile_evidence_ids,
        "rationale": "fixture",
        "confidence": "high",
        "status": "READY",
    }


def job_fit_result(
    *,
    direct: list[dict] | None = None,
    functional: list[dict] | None = None,
    transferable: list[dict] | None = None,
    gaps: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": "semantic-job-fit-result.v1",
        "status": "READY",
        "blocked": False,
        "direct_matches": direct or [],
        "functionally_equivalent_matches": functional or [],
        "transferable_matches": transferable or [],
        "gate_assessments": [],
        "gaps": gaps or [],
    }


class TestCvEvidenceCandidatePool(unittest.TestCase):
    def test_candidate_pool_retains_record_context_and_match_references(self):
        claims = [
            *employment_record("rec_old", "Senior Drilling Engineer", "North Sea Operator", "2014-2016"),
            claim(
                "clm_hpht",
                "employment",
                "responsibility_or_achievement",
                "Delivered North Sea HPHT drilling programme",
                record_id="rec_old",
            ),
        ]
        pool = build_cv_evidence_candidate_pool(
            profile_snapshot(claims),
            job_fit_result(direct=[match("m1", "direct", ["job_hpht"], ["clm_hpht"])]),
            resolved_job_evidence(),
        )
        candidate = next(item for item in pool["candidates"] if item["profile_evidence_id"] == "clm_hpht")
        self.assertEqual(candidate["record_id"], "rec_old")
        self.assertEqual(candidate["record_context"]["job_title"]["value"], "Senior Drilling Engineer")
        self.assertEqual(candidate["record_context"]["employer"]["value"], "North Sea Operator")
        self.assertEqual(candidate["matched_job_requirement_ids"], ["job_hpht"])
        self.assertEqual(candidate["match_type"], "direct")

    def test_conflict_and_placeholder_status_are_auditable_but_not_planned(self):
        claims = [
            claim("clm_safe", "skills", "technical_skill", "Python"),
            claim("clm_placeholder", "skills", "technical_skill", "German", placeholder=True),
            claim("clm_conflict", "skills", "technical_skill", "Conflicting skill", concept_id="cpt_conflict"),
        ]
        fit = job_fit_result(direct=[
            match("m_safe", "direct", ["job_python"], ["clm_safe"]),
            match("m_placeholder", "direct", ["job_uncovered"], ["clm_placeholder"]),
            match("m_conflict", "direct", ["job_leadership"], ["clm_conflict"]),
        ])
        plan = plan_cv_content(
            profile_snapshot(claims, conflicts=[{"concept_id": "cpt_conflict"}]),
            fit,
            resolved_job_evidence(),
        )
        candidates = {item["profile_evidence_id"]: item for item in plan["candidate_pool"]["candidates"]}
        self.assertTrue(candidates["clm_placeholder"]["placeholder"])
        self.assertTrue(candidates["clm_conflict"]["conflicted"])
        selected = set(plan["provenance"]["selected_profile_evidence_ids"])
        self.assertNotIn("clm_placeholder", selected)
        self.assertNotIn("clm_conflict", selected)


class TestCvContentPlanning(unittest.TestCase):
    def test_direct_evidence_outranks_weaker_transferable_for_same_requirement(self):
        claims = [
            claim("clm_direct", "skills", "technical_skill", "Python"),
            claim("clm_transfer", "skills", "technical_skill", "MATLAB scripting"),
        ]
        fit = job_fit_result(
            direct=[match("m_direct", "direct", ["job_python"], ["clm_direct"])],
            transferable=[match("m_transfer", "transferable", ["job_python"], ["clm_transfer"])],
        )
        plan = plan_cv_content(profile_snapshot(claims), fit, resolved_job_evidence(), budgets={"max_planned_evidence": 1})
        self.assertEqual(plan["provenance"]["selected_profile_evidence_ids"], ["clm_direct"])

    def test_older_strong_direct_evidence_can_outrank_newer_generic_evidence(self):
        claims = [
            *employment_record("rec_old", "Senior Drilling Engineer", "North Sea Operator", "2014-2016"),
            claim(
                "clm_old_hpht",
                "employment",
                "responsibility_or_achievement",
                "Delivered North Sea HPHT drilling programme",
                record_id="rec_old",
            ),
            *employment_record("rec_new", "Operations Consultant", "GenericCo", "2022-2024"),
            claim(
                "clm_new_generic",
                "employment",
                "responsibility_or_achievement",
                "Supported operational reporting",
                record_id="rec_new",
            ),
        ]
        fit = job_fit_result(
            direct=[match("m_old", "direct", ["job_hpht"], ["clm_old_hpht"])],
            transferable=[match("m_new", "transferable", ["job_hpht"], ["clm_new_generic"])],
        )
        plan = plan_cv_content(profile_snapshot(claims), fit, resolved_job_evidence(), budgets={"max_planned_evidence": 1})
        self.assertEqual(plan["provenance"]["selected_profile_evidence_ids"], ["clm_old_hpht"])

    def test_measurable_specific_evidence_preferred_when_relevance_is_comparable(self):
        claims = [
            claim("clm_generic", "employment", "responsibility_or_achievement", "Built data pipelines", record_id="rec_a"),
            claim(
                "clm_specific",
                "employment",
                "responsibility_or_achievement",
                "Built 12 production Python data pipelines processing 4TB monthly",
                record_id="rec_b",
            ),
        ]
        fit = job_fit_result(direct=[
            match("m_generic", "direct", ["job_python"], ["clm_generic"]),
            match("m_specific", "direct", ["job_python"], ["clm_specific"]),
        ])
        plan = plan_cv_content(profile_snapshot(claims), fit, resolved_job_evidence(), budgets={"max_planned_evidence": 1})
        self.assertEqual(plan["provenance"]["selected_profile_evidence_ids"], ["clm_specific"])

    def test_different_employment_records_are_never_grouped_into_one_role_bullet_plan(self):
        claims = [
            *employment_record("rec_a", "Data Engineer", "Alpha", "2020-2021"),
            claim("clm_a", "employment", "responsibility_or_achievement", "Built Python pipelines", record_id="rec_a"),
            *employment_record("rec_b", "Platform Engineer", "Beta", "2022-2023"),
            claim("clm_b", "employment", "responsibility_or_achievement", "Led technical delivery", record_id="rec_b"),
        ]
        fit = job_fit_result(direct=[
            match("m_a", "direct", ["job_python"], ["clm_a"]),
            match("m_b", "direct", ["job_leadership"], ["clm_b"]),
        ])
        plan = plan_cv_content(profile_snapshot(claims), fit, resolved_job_evidence())
        self.assertEqual({item["target_record_id"] for item in plan["role_bullet_plans"]}, {"rec_a", "rec_b"})
        for item in plan["role_bullet_plans"]:
            evidence_records = {
                candidate["record_id"]
                for candidate in plan["candidate_pool"]["candidates"]
                if candidate["profile_evidence_id"] in item["supporting_profile_evidence_ids"]
            }
            self.assertEqual(evidence_records, {item["target_record_id"]})

    def test_generic_skill_is_not_falsely_assigned_to_an_employer(self):
        claims = [
            *employment_record("rec_a", "Data Engineer", "Alpha", "2020-2021"),
            claim("clm_skill", "skills", "technical_skill", "Python"),
        ]
        fit = job_fit_result(direct=[match("m_skill", "direct", ["job_python"], ["clm_skill"])])
        plan = plan_cv_content(profile_snapshot(claims), fit, resolved_job_evidence())
        self.assertFalse(plan["role_bullet_plans"])
        self.assertEqual(plan["skills_to_surface"][0]["profile_evidence_id"], "clm_skill")

    def test_supported_important_requirements_receive_planned_coverage(self):
        claims = [
            claim("clm_python", "skills", "technical_skill", "Python"),
            claim("clm_hpht", "employment", "responsibility_or_achievement", "Delivered North Sea HPHT drilling", record_id="rec_old"),
        ]
        fit = job_fit_result(direct=[
            match("m_python", "direct", ["job_python"], ["clm_python"]),
            match("m_hpht", "direct", ["job_hpht"], ["clm_hpht"]),
        ])
        plan = plan_cv_content(profile_snapshot(claims), fit, resolved_job_evidence())
        covered = {
            item["job_requirement_id"]
            for item in plan["must_cover_requirements"]
            if item["covered"]
        }
        self.assertGreaterEqual(covered, {"job_python", "job_hpht"})

    def test_unsupported_requirements_remain_explicit_gaps(self):
        claims = [claim("clm_python", "skills", "technical_skill", "Python")]
        fit = job_fit_result(
            direct=[match("m_python", "direct", ["job_python"], ["clm_python"])],
            gaps=[{"gap_id": "gap_german", "job_requirement_ids": ["job_uncovered"], "gap_type": "unsupported"}],
        )
        plan = plan_cv_content(profile_snapshot(claims), fit, resolved_job_evidence())
        gaps = {item["job_requirement_id"] for item in plan["uncovered_requirements"]}
        self.assertIn("job_uncovered", gaps)
        self.assertNotIn("job_uncovered", plan["provenance"]["selected_job_requirement_ids"])

    def test_duplicate_evidence_does_not_consume_entire_budget(self):
        claims = [
            claim("clm_python_a", "skills", "technical_skill", "Python"),
            claim("clm_python_b", "skills", "technical_skill", "Python"),
            claim("clm_hpht", "employment", "responsibility_or_achievement", "North Sea HPHT drilling", record_id="rec_old"),
        ]
        fit = job_fit_result(direct=[
            match("m_python_a", "direct", ["job_python"], ["clm_python_a"]),
            match("m_python_b", "direct", ["job_python"], ["clm_python_b"]),
            match("m_hpht", "direct", ["job_hpht"], ["clm_hpht"]),
        ])
        plan = plan_cv_content(profile_snapshot(claims), fit, resolved_job_evidence(), budgets={"max_planned_evidence": 2})
        self.assertEqual(set(plan["provenance"]["selected_job_requirement_ids"]), {"job_hpht", "job_python"})
        selected_python = [item for item in plan["provenance"]["selected_profile_evidence_ids"] if item.startswith("clm_python")]
        self.assertEqual(len(selected_python), 1)

    def test_plan_respects_configurable_role_and_overall_budgets(self):
        claims = [
            claim("clm_a", "employment", "responsibility_or_achievement", "Built Python pipelines", record_id="rec_a"),
            claim("clm_b", "employment", "responsibility_or_achievement", "Delivered North Sea HPHT drilling", record_id="rec_b"),
            claim("clm_c", "employment", "responsibility_or_achievement", "Led technical delivery", record_id="rec_c"),
        ]
        fit = job_fit_result(direct=[
            match("m_a", "direct", ["job_python"], ["clm_a"]),
            match("m_b", "direct", ["job_hpht"], ["clm_b"]),
            match("m_c", "direct", ["job_leadership"], ["clm_c"]),
        ])
        plan = plan_cv_content(
            profile_snapshot(claims),
            fit,
            resolved_job_evidence(),
            budgets={"max_planned_evidence": 2, "max_role_bullet_plans": 1},
        )
        self.assertEqual(len(plan["provenance"]["selected_profile_evidence_ids"]), 2)
        self.assertEqual(len(plan["role_bullet_plans"]), 1)
        self.assertEqual(plan["budgets"]["max_role_bullet_plans"], 1)

    def test_every_selected_plan_item_retains_profile_and_job_provenance(self):
        claims = [claim("clm_hpht", "employment", "responsibility_or_achievement", "North Sea HPHT drilling", record_id="rec_old")]
        fit = job_fit_result(direct=[match("m_hpht", "direct", ["job_hpht"], ["clm_hpht"])])
        plan = plan_cv_content(profile_snapshot(claims), fit, resolved_job_evidence())
        role_plan = plan["role_bullet_plans"][0]
        self.assertEqual(role_plan["supporting_profile_evidence_ids"], ["clm_hpht"])
        self.assertEqual(role_plan["matched_job_requirement_ids"], ["job_hpht"])
        self.assertIn("job_relevance", role_plan["ranking_rationale"])
        self.assertIn("evidence_strength", role_plan["ranking_rationale"])

    def test_matched_items_retain_job_fit_requirement_references(self):
        claims = [claim("clm_python", "skills", "technical_skill", "Python")]
        fit = job_fit_result(direct=[match("m_python", "direct", ["job_python"], ["clm_python"])])
        plan = plan_cv_content(profile_snapshot(claims), fit, resolved_job_evidence())
        skill = plan["skills_to_surface"][0]
        self.assertEqual(skill["matched_job_requirement_ids"], ["job_python"])
        pool_item = next(item for item in plan["candidate_pool"]["candidates"] if item["profile_evidence_id"] == "clm_python")
        self.assertEqual(pool_item["match_contexts"][0]["match_id"], "m_python")
        self.assertEqual(pool_item["match_contexts"][0]["job_evidence"][0]["text"], "Requires production Python experience.")

    def test_gate_only_evidence_remains_auditable_but_not_summary_or_role_content(self):
        claims = [
            claim(
                "clm_work_auth",
                "eligibility",
                "work_authorization",
                "Right to work in the UK",
                record_id="rec_auth",
            )
        ]
        job = {
            "schema_version": "resolved-job-evidence-bundle.v0",
            "evidence": [
                {
                    "id": "job_work_auth",
                    "category": "eligibility_requirements",
                    "text": "Applicants must have the right to work in the UK.",
                    "kind": "required",
                }
            ],
            "aliases": [],
            "excluded": {},
            "summary": {"evidence_count": 1, "alias_count": 0},
        }
        fit = job_fit_result()
        fit["gate_assessments"] = [
            {
                "gate_id": "eligibility",
                "status": "PASS",
                "reason": "Candidate has right-to-work evidence.",
                "job_evidence_ids": ["job_work_auth"],
                "profile_evidence_ids": ["clm_work_auth"],
            }
        ]
        plan = plan_cv_content(profile_snapshot(claims), fit, job)

        self.assertFalse(plan["summary_themes"])
        self.assertFalse(plan["role_bullet_plans"])
        self.assertEqual(plan["provenance"]["selected_profile_evidence_ids"], ["clm_work_auth"])
        self.assertEqual(plan["provenance"]["selected_job_requirement_ids"], ["job_work_auth"])
        self.assertEqual(
            plan["must_cover_requirements"],
            [
                {
                    "job_requirement_id": "job_work_auth",
                    "kind": "required",
                    "text": "Applicants must have the right to work in the UK.",
                    "covered": True,
                    "supporting_profile_evidence_ids": ["clm_work_auth"],
                }
            ],
        )
        pool_item = plan["candidate_pool"]["candidates"][0]
        self.assertEqual(pool_item["profile_evidence_id"], "clm_work_auth")
        self.assertEqual(pool_item["match_type"], "gate")
        self.assertEqual(pool_item["match_contexts"][0]["match_id"], "eligibility")

    def test_mixed_substantive_and_gate_evidence_remains_summary_eligible(self):
        claims = [claim("clm_python", "skills", "technical_skill", "Python")]
        fit = job_fit_result(direct=[match("m_python", "direct", ["job_python"], ["clm_python"])])
        fit["gate_assessments"] = [
            {
                "gate_id": "technical_gate",
                "status": "PASS",
                "reason": "Python also satisfies a gate-like check.",
                "job_evidence_ids": ["job_python"],
                "profile_evidence_ids": ["clm_python"],
            }
        ]
        plan = plan_cv_content(profile_snapshot(claims), fit, resolved_job_evidence())

        self.assertEqual(plan["summary_themes"][0]["profile_evidence_id"], "clm_python")
        pool_item = next(item for item in plan["candidate_pool"]["candidates"] if item["profile_evidence_id"] == "clm_python")
        self.assertEqual(pool_item["match_type"], "direct")
        self.assertEqual({context["match_type"] for context in pool_item["match_contexts"]}, {"direct", "gate"})

    def test_output_is_deterministic_for_identical_inputs(self):
        claims = [
            claim("clm_python", "skills", "technical_skill", "Python"),
            claim("clm_hpht", "employment", "responsibility_or_achievement", "North Sea HPHT drilling", record_id="rec_old"),
        ]
        fit = job_fit_result(direct=[
            match("m_python", "direct", ["job_python"], ["clm_python"]),
            match("m_hpht", "direct", ["job_hpht"], ["clm_hpht"]),
        ])
        profile = profile_snapshot(claims)
        first = plan_cv_content(profile, fit, resolved_job_evidence())
        second = plan_cv_content(profile, fit, resolved_job_evidence())
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], CV_CONTENT_PLAN_VERSION)


if __name__ == "__main__":
    unittest.main()
