"""Focused tests for the pure CV Generation Quality v2 review-authorization
projection (Phase 2B-2). No DB, no artifact lookups, no review decision
resolution -- callers supply the already-effective decisions."""

from __future__ import annotations

import copy
import unittest

from product.cv_document_model import build_cv_document_model
from product.cv_review_projection import (
    AUTHORIZED_DISPOSITION,
    CvReviewProjectionError,
    project_review_authorized_statement_plan,
    reviewable_cv_statements,
)


def statement(statement_id: str, target_section: str, text: str, *, record_id: str | None = None) -> dict:
    return {
        "statement_id": statement_id,
        "target_section": target_section,
        "target_record_id": record_id,
        "source_profile_evidence_ids": [f"clm_{statement_id}"],
        "source_job_requirement_ids": [],
        "statement_text": text,
        "provenance": {"profile_evidence_ids": [f"clm_{statement_id}"], "job_requirement_ids": []},
    }


def statement_plan(statements: list[dict], *, gaps: list[dict] | None = None) -> dict:
    return {
        "schema_version": "cv-statement-plan.v0",
        "statements": copy.deepcopy(statements),
        "omitted_evidence": [],
        "ungroupable_evidence": [],
        "uncovered_requirements": gaps or [],
        "provenance": {"statement_profile_evidence_ids": [], "source_selected_job_requirement_ids": []},
    }


def decision(
    statement_id: str, disposition: str, *, decision_id: str | None = None, source_artifact_id: str = "art_1",
) -> dict:
    return {
        "id": decision_id or f"rev_{statement_id}",
        "review_item_type": "cv_statement_v2",
        "source_artifact_id": source_artifact_id,
        "domain_item_id": statement_id,
        "disposition": disposition,
    }


class TestReviewableCvStatements(unittest.TestCase):
    def test_every_statement_in_the_plan_is_reviewable(self):
        plan = statement_plan([
            statement("stmt_a", "professional_summary", "A"),
            statement("stmt_b", "skills", "B"),
        ])
        reviewable = reviewable_cv_statements(plan)
        self.assertEqual([s["statement_id"] for s in reviewable], ["stmt_a", "stmt_b"])

    def test_diagnostic_collections_are_not_reviewable_statements(self):
        plan = statement_plan([statement("stmt_a", "professional_summary", "A")])
        plan["omitted_evidence"] = [{"profile_evidence_id": "clm_x", "reason": "budget"}]
        plan["ungroupable_evidence"] = [{"group_id": "g1"}]
        plan["uncovered_requirements"] = [{"job_requirement_id": "job_x"}]
        reviewable = reviewable_cv_statements(plan)
        self.assertEqual([s["statement_id"] for s in reviewable], ["stmt_a"])


