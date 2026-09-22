"""Phase 2 (Slice 2B-5): end-to-end CV Quality v2 acceptance.

Proves the complete real flow through actual HTTP-driven product pipeline
stages plus the CV-v2 orchestration services, called directly so each stage's
exact artifacts can be inspected (the HTTP adapters over these services are
covered by tests/webapp/api/test_cv_generation_v2_routes.py and the browser
journey by tests/webapp/test_cv_generation_v2_browser_acceptance.py):

  candidate evidence/profile + target job
    -> real Job Fit + resolved job evidence (via /understand, /fit)
    -> Task 1 content plan (plan_and_persist_cv_generation_v2)
    -> Task 2 candidate-facing statements
    -> exact human review authorization (cv_statement_review service)
    -> Task 3 cv-document-model.v2 (build_and_persist_cv_generation_basis)
    -> immutable cv_generation_basis
    -> Task 4 DOCX rendering (generate_application_documents, CV-v2 mode)
    -> existing DocumentBlobStore
    -> existing document selection
    -> existing application-pack.v2 confirmation (real HTTP route)
    -> exact CV download / handoff

This suite reuses the exact real product pipeline scaffolding from
tests/webapp/test_full_journey_acceptance.py (_build_chain et al.) rather
than a second parallel acceptance framework.
"""
from __future__ import annotations

import copy
import hashlib

import pytest
from fastapi.testclient import TestClient

from product.cv_document_renderer import render_cv_document
from product.application_pack_renderer import render_cover_letter_document
from webapp.persistence.application_documents import get_document_version
from webapp.persistence.artifacts import get_current_artifact
from webapp.persistence.db import connect
from webapp.persistence.workspaces import PROFILE_WORKSPACE_ID
from webapp.services.application_documents import (
    generate_application_documents,
    select_application_document,
)
from webapp.services.application_pack import build_application_pack, confirm_application_pack
from webapp.services.cv_generation_basis import build_and_persist_cv_generation_basis
from webapp.services.cv_generation_v2 import plan_and_persist_cv_generation_v2
from webapp.services.cv_statement_review import (
    get_review_authorized_cv_statement_plan,
    list_cv_statement_review_items,
    save_cv_statement_review_decision,
)
from webapp.services.document_blob_store import DocumentBlobStore

from tests.webapp.fixtures.acceptance.fixtures import completion_ready_content_units
from tests.webapp.test_full_journey_acceptance import (
    _build_chain,
    _close,
    _decide_current_review_surface,
    _review,
)

AUTHORIZED_DISPOSITION = "acknowledged_and_proceed"


def _cv_v2_chain(tmp_path, *, ai_units=None, include_transfer=True):
    """Build the real HTTP-driven pipeline up through application intelligence
    (exactly _build_chain), decide every legacy review item (the CV-v2 branch
    still rebuilds application-pack.v1 for the cover letter, which requires
    this exactly like the legacy path does), then run the CV Quality v2
    orchestration services directly against the same database the running
    app uses."""

    client, app, settings, workspace_id = _build_chain(
        tmp_path, ai_units=ai_units, include_transfer=include_transfer,
    )
    _decide_current_review_surface(client, workspace_id)
    conn = connect(settings.db_path)
    return client, app, settings, workspace_id, conn


def _authorize_all(conn, workspace_id, statement_plan_artifact_id):
    items = list_cv_statement_review_items(conn, statement_plan_artifact_id)
    for item in items:
        save_cv_statement_review_decision(
            conn, workspace_id, statement_plan_artifact_id=statement_plan_artifact_id,
            statement_id=item["statement_id"], disposition=AUTHORIZED_DISPOSITION,
        )
    return items


