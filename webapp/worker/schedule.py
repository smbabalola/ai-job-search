"""Periodic jobs (Bundle 7 spec §20.2): each kind is enqueued once per time
slot, deduplicated by ``periodic:{kind}:{slot}``."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from webapp.persistence import dbapi
from webapp.worker.runner import enqueue

# kind → period in seconds. Retention expiry (Task 28) is scheduled under
# account.purge there: the spec's closed kind list has no retention kind.
PERIODIC: tuple[tuple[str, int], ...] = (
    ("outbox.dispatch", 60),  # wakes retries whose backoff elapsed; enqueue also kicks it at once
    ("usage.sweep", 5 * 60),
    ("tokens.sweep", 60 * 60),
    ("notify.approval_expiry_scan", 60 * 60),
    ("notify.digest", 60 * 60),  # hourly slot; the handler sends each account's digest at its local 07:00
)


def _slot(now: datetime, period: int) -> int:
    return int(now.timestamp()) // period


def enqueue_periodic(conn: dbapi.Connection, *, settings: Any, now: datetime) -> None:
    """No commit. Hosted mode also ticks the 6C driver (it never runs in the web process there)."""
    schedule = list(PERIODIC)
    if settings.is_hosted:
        schedule.append(("autonomy.tick", max(1, int(settings.autonomy_tick_interval))))
    for kind, period in schedule:
        enqueue(conn, kind=kind, payload={}, dedupe_key=f"periodic:{kind}:{_slot(now, period)}", max_attempts=3,
                now=now)
