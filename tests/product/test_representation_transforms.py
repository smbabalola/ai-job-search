from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from product.representation_transforms import TRANSFORM_IDS, TransformError, apply_transform


def test_closed_set():
    assert TRANSFORM_IDS == frozenset({
        "identity", "whitespace_normalize", "country_name_to_iso2", "country_iso2_to_name",
        "date_iso_to_dmy", "date_iso_to_mdy", "phone_e164",
    })


@pytest.mark.parametrize("tid, value, expected", [
    ("identity", "Yes", "Yes"),
    ("whitespace_normalize", "  three   months ", "three months"),
    ("country_name_to_iso2", "United Kingdom", "GB"),
    ("country_name_to_iso2", "UK", "GB"),
    ("country_iso2_to_name", "GB", "United Kingdom"),
    ("country_name_to_iso2", "United Arab Emirates", "AE"),
    ("date_iso_to_dmy", "2026-10-01", "01/10/2026"),
    ("date_iso_to_mdy", "2026-10-01", "10/01/2026"),
    ("phone_e164", "+44 (0)7700 900-123", "+447700900123"),
    ("phone_e164", "+44 7700 900123", "+447700900123"),
])
def test_transforms(tid, value, expected):
    assert apply_transform(tid, value) == expected


@pytest.mark.parametrize("tid, value", [
    ("country_name_to_iso2", "Atlantis"),
    ("date_iso_to_dmy", "1 Oct 2026"),
    ("phone_e164", "07700 900123"),
    ("no_such_transform", "x"),
])
def test_fail_closed(tid, value):
    with pytest.raises(TransformError):
        apply_transform(tid, value)


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=30))
def test_whitespace_normalize_preserves_value(text):
    try:
        out = apply_transform("whitespace_normalize", text)
    except TransformError:
        return
    assert out.split() == text.split()
