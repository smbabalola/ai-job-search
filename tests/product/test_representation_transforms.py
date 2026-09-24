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


def test_canonical_not_same_object_as_apply():
    """Fix 1: Verify phone_e164 and whitespace_normalize have independent canonical oracles."""
    from product.representation_transforms import _TRANSFORMS

    # phone_e164: canonical should be _phone_digits, not _phone_canonical (different objects)
    phone_transform = _TRANSFORMS["phone_e164"]
    assert phone_transform.apply is not phone_transform.canonical, \
        "phone_e164: apply and canonical must be different function objects"

    # whitespace_normalize: canonical should not be the same lambda as apply
    ws_transform = _TRANSFORMS["whitespace_normalize"]
    assert ws_transform.apply is not ws_transform.canonical, \
        "whitespace_normalize: apply and canonical must be different function objects"


@pytest.mark.parametrize("tid, value", [
    ("date_iso_to_dmy", "2026-13-01"),   # Invalid month
    ("date_iso_to_dmy", "2026-02-30"),   # Invalid day for February
    ("date_iso_to_mdy", "2026-13-01"),   # Invalid month
    ("date_iso_to_mdy", "2026-02-30"),   # Invalid day for February
])
def test_invalid_calendar_dates(tid, value):
    """Fix 2: Invalid calendar dates must raise TransformError."""
    with pytest.raises(TransformError):
        apply_transform(tid, value)


def test_phone_non_ascii_digits():
    """Fix 3: Non-ASCII digits must be rejected."""
    with pytest.raises(TransformError):
        apply_transform("phone_e164", "+44٧٧٠٠٩٠٠١٢٣")


@pytest.mark.parametrize("value, should_raise", [
    ("United Kingdom", True),   # Country name, not ISO2 code
    ("gb", False),              # Valid ISO2 code (lowercase)
    ("GB", False),              # Valid ISO2 code (uppercase)
    ("US", False),              # Valid ISO2 code
])
def test_country_iso2_to_name_validates_code(value, should_raise):
    """Fix 4: country_iso2_to_name must require ISO2 input, not country name."""
    if should_raise:
        with pytest.raises(TransformError):
            apply_transform("country_iso2_to_name", value)
    else:
        result = apply_transform("country_iso2_to_name", value)
        assert isinstance(result, str)
        assert result in ["United Kingdom", "United States"]