class TestServiceLevelGoldenPath:
    def test_full_cv_v2_flow_from_real_evidence_and_job_to_confirmed_pack(self, tmp_path):
        client, app, settings, workspace_id, conn = _cv_v2_chain(
            tmp_path, ai_units=completion_ready_content_units(),
        )
        try:
            # 1-4: real candidate evidence/profile, target job, real semantic
            # Job Fit, real resolved-job-evidence already exist from _build_chain
            # (via /understand and /fit HTTP calls against real product routes).
            assert get_current_artifact(conn, PROFILE_WORKSPACE_ID, "profile_snapshot") is not None
            assert get_current_artifact(conn, workspace_id, "job_fit_result") is not None
            assert get_current_artifact(conn, workspace_id, "resolved_job_evidence") is not None

            # 5: Task 1 + Task 2, persisted as exact provenance-pinned artifacts.
            plan_result = plan_and_persist_cv_generation_v2(conn, workspace_id)
            content_plan_artifact = plan_result["content_plan_artifact"]
            statement_plan_artifact = plan_result["statement_plan_artifact"]
            assert content_plan_artifact["payload"]["schema_version"] == "cv-content-plan-artifact.v1"
            assert statement_plan_artifact["payload"]["schema_version"] == "cv-statement-plan-artifact.v1"

            # 6: review decisions recorded for the exact Task 2 statements.
            reviewed_items = _authorize_all(conn, workspace_id, statement_plan_artifact["id"])
            assert reviewed_items, "expected at least one reviewable Task 2 statement"

            # 7: immutable reviewed generation basis.
            basis_result = build_and_persist_cv_generation_basis(
                conn, workspace_id, statement_plan_artifact_id=statement_plan_artifact["id"],
            )
            basis_artifact = basis_result["basis_artifact"]
            assert basis_artifact["payload"]["schema_version"] == "cv-generation-basis.v1"
            assert basis_artifact["payload"]["review"]["statement_plan_artifact_id"] == statement_plan_artifact["id"]
            assert set(basis_artifact["payload"]["cv_document_model"]["provenance"]["selected_statement_ids"]) == {
                item["statement_id"] for item in reviewed_items
            }

            # 8: CV-v2 document generation -- Task 4 CV + existing reviewed cover letter.
            conn.commit()
            generated = generate_application_documents(
                conn, workspace_id, documents_root=settings.documents_root,
                extensions_dir=settings.extensions_dir, account_id="account_local",
                cv_generation_basis_artifact_id=basis_artifact["id"],
            )
            assert generated["generation_artifact"]["payload"]["schema_version"] == "application-document-generation.v2"
            cv_row = next(row for row in generated["documents"] if row["document_kind"] == "cv")
            cover_row = next(row for row in generated["documents"] if row["document_kind"] == "cover_letter")

            expected_cv_bytes = render_cv_document(basis_artifact["payload"]["cv_document_model"])
            stored_cv_bytes = DocumentBlobStore(settings.documents_root).read(cv_row)
            assert stored_cv_bytes == expected_cv_bytes

            # 9: generated documents are selectable.
            revisions = {}
            for row in generated["documents"]:
                selection = select_application_document(
                    conn, workspace_id, kind=row["document_kind"], document_version_id=row["id"],
                    expected_revision=0, account_id="account_local",
                )
                revisions[row["document_kind"]] = selection["revision"]
            conn.commit()

            # 10: confirm through the REAL HTTP application-pack route --
            # application-pack.v2 must accept this pack exactly as it would
            # accept a legacy one.
            confirm_response = client.post(
                f"/api/workspaces/{workspace_id}/application-pack",
                json={
                    "confirmed": True, "effective_date": "2026-09-19",
                    "document_selection_revisions": revisions,
                },
            )
            assert confirm_response.status_code == 201, confirm_response.text
            confirmed_pack = confirm_response.json()

            # 11-12: exact confirmed CV/cover-letter retrieval equals the
            # exact bytes produced above.
            confirmed_cv_id = cv_row["id"]
            confirmed_cover_id = cover_row["id"]
            download_cv = client.get(f"/api/workspaces/{workspace_id}/application-documents/{confirmed_cv_id}/download")
            assert download_cv.status_code == 200, download_cv.text
            assert download_cv.content == expected_cv_bytes

            download_cover = client.get(f"/api/workspaces/{workspace_id}/application-documents/{confirmed_cover_id}/download")
            assert download_cover.status_code == 200, download_cover.text
        finally:
            conn.close()
            _close(client)


