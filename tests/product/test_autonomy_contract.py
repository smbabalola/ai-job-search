from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, strategies as st

from product.autonomy_contract import (
    CLICK_DISPATCH_TTL, FILL_SESSION_TTL, SUBMIT_GRANT_TTL,
    Capability, CanonicalHashError, UNKNOWN, canonical_hash, canonical_json,
    is_unknown, normalized_employer_key, parse_utc, reason, to_utc_iso,
)


def test_capability_is_totally_ordered():
    assert Capability.NONE < Capability.PREPARE < Capability.FILL < Capability.SUBMIT


def test_ttls_are_stage_specific():
    assert FILL_SESSION_TTL == timedelta(minutes=30)
    assert SUBMIT_GRANT_TTL == timedelta(seconds=120)
    assert CLICK_DISPATCH_TTL == timedelta(seconds=60)


def test_hash_ignores_key_order_and_whitespace():
    a = canonical_hash("s", "v1", {"b": 1, "a": {"y": [1, 2], "x": "é"}})
    b = canonical_hash("s", "v1", {"a": {"x": "é", "y": [1, 2]}, "b": 1})
    assert a == b
    assert a.startswith("sha256:")


def test_hash_includes_schema_and_version():
    payload = {"a": 1}
    assert canonical_hash("s", "v1", payload) != canonical_hash("s", "v2", payload)
    assert canonical_hash("s", "v1", payload) != canonical_hash("t", "v1", payload)


def test_floats_and_nan_are_rejected():
    with pytest.raises(CanonicalHashError):
        canonical_hash("s", "v1", {"x": 75.5})
    with pytest.raises(CanonicalHashError):
        canonical_json(float("nan"))


def test_decimals_serialize_as_strings():
    assert canonical_json({"x": Decimal("1.50")}) == '{"x":"1.50"}'


def test_naive_datetime_rejected_aware_normalized_to_utc():
    with pytest.raises(CanonicalHashError):
        canonical_json(datetime(2026, 9, 24, 12, 0))
    bst = timezone(timedelta(hours=1))
    assert canonical_json(datetime(2026, 9, 24, 13, 0, tzinfo=bst)) == '"2026-09-24T12:00:00.000000+00:00"'


def test_sets_are_sorted_and_unknown_is_explicit():
    assert canonical_json(frozenset({"b", "a"})) == '["a","b"]'
    assert canonical_json(UNKNOWN) == '{"$unknown":true}'
    assert is_unknown(UNKNOWN) and not is_unknown(None)


def test_nfc_key_collision_rejected():
    with pytest.raises(CanonicalHashError):
        canonical_json({"é": 1, "é": 2})


def test_capability_serializes_by_name():
    assert canonical_json(Capability.FILL) == '"FILL"'


def test_reason_params_are_sorted_strings():
    r = reason("rule_reduce", rule="r1", to="PREPARE")
    assert r.code == "rule_reduce"
    assert r.params == (("rule", "r1"), ("to", "PREPARE"))


def test_normalized_employer_key():
    assert normalized_employer_key("  Wood  Group PLC ") == "name:wood group plc"
    assert normalized_employer_key("Wood Group, PLC.") == "name:wood group plc"
    assert normalized_employer_key("") is None
    assert normalized_employer_key(None) is None


def test_utc_iso_round_trip():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    assert parse_utc(to_utc_iso(now)) == now
    with pytest.raises(ValueError):
        to_utc_iso(datetime(2026, 9, 24, 12, 0))


json_leaf = st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=8))
json_value = st.recursive(
    json_leaf,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(alphabet="abcxyz_", max_size=6), children, max_size=4),
    ),
    max_leaves=12,
)


@given(json_value)
def test_hash_is_stable_under_dict_reordering(value):
    def reorder(v):
        if isinstance(v, dict):
            return {k: reorder(v[k]) for k in reversed(list(v))}
        if isinstance(v, list):
            return [reorder(x) for x in v]
        return v
    assert canonical_hash("s", "v1", value) == canonical_hash("s", "v1", reorder(value))
