"""Generate tests/fixtures/fill/hash_vectors.json, the ONE shared S5 vector
set (6D-B spec §7.5). Expected hashes come only from the Python canonical
implementation (product.fill_hash.fill_value_hash). The TypeScript suite
loads this file unmodified.

The file is pure-ASCII JSON (every non-ASCII and control character is
\\u-escaped), so line-ending conversion can never alter a vector.

Usage: python tools/gen_fill_hash_vectors.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from product.fill_hash import UnpairedSurrogateError, fill_value_hash  # noqa: E402

OUTPUT = ROOT / "tests" / "fixtures" / "fill" / "hash_vectors.json"


def _long_value() -> str:
    """Exactly 10 000 BMP code points (also 10 000 UTF-16 units), mixing
    ASCII, Latin-1, CJK and whitespace."""
    unit = "Lorem ipsum \u00e9t\u00e9 \u4e2d\u6587 \u00e7a va\n"
    text = (unit * (10_000 // len(unit) + 1))[:10_000]
    assert len(text) == 10_000
    return text


# (name, category, input). Order is part of the file.
VECTORS: list[tuple[str, str, str]] = [
    ("ascii_simple", "ascii", "1 month"),
    ("ascii_sentence", "ascii", "Hello, world. 42 applications."),
    ("empty", "empty", ""),
    ("leading_space", "whitespace", "  leading"),
    ("trailing_space", "whitespace", "trailing  "),
    ("internal_spaces", "whitespace", "internal   spaces"),
    ("nbsp", "whitespace", "a\u00a0b"),
    ("tab", "tab_newline", "a\tb"),
    ("multiline_lf", "tab_newline", "multi\nline\ntext"),
    ("emoji", "emoji", "\u2705 done \U0001F680"),
    ("emoji_zwj", "emoji", "\U0001F469\u200d\U0001F4BB and \U0001F468\u200d\U0001F469\u200d\U0001F467"),
    ("rtl_arabic", "rtl", "\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645"),
    ("rtl_hebrew_mixed", "rtl", "\u05e9\u05dc\u05d5\u05dd world 123"),
    ("combining_stack", "combining", "e\u0301\u0302"),
    ("combining_order", "combining", "a\u0328\u0301"),
    ("nfc_precomposed", "nfc_equivalence", "Caf\u00e9"),
    ("nfd_decomposed", "nfc_equivalence", "Cafe\u0301"),
    ("a_ring_precomposed", "nfc_equivalence", "\u00c5"),
    ("a_ring_decomposed", "nfc_equivalence", "A\u030a"),
    ("angstrom_sign", "nfc_equivalence", "\u212b"),
    ("newline_crlf", "crlf", "line1\r\nline2"),
    ("newline_lone_cr", "lone_cr", "line1\rline2"),
    ("newline_lf", "lf", "line1\nline2"),
    ("mixed_newlines", "crlf", "a\r\n\rb\n"),
    ("mixed_newlines_lf", "lf", "a\n\nb\n"),
    ("long_10000", "long", _long_value()),
    ("double_quotes", "quotes", 'He said "hi"'),
    ("single_quote", "quotes", "it's"),
    ("backslashes", "backslash", "C:\\path\\to\\cv.docx"),
    ("escape_lookalike", "backslash", "\\u0041 is not A"),
    ("line_separator", "line_separator", "a\u2028b"),
    ("paragraph_separator", "line_separator", "a\u2029b"),
    ("nul", "json_hostile", "a\u0000b"),
    ("unit_separator", "json_hostile", "\u001f"),
    ("delete", "json_hostile", "\u007f"),
    ("backspace_formfeed", "json_hostile", "\b\f"),
    ("script_close", "json_hostile", "</script><!--"),
    ("bom", "json_hostile", "\ufeffBOM"),
    ("astral_math", "json_hostile", "\U0001D400\U0001D401"),
    ("max_bmp", "json_hostile", "\uffff"),
]

# Inputs that must be REJECTED identically in both languages.
ERROR_VECTORS: list[tuple[str, str, str]] = [
    ("lone_high_surrogate", "unpaired_surrogate", "\ud800"),
    ("lone_low_surrogate_inside", "unpaired_surrogate", "a\udc00b"),
]

EQUIVALENCE_GROUPS = [
    ["nfc_precomposed", "nfd_decomposed"],
    ["a_ring_precomposed", "a_ring_decomposed", "angstrom_sign"],
    ["newline_crlf", "newline_lone_cr", "newline_lf"],
    ["mixed_newlines", "mixed_newlines_lf"],
]


def build() -> dict:
    vectors = [{"name": n, "category": c, "input": v, "expected": fill_value_hash(v)} for n, c, v in VECTORS]
    for name, category, value in ERROR_VECTORS:
        try:
            fill_value_hash(value)
        except UnpairedSurrogateError:
            vectors.append({"name": name, "category": category, "input": value, "error": "unpaired_surrogate"})
        else:  # pragma: no cover - a generator self-check
            raise AssertionError(f"{name} was expected to be rejected")
    by_name = {v["name"]: v for v in vectors}
    for group in EQUIVALENCE_GROUPS:
        if len({by_name[name]["expected"] for name in group}) != 1:
            raise AssertionError(f"equivalence group does not hash identically: {group}")
    return {
        "schema": "fill-hash-vectors",
        "schema_version": "v1",
        "generated_by": "tools/gen_fill_hash_vectors.py",
        "hash_function": "product.fill_hash.fill_value_hash",
        "vectors": vectors,
        "equivalence_groups": EQUIVALENCE_GROUPS,
    }


def render() -> str:
    return json.dumps(build(), ensure_ascii=True, indent=2) + "\n"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(render().encode("ascii"))
    print(f"wrote {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