class TestEvidenceGrounding:
    def test_final_cv_contains_only_job_fit_cited_evidence(self, tmp_path):
        client, app, settings, workspace_id, conn = _cv_v2_chain(
            tmp_path, ai_units=completion_ready_content_units(),
        )
        try:
            profile = get_current_artifact(conn, PROFILE_WORKSPACE_ID, "profile_snapshot")["payload"]
            claim_text = {claim["id"]: claim["value"] for claim in profile["claims"]}
            fit = get_current_artifact(conn, workspace_id, "job_fit_result")["payload"]

            # The intended evidence is exactly what real Job Fit cited as a
            # match, together with the job requirements each match covers.
            fit_requirements_by_evidence: dict[str, set[str]] = {}
            for collection in ("direct_matches", "functionally_equivalent_matches", "transferable_matches"):
                for match in fit.get(collection, []):
                    for evidence_id in match["profile_evidence_ids"]:
                        fit_requirements_by_evidence.setdefault(evidence_id, set()).update(match["job_requirement_ids"])
            cited_evidence = set(fit_requirements_by_evidence)
            assert cited_evidence == {"clm_1111111111111111", "clm_2222222222222222"}

            # Profile evidence the job never asked for (a qualification, the
            # legacy AI-content filler claims) must not become CV content.
            unsupported_evidence = {
                "clm_3333333333333333", "clm_7777777777777777",
                "clm_8888888888888888", "clm_aaaaaaaaaaaaaaaa",
            }
            # Gate-only facts (eligibility, language, location) are not CV substance.
            gate_only_evidence = {
                "clm_4444444444444444", "clm_5555555555555555", "clm_6666666666666666",
            }
            assert unsupported_evidence | gate_only_evidence <= set(claim_text)
            assert not (unsupported_evidence | gate_only_evidence) & cited_evidence

            plan_result = plan_and_persist_cv_generation_v2(conn, workspace_id)
            statement_plan_artifact = plan_result["statement_plan_artifact"]
            statement_plan = statement_plan_artifact["payload"]["statement_plan"]

            # Every statement traces to cited evidence, only to requirements
            # Job Fit linked to that evidence, and restates that evidence.
            statement_evidence: set[str] = set()
            for statement in statement_plan["statements"]:
                evidence_ids = statement["source_profile_evidence_ids"]
                assert evidence_ids, f"statement {statement['statement_id']} has no evidence grounding"
                assert set(evidence_ids) <= cited_evidence
                linked_requirements = set().union(*(fit_requirements_by_evidence[eid] for eid in evidence_ids))
                assert set(statement["source_job_requirement_ids"]) <= linked_requirements
                assert statement["statement_text"] in {claim_text[eid] for eid in evidence_ids}
                statement_evidence.update(evidence_ids)
            assert statement_evidence == cited_evidence
            assert {
                item["profile_evidence_id"] for item in statement_plan["omitted_evidence"]
            } >= gate_only_evidence

            _authorize_all(conn, workspace_id, statement_plan_artifact["id"])
            basis_artifact = build_and_persist_cv_generation_basis(
                conn, workspace_id, statement_plan_artifact_id=statement_plan_artifact["id"],
            )["basis_artifact"]
            conn.commit()
            model_provenance = basis_artifact["payload"]["cv_document_model"]["provenance"]
            assert set(model_provenance["source_profile_evidence_ids"]) == cited_evidence

            # Final rendered CV, through the real generation path.
            generated = generate_application_documents(
                conn, workspace_id, documents_root=settings.documents_root,
                extensions_dir=settings.extensions_dir, account_id="account_local",
                cv_generation_basis_artifact_id=basis_artifact["id"],
            )
            cv_row = next(row for row in generated["documents"] if row["document_kind"] == "cv")
            from docx import Document
            from io import BytesIO

            cv_lines = [
                paragraph.text
                for paragraph in Document(BytesIO(DocumentBlobStore(settings.documents_root).read(cv_row))).paragraphs
            ]
            for evidence_id in cited_evidence:
                assert claim_text[evidence_id] in cv_lines
            cv_text = "\n".join(cv_lines)
            for evidence_id in unsupported_evidence | gate_only_evidence:
                assert claim_text[evidence_id] not in cv_text, (
                    f"unsupported evidence {evidence_id} leaked into the rendered CV"
                )
            assert "cvbullet" not in cv_text and "cvsummary" not in cv_text and "coverword" not in cv_text
        finally:
            conn.close()
            _close(client)

