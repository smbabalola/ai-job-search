"""Phase 2B-4: CV Quality v2 integration with application document generation.

Proves the explicit activation rule: legacy generation (no basis ID) is
completely unchanged; supplying an exact cv_generation_basis_artifact_id
activates CV-v2 mode, where the CV comes exclusively from that pinned
basis's cv_document_model rendered once through the frozen Task 4 API, the
cover letter still comes from the exact existing reviewed Application Pack
path, and both flow through the existing DocumentBlobStore/selection/
application-pack.v2 pipeline unchanged.
"""
from __future__ import annotations

import copy
import hashlib

import pytest

from product.cv_document_renderer import CV_DOCUMENT_RENDERER_VERSION, render_cv_document
from tests.webapp.services.test_application_pack import _seed_completion_ready, _workspace
from webapp.persistence.artifacts import get_artifact, get_current_artifact, save_artifact
from webapp.persistence.workspaces import PROFILE_WORKSPACE_ID
from webapp.services.application_documents import (
    APPLICATION_DOCUMENT_GENERATION_V2,
    generate_application_documents,
    select_application_document,
)
from webapp.services.application_pack import confirm_application_pack
from webapp.services.document_blob_store import DocumentBlobStore
from webapp.services.pipeline import PipelineError


def _document_model(selected_statement_ids: list[str], *, summary_text: str = "Delivered Python systems.") -> dict:
    items = [
        {
            "status": "SELECTED", "section_id": "professional_summary", "statement_id": sid,
            "target_record_id": None, "statement_text": summary_text,
            "source_profile_evidence_ids": [], "source_job_requirement_ids": [], "provenance": {},
            "structural_decision": {"reason": "matches_statement_target_section", "source_target_section": "professional_summary"},
        }
        for sid in selected_statement_ids
    ]
    sections = []
    if items:
        sections.append({
            "section_id": "professional_summary", "title": "Professional Summary", "order": 0,
            "budget": {"max_statements": 3, "candidate_statement_count": len(items), "selected_statement_count": len(items)},
            "items": items,
        })
    return {
        "schema_version": "cv-document-model.v2",
        "source_statement_plan_version": "cv-statement-plan.v0",
        "strategy": "evidence_first",
        "budgets": {},
        "sections": sections,
        "suppressed_statements": [],
        "source_omitted_evidence": [],
        "source_ungroupable_evidence": [],
        "provenance": {
            "source_statement_ids": selected_statement_ids,
            "selected_statement_ids": selected_statement_ids,
            "suppressed_statement_ids": [],
            "source_profile_evidence_ids": [],
            "source_job_requirement_ids": [],
        },
        "decision_metadata": {
            "section_order": ["professional_summary", "skills", "professional_experience", "qualifications", "evidence_gaps"],
            "ordering_rule": "target_section_then_record_then_statement_id",
            "omission_policy": "selected_or_explicitly_suppressed",
        },
        "document_id": "cvdoc_test0000000000000000000000",
    }


def _ref(artifact):
    return {"artifact_id": artifact["id"], "artifact_type": artifact["artifact_type"], "content_id": artifact["content_id"]}


