"""Bundle 7 spec §11.4: the pure entitlement resolver."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings as hsettings, strategies as st

from product.entitlements import (
    FEATURES, CatalogSet, Grant, SubscriptionView, effective_entitlements, load_catalog, parse_catalog,
)

DEV = Path(__file__).parents[2] / "product" / "plans" / "plan-catalog.dev.json"
CATALOG = load_catalog(DEV)
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
PERIOD_START = datetime(2026, 9, 3, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 10, 3, tzinfo=timezone.utc)
ALL_ON = {"AI_ENABLED": True, "AUTOMATION_ENABLED": True, "DISCOVERY_ENABLED": True, "SIGNUPS_ENABLED": True}
GRACE = timedelta(days=3)


def _sub(state: str, plan_id: str = "pro", *, past_due_since=None, catalog_version=None) -> SubscriptionView:
    return SubscriptionView(state=state, plan_id=plan_id, catalog_version=catalog_version or CATALOG.catalog_version,
                            current_period_start=PERIOD_START, current_period_end=PERIOD_END,
                            past_due_since=past_due_since)


def _grant(kind: str, *, plan_id=None, allowance=None, amount=None, starts_at=None, expires_at=None,
           revoked_at=None) -> Grant:
    return Grant(id="g1", kind=kind, plan_id=plan_id, allowance=allowance, amount=amount,
                 starts_at=starts_at or NOW - timedelta(days=1), expires_at=expires_at, revoked_at=revoked_at)


def _resolve(snapshot=None, grants=(), controls=ALL_ON, *, now=NOW, catalog=CATALOG):
    return effective_entitlements(catalog, snapshot, list(grants), controls, grace=GRACE, now=now)


@pytest.mark.parametrize("snapshot, expected_plan", [
    (None, "free"),
    (_sub("ACTIVE"), "pro"),
    (_sub("TRIALING", "power"), "power"),
    (_sub("PAST_DUE", past_due_since=NOW - timedelta(days=2)), "pro"),
    (_sub("PAST_DUE", past_due_since=NOW - timedelta(days=4)), "free"),
    (_sub("PAST_DUE", past_due_since=None), "free"),
    (_sub("CANCEL_SCHEDULED"), "pro"),
    (_sub("UNKNOWN"), "free"),
    (_sub("INCOMPLETE"), "free"),
    (_sub("ENDED"), "free"),
    (_sub("INCOMPLETE_EXPIRED"), "free"),
])
def test_the_plan_follows_the_subscription_state(snapshot, expected_plan):
    resolved = _resolve(snapshot)
    assert resolved.plan_id == expected_plan
    assert dict(resolved.features) == {f: CATALOG.plan(expected_plan).features[f] for f in FEATURES}


def test_cancel_scheduled_keeps_the_plan_only_until_the_period_end():
    assert _resolve(_sub("CANCEL_SCHEDULED"), now=PERIOD_END - timedelta(seconds=1)).plan_id == "pro"
    assert _resolve(_sub("CANCEL_SCHEDULED"), now=PERIOD_END).plan_id == "free"


def test_free_uses_the_utc_calendar_month_and_paid_uses_the_snapshot_period():
    free = _resolve(None).window
    assert (free.start, free.end) == (datetime(2026, 9, 1, tzinfo=timezone.utc),
                                      datetime(2026, 10, 1, tzinfo=timezone.utc))
    december = _resolve(None, now=datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)).window
    assert december.end == datetime(2027, 1, 1, tzinfo=timezone.utc)
    paid = _resolve(_sub("ACTIVE")).window
    assert (paid.start, paid.end) == (PERIOD_START, PERIOD_END)
    assert free.key != paid.key
    assert _resolve(None).window.key == _resolve(None, now=NOW + timedelta(days=5)).window.key


def test_null_allowances_resolve_to_zero():
    data = json.loads(DEV.read_text(encoding="utf-8"))
    data["plans"]["pro"]["allowances"]["cv.tailor"]["limit"] = None
    assert _resolve(_sub("ACTIVE"), catalog=parse_catalog(data)).allowances["cv.tailor"] == 0


def test_a_plan_override_grant_raises_the_plan_until_it_expires():
    grant = _grant("PLAN_OVERRIDE", plan_id="power", expires_at=NOW + timedelta(days=1))
    resolved = _resolve(None, [grant])
    assert (resolved.plan_id, resolved.source) == ("power", "grant")
    assert resolved.features["automation.prepare"] is True
    assert _resolve(None, [grant], now=NOW + timedelta(days=1)).plan_id == "free"
    revoked = _grant("PLAN_OVERRIDE", plan_id="power", expires_at=NOW + timedelta(days=1), revoked_at=NOW)
    assert _resolve(None, [revoked]).plan_id == "free"


def test_a_grant_never_lowers_anything():
    downgrade = _grant("PLAN_OVERRIDE", plan_id="free", expires_at=NOW + timedelta(days=1))
    assert _resolve(_sub("ACTIVE", "power"), [downgrade]).plan_id == "power"
    negative = _grant("ALLOWANCE_BONUS", allowance="cv.tailor", amount=-5)
    base = _resolve(_sub("ACTIVE")).allowances["cv.tailor"]
    assert _resolve(_sub("ACTIVE"), [negative]).allowances["cv.tailor"] == base


def test_an_allowance_bonus_adds_only_within_its_window():
    bonus = _grant("ALLOWANCE_BONUS", allowance="applications.prepare", amount=5,
                   starts_at=PERIOD_START + timedelta(days=1))
    base = _resolve(_sub("ACTIVE")).allowances["applications.prepare"]
    assert _resolve(_sub("ACTIVE"), [bonus]).allowances["applications.prepare"] == base + 5
    earlier = _grant("ALLOWANCE_BONUS", allowance="applications.prepare", amount=5,
                     starts_at=PERIOD_START - timedelta(days=1))
    assert _resolve(_sub("ACTIVE"), [earlier]).allowances["applications.prepare"] == base


@pytest.mark.parametrize("control, features", [
    ("AI_ENABLED", {"ai.prepare", "ai.cv_tailor", "profile.cv_import"}),
    ("AUTOMATION_ENABLED", {"automation.screening", "automation.prepare", "rules.enforced_automation"}),
    ("DISCOVERY_ENABLED", {"discovery.on_demand", "discovery.scheduled", "notifications.digest"}),
])
def test_a_disabled_platform_control_turns_off_its_features(control, features):
    resolved = _resolve(_sub("ACTIVE", "power"), controls={**ALL_ON, control: False})
    assert {f for f in FEATURES if not resolved.features[f]} == features


def test_a_missing_platform_control_is_fail_closed():
    resolved = _resolve(_sub("ACTIVE", "power"), controls={})
    assert resolved.features["ai.prepare"] is False
    assert resolved.features["workspace.tracking"] is True


def test_a_pinned_catalog_version_is_honoured():
    older = json.loads(DEV.read_text(encoding="utf-8"))
    older["catalog_version"] = "2026-01.1"
    older["plans"]["pro"]["allowances"]["applications.prepare"]["limit"] = 7
    catalogs = CatalogSet(current=CATALOG, pinned={"2026-01.1": parse_catalog(older)})
    pinned = effective_entitlements(catalogs, _sub("ACTIVE", catalog_version="2026-01.1"), [], ALL_ON,
                                    grace=GRACE, now=NOW)
    assert (pinned.catalog_version, pinned.allowances["applications.prepare"]) == ("2026-01.1", 7)
    free = effective_entitlements(catalogs, None, [], ALL_ON, grace=GRACE, now=NOW)
    assert free.catalog_version == CATALOG.catalog_version


_plans = st.sampled_from(["free", "pro", "power"])
_states = st.sampled_from(["ACTIVE", "TRIALING", "PAST_DUE", "CANCEL_SCHEDULED", "UNKNOWN", "ENDED", "INCOMPLETE"])


@hsettings(max_examples=150, deadline=None)
@given(plan=_plans, state=_states, granted=st.one_of(st.none(), _plans), days=st.integers(-10, 10),
       controls=st.dictionaries(st.sampled_from(list(ALL_ON)), st.booleans()))
def test_resolved_features_never_exceed_the_plan_and_granted_plan(plan, state, granted, days, controls):
    grants = [] if granted is None else [
        _grant("PLAN_OVERRIDE", plan_id=granted, expires_at=NOW + timedelta(days=days))]
    snapshot = _sub(state, plan, past_due_since=NOW - timedelta(days=abs(days)))
    resolved = _resolve(snapshot, grants, controls)
    allowed = {f for f in FEATURES if CATALOG.plan(plan).features[f]}
    allowed |= {f for f in FEATURES if CATALOG.plan("free").features[f]}
    if granted is not None:
        allowed |= {f for f in FEATURES if CATALOG.plan(granted).features[f]}
    assert {f for f in FEATURES if resolved.features[f]} <= allowed


def test_an_exhausted_allowance_points_to_the_next_plan_with_more_of_it():
    assert CATALOG.upgrade_for_allowance("applications.prepare", "free") == "pro"
    assert CATALOG.upgrade_for_allowance("applications.prepare", "pro") == "power"
    assert CATALOG.upgrade_for_allowance("applications.prepare", "power") is None
    # pro has no more automation.prepare than free (both 0): the way up is power
    assert CATALOG.upgrade_for_allowance("automation.prepare", "free") == "power"