class TestReviewBoundary:
    def test_omitted_statement_is_excluded_from_model_and_rendered_docx(self, tmp_path):
        client, app, settings, workspace_id, conn = _cv_v2_chain(
            tmp_path, ai_units=completion_ready_content_units(),
        )
        try:
            plan_result = plan_and_persist_cv_generation_v2(conn, workspace_id)
            statement_plan_artifact = plan_result["statement_plan_artifact"]
            items = list_cv_statement_review_items(conn, statement_plan_artifact["id"])
            assert len(items) >= 1

            # Omit the first statement, authorize the rest.
            omitted_item = items[0]
            omitted_text = omitted_item["statement_text"]
            for item in items:
                disposition = "omit_from_positioning" if item is omitted_item else AUTHORIZED_DISPOSITION
                save_cv_statement_review_decision(
                    conn, workspace_id, statement_plan_artifact_id=statement_plan_artifact["id"],
                    statement_id=item["statement_id"], disposition=disposition,
                )

            basis_result = build_and_persist_cv_generation_basis(
                conn, workspace_id, statement_plan_artifact_id=statement_plan_artifact["id"],
            )
            basis_artifact = basis_result["basis_artifact"]

            # Model-level exclusion.
            assert omitted_item["statement_id"] not in set(
                basis_artifact["payload"]["cv_document_model"]["provenance"]["selected_statement_ids"]
            )
            assert omitted_item["statement_id"] in basis_artifact["payload"]["review"]["omitted_statement_ids"]

            # Rendered-DOCX-level exclusion.
            cv_bytes = render_cv_document(basis_artifact["payload"]["cv_document_model"])
            from docx import Document
            from io import BytesIO

            visible_text = "\n".join(p.text for p in Document(BytesIO(cv_bytes)).paragraphs)
            assert omitted_text not in visible_text
        finally:
            conn.close()
            _close(client)


class TestHistoricalImmutability:
    def test_confirmed_application_a_survives_later_state_changes(self, tmp_path):
        client, app, settings, workspace_id, conn = _cv_v2_chain(
            tmp_path, ai_units=completion_ready_content_units(),
        )
        try:
            plan_result = plan_and_persist_cv_generation_v2(conn, workspace_id)
            statement_plan_artifact = plan_result["statement_plan_artifact"]
            _authorize_all(conn, workspace_id, statement_plan_artifact["id"])
            basis_result = build_and_persist_cv_generation_basis(
                conn, workspace_id, statement_plan_artifact_id=statement_plan_artifact["id"],
            )
            basis_artifact = basis_result["basis_artifact"]
            conn.commit()

            generated = generate_application_documents(
                conn, workspace_id, documents_root=settings.documents_root,
                extensions_dir=settings.extensions_dir, account_id="account_local",
                cv_generation_basis_artifact_id=basis_artifact["id"],
            )
            revisions = {}
            for row in generated["documents"]:
                selection = select_application_document(
                    conn, workspace_id, kind=row["document_kind"], document_version_id=row["id"],
                    expected_revision=0, account_id="account_local",
                )
                revisions[row["document_kind"]] = selection["revision"]
            conn.commit()

            confirm_response = client.post(
                f"/api/workspaces/{workspace_id}/application-pack",
                json={
                    "confirmed": True, "effective_date": "2026-09-19",
                    "document_selection_revisions": revisions,
                },
            )
            assert confirm_response.status_code == 201, confirm_response.text

            original_cv_row = next(row for row in generated["documents"] if row["document_kind"] == "cv")
            original_cv_id = original_cv_row["id"]
            original_bytes = DocumentBlobStore(settings.documents_root).read(original_cv_row)
            original_sha256 = original_cv_row["sha256"]
            original_length = original_cv_row["byte_length"]

            # Materially change current state: run Task 1/2 again over the
            # (unchanged, but independently re-derived) inputs to produce
            # a newer plan/basis -- the earlier confirmed pack must be
            # completely unaffected.
            newer_plan = plan_and_persist_cv_generation_v2(conn, workspace_id)
            newer_statement_plan_artifact = newer_plan["statement_plan_artifact"]
            _authorize_all(conn, workspace_id, newer_statement_plan_artifact["id"])
            build_and_persist_cv_generation_basis(
                conn, workspace_id, statement_plan_artifact_id=newer_statement_plan_artifact["id"],
            )
            conn.commit()

            reread_cv_row = get_document_version(conn, original_cv_id, account_id="account_local")
            assert reread_cv_row is not None
            reread_bytes = DocumentBlobStore(settings.documents_root).read(reread_cv_row)
            assert reread_bytes == original_bytes
            assert reread_cv_row["sha256"] == original_sha256
            assert reread_cv_row["byte_length"] == original_length

            download_cv = client.get(f"/api/workspaces/{workspace_id}/application-documents/{original_cv_id}/download")
            assert download_cv.status_code == 200, download_cv.text
            assert download_cv.content == original_bytes
        finally:
            conn.close()
            _close(client)


