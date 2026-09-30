"""Usage accounting (Bundle 7 spec §13): the reservation ledger, gauges and
``Metering``, the one object the gated entry points of §11.4 call.

A reservation is taken in its own short account transaction before the work,
the work runs outside any lock (it calls AI providers), and the reservation is
consumed on success or released on failure, each in its own account
transaction. Metering is enforced for real accounts (``auth_enabled``); the
local single-user operator account is unmetered.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, TypeVar

from product.entitlements import ALLOWANCES, GAUGE_ALLOWANCES, HIDDEN_ALLOWANCES
from webapp.persistence import dbapi
from webapp.persistence import usage as rows

__all__ = [
    "AllowanceExhausted", "GAUGE_READERS", "Metering", "Reservation", "UsageService", "prepare_key",
    "sweep_expired",
]

logger = logging.getLogger("webapp.usage")

RESERVATION_TTL = timedelta(minutes=30)
T = TypeVar("T")


class AllowanceExhausted(Exception):
    def __init__(self, allowance: str, used: int, limit: int, window_end: datetime):
        super().__init__(f"{allowance} allowance exhausted ({used} of {limit} used)")
        self.allowance = allowance
        self.used = used
        self.limit = limit
        self.window_end = window_end


class ActionInProgress(Exception):
    """The same logical metered action is already executing (§13.2 single
    flight): the duplicate is refused rather than run a second time."""

    def __init__(self, action_key: str, retry_after: int = 5):
        super().__init__(f"{action_key} is already in progress")
        self.action_key = action_key
        self.retry_after = retry_after


@dataclass(frozen=True)
class Reservation:
    id: str
    account_id: str
    allowance: str
    amount: int
    idempotency_key: str
    window_key: str
    status: str
    created: bool  # False when an existing reservation was returned for the key

    @classmethod
    def from_row(cls, row: Mapping[str, Any], *, created: bool) -> "Reservation":
        return cls(id=row["id"], account_id=row["account_id"], allowance=row["allowance"], amount=row["amount"],
                   idempotency_key=row["idempotency_key"], window_key=row["window_key"], status=row["status"],
                   created=created)


def prepare_key(workspace_id: str, window_key: str) -> str:
    return f"prepare:{workspace_id}:{window_key}"


# Gauges are computed live from their source tables (§13.2). Later tasks
# register theirs: library.cv_items (Task 21), discovery.scheduled_searches (Task 26).
GAUGE_READERS: dict[str, Callable[[dbapi.Connection, str], int]] = {
    "storage.bytes": rows.storage_bytes,
}


class UsageService:
    def __init__(self, gate: Any) -> None:
        self.gate = gate

    def reserve(self, conn: dbapi.Connection, scope: Any, *, allowance: str, amount: int = 1, subject_type: str,
                subject_id: str, idempotency_key: str, now: datetime) -> Reservation:
        """Inside ``dbapi.account_transaction(conn, scope.account_id)``; does not commit."""
        if allowance not in ALLOWANCES or allowance in GAUGE_ALLOWANCES or allowance in HIDDEN_ALLOWANCES:
            raise ValueError(f"{allowance} is not a reservable allowance")
        if amount <= 0:
            raise ValueError("amount must be positive")
        if not conn.in_transaction:
            raise dbapi.OperationalError("reserve runs inside account_transaction")
        rows.release_expired(conn, account_id=scope.account_id, now=now)
        existing = rows.live_by_key(conn, idempotency_key)
        if existing is not None:
            if existing["account_id"] != scope.account_id or existing["allowance"] != allowance:
                raise ValueError("idempotency key belongs to another reservation")
            return Reservation.from_row(existing, created=False)
        resolved = self.gate.entitlements(conn, scope, now=now)
        limit = resolved.allowances.get(allowance, 0)
        used = rows.used(conn, account_id=scope.account_id, allowance=allowance, window_key=resolved.window.key)
        if used + amount > limit:
            raise AllowanceExhausted(allowance, used, limit, resolved.window.end)
        row = rows.insert(conn, account_id=scope.account_id, allowance=allowance, amount=amount,
                          subject_type=subject_type, subject_id=subject_id, idempotency_key=idempotency_key,
                          window_key=resolved.window.key, now=now, expires_at=now + RESERVATION_TTL)
        return Reservation.from_row(row, created=True)

    def consume(self, conn: dbapi.Connection, reservation_id: str, *, settlement_ref: str | None,
                now: datetime) -> bool:
        if not rows.settle(conn, reservation_id, status="CONSUMED", now=now, settlement_ref=settlement_ref):
            return False
        self._notify_thresholds(conn, reservation_id, now=now)
        return True

    def _notify_thresholds(self, conn: dbapi.Connection, reservation_id: str, *, now: datetime) -> None:
        """§13.3: usage.limit_near at ≥ 80 %, usage.limit_reached at 100 %, once per allowance per window."""
        from types import SimpleNamespace
        from webapp.services.notifications import notify
        row = rows.get(conn, reservation_id)
        resolved = self.gate.entitlements(conn, SimpleNamespace(account_id=row["account_id"]), now=now)
        limit = resolved.allowances.get(row["allowance"]) or 0
        if limit <= 0:
            return
        used = rows.used(conn, account_id=row["account_id"], allowance=row["allowance"], window_key=row["window_key"])
        kind = "usage.limit_reached" if used >= limit else "usage.limit_near" if used * 100 >= 80 * limit else None
        if kind is None:
            return
        notify(conn, account_id=row["account_id"], kind=kind, subject_type="allowance", subject_id=row["allowance"],
               dedupe_key=f"{kind}:{row['allowance']}:{row['window_key']}",
               detail={"allowance": row["allowance"], "used": used, "limit": limit,
                       "window_end": resolved.window.end.date().isoformat()}, now=now)

    def release(self, conn: dbapi.Connection, reservation_id: str, *, now: datetime) -> bool:
        return rows.settle(conn, reservation_id, status="RELEASED", now=now, settlement_ref="released")

    def gauge_check(self, conn: dbapi.Connection, scope: Any, *, allowance: str, adding: int, now: datetime) -> None:
        if allowance not in GAUGE_READERS:
            raise ValueError(f"{allowance} is not a gauge")
        resolved = self.gate.entitlements(conn, scope, now=now)
        limit = resolved.allowances.get(allowance, 0)
        current = GAUGE_READERS[allowance](conn, scope.account_id)
        if current + adding > limit:
            raise AllowanceExhausted(allowance, current, limit, resolved.window.end)

    def summary(self, conn: dbapi.Connection, scope: Any, *, now: datetime) -> list[dict[str, Any]]:
        """Every visible allowance: used / limit / window end (§13.3)."""
        resolved = self.gate.entitlements(conn, scope, now=now)
        out = []
        for allowance in ALLOWANCES:
            if allowance in HIDDEN_ALLOWANCES:
                continue
            gauge = allowance in GAUGE_ALLOWANCES
            if gauge and allowance not in GAUGE_READERS:
                continue
            used = (GAUGE_READERS[allowance](conn, scope.account_id) if gauge else
                    rows.used(conn, account_id=scope.account_id, allowance=allowance,
                              window_key=resolved.window.key))
            out.append({"allowance": allowance, "used": used, "limit": resolved.allowances.get(allowance, 0),
                        "gauge": gauge, "window_end": resolved.window.end.isoformat()})
        return out


def sweep_expired(conn: dbapi.Connection, now: datetime) -> int:
    """The worker's ``usage.sweep``: release RESERVED rows past ``expires_at``,
    one account transaction per account."""
    released = 0
    for account_id in rows.accounts_with_expired(conn, now=now):
        with dbapi.account_transaction(conn, account_id):
            released += rows.release_expired(conn, account_id=account_id, now=now)
    return released


class Metering:
    """The gate and the ledger as the §11.4 entry points use them. Not enforced
    (local single-user mode) → every check passes and nothing is recorded."""

    def __init__(self, gate: Any, usage: UsageService, *, enforced: bool,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.gate = gate
        self.usage = usage
        self.enforced = enforced
        self.clock = clock

    def _now(self) -> datetime:
        from datetime import timezone
        return self.clock() if self.clock else datetime.now(timezone.utc)

    def require_feature(self, conn: dbapi.Connection, scope: Any, feature: str) -> None:
        if self.enforced:
            self.gate.require_feature(conn, scope, feature, now=self._now())

    def gauge_check(self, conn: dbapi.Connection, scope: Any, allowance: str, *, adding: int) -> None:
        if self.enforced:
            self.usage.gauge_check(conn, scope, allowance=allowance, adding=adding, now=self._now())

    def metered(self, conn: dbapi.Connection, scope: Any, *, feature: str, allowance: str, subject_type: str,
                subject_id: str, key: Callable[[str], str], action: str, work: Callable[[], T],
                settlement_ref: Callable[[T], str] | None = None) -> T:
        """Feature check → claim ``action`` and reserve (key built from the
        window key) in one account transaction → work → consume on success /
        release on failure, and finish the claim.

        Single flight: one logical action (``action``) executes at most once at
        a time. A simultaneous duplicate is refused with ``ActionInProgress``
        before any work, so it never causes a second provider execution; so is
        one that arrived while the action ran but got the lock after it
        succeeded (it asked for the same thing, which now exists). A
        claim whose holder died is taken over once its lease (the reservation
        TTL) has passed. A reservation already CONSUMED in this window is reused
        at no charge (a later stage or a rerun, §11.3); one RESERVED by a
        concurrent different action is left to its creator to settle.
        ``settlement_ref`` names what the consumed unit paid for (default: the
        subject), e.g. the discovery run id its candidates are authorized by."""
        if not self.enforced:
            return work()
        now = self._now()
        self.gate.require_feature(conn, scope, feature, now=now)
        with dbapi.account_transaction(conn, scope.account_id):
            claimed_at = self._now()  # after the lock wait; ``now`` is the request's arrival
            claim_id = rows.claim_action(conn, account_id=scope.account_id, action_key=action, now=claimed_at,
                                         lease_expires_at=claimed_at + RESERVATION_TTL, arrived_at=now)
            if claim_id is None:
                raise ActionInProgress(action)
            window_key = self.gate.entitlements(conn, scope, now=now).window.key
            reservation = self.usage.reserve(conn, scope, allowance=allowance, subject_type=subject_type,
                                             subject_id=subject_id, idempotency_key=key(window_key), now=now)
        from webapp.services.metered_provider import cost_buffer, record_cost_events

        with cost_buffer() as spend:  # AI cost events of this action, written when it settles
            try:
                result = work()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()  # the failed stage's uncommitted writes are not kept
                with dbapi.account_transaction(conn, scope.account_id):
                    record_cost_events(conn, spend.events)  # spend incurred before the failure still counts
                    if reservation.created:
                        self.usage.release(conn, reservation.id, now=self._now())
                    rows.finish_action(conn, claim_id, status="FAILED", now=self._now())
                raise
        if conn.in_transaction:
            conn.commit()  # the successful work's own writes
        with dbapi.account_transaction(conn, scope.account_id):
            record_cost_events(conn, spend.events)
            ref = settlement_ref(result) if settlement_ref else subject_id
            if reservation.created and not self.usage.consume(conn, reservation.id, settlement_ref=ref,
                                                              now=self._now()):
                logger.warning("usage_consume_after_release reservation=%s", reservation.id)
            if not rows.finish_action(conn, claim_id, status="SUCCEEDED", now=self._now()):
                logger.warning("metered_action_finished_after_takeover action=%s", action)
        return result

    def prepare(self, conn: dbapi.Connection, scope: Any, workspace_id: str, work: Callable[[], T], *,
                stage: str) -> T:
        """Every AI prepare stage (understanding, fit, intelligence) of §11.3.
        The single-flight action is the stage of the workspace."""
        return self.metered(conn, scope, feature="ai.prepare", allowance="applications.prepare",
                            subject_type="workspace", subject_id=workspace_id,
                            key=lambda window_key: prepare_key(workspace_id, window_key),
                            action=f"prepare:{stage}:{workspace_id}", work=work)
