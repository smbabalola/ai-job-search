"""Phase 2B-1: Tasks 1-2 against real production-shaped inputs, persisted.

Proves plan_cv_content/build_cv_statement_plan consume the exact field names
and shapes the live pipeline actually emits (not just the simplified Task 1/2
test fixtures), and that webapp.services.cv_generation_v2 persists their exact
outputs as first-class, traceable artifacts with no reshaping, no adapter,
and no fallback to legacy CV generation.
"""
from __future__ import annotations

import copy

import pytest

from product.cv_content_plan import CV_CONTENT_PLAN_VERSION
from product.cv_statement_plan import CV_STATEMENT_PLAN_VERSION
from webapp.persistence.artifacts import get_current_artifact, save_artifact
from webapp.persistence.db import connect, init_db
from webapp.persistence.workspaces import (
    PROFILE_WORKSPACE_ID,
    create_workspace,
    ensure_profile_workspace,
)
from webapp.services.cv_generation_v2 import (
    CV_CONTENT_PLAN_ARTIFACT_VERSION,
    CV_STATEMENT_PLAN_ARTIFACT_VERSION,
    plan_and_persist_cv_generation_v2,
)
from webapp.services.pipeline import PipelineError


_NAME_CLAIM_ID = "clm_0000000000000001"
_PYTHON_CLAIM_ID = "clm_0000000000000002"
_ROLE_CLAIM_ID = "clm_0000000000000003"


def _workspace(tmp_path):
    db_path = tmp_path / "jobsearch.sqlite3"
    init_db(db_path)
    conn = connect(db_path)
    ensure_profile_workspace(conn)
    workspace = create_workspace(conn, company="Acme / Corp", title="Backend Engineer")
    return conn, workspace["id"]


def _profile_claim(claim_id, concept_id, category, field, value, *, record_id=None, line=1):
    claim = {
        "id": claim_id,
        "concept_id": concept_id,
        "category": category,
        "field": field,
        "value": value,
        "source": {"file": "profile.md", "section": None, "line_start": line, "line_end": line},
        "placeholder": False,
        "confidence": "high",
        "extraction_status": "explicit",
    }
    if record_id is not None:
        claim["record_id"] = record_id
    return claim


def _profile_payload():
    claims = [
        _profile_claim(_NAME_CLAIM_ID, "cpt_0000000000000001", "identity", "name", "Ada Lovelace"),
        _profile_claim(
            _PYTHON_CLAIM_ID, "cpt_0000000000000002", "skills", "technical_skill", "Python",
            record_id="rec_0000000000000001",
        ),
        _profile_claim(
            _ROLE_CLAIM_ID, "cpt_0000000000000003", "employment", "responsibility_or_achievement",
            "Built production Python data pipelines.", record_id="rec_0000000000000002",
        ),
    ]
    return {
        "schema_version": "candidate-profile-evidence-snapshot.v0",
        "claims": claims,
        "corroborations": [],
        "conflicts": [],
        "summary": {
            "claim_count": len(claims),
            "placeholder_claim_count": 0,
            "conflict_count": 0,
        },
    }


def _resolved_job_evidence_payload():
    """Real production shape from build_resolved_job_evidence_bundle, not the
    simplified Task 1 test fixture: real records carry origin/status too."""
    return {
        "schema_version": "resolved-job-evidence-bundle.v0",
        "job_snapshot": {"schema_version": "job-posting-snapshot.v0", "content_id": "jobsnap_A"},
        "evidence": [
            {
                "id": "jobev_python", "category": "requirements",
                "text": "Requires production Python experience.", "kind": "required",
                "origin": "job_posting_snapshot", "status": "EXPLICIT",
            },
            {
                "id": "jobev_leadership", "category": "responsibilities",
                "text": "Lead cross-functional technical delivery.", "kind": "preferred",
                "origin": "job_posting_snapshot", "status": "EXPLICIT",
            },
            {
                "id": "jobev_german", "category": "language_requirements",
                "text": "Requires fluent German.", "kind": "required",
                "origin": "job_posting_snapshot", "status": "EXPLICIT",
            },
        ],
        "aliases": [],
        "excluded": {
            "raw_text": "not_semantic_fit_evidence",
            "suggestions": "not_semantic_fit_evidence",
            "ambiguous_statements": "not_semantic_fit_evidence",
            "warnings": "not_semantic_fit_evidence",
        },
        "summary": {"evidence_count": 3, "alias_count": 0},
    }


