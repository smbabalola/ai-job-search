"""Bundle 7 spec §10.7: ``account_transaction`` — the per-account lock new
invariants use instead of the transitional global writer lock. It STARTS the
transaction, so on PostgreSQL the lock is held before the REPEATABLE READ
snapshot is taken and the holder always sees the previous holder's commit."""
from __future__ import annotations

import threading
import time

import pytest

from webapp.persistence import dbapi
from webapp.persistence.db import connect, init_db


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    conn = connect(path)
    conn.execute("CREATE TABLE counters (account_id TEXT PRIMARY KEY, n INTEGER NOT NULL)")
    conn.execute("INSERT INTO counters VALUES ('a', 0)")
    conn.execute("INSERT INTO counters VALUES ('b', 0)")
    conn.commit()
    conn.close()
    return path


def test_it_commits_on_success_and_rolls_back_on_error(db):
    conn = connect(db)
    with dbapi.account_transaction(conn, "a"):
        conn.execute("UPDATE counters SET n = 1 WHERE account_id = 'a'")
    with pytest.raises(RuntimeError):
        with dbapi.account_transaction(conn, "a"):
            conn.execute("UPDATE counters SET n = 99 WHERE account_id = 'a'")
            raise RuntimeError("boom")
    assert conn.execute("SELECT n FROM counters WHERE account_id = 'a'").fetchone()[0] == 1
    assert not conn.in_transaction
    conn.close()


def test_it_refuses_to_join_an_open_transaction(db):
    conn = connect(db)
    conn.execute("UPDATE counters SET n = 5 WHERE account_id = 'b'")
    assert conn.in_transaction
    with pytest.raises(dbapi.OperationalError):
        with dbapi.account_transaction(conn, "a"):
            pass
    conn.rollback()
    conn.close()


def test_concurrent_increments_under_the_account_lock_never_lose_an_update(db):
    """Read-modify-write under the lock: 12 threads x 5 increments == 60."""
    errors: list[BaseException] = []

    def work():
        conn = connect(db)
        try:
            for _ in range(5):
                with dbapi.account_transaction(conn, "a"):
                    n = conn.execute("SELECT n FROM counters WHERE account_id = 'a'").fetchone()[0]
                    time.sleep(0.001)
                    conn.execute("UPDATE counters SET n = ? WHERE account_id = 'a'", (n + 1,))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=work) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    conn = connect(db)
    assert conn.execute("SELECT n FROM counters WHERE account_id = 'a'").fetchone()[0] == 60
    conn.close()


@pytest.mark.postgres_only
def test_other_accounts_are_not_blocked_and_the_wait_is_bounded(db):
    holder, other, waiter = connect(db), connect(db), dbapi.connect(db, writer_lock_timeout_ms=1000)
    try:
        with dbapi.account_transaction(holder, "a"):
            holder.execute("UPDATE counters SET n = n + 1 WHERE account_id = 'a'")
            with dbapi.account_transaction(other, "b"):  # a different account proceeds at once
                other.execute("UPDATE counters SET n = n + 1 WHERE account_id = 'b'")
            started = time.monotonic()
            with pytest.raises(dbapi.DatabaseBusy) as exc_info:
                with dbapi.account_transaction(waiter, "a"):
                    pass
            assert exc_info.value.reason == "account_lock_timeout"
            assert time.monotonic() - started < 5
        assert not waiter.in_transaction
    finally:
        for conn in (holder, other, waiter):
            conn.close()
