"""Bundle 7 Task 22 (spec §14.2, §14.5): CV strategy validation and rule lookup."""
from __future__ import annotations

import pytest

from product.cv_strategy import CvStrategyInvalid, ItemView, default_strategy, normalize_cv_strategy, resolve_rule
from product.cv_templates import CV_TEMPLATES

ITEMS = {"cvi_a": ItemView("cvi_a", "ACTIVE", ("cvv_a1", "cvv_a2")), "cvi_old": ItemView("cvi_old", "ARCHIVED", ("cvv_o1",))}


def _normalize(doc):
    return normalize_cv_strategy(doc, account_items=ITEMS, templates=CV_TEMPLATES)


def test_the_template_registry():
    assert CV_TEMPLATES == {"standard@1": {"renderer": "cv_document_renderer", "formats": ("docx",)}}


def test_the_initial_default_needs_a_user_choice():
    doc = default_strategy()
    assert doc == {"default": {"mode": "LATEST_VERSION", "item_id": None}, "by_family": {}}
    assert _normalize(doc) == doc


def test_a_valid_strategy_and_the_rule_lookup():
    doc = _normalize({"default": {"mode": "LATEST_VERSION", "item_id": "cvi_a"},
                      "by_family": {"drilling": {"mode": "FIXED_VERSION", "version_id": "cvv_a1"},
                                    "fluids": {"mode": "TAILOR_FROM", "item_id": "cvi_a", "template_id": "standard@1"}}})
    assert resolve_rule(doc, "drilling") == {"mode": "FIXED_VERSION", "version_id": "cvv_a1"}
    assert resolve_rule(doc, "UNKNOWN") == {"mode": "LATEST_VERSION", "item_id": "cvi_a"}
    assert resolve_rule(doc, "not-mapped") == doc["default"]


@pytest.mark.parametrize("doc,fragment", [
    ({"default": {"mode": "LATEST_VERSION", "item_id": "cvi_foreign"}, "by_family": {}}, "not one of your CVs"),
    ({"default": {"mode": "LATEST_VERSION", "item_id": "cvi_old"}, "by_family": {}}, "archived"),
    ({"by_family": {}}, "default"),
    ({"default": {"mode": "TAILOR_FROM", "item_id": "cvi_a", "template_id": "fancy@9"}, "by_family": {}}, "template"),
    ({"default": {"mode": "FIXED_VERSION", "version_id": "cvv_nope"}, "by_family": {}}, "not one of your CVs"),
    ({"default": {"mode": "SOMETHING", "item_id": "cvi_a"}, "by_family": {}}, "mode"),
    ({"default": {"mode": "LATEST_VERSION", "item_id": "cvi_a", "extra": 1}, "by_family": {}}, "fields"),
])
def test_invalid_strategies_are_refused(doc, fragment):
    with pytest.raises(CvStrategyInvalid, match=fragment):
        _normalize(doc)