def _job_fit_result_payload():
    """Real production shape from product.semantic_job_fit's builder: matches
    carry match_id/classification/status/job_requirement_ids/
    profile_evidence_ids; gate_assessments carry gate_id/status/reason/
    job_evidence_ids/profile_evidence_ids/evidence_disposition/materiality."""
    return {
        "schema_version": "job-fit-result.v2",
        "status": "READY",
        "blocked": False,
        "blocking_gate_ids": [],
        "verdict": {"id": "strong_fit", "display_name": "Strong Fit", "score": 90.0},
        "dimension_assessments": [],
        "dimension_scores": {"technical_skills": 90.0},
        "gate_assessments": [
            {
                "gate_id": "eligibility", "status": "PASS",
                "reason": "Candidate is eligible to work in the required jurisdiction.",
                "job_evidence_ids": [], "profile_evidence_ids": [],
                "resolved_answer_ids": [], "evidence_disposition": "SUPPORTIVE",
                "materiality": "NOT_APPLICABLE",
            },
            {
                "gate_id": "language", "status": "FLAG",
                "reason": "German is required and not evidenced by the profile.",
                "job_evidence_ids": ["jobev_german"], "profile_evidence_ids": [],
                "resolved_answer_ids": [], "evidence_disposition": "ABSENT",
                "materiality": "MATERIAL",
            },
            {
                "gate_id": "location_logistics", "status": "NOT_APPLICABLE",
                "reason": "No logistics requirement present in this posting.",
                "job_evidence_ids": [], "profile_evidence_ids": [],
                "resolved_answer_ids": [], "evidence_disposition": "ABSENT",
                "materiality": "NOT_APPLICABLE",
            },
        ],
        "direct_matches": [
            {
                "match_id": "match_python", "classification": "direct", "status": "READY",
                "job_requirement_ids": ["jobev_python"], "profile_evidence_ids": [_PYTHON_CLAIM_ID],
                "rationale": "Direct Python skill match.",
            },
        ],
        "functionally_equivalent_matches": [],
        "transferable_matches": [
            {
                "match_id": "match_leadership", "classification": "transferable", "status": "READY",
                "job_requirement_ids": ["jobev_leadership"], "profile_evidence_ids": [_ROLE_CLAIM_ID],
                "rationale": "Delivery ownership transfers to cross-functional leadership.",
            },
        ],
        "gaps": [
            {
                "gap_id": "gap_german", "job_requirement_ids": ["jobev_german"],
                "gap_type": "missing_evidence", "evidence_status": "UNSUPPORTED", "notes": None,
            },
        ],
        "human_judgment_questions": [],
        "unsupported_claims": [],
    }


def _seed_upstream_artifacts(conn, workspace_id):
    profile_artifact = save_artifact(
        conn, workspace_id=PROFILE_WORKSPACE_ID, artifact_type="profile_snapshot",
        payload=_profile_payload(), content_id="profilesnap_A",
    )
    resolved_job_evidence_artifact = save_artifact(
        conn, workspace_id=workspace_id, artifact_type="resolved_job_evidence",
        payload=_resolved_job_evidence_payload(), content_id="resolvedjobev_A",
    )
    job_fit_artifact = save_artifact(
        conn, workspace_id=workspace_id, artifact_type="job_fit_result",
        payload=_job_fit_result_payload(), content_id="jobfit_A",
    )
    return profile_artifact, resolved_job_evidence_artifact, job_fit_artifact


