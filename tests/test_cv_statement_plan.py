"""Focused tests for CV Generation Quality v2 Task 2 statement composition."""

import copy
import unittest

from product.cv_content_plan import plan_cv_content
from product.cv_statement_plan import CV_STATEMENT_PLAN_VERSION, build_cv_statement_plan


def source() -> dict:
    return {"file": "PROFILE.md", "section": "Profile", "line_start": 1, "line_end": 1}


def claim(
    claim_id: str,
    category: str,
    field: str,
    value: str,
    *,
    record_id: str | None = None,
) -> dict:
    item = {
        "id": claim_id,
        "concept_id": f"cpt_{claim_id}",
        "category": category,
        "field": field,
        "value": value,
        "source": source(),
        "placeholder": False,
        "confidence": "high",
        "extraction_status": "explicit",
    }
    if record_id:
        item["record_id"] = record_id
    return item


def profile_snapshot(claims: list[dict]) -> dict:
    return {
        "schema_version": "candidate-profile-evidence-snapshot.v0",
        "claims": copy.deepcopy(claims),
        "corroborations": [],
        "conflicts": [],
        "summary": {"claim_count": len(claims), "placeholder_claim_count": 0, "conflict_count": 0},
    }


def job_evidence() -> dict:
    return {
        "schema_version": "resolved-job-evidence-bundle.v0",
        "evidence": [
            {"id": "job_python", "category": "requirements", "text": "Requires Python.", "kind": "required"},
            {"id": "job_hpht", "category": "requirements", "text": "Requires HPHT drilling.", "kind": "required"},
            {"id": "job_lead", "category": "responsibilities", "text": "Lead technical delivery.", "kind": "preferred"},
            {"id": "job_auth", "category": "eligibility_requirements", "text": "Right to work required.", "kind": "required"},
            {"id": "job_uncovered", "category": "requirements", "text": "Requires German.", "kind": "required"},
        ],
        "aliases": [],
        "excluded": {},
        "summary": {"evidence_count": 5, "alias_count": 0},
    }


def match(match_id: str, classification: str, job_ids: list[str], profile_ids: list[str]) -> dict:
    return {
        "match_id": match_id,
        "classification": classification,
        "job_requirement_ids": job_ids,
        "profile_evidence_ids": profile_ids,
        "rationale": "fixture",
        "confidence": "high",
        "status": "READY",
    }


def job_fit_result(*, direct=None, functional=None, transferable=None, gates=None, gaps=None) -> dict:
    return {
        "schema_version": "semantic-job-fit-result.v1",
        "status": "READY",
        "blocked": False,
        "direct_matches": direct or [],
        "functionally_equivalent_matches": functional or [],
        "transferable_matches": transferable or [],
        "gate_assessments": gates or [],
        "gaps": gaps or [],
    }


def content_plan_for(claims: list[dict], fit: dict | None = None, *, budgets: dict | None = None) -> dict:
    return plan_cv_content(profile_snapshot(claims), fit or job_fit_result(), job_evidence(), budgets=budgets)


def candidate(plan: dict, evidence_id: str) -> dict:
    return next(item for item in plan["candidate_pool"]["candidates"] if item["profile_evidence_id"] == evidence_id)