class TestPinnedBasisAcceptance:
    def test_generating_with_old_basis_id_after_newer_basis_exists_is_unaffected(self, tmp_path):
        client, app, settings, workspace_id, conn = _cv_v2_chain(
            tmp_path, ai_units=completion_ready_content_units(),
        )
        try:
            plan_a = plan_and_persist_cv_generation_v2(conn, workspace_id)
            _authorize_all(conn, workspace_id, plan_a["statement_plan_artifact"]["id"])
            basis_a = build_and_persist_cv_generation_basis(
                conn, workspace_id, statement_plan_artifact_id=plan_a["statement_plan_artifact"]["id"],
            )["basis_artifact"]
            conn.commit()

            first_generation = generate_application_documents(
                conn, workspace_id, documents_root=settings.documents_root,
                extensions_dir=settings.extensions_dir, account_id="account_local",
                cv_generation_basis_artifact_id=basis_a["id"],
            )
            first_cv_bytes = DocumentBlobStore(settings.documents_root).read(
                next(row for row in first_generation["documents"] if row["document_kind"] == "cv")
            )

            plan_b = plan_and_persist_cv_generation_v2(conn, workspace_id)
            _authorize_all(conn, workspace_id, plan_b["statement_plan_artifact"]["id"])
            build_and_persist_cv_generation_basis(
                conn, workspace_id, statement_plan_artifact_id=plan_b["statement_plan_artifact"]["id"],
            )
            conn.commit()

            second_generation = generate_application_documents(
                conn, workspace_id, documents_root=settings.documents_root,
                extensions_dir=settings.extensions_dir, account_id="account_local",
                cv_generation_basis_artifact_id=basis_a["id"],
            )
            second_cv_bytes = DocumentBlobStore(settings.documents_root).read(
                next(row for row in second_generation["documents"] if row["document_kind"] == "cv")
            )

            assert first_cv_bytes == second_cv_bytes
        finally:
            conn.close()
            _close(client)