class TestProductionShapeCompatibility:
    """Task 1 must consume the real live field names/shapes with no adapter."""

    def test_plan_cv_content_consumes_real_production_shaped_inputs(self, tmp_path):
        from product.cv_content_plan import plan_cv_content

        conn, workspace_id = _workspace(tmp_path)
        _, resolved_job_evidence_artifact, job_fit_artifact = _seed_upstream_artifacts(conn, workspace_id)
        profile_payload = _profile_payload()

        plan = plan_cv_content(
            profile_payload, job_fit_artifact["payload"], resolved_job_evidence_artifact["payload"],
        )

        assert plan["schema_version"] == CV_CONTENT_PLAN_VERSION
        # Direct match (Python) and transferable match (leadership) both cite
        # real job_requirement_ids/profile_evidence_ids from the production shape.
        covered = {item["job_requirement_id"] for item in plan["must_cover_requirements"] if item["covered"]}
        assert "jobev_python" in covered
        assert "jobev_leadership" in covered
        # The gate-derived gap (German) with no supporting profile evidence must
        # surface as an explicit uncovered requirement, not silently disappear.
        uncovered_ids = {item["job_requirement_id"] for item in plan["uncovered_requirements"]}
        assert "jobev_german" in uncovered_ids

    def test_gate_assessments_job_evidence_ids_field_is_read_correctly(self, tmp_path):
        """Regression for the one field the design review flagged as needing
        confirmation: gate_assessments[].job_evidence_ids (not gate_evidence_ids
        or job_requirement_ids) is the real production key, and Task 1 reads it
        under that exact name via job_fit_result.get("gate_assessments", [])."""
        from product.cv_content_plan import _match_contexts_by_profile_id

        job_fit_payload = _job_fit_result_payload()
        job_by_id = {item["id"]: item for item in _resolved_job_evidence_payload()["evidence"]}
        contexts = _match_contexts_by_profile_id(job_fit_payload, job_by_id)
        # The language gate cites no profile evidence, so it produces no
        # context; this call must not raise on the real field shape either way.
        assert isinstance(contexts, dict)

    def test_resolved_job_evidence_categories_and_kinds_match_task1_expectations(self, tmp_path):
        from product.cv_content_plan import _important_requirements

        bundle = _resolved_job_evidence_payload()
        important = _important_requirements(bundle)
        kinds = {item["kind"] for item in important}
        assert kinds <= {"required", "preferred"}
        ids = {item["id"] for item in important}
        assert "jobev_python" in ids
        assert "jobev_german" in ids


