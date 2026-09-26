"""Model providers and cost evidence for the 6C scheduler. The in-app driver
uses app.state overrides exactly like the HTTP routes; the CLI uses the
production providers."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderSet:
    understanding: Any
    semantic_adapter: Any
    intelligence: Any


def default_providers() -> ProviderSet:
    from product.openai_application_intelligence_provider import OpenAIApplicationIntelligenceProvider
    from product.openai_job_understanding_provider import OpenAIJobUnderstandingProvider
    from webapp.services.openai_semantic_proposer_client import OpenAISemanticProposerClient
    from webapp.services.semantic_proposal_adapter import SemanticProposalAdapter
    return ProviderSet(OpenAIJobUnderstandingProvider(), SemanticProposalAdapter(OpenAISemanticProposerClient()),
                       OpenAIApplicationIntelligenceProvider())


def providers_from_app_state(state: Any) -> ProviderSet:
    defaults: ProviderSet | None = None

    def pick(name: str, field: str):
        nonlocal defaults
        override = getattr(state, name, None)
        if override is not None:
            return override
        defaults = defaults or default_providers()
        return getattr(defaults, field)
    return ProviderSet(pick("job_understanding_provider", "understanding"),
                       pick("semantic_adapter", "semantic_adapter"),
                       pick("application_intelligence_provider", "intelligence"))


class CostMeter(Protocol):
    def actual_cost(self, step_kind: str, *, reserved: Decimal) -> Decimal | None: ...


class NoCostEvidence:
    """No reliable per-call cost evidence yet: every settlement uses the
    reserved hard maximum (spec §11.3)."""

    def actual_cost(self, step_kind: str, *, reserved: Decimal) -> Decimal | None:
        return None