def _seed_cv_generation_basis(conn, workspace_id, *, selected_statement_ids=("stmt_a",), suffix="A"):
    """Build a minimal, contract-valid cv_generation_basis artifact directly.

    Reuses the workspace's ACTUAL current profile_snapshot/job_fit_result/
    resolved_job_evidence (as set up by _seed_completion_ready) rather than
    writing new ones -- writing new artifacts here would shadow the ones
    build_application_pack()'s own staleness checks expect to still be
    current, since the CV-v2 branch also rebuilds the cover-letter basis via
    the exact same legacy path.
    """

    profile_artifact = get_current_artifact(conn, PROFILE_WORKSPACE_ID, "profile_snapshot")
    job_fit_artifact = get_current_artifact(conn, workspace_id, "job_fit_result")
    resolved_job_evidence_artifact = get_current_artifact(conn, workspace_id, "resolved_job_evidence")
    content_plan_artifact = save_artifact(
        conn, workspace_id=workspace_id, artifact_type="cv_content_plan",
        payload={
            "schema_version": "cv-content-plan-artifact.v1",
            "source_artifacts": {
                "profile_snapshot": _ref(profile_artifact),
                "job_fit_result": _ref(job_fit_artifact),
                "resolved_job_evidence": _ref(resolved_job_evidence_artifact),
            },
            "content_plan": {"schema_version": "cv-content-plan.v0"},
        },
        content_id=f"cvcontentplan_basis_{suffix}",
    )
    statement_plan_artifact = save_artifact(
        conn, workspace_id=workspace_id, artifact_type="cv_statement_plan",
        payload={
            "schema_version": "cv-statement-plan-artifact.v1",
            "source_artifacts": {"cv_content_plan": _ref(content_plan_artifact)},
            "statement_plan": {"schema_version": "cv-statement-plan.v0"},
        },
        content_id=f"cvstatementplan_basis_{suffix}",
    )
    basis_payload = {
        "schema_version": "cv-generation-basis.v1",
        "source_artifacts": {
            "profile_snapshot": _ref(profile_artifact),
            "job_fit_result": _ref(job_fit_artifact),
            "resolved_job_evidence": _ref(resolved_job_evidence_artifact),
            "cv_content_plan": _ref(content_plan_artifact),
            "cv_statement_plan": _ref(statement_plan_artifact),
        },
        "review": {
            "statement_plan_artifact_id": statement_plan_artifact["id"],
            "consulted_decision_ids": [f"rev_{suffix}"],
            "authorized_statement_ids": list(selected_statement_ids),
            "omitted_statement_ids": [],
        },
        "cv_document_model": _document_model(list(selected_statement_ids)),
    }
    basis_artifact = save_artifact(
        conn, workspace_id=workspace_id, artifact_type="cv_generation_basis",
        payload=basis_payload, content_id=f"cvgenbasis_{suffix}",
    )
    return basis_artifact


