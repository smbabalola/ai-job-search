# tests/webapp/persistence/test_review_approval_persistence.py
from __future__ import annotations

from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401
from webapp.persistence import review_approval as ra


def _approve(conn, ws, h, supersedes=None):
    return ra.insert_approval(conn, account_id=ACCOUNT, application_workspace_id=ws, binding={"h": h},
                              binding_hash=h, supersedes_id=supersedes, batch_id=None, resolved_delta_ids=[],
                              actor="u", now=NOW)


def test_latest_approval_is_by_seq_and_revocation_is_an_event(conn):
    ws = make_workspace(conn)
    a = _approve(conn, ws, "sha256:a")
    b = _approve(conn, ws, "sha256:b", a["id"])  # identical timestamp: seq decides
    latest = ra.latest_approval(conn, ws)
    assert latest["id"] == b["id"] and latest["binding"] == {"h": "sha256:b"}
    assert not ra.approval_revoked(conn, b)
    ra.record_event(conn, account_id=ACCOUNT, application_workspace_id=ws, event="REVOKED", binding_hash="sha256:b",
                    detail={"approval_id": b["id"]}, actor="u", now=NOW)
    assert ra.approval_revoked(conn, b) and not ra.approval_revoked(conn, a)


def test_presented_and_acknowledgements_are_exact(conn):
    ws = make_workspace(conn)
    ra.record_event(conn, account_id=ACCOUNT, application_workspace_id=ws, event="REVIEW_PRESENTED",
                    binding_hash="sha256:a", detail={}, actor="u", now=NOW)
    ra.record_event(conn, account_id=ACCOUNT, application_workspace_id=ws, event="WARNING_ACKNOWLEDGED",
                    binding_hash="sha256:a", detail={"warning_key": "w1"}, actor="u", now=NOW)
    assert ra.presented_at(conn, ws, "sha256:a") and not ra.presented_at(conn, ws, "sha256:b")
    assert ra.acknowledged_warning_keys(conn, ws) == frozenset({"w1"})


def test_deltas_open_until_resolved_and_dispositions_latest_wins(conn):
    ws = make_workspace(conn)
    d = ra.insert_delta(conn, account_id=ACCOUNT, application_workspace_id=ws, kind="NEW_QUESTION",
                        answer_key="subject:notice_period", subject="notice_period", required=True,
                        question="Notice period?", observed={"label": "Notice"}, source="FILL_SESSION:s1", now=NOW)
    assert [x["id"] for x in ra.open_deltas(conn, ws)] == [d["id"]]
    ra.record_event(conn, account_id=ACCOUNT, application_workspace_id=ws, event="DELTA_RESOLVED",
                    binding_hash="sha256:c", detail={"delta_id": d["id"]}, actor="u", now=NOW)
    assert ra.open_deltas(conn, ws) == []
    for disposition in ("OMIT", "ANSWER"):
        ra.set_disposition(conn, account_id=ACCOUNT, application_workspace_id=ws, answer_key="k",
                           disposition=disposition, actor="u", now=NOW)
    assert ra.current_dispositions(conn, ws) == {"k": "ANSWER"}


def test_invalidation_recorded_is_per_approval_and_exact_reason_set(conn):
    ws = make_workspace(conn)
    a = _approve(conn, ws, "sha256:a")
    ra.record_event(conn, account_id=ACCOUNT, application_workspace_id=ws, event="APPROVAL_INVALIDATED",
                    binding_hash="sha256:n",
                    detail={"approval_id": a["id"], "reasons": ["binding_changed", "field:k"],
                            "previous_hash": "sha256:a", "current_hash": "sha256:n"}, actor="system", now=NOW)
    # the reason set alone deduplicates: order-insensitive, hash-insensitive
    assert ra.invalidation_recorded(conn, ws, a["id"], ["field:k", "binding_changed"])
    assert not ra.invalidation_recorded(conn, ws, a["id"], ["revoked"])
