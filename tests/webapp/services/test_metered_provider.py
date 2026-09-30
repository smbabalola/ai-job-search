"""Bundle 7 Task 17 (spec §13.2 U3): the metered AI provider boundary and the
hidden per-account AI cost ceiling."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from product.entitlements import FeatureNotInPlan, load_catalog
from webapp.config import Settings
from webapp.persistence import identity
from webapp.persistence.db import connect, init_db
from webapp.services.entitlements import EntitlementGate, set_platform_control
from webapp.services.metered_provider import (
    FairUseLimitReached, MeteredProvider, PricingInvalid, load_pricing, parse_pricing,
)
from webapp.services.ownership import AccountScope
from webapp.services.usage import Metering, UsageService
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[3]
DEV = load_catalog(ROOT / "product" / "plans" / "plan-catalog.dev.json")  # Free ceiling 500000 µ$
PRICING = parse_pricing({
    "pricing_version": "test", "currency": "USD", "missing_usage_call_micro_usd": 7000,
    "models": {"default": {"input_per_mtok_micro_usd": 2_000_000, "output_per_mtok_micro_usd": 8_000_000},
               "gpt-5.4-mini": {"input_per_mtok_micro_usd": 1_000_000, "output_per_mtok_micro_usd": 4_000_000}},
})


class Audit:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class Response:
    def __init__(self, audit):
        self.payload = {"ok": True}
        self.audit = audit


class FakeProvider:
    provider_id = "openai"
    model_version = "fake"

    def __init__(self, model_id="gpt-5.4-mini", usage=(1000, 500)):
        self.model_id = model_id
        self.usage = usage
        self.calls = 0

    def extract(self, request):
        self.calls += 1
        return Response(None if self.usage is None else Audit(*self.usage))


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    conn = connect(path)
    created = identity.create_user_with_account(
        conn, email="ada@example.com", password_hash="h", display_name="Ada", legal_document_ids=[],
        now=NOW, profile_store=DatabaseProfileSourceStore())
    conn.commit()
    scope = AccountScope(account_id=created["account"]["id"], profile_root=tmp_path, user_id=created["user"]["id"])
    settings = Settings(db_path=path)
    gate = EntitlementGate(DEV, settings=settings)
    yield path, conn, scope, settings, gate
    conn.close()


def _metered(world, inner):
    path, _, scope, _, gate = world
    return MeteredProvider(inner, conn_factory=lambda: connect(path), scope=scope, subject_type="workspace",
                           subject_id="ws_1", pricing=PRICING, gate=gate, clock=lambda: NOW)


def _spend(conn, scope, micro_usd, *, created_at="2026-10-02T00:00:00.000000+00:00"):
    conn.execute("INSERT INTO ai_cost_events (id, account_id, subject_type, subject_id, provider, model, input_tokens, "
                 "output_tokens, cost_micro_usd, request_ref, created_at) VALUES (?, ?, 'workspace', 'ws_0', 'openai', "
                 "'gpt-5.4-mini', 0, 0, ?, NULL, ?)", (f"aic_{micro_usd}_{created_at}", scope.account_id, micro_usd,
                                                        created_at))
    conn.commit()


def _events(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT model, input_tokens, output_tokens, cost_micro_usd FROM ai_cost_events ORDER BY seq")]


def test_a_call_records_its_cost_from_the_response_usage(world):
    conn = world[1]
    inner = FakeProvider()
    assert _metered(world, inner).extract({"request_id": "r1"}).payload == {"ok": True}
    # 1000 in × 1 $/Mtok + 500 out × 4 $/Mtok = 1000 + 2000 µ$
    assert _events(conn) == [("gpt-5.4-mini", 1000, 500, 3000)]


def test_the_ceiling_reached_refuses_before_the_call(world):
    _, conn, scope, _, _ = world
    _spend(conn, scope, 500_000)  # the Free ceiling
    inner = FakeProvider()
    with pytest.raises(FairUseLimitReached):
        _metered(world, inner).extract({})
    assert inner.calls == 0


def test_spend_in_an_earlier_window_does_not_count(world):
    _, conn, scope, _, _ = world
    _spend(conn, scope, 900_000, created_at="2026-09-30T23:59:59.999999+00:00")
    inner = FakeProvider()
    _metered(world, inner).extract({})
    assert inner.calls == 1


def test_the_crossing_call_completes_and_records_then_the_next_is_refused(world):
    _, conn, scope, _, _ = world
    _spend(conn, scope, 499_999)
    inner = FakeProvider()
    metered = _metered(world, inner)
    metered.extract({})
    assert inner.calls == 1 and _events(conn)[-1][-1] == 3000
    with pytest.raises(FairUseLimitReached):
        metered.extract({})
    assert inner.calls == 1


def test_an_unknown_model_is_costed_at_the_default_rate(world):
    conn = world[1]
    _metered(world, FakeProvider(model_id="some-new-model")).extract({})
    assert _events(conn) == [("some-new-model", 1000, 500, 6000)]  # 1000 × 2 + 500 × 8


def test_a_response_without_usage_is_costed_at_the_missing_usage_rate(world):
    conn = world[1]
    _metered(world, FakeProvider(usage=None)).extract({})
    assert _events(conn) == [("gpt-5.4-mini", 0, 0, 7000)]


def test_a_pricing_file_without_a_default_rate_is_invalid():
    with pytest.raises(PricingInvalid, match="default"):
        parse_pricing({"pricing_version": "x", "currency": "USD", "missing_usage_call_micro_usd": 1,
                       "models": {"gpt-5.4-mini": {"input_per_mtok_micro_usd": 1, "output_per_mtok_micro_usd": 1}}})


def test_negative_or_non_integer_rates_are_invalid():
    for bad in (-1, 1.5, "1", True):
        with pytest.raises(PricingInvalid):
            parse_pricing({"pricing_version": "x", "currency": "USD", "missing_usage_call_micro_usd": 1,
                           "models": {"default": {"input_per_mtok_micro_usd": bad, "output_per_mtok_micro_usd": 1}}})


def test_the_shipped_pricing_files_parse(tmp_path):
    dev = load_pricing(ROOT / "product" / "policies" / "ai-pricing.dev.json")
    assert dev.configured
    operator = load_pricing(ROOT / "product" / "policies" / "ai-pricing.v1.json")
    assert not operator.configured  # DP-3: operator values are unresolved, never invented


def test_unconfigured_pricing_refuses_every_call(world):
    operator = load_pricing(ROOT / "product" / "policies" / "ai-pricing.v1.json")
    path, _, scope, _, gate = world
    inner = FakeProvider()
    metered = MeteredProvider(inner, conn_factory=lambda: connect(path), scope=scope, subject_type="workspace",
                              subject_id="ws_1", pricing=operator, gate=gate, clock=lambda: NOW)
    with pytest.raises(FairUseLimitReached):
        metered.extract({})
    assert inner.calls == 0


def test_ai_disabled_by_the_platform_control_refuses_before_any_call(world):
    _, conn, _, _, _ = world
    set_platform_control(conn, "AI_ENABLED", False, actor_user_id=None, reason="incident", now=NOW)
    conn.commit()
    inner = FakeProvider()
    with pytest.raises(FeatureNotInPlan):
        _metered(world, inner).extract({})
    assert inner.calls == 0


def test_other_attributes_pass_through_to_the_inner_provider(world):
    metered = _metered(world, FakeProvider())
    assert (metered.provider_id, metered.model_id, metered.model_version) == ("openai", "gpt-5.4-mini", "fake")


def test_a_failed_metered_stage_still_records_the_cost_it_incurred(world):
    """Inside Metering the event is buffered and written in the settle
    transaction, so the stage's own rollback never loses spend."""
    _, conn, scope, _, gate = world
    metering = Metering(gate, UsageService(gate), enforced=True, clock=lambda: NOW)
    metered = _metered(world, FakeProvider())

    def stage():
        metered.extract({})
        raise RuntimeError("validation failed after the call")

    with pytest.raises(RuntimeError):
        metering.prepare(conn, scope, "ws_1", stage, stage="understand")
    assert _events(conn) == [("gpt-5.4-mini", 1000, 500, 3000)]


