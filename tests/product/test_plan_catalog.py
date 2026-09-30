"""Bundle 7 spec §11.1: the versioned plan catalog and its validator."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from product.entitlements import ALLOWANCES, FEATURES, CatalogError, load_catalog, parse_catalog
from webapp.config import Settings
from webapp.deployment import validate_settings

PLANS = Path(__file__).parents[2] / "product" / "plans"
PRODUCTION = PLANS / "plan-catalog.v1.json"
DEV = PLANS / "plan-catalog.dev.json"


def _dev() -> dict:
    return json.loads(DEV.read_text(encoding="utf-8"))


def _problems(data: dict) -> list[str]:
    with pytest.raises(CatalogError) as exc_info:
        parse_catalog(data)
    return exc_info.value.problems


def test_production_catalog_loads_with_every_business_value_open():
    catalog = load_catalog(PRODUCTION)
    assert [plan.plan_id for plan in catalog.ranked()] == ["free", "pro", "power"]
    for plan in catalog.ranked():
        assert set(plan.allowances) == set(ALLOWANCES)
        assert all(limit is None for limit in plan.allowances.values()), plan.plan_id
        assert all(price is None for price in plan.provider_prices.values()), plan.plan_id
    assert catalog.plan("free").trial_days == 0
    assert catalog.plan("pro").trial_days is None and catalog.plan("power").trial_days is None


def test_the_shipped_feature_matrix_matches_the_spec():
    catalog = load_catalog(PRODUCTION)
    free, pro, power = (catalog.plan(plan_id).features for plan_id in ("free", "pro", "power"))
    initiative = {"discovery.scheduled", "automation.screening", "automation.prepare",
                  "rules.enforced_automation", "notifications.digest"}
    assert {f for f in FEATURES if not free[f]} == initiative | {"ai.cv_tailor"}
    assert {f for f in FEATURES if not pro[f]} == initiative
    assert all(power.values())
    assert load_catalog(DEV).plan("pro").features == pro


def test_the_catalog_hash_is_stable_and_content_sensitive():
    first, second = load_catalog(DEV), load_catalog(DEV)
    assert first.catalog_hash == second.catalog_hash
    changed = _dev()
    changed["plans"]["pro"]["display_name"] = "Pro+"
    assert parse_catalog(changed).catalog_hash != first.catalog_hash


@pytest.mark.parametrize("bad_limit", [-1, "unlimited", 1.5, True])
def test_the_validator_rejects_non_finite_or_malformed_allowances(bad_limit):
    data = _dev()
    data["plans"]["pro"]["allowances"]["cv.tailor"]["limit"] = bad_limit
    assert any("pro" in p and "cv.tailor" in p for p in _problems(data))


def test_the_validator_rejects_missing_and_unknown_keys():
    data = _dev()
    del data["plans"]["power"]["features"]["ai.prepare"]
    data["plans"]["power"]["features"]["ai.magic"] = True
    del data["plans"]["free"]["allowances"]["storage.bytes"]
    problems = _problems(data)
    assert any("power" in p and "ai.prepare" in p for p in problems)
    assert any("power" in p and "ai.magic" in p for p in problems)
    assert any("free" in p and "storage.bytes" in p for p in problems)


def test_the_validator_rejects_a_non_increasing_rank():
    data = _dev()
    data["plans"]["power"]["rank"] = 1
    assert any("rank" in p for p in _problems(data))


def test_the_validator_rejects_paid_prices_on_free_and_a_missing_free_plan():
    data = _dev()
    data["plans"]["free"]["provider_prices"] = {"month": "price_123"}
    assert any("free" in p and "provider_prices" in p for p in _problems(data))
    no_free = _dev()
    del no_free["plans"]["free"]
    assert any("free" in p for p in _problems(no_free))


def test_the_validator_rejects_a_wrong_schema_version_and_window():
    data = _dev()
    data["schema_version"] = "plan-catalog.v0"
    data["plans"]["pro"]["allowances"]["cv.tailor"]["window"] = "forever"
    problems = _problems(data)
    assert any("schema_version" in p for p in problems)
    assert any("window" in p for p in problems)


def test_a_valid_catalog_is_not_mutated_by_parsing():
    data = _dev()
    before = copy.deepcopy(data)
    parse_catalog(data)
    assert data == before


def test_hosted_mode_refuses_the_development_catalog(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", deployment="hosted", plan_catalog_path=DEV)
    assert "hosted mode refuses the development plan catalog" in validate_settings(settings)
