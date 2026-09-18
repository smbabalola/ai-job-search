"""Phase 2B-2: cv_statement_v2 review lane + exact authorization projection.

Proves the CV statement review service reuses the existing generic
review_decisions persistence layer's real effective-decision (newest-wins)
semantics unchanged, binds authorization strictly to
(source_artifact_id, statement_id), and fails closed on any unreviewed
reviewable statement.
"""
from __future__ import annotations

import copy

import pytest

from product.cv_review_projection import AUTHORIZED_DISPOSITION, CvReviewProjectionError
from webapp.persistence.artifacts import save_artifact
from webapp.persistence.db import connect, init_db
from webapp.persistence.workspaces import create_workspace, ensure_profile_workspace
from webapp.services.cv_statement_review import (
    CvStatementReviewError,
    get_review_authorized_cv_statement_plan,
    list_cv_statement_review_items,
    resolve_cv_statement_review_state,
    save_cv_statement_review_decision,
)
from webapp.services.pipeline import PipelineError


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


def _seed_statement_plan(conn, workspace_id, statements: list[dict], *, content_id: str = "cvstatementplan_A"):
    return save_artifact(
        conn, workspace_id=workspace_id, artifact_type="cv_statement_plan",
        payload=_statement_plan_payload(statements), content_id=content_id,
    )


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


