"""Bundle 6D-A acceptance (spec §18 criterion 17, §19): the real 6C
workflow with CV-v2 enabled and fake providers, then the whole review and
approval lifecycle through the HTTP routes a user's browser calls."""
from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from webapp.persistence import review_approval as ra
from webapp.persistence.db import connect
from webapp.services.autonomy_providers import ProviderSet
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, NOW, enable_prepare, portal_job
from tests.webapp.services.review_fixtures import docx_bytes
from tests.webapp.test_autonomy_prepare_acceptance import (
    ENVELOPES, Clock, _policy, answer_needs_user, catch_up, discover_and_wake, promoted_workspace, quiesce,
    zero_fill_submit,
)


@pytest.fixture
def journey(tmp_path):
    from tests.webapp.fixtures.acceptance.fixtures import completion_ready_content_units, provider_candidate
    from tests.webapp.test_full_journey_acceptance import (
        AIFake, FakeSemanticProposalAdapter, UnderstandingFake, _close, _install_extension, _install_profile,
        _settings,
    )
    from webapp.app import create_app
    base = dataclasses.replace(_settings(tmp_path), cv_quality_v2_enabled=True)
    _install_extension(base)
    client = TestClient(create_app(base))
    client.__enter__()
    _install_profile(base)
    conn = connect(base.db_path)
    enable_prepare(conn)
    _policy(conn)
    settings = dataclasses.replace(
        base, autonomy_max_capability="PREPARE", autonomy_scheduler_enabled=True,
        autonomy_step_cost_max={k: Decimal(v) for k, v in ENVELOPES.items()})
    providers = ProviderSet(UnderstandingFake(provider_candidate()),
                            FakeSemanticProposalAdapter(canned_response={"matches": [], "gates": []}),
                            AIFake({"content_units": completion_ready_content_units()}))
    client.app.state.settings = settings
    j = SimpleNamespace(client=client, conn=conn, settings=settings, providers=providers, clock=Clock(NOW))
    yield j
    conn.close()
    _close(client)


def _api(ws):
    return f"/api/workspaces/{ws}"


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def _review(j, ws):
    return _ok(j.client.get(f"{_api(ws)}/review/state"))


def _revision(j, ws, kind):
    from webapp.persistence.application_documents import get_selection
    return get_selection(j.conn, ws, kind, account_id=ACCOUNT)["revision"]


def _events(j, ws, name):
    return [e for e in ra.events(j.conn, ws) if e["event"] == name]


def _prepare(j):
    """6C prepares; with CV-v2 on it stops for the user's document selection."""
    out = discover_and_wake(j, [portal_job("g1", company="Grounded Co")])
    quiesce(j)
    ws = promoted_workspace(j.conn, out["candidate_ids"][0])
    answer_needs_user(j, ws)
    catch_up(j)
    quiesce(j)
    return ws


def _submit_refused(j, ws):
    from product.autonomy_contract import Capability
    from webapp.services.autonomy import SubmissionNotAvailable, pre_click_commit, request_grant
    with pytest.raises(SubmissionNotAvailable):
        request_grant(j.conn, settings=j.settings, account_id=ACCOUNT, application_workspace_id=ws,
                      stage=Capability.SUBMIT, now=NOW, fill_manifest=None)
    with pytest.raises(SubmissionNotAvailable):
        pre_click_commit(j.conn, settings=j.settings, grant_id="grant_none", verification={}, now=NOW)
    zero_fill_submit(j.conn)


def _approve(j, ws):
    shown = _review(j, ws)["binding_hash"]
    return j.client.post(f"{_api(ws)}/review/approve", json={"displayed_binding_hash": shown}), shown


def _acknowledge_all(j, ws):
    for w in _review(j, ws)["reviewable"]["warnings"]:
        if w["level"] == "ATTENTION" and not w["acknowledged"]:
            _ok(j.client.post(f"{_api(ws)}/review/warnings/ack", json={"warning_key": w["key"]}))


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _replace(j, ws, kind, text):
    return _ok(j.client.post(f"{_api(ws)}/review/documents/{kind}", data={"expected_revision": _revision(j, ws, kind)},
                             files={"file": (f"{kind}.docx", docx_bytes(text), DOCX)}))


