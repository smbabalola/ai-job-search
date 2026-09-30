"""Bundle 7 Task 22 (spec §14.2): job families and deterministic classification."""
from __future__ import annotations

import pytest

from product.job_families import JobFamiliesInvalid, classify, normalize_job_families


def _doc(*families):
    return normalize_job_families({"families": list(families)})


def _family(fid, name, title_any, *, title_none=(), seniority_in=(), priority=0):
    return {"id": fid, "name": name, "match": {"title_any": list(title_any), "title_none": list(title_none),
                                               "seniority_in": list(seniority_in)}, "priority": priority}


def test_a_whole_word_match():
    doc = _doc(_family("drilling", "Drilling", ["drilling"]))
    assert classify(doc, title="Senior Drilling Engineer", seniority=None).family_id == "drilling"
    assert classify(doc, title="Predrilling Analyst", seniority=None).family_id == "UNKNOWN"


def test_a_phrase_matches_whole_words_in_order():
    doc = _doc(_family("fluids", "Fluids", ["drilling fluids"]))
    assert classify(doc, title="Drilling Fluids Engineer", seniority=None).family_id == "fluids"
    assert classify(doc, title="Fluids Drilling Engineer", seniority=None).family_id == "UNKNOWN"


def test_title_none_excludes():
    doc = _doc(_family("drilling", "Drilling", ["drilling"], title_none=["sales"]))
    match = classify(doc, title="Drilling Sales Manager", seniority=None)
    assert match.family_id == "UNKNOWN"


def test_a_seniority_filter():
    doc = _doc(_family("lead", "Lead drilling", ["drilling"], seniority_in=["lead", "principal"]))
    assert classify(doc, title="Drilling Engineer", seniority="lead").family_id == "lead"
    assert classify(doc, title="Drilling Engineer", seniority="junior").family_id == "UNKNOWN"
    assert classify(doc, title="Drilling Engineer", seniority=None).family_id == "UNKNOWN"


def test_the_highest_priority_wins_and_a_tie_is_unknown():
    doc = _doc(_family("a", "A", ["engineer"], priority=1), _family("b", "B", ["drilling"], priority=5))
    assert classify(doc, title="Drilling Engineer", seniority=None).family_id == "b"
    tied = _doc(_family("a", "A", ["engineer"], priority=5), _family("b", "B", ["drilling"], priority=5))
    match = classify(tied, title="Drilling Engineer", seniority=None)
    assert match.family_id == "UNKNOWN" and match.reason == "tie" and sorted(match.matched) == ["a", "b"]


def test_nfkc_and_casefold():
    doc = _doc(_family("drilling", "Drilling", ["DRILLING"]))
    assert classify(doc, title="ｄｒｉｌｌｉｎｇ ENGINEER", seniority=None).family_id == "drilling"


def test_more_than_fifty_families_is_refused():
    with pytest.raises(JobFamiliesInvalid, match="50"):
        _doc(*[_family(f"f{i}", f"F{i}", [f"word{i}"]) for i in range(51)])


def test_invalid_documents_are_refused():
    with pytest.raises(JobFamiliesInvalid):
        normalize_job_families({"families": [_family("x", "X", [])]})  # nothing to match on
    with pytest.raises(JobFamiliesInvalid):
        normalize_job_families({"families": [_family("x", "X", ["a"]), _family("x", "Y", ["b"])]})  # duplicate id
    with pytest.raises(JobFamiliesInvalid):
        normalize_job_families({"families": [_family("x", "X", ["a"], seniority_in=["overlord"])]})
    with pytest.raises(JobFamiliesInvalid):
        normalize_job_families({"families": [], "extra": 1})


def test_an_empty_document_classifies_everything_as_unknown():
    assert classify(_doc(), title="Anything", seniority=None).reason == "no_match"
