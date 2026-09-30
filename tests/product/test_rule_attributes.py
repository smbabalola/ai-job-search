"""Bundle 7 Task 23 (spec §16.2): the pure rule-attribute derivation."""
from __future__ import annotations

import pytest

from product.rule_attributes import (
    UNKNOWN, annual_compensation_max, country_from, remote_mode_from, rotation_from_text,
)


@pytest.mark.parametrize("text,expected", [
    ("Offshore role on a 28/28 rotation.", "28/28"),
    ("rotation: 14 / 14, helicopter transfers", "14/14"),
    ("ROTATION pattern 21/21", "21/21"),
    ("24/7 support desk, no travel", UNKNOWN),                                    # no "rotation" nearby
    ("Start 12/2025. " + "x" * 60 + " rotation to be agreed", UNKNOWN),           # more than 40 characters away
    ("", UNKNOWN),
    (None, UNKNOWN),
])
def test_rotation_from_text(text, expected):
    assert rotation_from_text(text) == expected


def test_rotation_before_the_word_within_forty_characters():
    assert rotation_from_text("Work 14/14 on a rotation basis") == "14/14"


@pytest.mark.parametrize("compensation,currency,expected", [
    ({"currency": "GBP", "max": 250, "period": "day"}, "GBP", 65_000),
    ({"currency": "GBP", "max": 30, "period": "hour"}, "GBP", 62_400),
    ({"currency": "GBP", "max": 5_000, "period": "month"}, "GBP", 60_000),
    ({"currency": "GBP", "min": 55_000, "max": 70_000, "period": "year"}, "GBP", 70_000),
    ({"currency": "GBP", "min": 55_000, "period": "year"}, "GBP", 55_000),       # no max: the only figure
    ({"currency": "USD", "max": 90_000, "period": "year"}, "GBP", UNKNOWN),       # no FX conversion
    (None, "GBP", UNKNOWN),
    ({"currency": "GBP", "period": "year"}, "GBP", UNKNOWN),
    ({"currency": "GBP", "max": 60_000, "period": "fortnight"}, "GBP", UNKNOWN),
])
def test_annual_compensation_max(compensation, currency, expected):
    assert annual_compensation_max(compensation, currency) == expected


def test_country_and_remote_mode():
    assert country_from({"location": {"country_code": "gb"}}) == "GB"
    assert country_from({"country": "NO"}) == "NO"
    assert country_from({"location": "Aberdeen"}) == UNKNOWN
    assert country_from({}) == UNKNOWN
    assert remote_mode_from({"remote_mode": "hybrid"}) == "hybrid"
    assert remote_mode_from({}) == UNKNOWN
