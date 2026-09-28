"""6D-B cross-language observation check (spec §7.5): the observations the
TypeScript observer emitted (extension/test/fill-observer.test.ts writes
tests/fixtures/fill/observation_from_ts.json) validate against the Python
schema, including every field fingerprint recomputed here and every adapter
non-application proof re-verified against the catalogue."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from product.fill_certification import CATALOGUE
from product.fill_hash import fill_value_hash
from product.fill_observation import unsupported_causes, validate_observation

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fill" / "observation_from_ts.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
OBS = DATA["observations"]


def _el(doc, key):
    return next(e for e in doc["elements"] if e["page_field_key"] == key)


def test_the_fixture_was_generated_by_the_typescript_observer():
    assert DATA["generated_by"] == "extension/test/fill-observer.test.ts"
    assert set(OBS) == {"greenhouse", "lever", "aria_only", "duplicate", "conditional", "wizard", "cross_origin"}


@pytest.mark.parametrize("name", sorted(OBS))
def test_every_ts_observation_validates_with_recomputed_fingerprints(name):
    validate_observation(OBS[name], CATALOGUE)


def test_ts_value_hashes_equal_python():
    import hashlib
    gh = OBS["greenhouse"]
    assert _el(gh, "gh:email")["value_state"]["current_value_hash"] == fill_value_hash("ada@example.com")
    digest = hashlib.sha256(bytes.fromhex(DATA["expected"]["resume_sha256_bytes"])).hexdigest()
    assert _el(gh, "gh:resume")["value_state"]["current_value_hash"] == fill_value_hash(digest)


def test_a_decomposed_label_fingerprints_as_its_nfc_form():
    from product.fill_observation import field_fingerprint
    city = _el(OBS["greenhouse"], "gh:city")
    assert city["identity"]["label"] == "Café"  # the page's decomposed text, as observed
    composed = {**city["identity"], "label": "Café", "question": "Café"}
    assert field_fingerprint(composed) == city["field_fingerprint"]


@pytest.mark.parametrize("name,causes", [
    ("greenhouse", []), ("lever", []), ("conditional", []),
    ("aria_only", ["AMBIGUOUS_FIELD_IDENTITY"]),
    ("duplicate", ["AMBIGUOUS_FIELD_IDENTITY"]),
    ("wizard", ["MULTI_STEP"]),
    ("cross_origin", ["CROSS_ORIGIN_APPLICATION_FRAME", "AMBIGUOUS_FIELD_IDENTITY"]),
])
def test_the_server_decides_the_expected_unsupported_causes(name, causes):
    assert unsupported_causes(OBS[name], CATALOGUE) == causes


def test_a_tampered_ts_fingerprint_is_refused():
    from product.fill_observation import ObservationError
    doc = json.loads(json.dumps(OBS["greenhouse"]))
    doc["elements"][2]["identity"]["label"] = "Email address"
    with pytest.raises(ObservationError):
        validate_observation(doc, CATALOGUE)
