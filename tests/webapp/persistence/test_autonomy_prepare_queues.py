from __future__ import annotations

from datetime import timedelta

from webapp.persistence import autonomy_prepare as ap
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, NOW, conn, make_workspace  # noqa: F401

TTL = timedelta(minutes=12)


def _app(conn):
    ws = make_workspace(conn)
    ap.enqueue_application(conn, application_workspace_id=ws, account_id=ACCOUNT, now=NOW)
    return ws


def test_due_and_lease_generation_increments(conn):
    ws = _app(conn)
    assert [i["item_id"] for i in ap.due_items(conn, queue="APPLICATION", now=NOW, limit=10)] == [ws]
    gen = ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", now=NOW, ttl=TTL)
    assert gen == 1
    assert ap.due_items(conn, queue="APPLICATION", now=NOW, limit=10) == []  # leased
    assert ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w2", now=NOW, ttl=TTL) is None


def test_finalize_requires_holder_generation_and_unexpired_lease(conn):
    ws = _app(conn)
    gen = ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", now=NOW, ttl=TTL)
    assert ap.finalize_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w2", generation=gen, now=NOW,
                             next_eligible_at=None) is False
    assert ap.finalize_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", generation=gen + 1, now=NOW,
                             next_eligible_at=None) is False
    assert ap.finalize_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", generation=gen, now=NOW,
                             next_eligible_at=None) is True
    assert ap.due_items(conn, queue="APPLICATION", now=NOW + timedelta(days=1), limit=10) == []  # dormant


def test_expired_lease_cannot_finalize_even_if_not_retaken(conn):
    ws = _app(conn)
    gen = ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", now=NOW, ttl=TTL)
    late = NOW + TTL + timedelta(seconds=1)
    assert ap.lease_is_held(conn, queue="APPLICATION", item_id=ws, worker_id="w1", generation=gen, now=late) is False
    assert ap.finalize_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", generation=gen, now=late,
                             next_eligible_at=None) is False


def test_expired_lease_is_retaken_with_a_new_generation(conn):
    ws = _app(conn)
    g1 = ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", now=NOW, ttl=TTL)
    g2 = ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w2", now=NOW + TTL + timedelta(seconds=1),
                          ttl=TTL)
    assert g2 == g1 + 1
    assert ap.finalize_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", generation=g1,
                             now=NOW + TTL + timedelta(seconds=2), next_eligible_at=None) is False


def test_wake_reactivates_dormant_items_in_both_queues(conn):
    ws = _app(conn)
    ap.enqueue_candidate(conn, candidate_id="cand_1", account_id=ACCOUNT, search_workspace_id="search_default", now=NOW)
    ap.set_dormant(conn, queue="APPLICATION", item_id=ws, now=NOW)
    ap.set_dormant(conn, queue="CANDIDATE", item_id="cand_1", now=NOW)
    assert ap.due_items(conn, queue="APPLICATION", now=NOW, limit=5) == []
    assert ap.wake_account(conn, account_id=ACCOUNT, now=NOW) == 2
    assert [i["item_id"] for i in ap.due_items(conn, queue="CANDIDATE", now=NOW, limit=5)] == ["cand_1"]


def test_release_keeps_eligibility(conn):
    ws = _app(conn)
    gen = ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", now=NOW, ttl=TTL)
    assert ap.release_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", generation=gen, now=NOW) is True
    assert [i["item_id"] for i in ap.due_items(conn, queue="APPLICATION", now=NOW, limit=5)] == [ws]
