"""Focused tests for CV Generation Quality v2 Task 3 document planning."""

import copy
import unittest

from product.cv_document_model import CV_DOCUMENT_MODEL_VERSION, build_cv_document_model


def statement(
    statement_id: str,
    target_section: str,
    text: str,
    *,
    record_id: str | None = None,
    evidence_ids: list[str] | None = None,
    requirement_ids: list[str] | None = None,
) -> dict:
    evidence_ids = evidence_ids or [f"clm_{statement_id}"]
    requirement_ids = requirement_ids or []
    return {
        "statement_id": statement_id,
        "target_section": target_section,
        "target_record_id": record_id,
        "source_profile_evidence_ids": evidence_ids,
        "source_job_requirement_ids": requirement_ids,
        "statement_text": text,
        "structured_content": {
            "fragments": [
                {
                    "profile_evidence_id": evidence_id,
                    "record_id": record_id,
                    "text": text,
                }
                for evidence_id in evidence_ids
            ]
        },
        "provenance": {
            "profile_evidence_ids": evidence_ids,
            "job_requirement_ids": requirement_ids,
        },
    }


def statement_plan(statements: list[dict], *, gaps: list[dict] | None = None) -> dict:
    return {
        "schema_version": "cv-statement-plan.v0",
        "statements": copy.deepcopy(statements),
        "omitted_evidence": [],
        "ungroupable_evidence": [],
        "uncovered_requirements": gaps or [],
        "provenance": {
            "statement_profile_evidence_ids": sorted({
                evidence_id
                for item in statements
                for evidence_id in item.get("source_profile_evidence_ids", [])
            }),
            "source_selected_job_requirement_ids": sorted({
                requirement_id
                for item in statements
                for requirement_id in item.get("source_job_requirement_ids", [])
            }),
        },
    }


def section(model: dict, section_id: str) -> dict:
    return next(item for item in model["sections"] if item["section_id"] == section_id)


