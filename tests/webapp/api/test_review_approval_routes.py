from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.persistence import review_approval as ra
from tests.webapp.services.review_fixtures import (  # noqa: F401
    NOW, V2_ACCOUNT, diff_counts, docx_bytes, table_counts, v2_chain,
)


@pytest.fixture
def api(v2_chain):
    with TestClient(create_app(v2_chain.settings)) as client:
        yield client, v2_chain


def _review(client, ws):
    r = client.get(f"/api/workspaces/{ws}/review/state")
    assert r.status_code == 200, r.text
    return r.json()


def _make_approvable_via_api(client, world):
    body = _review(client, world.ws)
    for f in body["reviewable"]["fields"]:
        if not f["required"] and f["disposition"] is None:
            r = client.post(f"/api/workspaces/{world.ws}/review/fields/{f['answer_key']}/disposition",
                            json={"disposition": "OMIT"})
            assert r.status_code == 200, r.text
    for w in _review(client, world.ws)["reviewable"]["warnings"]:
        if w["level"] == "ATTENTION" and not w["acknowledged"]:
            r = client.post(f"/api/workspaces/{world.ws}/review/warnings/ack", json={"warning_key": w["key"]})
            assert r.status_code == 200, r.text
    return _review(client, world.ws)


def test_review_data_get_writes_nothing(api):
    client, world = api
    before = table_counts(world.conn)
    body = _review(client, world.ws)
    assert body["binding_hash"] and body["state"]["state"] == "READY_FOR_REVIEW"
    assert diff_counts(before, table_counts(world.conn)) == {}


def test_data_api_get_never_makes_an_item_eligible(api):
    client, world = api
    body = _make_approvable_via_api(client, world)
    r = client.post("/api/applications/approve-selected",
                    json={"items": [{"workspace_id": world.ws, "displayed_binding_hash": body["binding_hash"]}]})
    assert r.status_code == 200 and r.json()["results"][0]["outcome"] == "not_presented"
    assert not [e for e in ra.events(world.conn, world.ws) if e["event"] == "REVIEW_PRESENTED"]


def test_approve_and_revoke_through_the_api(api):
    client, world = api
    body = _make_approvable_via_api(client, world)
    r = client.post(f"/api/workspaces/{world.ws}/review/approve", json={"displayed_binding_hash": body["binding_hash"]})
    assert r.status_code == 200, r.text
    assert _review(client, world.ws)["state"]["state"] == "APPROVED_FOR_FILL"
    again = client.post(f"/api/workspaces/{world.ws}/review/approve",
                        json={"displayed_binding_hash": body["binding_hash"]})
    assert again.status_code == 409 and again.json()["detail"] == "already_approved"
    assert client.post(f"/api/workspaces/{world.ws}/review/revoke").status_code == 200
    assert _review(client, world.ws)["state"]["state"] == "NEEDS_REVIEW"


def test_approve_rejects_extra_fields(api):
    client, world = api
    r = client.post(f"/api/workspaces/{world.ws}/review/approve",
                    json={"displayed_binding_hash": "sha256:x", "acknowledged_warnings": []})
    assert r.status_code == 422


def test_refusals_map_to_409_with_the_reason(api):
    client, world = api
    r = client.post(f"/api/workspaces/{world.ws}/review/approve", json={"displayed_binding_hash": "sha256:not-shown"})
    assert r.status_code == 409 and r.json()["detail"] in ("stale", "blocking", "unacknowledged_attention")
    stale = world.selection("cv")["revision"] - 1
    r = client.post(f"/api/workspaces/{world.ws}/review/documents/cv/select",
                    json={"document_version_id": world.selection("cv")["document_version_id"],
                          "expected_revision": stale})
    assert r.status_code == 409 and r.json()["detail"] == "stale_selection"
    r = client.post(f"/api/workspaces/{world.ws}/review/fields/subject:nope/disposition", json={"disposition": "OMIT"})
    assert r.status_code == 409 and r.json()["detail"] == "unknown_field"


def test_replace_then_save_through_the_api(api):
    client, world = api
    r = client.post(f"/api/workspaces/{world.ws}/review/documents/cv",
                    files={"file": ("mine.docx", docx_bytes("my cv"), "application/octet-stream")},
                    data={"expected_revision": str(world.selection("cv")["revision"])})
    assert r.status_code == 200, r.text
    assert _review(client, world.ws)["binding_hash"] is None  # no approvable hash without the exact pack
    saved = client.post(f"/api/workspaces/{world.ws}/review/save")
    assert saved.status_code == 200 and saved.json()["binding_hash"] == _review(client, world.ws)["binding_hash"]


def test_prepared_list_and_bulk(api):
    client, world = api
    listing = client.get("/api/applications/prepared").json()
    assert [a["workspace_id"] for a in listing["applications"]] == [world.ws]
    assert listing["applications"][0]["state"] == "READY_FOR_REVIEW"


def test_delta_intake_route(api):
    client, world = api
    r = client.post(f"/api/workspaces/{world.ws}/review/deltas",
                    json={"kind": "NEW_QUESTION", "subject": None, "answer_key": None, "required": False,
                          "question": "Anything else?", "observed": {"field_key": "x"}, "source": "FILL_SESSION:1"})
    assert r.status_code == 201, r.text
    assert _review(client, world.ws)["view_mode"]["mode"] == "first_review"


@pytest.mark.parametrize("method,path,body", [
    ("get", "/review/state", None), ("post", "/review/save", None), ("post", "/review/revoke", None),
    ("post", "/review/approve", {"displayed_binding_hash": "x"}),
    ("post", "/review/warnings/ack", {"warning_key": "x"}),
    ("post", "/review/answers", {"answer_key": "x", "value": "v", "reach": "ACCOUNT"}),
    ("post", "/review/fields/x/disposition", {"disposition": "OMIT"}),
    ("post", "/review/documents/cv/select", {"document_version_id": "x", "expected_revision": 1}),
    ("post", "/review/deltas", {"kind": "NEW_QUESTION", "required": False, "question": "q", "observed": {},
                                "source": "s"}),
])
def test_unknown_or_foreign_workspace_is_404(api, method, path, body):
    client, _ = api
    kwargs = {"json": body} if body is not None else {}
    r = getattr(client, method)(f"/api/workspaces/ws_not_mine{path}", **kwargs)
    assert r.status_code == 404, (path, r.status_code, r.text)
