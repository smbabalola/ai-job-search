"""The metered AI provider boundary (Bundle 7 spec §13.2, U3).

``MeteredProvider`` wraps any AI provider (``extract`` / ``propose`` /
``complete``) with the same protocol. Before each call it checks the platform
AI control through the plan feature and the account's hidden AI cost ceiling
(Σ ``ai_cost_events`` in the entitlement window ≥ ceiling → refused with
``FairUseLimitReached``); the call that crosses the ceiling is allowed to
finish — a bounded one-call overshoot: every OpenAI provider caps its input
(understanding 100k characters, intelligence and semantic proposer 200k) and
its output (``max_output_tokens`` ≤ 8192) with at most two attempts, so the
overshoot is at most one capped call, and the next call is refused with
FAIR_USE_LIMIT_REACHED. After each call it records an ``ai_cost_events`` row priced from the
operator table (``ai-pricing.v1.json``; an unknown model is priced at the
required ``default`` rate, a response without usage at
``missing_usage_call_micro_usd``).

Recording: inside ``Metering.metered`` the events are buffered (a context
variable) and written in Metering's settle transaction, on success and on
failure alike, so a failed stage's rollback never loses spend and the
request connection's open write transaction (SQLite) never blocks it.
Outside a metered action they are written at once on a fresh connection.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from webapp.persistence import dbapi
from webapp.persistence import usage as rows

__all__ = [
    "CostEvent", "FairUseLimitReached", "MeteredProvider", "Pricing", "PricingInvalid", "cost_buffer",
    "load_pricing", "metered", "parse_pricing", "record_cost_events",
]

logger = logging.getLogger("webapp.usage")

CEILING_ALLOWANCE = "ai.cost_micro_usd"
CALL_METHODS = ("extract", "propose", "complete")
_RATE_KEYS = ("input_per_mtok_micro_usd", "output_per_mtok_micro_usd")


class FairUseLimitReached(Exception):
    """The account's hidden AI cost ceiling is reached (never shown as a number)."""

    def __init__(self, reason: str = "ceiling"):
        super().__init__("fair-use limit reached")
        self.reason = reason


class PricingInvalid(ValueError):
    pass


def metering_refusal(exc: BaseException | None) -> BaseException | None:
    """The FairUseLimitReached / FeatureNotInPlan inside ``exc``'s cause chain,
    if any. Provider layers wrap every provider exception into their own
    error; a metering refusal must still reach the caller as its §21.3 code."""
    from product.entitlements import FeatureNotInPlan

    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, (FairUseLimitReached, FeatureNotInPlan)):
            return exc
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


def reraise_metering_refusal(exc: BaseException) -> None:
    refusal = metering_refusal(exc)
    if refusal is not None:
        raise refusal


@dataclass(frozen=True)
class Rate:
    input_per_mtok_micro_usd: int
    output_per_mtok_micro_usd: int


@dataclass(frozen=True)
class Pricing:
    version: str
    models: Mapping[str, Rate | None]
    missing_usage_call_micro_usd: int | None

    @property
    def configured(self) -> bool:
        """DP-3: the operator file ships with nulls; null default or null
        missing-usage rate → every metered call is refused (fail closed)."""
        return self.models.get("default") is not None and self.missing_usage_call_micro_usd is not None

    def cost(self, model: str, input_tokens: int | None, output_tokens: int | None) -> int:
        if input_tokens is None or output_tokens is None:
            return int(self.missing_usage_call_micro_usd or 0)
        rate = self.models.get(model) or self.models["default"]
        assert rate is not None
        return math.ceil((input_tokens * rate.input_per_mtok_micro_usd
                          + output_tokens * rate.output_per_mtok_micro_usd) / 1_000_000)