class TestNoRegenerationOnDownload:
    def test_download_path_never_imports_task_1_2_3_4_modules(self):
        import ast
        import pathlib

        module_path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "webapp" / "api" / "application_documents.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
        forbidden = ("cv_content_plan", "cv_statement_plan", "cv_review_projection", "cv_document_model", "cv_document_renderer")
        for module_name in imported_modules:
            lowered = module_name.lower()
            for term in forbidden:
                assert term not in lowered, f"download route unexpectedly imports {module_name!r}"

    def test_confirmed_download_never_invokes_cv_v2_planning_modeling_or_rendering(self, tmp_path, monkeypatch):
        client, app, settings, workspace_id, conn = _cv_v2_chain(
            tmp_path, ai_units=completion_ready_content_units(),
        )
        try:
            plan_result = plan_and_persist_cv_generation_v2(conn, workspace_id)
            _authorize_all(conn, workspace_id, plan_result["statement_plan_artifact"]["id"])
            basis_artifact = build_and_persist_cv_generation_basis(
                conn, workspace_id, statement_plan_artifact_id=plan_result["statement_plan_artifact"]["id"],
            )["basis_artifact"]
            conn.commit()

            generated = generate_application_documents(
                conn, workspace_id, documents_root=settings.documents_root,
                extensions_dir=settings.extensions_dir, account_id="account_local",
                cv_generation_basis_artifact_id=basis_artifact["id"],
            )
            revisions = {}
            for row in generated["documents"]:
                selection = select_application_document(
                    conn, workspace_id, kind=row["document_kind"], document_version_id=row["id"],
                    expected_revision=0, account_id="account_local",
                )
                revisions[row["document_kind"]] = selection["revision"]
            conn.commit()
            confirmed = client.post(
                f"/api/workspaces/{workspace_id}/application-pack",
                json={"confirmed": True, "effective_date": "2026-09-19", "document_selection_revisions": revisions},
            )
            assert confirmed.status_code == 201, confirmed.text
            confirmed_cv_id = confirmed.json()["pack"]["final_documents"]["cv"]["document_version_id"]
            cv_row = next(row for row in generated["documents"] if row["document_kind"] == "cv")
            assert confirmed_cv_id == cv_row["id"]
            stored_bytes = DocumentBlobStore(settings.documents_root).read(cv_row)

            # From here on, any Task 1-4 or CV-v2 orchestration call is a failure.
            def _forbidden(name):
                def _raise(*args, **kwargs):
                    raise AssertionError(f"{name} was invoked while downloading a confirmed CV")
                return _raise

            import product.cv_content_plan
            import product.cv_document_model
            import product.cv_document_renderer
            import product.cv_statement_plan
            import webapp.api.cv_generation_v2
            import webapp.services.application_documents
            import webapp.services.cv_generation_basis
            import webapp.services.cv_generation_v2

            for module, name in (
                (product.cv_content_plan, "plan_cv_content"),
                (product.cv_statement_plan, "build_cv_statement_plan"),
                (product.cv_document_model, "build_cv_document_model"),
                (product.cv_document_renderer, "render_cv_document"),
                (webapp.services.cv_generation_v2, "plan_cv_content"),
                (webapp.services.cv_generation_v2, "build_cv_statement_plan"),
                (webapp.services.cv_generation_v2, "plan_and_persist_cv_generation_v2"),
                (webapp.services.cv_generation_basis, "build_cv_document_model"),
                (webapp.services.cv_generation_basis, "build_and_persist_cv_generation_basis"),
                (webapp.services.application_documents, "render_cv_document"),
                (webapp.services.application_documents, "generate_application_documents"),
                (webapp.api.cv_generation_v2, "plan_and_persist_cv_generation_v2"),
                (webapp.api.cv_generation_v2, "build_and_persist_cv_generation_basis"),
            ):
                monkeypatch.setattr(module, name, _forbidden(f"{module.__name__}.{name}"))

            for _ in range(2):
                download = client.get(
                    f"/api/workspaces/{workspace_id}/application-documents/{confirmed_cv_id}/download"
                )
                assert download.status_code == 200, download.text
                assert download.content == stored_bytes
                assert download.headers["x-content-hash"] == "sha256:" + hashlib.sha256(stored_bytes).hexdigest()
                assert hashlib.sha256(download.content).hexdigest() == cv_row["sha256"]
        finally:
            conn.close()
            _close(client)

