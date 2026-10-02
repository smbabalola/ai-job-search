"""CV extraction proposals (Bundle 7 spec §15.3).

A provider reads the CV text and returns *proposals* in a strict schema; the
server validates each against the profile entry definitions and the
answer-subject registry. An invalid proposal is dropped and counted. A
proposal is never a fact: nothing reaches the profile or the answers until
the user accepts, edits or rejects it."""
from __future__ import annotations

from typing import Any, Iterable, Mapping

MAX_TEXT_CHARACTERS = 60_000
MAX_PROPOSALS = 80
MAX_FIELD_LENGTH = 600
MAX_EXCERPT_LENGTH = 300
TARGETS = ("PROFILE_ENTRY", "ANSWER")

CV_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["proposals"],
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target", "kind", "fields", "source_excerpt", "confidence"],
                "properties": {
                    "target": {"type": "string", "enum": list(TARGETS)},
                    "kind": {"type": "string"},
                    "fields": {"type": "object", "additionalProperties": {"type": "string"}},
                    "source_excerpt": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
    },
}

INSTRUCTIONS = (
    "Extract facts from the candidate's CV as proposals for them to confirm. Use only what the CV states; never "
    "infer, embellish or combine. For PROFILE_ENTRY use one of the given entry kinds with exactly its fields. For "
    "ANSWER use one of the given answer subjects with the single field 'value'. Quote the CV text that supports "
    "each proposal in source_excerpt. Confidence is 0 to 1."
)


def build_request(text: str, *, request_id: str, entry_fields: Mapping[str, Iterable[str]],
                  answer_subjects: Iterable[str]) -> dict[str, Any]:
    clean = (text or "")[:MAX_TEXT_CHARACTERS]
    return {"request_id": request_id, "cv_text": clean, "truncated": len(text or "") > MAX_TEXT_CHARACTERS,
            "entry_kinds": {kind: list(fields) for kind, fields in entry_fields.items()},
            "answer_subjects": sorted(answer_subjects), "instructions": INSTRUCTIONS}


def _clean(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] if text else None


def _valid(item: Any, entry_fields: Mapping[str, Iterable[str]], answer_subjects: set[str]) -> dict | None:
    if not isinstance(item, Mapping) or set(item) != {"target", "kind", "fields", "source_excerpt", "confidence"}:
        return None
    target, kind, fields = item["target"], item["kind"], item["fields"]
    confidence = item["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        return None
    if not isinstance(fields, Mapping) or not fields:
        return None
    if target == "PROFILE_ENTRY":
        allowed = set(entry_fields.get(kind) or ())
        if not allowed or set(fields) - allowed:
            return None
    elif target == "ANSWER":
        if kind not in answer_subjects or set(fields) != {"value"}:
            return None
    else:
        return None
    cleaned = {}
    for name, value in fields.items():
        text = _clean(value, MAX_FIELD_LENGTH)
        if text is not None:
            cleaned[name] = text
    if not cleaned:
        return None
    excerpt = item["source_excerpt"] if isinstance(item["source_excerpt"], str) else ""
    return {"target": target, "kind": kind, "fields": cleaned,
            "source_excerpt": " ".join(excerpt.split())[:MAX_EXCERPT_LENGTH], "confidence": float(confidence)}


def validate_proposals(payload: Any, *, entry_fields: Mapping[str, Iterable[str]],
                       answer_subjects: Iterable[str]) -> tuple[list[dict[str, Any]], int]:
    """(valid proposals, dropped count). At most MAX_PROPOSALS are kept."""
    if not isinstance(payload, Mapping) or not isinstance(payload.get("proposals"), list):
        return [], 0
    subjects = set(answer_subjects)
    valid, dropped = [], 0
    for item in payload["proposals"]:
        checked = _valid(item, entry_fields, subjects)
        if checked is None or len(valid) >= MAX_PROPOSALS:
            dropped += 1
            continue
        valid.append(checked)
    return valid, dropped
