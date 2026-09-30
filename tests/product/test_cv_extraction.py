"""Bundle 7 Task 24 (spec §15.3): the CV-extraction proposal contract. Proposals
are validated against the profile entry definitions and the answer-subject
registry; an invalid proposal is dropped and counted, never guessed at."""
from __future__ import annotations

import pytest

from product.cv_extraction import (
    CV_EXTRACTION_SCHEMA, MAX_PROPOSALS, MAX_TEXT_CHARACTERS, build_request, validate_proposals,
)
from product.cv_extraction_providers import FakeCvExtractionProvider

ENTRY_KINDS = {"employment": ("job_title", "employer", "date_range", "location", "details"),
               "education": ("qualification", "date_range", "institution", "key_topics"),
               "technical_skill": ("subsection", "value"), "certification": ("value",),
               "language": ("language", "proficiency", "evidence"), "achievement": ("value",)}
SUBJECTS = {"contact.phone", "employment.notice_period", "work_authorization.right_to_work"}


def _validate(proposals):
    return validate_proposals({"proposals": proposals}, entry_fields=ENTRY_KINDS, answer_subjects=SUBJECTS)


def test_a_valid_entry_and_answer_pass():
    valid, dropped = _validate([
        {"target": "PROFILE_ENTRY", "kind": "employment", "confidence": 0.9, "source_excerpt": "Drilling Engineer, Acme",
         "fields": {"job_title": "Drilling Engineer", "employer": "Acme", "date_range": "2019-2024",
                    "location": "Aberdeen", "details": "Planned wells."}},
        {"target": "ANSWER", "kind": "employment.notice_period", "confidence": 0.7, "source_excerpt": "3 months notice",
         "fields": {"value": "3 months"}},
    ])
    assert dropped == 0 and [p["kind"] for p in valid] == ["employment", "employment.notice_period"]


def test_invalid_proposals_are_dropped_and_counted():
    valid, dropped = _validate([
        {"target": "PROFILE_ENTRY", "kind": "hobby", "confidence": 0.9, "source_excerpt": "", "fields": {"value": "x"}},
        {"target": "PROFILE_ENTRY", "kind": "certification", "confidence": 0.9, "source_excerpt": "",
         "fields": {"value": "BOSIET", "extra": "no"}},                                                  # unknown field
        {"target": "PROFILE_ENTRY", "kind": "education", "confidence": 0.9, "source_excerpt": "", "fields": {}},  # empty
        {"target": "ANSWER", "kind": "made.up", "confidence": 0.5, "source_excerpt": "", "fields": {"value": "y"}},
        {"target": "ELSEWHERE", "kind": "certification", "confidence": 0.5, "source_excerpt": "",
         "fields": {"value": "z"}},
        {"target": "PROFILE_ENTRY", "kind": "certification", "confidence": 3, "source_excerpt": "",
         "fields": {"value": "IWCF"}},                                                                   # bad confidence
        {"target": "PROFILE_ENTRY", "kind": "certification", "confidence": 0.8, "source_excerpt": "IWCF level 4",
         "fields": {"value": "IWCF Level 4"}},
    ])
    assert dropped == 6 and [p["fields"]["value"] for p in valid] == ["IWCF Level 4"]


def test_a_malformed_payload_drops_everything():
    assert validate_proposals({"nope": []}, entry_fields=ENTRY_KINDS, answer_subjects=SUBJECTS) == ([], 0)
    assert validate_proposals(["x"], entry_fields=ENTRY_KINDS, answer_subjects=SUBJECTS) == ([], 0)


def test_the_request_is_bounded():
    request = build_request("x" * (MAX_TEXT_CHARACTERS + 50), request_id="r1", entry_fields=ENTRY_KINDS,
                            answer_subjects=SUBJECTS)
    assert len(request["cv_text"]) == MAX_TEXT_CHARACTERS and request["truncated"] is True
    assert sorted(request["entry_kinds"]) == sorted(ENTRY_KINDS) and sorted(request["answer_subjects"]) == sorted(
        SUBJECTS)
    too_many = [{"target": "PROFILE_ENTRY", "kind": "certification", "confidence": 0.5, "source_excerpt": "",
                 "fields": {"value": f"c{i}"}} for i in range(MAX_PROPOSALS + 5)]
    valid, dropped = _validate(too_many)
    assert len(valid) == MAX_PROPOSALS and dropped == 5


def test_the_schema_is_strict():
    assert CV_EXTRACTION_SCHEMA["additionalProperties"] is False
    item = CV_EXTRACTION_SCHEMA["properties"]["proposals"]["items"]
    assert item["additionalProperties"] is False and set(item["required"]) == {
        "target", "kind", "fields", "source_excerpt", "confidence"}


def test_the_fake_provider_returns_its_proposals_and_records_calls():
    provider = FakeCvExtractionProvider([{"target": "PROFILE_ENTRY"}])
    response = provider.extract({"request_id": "r", "cv_text": "text"})
    assert response.payload == {"proposals": [{"target": "PROFILE_ENTRY"}]} and provider.calls == 1
