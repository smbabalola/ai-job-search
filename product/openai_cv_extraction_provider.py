"""OpenAI CV-extraction adapter (Bundle 7 spec §15.3), mirroring the existing
OpenAI providers: a pinned model, no SDK retries with explicit timeouts, a
bounded input (MAX_TEXT_CHARACTERS) and output (MAX_OUTPUT_TOKENS), strict
JSON schema output, and token usage exposed for AI cost metering. Strict mode
cannot express a free-form object, so ``fields`` travel as name/value pairs
on the wire and are turned back into an object here."""
from __future__ import annotations

import copy
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from product.cv_extraction import INSTRUCTIONS, MAX_TEXT_CHARACTERS, TARGETS
from product.cv_extraction_providers import CvExtractionProviderError
from product.job_understanding_providers import ProviderCallAudit, ProviderResponse

OPENAI_MODEL = "gpt-5.4-mini-2026-03-17"
OPENAI_MODEL_ID = "gpt-5.4-mini"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
MAX_OUTPUT_TOKENS = 8_192
MAX_ATTEMPTS = 2
CONNECT_TIMEOUT_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 90.0
SCHEMA_NAME = "cv_extraction_proposals_v1"

WIRE_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["proposals"],
    "properties": {"proposals": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["target", "kind", "fields", "source_excerpt", "confidence"],
        "properties": {
            "target": {"type": "string", "enum": list(TARGETS)},
            "kind": {"type": "string"},
            "fields": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                  "required": ["name", "value"],
                                                  "properties": {"name": {"type": "string"},
                                                                 "value": {"type": "string"}}}},
            "source_excerpt": {"type": "string"},
            "confidence": {"type": "number"},
        }}}},
}


def _default_client_factory(api_key: str) -> Any:
    try:
        import openai
    except ImportError:
        raise CvExtractionProviderError("openai provider dependency is unavailable") from None
    return openai.OpenAI(api_key=api_key, max_retries=0,
                         timeout=openai.Timeout(REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS))


def _count(response: Any, name: str) -> int | None:
    value = getattr(getattr(response, "usage", None), name, None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _decode(response: Any) -> dict[str, Any]:
    if getattr(response, "status", "completed") != "completed":
        raise CvExtractionProviderError("openai response did not complete")
    try:
        raw = json.loads(getattr(response, "output_text", "") or "")
    except ValueError:
        raise CvExtractionProviderError("openai response is not JSON") from None
    proposals = []
    for item in raw.get("proposals", []) if isinstance(raw, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("fields"), list):
            item = {**item, "fields": {f.get("name"): f.get("value") for f in item["fields"]
                                       if isinstance(f, dict) and isinstance(f.get("name"), str)}}
        proposals.append(item)
    return {"proposals": proposals}


class OpenAICvExtractionProvider:
    provider_id = "openai"
    model_id = OPENAI_MODEL_ID
    model_version = OPENAI_MODEL

    def __init__(self, *, environ: Mapping[str, str] | None = None, client_factory: Callable[[str], Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._environ = os.environ if environ is None else environ
        self._client_factory = client_factory or _default_client_factory
        self._sleep = sleep
        self.last_audit: ProviderCallAudit | None = None
        self.last_usage: dict[str, int | None] | None = None

    def extract(self, request: dict[str, Any]) -> ProviderResponse:
        self.last_audit = None
        self.last_usage = None
        api_key = (self._environ.get(OPENAI_API_KEY_ENV) or "").strip()
        if not api_key:
            raise CvExtractionProviderError(f"openai provider is not configured: {OPENAI_API_KEY_ENV} is missing")
        text = request.get("cv_text") or ""
        if len(text) > MAX_TEXT_CHARACTERS:
            raise CvExtractionProviderError(f"cv text exceeds {MAX_TEXT_CHARACTERS} characters")
        call = {
            "model": OPENAI_MODEL, "instructions": INSTRUCTIONS,
            "input": json.dumps({"cv_text": text, "entry_kinds": request.get("entry_kinds", {}),
                                 "answer_subjects": request.get("answer_subjects", [])},
                                ensure_ascii=False, separators=(",", ":")),
            "reasoning": {"effort": "low"},
            "text": {"format": {"type": "json_schema", "name": SCHEMA_NAME, "strict": True,
                                "schema": copy.deepcopy(WIRE_SCHEMA)}},
            "max_output_tokens": MAX_OUTPUT_TOKENS, "store": False, "stream": False, "background": False,
            "tools": [], "truncation": "disabled",
        }
        client = self._client_factory(api_key)
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        response, attempts = None, 0
        for attempts in range(1, MAX_ATTEMPTS + 1):
            try:
                response = client.responses.create(**copy.deepcopy(call))
                break
            except Exception as exc:  # noqa: BLE001 - classified by the retry budget
                if attempts >= MAX_ATTEMPTS:
                    raise CvExtractionProviderError(f"openai cv extraction failed: {type(exc).__name__}") from None
                self._sleep(1.0)
        self.last_usage = {"input_tokens": _count(response, "input_tokens"),
                           "output_tokens": _count(response, "output_tokens")}
        payload = _decode(response)
        response_id = getattr(response, "id", None)
        self.last_audit = ProviderCallAudit(
            provider_id=self.provider_id, model_id=self.model_id, model_version=self.model_version,
            provider_response_id=response_id if isinstance(response_id, str) else None, started_at=started_at,
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)), attempt_count=attempts,
            input_tokens=self.last_usage["input_tokens"], output_tokens=self.last_usage["output_tokens"],
            total_tokens=_count(response, "total_tokens"), local_request_id=request.get("request_id"))
        return ProviderResponse(payload=payload, response_id=self.last_audit.provider_response_id,
                                audit=self.last_audit)