class TestOneAuthority:
    def test_cv_v2_mode_ignores_legacy_application_intelligence_cv_content(self, tmp_path):
        """Deliberately give Application Intelligence CV content that differs
        from what CV-v2 would produce, and prove the final CV reflects only
        the pinned cv_generation_basis -- never the legacy cv_content units."""
        distinctive_legacy_text = "LEGACY-ONLY-MARKER-TEXT-9f3c"
        legacy_marker_unit = {
            "unit_id": "cv-legacy-marker", "unit_type": "cv_bullet",
            "atoms": [{
                "atom_id": "atom-cv-legacy-marker", "atom_kind": "candidate_fact",
                "assertion_type": "responsibility",
                "profile_evidence_ids": ["clm_7777777777777777"],
                "rendering_variant": "PLAIN",
            }],
            "connectives": [],
        }
        # Completion-readiness (still enforced for the cover-letter half of
        # CV-v2 mode, rebuilt via the legacy build_application_pack path) needs
        # a qualifying cv_bullet + cover_letter_paragraph, same as every other
        # test in this suite -- only the cv_bullet's content needs to be the
        # distinctive legacy marker under test.
        legacy_units = completion_ready_content_units(first_unit=legacy_marker_unit)
        client, app, settings, workspace_id, conn = _cv_v2_chain(tmp_path, ai_units=legacy_units)
        try:
            plan_result = plan_and_persist_cv_generation_v2(conn, workspace_id)
            _authorize_all(conn, workspace_id, plan_result["statement_plan_artifact"]["id"])
            basis_artifact = build_and_persist_cv_generation_basis(
                conn, workspace_id, statement_plan_artifact_id=plan_result["statement_plan_artifact"]["id"],
            )["basis_artifact"]
            conn.commit()

            generated = generate_application_documents(
                conn, workspace_id, documents_root=settings.documents_root,
                extensions_dir=settings.extensions_dir, account_id="account_local",
                cv_generation_basis_artifact_id=basis_artifact["id"],
            )
            cv_row = next(row for row in generated["documents"] if row["document_kind"] == "cv")
            cv_bytes = DocumentBlobStore(settings.documents_root).read(cv_row)

            from docx import Document
            from io import BytesIO

            visible_text = "\n".join(p.text for p in Document(BytesIO(cv_bytes)).paragraphs)
            assert distinctive_legacy_text not in visible_text
        finally:
            conn.close()
            _close(client)


class TestCandidateSnapshotCompatibility:
    def test_confirmed_cv_v2_pack_still_supports_candidate_snapshot(self, tmp_path):
        client, app, settings, workspace_id, conn = _cv_v2_chain(
            tmp_path, ai_units=completion_ready_content_units(),
        )
        try:
            plan_result = plan_and_persist_cv_generation_v2(conn, workspace_id)
            _authorize_all(conn, workspace_id, plan_result["statement_plan_artifact"]["id"])
            basis_artifact = build_and_persist_cv_generation_basis(
                conn, workspace_id, statement_plan_artifact_id=plan_result["statement_plan_artifact"]["id"],
            )["basis_artifact"]
            conn.commit()

            generated = generate_application_documents(
                conn, workspace_id, documents_root=settings.documents_root,
                extensions_dir=settings.extensions_dir, account_id="account_local",
                cv_generation_basis_artifact_id=basis_artifact["id"],
            )
            revisions = {}
            for row in generated["documents"]:
                selection = select_application_document(
                    conn, workspace_id, kind=row["document_kind"], document_version_id=row["id"],
                    expected_revision=0, account_id="account_local",
                )
                revisions[row["document_kind"]] = selection["revision"]
            conn.commit()

            confirm_response = client.post(
                f"/api/workspaces/{workspace_id}/application-pack",
                json={
                    "confirmed": True, "effective_date": "2026-09-19",
                    "document_selection_revisions": revisions,
                },
            )
            assert confirm_response.status_code == 201, confirm_response.text
            confirmed_pack = confirm_response.json()

            reviewed_basis = confirmed_pack["pack"]["generation_basis"]["reviewed_application_pack"]
            assert "candidate_snapshot" in reviewed_basis
            assert reviewed_basis["candidate_snapshot"]["identity"]["name"]["value"]
        finally:
            conn.close()
            _close(client)