class TestActivation:
    def test_legacy_generation_unchanged_without_basis_id(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_completion_ready(conn, workspace_id)

        result = generate_application_documents(
            conn, workspace_id, documents_root=tmp_path / "docs", extensions_dir=tmp_path / "extensions",
            account_id="account_local",
        )

        assert result["generation_artifact"]["payload"]["schema_version"] == "application-document-generation.v1"

    def test_explicit_basis_id_activates_cv_v2(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_completion_ready(conn, workspace_id)
        basis = _seed_cv_generation_basis(conn, workspace_id)

        result = generate_application_documents(
            conn, workspace_id, documents_root=tmp_path / "docs", extensions_dir=tmp_path / "extensions",
            account_id="account_local", cv_generation_basis_artifact_id=basis["id"],
        )

        payload = result["generation_artifact"]["payload"]
        assert payload["schema_version"] == APPLICATION_DOCUMENT_GENERATION_V2
        assert payload["cv_generation_basis"]["artifact_id"] == basis["id"]
        assert payload["renderers"]["cv"] == CV_DOCUMENT_RENDERER_VERSION


class TestCvV2Fields:
    def test_exact_reviewed_pack_and_basis_pinned(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_completion_ready(conn, workspace_id)
        basis = _seed_cv_generation_basis(conn, workspace_id)

        result = generate_application_documents(
            conn, workspace_id, documents_root=tmp_path / "docs", extensions_dir=tmp_path / "extensions",
            account_id="account_local", cv_generation_basis_artifact_id=basis["id"],
        )
        payload = result["generation_artifact"]["payload"]

        assert payload["reviewed_application_pack"]["schema_version"] in ("application-pack.v0", "application-pack.v1")
        assert payload["cv_generation_basis"] == {
            "artifact_id": basis["id"], "artifact_type": "cv_generation_basis", "content_id": basis["content_id"],
        }
        assert "cover_letter" in payload["renderers"]


class TestByteIdentity:
    def test_stored_cv_bytes_exactly_equal_direct_task4_output(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_completion_ready(conn, workspace_id)
        basis = _seed_cv_generation_basis(conn, workspace_id)
        documents_root = tmp_path / "docs"

        expected_cv_bytes = render_cv_document(basis["payload"]["cv_document_model"])

        result = generate_application_documents(
            conn, workspace_id, documents_root=documents_root, extensions_dir=tmp_path / "extensions",
            account_id="account_local", cv_generation_basis_artifact_id=basis["id"],
        )
        cv_row = next(row for row in result["documents"] if row["document_kind"] == "cv")
        stored_bytes = DocumentBlobStore(documents_root).read(cv_row)

        assert stored_bytes == expected_cv_bytes
        assert cv_row["sha256"] == hashlib.sha256(expected_cv_bytes).hexdigest()
        assert cv_row["byte_length"] == len(expected_cv_bytes)


class TestDeterminism:
    def test_repeated_generation_from_same_basis_yields_identical_cv_bytes(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_completion_ready(conn, workspace_id)
        basis = _seed_cv_generation_basis(conn, workspace_id)
        documents_root = tmp_path / "docs"

        first = generate_application_documents(
            conn, workspace_id, documents_root=documents_root, extensions_dir=tmp_path / "extensions",
            account_id="account_local", cv_generation_basis_artifact_id=basis["id"],
        )
        second = generate_application_documents(
            conn, workspace_id, documents_root=documents_root, extensions_dir=tmp_path / "extensions",
            account_id="account_local", cv_generation_basis_artifact_id=basis["id"],
        )

        first_cv = next(row for row in first["documents"] if row["document_kind"] == "cv")
        second_cv = next(row for row in second["documents"] if row["document_kind"] == "cv")
        assert first_cv["sha256"] == second_cv["sha256"]
        store = DocumentBlobStore(documents_root)
        assert store.read(first_cv) == store.read(second_cv)


class TestHistoricalBasisImmutabilityThroughRendering:
    def test_old_basis_still_renders_identically_after_a_newer_basis_exists(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_completion_ready(conn, workspace_id)
        basis_a = _seed_cv_generation_basis(conn, workspace_id, selected_statement_ids=("stmt_a",), suffix="A")
        documents_root = tmp_path / "docs"

        first = generate_application_documents(
            conn, workspace_id, documents_root=documents_root, extensions_dir=tmp_path / "extensions",
            account_id="account_local", cv_generation_basis_artifact_id=basis_a["id"],
        )
        first_cv = next(row for row in first["documents"] if row["document_kind"] == "cv")
        first_bytes = DocumentBlobStore(documents_root).read(first_cv)

        # A newer, unrelated basis now exists (as if Profile/plans/review advanced).
        _seed_cv_generation_basis(conn, workspace_id, selected_statement_ids=("stmt_x",), suffix="B")

        second = generate_application_documents(
            conn, workspace_id, documents_root=documents_root, extensions_dir=tmp_path / "extensions",
            account_id="account_local", cv_generation_basis_artifact_id=basis_a["id"],
        )
        second_cv = next(row for row in second["documents"] if row["document_kind"] == "cv")
        second_bytes = DocumentBlobStore(documents_root).read(second_cv)

        assert first_bytes == second_bytes


class TestWrongOrMalformedBasis:
    def test_nonexistent_basis_fails_closed(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_completion_ready(conn, workspace_id)
        with pytest.raises(PipelineError, match="not found"):
            generate_application_documents(
                conn, workspace_id, documents_root=tmp_path / "docs", extensions_dir=tmp_path / "extensions",
                account_id="account_local", cv_generation_basis_artifact_id="art_nonexistent",
            )

    def test_wrong_artifact_type_fails_closed(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_completion_ready(conn, workspace_id)
        wrong = save_artifact(
            conn, workspace_id=workspace_id, artifact_type="cv_content_plan",
            payload={"schema_version": "cv-content-plan-artifact.v1"}, content_id="cvcontentplan_wrong",
        )
        with pytest.raises(PipelineError, match="not a cv_generation_basis"):
            generate_application_documents(
                conn, workspace_id, documents_root=tmp_path / "docs", extensions_dir=tmp_path / "extensions",
                account_id="account_local", cv_generation_basis_artifact_id=wrong["id"],
            )

    def test_malformed_basis_fails_closed(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_completion_ready(conn, workspace_id)
        malformed = save_artifact(
            conn, workspace_id=workspace_id, artifact_type="cv_generation_basis",
            payload={"schema_version": "cv-generation-basis.v1"}, content_id="cvgenbasis_malformed",
        )
        with pytest.raises(Exception):
            generate_application_documents(
                conn, workspace_id, documents_root=tmp_path / "docs", extensions_dir=tmp_path / "extensions",
                account_id="account_local", cv_generation_basis_artifact_id=malformed["id"],
            )

    def test_no_partial_generation_artifact_after_failure(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_completion_ready(conn, workspace_id)
        with pytest.raises(PipelineError):
            generate_application_documents(
                conn, workspace_id, documents_root=tmp_path / "docs", extensions_dir=tmp_path / "extensions",
                account_id="account_local", cv_generation_basis_artifact_id="art_nonexistent",
            )
        count = conn.execute(
            "SELECT count(*) FROM artifacts WHERE workspace_id=? AND artifact_type='application_document_generation'",
            (workspace_id,),
        ).fetchone()[0]
        assert count == 0


class TestSelectionAndConfirmationCompatibility:
    def test_generated_cv_v2_documents_selectable_and_confirm_as_v2_unchanged(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_completion_ready(conn, workspace_id)
        basis = _seed_cv_generation_basis(conn, workspace_id)
        documents_root = tmp_path / "docs"

        generated = generate_application_documents(
            conn, workspace_id, documents_root=documents_root, extensions_dir=tmp_path / "extensions",
            account_id="account_local", cv_generation_basis_artifact_id=basis["id"],
        )
        revisions = {}
        for row in generated["documents"]:
            selection = select_application_document(
                conn, workspace_id, kind=row["document_kind"], document_version_id=row["id"],
                expected_revision=0, account_id="account_local",
            )
            revisions[row["document_kind"]] = selection["revision"]

        confirmed = confirm_application_pack(
            conn, workspace_id, effective_date="2026-09-19", documents_root=documents_root,
            account_id="account_local", document_selection_revisions=revisions,
        )

        assert confirmed["pack"]["schema_version"] == "application-pack.v2"
        assert {item["document_version_id"] for item in confirmed["pack"]["final_documents"].values()} == {
            row["id"] for row in generated["documents"]
        }

    def test_confirmed_cv_fetch_returns_original_task4_bytes(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_completion_ready(conn, workspace_id)
        basis = _seed_cv_generation_basis(conn, workspace_id)
        documents_root = tmp_path / "docs"

        expected_cv_bytes = render_cv_document(basis["payload"]["cv_document_model"])

        generated = generate_application_documents(
            conn, workspace_id, documents_root=documents_root, extensions_dir=tmp_path / "extensions",
            account_id="account_local", cv_generation_basis_artifact_id=basis["id"],
        )
        revisions = {}
        for row in generated["documents"]:
            selection = select_application_document(
                conn, workspace_id, kind=row["document_kind"], document_version_id=row["id"],
                expected_revision=0, account_id="account_local",
            )
            revisions[row["document_kind"]] = selection["revision"]
        confirm_application_pack(
            conn, workspace_id, effective_date="2026-09-19", documents_root=documents_root,
            account_id="account_local", document_selection_revisions=revisions,
        )

        cv_row = next(row for row in generated["documents"] if row["document_kind"] == "cv")
        fetched_bytes = DocumentBlobStore(documents_root).read(cv_row)
        assert fetched_bytes == expected_cv_bytes


class TestNoTaskRerunsDuringGeneration:
    def test_only_cv_generation_basis_and_cv_document_renderer_are_touched_for_cv(self, tmp_path):
        """Structural proof: the CV-v2 branch never imports Task 1/2/3 or the
        review projection module -- it can only consume the already-frozen
        basis, never rebuild it."""
        import ast
        import pathlib

        module_path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "webapp" / "services" / "application_documents.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
        forbidden = ("cv_content_plan", "cv_statement_plan", "cv_review_projection")
        for module_name in imported_modules:
            lowered = module_name.lower()
            for term in forbidden:
                assert term not in lowered, f"unexpected import {module_name!r} contains {term!r}"
