"""Database-backed fixed-window rate limits (Bundle 7 spec A8).

One atomic upsert per hit, so the limit holds across web instances and under
concurrency on both dialects. The counter is committed on its own (a refused
request must still count), unless the caller already has a transaction open."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from webapp.persistence import dbapi

# name -> (limit, window seconds), spec A8 verbatim
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "signup": (5, 3600),           # per IP
    "login": (10, 900),            # per IP + email
    "password_reset": (5, 3600),   # per email
    "verify_resend": (5, 3600),    # per user (keyed by email before sign-in)
    "pairing_code": (10, 3600),    # per user
    "token_refresh": (60, 3600),   # per device
    "ai_start": (30, 60),          # per account
}


MAX_BUSY_RETRIES = 20


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after: int


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("RATE_LIMITED")
        self.retry_after = retry_after


def _window_start(now: datetime, window_seconds: int) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % window_seconds, tz=timezone.utc)


def hit(conn: dbapi.Connection, *, key: str, limit: int, window_seconds: int, now: datetime) -> RateDecision:
    start = _window_start(now, window_seconds)
    caller_transaction = conn.in_transaction
    for attempt in range(MAX_BUSY_RETRIES + 1):
        try:
            row = conn.execute(
                "INSERT INTO rate_limit_buckets (key, window_start, count) VALUES (?, ?, 1) "
                "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limit_buckets.count + 1 "
                "RETURNING count",
                (key, start.isoformat()),
            ).fetchone()
            if not caller_transaction:
                conn.commit()
            break
        except dbapi.DatabaseBusy:
            # Concurrent hits on one counter under REPEATABLE READ: the loser
            # retries in a fresh transaction (never inside the caller's).
            if caller_transaction or attempt == MAX_BUSY_RETRIES:
                raise
            conn.rollback()
    retry_after = max(1, int((start + timedelta(seconds=window_seconds) - now).total_seconds()))
    return RateDecision(allowed=row["count"] <= limit, retry_after=retry_after)


def enforce(conn: dbapi.Connection, name: str, subject: str, *, now: datetime) -> None:
    """Raise RateLimited when ``subject`` exceeded the named limit."""
    limit, window = RATE_LIMITS[name]
    decision = hit(conn, key=f"{name}:{subject}", limit=limit, window_seconds=window, now=now)
    if not decision.allowed:
        raise RateLimited(decision.retry_after)
