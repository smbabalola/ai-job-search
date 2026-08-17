from __future__ import annotations

import copy
import inspect
import json
import sys
import unittest
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from product.job_understanding import (
    DEFAULT_POLICY,
    JobUnderstandingValidationError,
    SCHEMA,
    _provider_request,
    build_job_understanding_request,
    extract_job_understanding,
    validate_provider_candidate,
)
from product.job_understanding_providers import JobUnderstandingProviderError
from product.openai_job_understanding_provider import (
    MAX_OUTPUT_TOKENS,
    MAX_RETRY_AFTER_SECONDS,
    MAX_SOURCE_CHARACTERS,
    OPENAI_MODEL,
    OpenAIJobUnderstandingProvider,
    normalize_openai_candidate,
    openai_call_parameters,
    openai_candidate_schema,
)


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = ROOT / "tests" / "fixtures" / "job_understanding" / "job-snapshot.json"


def snapshot() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def candidate(quote: str = "Python is required.") -> dict:
    return {
        "schema_version": "job-understanding-candidate.v0",
        "items": [
            {
                "proposal_id": "proposal-hosted-1",
                "category": "requirements",
                "kind": "required",
                "quote": quote,
                "certainty": "explicit",
            }
        ],
        "suggestions": [],
        "ambiguous_statements": [],
        "warnings": [],
    }


def internal_request(job: dict | None = None) -> dict:
    request = build_job_understanding_request(
        job or snapshot(), "hosted-test-request", requested_categories=["requirements"]
    )
    return _provider_request(request, DEFAULT_POLICY)


def response(payload: dict | str | None = None, **overrides):
    if isinstance(payload, dict):
        output_text = json.dumps(payload, ensure_ascii=False)
    else:
        output_text = payload
    values = {
        "id": "resp_test_123",
        "status": "completed",
        "output_text": output_text,
        "output": [],
        "usage": SimpleNamespace(input_tokens=101, output_tokens=42, total_tokens=143),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeResponses:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.responses = FakeResponses(outcomes)


def provider(outcomes, **kwargs):
    client = FakeClient(outcomes)
    times = iter([10.0, 10.125])
    instance = OpenAIJobUnderstandingProvider(
        environ={"OPENAI_API_KEY": "test-secret-key"},
        client_factory=lambda _: client,
        clock=lambda: next(times),
        utc_now=lambda: datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc),
        sleep=kwargs.get("sleep", lambda _: None),
    )
    return instance, client


class OpenAISchemaTranslationTests(unittest.TestCase):
    def test_root_and_every_object_require_all_properties(self):
        schema = openai_candidate_schema()

        def visit(value):
            if isinstance(value, dict):
                if isinstance(value.get("properties"), dict):
                    self.assertEqual(list(value["properties"]), value["required"])
                    self.assertIs(value.get("additionalProperties"), False)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(schema)

    def test_only_product_optional_fields_become_nullable(self):
        schema = openai_candidate_schema()
        item = schema["$defs"]["providerItem"]
        review = schema["$defs"]["reviewProposal"]
        self.assertEqual(item["properties"]["occurrence"]["anyOf"][-1], {"type": "null"})
        for field in ("category", "kind", "occurrence"):
            self.assertEqual(review["properties"][field]["anyOf"][-1], {"type": "null"})
        self.assertNotIn("anyOf", item["properties"]["quote"])

    def test_schema_is_derived_from_only_reachable_product_definitions(self):
        schema = openai_candidate_schema()
        self.assertEqual(
            set(schema["$defs"]),
            {"candidateVersion", "category", "certainty", "id", "providerItem", "requirementKind", "reviewProposal"},
        )
        self.assertEqual(
            schema["$defs"]["category"],
            SCHEMA["$defs"]["category"],
        )

    def test_null_normalization_removes_only_known_optional_fields(self):
        wire = candidate()
        wire["items"][0]["occurrence"] = None
        wire["suggestions"] = [
            {
                "proposal_id": "review-1",
                "text": "text",
                "reason": "reason",
                "quote": "quote",
                "category": None,
                "kind": None,
                "occurrence": None,
            }
        ]
        normalized = normalize_openai_candidate(wire)
        self.assertNotIn("occurrence", normalized["items"][0])
        for field in ("category", "kind", "occurrence"):
            self.assertNotIn(field, normalized["suggestions"][0])

    def test_normalized_candidate_still_uses_product_validator(self):
        request = build_job_understanding_request(
            snapshot(), "schema-parity", requested_categories=["requirements"]
        )
        validate_provider_candidate(request, normalize_openai_candidate(candidate()))


