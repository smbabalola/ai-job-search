"""Phase 2B-5A: HTTP wiring for CV Quality v2.

Thin-adapter coverage only: each route delegates to the existing CV-v2
services, operates on exact caller-pinned artifact IDs, never exposes another
workspace's/account's artifacts, honours the existing submission lock, and the
generic review endpoint can no longer inject cv_statement_v2 decisions.
Deeper grounding/immutability behaviour is covered by
tests/webapp/test_cv_generation_v2_acceptance.py.
"""
from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.persistence.accounts import create_account
from webapp.persistence.artifacts import get_artifact, list_artifact_history
from webapp.persistence.db import connect
from webapp.persistence.review import list_review_decisions
from webapp.persistence.workspaces import create_workspace

from tests.webapp.fixtures.acceptance.fixtures import completion_ready_content_units
from tests.webapp.test_full_journey_acceptance import (
    _build_chain,
    _close,
    _decide_current_review_surface,
)

USE = "acknowledged_and_proceed"
LEAVE_OUT = "omit_from_positioning"


def _chain(tmp_path):
    client, app, settings, workspace_id = _build_chain(
        tmp_path, ai_units=completion_ready_content_units(),
    )
    _decide_current_review_surface(client, workspace_id)
    return client, settings, workspace_id


def _base(workspace_id: str) -> str:
    return f"/api/workspaces/{workspace_id}/cv-v2"


def _start(client, workspace_id) -> str:
    response = client.post(f"{_base(workspace_id)}/plans")
    assert response.status_code == 201, response.text
    return response.json()["statement_plan_artifact_id"]


def _decide_all(client, workspace_id, plan_id, *, omit_first=False) -> list[dict]:
    items = client.get(f"{_base(workspace_id)}/plans/{plan_id}").json()["items"]
    for index, item in enumerate(items):
        disposition = LEAVE_OUT if omit_first and index == 0 else USE
        response = client.post(
            f"{_base(workspace_id)}/plans/{plan_id}/decisions",
            json={"statement_id": item["statement_id"], "disposition": disposition},
        )
        assert response.status_code == 201, response.text
    return items


def _build(client, workspace_id, plan_id) -> str:
    response = client.post(f"{_base(workspace_id)}/plans/{plan_id}/basis")
    assert response.status_code == 201, response.text
    return response.json()["basis_artifact_id"]


def test_start_review_build_and_generate_from_exact_basis(tmp_path):
    client, settings, workspace_id = _chain(tmp_path)
    try:
        started = client.post(f"{_base(workspace_id)}/plans")
        assert started.status_code == 201, started.text
        body = started.json()
        plan_id = body["statement_plan_artifact_id"]
        assert body["review_url"] == f"/workspaces/{workspace_id}/cv-v2/{plan_id}"

        listed = client.get(f"{_base(workspace_id)}/plans/{plan_id}")
        assert listed.status_code == 200, listed.text
        view = listed.json()
        assert view["statement_plan_artifact_id"] == plan_id
        assert view["items"], "expected reviewable Task 2 statements"
        assert all(item["decision"] is None for item in view["items"])
        assert view["state"]["pending"] == [item["statement_id"] for item in view["items"]]
        assert view["bases"] == []

        items = _decide_all(client, workspace_id, plan_id, omit_first=True)
        view = client.get(f"{_base(workspace_id)}/plans/{plan_id}").json()
        assert view["state"]["pending"] == []
        assert view["state"]["omitted"] == [items[0]["statement_id"]]
        assert view["items"][0]["decision"] == "omitted"
        assert all(item["decision"] == "authorized" for item in view["items"][1:])

        basis_id = _build(client, workspace_id, plan_id)
        conn = connect(settings.db_path)
        try:
            basis = get_artifact(conn, basis_id)
        finally:
            conn.close()
        assert basis["artifact_type"] == "cv_generation_basis"
        assert basis["payload"]["review"]["statement_plan_artifact_id"] == plan_id
        assert basis["payload"]["review"]["omitted_statement_ids"] == [items[0]["statement_id"]]
        view = client.get(f"{_base(workspace_id)}/plans/{plan_id}").json()
        assert [item["basis_artifact_id"] for item in view["bases"]] == [basis_id]

        generated = client.post(
            f"/api/workspaces/{workspace_id}/application-documents/generate",
            json={"cv_generation_basis_artifact_id": basis_id},
        )
        assert generated.status_code == 201, generated.text
        payload = generated.json()["generation_artifact"]["payload"]
        assert payload["schema_version"] == "application-document-generation.v2"
        assert payload["cv_generation_basis"]["artifact_id"] == basis_id
    finally:
        _close(client)


