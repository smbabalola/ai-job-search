"""Closed, deterministic format-only transforms (6B spec §7.2). Each transform
has a canonical form; apply_transform proves the underlying value is
unchanged by round-tripping through it, and fails closed otherwise."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

_COUNTRIES: dict[str, tuple[str, ...]] = {
    "GB": ("United Kingdom", "UK", "Great Britain", "Britain"),
    "US": ("United States", "USA", "United States of America"),
    "AE": ("United Arab Emirates", "UAE"),
    "SA": ("Saudi Arabia", "KSA"),
    "QA": ("Qatar",), "KW": ("Kuwait",), "OM": ("Oman",), "BH": ("Bahrain",),
    "NO": ("Norway",), "NL": ("Netherlands",), "DE": ("Germany",), "FR": ("France",),
    "IE": ("Ireland",), "CA": ("Canada",), "AU": ("Australia",), "NG": ("Nigeria",),
    "IN": ("India",),
}
_COUNTRY_LOOKUP = {code.casefold(): code for code in _COUNTRIES}
_COUNTRY_LOOKUP.update({name.casefold(): code for code, names in _COUNTRIES.items() for name in names})


class TransformError(ValueError):
    pass


def _country_code(value: str) -> str:
    code = _COUNTRY_LOOKUP.get(" ".join(value.split()).casefold())
    if code is None:
        raise TransformError(f"unknown country: {value!r}")
    return code


def _iso_date(value: str) -> date:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return date.fromisoformat(value.strip())
    raise TransformError(f"not an ISO date: {value!r}")


def _date_canonical_dmy(value: str) -> str:
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", value.strip())
    if m:
        return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
    return _iso_date(value).isoformat()


def _date_canonical_mdy(value: str) -> str:
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", value.strip())
    if m:
        return date(int(m[3]), int(m[1]), int(m[2])).isoformat()
    return _iso_date(value).isoformat()


def _phone_canonical(value: str) -> str:
    text = value.strip()
    if not text.startswith("+"):
        raise TransformError("phone must be in international format starting with '+'")
    text = text.replace("(0)", "")
    digits = re.sub(r"[\s\-().]", "", text[1:])
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        raise TransformError(f"not a valid international phone number: {value!r}")
    return "+" + digits


@dataclass(frozen=True)
class _Transform:
    apply: Callable[[str], str]
    canonical: Callable[[str], str]


_TRANSFORMS: dict[str, _Transform] = {
    "identity": _Transform(lambda v: v, lambda v: v),
    "whitespace_normalize": _Transform(lambda v: " ".join(v.split()), lambda v: " ".join(v.split())),
    "country_name_to_iso2": _Transform(_country_code, _country_code),
    "country_iso2_to_name": _Transform(lambda v: _COUNTRIES[_country_code(v)][0], _country_code),
    "date_iso_to_dmy": _Transform(lambda v: _iso_date(v).strftime("%d/%m/%Y"), _date_canonical_dmy),
    "date_iso_to_mdy": _Transform(lambda v: _iso_date(v).strftime("%m/%d/%Y"), _date_canonical_mdy),
    "phone_e164": _Transform(_phone_canonical, _phone_canonical),
}
TRANSFORM_IDS = frozenset(_TRANSFORMS)


def apply_transform(transform_id: str, value: str) -> str:
    transform = _TRANSFORMS.get(transform_id)
    if transform is None:
        raise TransformError(f"unknown transform: {transform_id!r}")
    if not isinstance(value, str):
        raise TransformError("transforms apply to strings only")
    result = transform.apply(value)
    if transform.canonical(result) != transform.canonical(value):
        raise TransformError(f"{transform_id} changed the underlying value")
    return result