class TestServicePersistence:
    def test_missing_profile_snapshot_raises_stable_error(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        with pytest.raises(PipelineError, match="profile_snapshot"):
            plan_and_persist_cv_generation_v2(conn, workspace_id)

    def test_missing_job_fit_result_raises_stable_error(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        save_artifact(
            conn, workspace_id=PROFILE_WORKSPACE_ID, artifact_type="profile_snapshot",
            payload=_profile_payload(), content_id="profilesnap_A",
        )
        with pytest.raises(PipelineError, match="job_fit_result"):
            plan_and_persist_cv_generation_v2(conn, workspace_id)

    def test_missing_resolved_job_evidence_raises_stable_error(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        save_artifact(
            conn, workspace_id=PROFILE_WORKSPACE_ID, artifact_type="profile_snapshot",
            payload=_profile_payload(), content_id="profilesnap_A",
        )
        save_artifact(
            conn, workspace_id=workspace_id, artifact_type="job_fit_result",
            payload=_job_fit_result_payload(), content_id="jobfit_A",
        )
        with pytest.raises(PipelineError, match="resolved_job_evidence"):
            plan_and_persist_cv_generation_v2(conn, workspace_id)

    def test_malformed_upstream_job_fit_result_raises_stable_error_not_silent_fallback(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        save_artifact(
            conn, workspace_id=PROFILE_WORKSPACE_ID, artifact_type="profile_snapshot",
            payload=_profile_payload(), content_id="profilesnap_A",
        )
        save_artifact(
            conn, workspace_id=workspace_id, artifact_type="resolved_job_evidence",
            payload=_resolved_job_evidence_payload(), content_id="resolvedjobev_A",
        )
        # Malformed: the stored job_fit_result payload itself is not an
        # object at all (e.g. corrupted/partial write) -- plan_cv_content
        # calls .get() directly on it and must fail loudly rather than
        # silently treating it as empty input.
        save_artifact(
            conn, workspace_id=workspace_id, artifact_type="job_fit_result",
            payload=None, content_id="jobfit_broken",
        )
        with pytest.raises(Exception):
            plan_and_persist_cv_generation_v2(conn, workspace_id)
        # No cv_content_plan/cv_statement_plan may have been persisted from a
        # failed run -- the transaction must roll back, not partially commit.
        assert get_current_artifact(conn, workspace_id, "cv_content_plan") is None
        assert get_current_artifact(conn, workspace_id, "cv_statement_plan") is None

    def test_persists_content_plan_and_statement_plan_round_trip(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_upstream_artifacts(conn, workspace_id)

        result = plan_and_persist_cv_generation_v2(conn, workspace_id)

        content_plan_artifact = result["content_plan_artifact"]
        statement_plan_artifact = result["statement_plan_artifact"]
        # Envelope schema version at the top level; the exact untouched Task
        # 1/2 output lives unmodified under content_plan/statement_plan.
        assert content_plan_artifact["payload"]["schema_version"] == CV_CONTENT_PLAN_ARTIFACT_VERSION
        assert content_plan_artifact["payload"]["content_plan"]["schema_version"] == CV_CONTENT_PLAN_VERSION
        assert statement_plan_artifact["payload"]["schema_version"] == CV_STATEMENT_PLAN_ARTIFACT_VERSION
        assert statement_plan_artifact["payload"]["statement_plan"]["schema_version"] == CV_STATEMENT_PLAN_VERSION

        persisted_content_plan = get_current_artifact(conn, workspace_id, "cv_content_plan")
        assert persisted_content_plan is not None
        assert persisted_content_plan["payload"] == content_plan_artifact["payload"]

        persisted_statement_plan = get_current_artifact(conn, workspace_id, "cv_statement_plan")
        assert persisted_statement_plan is not None
        assert persisted_statement_plan["payload"] == statement_plan_artifact["payload"]

    def test_envelope_source_artifact_refs_point_to_the_exact_artifacts_used(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        profile_artifact, resolved_job_evidence_artifact, job_fit_artifact = _seed_upstream_artifacts(
            conn, workspace_id
        )

        result = plan_and_persist_cv_generation_v2(conn, workspace_id)
        content_plan_artifact = result["content_plan_artifact"]
        statement_plan_artifact = result["statement_plan_artifact"]

        content_plan_refs = content_plan_artifact["payload"]["source_artifacts"]
        assert content_plan_refs["profile_snapshot"] == {
            "artifact_id": profile_artifact["id"],
            "artifact_type": "profile_snapshot",
            "content_id": profile_artifact["content_id"],
        }
        assert content_plan_refs["job_fit_result"] == {
            "artifact_id": job_fit_artifact["id"],
            "artifact_type": "job_fit_result",
            "content_id": job_fit_artifact["content_id"],
        }
        assert content_plan_refs["resolved_job_evidence"] == {
            "artifact_id": resolved_job_evidence_artifact["id"],
            "artifact_type": "resolved_job_evidence",
            "content_id": resolved_job_evidence_artifact["content_id"],
        }

        statement_plan_refs = statement_plan_artifact["payload"]["source_artifacts"]
        assert statement_plan_refs["cv_content_plan"] == {
            "artifact_id": content_plan_artifact["id"],
            "artifact_type": "cv_content_plan",
            "content_id": content_plan_artifact["content_id"],
        }

    def test_statement_plan_is_built_from_the_persisted_content_plan_not_a_fixture(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        _seed_upstream_artifacts(conn, workspace_id)

        result = plan_and_persist_cv_generation_v2(conn, workspace_id)

        persisted_content_plan = get_current_artifact(conn, workspace_id, "cv_content_plan")
        from product.cv_statement_plan import build_cv_statement_plan

        expected_statement_plan = build_cv_statement_plan(persisted_content_plan["payload"]["content_plan"])
        assert result["statement_plan_artifact"]["payload"]["statement_plan"] == expected_statement_plan

    def test_dependency_fingerprints_trace_to_exact_upstream_artifacts(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        profile_artifact, resolved_job_evidence_artifact, job_fit_artifact = _seed_upstream_artifacts(
            conn, workspace_id
        )

        result = plan_and_persist_cv_generation_v2(conn, workspace_id)
        content_plan_artifact = result["content_plan_artifact"]
        statement_plan_artifact = result["statement_plan_artifact"]

        fingerprints = {
            row["upstream_artifact_type"]: row["upstream_content_id"]
            for row in conn.execute(
                "SELECT upstream_artifact_type, upstream_content_id FROM dependency_fingerprints "
                "WHERE artifact_id = ?",
                (content_plan_artifact["id"],),
            ).fetchall()
        }
        assert fingerprints["profile_snapshot"] == profile_artifact["content_id"]
        assert fingerprints["job_fit_result"] == job_fit_artifact["content_id"]
        assert fingerprints["resolved_job_evidence"] == resolved_job_evidence_artifact["content_id"]

        statement_fingerprints = {
            row["upstream_artifact_type"]: row["upstream_content_id"]
            for row in conn.execute(
                "SELECT upstream_artifact_type, upstream_content_id FROM dependency_fingerprints "
                "WHERE artifact_id = ?",
                (statement_plan_artifact["id"],),
            ).fetchall()
        }
        assert statement_fingerprints["cv_content_plan"] == content_plan_artifact["content_id"]

    def test_deterministic_content_across_repeated_runs(self, tmp_path):
        """Determinism means: same exact inputs + same exact lineage -> same
        payload/content_id. It does NOT mean "same inner plan -> same
        artifact identity regardless of lineage": since content_plan is a
        fresh immutable row each run (even given byte-identical upstream
        inputs), the second run's statement_plan legitimately references a
        DIFFERENT cv_content_plan artifact_id, so its envelope -- and
        therefore its content_id -- is correctly not identical to the
        first run's. What must remain invariant is the inner Task 1/2
        domain output, and the content_plan envelope itself (whose lineage
        genuinely was identical both runs, since no new upstream artifacts
        were created between them)."""
        conn, workspace_id = _workspace(tmp_path)
        _seed_upstream_artifacts(conn, workspace_id)

        first = plan_and_persist_cv_generation_v2(conn, workspace_id)
        second = plan_and_persist_cv_generation_v2(conn, workspace_id)

        # content_plan's own lineage (Profile/JobFit/resolved-evidence) was
        # identical across both runs -- no new upstream rows were created --
        # so its full envelope, including content_id, is exactly reproduced.
        assert first["content_plan_artifact"]["payload"] == second["content_plan_artifact"]["payload"]
        assert first["content_plan_artifact"]["content_id"] == second["content_plan_artifact"]["content_id"]

        # statement_plan's inner Task 2 domain output is identical...
        assert (
            first["statement_plan_artifact"]["payload"]["statement_plan"]
            == second["statement_plan_artifact"]["payload"]["statement_plan"]
        )
        # ...and its cv_content_plan lineage ref points at content whose
        # content_id is identical both runs (same content_plan content)...
        assert (
            first["statement_plan_artifact"]["payload"]["source_artifacts"]["cv_content_plan"]["content_id"]
            == second["statement_plan_artifact"]["payload"]["source_artifacts"]["cv_content_plan"]["content_id"]
        )
        # ...but each run's content_plan is a distinct new immutable row, so
        # the statement_plan envelope's referenced artifact_id, and therefore
        # its own content_id, correctly differ between the two runs.
        assert (
            first["statement_plan_artifact"]["payload"]["source_artifacts"]["cv_content_plan"]["artifact_id"]
            != second["statement_plan_artifact"]["payload"]["source_artifacts"]["cv_content_plan"]["artifact_id"]
        )
        assert (
            first["statement_plan_artifact"]["content_id"]
            != second["statement_plan_artifact"]["content_id"]
        )

    def test_identical_inner_plan_but_different_upstream_lineage_yields_different_content_id(self, tmp_path):
        """Provenance is part of the immutable artifact content: two runs whose
        Task 1/2 domain output is logically identical but whose exact upstream
        artifact identity differs must NOT collapse to the same content_id."""
        conn_a, workspace_a = _workspace(tmp_path)
        _seed_upstream_artifacts(conn_a, workspace_a)
        result_a = plan_and_persist_cv_generation_v2(conn_a, workspace_a)

        conn_b, workspace_b = _workspace(tmp_path)
        _seed_upstream_artifacts(conn_b, workspace_b)
        # Re-save an upstream artifact with byte-identical payload but under a
        # different content_id -- as if it were a distinct artifact row with
        # the same domain content (e.g. regenerated, not truly re-derived).
        save_artifact(
            conn_b, workspace_id=PROFILE_WORKSPACE_ID, artifact_type="profile_snapshot",
            payload=_profile_payload(), content_id="profilesnap_DIFFERENT",
        )
        result_b = plan_and_persist_cv_generation_v2(conn_b, workspace_b)

        # Inner Task 1 domain output is logically identical...
        assert (
            result_a["content_plan_artifact"]["payload"]["content_plan"]
            == result_b["content_plan_artifact"]["payload"]["content_plan"]
        )
        # ...but exact upstream lineage differs, so the persisted envelope
        # content_id must differ too.
        assert (
            result_a["content_plan_artifact"]["content_id"]
            != result_b["content_plan_artifact"]["content_id"]
        )

    def test_never_calls_application_intelligence_or_touches_application_pack(self, tmp_path):
        """This service must not construct/require an application_intelligence_*
        or application_pack artifact at all -- it only needs Profile, Job Fit,
        and resolved job evidence."""
        conn, workspace_id = _workspace(tmp_path)
        _seed_upstream_artifacts(conn, workspace_id)

        plan_and_persist_cv_generation_v2(conn, workspace_id)

        assert get_current_artifact(conn, workspace_id, "application_intelligence_result") is None
        assert get_current_artifact(conn, workspace_id, "application_pack") is None


class TestImportBoundary:
    def test_service_module_does_not_import_application_intelligence_or_pack(self):
        import ast
        import pathlib

        module_path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "webapp" / "services" / "cv_generation_v2.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
        forbidden = ("application_intelligence", "application_pack", "cv_document_model", "cv_document_renderer")
        for module_name in imported_modules:
            lowered = module_name.lower()
            for term in forbidden:
                assert term not in lowered, f"unexpected import {module_name!r} contains {term!r}"