def test_a_newer_basis_is_listed_without_mutating_the_older_one(tmp_path):
    client, settings, workspace_id = _chain(tmp_path)
    try:
        plan_id = _start(client, workspace_id)
        _decide_all(client, workspace_id, plan_id)
        first = _build(client, workspace_id, plan_id)
        conn = connect(settings.db_path)
        try:
            first_before = get_artifact(conn, first)
        finally:
            conn.close()
        _decide_all(client, workspace_id, plan_id, omit_first=True)
        second = _build(client, workspace_id, plan_id)

        # A second plan's bases never leak into this plan's list.
        other_plan = _start(client, workspace_id)
        _decide_all(client, workspace_id, other_plan)
        _build(client, workspace_id, other_plan)

        view = client.get(f"{_base(workspace_id)}/plans/{plan_id}").json()
        assert [item["basis_artifact_id"] for item in view["bases"]] == [second, first]
        conn = connect(settings.db_path)
        try:
            assert get_artifact(conn, first) == first_before
        finally:
            conn.close()
    finally:
        _close(client)


def test_build_is_rejected_while_review_is_pending(tmp_path):
    client, settings, workspace_id = _chain(tmp_path)
    try:
        plan_id = _start(client, workspace_id)
        items = client.get(f"{_base(workspace_id)}/plans/{plan_id}").json()["items"]
        client.post(
            f"{_base(workspace_id)}/plans/{plan_id}/decisions",
            json={"statement_id": items[0]["statement_id"], "disposition": USE},
        )
        if len(items) == 1:  # ensure something is still pending
            raise AssertionError("fixture must yield at least two statements")
        response = client.post(f"{_base(workspace_id)}/plans/{plan_id}/basis")
        assert response.status_code == 400, response.text
        conn = connect(settings.db_path)
        try:
            assert list_artifact_history(conn, workspace_id, "cv_generation_basis") == []
        finally:
            conn.close()
    finally:
        _close(client)


def test_bad_disposition_and_unknown_statement_are_rejected(tmp_path):
    client, _, workspace_id = _chain(tmp_path)
    try:
        plan_id = _start(client, workspace_id)
        items = client.get(f"{_base(workspace_id)}/plans/{plan_id}").json()["items"]
        bad_disposition = client.post(
            f"{_base(workspace_id)}/plans/{plan_id}/decisions",
            json={"statement_id": items[0]["statement_id"], "disposition": "approve_everything"},
        )
        assert bad_disposition.status_code == 400
        unknown = client.post(
            f"{_base(workspace_id)}/plans/{plan_id}/decisions",
            json={"statement_id": "stmt_does_not_exist", "disposition": USE},
        )
        assert unknown.status_code == 400
        extra_field = client.post(
            f"{_base(workspace_id)}/plans/{plan_id}/decisions",
            json={"statement_id": items[0]["statement_id"], "disposition": USE, "workspace_id": "forged"},
        )
        assert extra_field.status_code == 422
        view = client.get(f"{_base(workspace_id)}/plans/{plan_id}").json()
        assert view["state"]["pending"] == [item["statement_id"] for item in items]
    finally:
        _close(client)


def test_unknown_and_wrong_type_plan_ids_are_not_found(tmp_path):
    client, _, workspace_id = _chain(tmp_path)
    try:
        assert client.get(f"{_base(workspace_id)}/plans/art_missing").status_code == 404
        review = client.get(f"/api/workspaces/{workspace_id}/review").json()
        fit_id = review["job_fit_result"]["id"]
        assert client.get(f"{_base(workspace_id)}/plans/{fit_id}").status_code == 404
        assert client.post(f"{_base(workspace_id)}/plans/{fit_id}/basis").status_code == 404
    finally:
        _close(client)


def test_plan_and_basis_from_another_workspace_are_not_found(tmp_path):
    client, settings, workspace_id = _chain(tmp_path)
    try:
        plan_id = _start(client, workspace_id)
        items = _decide_all(client, workspace_id, plan_id)
        basis_id = _build(client, workspace_id, plan_id)
        conn = connect(settings.db_path)
        try:
            other_id = create_workspace(conn, company="Other", title="Role")["id"]
        finally:
            conn.close()

        assert client.get(f"{_base(other_id)}/plans/{plan_id}").status_code == 404
        assert client.post(
            f"{_base(other_id)}/plans/{plan_id}/decisions",
            json={"statement_id": items[0]["statement_id"], "disposition": LEAVE_OUT},
        ).status_code == 404
        assert client.post(f"{_base(other_id)}/plans/{plan_id}/basis").status_code == 404
        generated = client.post(
            f"/api/workspaces/{other_id}/application-documents/generate",
            json={"cv_generation_basis_artifact_id": basis_id},
        )
        assert generated.status_code == 404
        assert workspace_id not in generated.text

        conn = connect(settings.db_path)
        try:
            assert list_review_decisions(conn, other_id, plan_id) == []
            assert list_artifact_history(conn, other_id, "cv_generation_basis") == []
        finally:
            conn.close()
    finally:
        _close(client)


