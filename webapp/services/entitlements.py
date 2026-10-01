"""The entitlement gate, grants, platform controls and recorded catalog versions
(Bundle 7 spec §11.4, §12.1, §19.3). Resolution itself is pure and lives in
``product.entitlements``; this module only reads its inputs from the database.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from product.entitlements import (
    PLATFORM_CONTROL_KEYS, Catalog, CatalogError, CatalogSet, Entitlements, FeatureNotInPlan, Grant,
    SubscriptionView, effective_entitlements, parse_catalog,
)
from webapp.persistence import dbapi

__all__ = [
    "PLATFORM_CONTROL_KEYS", "CatalogError", "EntitlementGate", "add_grant", "platform_control",
    "record_catalog", "revoke_grant", "set_platform_control",
]

# §19.3: a missing key reads false, except these three in local mode only.
LOCAL_DEFAULT_ON = frozenset({"SIGNUPS_ENABLED", "AI_ENABLED", "DISCOVERY_ENABLED"})

SubscriptionReader = Callable[[dbapi.Connection, str], "SubscriptionView | None"]


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


# ---- platform controls --------------------------------------------------------

def platform_control(conn: dbapi.Connection, key: str, *, settings: Any) -> bool:
    if key not in PLATFORM_CONTROL_KEYS:
        raise ValueError(f"unknown platform control {key}")
    row = conn.execute("SELECT value FROM platform_controls WHERE key = ? ORDER BY seq DESC LIMIT 1",
                       (key,)).fetchone()
    if row is None:
        return key in LOCAL_DEFAULT_ON and not settings.is_hosted
    return bool(row[0])


def platform_controls(conn: dbapi.Connection, *, settings: Any) -> dict[str, bool]:
    return {key: platform_control(conn, key, settings=settings) for key in PLATFORM_CONTROL_KEYS}


def set_platform_control(conn: dbapi.Connection, key: str, value: bool, *, actor_user_id: str | None,
                         reason: str, now: datetime) -> None:
    if key not in PLATFORM_CONTROL_KEYS:
        raise ValueError(f"unknown platform control {key}")
    conn.execute(
        "INSERT INTO platform_controls (id, key, value, actor_user_id, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (f"pctl_{uuid.uuid4().hex[:20]}", key, int(bool(value)), actor_user_id, reason, _iso(now)),
    )


# ---- catalog versions ---------------------------------------------------------

def record_catalog(conn: dbapi.Connection, catalog: Catalog, *, now: datetime) -> None:
    """Record the loaded catalog. A version whose content changed without a new
    catalog_version is refused: subscriptions pin versions (DP-8)."""
    row = conn.execute("SELECT catalog_hash FROM plan_catalog_versions WHERE catalog_version = ?",
                       (catalog.catalog_version,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO plan_catalog_versions (catalog_version, catalog_hash, catalog_json, loaded_at) "
            "VALUES (?, ?, ?, ?)",
            (catalog.catalog_version, catalog.catalog_hash, catalog.catalog_json, _iso(now)),
        )
    elif row[0] != catalog.catalog_hash:
        raise CatalogError([f"catalog_version {catalog.catalog_version} was already recorded with different "
                            "content; publish a new catalog_version instead of editing it"])


def recorded_catalog(conn: dbapi.Connection, catalog_version: str) -> Catalog | None:
    import json

    row = conn.execute("SELECT catalog_json FROM plan_catalog_versions WHERE catalog_version = ?",
                       (catalog_version,)).fetchone()
    return None if row is None else parse_catalog(json.loads(row[0]))


# ---- grants -------------------------------------------------------------------

def add_grant(conn: dbapi.Connection, *, account_id: str, kind: str, reason: str, actor_user_id: str | None,
              starts_at: datetime, expires_at: datetime | None, now: datetime, plan_id: str | None = None,
              allowance: str | None = None, amount: int | None = None) -> str:
    grant_id = f"grant_{uuid.uuid4().hex[:20]}"
    conn.execute(
        "INSERT INTO entitlement_grants (id, account_id, kind, plan_id, allowance, amount, reason, actor_user_id, "
        "starts_at, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (grant_id, account_id, kind, plan_id, allowance, amount, reason, actor_user_id, _iso(starts_at),
         None if expires_at is None else _iso(expires_at), _iso(now)),
    )
    return grant_id


def revoke_grant(conn: dbapi.Connection, grant_id: str, *, now: datetime) -> None:
    conn.execute("UPDATE entitlement_grants SET revoked_at = ? WHERE id = ?", (_iso(now), grant_id))


def account_grants(conn: dbapi.Connection, account_id: str) -> list[Grant]:
    rows = conn.execute(
        "SELECT id, kind, plan_id, allowance, amount, starts_at, expires_at, revoked_at FROM entitlement_grants "
        "WHERE account_id = ? ORDER BY seq", (account_id,)).fetchall()
    return [Grant(id=r[0], kind=r[1], plan_id=r[2], allowance=r[3], amount=r[4], starts_at=_parse(r[5]),
                  expires_at=_parse(r[6]), revoked_at=_parse(r[7])) for r in rows]


# ---- the gate -----------------------------------------------------------------

def _no_subscription(conn: dbapi.Connection, account_id: str) -> SubscriptionView | None:
    return None


def payment_grace(settings: Any) -> timedelta:
    """DP-4: the payment grace period of the deployment's retention policy."""
    from product.retention_policy import load_retention_policy
    from webapp.app import _project_path
    return load_retention_policy(_project_path(settings.retention_policy_path)).payment_grace


