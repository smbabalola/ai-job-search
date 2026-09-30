"""Job families (Bundle 7 spec §14.2): `job-families.v1` documents and the
deterministic classifier. Matching is NFKC-normalised, casefolded, whole-word
matching over the job title (phrases match as consecutive whole words) plus an
optional seniority filter. The highest priority wins; a tie at the top, or no
match, is UNKNOWN."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping

from product.user_profile import SENIORITY_LEVELS

SCHEMA_VERSION = "job-families.v1"
MAX_FAMILIES = 50
MAX_TERMS = 30
MAX_TERM_LENGTH = 80
UNKNOWN = "UNKNOWN"
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class JobFamiliesInvalid(ValueError):
    pass


@dataclass(frozen=True)
class FamilyMatch:
    family_id: str
    matched: list[str] = field(default_factory=list)
    reason: str = "match"


def _fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def _terms(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_TERMS:
        raise JobFamiliesInvalid(f"{where} must be a list of at most {MAX_TERMS} terms")
    terms = []
    for term in value:
        folded = _fold(term) if isinstance(term, str) else ""
        if not folded or len(folded) > MAX_TERM_LENGTH:
            raise JobFamiliesInvalid(f"{where} terms must be 1 to {MAX_TERM_LENGTH} characters")
        terms.append(folded)
    return sorted(set(terms))


def normalize_job_families(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, Mapping) or set(doc) - {"families", "schema_version"}:
        raise JobFamiliesInvalid("a job-families document has only 'families'")
    families = doc.get("families")
    if not isinstance(families, list):
        raise JobFamiliesInvalid("families must be a list")
    if len(families) > MAX_FAMILIES:
        raise JobFamiliesInvalid(f"at most {MAX_FAMILIES} job families")
    out, seen = [], set()
    for family in families:
        if not isinstance(family, Mapping) or set(family) - {"id", "name", "match", "priority"}:
            raise JobFamiliesInvalid("a family has id, name, match and priority")
        family_id = family.get("id")
        if not isinstance(family_id, str) or not _ID.match(family_id) or family_id == UNKNOWN.casefold():
            raise JobFamiliesInvalid("a family id is 1-40 lowercase letters, digits, - or _")
        if family_id in seen:
            raise JobFamiliesInvalid(f"duplicate family id {family_id}")
        seen.add(family_id)
        name = " ".join(str(family.get("name") or "").split())
        if not name or len(name) > 80:
            raise JobFamiliesInvalid("a family name is 1 to 80 characters")
        match = family.get("match") or {}
        if not isinstance(match, Mapping) or set(match) - {"title_any", "title_none", "seniority_in"}:
            raise JobFamiliesInvalid("match has title_any, title_none and seniority_in")
        title_any = _terms(match.get("title_any", []), "title_any")
        if not title_any:
            raise JobFamiliesInvalid(f"family {family_id} needs at least one title word")
        seniority = match.get("seniority_in", [])
        if not isinstance(seniority, list) or any(s not in SENIORITY_LEVELS for s in seniority):
            raise JobFamiliesInvalid("seniority_in holds known seniority levels")
        priority = family.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int) or not -1000 <= priority <= 1000:
            raise JobFamiliesInvalid("priority is an integer from -1000 to 1000")
        out.append({"id": family_id, "name": name,
                    "match": {"title_any": title_any, "title_none": _terms(match.get("title_none", []), "title_none"),
                              "seniority_in": sorted(set(seniority))},
                    "priority": priority})
    return {"families": out}


def _contains(title: str, term: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", title) is not None


def classify(doc: Mapping[str, Any], *, title: str, seniority: str | None) -> FamilyMatch:
    folded = _fold(title)
    level = _fold(seniority or "") or None
    candidates = []
    for family in doc.get("families", []):
        match = family["match"]
        if not any(_contains(folded, term) for term in match["title_any"]):
            continue
        if any(_contains(folded, term) for term in match["title_none"]):
            continue
        if match["seniority_in"] and level not in match["seniority_in"]:
            continue
        candidates.append(family)
    if not candidates:
        return FamilyMatch(UNKNOWN, [], "no_match")
    top = max(f["priority"] for f in candidates)
    best = [f["id"] for f in candidates if f["priority"] == top]
    if len(best) > 1:
        return FamilyMatch(UNKNOWN, best, "tie")
    return FamilyMatch(best[0], [f["id"] for f in candidates], "match")
