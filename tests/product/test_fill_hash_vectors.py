"""6D-B S5 (spec §7.5): the canonical value hash, checked against the one
shared vector file that the TypeScript suite also loads, unmodified."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from product.autonomy_contract import canonical_hash
from product.fill_hash import UnpairedSurrogateError, fill_value_hash

VECTORS_PATH = Path(__file__).parents[1] / "fixtures" / "fill" / "hash_vectors.json"


def _vectors() -> dict:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def test_vector_file_is_exactly_what_the_generator_writes():
    """The expected hashes come only from tools/gen_fill_hash_vectors.py
    (the Python canonical implementation); a hand edit or a stale file fails."""
    from tools.gen_fill_hash_vectors import render
    assert VECTORS_PATH.read_text(encoding="utf-8") == render()


def test_at_least_twenty_hashing_vectors_across_the_required_categories():
    data = _vectors()
    hashing = [v for v in data["vectors"] if "expected" in v]
    assert len(hashing) >= 20
    required = {"ascii", "empty", "whitespace", "tab_newline", "emoji", "rtl", "combining", "nfc_equivalence",
                "crlf", "lone_cr", "lf", "long", "quotes", "backslash", "line_separator", "json_hostile"}
    assert required <= {v["category"] for v in data["vectors"]}


@pytest.mark.parametrize("vector", [v for v in _vectors()["vectors"] if "expected" in v], ids=lambda v: v["name"])
def test_python_matches_every_shared_vector(vector):
    assert fill_value_hash(vector["input"]) == vector["expected"]


@pytest.mark.parametrize("vector", [v for v in _vectors()["vectors"] if "error" in v], ids=lambda v: v["name"])
def test_python_rejects_every_error_vector(vector):
    assert vector["error"] == "unpaired_surrogate"
    with pytest.raises(UnpairedSurrogateError):
        fill_value_hash(vector["input"])


@pytest.mark.parametrize("group", _vectors()["equivalence_groups"], ids=lambda g: "=".join(g))
def test_equivalent_inputs_hash_identically(group):
    by_name = {v["name"]: v for v in _vectors()["vectors"]}
    assert len({by_name[name]["expected"] for name in group}) == 1


def test_the_normalization_order_is_nfc_then_newlines_then_canonical_hash():
    raw = "Café\r\nline\rend"
    assert fill_value_hash(raw) == canonical_hash("fill-rendered-value", "v1", "Café\nline\nend")


def test_only_strings_are_hashed():
    with pytest.raises(TypeError):
        fill_value_hash(1)  # type: ignore[arg-type]
