"""Bundle 6D-B canonical value hash (spec §7.5). The single function behind
both `rendered_value_hash` (server) and `current_value_hash` (observer).
extension/src/fill/hash.ts must produce byte-identical results; the shared
vectors in tests/fixtures/fill/hash_vectors.json prove it.

The normalization order is exact:
1. Unicode NFC;
2. CRLF, then lone CR, to LF;
3. canonical_hash("fill-rendered-value", "v1", normalized).

Nothing else is trimmed or changed. An unpaired UTF-16 surrogate can't be
encoded as UTF-8, so it is rejected in both languages rather than
replaced (JavaScript's TextEncoder would silently substitute U+FFFD,
giving a different hash)."""
from __future__ import annotations

import re
import unicodedata

from product.autonomy_contract import canonical_hash

FILL_VALUE_HASH_SCHEMA = "fill-rendered-value"
FILL_VALUE_HASH_VERSION = "v1"
_SURROGATE = re.compile("[\ud800-\udfff]")


class UnpairedSurrogateError(ValueError):
    """The value contains an unpaired UTF-16 surrogate code point."""


def normalize_fill_value(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"fill values are strings, got {type(value).__name__}")
    if _SURROGATE.search(value):
        # A Python str holds code points: any surrogate here is unpaired.
        raise UnpairedSurrogateError("value contains an unpaired surrogate")
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def fill_value_hash(value: str) -> str:
    return canonical_hash(FILL_VALUE_HASH_SCHEMA, FILL_VALUE_HASH_VERSION, normalize_fill_value(value))
