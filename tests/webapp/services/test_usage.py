"""Bundle 7 spec §13: the usage reservation ledger. Review Focus 2: concurrent
prepare of one workspace takes one reservation and consumes at most one unit."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from product.entitlements import load_catalog, parse_catalog
from webapp.config import Settings
from webapp.persistence import dbapi, identity
from webapp.persistence.db import connect, init_db
from webapp.services.entitlements import EntitlementGate
from webapp.services.ownership import AccountScope
from webapp.services.usage import AllowanceExhausted, Metering, UsageService, prepare_key, sweep_expired
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
PLANS = Path(__file__).parents[3] / "product" / "plans"
DEV = load_catalog(PLANS / "plan-catalog.dev.json")  # Free: applications.prepare 3


def _catalog(**free_allowances):
    doc = json.loads((PLANS / "plan-catalog.dev.json").read_text(encoding="utf-8"))
    doc["catalog_version"] = "test-usage"
    for key, limit in free_allowances.items():
        doc["plans"]["free"]["allowances"][key]["limit"] = limit
    return parse_catalog(doc)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    conn = connect(path)
    created = identity.create_user_with_account(
        conn, email="ada@example.com", password_hash="h", display_name="Ada", legal_document_ids=[],
        now=NOW, profile_store=DatabaseProfileSourceStore())
    conn.commit()
    scope = AccountScope(account_id=created["account"]["id"], profile_root=tmp_path, user_id=created["user"]["id"])
    settings = Settings(db_path=path)
    yield path, conn, scope, settings
    conn.close()


def _service(settings, catalog=DEV):
    return UsageService(EntitlementGate(catalog, settings=settings))


def _reserve(service, conn, scope, key="k1", *, allowance="applications.prepare", now=NOW):
    with dbapi.account_transaction(conn, scope.account_id):
        return service.reserve(conn, scope, allowance=allowance, subject_type="workspace", subject_id="ws",
                               idempotency_key=key, now=now)


def _statuses(conn):
    return [tuple(r) for r in conn.execute("SELECT idempotency_key, status FROM usage_reservations ORDER BY reserved_at, id")]


def test_reserve_then_consume_and_reserve_then_release(world):
    _, conn, scope, settings = world
    service = _service(settings)
    first = _reserve(service, conn, scope, "k1")
    second = _reserve(service, conn, scope, "k2")
    assert first.created and first.status == "RESERVED"
    with dbapi.account_transaction(conn, scope.account_id):
        assert service.consume(conn, first.id, settlement_ref="ws", now=NOW)
        assert service.release(conn, second.id, now=NOW)
        assert not service.consume(conn, second.id, settlement_ref="ws", now=NOW)  # released stays released
    assert sorted(_statuses(conn)) == [("k1", "CONSUMED"), ("k2", "RELEASED")]


def test_the_trigger_refuses_every_other_transition(world):
    _, conn, scope, settings = world
    service = _service(settings)
    reservation = _reserve(service, conn, scope)
    with dbapi.account_transaction(conn, scope.account_id):
        service.consume(conn, reservation.id, settlement_ref="ws", now=NOW)
    for sql in ("UPDATE usage_reservations SET status = 'RELEASED' WHERE id = ?",
                "UPDATE usage_reservations SET status = 'RESERVED', settled_at = NULL WHERE id = ?"):
        with pytest.raises(dbapi.IntegrityError, match="RESERVED to CONSUMED or RELEASED"):
            conn.execute(sql, (reservation.id,))
        conn.rollback()
    other = _reserve(service, conn, scope, "k2")
    with pytest.raises(dbapi.IntegrityError, match="RESERVED to CONSUMED or RELEASED"):
        conn.execute("UPDATE usage_reservations SET amount = 5 WHERE id = ?", (other.id,))
    conn.rollback()


def test_a_null_limit_is_exhausted_at_zero(world):
    _, conn, scope, settings = world
    service = _service(settings, _catalog(**{"applications.prepare": None}))
    with pytest.raises(AllowanceExhausted) as exc_info:
        _reserve(service, conn, scope)
    assert (exc_info.value.used, exc_info.value.limit) == (0, 0)
    assert not conn.in_transaction
    assert _statuses(conn) == []


def test_the_same_key_returns_the_same_reservation(world):
    _, conn, scope, settings = world
    service = _service(settings)
    first = _reserve(service, conn, scope, "same")
    again = _reserve(service, conn, scope, "same")
    assert again.id == first.id and not again.created
    assert conn.execute("SELECT COUNT(*) FROM usage_reservations").fetchone()[0] == 1


def test_the_limit_counts_reserved_and_consumed_but_not_released(world):
    _, conn, scope, settings = world
    service = _service(settings)  # Free: 3
    a, b, c = (_reserve(service, conn, scope, key) for key in ("a", "b", "c"))
    with pytest.raises(AllowanceExhausted) as exc_info:
        _reserve(service, conn, scope, "d")
    assert (exc_info.value.used, exc_info.value.limit) == (3, 3)
    assert exc_info.value.window_end == datetime(2026, 11, 1, tzinfo=timezone.utc)
    with dbapi.account_transaction(conn, scope.account_id):
        service.release(conn, c.id, now=NOW)
    assert _reserve(service, conn, scope, "d").created


def test_expired_reservations_are_swept_and_stop_counting(world):
    _, conn, scope, settings = world
    service = _service(settings)
    for key in ("a", "b", "c"):
        _reserve(service, conn, scope, key)
    assert sweep_expired(conn, NOW + timedelta(minutes=29)) == 0
    assert sweep_expired(conn, NOW + timedelta(minutes=30)) == 3
    assert {status for _, status in _statuses(conn)} == {"RELEASED"}
    # reserve also releases this account's expired rows before counting
    for key in ("d", "e", "f"):
        _reserve(service, conn, scope, key, now=NOW + timedelta(hours=1))
    assert _reserve(service, conn, scope, "g", now=NOW + timedelta(hours=2)).created


def test_the_window_turns_over_at_the_first_second_of_the_month(world):
    _, conn, scope, settings = world
    service = _service(settings)
    last_second = datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)
    for key in ("a", "b", "c"):
        with dbapi.account_transaction(conn, scope.account_id):
            r = service.reserve(conn, scope, allowance="applications.prepare", subject_type="workspace",
                                subject_id="ws", idempotency_key=key, now=last_second)
            service.consume(conn, r.id, settlement_ref="ws", now=last_second)
    with pytest.raises(AllowanceExhausted):
        _reserve(service, conn, scope, "d", now=last_second)
    fresh = _reserve(service, conn, scope, "d", now=last_second + timedelta(seconds=1))
    assert fresh.window_key == "month:2026-11"


def test_gauges_and_summary(world):
    _, conn, scope, settings = world
    service = _service(settings, _catalog(**{"storage.bytes": 100}))
    service.gauge_check(conn, scope, allowance="storage.bytes", adding=100, now=NOW)
    with pytest.raises(AllowanceExhausted):
        service.gauge_check(conn, scope, allowance="storage.bytes", adding=101, now=NOW)
    _reserve(service, conn, scope)
    summary = {row["allowance"]: row for row in service.summary(conn, scope, now=NOW)}
    assert "ai.cost_micro_usd" not in summary  # hidden
    assert summary["applications.prepare"]["used"] == 1 and summary["applications.prepare"]["limit"] == 3
    assert summary["storage.bytes"] == {"allowance": "storage.bytes", "used": 0, "limit": 100, "gauge": True,
                                        "window_end": "2026-11-01T00:00:00+00:00"}


def test_reserve_refuses_gauges_and_outside_a_transaction(world):
    _, conn, scope, settings = world
    service = _service(settings)
    with pytest.raises(ValueError):
        _reserve(service, conn, scope, allowance="storage.bytes")
    with pytest.raises(dbapi.OperationalError):
        service.reserve(conn, scope, allowance="applications.prepare", subject_type="workspace", subject_id="ws",
                        idempotency_key="k", now=NOW)


# ---- Metering.prepare: the stage wrapper --------------------------------------

def _metering(settings, catalog=DEV):
    gate = EntitlementGate(catalog, settings=settings)
    return Metering(gate, UsageService(gate), enforced=True, clock=Clock(NOW))


def test_a_failed_stage_releases_and_the_next_success_consumes_once(world):
    _, conn, scope, settings = world
    metering = _metering(settings)

    def fail():
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        metering.prepare(conn, scope, "ws_1", fail)
    assert _statuses(conn) == [(prepare_key("ws_1", "month:2026-10"), "RELEASED")]
    assert metering.prepare(conn, scope, "ws_1", lambda: "understood") == "understood"
    assert metering.prepare(conn, scope, "ws_1", lambda: "fitted") == "fitted"  # later stage: no new charge
    assert metering.prepare(conn, scope, "ws_1", lambda: "rerun") == "rerun"
    assert sorted(s for _, s in _statuses(conn)) == ["CONSUMED", "RELEASED"]  # one charge in the window


def test_unenforced_metering_records_nothing(world):
    _, conn, scope, settings = world
    gate = EntitlementGate(_catalog(**{"applications.prepare": 0}), settings=settings)
    metering = Metering(gate, UsageService(gate), enforced=False)
    assert metering.prepare(conn, scope, "ws_1", lambda: "ok") == "ok"
    metering.require_feature(conn, scope, "ai.cv_tailor")
    assert _statuses(conn) == []


def _run_threads(path, scope, settings, target_for):
    results: list[object] = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker(i):
        conn = connect(path)
        try:
            barrier.wait()
            outcome = target_for(i, conn)
        except BaseException as exc:  # noqa: BLE001
            outcome = exc
        finally:
            conn.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_twenty_concurrent_prepares_of_one_workspace_take_one_reservation(world):
    """Review Focus 2 (double-click, two tabs)."""
    path, conn, scope, settings = world
    metering = _metering(settings)

    def work():
        time.sleep(0.01)
        return "done"

    results = _run_threads(path, scope, settings, lambda i, c: metering.prepare(c, scope, "ws_same", work))
    assert results == ["done"] * 20
    assert _statuses(conn) == [(prepare_key("ws_same", "month:2026-10"), "CONSUMED")]


def test_twenty_workspaces_racing_for_the_last_unit_let_exactly_one_through(world):
    path, conn, scope, settings = world
    metering = _metering(settings)
    for ws in ("ws_a", "ws_b"):
        metering.prepare(conn, scope, ws, lambda: None)  # 2 of 3 used
    results = _run_threads(path, scope, settings,
                           lambda i, c: metering.prepare(c, scope, f"ws_race_{i}", lambda: "done"))
    assert results.count("done") == 1
    assert all(isinstance(r, AllowanceExhausted) for r in results if r != "done")
    assert conn.execute("SELECT COUNT(*) FROM usage_reservations WHERE status = 'CONSUMED'").fetchone()[0] == 3


def test_two_simultaneous_reservations_cannot_overspend_the_last_unit(world):
    """The account lock is held from before the first protected read: two
    reservations started at the same instant both see the other's commit."""
    path, conn, scope, settings = world
    service = _service(settings)
    for key in ("a", "b"):
        _reserve(service, conn, scope, key)  # 2 of 3 used
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def reserve(key):
        own = connect(path)
        try:
            barrier.wait()
            with dbapi.account_transaction(own, scope.account_id):
                reservation = service.reserve(own, scope, allowance="applications.prepare", subject_type="workspace",
                                              subject_id=key, idempotency_key=key, now=NOW)
                time.sleep(0.05)  # hold the lock across the read-then-insert window
            outcomes.append(reservation)
        except AllowanceExhausted as exc:
            outcomes.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=reserve, args=(key,)) for key in ("x", "y")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(type(o).__name__ for o in outcomes) == ["AllowanceExhausted", "Reservation"]
    assert conn.execute("SELECT COUNT(*) FROM usage_reservations WHERE status = 'RESERVED'").fetchone()[0] == 3
