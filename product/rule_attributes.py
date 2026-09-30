"""Pure derivation of the standing-policy v2 job attributes (Bundle 7 spec
§16.2). Everything that cannot be derived deterministically is UNKNOWN (the
three-valued predicates' unknown), never a guess: no FX conversion, no
location geocoding, no inference of a rotation from other numbers."""
from __future__ import annotations

import re
from typing import Any, Mapping

from product.autonomy_contract import UNKNOWN

__all__ = ["UNKNOWN", "annual_compensation_max", "country_from", "family_attribute", "remote_mode_from",
           "rotation_from_text"]

_ROTATION = re.compile(r"\b(\d{1,2})\s*/\s*(\d{1,2})\b")
_WORD = re.compile(r"rotation")
WINDOW = 40
PERIOD_MULTIPLIERS = {"hour": 2080, "day": 260, "month": 12, "year": 1}


def rotation_from_text(text: str | None) -> Any:
    """The first "N/M" within 40 characters of the word "rotation" (casefolded)."""
    if not text:
        return UNKNOWN
    folded = text.casefold()
    words = [m.span() for m in _WORD.finditer(folded)]
    if not words:
        return UNKNOWN
    for match in _ROTATION.finditer(folded):
        start, end = match.span()
        for word_start, word_end in words:
            gap = word_start - end if word_start >= end else start - word_end
            if gap <= WINDOW:
                return f"{int(match.group(1))}/{int(match.group(2))}"
    return UNKNOWN


def annual_compensation_max(compensation: Mapping[str, Any] | None, currency: str | None) -> Any:
    """The highest stated figure annualised (hour ×2080, day ×260, month ×12)
    in ``currency``; a different currency is UNKNOWN (no FX)."""
    if not isinstance(compensation, Mapping) or not currency:
        return UNKNOWN
    if str(compensation.get("currency") or "").upper() != currency.upper():
        return UNKNOWN
    multiplier = PERIOD_MULTIPLIERS.get(compensation.get("period"))
    figure = compensation.get("max") if compensation.get("max") is not None else compensation.get("min")
    if multiplier is None or isinstance(figure, bool) or not isinstance(figure, (int, float)) or figure < 0:
        return UNKNOWN
    return int(round(figure * multiplier))


def country_from(payload: Mapping[str, Any] | None) -> Any:
    """ISO-3166 alpha-2 from the job understanding's location, else UNKNOWN."""
    payload = payload or {}
    location = payload.get("location")
    candidates = [payload.get("country_code"), payload.get("country")]
    if isinstance(location, Mapping):
        candidates = [location.get("country_code"), location.get("country")] + candidates
    for value in candidates:
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{2}", value.strip()):
            return value.strip().upper()
    return UNKNOWN


def remote_mode_from(payload: Mapping[str, Any] | None) -> Any:
    value = (payload or {}).get("remote_mode") or (payload or {}).get("work_mode")
    return value.strip().casefold() if isinstance(value, str) and value.strip() else UNKNOWN


def family_attribute(family_id: str | None) -> Any:
    return UNKNOWN if not family_id or family_id == "UNKNOWN" else family_id
