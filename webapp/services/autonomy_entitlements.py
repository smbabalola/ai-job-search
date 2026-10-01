"""What automation an account's plan allows (Bundle 7 spec §11.2, §11.3),
read per account at every 6C tick.

The effective autonomy capability is min(the user's autonomy authorization
(6B), the entitlement (PREPARE with ``automation.prepare``, else NONE), the
deployment ceiling, the ``AUTOMATION_ENABLED`` platform control). Real
accounts are metered; the local single-user operator is not, so for it the
entitlement and platform terms do not apply (6B/6C behave as before)."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from product.autonomy_contract import Capability
from webapp.persistence import dbapi
from webapp.persistence.autonomy_authority import resolve_authority

__all__ = [
    "automation_capability", "entitlement_ceiling", "has_feature", "metering_enforced", "reserve_autonomous_prepare",
    "settle_autonomous_prepare",
]


def metering_enforced(settings: Any) -> bool:
    return bool(settings.auth_enabled)


def _active(conn: dbapi.Connection, account_id: str) -> bool:
    row = conn.execute("SELECT status FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return row is not None and row[0] == "ACTIVE"


def has_feature(conn: dbapi.Connection, account_id: str, feature: str, *, settings: Any, now: datetime) -> bool:
    """An ACTIVE account whose resolved entitlements (plan, grants, platform
    controls) include ``feature``. Always true when unmetered."""
    if not metering_enforced(settings):
        return True
    if not _active(conn, account_id):
        return False
    from webapp.services.entitlements import gate_for
    return gate_for(settings).entitlements(conn, SimpleNamespace(account_id=account_id), now=now).has(feature)


def entitlement_ceiling(conn: dbapi.Connection, account_id: str, *, settings: Any, now: datetime) -> Capability:
    """The plan's automation ceiling: PREPARE for Power, NONE otherwise, NONE
    when AUTOMATION_ENABLED is off (it turns the feature off) or the account
    is not ACTIVE. Unmetered: no ceiling."""
    if not metering_enforced(settings):
        return Capability.SUBMIT
    return Capability.PREPARE if has_feature(conn, account_id, "automation.prepare", settings=settings, now=now) \
        else Capability.NONE


def automation_capability(conn: dbapi.Connection, account_id: str, *, settings: Any, now: datetime) -> Capability:
    account_max, _ = resolve_authority(conn, account_id=account_id, search_workspace_id=None)
    return min(account_max, entitlement_ceiling(conn, account_id, settings=settings, now=now),
               settings.autonomy_deployment_ceiling())


# ---- plan units for autonomous prepares (§11.3) --------------------------------

def reserve_autonomous_prepare(conn: dbapi.Connection, account_id: str, workspace_id: str, *, settings: Any,
                               now: datetime) -> list[str]:
    """Before a 6C paid prepare step: ``applications.prepare`` (the same key as
    a manual prepare, so one unit per application and window) plus
    ``automation.prepare``, in one account transaction; both or neither.
    Returns the reservations this call created (a unit already consumed in
    the window is reused at no charge). Raises AllowanceExhausted. Unmetered:
    nothing."""
    if not metering_enforced(settings):
        return []
    from webapp.services.entitlements import gate_for
    from webapp.services.usage import UsageService, prepare_key
    gate = gate_for(settings)
    usage = UsageService(gate)
    scope = SimpleNamespace(account_id=account_id)
    created: list[str] = []
    with dbapi.account_transaction(conn, account_id):
        window = gate.entitlements(conn, scope, now=now).window.key
        for allowance, key in (("applications.prepare", prepare_key(workspace_id, window)),
                               ("automation.prepare", f"automation.prepare:{workspace_id}:{window}")):
            reservation = usage.reserve(conn, scope, allowance=allowance, subject_type="workspace",
                                        subject_id=workspace_id, idempotency_key=key, now=now)
            if reservation.created:
                created.append(reservation.id)
    return created


def settle_autonomous_prepare(conn: dbapi.Connection, account_id: str, reservation_ids: list[str], *,
                              succeeded: bool, settings: Any, now: datetime) -> None:
    """After the step: consume on success, release on failure. The step
    finishes under the entitlement it started with, even if the plan changed
    meanwhile (a downgrade never strands a reservation)."""
    if not reservation_ids:
        return
    from webapp.services.entitlements import gate_for
    from webapp.services.usage import UsageService
    usage = UsageService(gate_for(settings))
    with dbapi.account_transaction(conn, account_id):
        for reservation_id in reservation_ids:
            if succeeded:
                usage.consume(conn, reservation_id, settlement_ref="autonomy.prepare", now=now)
            else:
                usage.release(conn, reservation_id, now=now)
