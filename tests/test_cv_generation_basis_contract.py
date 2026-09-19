"""Focused tests for the pure cv-generation-basis.v1 contract (Phase 2B-3).

No DB, no artifact lookup, no Task 1/2/3 execution -- validates an
already-assembled payload only.
"""
from __future__ import annotations

import copy
import unittest

from product.cv_generation_basis_contract import (
    CV_GENERATION_BASIS_VERSION,
    CvGenerationBasisContractError,
    validate_cv_generation_basis,
)


def _ref(artifact_type: str, suffix: str) -> dict:
    return {
        "artifact_id": f"art_{artifact_type}_{suffix}",
        "artifact_type": artifact_type,
        "content_id": f"{artifact_type}_{suffix}",
    }


def _model(selected_statement_ids: list[str]) -> dict:
    return {
        "schema_version": "cv-document-model.v2",
        "sections": [],
        "provenance": {
            "selected_statement_ids": selected_statement_ids,
            "suppressed_statement_ids": [],
        },
    }


def _basis(*, authorized=("stmt_a", "stmt_b"), omitted=(), model_selected=None, statement_plan_artifact_id="art_cv_statement_plan_A") -> dict:
    model_selected = list(authorized) if model_selected is None else model_selected
    return {
        "schema_version": CV_GENERATION_BASIS_VERSION,
        "source_artifacts": {
            "profile_snapshot": _ref("profile_snapshot", "A"),
            "job_fit_result": _ref("job_fit_result", "A"),
            "resolved_job_evidence": _ref("resolved_job_evidence", "A"),
            "cv_content_plan": _ref("cv_content_plan", "A"),
            "cv_statement_plan": {
                "artifact_id": statement_plan_artifact_id,
                "artifact_type": "cv_statement_plan",
                "content_id": "cv_statement_plan_A",
            },
        },
        "review": {
            "statement_plan_artifact_id": statement_plan_artifact_id,
            "consulted_decision_ids": ["rev_1", "rev_2"],
            "authorized_statement_ids": list(authorized),
            "omitted_statement_ids": list(omitted),
        },
        "cv_document_model": _model(model_selected),
    }


class TestValidBasis(unittest.TestCase):
    def test_valid_basis_passes(self):
        basis = _basis()
        result = validate_cv_generation_basis(basis)
        self.assertEqual(result, basis)

    def test_empty_authorized_and_empty_model_is_valid(self):
        basis = _basis(authorized=(), omitted=("stmt_a", "stmt_b"), model_selected=[])
        validate_cv_generation_basis(basis)


class TestRejections(unittest.TestCase):
    def test_unknown_schema_version_is_rejected(self):
        basis = _basis()
        basis["schema_version"] = "cv-generation-basis.v99"
        with self.assertRaises(CvGenerationBasisContractError):
            validate_cv_generation_basis(basis)

    def test_missing_schema_version_is_rejected(self):
        basis = _basis()
        del basis["schema_version"]
        with self.assertRaises(CvGenerationBasisContractError):
            validate_cv_generation_basis(basis)

    def test_non_dict_input_is_rejected(self):
        with self.assertRaises(CvGenerationBasisContractError):
            validate_cv_generation_basis(["not", "a", "dict"])

    def test_missing_source_artifact_ref_is_rejected(self):
        basis = _basis()
        del basis["source_artifacts"]["resolved_job_evidence"]
        with self.assertRaises(CvGenerationBasisContractError):
            validate_cv_generation_basis(basis)

    def test_source_artifact_ref_wrong_type_is_rejected(self):
        basis = _basis()
        basis["source_artifacts"]["job_fit_result"]["artifact_type"] = "profile_snapshot"
        with self.assertRaises(CvGenerationBasisContractError):
            validate_cv_generation_basis(basis)

    def test_source_artifact_ref_empty_content_id_is_rejected(self):
        basis = _basis()
        basis["source_artifacts"]["cv_content_plan"]["content_id"] = ""
        with self.assertRaises(CvGenerationBasisContractError):
            validate_cv_generation_basis(basis)

    def test_authorized_omitted_overlap_is_rejected(self):
        basis = _basis(authorized=("stmt_a", "stmt_b"), omitted=("stmt_b",), model_selected=["stmt_a"])
        with self.assertRaises(CvGenerationBasisContractError):
            validate_cv_generation_basis(basis)

    def test_model_selected_id_outside_authorized_is_rejected(self):
        basis = _basis(authorized=("stmt_a",), omitted=(), model_selected=["stmt_a", "stmt_unauthorized"])
        with self.assertRaises(CvGenerationBasisContractError):
            validate_cv_generation_basis(basis)

    def test_wrong_embedded_model_schema_version_is_rejected(self):
        basis = _basis()
        basis["cv_document_model"]["schema_version"] = "cv-document-model.v1"
        with self.assertRaises(CvGenerationBasisContractError):
            validate_cv_generation_basis(basis)

    def test_statement_plan_artifact_id_mismatch_is_rejected(self):
        basis = _basis()
        basis["review"]["statement_plan_artifact_id"] = "art_cv_statement_plan_DIFFERENT"
        with self.assertRaises(CvGenerationBasisContractError):
            validate_cv_generation_basis(basis)

    def test_duplicate_consulted_decision_ids_are_rejected(self):
        basis = _basis()
        basis["review"]["consulted_decision_ids"] = ["rev_1", "rev_1"]
        with self.assertRaises(CvGenerationBasisContractError):
            validate_cv_generation_basis(basis)

    def test_malformed_review_block_is_rejected(self):
        basis = _basis()
        basis["review"] = {"statement_plan_artifact_id": "art_x"}
        with self.assertRaises(CvGenerationBasisContractError):
            validate_cv_generation_basis(basis)

    def test_unexpected_top_level_key_is_rejected(self):
        basis = _basis()
        basis["extra_field"] = "should not be here"
        with self.assertRaises(CvGenerationBasisContractError):
            validate_cv_generation_basis(basis)


if __name__ == "__main__":
    unittest.main()