class TestProjection(unittest.TestCase):
    def test_all_acknowledged_projects_all_reviewable_statements(self):
        plan = statement_plan([
            statement("stmt_a", "professional_summary", "A"),
            statement("stmt_b", "skills", "B"),
        ])
        decisions = {
            "stmt_a": decision("stmt_a", AUTHORIZED_DISPOSITION),
            "stmt_b": decision("stmt_b", AUTHORIZED_DISPOSITION),
        }
        projected = project_review_authorized_statement_plan(plan, decisions, source_artifact_id="art_1")
        self.assertEqual([s["statement_id"] for s in projected["statements"]], ["stmt_a", "stmt_b"])

    def test_acknowledged_and_omitted_projects_only_acknowledged(self):
        plan = statement_plan([
            statement("stmt_a", "professional_summary", "A"),
            statement("stmt_b", "skills", "B"),
        ])
        decisions = {
            "stmt_a": decision("stmt_a", AUTHORIZED_DISPOSITION),
            "stmt_b": decision("stmt_b", "omit_from_positioning"),
        }
        projected = project_review_authorized_statement_plan(plan, decisions, source_artifact_id="art_1")
        self.assertEqual([s["statement_id"] for s in projected["statements"]], ["stmt_a"])

    def test_pending_statement_fails_closed(self):
        plan = statement_plan([
            statement("stmt_a", "professional_summary", "A"),
            statement("stmt_b", "skills", "B"),
        ])
        decisions = {"stmt_a": decision("stmt_a", AUTHORIZED_DISPOSITION)}
        with self.assertRaises(CvReviewProjectionError):
            project_review_authorized_statement_plan(plan, decisions, source_artifact_id="art_1")

    def test_no_decisions_at_all_fails_closed(self):
        plan = statement_plan([statement("stmt_a", "professional_summary", "A")])
        with self.assertRaises(CvReviewProjectionError):
            project_review_authorized_statement_plan(plan, {}, source_artifact_id="art_1")

    def test_original_statement_order_is_preserved(self):
        plan = statement_plan([
            statement("stmt_c", "skills", "C"),
            statement("stmt_a", "professional_summary", "A"),
            statement("stmt_b", "skills", "B"),
        ])
        decisions = {
            sid: decision(sid, AUTHORIZED_DISPOSITION) for sid in ("stmt_c", "stmt_a", "stmt_b")
        }
        projected = project_review_authorized_statement_plan(plan, decisions, source_artifact_id="art_1")
        self.assertEqual(
            [s["statement_id"] for s in projected["statements"]], ["stmt_c", "stmt_a", "stmt_b"]
        )

    def test_statement_text_is_never_rewritten(self):
        exact_text = "Managed R&D operations — reducing NPT by 15%."
        plan = statement_plan([statement("stmt_a", "professional_summary", exact_text)])
        decisions = {"stmt_a": decision("stmt_a", AUTHORIZED_DISPOSITION)}
        projected = project_review_authorized_statement_plan(plan, decisions, source_artifact_id="art_1")
        self.assertEqual(projected["statements"][0]["statement_text"], exact_text)

    def test_source_plan_is_not_mutated(self):
        plan = statement_plan([statement("stmt_a", "professional_summary", "A")])
        before = copy.deepcopy(plan)
        decisions = {"stmt_a": decision("stmt_a", AUTHORIZED_DISPOSITION)}
        project_review_authorized_statement_plan(plan, decisions, source_artifact_id="art_1")
        self.assertEqual(plan, before)

    def test_no_ids_are_reassigned_and_no_statements_invented(self):
        plan = statement_plan([statement("stmt_a", "professional_summary", "A")])
        decisions = {"stmt_a": decision("stmt_a", AUTHORIZED_DISPOSITION)}
        projected = project_review_authorized_statement_plan(plan, decisions, source_artifact_id="art_1")
        self.assertEqual(len(projected["statements"]), 1)
        self.assertEqual(projected["statements"][0]["statement_id"], "stmt_a")

    def test_decision_belonging_to_a_different_source_artifact_is_rejected(self):
        """source_artifact_id is not decorative: a decision map assembled
        (e.g. by caller error) with a decision from a different artifact must
        be rejected here rather than silently trusted."""
        plan = statement_plan([statement("stmt_a", "professional_summary", "A")])
        decisions = {
            "stmt_a": decision("stmt_a", AUTHORIZED_DISPOSITION, source_artifact_id="art_OTHER"),
        }
        with self.assertRaises(CvReviewProjectionError):
            project_review_authorized_statement_plan(plan, decisions, source_artifact_id="art_1")

    def test_repeated_calls_return_identical_logical_results(self):
        plan = statement_plan([statement("stmt_a", "professional_summary", "A")])
        decisions = {"stmt_a": decision("stmt_a", AUTHORIZED_DISPOSITION)}
        first = project_review_authorized_statement_plan(plan, decisions, source_artifact_id="art_1")
        second = project_review_authorized_statement_plan(plan, decisions, source_artifact_id="art_1")
        self.assertEqual(first, second)


class TestTask3AcceptsTheProjectionUnchanged(unittest.TestCase):
    """Test-only: proves contract compatibility with Task 3's existing API.
    Production Slice 2B-2 code never calls Task 3."""

    def test_projected_plan_is_accepted_by_build_cv_document_model_unchanged(self):
        plan = statement_plan([
            statement("stmt_a", "professional_summary", "Delivered Python systems."),
            statement("stmt_b", "skills", "Python"),
            statement("stmt_c", "professional_experience", "Built pipelines.", record_id="rec_a"),
        ])
        decisions = {
            "stmt_a": decision("stmt_a", AUTHORIZED_DISPOSITION),
            "stmt_b": decision("stmt_b", "omit_from_positioning"),
            "stmt_c": decision("stmt_c", AUTHORIZED_DISPOSITION),
        }
        projected = project_review_authorized_statement_plan(plan, decisions, source_artifact_id="art_1")

        model = build_cv_document_model(projected)

        section_ids = [section["section_id"] for section in model["sections"]]
        self.assertIn("professional_summary", section_ids)
        self.assertIn("professional_experience", section_ids)
        self.assertNotIn("skills", section_ids)


if __name__ == "__main__":
    unittest.main()