def test_another_accounts_workspace_and_plan_are_not_found(tmp_path):
    client, settings, workspace_id = _chain(tmp_path)
    try:
        plan_id = _start(client, workspace_id)
        items = _decide_all(client, workspace_id, plan_id)
        basis_id = _build(client, workspace_id, plan_id)
        conn = connect(settings.db_path)
        try:
            create_account(conn, account_id="account_b", display_name="B")
            b_workspace = create_workspace(conn, company="B Co", title="Role", account_id="account_b")["id"]
        finally:
            conn.close()

        b_app = create_app(dataclasses.replace(settings, account_id="account_b"))
        with TestClient(b_app) as b_client:
            # Account A's own workspace is invisible to account B ...
            assert b_client.post(f"{_base(workspace_id)}/plans").status_code == 404
            assert b_client.get(f"{_base(workspace_id)}/plans/{plan_id}").status_code == 404
            assert b_client.post(
                f"{_base(workspace_id)}/plans/{plan_id}/decisions",
                json={"statement_id": items[0]["statement_id"], "disposition": LEAVE_OUT},
            ).status_code == 404
            assert b_client.post(f"{_base(workspace_id)}/plans/{plan_id}/basis").status_code == 404
            assert b_client.post(
                f"/api/workspaces/{workspace_id}/application-documents/generate",
                json={"cv_generation_basis_artifact_id": basis_id},
            ).status_code == 404
            # ... and A's exact IDs are invisible through B's own workspace.
            assert b_client.get(f"{_base(b_workspace)}/plans/{plan_id}").status_code == 404
            assert b_client.post(f"{_base(b_workspace)}/plans/{plan_id}/basis").status_code == 404
            assert b_client.post(
                f"/api/workspaces/{b_workspace}/application-documents/generate",
                json={"cv_generation_basis_artifact_id": basis_id},
            ).status_code == 404
            assert b_client.get(f"/workspaces/{workspace_id}/cv-v2/{plan_id}").status_code == 404
            assert b_client.get(f"/workspaces/{b_workspace}/cv-v2/{plan_id}").status_code == 404

        conn = connect(settings.db_path)
        try:
            decisions = list_review_decisions(conn, workspace_id, plan_id)
            assert all(item["disposition"] == USE for item in decisions)
        finally:
            conn.close()
    finally:
        _close(client)


def test_generate_without_basis_remains_legacy_v1(tmp_path):
    client, _, workspace_id = _chain(tmp_path)
    try:
        for kwargs in ({}, {"json": {}}, {"json": {"cv_generation_basis_artifact_id": None}}):
            response = client.post(
                f"/api/workspaces/{workspace_id}/application-documents/generate", **kwargs,
            )
            assert response.status_code == 201, response.text
            payload = response.json()["generation_artifact"]["payload"]
            assert payload["schema_version"] == "application-document-generation.v1"
            assert "cv_generation_basis" not in payload
        forged = client.post(
            f"/api/workspaces/{workspace_id}/application-documents/generate",
            json={"account_id": "forged"},
        )
        assert forged.status_code == 422
    finally:
        _close(client)


def test_generic_review_endpoint_cannot_create_cv_statement_decisions(tmp_path):
    client, settings, workspace_id = _chain(tmp_path)
    try:
        plan_id = _start(client, workspace_id)
        items = client.get(f"{_base(workspace_id)}/plans/{plan_id}").json()["items"]
        forged = {
            "review_item_type": "cv_statement_v2", "source_artifact_id": plan_id,
            "domain_item_id": items[0]["statement_id"], "disposition": USE,
        }
        single = client.post(f"/api/workspaces/{workspace_id}/review-decisions", json=forged)
        assert single.status_code == 400
        # An unknown statement ID would never pass the dedicated endpoint either.
        unknown = client.post(
            f"/api/workspaces/{workspace_id}/review-decisions",
            json={**forged, "domain_item_id": "stmt_injected"},
        )
        assert unknown.status_code == 400

        review = client.get(f"/api/workspaces/{workspace_id}/review").json()
        legitimate = {
            "review_item_type": "content_unit",
            "source_artifact_id": review["application_intelligence_result"]["id"],
            "domain_item_id": "cv_1", "disposition": USE,
        }
        batch = client.post(
            f"/api/workspaces/{workspace_id}/review-decisions/batch",
            json={"decisions": [legitimate, forged]},
        )
        assert batch.status_code == 400

        conn = connect(settings.db_path)
        try:
            assert list_review_decisions(conn, workspace_id, plan_id) == []
        finally:
            conn.close()
        view = client.get(f"{_base(workspace_id)}/plans/{plan_id}").json()
        assert view["state"]["authorized"] == []

        # Legitimate generic review decisions still work.
        assert client.post(
            f"/api/workspaces/{workspace_id}/review-decisions", json=legitimate,
        ).status_code == 201
    finally:
        _close(client)


