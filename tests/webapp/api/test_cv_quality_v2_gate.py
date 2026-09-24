"""CV Quality v2 is off by default in the product.

Its candidate-facing CV is not yet presentable (no name/contact header, no
role headings, and a visible "Evidence Gaps" section), so every HTTP/UI entry
point is gated behind ``Settings.cv_quality_v2_enabled`` until that is fixed.
The CV-v2 services and the legacy document path are unchanged. These tests
build real CV-v2 artifacts with the gate on, then prove a default-configured
app exposes none of it while legacy generation still works.
"""
from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.config import Settings

from tests.webapp.fixtures.acceptance.fixtures import completion_ready_content_units
from tests.webapp.test_full_journey_acceptance import (
    _build_chain,
    _close,
    _decide_current_review_surface,
)

ENV = "JOBSEARCH_ENABLE_CV_QUALITY_V2"


def test_cv_quality_v2_is_disabled_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert Settings().cv_quality_v2_enabled is False
    monkeypatch.setenv(ENV, "0")
    assert Settings().cv_quality_v2_enabled is False
    monkeypatch.setenv(ENV, "1")
    assert Settings().cv_quality_v2_enabled is True


def _enabled_chain_with_plan_and_basis(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    client, app, settings, workspace_id = _build_chain(
        tmp_path, ai_units=completion_ready_content_units(),
    )
    _decide_current_review_surface(client, workspace_id)
    base = f"/api/workspaces/{workspace_id}/cv-v2"
    plan_id = client.post(f"{base}/plans").json()["statement_plan_artifact_id"]
    for item in client.get(f"{base}/plans/{plan_id}").json()["items"]:
        client.post(
            f"{base}/plans/{plan_id}/decisions",
            json={"statement_id": item["statement_id"], "disposition": "acknowledged_and_proceed"},
        )
    basis_id = client.post(f"{base}/plans/{plan_id}/basis").json()["basis_artifact_id"]
    return client, settings, workspace_id, plan_id, basis_id


def test_default_app_exposes_no_cv_quality_v2_entry_point(tmp_path, monkeypatch):
    enabled_client, settings, workspace_id, plan_id, basis_id = (
        _enabled_chain_with_plan_and_basis(tmp_path, monkeypatch)
    )
    try:
        assert "cv-v2-start" in enabled_client.get(f"/workspaces/{workspace_id}").text

        monkeypatch.delenv(ENV)
        default_settings = dataclasses.replace(settings, cv_quality_v2_enabled=Settings().cv_quality_v2_enabled)
        default_app = create_app(default_settings)
        default_app.state.application_intelligence_provider = (
            enabled_client.app.state.application_intelligence_provider
        )
        with TestClient(default_app) as client:
            page = client.get(f"/workspaces/{workspace_id}")
            assert page.status_code == 200
            assert "cv-v2-start" not in page.text
            assert "CV Quality v2" not in page.text

            base = f"/api/workspaces/{workspace_id}/cv-v2"
            assert client.post(f"{base}/plans").status_code == 404
            assert client.get(f"{base}/plans/{plan_id}").status_code == 404
            assert client.post(
                f"{base}/plans/{plan_id}/decisions",
                json={"statement_id": "any", "disposition": "acknowledged_and_proceed"},
            ).status_code == 404
            assert client.post(f"{base}/plans/{plan_id}/basis").status_code == 404
            assert client.get(f"/workspaces/{workspace_id}/cv-v2/{plan_id}").status_code == 404

            generate = f"/api/workspaces/{workspace_id}/application-documents/generate"
            assert client.post(
                generate, json={"cv_generation_basis_artifact_id": basis_id},
            ).status_code == 404

            legacy = client.post(generate)
            assert legacy.status_code == 201, legacy.text
            payload = legacy.json()["generation_artifact"]["payload"]
            assert "renderer_version" in payload
            assert "cv_generation_basis" not in str(payload)
    finally:
        _close(enabled_client)
