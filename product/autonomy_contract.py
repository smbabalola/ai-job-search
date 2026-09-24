"""Bundle 6B autonomy contract: shared vocabulary, context/decision types and
canonical hashing (spec §3, §9, §15.1).

Pure domain module: no I/O, no clock, no webapp imports. Every hash and
fingerprint in the autonomy contract goes through canonical_hash so that
semantically identical inputs hash identically regardless of key order,
whitespace or Unicode composition.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Any, Mapping

ENGINE_VERSION = "autonomy-gate.v1"
CONTEXT_SCHEMA = "autonomy-context"
CONTEXT_SCHEMA_VERSION = "v1"

FILL_SESSION_TTL = timedelta(minutes=30)
SUBMIT_GRANT_TTL = timedelta(seconds=120)
CLICK_DISPATCH_TTL = timedelta(seconds=60)


class Capability(IntEnum):
    NONE = 0
    PREPARE = 1
    FILL = 2
    SUBMIT = 3


class Mode(str, Enum):
    LIVE = "LIVE"
    SHADOW = "SHADOW"
    DRY_RUN = "DRY_RUN"


class ResultKind(str, Enum):
    ALLOW = "ALLOW"
    REQUIRE_USER = "REQUIRE_USER"
    BLOCK = "BLOCK"
    DENY = "DENY"
    DENY_TEMPORARY = "DENY_TEMPORARY"


class ProvenanceTier(str, Enum):
    DISCOVERY_VERIFIED = "discovery_verified"
    USER_CONFIRMED_APPLY_TARGET = "user_confirmed_apply_target"
    USER_SUPPLIED = "user_supplied"
    IMPORTED_SOURCE = "imported_source"


class IdentityStrength(str, Enum):
    SOURCE_RECORD = "SOURCE_RECORD"
    CANONICAL_URL = "CANONICAL_URL"
    WEAK = "WEAK"


class EmployerKeyStrength(str, Enum):
    ATS_TENANT = "ATS_TENANT"
    NORMALIZED_NAME = "NORMALIZED_NAME"
    UNKNOWN = "UNKNOWN"


class Reach(str, Enum):
    EMPLOYER = "EMPLOYER"
    SEARCH_WORKSPACE = "SEARCH_WORKSPACE"
    ACCOUNT = "ACCOUNT"


REACH_ORDER = {Reach.EMPLOYER: 0, Reach.SEARCH_WORKSPACE: 1, Reach.ACCOUNT: 2}


class _Unknown:
    _instance: "_Unknown | None" = None

    def __new__(cls) -> "_Unknown":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = _Unknown()


def is_unknown(value: Any) -> bool:
    return value is UNKNOWN


@dataclass(frozen=True, order=True)
class Reason:
    code: str
    params: tuple[tuple[str, str], ...] = ()


def reason(code: str, **params: Any) -> Reason:
    return Reason(code, tuple(sorted((k, str(v)) for k, v in params.items())))


@dataclass(frozen=True, order=True)
class RequireUserItem:
    kind: str
    ref: str


@dataclass(frozen=True)
class AnswerCandidate:
    approved_answer_id: str
    subject: str
    reach: Reach
    scope_id: str | None
    context: Mapping[str, str]
    confirmed_at: datetime
    basis_kind: str  # "EVIDENCE" | "USER_ASSERTION"
    basis_hash_at_approval: str | None
    basis_hash_current: str | None  # None: basis evidence no longer present
    contradicted: bool


@dataclass(frozen=True)
class RepresentationRequirement:
    key: str
    subject: str | None  # None: unclassified (unless evidence_available)
    required: bool
    evidence_available: bool
    job_context: Mapping[str, Any] = field(default_factory=dict)
    candidates: tuple[AnswerCandidate, ...] = ()


@dataclass(frozen=True)
class ApplyTargetFacts:
    provenance: ProvenanceTier | None  # None: no apply target
    adapter_id: str | None = None
    adapter_submit_capable: bool = False
    landing_within_redirect_set: Any = UNKNOWN  # bool | UNKNOWN
    tenant_matches_employer: Any = UNKNOWN  # bool | UNKNOWN
    ats_job_id_matches: Any = None  # bool | UNKNOWN | None (no ATS job id available)
    unexplained_redirect: Any = UNKNOWN  # bool | UNKNOWN


@dataclass(frozen=True)
class CounterState:
    name: str
    stage: Capability
    used: int
    limit: int
    retry_at: datetime | None


@dataclass(frozen=True)
class BudgetState:
    category: str
    window: str
    used: Decimal
    reserved: Decimal
    cap: Decimal
    estimate: Decimal
    retry_at: datetime | None


@dataclass(frozen=True)
class RuleAcknowledgement:
    rule_id: str
    rule_hash: str
    observed_fingerprint: str
    disposition: str  # "PROCEED" | "DO_NOT_PROCEED"


@dataclass(frozen=True)
class AuthorizationContext:
    mode: Mode
    requested_stage: Capability
    now: datetime
    account_id: str
    application_workspace_id: str
    search_workspace_id: str | None
    deployment_ceiling: Capability
    account_max: Capability
    workspace_ceiling: Capability
    kill_switch_engaged: bool
    sentinel_present: bool
    standing_policy: Mapping[str, Any] | None
    subject_policy: Mapping[str, Any]
    attributes: Mapping[str, Any]
    governing_auto_reject: bool
    unresolved_governing_require_user: tuple[str, ...]
    pack_artifact_id: str | None
    pack_auto_confirmable: bool
    requirements: tuple[RepresentationRequirement, ...]
    apply_target: ApplyTargetFacts
    identity_key: str | None
    identity_strength: IdentityStrength
    identity_conflict: bool
    existing_intent_state: str | None  # None | "CLAIMED" | "CONFIRMED"
    intent_overridden: bool
    employer_key: str | None
    employer_key_strength: EmployerKeyStrength
    counters: tuple[CounterState, ...]
    budgets: tuple[BudgetState, ...]
    rule_acknowledgements: tuple[RuleAcknowledgement, ...]
    executor_hard_stops: tuple[str, ...] = ()
    grant_binding_drift: tuple[str, ...] = ()
    run_id: str | None = None


@dataclass(frozen=True)
class AuthorizationDecision:
    mode: Mode
    result: ResultKind
    requested_stage: Capability
    effective_capability: Capability
    grantable: bool
    deny_reason: str | None
    reasons: tuple[Reason, ...]
    require_user_items: tuple[RequireUserItem, ...]
    retry_at: datetime | None
    input_fingerprint: str
    engine_version: str
    policy_version_hash: str | None
    subject_policy_hash: str | None


class CanonicalHashError(ValueError):
    pass


def _canonicalize(value: Any) -> Any:
    if value is UNKNOWN:
        return {"$unknown": True}
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, IntEnum):
        return value.name
    if isinstance(value, Enum):
        return _canonicalize(value.value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CanonicalHashError("floats are not allowed in hashed payloads; use int or Decimal")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalHashError("non-finite Decimal")
        return str(value)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, datetime):
        return to_utc_iso(value, error=CanonicalHashError)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _canonicalize(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalHashError(f"non-string key: {k!r}")
            key = unicodedata.normalize("NFC", k)
            if key in out:
                raise CanonicalHashError(f"keys collide after NFC normalization: {k!r}")
            out[key] = _canonicalize(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    if isinstance(value, (set, frozenset)):
        items = [_canonicalize(v) for v in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    raise CanonicalHashError(f"unsupported type in hashed payload: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonicalize(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )


def canonical_hash(schema: str, schema_version: str, payload: Any) -> str:
    envelope = {"schema": schema, "schema_version": schema_version, "payload": payload}
    digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def to_utc_iso(value: datetime, *, error: type[Exception] = ValueError) -> str:
    """Fixed-width UTC ISO-8601 (always microseconds), so stored timestamps
    compare correctly as strings in SQL (e.g. grant expiry checks)."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise error("naive datetime is not allowed")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"naive timestamp: {value!r}")
    return parsed.astimezone(timezone.utc)


_EMPLOYER_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalized_employer_key(company: str | None) -> str | None:
    if not company:
        return None
    text = unicodedata.normalize("NFKC", company).casefold()
    text = _WS.sub(" ", _EMPLOYER_PUNCT.sub(" ", text)).strip()
    return f"name:{text}" if text else None