class TestCvDocumentModel(unittest.TestCase):
    def test_statement_plan_allocates_to_ordered_renderer_independent_sections(self):
        plan = statement_plan([
            statement("stmt_summary", "professional_summary", "Python delivery", requirement_ids=["job_python"]),
            statement("stmt_skill", "skills", "Python"),
            statement("stmt_role", "professional_experience", "Built Python pipelines", record_id="rec_a"),
            statement("stmt_qual", "qualifications", "MSc Engineering"),
        ])
        model = build_cv_document_model(plan)
        self.assertEqual(model["schema_version"], CV_DOCUMENT_MODEL_VERSION)
        self.assertEqual([item["section_id"] for item in model["sections"]], [
            "professional_summary", "skills", "professional_experience", "qualifications"
        ])
        self.assertEqual(section(model, "professional_summary")["items"][0]["statement_text"], "Python delivery")

    def test_role_statements_are_integrated_under_their_evidenced_record(self):
        plan = statement_plan([
            statement("stmt_b", "professional_experience", "Delivered B", record_id="rec_b"),
            statement("stmt_a", "professional_experience", "Delivered A", record_id="rec_a"),
        ])
        roles = section(build_cv_document_model(plan), "professional_experience")["roles"]
        self.assertEqual([role["record_id"] for role in roles], ["rec_a", "rec_b"])
        self.assertEqual(roles[0]["statements"][0]["target_record_id"], "rec_a")

    def test_statement_cannot_migrate_to_an_unsupported_role(self):
        plan = statement_plan([
            statement("stmt_missing_role", "professional_experience", "Built pipelines", record_id=None),
        ])
        model = build_cv_document_model(plan)
        self.assertFalse(any(item["section_id"] == "professional_experience" for item in model["sections"]))
        self.assertEqual(model["suppressed_statements"][0]["status"], "UNPLACEABLE_MISSING_RECORD")

    def test_role_budget_is_explicit_and_budget_omissions_are_inspectable(self):
        plan = statement_plan([
            statement("stmt_1", "professional_experience", "One", record_id="rec_a"),
            statement("stmt_2", "professional_experience", "Two", record_id="rec_a"),
            statement("stmt_3", "professional_experience", "Three", record_id="rec_a"),
        ])
        model = build_cv_document_model(plan, budgets={"max_statements_per_role": 2})
        role = section(model, "professional_experience")["roles"][0]
        self.assertEqual(role["budget"]["candidate_statement_count"], 3)
        self.assertEqual(role["budget"]["selected_statement_count"], 2)
        self.assertEqual(role["budget"]["omitted_statement_count"], 1)
        self.assertEqual(model["suppressed_statements"][0]["status"], "OMITTED_BUDGET")

    def test_section_budget_is_explicit_and_enforced(self):
        plan = statement_plan([
            statement("stmt_s1", "skills", "Python"),
            statement("stmt_s2", "skills", "SQL"),
        ])
        model = build_cv_document_model(plan, budgets={"max_skill_statements": 1})
        skills = section(model, "skills")
        self.assertEqual(skills["budget"]["max_statements"], 1)
        self.assertEqual(len(skills["items"]), 1)
        self.assertEqual(model["suppressed_statements"][0]["status"], "OMITTED_BUDGET")

    def test_source_statement_and_evidence_provenance_survives(self):
        plan = statement_plan([
            statement("stmt_role", "professional_experience", "Built Python pipelines", record_id="rec_a", evidence_ids=["clm_a"], requirement_ids=["job_python"]),
        ])
        model = build_cv_document_model(plan)
        item = section(model, "professional_experience")["roles"][0]["statements"][0]
        self.assertEqual(item["statement_id"], "stmt_role")
        self.assertEqual(item["source_profile_evidence_ids"], ["clm_a"])
        self.assertEqual(item["source_job_requirement_ids"], ["job_python"])
        self.assertIn("stmt_role", model["provenance"]["selected_statement_ids"])

    def test_uncovered_requirements_remain_visible_as_gaps(self):
        plan = statement_plan(
            [statement("stmt_skill", "skills", "Python", requirement_ids=["job_python"])],
            gaps=[{"job_requirement_id": "job_german", "text": "Requires German", "kind": "required", "source": "resolved_job_evidence", "gap": None}],
        )
        gaps = section(build_cv_document_model(plan), "evidence_gaps")["items"]
        self.assertEqual(gaps[0]["status"], "GAP")
        self.assertEqual(gaps[0]["job_requirement_id"], "job_german")

    def test_unsupported_section_is_explicitly_unplaceable(self):
        plan = statement_plan([
            statement("stmt_unknown", "unsupported_section", "Something"),
        ])
        model = build_cv_document_model(plan)
        self.assertEqual(model["suppressed_statements"][0]["status"], "UNPLACEABLE_UNSUPPORTED_SECTION")

    def test_no_statement_silently_disappears(self):
        plan = statement_plan([
            statement("stmt_summary", "professional_summary", "Summary"),
            statement("stmt_unplaceable", "professional_experience", "Missing role", record_id=None),
        ])
        model = build_cv_document_model(plan)
        represented = set(model["provenance"]["selected_statement_ids"]) | set(model["provenance"]["suppressed_statement_ids"])
        self.assertEqual(represented, set(model["provenance"]["source_statement_ids"]))

    def test_identical_input_produces_identical_output_and_document_id(self):
        plan = statement_plan([
            statement("stmt_skill", "skills", "Python"),
            statement("stmt_role", "professional_experience", "Built pipelines", record_id="rec_a"),
        ])
        first = build_cv_document_model(plan)
        second = build_cv_document_model(copy.deepcopy(plan))
        self.assertEqual(first, second)
        self.assertEqual(first["document_id"], second["document_id"])

    def test_final_page_or_renderer_metadata_is_absent(self):
        model = build_cv_document_model(statement_plan([
            statement("stmt_skill", "skills", "Python"),
        ]))
        serialized = str(model).lower()
        self.assertNotIn("font", serialized)
        self.assertNotIn("margin", serialized)
        self.assertNotIn("page", serialized)
        self.assertNotIn("docx", serialized)


if __name__ == "__main__":
    unittest.main()
