"""Plans, the capability matrix and entitlement resolution (Bundle 7 spec §11).

Pure: the catalog is data validated here, and ``effective_entitlements`` maps
(catalog, subscription snapshot, grants, platform controls, now) to what an
account may do. No I/O; the webapp's ``EntitlementGate`` reads the inputs.
Every business number is a catalog value (DP-3); ``null`` means 0.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "plan-catalog.v1"

FEATURES: tuple[str, ...] = (
    "workspace.tracking",
    "library.cv",
    "profile.onboarding",
    "profile.cv_import",
    "ai.prepare",
    "ai.cv_tailor",
    "apply.assisted_fill",
    "apply.human_submit",
    "discovery.on_demand",
    "discovery.scheduled",
    "automation.screening",
    "automation.prepare",
    "rules.enforced_automation",
    "notifications.digest",
)

ALLOWANCES: tuple[str, ...] = (
    "applications.prepare",
    "cv.tailor",
    "profile.cv_import",
    "library.cv_items",
    "storage.bytes",
    "discovery.on_demand_runs",
    "discovery.scheduled_searches",
    "automation.prepare",
    "ai.cost_micro_usd",
)
HIDDEN_ALLOWANCES = frozenset({"ai.cost_micro_usd"})
GAUGE_ALLOWANCES = frozenset({"library.cv_items", "storage.bytes", "discovery.scheduled_searches"})

# §19.3. A key missing from the controls mapping reads as false (fail closed).
PLATFORM_CONTROL_KEYS: tuple[str, ...] = (
    "SIGNUPS_ENABLED",
    "AI_ENABLED",
    "AUTOMATION_ENABLED",
    "DISCOVERY_ENABLED",
    "SUBMIT_ENABLED",
    "HOSTED_THREAT_MODEL_SIGNED_OFF",
)
# §11.4 step 4: the features each ceiling control turns off.
CONTROLLED_FEATURES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "AI_ENABLED": ("ai.prepare", "ai.cv_tailor", "profile.cv_import"),
    "AUTOMATION_ENABLED": ("automation.screening", "automation.prepare", "rules.enforced_automation"),
    "DISCOVERY_ENABLED": ("discovery.on_demand", "discovery.scheduled", "notifications.digest"),
})

PAID_STATES = frozenset({"ACTIVE", "TRIALING", "CANCEL_SCHEDULED"})
PRICE_INTERVALS = frozenset({"month", "year"})


class CatalogError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class PlanSpec:
    plan_id: str
    display_name: str
    rank: int
    trial_days: int | None
    features: Mapping[str, bool]
    allowances: Mapping[str, int | None]
    provider_prices: Mapping[str, str | None]


@dataclass(frozen=True)
class Catalog:
    catalog_version: str
    catalog_hash: str
    catalog_json: str
    plans: Mapping[str, PlanSpec]

    def plan(self, plan_id: str) -> PlanSpec:
        return self.plans[plan_id]

    def ranked(self) -> list[PlanSpec]:
        return sorted(self.plans.values(), key=lambda plan: plan.rank)

    def upgrade_for(self, feature: str, from_plan: str) -> str | None:
        """The lowest-ranked plan above ``from_plan`` that includes ``feature``."""
        floor = self.plans[from_plan].rank if from_plan in self.plans else -1
        for plan in self.ranked():
            if plan.rank > floor and plan.features.get(feature):
                return plan.plan_id
        return None

    def upgrade_for_allowance(self, allowance: str, from_plan: str) -> str | None:
        """The lowest-ranked plan above ``from_plan`` with more of ``allowance``
        (``None`` is unlimited)."""
        current = self.plans[from_plan].allowances.get(allowance, 0) if from_plan in self.plans else 0
        floor = self.plans[from_plan].rank if from_plan in self.plans else -1
        if current is None:
            return None
        for plan in self.ranked():
            limit = plan.allowances.get(allowance, 0)
            if plan.rank > floor and (limit is None or limit > current):
                return plan.plan_id
        return None


@dataclass(frozen=True)
class CatalogSet:
    """The current catalog plus the older versions subscriptions pin (DP-8)."""
    current: Catalog
    pinned: Mapping[str, Catalog] = field(default_factory=dict)

    def version(self, catalog_version: str) -> Catalog:
        if catalog_version == self.current.catalog_version:
            return self.current
        return self.pinned.get(catalog_version, self.current)


def _canonical(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _plan_problems(plan_id: str, plan: Any) -> list[str]:
    if not isinstance(plan, dict):
        return [f"plans.{plan_id} must be an object"]
    problems: list[str] = []
    if not isinstance(plan.get("display_name"), str) or not plan["display_name"]:
        problems.append(f"plans.{plan_id}.display_name must be a non-empty string")
    if not _is_int(plan.get("rank")):
        problems.append(f"plans.{plan_id}.rank must be an integer")
    trial = plan.get("trial_days")
    if trial is not None and not (_is_int(trial) and trial >= 0):
        problems.append(f"plans.{plan_id}.trial_days must be an integer >= 0 or null")
    if plan_id == "free" and trial != 0:
        problems.append("plans.free.trial_days must be 0")

    features = plan.get("features")
    if not isinstance(features, dict):
        problems.append(f"plans.{plan_id}.features must be an object")
    else:
        for key in FEATURES:
            if key not in features:
                problems.append(f"plans.{plan_id}.features is missing {key}")
            elif not isinstance(features[key], bool):
                problems.append(f"plans.{plan_id}.features.{key} must be a boolean")
        for key in sorted(set(features) - set(FEATURES)):
            problems.append(f"plans.{plan_id}.features has unknown key {key}")

    allowances = plan.get("allowances")
    if not isinstance(allowances, dict):
        problems.append(f"plans.{plan_id}.allowances must be an object")
    else:
        for key in ALLOWANCES:
            entry = allowances.get(key)
            if key not in allowances:
                problems.append(f"plans.{plan_id}.allowances is missing {key}")
            elif not isinstance(entry, dict) or set(entry) != {"limit", "window"}:
                problems.append(f"plans.{plan_id}.allowances.{key} must be {{limit, window}}")
            else:
                limit = entry["limit"]
                if limit is not None and not (_is_int(limit) and limit >= 0):
                    problems.append(f"plans.{plan_id}.allowances.{key}.limit must be an integer >= 0 or null")
                if entry["window"] != "period":
                    problems.append(f"plans.{plan_id}.allowances.{key}.window must be 'period'")
        for key in sorted(set(allowances) - set(ALLOWANCES)):
            problems.append(f"plans.{plan_id}.allowances has unknown key {key}")

    prices = plan.get("provider_prices")
    if not isinstance(prices, dict):
        problems.append(f"plans.{plan_id}.provider_prices must be an object")
    elif plan_id == "free":
        if prices:
            problems.append("plans.free.provider_prices must be empty")
    else:
        for interval, price in prices.items():
            if interval not in PRICE_INTERVALS:
                problems.append(f"plans.{plan_id}.provider_prices has unknown interval {interval}")
            elif price is not None and (not isinstance(price, str) or not price):
                problems.append(f"plans.{plan_id}.provider_prices.{interval} must be a string or null")
    return problems


def parse_catalog(data: Mapping[str, Any]) -> Catalog:
    problems: list[str] = []
    if data.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}")
    version = data.get("catalog_version")
    if not isinstance(version, str) or not version:
        problems.append("catalog_version must be a non-empty string")
    plans = data.get("plans")
    if not isinstance(plans, dict) or not plans:
        raise CatalogError(problems + ["plans must be a non-empty object"])
    if "free" not in plans:
        problems.append("plans must include free")
    for plan_id, plan in plans.items():
        problems.extend(_plan_problems(plan_id, plan))
    ranks = [plan.get("rank") for plan in plans.values() if isinstance(plan, dict)]
    # Ranks form a strict order (independent of key order, which hashing canonicalizes).
    if all(_is_int(rank) for rank in ranks) and len(set(ranks)) != len(ranks):
        problems.append("plan rank must be strictly increasing (no two plans share a rank)")
    if "free" in plans and isinstance(plans["free"], dict) and ranks and plans["free"].get("rank") != min(
            r for r in ranks if _is_int(r)):
        problems.append("plans.free must have the lowest rank")
    if problems:
        raise CatalogError(problems)

    canonical = _canonical(data)
    return Catalog(
        catalog_version=version,
        catalog_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        catalog_json=canonical,
        plans=MappingProxyType({
            plan_id: PlanSpec(
                plan_id=plan_id, display_name=plan["display_name"], rank=plan["rank"],
                trial_days=plan["trial_days"],
                features=MappingProxyType(dict(plan["features"])),
                allowances=MappingProxyType({k: v["limit"] for k, v in plan["allowances"].items()}),
                provider_prices=MappingProxyType(dict(plan["provider_prices"])),
            )
            for plan_id, plan in plans.items()
        }),
    )


def load_catalog(path: str | Path) -> Catalog:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError([f"cannot read plan catalog {path}: {exc}"]) from exc
    if not isinstance(data, dict):
        raise CatalogError(["plan catalog must be a JSON object"])
    return parse_catalog(data)


# ---- resolution ---------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    key: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class SubscriptionView:
    state: str
    plan_id: str
    catalog_version: str
    current_period_start: datetime
    current_period_end: datetime
    past_due_since: datetime | None


@dataclass(frozen=True)
class Grant:
    id: str
    kind: str  # PLAN_OVERRIDE | ALLOWANCE_BONUS
    plan_id: str | None
    allowance: str | None
    amount: int | None
    starts_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None

    def active(self, now: datetime) -> bool:
        return (self.revoked_at is None and self.starts_at <= now
                and (self.expires_at is None or now < self.expires_at))


@dataclass(frozen=True)
class Entitlements:
    plan_id: str
    catalog_version: str
    features: Mapping[str, bool]
    allowances: Mapping[str, int]
    window: Window
    source: str  # "free" | "subscription" | "grant"

    def has(self, feature: str) -> bool:
        return bool(self.features.get(feature))


class FeatureNotInPlan(Exception):
    def __init__(self, feature: str, plan_id: str, upgrade_to: str | None):
        super().__init__(f"{feature} is not included in the {plan_id} plan")
        self.feature = feature
        self.plan_id = plan_id
        self.upgrade_to = upgrade_to


def calendar_month_window(now: datetime) -> Window:
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=timezone.utc)
    return Window(key=f"month:{start:%Y-%m}", start=start, end=end)


def _subscription_is_paid(snapshot: SubscriptionView, *, grace: timedelta, now: datetime) -> bool:
    if snapshot.state == "CANCEL_SCHEDULED":
        return now < snapshot.current_period_end
    if snapshot.state in PAID_STATES:
        return True
    if snapshot.state == "PAST_DUE":
        return snapshot.past_due_since is not None and now - snapshot.past_due_since <= grace
    return False  # INCOMPLETE, ENDED, INCOMPLETE_EXPIRED, UNKNOWN: fail closed to Free


def effective_entitlements(catalog: Catalog | CatalogSet, snapshot: SubscriptionView | None,
                           grants: Sequence[Grant], controls: Mapping[str, bool], *,
                           grace: timedelta, now: datetime) -> Entitlements:
    catalogs = catalog if isinstance(catalog, CatalogSet) else CatalogSet(current=catalog)
    current = catalogs.current

    # 1-2. Plan and catalog version.
    base_catalog, plan, source = current, current.plan("free"), "free"
    window = calendar_month_window(now)
    if snapshot is not None and _subscription_is_paid(snapshot, grace=grace, now=now):
        pinned = catalogs.version(snapshot.catalog_version)
        if snapshot.plan_id in pinned.plans:
            base_catalog, plan, source = pinned, pinned.plan(snapshot.plan_id), "subscription"
            start, end = snapshot.current_period_start, snapshot.current_period_end
            window = Window(key=f"period:{start.isoformat()}", start=start, end=end)

    features = dict(plan.features)
    allowances = {key: limit or 0 for key, limit in plan.allowances.items()}
    plan_id = plan.plan_id

    # 3. Grants only ever raise: an override unions features and maxes limits.
    overrides = [g for g in grants if g.kind == "PLAN_OVERRIDE" and g.active(now) and g.plan_id in current.plans]
    for grant in sorted(overrides, key=lambda g: current.plan(g.plan_id).rank):
        granted = current.plan(grant.plan_id)
        if granted.rank <= current.plans.get(plan_id, plan).rank:
            continue
        features = {key: features[key] or granted.features[key] for key in FEATURES}
        allowances = {key: max(allowances[key], granted.allowances[key] or 0) for key in ALLOWANCES}
        plan_id, source = granted.plan_id, "grant"
    for grant in grants:
        if (grant.kind == "ALLOWANCE_BONUS" and grant.active(now) and grant.allowance in allowances
                and (grant.amount or 0) > 0 and window.start <= grant.starts_at < window.end):
            allowances[grant.allowance] += grant.amount

    # 4. Ceilings: a control that is off (or missing) turns its features off.
    for control, controlled in CONTROLLED_FEATURES.items():
        if not controls.get(control, False):
            for feature in controlled:
                features[feature] = False

    return Entitlements(
        plan_id=plan_id, catalog_version=base_catalog.catalog_version,
        features=MappingProxyType(features), allowances=MappingProxyType(allowances),
        window=window, source=source,
    )
