"""Release-journey fixtures (Bundle 7 spec §25.4): a job posting paired with a
Greenhouse-shaped fixture form, a fixture CV, and deterministic AI providers
keyed to them. The intelligence fake reads the request's own profile snapshot,
so its content units cite the evidence the signed-up account really has."""
from __future__ import annotations

import copy
from typing import Any

from product.application_intelligence_providers import DeterministicFakeProvider as IntelligenceFake
from product.cv_extraction_providers import FakeCvExtractionProvider
from product.job_understanding_providers import DeterministicFakeProvider as UnderstandingFake
from tests.webapp.fixtures.acceptance.fixtures import job_snapshot, provider_candidate
from tests.webapp.services.review_fixtures import docx_bytes
from webapp.services.semantic_proposal_adapter import FakeSemanticProposalAdapter

EMPLOYER = "http://localhost:8430"
TARGET_URL = f"{EMPLOYER}/acme/jobs/123?s=happy"  # tests/webapp/fixtures/fill/submit/apply.html
FAMILY = "Data Engineering"  # the fixture posting is a data-engineering role
CV_TEXT = "Ada Lovelace - Data Engineer. North Sea Energy, Basin Drilling, Rig Services. WellPlan, Python."


def cv_docx() -> bytes:
    return docx_bytes(CV_TEXT)


def posting() -> dict[str, str]:
    """What a person pastes: the company, the title and the exact posting text."""
    job = job_snapshot()
    return {"company": job["company"], "title": job["title"], "text": job["raw_text"]}


def _employment(title: str, employer: str, dates: str, detail: str) -> dict[str, Any]:
    return {"target": "PROFILE_ENTRY", "kind": "employment", "confidence": 0.9, "source_excerpt": title,
            "fields": {"job_title": title, "employer": employer, "date_range": dates, "location": "Aberdeen",
                       "details": detail}}


# What the CV import "extracts" from cv_docx(): three roles whose details are long
# enough for a CV bullet, a CV summary line and a cover-letter paragraph.
CV_PROPOSALS = [
    {"target": "PROFILE_ENTRY", "kind": "technical_skill", "confidence": 0.8, "source_excerpt": "WellPlan",
     "fields": {"subsection": "Software", "value": "WellPlan"}},
    _employment("Data Engineer", "North Sea Energy", "2019 - 2025",
                "Built and ran the Python data pipelines behind twelve offshore wells"),
    _employment("Well Engineer", "Basin Drilling", "2015 - 2019",
                "Led well design reviews for complex high pressure wells across three platforms in the northern "
                "North Sea basin region every year"),
    _employment("Graduate Engineer", "Rig Services", "2012 - 2015",
                "Wrote the drilling programme for every well and coordinated rig crews, service contractors and the "
                "regulator so that each plan was approved before spud, keeping the operation on schedule, within "
                "budget and inside every safety limit the company and the regulator had set for the whole campaign"),
]


class CountingUnderstanding(UnderstandingFake):
    def __init__(self) -> None:
        super().__init__(provider_candidate())
        self.calls: list[int] = []

    def extract(self, request):
        self.calls.append(1)
        return super().extract(request)


class EvidenceKeyedIntelligence(IntelligenceFake):
    """A CV bullet, a CV summary line and a cover-letter paragraph, each citing one
    of the request profile's responsibility claims (shortest to longest)."""

    def __init__(self) -> None:
        super().__init__({"content_units": []})

    def propose(self, request: dict[str, Any]):
        claims = sorted((c for c in request["profile_snapshot"]["claims"]
                         if c["field"] == "responsibility_or_achievement"), key=lambda c: len(c["value"].split()))
        units = [_unit(uid, utype, claim["id"]) for (uid, utype), claim in
                 zip((("cv-ready", "cv_bullet"), ("cv-summary-ready", "cv_summary_line"),
                      ("cover-ready", "cover_letter_paragraph")), claims)]
        self._payload = {"content_units": units}
        return super().propose(request)


def _unit(unit_id: str, unit_type: str, claim_id: str) -> dict[str, Any]:
    return {"unit_id": unit_id, "unit_type": unit_type, "connectives": [],
            "atoms": [{"atom_id": f"atom-{unit_id}", "atom_kind": "candidate_fact", "assertion_type": "responsibility",
                       "profile_evidence_ids": [claim_id], "rendering_variant": "PLAIN"}]}


def install_fake_providers(state: Any) -> None:
    state.cv_extraction_provider = FakeCvExtractionProvider(copy.deepcopy(CV_PROPOSALS))
    state.job_understanding_provider = CountingUnderstanding()
    state.semantic_adapter = FakeSemanticProposalAdapter(canned_response={"matches": [], "gates": []})
    state.application_intelligence_provider = EvidenceKeyedIntelligence()
