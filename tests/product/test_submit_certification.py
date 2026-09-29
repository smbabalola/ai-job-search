"""6E-A spec §9: the submit-certification.v1 catalogue, egress templates
resolved with every bound value escaped, and the permission rule (§9.5)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from product.submit_certification import (
    SUBMIT_CATALOGUE, is_loopback_origin, resolve_egress, submission_permitted, submit_certified,
)

VECTORS = Path(__file__).parents[1] / "fixtures" / "submit" / "egress_vectors.json"
GH = submit_certified("greenhouse", "greenhouse@2")


def test_the_catalogue_has_exactly_greenhouse_fixture_certified():
    assert list(SUBMIT_CATALOGUE) == [("greenhouse", "greenhouse@2")]
    assert GH.certification_id == "greenhouse@2/submit@1"
    assert GH.status == "FIXTURE_CERTIFIED" and GH.live_evidence is None
    assert GH.failure_signal_proves_not_submitted is True
    assert submit_certified("lever", "lever@2") is None
    assert [e.id for e in GH.egress] == ["E1_SUBMIT", "E2_CONFIRM", "E3_RENDER", "C1_RECAPTCHA"]


def test_resolved_egress_is_exactly_the_spec_table():
    out = resolve_egress(GH, origin="http://127.0.0.1:8430", tenant_key="acme", ats_job_id="123")
    assert out == [
        {"id": "E1_SUBMIT", "methods": ["post"], "types": ["main_frame", "xmlhttprequest"],
         "regex": r"^http://127\.0\.0\.1:8430/acme/jobs/123$"},
        {"id": "E2_CONFIRM", "methods": ["get"], "types": ["main_frame", "xmlhttprequest"],
         "regex": r"^http://127\.0\.0\.1:8430/acme/jobs/123/confirmation(\?.*)?$"},
        {"id": "E3_RENDER", "methods": ["get"], "types": ["stylesheet", "script", "image", "font"],
         "regex": r"^http://127\.0\.0\.1:8430/.*$"},
        {"id": "C1_RECAPTCHA", "methods": ["get", "post"], "types": ["script", "sub_frame", "xmlhttprequest", "image"],
         "regex": r"^https://www\.(google|gstatic|recaptcha)\.(com|net)/recaptcha/.*$"},
    ]


def test_bound_values_are_escaped_so_a_lookalike_never_matches():  # Review Focus 5
    out = resolve_egress(GH, origin="https://boards.greenhouse.io", tenant_key="acme.co+x", ats_job_id="9")
    e1 = out[0]["regex"]
    assert r"acme\.co\+x" in e1
    assert re.fullmatch(e1[1:-1], "https://boards.greenhouse.io/acme.co+x/jobs/9")
    assert not re.fullmatch(e1[1:-1], "https://boards.greenhouse.io/acmeXco+x/jobs/9")
    assert not re.fullmatch(e1[1:-1], "https://boards.greenhouse.io/acme.cooox/jobs/9")
    assert not re.fullmatch(e1[1:-1], "https://boardsXgreenhouse.io/acme.co+x/jobs/9")


@pytest.mark.parametrize("origin,expected", [
    ("http://127.0.0.1:8430", True), ("http://localhost:8430", True), ("http://localhost", True),
    ("https://127.0.0.1:8430", False), ("http://localhost.evil.com", False), ("http://127.0.0.1.nip.io", False),
    ("https://boards.greenhouse.io", False),
])
def test_loopback_origins(origin, expected):
    assert is_loopback_origin(origin) is expected


def test_submission_permitted_rules():
    assert submission_permitted(None, "http://127.0.0.1:8430", fixture_origins_enabled=True) == \
        (False, "adapter_not_submit_certified")
    assert submission_permitted(GH, "https://boards.greenhouse.io", fixture_origins_enabled=True) == \
        (False, "adapter_not_live_certified")
    assert submission_permitted(GH, "http://127.0.0.1:8430", fixture_origins_enabled=False) == \
        (False, "adapter_not_live_certified")
    assert submission_permitted(GH, "http://127.0.0.1:8430", fixture_origins_enabled=True) == (True, None)
    assert submission_permitted(GH, "http://localhost.evil.com", fixture_origins_enabled=True) == \
        (False, "adapter_not_live_certified")


def test_live_certified_without_evidence_is_not_permitted():
    from dataclasses import replace
    live_no_evidence = replace(GH, status="LIVE_CERTIFIED", live_evidence=None)
    assert submission_permitted(live_no_evidence, "https://boards.greenhouse.io", fixture_origins_enabled=False) == \
        (False, "adapter_not_live_certified")
    live = replace(GH, status="LIVE_CERTIFIED", live_evidence="docs/superpowers/notes/x.md")
    assert submission_permitted(live, "https://boards.greenhouse.io", fixture_origins_enabled=False) == (True, None)


def test_shared_vectors_file_matches_python():
    """The TypeScript suite asserts the same vectors (extension/test/submit-certification.test.ts)."""
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    for v in vectors:
        assert resolve_egress(GH, origin=v["origin"], tenant_key=v["tenant_key"], ats_job_id=v["ats_job_id"]) \
            == v["expected"], v["name"]