class TestCvStatementPlan(unittest.TestCase):
    def test_one_evidence_item_emits_one_grounded_statement(self):
        plan = content_plan_for(
            [claim("clm_python", "skills", "technical_skill", "Python")],
            job_fit_result(direct=[match("m1", "direct", ["job_python"], ["clm_python"])]),
        )
        result = build_cv_statement_plan(plan)
        self.assertEqual(result["schema_version"], CV_STATEMENT_PLAN_VERSION)
        self.assertEqual(len(result["statements"]), 1)
        statement = result["statements"][0]
        self.assertEqual(statement["statement_text"], "Python")
        self.assertEqual(statement["source_profile_evidence_ids"], ["clm_python"])

    def test_multiple_safe_same_record_facts_compose_with_complete_provenance(self):
        claims = [
            claim("clm_a", "employment", "responsibility_or_achievement", "Managed drilling-fluid operations across 9 rigs", record_id="rec_bh"),
            claim("clm_b", "employment", "responsibility_or_achievement", "Standardized the drilling-fluid programme", record_id="rec_bh"),
            claim("clm_c", "employment", "responsibility_or_achievement", "Reduced NPT", record_id="rec_bh"),
        ]
        plan = content_plan_for(claims)
        plan["role_bullet_plans"] = [{
            "plan_id": "role-bullet-composed",
            "target_section": "professional_experience",
            "target_record_id": "rec_bh",
            "supporting_profile_evidence_ids": ["clm_a", "clm_b", "clm_c"],
            "matched_job_requirement_ids": ["job_hpht"],
        }]
        plan["provenance"]["selected_profile_evidence_ids"] = ["clm_a", "clm_b", "clm_c"]
        for evidence_id in plan["provenance"]["selected_profile_evidence_ids"]:
            candidate(plan, evidence_id)["match_type"] = "direct"
            candidate(plan, evidence_id)["matched_job_requirement_ids"] = ["job_hpht"]
        result = build_cv_statement_plan(plan)
        self.assertEqual(len(result["statements"]), 1)
        statement = result["statements"][0]
        self.assertEqual(statement["composition"]["mode"], "composed")
        self.assertEqual(statement["source_profile_evidence_ids"], ["clm_a", "clm_b", "clm_c"])
        self.assertEqual(statement["source_job_requirement_ids"], ["job_hpht"])
        self.assertEqual([fragment["profile_evidence_id"] for fragment in statement["structured_content"]["fragments"]], ["clm_a", "clm_b", "clm_c"])

    def test_same_record_facts_not_safe_to_combine_emit_separate_statements(self):
        claims = [
            claim("clm_title", "employment", "job_title", "Drilling Engineer", record_id="rec_bh"),
            claim("clm_resp", "employment", "responsibility_or_achievement", "Supported liner-hanger operations", record_id="rec_bh"),
        ]
        plan = content_plan_for(claims)
        plan["role_bullet_plans"] = [{
            "plan_id": "role-bullet-unsafe",
            "target_section": "professional_experience",
            "target_record_id": "rec_bh",
            "supporting_profile_evidence_ids": ["clm_title", "clm_resp"],
        }]
        plan["provenance"]["selected_profile_evidence_ids"] = ["clm_title", "clm_resp"]
        for evidence_id in plan["provenance"]["selected_profile_evidence_ids"]:
            candidate(plan, evidence_id)["match_type"] = "direct"
        result = build_cv_statement_plan(plan)
        self.assertEqual(len(result["statements"]), 2)
        self.assertEqual(result["ungroupable_evidence"][0]["reason"], "UNSAFE_TO_COMBINE")

    def test_cross_record_evidence_is_never_merged(self):
        claims = [
            claim("clm_bh", "employment", "responsibility_or_achievement", "Supported liner-hanger operations", record_id="rec_bh"),
            claim("clm_koc", "employment", "responsibility_or_achievement", "Prepared drilling reports", record_id="rec_koc"),
        ]
        plan = content_plan_for(claims)
        plan["role_bullet_plans"] = [{
            "plan_id": "cross-record",
            "target_section": "professional_experience",
            "target_record_id": "rec_bh",
            "supporting_profile_evidence_ids": ["clm_bh", "clm_koc"],
        }]
        plan["provenance"]["selected_profile_evidence_ids"] = ["clm_bh", "clm_koc"]
        for evidence_id in plan["provenance"]["selected_profile_evidence_ids"]:
            candidate(plan, evidence_id)["match_type"] = "direct"
        result = build_cv_statement_plan(plan)
        self.assertEqual(result["ungroupable_evidence"][0]["reason"], "CROSS_RECORD_GROUPING_FORBIDDEN")
        self.assertEqual(len(result["statements"]), 2)
        self.assertNotIn(";", result["statements"][0]["statement_text"])

    def test_unsupported_strengthening_is_not_introduced(self):
        plan = content_plan_for(
            [claim("clm_support", "employment", "responsibility_or_achievement", "Supported liner-hanger operations", record_id="rec_bh")],
            job_fit_result(direct=[match("m1", "direct", ["job_hpht"], ["clm_support"])]),
        )
        text = build_cv_statement_plan(plan)["statements"][0]["statement_text"]
        self.assertEqual(text, "Supported liner-hanger operations")
        self.assertNotIn("Led", text)

    def test_ownership_or_leadership_is_not_inferred_from_weaker_wording(self):
        plan = content_plan_for(
            [claim("clm_rigs", "employment", "responsibility_or_achievement", "Worked across 9 rigs", record_id="rec_bh")],
            job_fit_result(direct=[match("m1", "direct", ["job_hpht"], ["clm_rigs"])]),
        )
        text = build_cv_statement_plan(plan)["statements"][0]["statement_text"]
        self.assertEqual(text, "Worked across 9 rigs")
        self.assertNotIn("Managed", text)

    def test_numeric_facts_remain_exactly_grounded(self):
        plan = content_plan_for(
            [claim("clm_npt", "employment", "responsibility_or_achievement", "Helped reduce NPT", record_id="rec_bh")],
            job_fit_result(direct=[match("m1", "direct", ["job_hpht"], ["clm_npt"])]),
        )
        text = build_cv_statement_plan(plan)["statements"][0]["statement_text"]
        self.assertEqual(text, "Helped reduce NPT")
        self.assertNotIn("%", text)

    def test_planned_but_ungroupable_evidence_is_explicitly_represented(self):
        claims = [
            claim("clm_a", "employment", "job_title", "Engineer", record_id="rec_a"),
            claim("clm_b", "employment", "responsibility_or_achievement", "Delivered reports", record_id="rec_a"),
        ]
        plan = content_plan_for(claims)
        plan["role_bullet_plans"] = [{
            "plan_id": "unsafe",
            "target_section": "professional_experience",
            "target_record_id": "rec_a",
            "supporting_profile_evidence_ids": ["clm_a", "clm_b"],
        }]
        plan["provenance"]["selected_profile_evidence_ids"] = ["clm_a", "clm_b"]
        for evidence_id in plan["provenance"]["selected_profile_evidence_ids"]:
            candidate(plan, evidence_id)["match_type"] = "direct"
        result = build_cv_statement_plan(plan)
        self.assertEqual(result["ungroupable_evidence"][0]["profile_evidence_ids"], ["clm_a", "clm_b"])
        self.assertTrue(result["ungroupable_evidence"][0]["emitted_as_separate_statements"])

    def test_planned_unknown_evidence_is_explicitly_omitted(self):
        plan = content_plan_for(
            [claim("clm_python", "skills", "technical_skill", "Python")],
            job_fit_result(direct=[match("m1", "direct", ["job_python"], ["clm_python"])]),
        )
        plan["skills_to_surface"].append({
            "profile_evidence_id": "clm_missing",
            "category": "skills",
            "field": "technical_skill",
            "value": "Missing",
            "matched_job_requirement_ids": ["job_python"],
        })
        plan["provenance"]["selected_profile_evidence_ids"].append("clm_missing")
        result = build_cv_statement_plan(plan)
        self.assertIn(
            {"profile_evidence_id": "clm_missing", "reason": "INSUFFICIENT_STATEMENT_CONTEXT", "source_plan_type": "skill", "source_plan_id": "clm_missing"},
            result["omitted_evidence"],
        )

    def test_no_selected_planned_evidence_silently_disappears(self):
        claims = [
            claim("clm_python", "skills", "technical_skill", "Python"),
            claim("clm_auth", "eligibility", "work_authorization", "Right to work in the UK"),
        ]
        fit = job_fit_result(
            direct=[match("m1", "direct", ["job_python"], ["clm_python"])],
            gates=[{"gate_id": "eligibility", "status": "PASS", "job_evidence_ids": ["job_auth"], "profile_evidence_ids": ["clm_auth"]}],
        )
        plan = content_plan_for(claims, fit)
        result = build_cv_statement_plan(plan)
        represented = set(result["provenance"]["statement_profile_evidence_ids"]) | set(result["provenance"]["omitted_profile_evidence_ids"])
        self.assertEqual(represented, set(plan["provenance"]["selected_profile_evidence_ids"]))

    def test_gate_only_evidence_does_not_leak_into_substantive_statements(self):
        plan = content_plan_for(
            [claim("clm_auth", "eligibility", "work_authorization", "Right to work in the UK")],
            job_fit_result(gates=[{"gate_id": "eligibility", "status": "PASS", "job_evidence_ids": ["job_auth"], "profile_evidence_ids": ["clm_auth"]}]),
        )
        result = build_cv_statement_plan(plan)
        self.assertFalse(result["statements"])
        self.assertEqual(result["omitted_evidence"][0]["reason"], "GATE_ONLY_NOT_SUBSTANTIVE")

    def test_mixed_substantive_and_gate_evidence_follows_task1_classification(self):
        fit = job_fit_result(
            direct=[match("m1", "direct", ["job_python"], ["clm_python"])],
            gates=[{"gate_id": "technical_gate", "status": "PASS", "job_evidence_ids": ["job_python"], "profile_evidence_ids": ["clm_python"]}],
        )
        plan = content_plan_for([claim("clm_python", "skills", "technical_skill", "Python")], fit)
        result = build_cv_statement_plan(plan)
        self.assertEqual(len(result["statements"]), 1)
        self.assertEqual(result["statements"][0]["source_profile_evidence_ids"], ["clm_python"])

    def test_identical_input_produces_identical_output_and_ordering(self):
        plan = content_plan_for(
            [claim("clm_python", "skills", "technical_skill", "Python")],
            job_fit_result(direct=[match("m1", "direct", ["job_python"], ["clm_python"])]),
        )
        self.assertEqual(build_cv_statement_plan(plan), build_cv_statement_plan(copy.deepcopy(plan)))

    def test_stable_statement_id_across_repeated_runs(self):
        plan = content_plan_for(
            [claim("clm_python", "skills", "technical_skill", "Python")],
            job_fit_result(direct=[match("m1", "direct", ["job_python"], ["clm_python"])]),
        )
        first = build_cv_statement_plan(plan)["statements"][0]["statement_id"]
        second = build_cv_statement_plan(copy.deepcopy(plan))["statements"][0]["statement_id"]
        self.assertEqual(first, second)

    def test_uncovered_task1_requirements_remain_uncovered(self):
        fit = job_fit_result(
            direct=[match("m1", "direct", ["job_python"], ["clm_python"])],
            gaps=[{"gap_id": "gap_german", "job_requirement_ids": ["job_uncovered"], "gap_type": "unsupported"}],
        )
        plan = content_plan_for([claim("clm_python", "skills", "technical_skill", "Python")], fit)
        result = build_cv_statement_plan(plan)
        self.assertIn("job_uncovered", {item["job_requirement_id"] for item in result["uncovered_requirements"]})
        self.assertNotIn("job_uncovered", result["provenance"]["source_selected_job_requirement_ids"])


if __name__ == "__main__":
    unittest.main()