def _micro(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PricingInvalid(f"{where} must be a non-negative integer or null")
    return value


def parse_pricing(doc: Mapping[str, Any]) -> Pricing:
    if not isinstance(doc, Mapping) or not isinstance(doc.get("pricing_version"), str):
        raise PricingInvalid("pricing_version is required")
    if doc.get("currency") != "USD":
        raise PricingInvalid("currency must be USD (costs are micro-USD)")
    models = doc.get("models")
    if not isinstance(models, Mapping) or "default" not in models:
        raise PricingInvalid("models must include a 'default' rate")
    parsed: dict[str, Rate | None] = {}
    for model, rate in models.items():
        if rate is None:
            parsed[model] = None
            continue
        if not isinstance(rate, Mapping) or set(rate) != set(_RATE_KEYS):
            raise PricingInvalid(f"models.{model} must have exactly {', '.join(_RATE_KEYS)}")
        values = [_micro(rate[key], f"models.{model}.{key}") for key in _RATE_KEYS]
        parsed[model] = None if None in values else Rate(*values)  # type: ignore[arg-type]
    if "missing_usage_call_micro_usd" not in doc:
        raise PricingInvalid("missing_usage_call_micro_usd is required")
    return Pricing(version=doc["pricing_version"], models=parsed,
                   missing_usage_call_micro_usd=_micro(doc["missing_usage_call_micro_usd"],
                                                       "missing_usage_call_micro_usd"))


def load_pricing(path: Path) -> Pricing:
    path = Path(path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return parse_pricing(json.loads(path.read_text(encoding="utf-8")))


@dataclass
class CostEvent:
    account_id: str
    subject_type: str
    subject_id: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_micro_usd: int
    request_ref: str | None
    created_at: datetime


@dataclass
class _Buffer:
    events: list[CostEvent] = field(default_factory=list)


_BUFFER: contextvars.ContextVar[_Buffer | None] = contextvars.ContextVar("ai_cost_buffer", default=None)


@contextlib.contextmanager
def cost_buffer() -> Iterator[_Buffer]:
    """Buffer the cost events of one metered action (``Metering.metered``)."""
    buffer = _Buffer()
    token = _BUFFER.set(buffer)
    try:
        yield buffer
    finally:
        _BUFFER.reset(token)


def record_cost_events(conn: dbapi.Connection, events: list[CostEvent]) -> None:
    """Inside the caller's transaction; append-only."""
    for event in events:
        conn.execute(
            "INSERT INTO ai_cost_events (id, account_id, subject_type, subject_id, provider, model, input_tokens, "
            "output_tokens, cost_micro_usd, request_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"aic_{uuid.uuid4().hex[:20]}", event.account_id, event.subject_type, event.subject_id, event.provider,
             event.model, event.input_tokens, event.output_tokens, event.cost_micro_usd, event.request_ref,
             rows.ts(event.created_at)))


def window_cost(conn: dbapi.Connection, account_id: str, start: datetime, end: datetime) -> int:
    row = conn.execute("SELECT COALESCE(SUM(cost_micro_usd), 0) FROM ai_cost_events WHERE account_id = ? "
                       "AND created_at >= ? AND created_at < ?", (account_id, rows.ts(start), rows.ts(end))).fetchone()
    return int(row[0])


def _token(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(result: Any, inner: Any) -> tuple[int | None, int | None, str | None]:
    """(input, output, response id) from the response's audit, else the
    provider's ``last_audit`` (an object or the semantic client's dict)."""
    audit = getattr(result, "audit", None)
    if audit is None:
        audit = getattr(inner, "last_audit", None)
    if audit is None or (isinstance(audit, Mapping) and "input_tokens" not in audit):
        audit = getattr(inner, "last_usage", None) or audit  # set as soon as a response arrived
    if isinstance(audit, Mapping):
        return (_token(audit.get("input_tokens")), _token(audit.get("output_tokens")),
                audit.get("provider_response_id"))
    return (_token(getattr(audit, "input_tokens", None)), _token(getattr(audit, "output_tokens", None)),
            getattr(audit, "provider_response_id", None))


class MeteredProvider:
    def __init__(self, inner: Any, *, conn_factory: Callable[[], dbapi.Connection], scope: Any, subject_type: str,
                 subject_id: str, pricing: Pricing, gate: Any, clock: Callable[[], datetime] | None = None,
                 feature: str = "ai.prepare") -> None:
        self._inner = inner
        self._conn_factory = conn_factory
        self._scope = scope
        self._subject = (subject_type, subject_id)
        self._pricing = pricing
        self._gate = gate
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._feature = feature

    def __getattr__(self, name: str) -> Any:
        if name in CALL_METHODS:
            method = getattr(self._inner, name)
            return lambda *args, **kwargs: self._call(method, *args, **kwargs)
        return getattr(self._inner, name)

    def _check(self) -> None:
        now = self._clock()
        conn = self._conn_factory()
        try:
            resolved = self._gate.require_feature(conn, self._scope, self._feature, now=now)  # AI_ENABLED, plan
            if not self._pricing.configured:
                logger.error("ai_pricing_unconfigured account=%s", self._scope.account_id)
                raise FairUseLimitReached("pricing_unconfigured")
            ceiling = resolved.allowances.get(CEILING_ALLOWANCE) or 0
            spent = window_cost(conn, self._scope.account_id, resolved.window.start, resolved.window.end)
        finally:
            conn.close()
        buffer = _BUFFER.get()
        if buffer is not None:
            spent += sum(e.cost_micro_usd for e in buffer.events if e.account_id == self._scope.account_id)
        if spent >= ceiling:
            logger.warning("ai_fair_use_limit account=%s", self._scope.account_id)
            raise FairUseLimitReached()

    def _call(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self._check()
        try:
            result = method(*args, **kwargs)
        except BaseException:
            # A failed call still spent: its real usage when a response arrived,
            # else the conservative missing-usage rate (the call may have been billed).
            self._record(None)
            raise
        self._record(result)
        return result

    def _record(self, result: Any) -> None:
        model = str(getattr(self._inner, "model_id", None) or "unknown")
        input_tokens, output_tokens, response_id = _usage(result, self._inner)
        event = CostEvent(
            account_id=self._scope.account_id, subject_type=self._subject[0], subject_id=self._subject[1],
            provider=str(getattr(self._inner, "provider_id", None) or "unknown"), model=model,
            input_tokens=input_tokens or 0, output_tokens=output_tokens or 0,
            cost_micro_usd=self._pricing.cost(model, input_tokens, output_tokens),
            request_ref=response_id if isinstance(response_id, str) else None, created_at=self._clock())
        buffer = _BUFFER.get()
        if buffer is not None:
            buffer.events.append(event)
        else:
            conn = self._conn_factory()
            try:
                record_cost_events(conn, [event])
                conn.commit()
            finally:
                conn.close()


def metered(provider: Any, scope: Any, subject_type: str, subject_id: str, *, state: Any,
            feature: str = "ai.prepare") -> Any:
    """The construction-site helper: wrap ``provider`` when metering is
    enforced (real accounts); the local operator account is unmetered."""
    metering = getattr(state, "metering", None)
    if metering is None or not metering.enforced:
        return provider
    from webapp.persistence.db import connect
    settings = state.settings
    return MeteredProvider(provider, conn_factory=lambda: connect(settings), scope=scope, subject_type=subject_type,
                           subject_id=subject_id, pricing=state.ai_pricing, gate=metering.gate,
                           clock=metering.clock, feature=feature)