class OpenAIProviderTests(unittest.TestCase):
    def test_pinned_sdk_accepts_the_exact_responses_api_parameters(self):
        import openai

        self.assertEqual(version("openai"), "3.1.0")
        client = openai.OpenAI(api_key="offline-signature-check", max_retries=0)
        try:
            call = openai_call_parameters(
                instructions="instructions",
                source_text="Synthetic requirement.",
                requested_categories=["requirements"],
                response_schema=openai_candidate_schema(),
            )
            inspect.signature(client.responses.create).bind(**call)
        finally:
            client.close()

    def test_successful_response_and_locally_owned_audit(self):
        instance, client = provider([response(candidate())])
        result = instance.extract(internal_request())
        self.assertEqual(result.payload, candidate())
        self.assertEqual(result.response_id, "resp_test_123")
        self.assertEqual(result.audit.provider_id, "openai")
        self.assertEqual(result.audit.model_version, OPENAI_MODEL)
        self.assertEqual(result.audit.elapsed_ms, 125)
        self.assertEqual(result.audit.attempt_count, 1)
        self.assertEqual(result.audit.total_tokens, 143)
        self.assertEqual(result.audit.local_request_id, "hosted-test-request")
        self.assertEqual(
            result.audit.source_content_id,
            internal_request()["source"]["content_id"],
        )
        self.assertEqual(len(client.responses.calls), 1)

    def test_actual_serialized_model_data_is_minimized(self):
        request = internal_request()
        forbidden_values = [
            request["request_id"],
            request["source"]["content_id"],
            request["source"]["field"],
            str(request["source"]["character_length"]),
            request["policy"]["id"],
            request["policy"]["prompt_id"],
        ]
        instance, client = provider([response(candidate())])
        instance.extract(request)
        call = client.responses.calls[0]
        model_input = json.loads(call["input"])
        self.assertEqual(
            set(model_input), {"selected_job_text", "requested_categories"}
        )
        self.assertEqual(model_input["selected_job_text"], request["source"]["text"])
        self.assertEqual(model_input["requested_categories"], ["requirements"])
        serialized = json.dumps(call, ensure_ascii=False)
        for value in forbidden_values:
            self.assertNotIn(value, serialized)

    def test_pinned_bounded_stateless_configuration(self):
        instance, client = provider([response(candidate())])
        instance.extract(internal_request())
        call = client.responses.calls[0]
        self.assertEqual(call["model"], OPENAI_MODEL)
        self.assertEqual(call["reasoning"], {"effort": "low"})
        self.assertEqual(call["max_output_tokens"], MAX_OUTPUT_TOKENS)
        self.assertIs(call["store"], False)
        self.assertIs(call["stream"], False)
        self.assertIs(call["background"], False)
        self.assertEqual(call["tools"], [])
        self.assertEqual(call["truncation"], "disabled")
        self.assertNotIn("temperature", call)
        self.assertNotIn("previous_response_id", call)
        self.assertNotIn("metadata", call)
        self.assertTrue(call["text"]["format"]["strict"])

    def test_unexpected_returned_model_is_rejected(self):
        instance, client = provider(
            [response(candidate(), model="gpt-unexpected")]
        )
        with self.assertRaises(JobUnderstandingProviderError) as error:
            instance.extract(internal_request())
        self.assertIn("unexpected model", str(error.exception))
        self.assertEqual(len(client.responses.calls), 1)

    def test_api_key_is_read_from_environment_but_not_serialized_or_represented(self):
        captured = []
        client = FakeClient([response(candidate())])
        instance = OpenAIJobUnderstandingProvider(
            environ={"OPENAI_API_KEY": "highly-secret"},
            client_factory=lambda key: captured.append(key) or client,
        )
        instance.extract(internal_request())
        self.assertEqual(captured, ["highly-secret"])
        self.assertNotIn("highly-secret", repr(instance))
        self.assertNotIn("highly-secret", json.dumps(client.responses.calls[0]))

    def test_default_sdk_client_disables_retries_and_sets_connect_and_request_timeouts(self):
        client = FakeClient([response(candidate())])
        constructed = []
        fake_sdk = SimpleNamespace(
            Timeout=lambda total, **kwargs: (total, kwargs),
            OpenAI=lambda **kwargs: constructed.append(kwargs) or client,
        )
        instance = OpenAIJobUnderstandingProvider(
            environ={"OPENAI_API_KEY": "sdk-test-key"}
        )
        with patch.dict(sys.modules, {"openai": fake_sdk}):
            instance.extract(internal_request())
        self.assertEqual(constructed[0]["max_retries"], 0)
        self.assertEqual(constructed[0]["timeout"], (60.0, {"connect": 5.0}))
        self.assertEqual(constructed[0]["api_key"], "sdk-test-key")

    def test_missing_and_blank_credentials_fail_before_client_creation(self):
        for environ in ({}, {"OPENAI_API_KEY": "  "}):
            called = []
            instance = OpenAIJobUnderstandingProvider(
                environ=environ, client_factory=lambda key: called.append(key)
            )
            with self.assertRaises(JobUnderstandingProviderError) as error:
                instance.extract(internal_request())
            self.assertIn("OPENAI_API_KEY", str(error.exception))
            self.assertEqual(called, [])

    def test_credential_is_not_leaked_by_client_initialization_failure(self):
        def broken(key):
            raise RuntimeError(f"bad key {key}")

        instance = OpenAIJobUnderstandingProvider(
            environ={"OPENAI_API_KEY": "never-leak-me"}, client_factory=broken
        )
        with self.assertRaises(JobUnderstandingProviderError) as error:
            instance.extract(internal_request())
        self.assertNotIn("never-leak-me", str(error.exception))

    def test_oversized_source_fails_before_client_invocation_without_truncation(self):
        job = snapshot()
        job["raw_text"] = "x" * (MAX_SOURCE_CHARACTERS + 1)
        called = []
        instance = OpenAIJobUnderstandingProvider(
            environ={"OPENAI_API_KEY": "test"},
            client_factory=lambda key: called.append(key),
        )
        with self.assertRaises(JobUnderstandingProviderError) as error:
            instance.extract(internal_request(job))
        self.assertIn(str(MAX_SOURCE_CHARACTERS), str(error.exception))
        self.assertEqual(called, [])

    def test_exact_source_at_limit_is_not_truncated(self):
        job = snapshot()
        job["raw_text"] = "å" * MAX_SOURCE_CHARACTERS
        empty = candidate(quote="å")
        empty["items"][0]["occurrence"] = 0
        instance, client = provider([response(empty)])
        instance.extract(internal_request(job))
        self.assertEqual(
            json.loads(client.responses.calls[0]["input"])["selected_job_text"],
            job["raw_text"],
        )

    def test_transport_timeout_network_429_and_5xx_retry_once(self):
        exception_types = [
            type("APITimeoutError", (Exception,), {}),
            type("APIConnectionError", (Exception,), {}),
        ]
        failures = [cls("secret transport detail") for cls in exception_types]
        failures.extend([StatusError(429), StatusError(500), StatusError(503)])
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                sleeps = []
                instance, client = provider(
                    [failure, response(candidate())], sleep=sleeps.append
                )
                result = instance.extract(internal_request())
                self.assertEqual(len(client.responses.calls), 2)
                self.assertEqual(result.audit.attempt_count, 2)
                self.assertEqual(len(sleeps), 1)

    def test_retry_after_is_bounded(self):
        sleeps = []
        instance, _ = provider(
            [StatusError(429, retry_after="999"), response(candidate())],
            sleep=sleeps.append,
        )
        instance.extract(internal_request())
        self.assertEqual(sleeps, [MAX_RETRY_AFTER_SECONDS])

    def test_retry_limit_is_enforced_and_exception_detail_is_redacted(self):
        instance, client = provider([StatusError(500), StatusError(500)])
        with self.assertRaises(JobUnderstandingProviderError) as error:
            instance.extract(internal_request())
        self.assertEqual(len(client.responses.calls), 2)
        self.assertEqual(str(error.exception), "openai provider failed: http_500")

    def test_normal_4xx_and_authentication_are_not_retried(self):
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                instance, client = provider([StatusError(status)])
                with self.assertRaises(JobUnderstandingProviderError):
                    instance.extract(internal_request())
                self.assertEqual(len(client.responses.calls), 1)

    def test_refusal_incomplete_empty_and_malformed_outputs_are_not_retried(self):
        refusal = response(
            None,
            output=[SimpleNamespace(content=[SimpleNamespace(type="refusal", refusal="private")])],
        )
        cases = [
            refusal,
            response(None, status="incomplete"),
            response(""),
            response("not-json"),
        ]
        for outcome in cases:
            with self.subTest(outcome=outcome):
                instance, client = provider([outcome])
                with self.assertRaises(JobUnderstandingProviderError):
                    instance.extract(internal_request())
                self.assertEqual(len(client.responses.calls), 1)

    def test_model_cannot_spoof_execution_metadata(self):
        spoofed = candidate()
        spoofed["execution"] = {"provider_id": "attacker"}
        instance, client = provider([response(spoofed)])
        with self.assertRaises(JobUnderstandingValidationError):
            extract_job_understanding(
                snapshot(), instance, "spoof-test", requested_categories=["requirements"]
            )
        self.assertEqual(len(client.responses.calls), 1)

    def test_schema_invalid_candidate_is_not_retried(self):
        instance, client = provider([response({"schema_version": "wrong"})])
        with self.assertRaises(JobUnderstandingValidationError):
            extract_job_understanding(
                snapshot(), instance, "invalid-schema", requested_categories=["requirements"]
            )
        self.assertEqual(len(client.responses.calls), 1)

    def test_ungrounded_quote_is_rejected_locally_without_model_retry(self):
        instance, client = provider([response(candidate("fabricated requirement"))])
        with self.assertRaises(JobUnderstandingValidationError):
            extract_job_understanding(
                snapshot(), instance, "invalid-grounding", requested_categories=["requirements"]
            )
        self.assertEqual(len(client.responses.calls), 1)

    def test_successful_provider_integrates_with_normal_ticket_6_result(self):
        instance, _ = provider([response(candidate())])
        result = extract_job_understanding(
            snapshot(), instance, "hosted-integration", requested_categories=["requirements"]
        )
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["requirements"][0]["text"], candidate()["items"][0]["quote"])
        self.assertEqual(result["execution"]["provider_id"], "openai")
        self.assertNotIn("audit", result)


class StatusError(Exception):
    def __init__(self, status_code: int, *, retry_after: str | None = None):
        super().__init__(f"sensitive HTTP {status_code} body")
        self.status_code = status_code
        self.response = SimpleNamespace(headers={"retry-after": retry_after})


if __name__ == "__main__":
    unittest.main()
