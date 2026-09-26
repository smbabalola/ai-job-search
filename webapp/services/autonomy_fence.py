"""Lease fencing for every DB-visible step result (6C spec §6.3, §10.1).

A step's writes are authoritative only while its worker still holds the
lease (holder, generation, unexpired). Paid steps run on their own thread and
connection wrapped in a FencedConnection: every commit re-checks the lease
inside the open write transaction (the write lock is held, so no other worker
can change the lease between the check and the commit) and rolls back when it
is gone. The scheduler waits at most the step's hard timeout; on timeout it
supersedes its own lease generation, so the abandoned call can never commit
afterwards."""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable

from webapp.persistence import autonomy_prepare as ap
from webapp.persistence.db import connect


class LeaseLost(Exception):
    """The worker no longer holds the lease: nothing it does may commit."""


class StepTimeout(TimeoutError):
    """The step exceeded its hard timeout (a genuine transient failure)."""


Fence = Callable[[Any], bool]


def lease_fence(*, queue: str, item_id: str, worker_id: str, generation: int,
                clock: Callable[[], datetime]) -> Fence:
    def held(conn) -> bool:
        return ap.lease_is_held(conn, queue=queue, item_id=item_id, worker_id=worker_id, generation=generation,
                                now=clock())
    return held


class FencedConnection:
    """Forwards everything to a real connection except commit, which first
    verifies the lease inside the pending write transaction."""

    def __init__(self, conn, fence: Fence):
        self._conn, self._fence = conn, fence

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def commit(self) -> None:
        if self._conn.in_transaction and not self._fence(self._conn):
            self._conn.rollback()
            raise LeaseLost("the lease was lost; the step's writes were rolled back")
        self._conn.commit()


def database_file(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


class FencedStep:
    """Runs work(fenced_conn) on its own thread and connection."""

    def __init__(self, db_file: str, fence: Fence, work: Callable[[Any], Any]):
        self._done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None

        def target() -> None:
            conn = connect(db_file)
            try:
                self.result = work(FencedConnection(conn, fence))
            except BaseException as exc:  # noqa: BLE001 - reported to the scheduler
                self.error = exc
            finally:
                conn.close()
                self._done.set()
        self._thread = threading.Thread(target=target, name="autonomy-step", daemon=True)
        self._thread.start()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    @property
    def finished(self) -> bool:
        return self._done.is_set()