def test_review_and_approval_lifecycle_on_the_real_workflow(journey):
    import hashlib
    from tests.webapp.services.review_fixtures import blocker
    from webapp.services import review_approval as svc
    from webapp.services.review_application import review_state
    j = journey

    # 1. 6C prepares; with CV-v2 on, the user generates and selects the documents.
    ws = _prepare(j)
    generated = _ok(j.client.post(f"{_api(ws)}/application-documents/generate"), 201)
    for row in generated["documents"]:
        _ok(j.client.put(f"{_api(ws)}/application-documents/selection/{row['document_kind']}",
                         json={"document_version_id": row["id"], "expected_revision": 0}))
    _ok(j.client.post(f"{_api(ws)}/review/save"))
    # The employer asks a question the profile cannot answer (a governing requirement).
    blocker(j.conn, ws, "employment.notice_period", "What is your notice period?")
    j.conn.commit()
    _submit_refused(j, ws)

    # 2. The page is presented at the exact hash it shows.
    page = j.client.get(f"/workspaces/{ws}/review")
    assert page.status_code == 200 and "Approve for filling" in page.text and "Submit application" not in page.text
    first = _review(j, ws)
    assert first["state"]["state"] == "READY_FOR_REVIEW" and first["view_mode"]["mode"] == "first_review"
    assert [e["binding_hash"] for e in _events(j, ws, "REVIEW_PRESENTED")] == [first["binding_hash"]]

    # 3. Download the exact CV, replace it with an edited DOCX, then Save changes.
    cv = next(d for d in first["reviewable"]["documents"] if d["kind"] == "cv")
    download = j.client.get(f"{_api(ws)}/application-documents/{cv['document_version_id']}/download")
    assert download.status_code == 200 and hashlib.sha256(download.content).hexdigest() == cv["sha256"]
    replaced = _replace(j, ws, "cv", "My edited CV")
    assert _review(j, ws)["binding_hash"] is None  # not yet saved as the exact pack
    _ok(j.client.post(f"{_api(ws)}/review/save"))
    after_save = _review(j, ws)
    cv_now = next(d for d in after_save["reviewable"]["documents"] if d["kind"] == "cv")
    assert cv_now["document_version_id"] == replaced["document_version_id"] and cv_now["origin"] == "user_uploaded"
    preview = _ok(j.client.get(f"{_api(ws)}/review/documents/cv/preview"))
    assert [p["text"] for p in preview["paragraphs"]] == ["My edited CV"]

    # 4. Answer the missing required field; Leave blank the optional one.
    fields = {f["answer_key"]: f for f in after_save["reviewable"]["fields"]}
    missing = [k for k, f in fields.items() if f["required"] and f["display_value"] is None]
    assert missing == ["subject:employment.notice_period"]
    _ok(j.client.post(f"{_api(ws)}/review/answers",
                      json={"answer_key": missing[0], "value": "1 month", "reach": "ACCOUNT"}))
    optional = [k for k, f in fields.items() if not f["required"] and f["disposition"] is None]
    assert optional == ["contact:location"]
    _ok(j.client.post(f"{_api(ws)}/review/fields/contact:location/disposition", json={"disposition": "OMIT"}))

    # 5. Acknowledge the ATTENTION warnings (the user-managed CV).
    attention = [w for w in _review(j, ws)["reviewable"]["warnings"] if w["level"] == "ATTENTION"]
    assert [w["key"].split(":", 1)[0] for w in attention] == ["user_managed"]
    refused, _ = _approve(j, ws)
    assert refused.status_code == 409 and refused.json()["detail"] == "unacknowledged_attention"
    _acknowledge_all(j, ws)

    # 6. Pause automation, then approve: it succeeds and is effective.
    _ok(j.client.post("/api/autonomy/pause", json={"scope_type": "APPLICATION", "scope_id": ws, "reason": "review"}))
    response, shown = _approve(j, ws)
    assert _ok(response)["binding_hash"] == shown
    state = _review(j, ws)["state"]
    assert state["state"] == "APPROVED_FOR_FILL" and state["approval_effective"]
    _submit_refused(j, ws)

    # 7. Replacing the cover letter needs a full review naming that document.
    _replace(j, ws, "cover_letter", "My edited cover letter")
    changed = _review(j, ws)
    assert changed["state"]["state"] == "NEEDS_REVIEW" and not changed["state"]["approval_effective"]
    assert changed["view_mode"]["mode"] == "full"
    assert "document:cover_letter" in changed["view_mode"]["changed_sections"]

    # 8. Save, acknowledge the new user-managed letter, re-approve.
    _ok(j.client.post(f"{_api(ws)}/review/save"))
    _acknowledge_all(j, ws)
    _ok(_approve(j, ws)[0])
    assert _review(j, ws)["state"]["approval_effective"]

    # 9. A new question during filling: delta-only review, answer it, re-approve.
    delta = _ok(j.client.post(f"{_api(ws)}/review/deltas", json={
        "kind": "NEW_QUESTION", "subject": "employment.availability_start", "required": True,
        "question": "When could you start?", "observed": {"field_key": "q_auth"},
        "source": "FILL_SESSION:s1"}), 201)
    delta_view = _review(j, ws)
    assert delta_view["state"]["state"] == "NEEDS_REVIEW" and delta_view["view_mode"]["mode"] == "delta_only"
    assert delta_view["view_mode"]["delta_keys"] == [delta["answer_key"]]
    _ok(j.client.post(f"{_api(ws)}/review/answers",
                      json={"answer_key": delta["answer_key"], "value": "2026-11-01", "reach": "ACCOUNT"}))
    _ok(_approve(j, ws)[0])
    assert [e["detail"]["delta_id"] for e in _events(j, ws, "DELTA_RESOLVED")] == [delta["id"]]
    assert ra.open_deltas(j.conn, ws) == [] and _review(j, ws)["state"]["approval_effective"]

    # 10. Revoke; then a fresh approval expires on the clock.
    _ok(j.client.post(f"{_api(ws)}/review/revoke"))
    assert not _review(j, ws)["state"]["approval_effective"]
    _ok(_approve(j, ws)[0])
    later = NOW + timedelta(days=j.settings.review_approval_ttl_days + 400)
    assert svc.record_invalidation_if_needed(j.conn, settings=j.settings, account_id=ACCOUNT,
                                             application_workspace_id=ws, now=later)
    assert len(_events(j, ws, "EXPIRED")) == 1
    expired = review_state(j.conn, settings=j.settings, account_id=ACCOUNT, application_workspace_id=ws, now=later)
    assert not expired.approval_effective and "expired" in expired.reasons

    assert len(_events(j, ws, "APPROVED")) == 4
    _submit_refused(j, ws)
