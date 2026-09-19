"""Phase 2B-3: immutable reviewed CV generation basis service.

Proves the full chain: pinned cv_statement_plan artifact -> exact effective
cv_statement_v2 review decisions -> review-authorized projection -> unchanged
Task 3 build_cv_document_model(...) -> validated immutable
cv_generation_basis artifact. Uses only the exact lineage refs embedded by
Slice 2B-1A -- no current-artifact lookups, no content-ID guessing.
"""
from __future__ import annotations

import copy
import json

import pytest

from product.cv_generation_basis_contract import CvGenerationBasisContractError
from product.cv_review_projection import AUTHORIZED_DISPOSITION
from webapp.persistence.artifacts import get_artifact, save_artifact
from webapp.persistence.db import connect, init_db
from webapp.persistence.workspaces import (
    PROFILE_WORKSPACE_ID,
    create_workspace,
    ensure_profile_workspace,
)
from webapp.services.cv_generation_basis import (
    CvGenerationBasisError,
    build_and_persist_cv_generation_basis,
)
from webapp.services.cv_statement_review import save_cv_statement_review_decision


def _workspace(tmp_path):
    db_path = tmp_path / "jobsearch.sqlite3"
    init_db(db_path)
    conn = connect(db_path)
    ensure_profile_workspace(conn)
    workspace = create_workspace(conn, company="Acme / Corp", title="Backend Engineer")
    return conn, workspace["id"]


def _statement(statement_id: str, target_section: str, text: str, *, record_id: str | None = None) -> dict:
    return {
        "statement_id": statement_id,
        "target_section": target_section,
        "target_record_id": record_id,
        "source_profile_evidence_ids": [f"clm_{statement_id}"],
        "source_job_requirement_ids": [],
        "statement_text": text,
        "provenance": {"profile_evidence_ids": [f"clm_{statement_id}"], "job_requirement_ids": []},
    }


def _statement_plan_payload(statements: list[dict]) -> dict:
    return {
        "schema_version": "cv-statement-plan.v0",
        "statements": statements,
        "omitted_evidence": [],
        "ungroupable_evidence": [],
        "uncovered_requirements": [],
        "provenance": {"statement_profile_evidence_ids": [], "source_selected_job_requirement_ids": []},
    }


def _content_plan_payload() -> dict:
    return {
        "schema_version": "cv-content-plan.v0",
        "budgets": {},
        "candidate_pool": {"schema_version": "cv-evidence-candidate-pool.v0", "candidates": [], "summary": {}},
        "summary_themes": [],
        "must_cover_requirements": [],
        "role_bullet_plans": [],
        "skills_to_surface": [],
        "qualifications_to_surface": [],
        "omitted_evidence": [],
        "uncovered_requirements": [],
        "section_priorities": [],
        "provenance": {
            "selected_profile_evidence_ids": [],
            "selected_job_requirement_ids": [],
            "explicit_gap_job_requirement_ids": [],
        },
    }


def _seed_full_lineage(conn, workspace_id, statements: list[dict], *, suffix: str = "A"):
    """Build the exact Slice 2B-1A lineage chain by hand (bypassing
    plan_and_persist_cv_generation_v2, so tests can control statement
    content precisely) and return (statement_plan_artifact, refs)."""

    profile_artifact = save_artifact(
        conn, workspace_id=PROFILE_WORKSPACE_ID, artifact_type="profile_snapshot",
        payload={"schema_version": "candidate-profile-evidence-snapshot.v0", "claims": []},
        content_id=f"profilesnap_{suffix}",
    )
    job_fit_artifact = save_artifact(
        conn, workspace_id=workspace_id, artifact_type="job_fit_result",
        payload={"schema_version": "job-fit-result.v2"}, content_id=f"jobfit_{suffix}",
    )
    resolved_job_evidence_artifact = save_artifact(
        conn, workspace_id=workspace_id, artifact_type="resolved_job_evidence",
        payload={"schema_version": "resolved-job-evidence-bundle.v0"}, content_id=f"resolvedjobev_{suffix}",
    )

    def ref(artifact):
        return {
            "artifact_id": artifact["id"], "artifact_type": artifact["artifact_type"],
            "content_id": artifact["content_id"],
        }

    content_plan_envelope = {
        "schema_version": "cv-content-plan-artifact.v1",
        "source_artifacts": {
            "profile_snapshot": ref(profile_artifact),
            "job_fit_result": ref(job_fit_artifact),
            "resolved_job_evidence": ref(resolved_job_evidence_artifact),
        },
        "content_plan": _content_plan_payload(),
    }
    content_plan_artifact = save_artifact(
        conn, workspace_id=workspace_id, artifact_type="cv_content_plan",
        payload=content_plan_envelope, content_id=f"cvcontentplan_{suffix}",
    )

    statement_plan_envelope = {
        "schema_version": "cv-statement-plan-artifact.v1",
        "source_artifacts": {"cv_content_plan": ref(content_plan_artifact)},
        "statement_plan": _statement_plan_payload(statements),
    }
    statement_plan_artifact = save_artifact(
        conn, workspace_id=workspace_id, artifact_type="cv_statement_plan",
        payload=statement_plan_envelope, content_id=f"cvstatementplan_{suffix}",
    )
    return statement_plan_artifact, {
        "profile_snapshot": profile_artifact,
        "job_fit_result": job_fit_artifact,
        "resolved_job_evidence": resolved_job_evidence_artifact,
        "cv_content_plan": content_plan_artifact,
    }