class EntitlementGate:
    """Reads an account's inputs and resolves them. ``subscriptions`` reads the
    account's current subscription (Task 14 supplies the billing reader);
    ``grace`` is the DP-4 payment grace period (fail-closed default 0)."""

    def __init__(self, catalog: Catalog, *, settings: Any, subscriptions: SubscriptionReader = _no_subscription,
                 grace: timedelta = timedelta(0)) -> None:
        self.catalog = catalog
        self.settings = settings
        self.subscriptions = subscriptions
        self.grace = grace

    def _catalogs(self, conn: dbapi.Connection, snapshot: SubscriptionView | None) -> CatalogSet:
        pinned: dict[str, Catalog] = {}
        if snapshot is not None and snapshot.catalog_version != self.catalog.catalog_version:
            older = recorded_catalog(conn, snapshot.catalog_version)
            if older is not None:
                pinned[older.catalog_version] = older
        return CatalogSet(current=self.catalog, pinned=pinned)

    def entitlements(self, conn: dbapi.Connection, scope: Any, *, now: datetime) -> Entitlements:
        snapshot = self.subscriptions(conn, scope.account_id)
        return effective_entitlements(
            self._catalogs(conn, snapshot), snapshot, account_grants(conn, scope.account_id),
            platform_controls(conn, settings=self.settings), grace=self.grace, now=now,
        )

    def require_feature(self, conn: dbapi.Connection, scope: Any, feature: str, *, now: datetime) -> Entitlements:
        resolved = self.entitlements(conn, scope, now=now)
        if resolved.has(feature):
            return resolved
        # Switched off by a platform control: no plan would help, so no upgrade is offered.
        plans = self._catalogs(conn, None).current.plans
        in_plan = resolved.plan_id in plans and plans[resolved.plan_id].features.get(feature)
        upgrade_to = None if in_plan else self.catalog.upgrade_for(feature, resolved.plan_id)
        raise FeatureNotInPlan(feature, resolved.plan_id, upgrade_to)


# ---- a gate outside the web app (the worker, the 6C driver) -------------------

_GATES: dict[tuple[str, str], EntitlementGate] = {}


def gate_for(settings: Any) -> EntitlementGate:
    """The same gate the app builds (catalog + billing reader), cached per
    catalog path and database."""
    from product.entitlements import load_catalog
    from webapp.app import _project_path
    from webapp.persistence import billing as billing_rows
    key = (str(settings.plan_catalog_path), str(settings.database_url or settings.db_path))
    gate = _GATES.get(key)
    if gate is None:
        catalog = load_catalog(_project_path(settings.plan_catalog_path))
        gate = _GATES[key] = EntitlementGate(catalog, settings=settings, subscriptions=billing_rows.subscription_view,
                                             grace=payment_grace(settings))
    return gate
