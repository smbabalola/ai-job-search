"""The CV-extraction provider protocol and a deterministic fake (Bundle 7 §15.3)."""
from __future__ import annotations

from typing import Any, Protocol

from product.job_understanding_providers import ProviderResponse


class CvExtractionProviderError(RuntimeError):
    pass


class CvExtractionProvider(Protocol):
    provider_id: str
    model_id: str
    model_version: str

    def extract(self, request: dict[str, Any]) -> ProviderResponse:
        """Return untrusted proposals: {"proposals": [...]} in CV_EXTRACTION_SCHEMA."""


class FakeCvExtractionProvider:
    provider_id = "fake"
    model_id = "fake-cv-extraction"
    model_version = "fake"

    def __init__(self, proposals: list[dict[str, Any]]) -> None:
        self.proposals = list(proposals)
        self.calls = 0
        self.requests: list[dict[str, Any]] = []

    def extract(self, request: dict[str, Any]) -> ProviderResponse:
        self.calls += 1
        self.requests.append(request)
        return ProviderResponse(payload={"proposals": list(self.proposals)}, response_id=f"fake-{self.calls}")