def test_submitted_application_rejects_every_cv_v2_mutation(tmp_path):
    client, settings, workspace_id = _chain(tmp_path)
    try:
        plan_id = _start(client, workspace_id)
        items = _decide_all(client, workspace_id, plan_id)
        basis_id = _build(client, workspace_id, plan_id)
        generated = client.post(
            f"/api/workspaces/{workspace_id}/application-documents/generate",
            json={"cv_generation_basis_artifact_id": basis_id},
        ).json()
        revisions = {}
        for row in generated["documents"]:
            selected = client.put(
                f"/api/workspaces/{workspace_id}/application-documents/selection/{row['document_kind']}",
                json={"document_version_id": row["id"], "expected_revision": 0},
            )
            assert selected.status_code == 200, selected.text
            revisions[row["document_kind"]] = selected.json()["revision"]
        confirmed = client.post(
            f"/api/workspaces/{workspace_id}/application-pack",
            json={"confirmed": True, "effective_date": "2026-09-22", "document_selection_revisions": revisions},
        )
        assert confirmed.status_code == 201, confirmed.text
        applied = client.patch(
            f"/api/workspaces/{workspace_id}/status",
            json={"new_status": "applied", "effective_date": "2026-09-22"},
        )
        assert applied.status_code == 200, applied.text

        conn = connect(settings.db_path)
        try:
            plans_before = len(list_artifact_history(conn, workspace_id, "cv_statement_plan"))
            decisions_before = len(list_review_decisions(conn, workspace_id, plan_id))
        finally:
            conn.close()

        assert client.post(f"{_base(workspace_id)}/plans").status_code == 400
        assert client.post(
            f"{_base(workspace_id)}/plans/{plan_id}/decisions",
            json={"statement_id": items[0]["statement_id"], "disposition": LEAVE_OUT},
        ).status_code == 400
        assert client.post(f"{_base(workspace_id)}/plans/{plan_id}/basis").status_code == 400
        assert client.post(
            f"/api/workspaces/{workspace_id}/application-documents/generate",
            json={"cv_generation_basis_artifact_id": basis_id},
        ).status_code == 400
        # Reading the exact reviewed history stays available.
        assert client.get(f"{_base(workspace_id)}/plans/{plan_id}").status_code == 200

        conn = connect(settings.db_path)
        try:
            assert len(list_artifact_history(conn, workspace_id, "cv_statement_plan")) == plans_before
            assert len(list_review_decisions(conn, workspace_id, plan_id)) == decisions_before
            assert len(list_artifact_history(conn, workspace_id, "cv_generation_basis")) == 1
        finally:
            conn.close()

        page = client.get(f"/workspaces/{workspace_id}/cv-v2/{plan_id}")
        assert page.status_code == 200
        assert "cv-v2-decision" not in page.text
        assert "cv-v2-build" not in page.text
        workspace_page = client.get(f"/workspaces/{workspace_id}")
        assert "cv-v2-start" not in workspace_page.text
    finally:
        _close(client)


def test_review_page_renders_exact_plan_controls(tmp_path):
    client, _, workspace_id = _chain(tmp_path)
    try:
        workspace_page = client.get(f"/workspaces/{workspace_id}")
        assert workspace_page.status_code == 200
        assert "Start CV Quality v2 review" in workspace_page.text

        plan_id = _start(client, workspace_id)
        items = client.get(f"{_base(workspace_id)}/plans/{plan_id}").json()["items"]
        page = client.get(f"/workspaces/{workspace_id}/cv-v2/{plan_id}")
        assert page.status_code == 200, page.text
        for item in items:
            assert item["statement_id"] in page.text
        assert f'data-plan-id="{plan_id}"' in page.text
        assert f"{len(items)} pending" in page.text
        assert 'class="button cv-v2-build"' in page.text and "disabled" in page.text

        _decide_all(client, workspace_id, plan_id)
        basis_id = _build(client, workspace_id, plan_id)
        page = client.get(f"/workspaces/{workspace_id}/cv-v2/{plan_id}")
        assert "0 pending" in page.text
        assert f'data-basis-id="{basis_id}"' in page.text
        assert client.get(f"/workspaces/{workspace_id}/cv-v2/art_missing").status_code == 404
    finally:
        _close(client)
