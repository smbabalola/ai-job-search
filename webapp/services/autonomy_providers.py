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


def request_providers(state: Any, scope: Any, subject_type: str, subject_id: str) -> ProviderSet:
    """The providers an HTTP route uses (app.state overrides, else the
    production ones), each behind the Bundle 7 metered boundary (§13.2): the
    AI control and the hidden cost ceiling are checked before every call and
    each call's cost is recorded. The local operator account is unmetered."""
    from webapp.services.metered_provider import metered

    def wrap(provider: Any) -> Any:
        return metered(provider, scope, subject_type, subject_id, state=state)

    semantic = getattr(state, "semantic_adapter", None)
    if semantic is None:
        from webapp.services.openai_semantic_proposer_client import OpenAISemanticProposerClient
        from webapp.services.semantic_proposal_adapter import SemanticProposalAdapter
        semantic = SemanticProposalAdapter(wrap(OpenAISemanticProposerClient()))  # meter the model client itself
    else:
        semantic = wrap(semantic)
    understanding = getattr(state, "job_understanding_provider", None)
    if understanding is None:
        from product.openai_job_understanding_provider import OpenAIJobUnderstandingProvider
        understanding = OpenAIJobUnderstandingProvider()
    intelligence = getattr(state, "application_intelligence_provider", None)
    if intelligence is None:
        from product.openai_application_intelligence_provider import OpenAIApplicationIntelligenceProvider
        intelligence = OpenAIApplicationIntelligenceProvider()
    return ProviderSet(wrap(understanding), semantic, wrap(intelligence))


class CostMeter(Protocol):
    def actual_cost(self, step_kind: str, *, reserved: Decimal) -> Decimal | None: ...


class NoCostEvidence:
    """No reliable per-call cost evidence yet: every settlement uses the
    reserved hard maximum (spec §11.3)."""

    def actual_cost(self, step_kind: str, *, reserved: Decimal) -> Decimal | None:
        return None