def test_a_stage_holding_an_open_write_transaction_is_not_blocked_by_cost_recording(world):
    """SQLite: the pipeline writes artifacts before the provider call, so the
    request connection holds the write lock while the provider runs."""
    _, conn, scope, _, gate = world
    metering = Metering(gate, UsageService(gate), enforced=True, clock=lambda: NOW)
    metered = _metered(world, FakeProvider())

    def stage():
        set_platform_control(conn, "SIGNUPS_ENABLED", True, actor_user_id=None, reason="open write", now=NOW)
        assert conn.in_transaction
        return metered.extract({}).payload

    assert metering.prepare(conn, scope, "ws_1", stage, stage="understand") == {"ok": True}
    assert _events(conn) == [("gpt-5.4-mini", 1000, 500, 3000)]


def test_the_semantic_client_audit_surfaces_token_usage(monkeypatch):
    from webapp.services import openai_semantic_proposer_client as module
    from webapp.services.openai_semantic_proposer_client import OpenAISemanticProposerClient

    monkeypatch.setattr(module, "_decode_response", lambda response: {"matches": [], "gates": []})

    class Usage:
        input_tokens, output_tokens, total_tokens = 11, 22, 33

    class Resp:
        id = "resp_1"
        usage = Usage()
        output_text = json.dumps({"matches": [], "gates": []})
        status = "completed"

    class Client:
        class responses:
            @staticmethod
            def create(**kwargs):
                return Resp()

    client = OpenAISemanticProposerClient(environ={"OPENAI_API_KEY": "k"}, client_factory=lambda key: Client())
    assert client.complete({}) == {"matches": [], "gates": []}  # the output is unchanged
    assert client.last_audit["input_tokens"] == 11 and client.last_audit["output_tokens"] == 22