def _ack(conn, workspace_id, artifact_id, statement_id):
    return save_cv_statement_review_decision(
        conn, workspace_id, statement_plan_artifact_id=artifact_id,
        statement_id=statement_id, disposition=AUTHORIZED_DISPOSITION,
    )


def _omit(conn, workspace_id, artifact_id, statement_id):
    return save_cv_statement_review_decision(
        conn, workspace_id, statement_plan_artifact_id=artifact_id,
        statement_id=statement_id, disposition="omit_from_positioning",
    )


class TestBuildAndPersist:
    def test_fully_reviewed_plan_creates_a_valid_basis(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan, refs = _seed_full_lineage(conn, workspace_id, [
            _statement("stmt_a", "professional_summary", "Delivered Python systems."),
            _statement("stmt_b", "skills", "Python"),
        ])
        _ack(conn, workspace_id, plan["id"], "stmt_a")
        _ack(conn, workspace_id, plan["id"], "stmt_b")

        result = build_and_persist_cv_generation_basis(
            conn, workspace_id, statement_plan_artifact_id=plan["id"],
        )
        basis = result["basis_artifact"]["payload"]

        assert basis["schema_version"] == "cv-generation-basis.v1"
        assert basis["cv_document_model"]["schema_version"] == "cv-document-model.v2"

    def test_exact_lineage_refs_pinned(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan, refs = _seed_full_lineage(conn, workspace_id, [
            _statement("stmt_a", "professional_summary", "A"),
        ])
        _ack(conn, workspace_id, plan["id"], "stmt_a")

        result = build_and_persist_cv_generation_basis(
            conn, workspace_id, statement_plan_artifact_id=plan["id"],
        )
        source_artifacts = result["basis_artifact"]["payload"]["source_artifacts"]

        for upstream_type in ("profile_snapshot", "job_fit_result", "resolved_job_evidence", "cv_content_plan"):
            assert source_artifacts[upstream_type]["artifact_id"] == refs[upstream_type]["id"]
            assert source_artifacts[upstream_type]["content_id"] == refs[upstream_type]["content_id"]
        assert source_artifacts["cv_statement_plan"]["artifact_id"] == plan["id"]
        assert source_artifacts["cv_statement_plan"]["content_id"] == plan["content_id"]

    def test_review_audit_pinned(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan, _refs = _seed_full_lineage(conn, workspace_id, [
            _statement("stmt_a", "professional_summary", "Kept."),
            _statement("stmt_b", "skills", "Dropped."),
        ])
        ack_decision = _ack(conn, workspace_id, plan["id"], "stmt_a")
        omit_decision = _omit(conn, workspace_id, plan["id"], "stmt_b")

        result = build_and_persist_cv_generation_basis(
            conn, workspace_id, statement_plan_artifact_id=plan["id"],
        )
        review = result["basis_artifact"]["payload"]["review"]

        assert review["statement_plan_artifact_id"] == plan["id"]
        assert review["authorized_statement_ids"] == ["stmt_a"]
        assert review["omitted_statement_ids"] == ["stmt_b"]
        assert set(review["consulted_decision_ids"]) == {ack_decision["id"], omit_decision["id"]}

    def test_task3_receives_only_authorized_projection_and_omitted_absent_from_model(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan, _refs = _seed_full_lineage(conn, workspace_id, [
            _statement("stmt_a", "professional_summary", "Kept summary."),
            _statement("stmt_b", "skills", "Dropped skill."),
        ])
        _ack(conn, workspace_id, plan["id"], "stmt_a")
        _omit(conn, workspace_id, plan["id"], "stmt_b")

        result = build_and_persist_cv_generation_basis(
            conn, workspace_id, statement_plan_artifact_id=plan["id"],
        )
        model = result["basis_artifact"]["payload"]["cv_document_model"]

        selected_ids = set(model["provenance"]["selected_statement_ids"])
        assert selected_ids == {"stmt_a"}
        section_ids = {section["section_id"] for section in model["sections"]}
        assert "skills" not in section_ids

    def test_incomplete_review_blocks_basis_creation(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan, _refs = _seed_full_lineage(conn, workspace_id, [
            _statement("stmt_a", "professional_summary", "A"),
            _statement("stmt_b", "skills", "B"),
        ])
        _ack(conn, workspace_id, plan["id"], "stmt_a")
        # stmt_b left pending.

        with pytest.raises(Exception):
            build_and_persist_cv_generation_basis(conn, workspace_id, statement_plan_artifact_id=plan["id"])
        from webapp.persistence.artifacts import get_current_artifact

        assert get_current_artifact(conn, workspace_id, "cv_generation_basis") is None

    def test_all_statements_omitted_still_produces_a_valid_empty_basis(self, tmp_path):
        """Task 3 legitimately accepts an empty authorized projection (no
        sections). Review-complete-but-all-omitted must still build a basis
        rather than raising or inventing replacement content."""
        conn, workspace_id = _workspace(tmp_path)
        plan, _refs = _seed_full_lineage(conn, workspace_id, [
            _statement("stmt_a", "professional_summary", "A"),
        ])
        _omit(conn, workspace_id, plan["id"], "stmt_a")

        result = build_and_persist_cv_generation_basis(
            conn, workspace_id, statement_plan_artifact_id=plan["id"],
        )
        basis = result["basis_artifact"]["payload"]

        assert basis["review"]["authorized_statement_ids"] == []
        assert basis["review"]["omitted_statement_ids"] == ["stmt_a"]
        assert basis["cv_document_model"]["provenance"]["selected_statement_ids"] == []
        assert basis["cv_document_model"]["sections"] == []

    def test_wrong_pinned_artifact_type_is_rejected(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        wrong = save_artifact(
            conn, workspace_id=workspace_id, artifact_type="cv_content_plan",
            payload={"schema_version": "cv-content-plan-artifact.v1"}, content_id="cvcontentplan_A",
        )
        with pytest.raises(CvGenerationBasisError, match="not a cv_statement_plan"):
            build_and_persist_cv_generation_basis(conn, workspace_id, statement_plan_artifact_id=wrong["id"])

    def test_nonexistent_pinned_artifact_is_rejected(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        with pytest.raises(CvGenerationBasisError, match="not found"):
            build_and_persist_cv_generation_basis(
                conn, workspace_id, statement_plan_artifact_id="art_nonexistent",
            )

    def test_lineage_ref_mismatch_against_resolved_artifact_is_rejected(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan, refs = _seed_full_lineage(conn, workspace_id, [_statement("stmt_a", "professional_summary", "A")])
        _ack(conn, workspace_id, plan["id"], "stmt_a")

        # Corrupt the content_plan artifact's embedded profile_snapshot ref
        # so its content_id no longer matches the real resolved artifact row.
        content_plan_artifact = get_artifact(conn, refs["cv_content_plan"]["id"])
        tampered_payload = copy.deepcopy(content_plan_artifact["payload"])
        tampered_payload["source_artifacts"]["profile_snapshot"]["content_id"] = "profilesnap_TAMPERED"
        conn.execute(
            "UPDATE artifacts SET payload_json = ? WHERE id = ?",
            (json.dumps(tampered_payload), content_plan_artifact["id"]),
        )
        conn.commit()

        with pytest.raises(CvGenerationBasisError, match="content_id mismatch"):
            build_and_persist_cv_generation_basis(conn, workspace_id, statement_plan_artifact_id=plan["id"])


class TestDeterminism:
    def test_repeated_identical_build_yields_identical_payload_and_content_id(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan, _refs = _seed_full_lineage(conn, workspace_id, [_statement("stmt_a", "professional_summary", "A")])
        _ack(conn, workspace_id, plan["id"], "stmt_a")

        first = build_and_persist_cv_generation_basis(conn, workspace_id, statement_plan_artifact_id=plan["id"])
        second = build_and_persist_cv_generation_basis(conn, workspace_id, statement_plan_artifact_id=plan["id"])

        assert first["basis_artifact"]["payload"] == second["basis_artifact"]["payload"]
        assert first["basis_artifact"]["content_id"] == second["basis_artifact"]["content_id"]

    def test_changed_effective_review_decision_changes_content_id(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan, _refs = _seed_full_lineage(conn, workspace_id, [
            _statement("stmt_a", "professional_summary", "A"),
            _statement("stmt_b", "skills", "B"),
        ])
        _ack(conn, workspace_id, plan["id"], "stmt_a")
        _ack(conn, workspace_id, plan["id"], "stmt_b")
        basis_a = build_and_persist_cv_generation_basis(conn, workspace_id, statement_plan_artifact_id=plan["id"])

        _omit(conn, workspace_id, plan["id"], "stmt_b")
        basis_b = build_and_persist_cv_generation_basis(conn, workspace_id, statement_plan_artifact_id=plan["id"])

        assert (
            basis_a["basis_artifact"]["content_id"] != basis_b["basis_artifact"]["content_id"]
        )


class TestHistoricalImmutability:
    def test_newer_upstream_state_does_not_mutate_historical_basis(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan_a, _refs = _seed_full_lineage(conn, workspace_id, [_statement("stmt_a", "professional_summary", "A")], suffix="A")
        _ack(conn, workspace_id, plan_a["id"], "stmt_a")
        basis_a = build_and_persist_cv_generation_basis(conn, workspace_id, statement_plan_artifact_id=plan_a["id"])
        basis_a_id = basis_a["basis_artifact"]["id"]
        basis_a_payload_before = copy.deepcopy(basis_a["basis_artifact"]["payload"])

        # Generate a newer, unrelated plan (as if upstream Profile/Job Fit
        # changed and a fresh plan+review cycle ran).
        plan_b, _refs_b = _seed_full_lineage(conn, workspace_id, [_statement("stmt_x", "professional_summary", "X")], suffix="B")
        _ack(conn, workspace_id, plan_b["id"], "stmt_x")
        build_and_persist_cv_generation_basis(conn, workspace_id, statement_plan_artifact_id=plan_b["id"])

        reread = get_artifact(conn, basis_a_id)
        assert reread["payload"] == basis_a_payload_before

    def test_review_supersession_does_not_mutate_historical_basis_and_new_basis_reflects_current_state(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan, _refs = _seed_full_lineage(conn, workspace_id, [
            _statement("stmt_a", "professional_summary", "A statement."),
            _statement("stmt_b", "skills", "B statement."),
        ])
        _ack(conn, workspace_id, plan["id"], "stmt_a")
        _ack(conn, workspace_id, plan["id"], "stmt_b")

        result_a = build_and_persist_cv_generation_basis(conn, workspace_id, statement_plan_artifact_id=plan["id"])
        basis_a = result_a["basis_artifact"]
        basis_a_payload_before = copy.deepcopy(basis_a["payload"])

        # Append a newer, superseding decision for stmt_b.
        _omit(conn, workspace_id, plan["id"], "stmt_b")

        result_b = build_and_persist_cv_generation_basis(conn, workspace_id, statement_plan_artifact_id=plan["id"])
        basis_b = result_b["basis_artifact"]

        # Basis A: unchanged, still authorizes A+B, model retains both.
        reread_a = get_artifact(conn, basis_a["id"])
        assert reread_a["payload"] == basis_a_payload_before
        assert reread_a["payload"]["review"]["authorized_statement_ids"] == ["stmt_a", "stmt_b"]
        assert set(reread_a["payload"]["cv_document_model"]["provenance"]["selected_statement_ids"]) == {"stmt_a", "stmt_b"}

        # Basis B: reflects the newer effective decision -- authorizes only A,
        # records B as omitted, model excludes B.
        assert basis_b["payload"]["review"]["authorized_statement_ids"] == ["stmt_a"]
        assert basis_b["payload"]["review"]["omitted_statement_ids"] == ["stmt_b"]
        assert set(basis_b["payload"]["cv_document_model"]["provenance"]["selected_statement_ids"]) == {"stmt_a"}

        assert basis_a["content_id"] != basis_b["content_id"]


class TestUninvolvedComponents:
    def test_task4_and_document_generation_completely_uninvolved(self, tmp_path):
        import ast
        import pathlib

        module_path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "webapp" / "services" / "cv_generation_basis.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
        forbidden = ("cv_document_renderer", "application_pack", "application_intelligence", "application_documents")
        for module_name in imported_modules:
            lowered = module_name.lower()
            for term in forbidden:
                assert term not in lowered, f"unexpected import {module_name!r} contains {term!r}"