class TestEnumerateReviewItems:
    def test_returns_reviewable_statements_in_deterministic_order(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [
            _statement("stmt_a", "professional_summary", "Summary text."),
            _statement("stmt_b", "skills", "Python"),
        ])
        items = list_cv_statement_review_items(conn, plan["id"])
        assert [item["statement_id"] for item in items] == ["stmt_a", "stmt_b"]
        assert items[0]["statement_text"] == "Summary text."

    def test_rejects_nonexistent_artifact(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        with pytest.raises(CvStatementReviewError, match="not found"):
            list_cv_statement_review_items(conn, "art_nonexistent")

    def test_rejects_wrong_artifact_type(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        wrong = save_artifact(
            conn, workspace_id=workspace_id, artifact_type="cv_content_plan",
            payload={"schema_version": "cv-content-plan.v0"}, content_id="cvcontentplan_A",
        )
        with pytest.raises(CvStatementReviewError, match="not a cv_statement_plan"):
            list_cv_statement_review_items(conn, wrong["id"])

    def test_rejects_malformed_statement_plan(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        malformed = save_artifact(
            conn, workspace_id=workspace_id, artifact_type="cv_statement_plan",
            payload={"schema_version": "cv-statement-plan.v0", "statements": "not-a-list"},
            content_id="cvstatementplan_bad",
        )
        with pytest.raises((CvStatementReviewError, Exception)):
            list_cv_statement_review_items(conn, malformed["id"])


class TestSaveReviewDecision:
    def test_saves_decision_via_generic_review_layer(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [_statement("stmt_a", "professional_summary", "A")])
        decision = _ack(conn, workspace_id, plan["id"], "stmt_a")
        assert decision["review_item_type"] == "cv_statement_v2"
        assert decision["source_artifact_id"] == plan["id"]
        assert decision["domain_item_id"] == "stmt_a"
        assert decision["disposition"] == AUTHORIZED_DISPOSITION

    def test_rejects_unknown_statement_id(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [_statement("stmt_a", "professional_summary", "A")])
        with pytest.raises(CvStatementReviewError, match="not a reviewable statement"):
            _ack(conn, workspace_id, plan["id"], "stmt_nonexistent")

    def test_rejects_unknown_disposition(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [_statement("stmt_a", "professional_summary", "A")])
        with pytest.raises(CvStatementReviewError, match="unknown review disposition"):
            save_cv_statement_review_decision(
                conn, workspace_id, statement_plan_artifact_id=plan["id"],
                statement_id="stmt_a", disposition="not_a_real_disposition",
            )

    def test_rejects_decision_against_nonexistent_artifact(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        with pytest.raises(CvStatementReviewError, match="not found"):
            save_cv_statement_review_decision(
                conn, workspace_id, statement_plan_artifact_id="art_nonexistent",
                statement_id="stmt_a", disposition=AUTHORIZED_DISPOSITION,
            )

    def test_rejects_decision_against_wrong_artifact_type(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        wrong = save_artifact(
            conn, workspace_id=workspace_id, artifact_type="cv_content_plan",
            payload={"schema_version": "cv-content-plan.v0"}, content_id="cvcontentplan_A",
        )
        with pytest.raises(CvStatementReviewError, match="not a cv_statement_plan"):
            save_cv_statement_review_decision(
                conn, workspace_id, statement_plan_artifact_id=wrong["id"],
                statement_id="stmt_a", disposition=AUTHORIZED_DISPOSITION,
            )


class TestResolveReviewState:
    def test_all_authorized(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [
            _statement("stmt_a", "professional_summary", "A"),
            _statement("stmt_b", "skills", "B"),
        ])
        _ack(conn, workspace_id, plan["id"], "stmt_a")
        _ack(conn, workspace_id, plan["id"], "stmt_b")
        state = resolve_cv_statement_review_state(conn, workspace_id, plan["id"])
        assert state == {"authorized": ["stmt_a", "stmt_b"], "omitted": [], "pending": []}

    def test_mixed_authorized_omitted_pending(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [
            _statement("stmt_a", "professional_summary", "A"),
            _statement("stmt_b", "skills", "B"),
            _statement("stmt_c", "skills", "C"),
        ])
        _ack(conn, workspace_id, plan["id"], "stmt_a")
        _omit(conn, workspace_id, plan["id"], "stmt_b")
        state = resolve_cv_statement_review_state(conn, workspace_id, plan["id"])
        assert state == {"authorized": ["stmt_a"], "omitted": ["stmt_b"], "pending": ["stmt_c"]}


class TestGetReviewAuthorizedStatementPlan:
    def test_all_reviewed_projection_succeeds(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [
            _statement("stmt_a", "professional_summary", "Kept statement."),
            _statement("stmt_b", "skills", "Dropped statement."),
        ])
        _ack(conn, workspace_id, plan["id"], "stmt_a")
        _omit(conn, workspace_id, plan["id"], "stmt_b")

        result = get_review_authorized_cv_statement_plan(
            conn, workspace_id, statement_plan_artifact_id=plan["id"],
        )

        assert result["statement_plan_artifact_id"] == plan["id"]
        assert [s["statement_id"] for s in result["projected_statement_plan"]["statements"]] == ["stmt_a"]
        assert result["authorized_statement_ids"] == ["stmt_a"]
        assert result["omitted_statement_ids"] == ["stmt_b"]
        assert len(result["consulted_decision_ids"]) == 2

    def test_no_decisions_fails_closed(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [_statement("stmt_a", "professional_summary", "A")])
        with pytest.raises(CvReviewProjectionError):
            get_review_authorized_cv_statement_plan(conn, workspace_id, statement_plan_artifact_id=plan["id"])

    def test_some_reviewed_one_pending_fails_closed(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [
            _statement("stmt_a", "professional_summary", "A"),
            _statement("stmt_b", "skills", "B"),
        ])
        _ack(conn, workspace_id, plan["id"], "stmt_a")
        with pytest.raises(CvReviewProjectionError):
            get_review_authorized_cv_statement_plan(conn, workspace_id, statement_plan_artifact_id=plan["id"])

    def test_original_order_preserved_in_projection(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [
            _statement("stmt_c", "skills", "C"),
            _statement("stmt_a", "professional_summary", "A"),
            _statement("stmt_b", "skills", "B"),
        ])
        for sid in ("stmt_c", "stmt_a", "stmt_b"):
            _ack(conn, workspace_id, plan["id"], sid)
        result = get_review_authorized_cv_statement_plan(conn, workspace_id, statement_plan_artifact_id=plan["id"])
        assert [s["statement_id"] for s in result["projected_statement_plan"]["statements"]] == [
            "stmt_c", "stmt_a", "stmt_b",
        ]

    def test_source_plan_not_mutated(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [_statement("stmt_a", "professional_summary", "A")])
        _ack(conn, workspace_id, plan["id"], "stmt_a")
        before = copy.deepcopy(plan["payload"])
        get_review_authorized_cv_statement_plan(conn, workspace_id, statement_plan_artifact_id=plan["id"])
        from webapp.persistence.artifacts import get_artifact

        after = get_artifact(conn, plan["id"])
        assert after["payload"] == before

    def test_repeated_calls_return_identical_logical_results(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [_statement("stmt_a", "professional_summary", "A")])
        _ack(conn, workspace_id, plan["id"], "stmt_a")
        first = get_review_authorized_cv_statement_plan(conn, workspace_id, statement_plan_artifact_id=plan["id"])
        second = get_review_authorized_cv_statement_plan(conn, workspace_id, statement_plan_artifact_id=plan["id"])
        assert first == second


class TestNoHistoricalAuthorizationLeakage:
    def test_authorization_from_plan_a_does_not_authorize_plan_b_same_statement_id(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan_a = _seed_statement_plan(
            conn, workspace_id, [_statement("stmt_123", "professional_summary", "Led project A.")],
            content_id="cvstatementplan_A",
        )
        _ack(conn, workspace_id, plan_a["id"], "stmt_123")

        plan_b = _seed_statement_plan(
            conn, workspace_id, [_statement("stmt_123", "professional_summary", "Led project A.")],
            content_id="cvstatementplan_B",
        )
        with pytest.raises(CvReviewProjectionError):
            get_review_authorized_cv_statement_plan(conn, workspace_id, statement_plan_artifact_id=plan_b["id"])

    def test_same_text_under_a_different_statement_id_is_not_authorized(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [
            _statement("stmt_original", "professional_summary", "Led project A."),
            _statement("stmt_lookalike", "professional_summary", "Led project A."),
        ])
        _ack(conn, workspace_id, plan["id"], "stmt_original")
        with pytest.raises(CvReviewProjectionError):
            # stmt_lookalike is still pending despite identical text to an
            # authorized statement -- authorization must never transfer by text.
            get_review_authorized_cv_statement_plan(conn, workspace_id, statement_plan_artifact_id=plan["id"])
        _omit(conn, workspace_id, plan["id"], "stmt_lookalike")
        result = get_review_authorized_cv_statement_plan(conn, workspace_id, statement_plan_artifact_id=plan["id"])
        assert result["authorized_statement_ids"] == ["stmt_original"]

    def test_asking_for_a_newer_unreviewed_plan_never_falls_back_to_an_older_reviewed_one(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan_a = _seed_statement_plan(
            conn, workspace_id, [_statement("stmt_a", "professional_summary", "A")],
            content_id="cvstatementplan_A",
        )
        _ack(conn, workspace_id, plan_a["id"], "stmt_a")
        get_review_authorized_cv_statement_plan(conn, workspace_id, statement_plan_artifact_id=plan_a["id"])

        plan_b = _seed_statement_plan(
            conn, workspace_id, [_statement("stmt_a", "professional_summary", "A")],
            content_id="cvstatementplan_B",
        )
        with pytest.raises(CvReviewProjectionError):
            get_review_authorized_cv_statement_plan(conn, workspace_id, statement_plan_artifact_id=plan_b["id"])


class TestDecisionSupersession:
    def test_acknowledged_then_omitted_effective_result_is_omitted(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [_statement("stmt_a", "professional_summary", "A")])
        older = _ack(conn, workspace_id, plan["id"], "stmt_a")
        newer = _omit(conn, workspace_id, plan["id"], "stmt_a")
        conn.execute("UPDATE review_decisions SET created_at=? WHERE id=?", ("2026-08-24T10:00:00+00:00", older["id"]))
        conn.execute("UPDATE review_decisions SET created_at=? WHERE id=?", ("2026-08-24T10:01:00+00:00", newer["id"]))
        conn.commit()

        state = resolve_cv_statement_review_state(conn, workspace_id, plan["id"])
        assert state == {"authorized": [], "omitted": ["stmt_a"], "pending": []}
        # Review is complete (every reviewable statement has an effective
        # decision) even though nothing is authorized -- review_complete !=
        # all_authorized. Projection succeeds; it just authorizes nothing.
        result = get_review_authorized_cv_statement_plan(
            conn, workspace_id, statement_plan_artifact_id=plan["id"],
        )
        assert result["authorized_statement_ids"] == []
        assert result["omitted_statement_ids"] == ["stmt_a"]

    def test_omitted_then_acknowledged_effective_result_is_authorized(self, tmp_path):
        conn, workspace_id = _workspace(tmp_path)
        plan = _seed_statement_plan(conn, workspace_id, [_statement("stmt_a", "professional_summary", "A")])
        older = _omit(conn, workspace_id, plan["id"], "stmt_a")
        newer = _ack(conn, workspace_id, plan["id"], "stmt_a")
        conn.execute("UPDATE review_decisions SET created_at=? WHERE id=?", ("2026-08-24T10:00:00+00:00", older["id"]))
        conn.execute("UPDATE review_decisions SET created_at=? WHERE id=?", ("2026-08-24T10:01:00+00:00", newer["id"]))
        conn.commit()

        state = resolve_cv_statement_review_state(conn, workspace_id, plan["id"])
        assert state == {"authorized": ["stmt_a"], "omitted": [], "pending": []}
        result = get_review_authorized_cv_statement_plan(conn, workspace_id, statement_plan_artifact_id=plan["id"])
        assert result["authorized_statement_ids"] == ["stmt_a"]