def test_the_intelligence_provider_audit_surfaces_token_usage(monkeypatch):
    from product import openai_application_intelligence_provider as module
    from product.openai_application_intelligence_provider import OpenAIApplicationIntelligenceProvider

    monkeypatch.setattr(module, "_hosted_input", lambda request: "input")
    monkeypatch.setattr(module, "_decode_response", lambda response: {"selections": []})

    class Usage:
        input_tokens, output_tokens, total_tokens = 5, 6, 11

    class Resp:
        id = "resp_2"
        usage = Usage()
        output_text = json.dumps({"selections": []})
        status = "completed"

    class Client:
        class responses:
            @staticmethod
            def create(**kwargs):
                return Resp()

    provider = OpenAIApplicationIntelligenceProvider(environ={"OPENAI_API_KEY": "k"},
                                                     client_factory=lambda key: Client())
    assert provider.propose({"request_id": "r"}).payload == {"selections": []}
    audit = provider.last_audit
    assert audit is not None and (audit.input_tokens, audit.output_tokens) == (5, 6)


# ---- bounded one-call overshoot: every metered provider call has a hard size bound ----

class _NeverCalledClient:
    class responses:
        @staticmethod
        def create(**kwargs):
            raise AssertionError("an oversize input must be refused before the provider call")


def test_every_openai_provider_caps_output_and_input():
    from product import openai_application_intelligence_provider as intelligence
    from product import openai_job_understanding_provider as understanding
    from webapp.services import openai_semantic_proposer_client as semantic
    for module in (intelligence, understanding, semantic):
        assert 0 < module.MAX_OUTPUT_TOKENS <= 8_192
    assert understanding.MAX_SOURCE_CHARACTERS == 100_000
    assert 0 < intelligence.MAX_INPUT_CHARACTERS <= 200_000
    assert 0 < semantic.MAX_INPUT_CHARACTERS <= 200_000


def test_the_intelligence_provider_refuses_oversize_input_before_the_call(monkeypatch):
    from product import openai_application_intelligence_provider as module
    from product.application_intelligence_providers import ApplicationIntelligenceProviderError
    monkeypatch.setattr(module, "_hosted_input", lambda request: "x" * (module.MAX_INPUT_CHARACTERS + 1))
    provider = module.OpenAIApplicationIntelligenceProvider(environ={"OPENAI_API_KEY": "k"},
                                                            client_factory=lambda key: _NeverCalledClient())
    with pytest.raises(ApplicationIntelligenceProviderError, match="exceeds"):
        provider.propose({"request_id": "r"})


def test_the_semantic_client_refuses_oversize_input_before_the_call():
    from webapp.services import openai_semantic_proposer_client as module
    from webapp.services.semantic_proposer_errors import SemanticProposerProviderError
    client = module.OpenAISemanticProposerClient(environ={"OPENAI_API_KEY": "k"},
                                                 client_factory=lambda key: _NeverCalledClient())
    with pytest.raises(SemanticProposerProviderError, match="exceeds"):
        client.complete({"blob": "x" * (module.MAX_INPUT_CHARACTERS + 1)})


# ---- failed provider calls still count the spend they incurred ----

class _FailingAfterResponse(FakeProvider):
    """The provider got a response (tokens billed) and then failed validating it."""

    def extract(self, request):
        self.calls += 1
        self.last_usage = {"input_tokens": 2000, "output_tokens": 1000}
        raise RuntimeError("response failed validation")


class _FailingWithoutUsage(FakeProvider):
    def extract(self, request):
        self.calls += 1
        raise TimeoutError("no response")


def test_a_call_that_fails_after_a_response_records_its_real_usage(world):
    conn = world[1]
    with pytest.raises(RuntimeError):
        _metered(world, _FailingAfterResponse()).extract({})
    assert _events(conn) == [("gpt-5.4-mini", 2000, 1000, 6000)]  # 2000 × 1 + 1000 × 4


def test_a_call_that_fails_without_usage_is_charged_the_missing_usage_rate(world):
    conn = world[1]
    with pytest.raises(TimeoutError):
        _metered(world, _FailingWithoutUsage()).extract({})
    assert _events(conn) == [("gpt-5.4-mini", 0, 0, 7000)]


def test_the_understanding_provider_exposes_usage_when_validation_fails(monkeypatch):
    from product import openai_job_understanding_provider as module

    class Usage:
        input_tokens, output_tokens, total_tokens = 30, 40, 70

    class Client:
        class responses:
            @staticmethod
            def create(**kwargs):
                return SimpleResponse()

    class SimpleResponse:
        id = "resp_3"
        usage = Usage()

    def invalid(response):
        raise module.JobUnderstandingProviderError("bad model")

    monkeypatch.setattr(module, "_hosted_inputs", lambda request: ("text", "instructions", []))
    monkeypatch.setattr(module, "_validate_response_model", invalid)
    provider = module.OpenAIJobUnderstandingProvider(environ={"OPENAI_API_KEY": "k"},
                                                     client_factory=lambda key: Client())
    with pytest.raises(module.JobUnderstandingProviderError):
        provider.extract({})
    assert provider.last_usage == {"input_tokens": 30, "output_tokens": 40}
