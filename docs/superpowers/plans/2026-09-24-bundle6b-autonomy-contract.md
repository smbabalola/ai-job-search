# Bundle 6B: Autonomy Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the enforceable core of the Bundle 6 autonomy contract — a pure deterministic authorization gate, its policy data, the append-only decision ledger, single-use bound grants, limit reservations, submission intents, the pre-click transaction, kill switch (incl. file sentinel), shadow-mode decisions and the application dossier — with no scheduler, no browser automation and no live submission.

**Architecture:** Pure domain code in `product/` (contract types + canonical hashing, standing-policy evaluator, subject policy, transforms, fill-manifest schema, and `evaluate_authorization`). Persistence in three focused `webapp/persistence/autonomy_*.py` modules over one new migration. Orchestration in focused `webapp/services/autonomy_*.py` modules: context assembly reads the database into an immutable `AuthorizationContext`; the gate decides; services record decisions, issue/consume grants and run the atomic pre-click transaction. `product/` never imports `webapp/`.

**Tech Stack:** Python 3.13, sqlite3, FastAPI + Jinja2, pytest, Hypothesis 6.168.1 (already in `requirements-dev.txt`, commit `f15e29c`), `tzdata` (new runtime dependency, Task 2).

**Spec:** `docs/superpowers/specs/2026-09-24-bundle6b-autonomy-contract-design.md` (approved). Read it before starting any task; section numbers below (§n) refer to it.

## Global Constraints

- Branch: `bundle6/6b-autonomy-contract` (from `master` @ `91acb5a`). Commit after every task; never push without the user asking.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Run tests with `.venv/Scripts/python -m pytest …` (Windows). Full suite: `.venv/Scripts/python -m pytest -q`.
- Known Windows-only flakes on untouched master — NOT regressions: `test_application_blockers.py::test_latest_valid_answer_governs`, `test_artifacts.py::test_list_artifact_history_newest_first`, `test_workflow.py::test_record_status_change_tracks_previous_status` (fixed in 6A).
- `product/` must never import from `webapp/`.
- Capability order: `NONE < PREPARE < FILL < SUBMIT`. Only `deployment_ceiling`, `ACCOUNT_MAX`, `WORKSPACE_CEILING`/`DEFAULT_WORKSPACE_CEILING` may raise capability. **No record means `NONE`.**
- Decision result vocabulary: `ALLOW`, `REQUIRE_USER`, `BLOCK`, `DENY` (`invalid_input` | `kill_switch` | `stale_binding` | `duplicate`), `DENY_TEMPORARY` (`limit` | `budget`, `retryable`, `retry_at`).
- Precedence: `DENY(invalid_input|kill_switch|stale_binding) > BLOCK > DENY(duplicate) > DENY_TEMPORARY > REQUIRE_USER > ALLOW`. (`stale_binding` is the §10.3 step-3 denial.)
- Every append-only table has `seq INTEGER PRIMARY KEY AUTOINCREMENT`; every "current"/"latest" query orders by `seq DESC` — **never** by `created_at`.
- Every hash/fingerprint uses `product.autonomy_contract.canonical_hash(schema, schema_version, payload)`; hashed payloads contain no floats (use `int` or `Decimal`).
- All datetimes are timezone-aware; stored as ISO-8601 UTC strings. A naive datetime anywhere in a context is `DENY(invalid_input)`.
- TTLs: `FILL_SESSION_TTL = 30 min`, `SUBMIT_GRANT_TTL = 120 s`, `CLICK_DISPATCH_TTL = 60 s`, separate constants.
- Default limits: `submit_per_day=3`, `submit_per_run=3`, `fill_per_day=10`, `submit_per_employer_30d=2`. Live-submit canary cap (deployment): 1/day.
- Sentinel file: `<db_path.parent>/AUTONOMY_HALT`; presence = halt; checked synchronously; removal never resumes.
- Manual applications (no `application_workspace_origins` row) have no search workspace: their workspace ceiling is the account's `DEFAULT_WORKSPACE_CEILING` (or `NONE`).
- In 6B, an expired / stale-by-basis / context-unknown answer remains usable for FILL for every non-sensitive subject (spec §7.5 "where the subject allows" — v1 allows it for all non-sensitive subjects).
- Budget reconciliation against `provider_audits` actuals is deferred to 6C, where the first autonomous cost-generating operation runs; 6B implements budget *checks and reservations* from caller-supplied estimates.
- Existing Phase 4C `blocker_resolutions` are **not** migrated into `approved_answers` (§19.3).

## Review Focus

1. **Pre-6B applications already marked `applied`** — a person who applied to a job before 6B expects autonomy never to re-apply to it. Pinned by the backfill migration test in Task 16.
2. **Naive datetimes / clock input** — a context built with a naive `now` or a naive `confirmed_at` must fail closed as `DENY(invalid_input)`, not compare wrongly. Pinned in Task 5.
3. **Non-integer thresholds typed into the policy editor** (e.g. `75.5`) — must be rejected with a clear validation error, never a 500 or a silently different hash. Pinned in Task 2 and Task 19.
4. **Daylight-saving boundaries for day windows** — on the Europe/London clock-change days the "day" window and its `retry_at` (next local midnight) must still be correct. Pinned in Task 13.
5. **Kill switch operations repeated or out of order** — engaging twice, releasing when not engaged, or removing the sentinel must never resume anything and never error. Pinned in Task 12.

---

## File Structure

| File | Responsibility |
|---|---|
| `product/autonomy_contract.py` (new) | Enums, context/decision dataclasses, reason helpers, TTL constants, `UNKNOWN`, `canonical_json`/`canonical_hash`, `normalized_employer_key` |
| `product/standing_policy.py` (new) | Standing-policy schema/validation, attribute vocabulary, three-valued predicate evaluation, rule outcomes, hashes, employment-type normalization |
| `product/semantic_subject_registry.py` (modify) | Add 12 subject keys, bump version to `v2` |
| `product/semantic_subject_policy.v1.json` (new) | Per-subject policy data |
| `product/semantic_subject_policy.py` (new) | Load/validate/hash subject policy |
| `product/representation_transforms.py` (new) | Closed transform set with round-trip checks |
| `product/fill_manifest.py` (new) | Fill-manifest schema, validation, hashing |
| `product/autonomy_gate.py` (new) | `evaluate_authorization` |
| `webapp/persistence/migrations.py` (modify) | `016_autonomy_contract`, `017_autonomy_human_intent_backfill` |
| `webapp/persistence/autonomy_authority.py` (new) | Authorizations, kill switch rows, control events, policy versions, runs |
| `webapp/persistence/autonomy_answers.py` (new) | Approved/proposed answers, confirmations, rule acknowledgements, apply-target confirmations |
| `webapp/persistence/autonomy_ledger.py` (new) | Decisions, grants, reservations, intents, attempts, dry-run cases, queue items |
| `webapp/config.py` (modify) | Deployment-ceiling settings, sentinel path |
| `webapp/services/autonomy_controls.py` (new) | Kill switch (+ grant revocation), sentinel, pause/resume, resume-all, enable-preparation |
| `webapp/services/autonomy_context.py` (new) | Assemble `AuthorizationContext` from the database |
| `webapp/services/autonomy.py` (new) | Decide-and-record, grant issuance, pre-click transaction, dispatch, expiry, attempt state |
| `webapp/services/autonomy_intents.py` (new) | Human-path intents |
| `webapp/persistence/workflow.py` (modify) | Call human-intent hook on `applied` |
| `webapp/services/autonomy_shadow.py` (new) | Shadow decisions at existing workflow points |
| `webapp/services/autonomy_dossier.py` (new) | Application dossier |
| `webapp/api/autonomy.py` (new), `webapp/app.py` (modify) | Routes |
| `webapp/templates/autonomy.html`, `webapp/templates/autonomy_dossier.html` (new) | UI |
| `requirements.txt` (modify) | `tzdata` |

---

### Task 1: Contract types and canonical hashing

**Files:**
- Create: `product/autonomy_contract.py`
- Test: `tests/product/test_autonomy_contract.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Capability(IntEnum)`, `Mode`, `ResultKind`, `ProvenanceTier`, `IdentityStrength`, `EmployerKeyStrength`, `Reach`, `REACH_ORDER`, `UNKNOWN` / `is_unknown(v)`, `Reason`, `reason(code, **params)`, `RequireUserItem`, `AnswerCandidate`, `RepresentationRequirement`, `ApplyTargetFacts`, `CounterState`, `BudgetState`, `RuleAcknowledgement`, `AuthorizationContext`, `AuthorizationDecision`, `ENGINE_VERSION`, `CONTEXT_SCHEMA`, `CONTEXT_SCHEMA_VERSION`, TTL constants, `CanonicalHashError`, `canonical_json(value) -> str`, `canonical_hash(schema, schema_version, payload) -> str`, `normalized_employer_key(company: str | None) -> str | None`, `to_utc_iso(dt) -> str`, `parse_utc(s) -> datetime`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/product/test_autonomy_contract.py
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, strategies as st

from product.autonomy_contract import (
    CLICK_DISPATCH_TTL, FILL_SESSION_TTL, SUBMIT_GRANT_TTL,
    Capability, CanonicalHashError, UNKNOWN, canonical_hash, canonical_json,
    is_unknown, normalized_employer_key, parse_utc, reason, to_utc_iso,
)


def test_capability_is_totally_ordered():
    assert Capability.NONE < Capability.PREPARE < Capability.FILL < Capability.SUBMIT


def test_ttls_are_stage_specific():
    assert FILL_SESSION_TTL == timedelta(minutes=30)
    assert SUBMIT_GRANT_TTL == timedelta(seconds=120)
    assert CLICK_DISPATCH_TTL == timedelta(seconds=60)


def test_hash_ignores_key_order_and_whitespace():
    a = canonical_hash("s", "v1", {"b": 1, "a": {"y": [1, 2], "x": "é"}})
    b = canonical_hash("s", "v1", {"a": {"x": "é", "y": [1, 2]}, "b": 1})
    assert a == b
    assert a.startswith("sha256:")


def test_hash_includes_schema_and_version():
    payload = {"a": 1}
    assert canonical_hash("s", "v1", payload) != canonical_hash("s", "v2", payload)
    assert canonical_hash("s", "v1", payload) != canonical_hash("t", "v1", payload)


def test_floats_and_nan_are_rejected():
    with pytest.raises(CanonicalHashError):
        canonical_hash("s", "v1", {"x": 75.5})
    with pytest.raises(CanonicalHashError):
        canonical_json(float("nan"))


def test_decimals_serialize_as_strings():
    assert canonical_json({"x": Decimal("1.50")}) == '{"x":"1.50"}'


def test_naive_datetime_rejected_aware_normalized_to_utc():
    with pytest.raises(CanonicalHashError):
        canonical_json(datetime(2026, 9, 24, 12, 0))
    bst = timezone(timedelta(hours=1))
    assert canonical_json(datetime(2026, 9, 24, 13, 0, tzinfo=bst)) == '"2026-09-24T12:00:00.000000+00:00"'


def test_sets_are_sorted_and_unknown_is_explicit():
    assert canonical_json(frozenset({"b", "a"})) == '["a","b"]'
    assert canonical_json(UNKNOWN) == '{"$unknown":true}'
    assert is_unknown(UNKNOWN) and not is_unknown(None)


def test_nfc_key_collision_rejected():
    with pytest.raises(CanonicalHashError):
        canonical_json({"é": 1, "é": 2})


def test_capability_serializes_by_name():
    assert canonical_json(Capability.FILL) == '"FILL"'


def test_reason_params_are_sorted_strings():
    r = reason("rule_reduce", rule="r1", to="PREPARE")
    assert r.code == "rule_reduce"
    assert r.params == (("rule", "r1"), ("to", "PREPARE"))


def test_normalized_employer_key():
    assert normalized_employer_key("  Wood  Group PLC ") == "name:wood group plc"
    assert normalized_employer_key("Wood Group, PLC.") == "name:wood group plc"
    assert normalized_employer_key("") is None
    assert normalized_employer_key(None) is None


def test_utc_iso_round_trip():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    assert parse_utc(to_utc_iso(now)) == now
    with pytest.raises(ValueError):
        to_utc_iso(datetime(2026, 9, 24, 12, 0))


json_leaf = st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=8))
json_value = st.recursive(
    json_leaf,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(alphabet="abcxyz_", max_size=6), children, max_size=4),
    ),
    max_leaves=12,
)


@given(json_value)
def test_hash_is_stable_under_dict_reordering(value):
    def reorder(v):
        if isinstance(v, dict):
            return {k: reorder(v[k]) for k in reversed(list(v))}
        if isinstance(v, list):
            return [reorder(x) for x in v]
        return v
    assert canonical_hash("s", "v1", value) == canonical_hash("s", "v1", reorder(value))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/product/test_autonomy_contract.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'product.autonomy_contract'`

- [ ] **Step 3: Implement `product/autonomy_contract.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/product/test_autonomy_contract.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add product/autonomy_contract.py tests/product/test_autonomy_contract.py
git commit -m "feat(product): add autonomy contract types and canonical hashing

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Standing policy document (and the `tzdata` dependency)

**Files:**
- Modify: `requirements.txt` (separate commit, Step 1)
- Create: `product/standing_policy.py`
- Test: `tests/product/test_standing_policy.py`

**Interfaces:**
- Consumes: `canonical_hash`, `UNKNOWN`, `is_unknown`, `Capability` (Task 1).
- Produces: `STANDING_POLICY_SCHEMA_VERSION = "standing-policy.v1"`, `DEFAULT_LIMITS`, `BUDGET_CATEGORIES`, `StandingPolicyError(errors: list[str])`, `validate_standing_policy(doc) -> None`, `default_policy_document(timezone: str) -> dict`, `policy_hash(doc) -> str`, `rule_hash(rule) -> str`, `evaluate_predicate(pred, attributes, employer_lists) -> bool | UNKNOWN`, `referenced_attributes(pred) -> list[str]`, `observed_fingerprint(rule, attributes) -> str`, `RuleOutcome`, `evaluate_rules(doc, attributes) -> tuple[RuleOutcome, ...]`, `normalize_employment_type(text) -> str | UNKNOWN`.

- [ ] **Step 1: Add `tzdata` as a deliberate runtime dependency (own commit)**

On Windows, `zoneinfo` has no system time-zone database; without `tzdata`, `ZoneInfo("Europe/London")` raises `ZoneInfoNotFoundError` (verified 2026-09-24 in `.venv`). Append to `requirements.txt`:

```
tzdata==2026.4
```

Run: `.venv/Scripts/python -m pip install tzdata==2026.4` then `.venv/Scripts/python -c "import zoneinfo; print(zoneinfo.ZoneInfo('Europe/London'))"`
Expected: prints `Europe/London`.

```bash
git add requirements.txt
git commit -m "build: add tzdata for IANA time zones on Windows

Autonomy limit windows use each account's IANA timezone (6B spec §12.1).
Windows Python has no system tz database, so zoneinfo needs tzdata.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/product/test_standing_policy.py
from __future__ import annotations

import copy

import pytest

from product.autonomy_contract import UNKNOWN
from product.standing_policy import (
    DEFAULT_LIMITS, StandingPolicyError, default_policy_document, evaluate_predicate,
    evaluate_rules, normalize_employment_type, observed_fingerprint, policy_hash,
    rule_hash, validate_standing_policy,
)


def _doc(*rules, lists=None):
    doc = default_policy_document("Europe/London")
    doc["rules"] = list(rules)
    if lists:
        doc["employer_lists"] = lists
    return doc


MIN_FIT = {
    "id": "min-fit-75", "description": "Only prepare below fit 75",
    "when": {"attr": "fit.overall_score", "op": "lt", "value": 75},
    "effect": {"type": "REDUCE_TO", "level": "PREPARE"},
    "on_unknown": {"type": "REDUCE_TO", "level": "PREPARE"},
}
PERMANENT_ONLY = {
    "id": "permanent-only", "description": "Permanent roles only",
    "when": {"attr": "job.employment_type", "op": "ne", "value": "PERMANENT"},
    "effect": {"type": "REQUIRE_USER"},
    "on_unknown": {"type": "REQUIRE_USER"},
}
DENY_LIST = {
    "id": "deny-list", "description": "Never apply to denied employers",
    "when": {"attr": "company.key", "op": "in_list", "value": "deny"},
    "effect": {"type": "BLOCK"},
    "on_unknown": {"type": "REQUIRE_USER"},
}


def test_default_document_is_valid_and_has_default_limits():
    doc = default_policy_document("Europe/London")
    validate_standing_policy(doc)
    assert doc["limits"]["submit_per_day"] == DEFAULT_LIMITS["submit_per_day"] == 3
    assert doc["limits"]["submit_per_run"] == 3
    assert doc["limits"]["fill_per_day"] == 10
    assert doc["limits"]["submit_per_employer_30d"] == 2
    assert doc["rules"] == []


def test_valid_rules_accepted():
    validate_standing_policy(_doc(MIN_FIT, PERMANENT_ONLY, DENY_LIST, lists={"deny": ["name:acme"]}))


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(timezone="Mars/Olympus"), "timezone"),
    (lambda d: d.pop("timezone"), "timezone"),
    (lambda d: d["rules"].append({**MIN_FIT, "effect": {"type": "GRANT", "level": "SUBMIT"}}), "effect"),
    (lambda d: d["rules"].append({**MIN_FIT, "effect": {"type": "REDUCE_TO", "level": "SUBMIT"}}), "level"),
    (lambda d: d["rules"].append({k: v for k, v in MIN_FIT.items() if k != "on_unknown"}), "on_unknown"),
    (lambda d: d["rules"].append({**DENY_LIST, "on_unknown": {"type": "NO_EFFECT"}}), "NO_EFFECT"),
    (lambda d: d["rules"].append({**MIN_FIT, "when": {"attr": "salary.secret", "op": "lt", "value": 1}}), "attr"),
    (lambda d: d["rules"].append({**MIN_FIT, "when": {"attr": "fit.overall_score", "op": "approx", "value": 1}}), "op"),
    (lambda d: d["rules"].append({**MIN_FIT, "when": {"attr": "fit.overall_score", "op": "lt", "value": 75.5}}), "integer"),
    (lambda d: d["rules"].extend([MIN_FIT, MIN_FIT]), "duplicate"),
    (lambda d: d["limits"].update(submit_per_day=-1), "submit_per_day"),
    (lambda d: d["limits"]["budgets"].update(LLM={"per_day": "abc", "per_application": "1"}), "budget"),
    (lambda d: d.update(surprise=True), "unknown"),
    (lambda d: d["rules"].append({**DENY_LIST}), "employer list"),
])
def test_invalid_documents_rejected_with_all_errors(mutate, message):
    doc = default_policy_document("Europe/London")
    mutate(doc)
    with pytest.raises(StandingPolicyError) as exc:
        validate_standing_policy(doc)
    assert any(message in e for e in exc.value.errors), exc.value.errors


def test_three_valued_leaf_and_combinators():
    attrs = {"fit.overall_score": 60, "job.employment_type": UNKNOWN}
    assert evaluate_predicate({"attr": "fit.overall_score", "op": "lt", "value": 75}, attrs, {}) is True
    assert evaluate_predicate({"attr": "fit.overall_score", "op": "gte", "value": 75}, attrs, {}) is False
    unknown_leaf = {"attr": "job.employment_type", "op": "eq", "value": "PERMANENT"}
    assert evaluate_predicate(unknown_leaf, attrs, {}) is UNKNOWN
    assert evaluate_predicate({"attr": "job.title", "op": "eq", "value": "x"}, attrs, {}) is UNKNOWN
    false_leaf = {"attr": "fit.overall_score", "op": "gt", "value": 90}
    true_leaf = {"attr": "fit.overall_score", "op": "lt", "value": 90}
    assert evaluate_predicate({"all": [false_leaf, unknown_leaf]}, attrs, {}) is False
    assert evaluate_predicate({"all": [true_leaf, unknown_leaf]}, attrs, {}) is UNKNOWN
    assert evaluate_predicate({"any": [true_leaf, unknown_leaf]}, attrs, {}) is True
    assert evaluate_predicate({"any": [false_leaf, unknown_leaf]}, attrs, {}) is UNKNOWN
    assert evaluate_predicate({"not": unknown_leaf}, attrs, {}) is UNKNOWN
    assert evaluate_predicate({"not": true_leaf}, attrs, {}) is False


def test_wrong_runtime_type_is_unknown_not_error():
    assert evaluate_predicate(
        {"attr": "fit.overall_score", "op": "lt", "value": 75}, {"fit.overall_score": "high"}, {},
    ) is UNKNOWN


def test_decimal_scores_compare_exactly():
    from decimal import Decimal
    pred = {"attr": "fit.overall_score", "op": "lt", "value": 75}
    assert evaluate_predicate(pred, {"fit.overall_score": Decimal("74.9")}, {}) is True
    assert evaluate_predicate(pred, {"fit.overall_score": Decimal("75.0")}, {}) is False
    assert evaluate_predicate(pred, {"fit.overall_score": 74.9}, {}) is UNKNOWN  # floats never trusted


def test_in_list_and_contains():
    lists = {"deny": ["name:acme"]}
    assert evaluate_predicate({"attr": "company.key", "op": "in_list", "value": "deny"}, {"company.key": "name:acme"}, lists) is True
    assert evaluate_predicate({"attr": "company.key", "op": "in_list", "value": "deny"}, {"company.key": UNKNOWN}, lists) is UNKNOWN
    assert evaluate_predicate({"attr": "job.title", "op": "contains", "value": "Fluids"}, {"job.title": "Senior drilling fluids engineer"}, {}) is True


def test_rule_outcomes_use_declared_on_unknown():
    doc = _doc(MIN_FIT, PERMANENT_ONLY)
    outcomes = {o.rule_id: o for o in evaluate_rules(doc, {"fit.overall_score": UNKNOWN, "job.employment_type": "PERMANENT"})}
    assert outcomes["min-fit-75"].applied_effect == {"type": "REDUCE_TO", "level": "PREPARE"}
    assert outcomes["min-fit-75"].via_unknown is True
    assert outcomes["permanent-only"].applied_effect is None


def test_no_effect_on_unknown_means_no_effect():
    rule = {**MIN_FIT, "on_unknown": {"type": "NO_EFFECT"}}
    (outcome,) = evaluate_rules(_doc(rule), {"fit.overall_score": UNKNOWN})
    assert outcome.applied_effect is None and outcome.via_unknown is True


def test_hashes():
    doc = _doc(MIN_FIT)
    reordered = copy.deepcopy(doc)
    reordered["rules"][0] = dict(reversed(list(reordered["rules"][0].items())))
    assert policy_hash(doc) == policy_hash(reordered)
    assert rule_hash(MIN_FIT) == rule_hash(dict(reversed(list(MIN_FIT.items()))))
    assert rule_hash(MIN_FIT) != rule_hash({**MIN_FIT, "when": {**MIN_FIT["when"], "value": 70}})


def test_observed_fingerprint_depends_only_on_referenced_attributes():
    a = observed_fingerprint(PERMANENT_ONLY, {"job.employment_type": "CONTRACT", "fit.overall_score": 10})
    b = observed_fingerprint(PERMANENT_ONLY, {"job.employment_type": "CONTRACT", "fit.overall_score": 99})
    c = observed_fingerprint(PERMANENT_ONLY, {"job.employment_type": "TEMPORARY"})
    assert a == b and a != c


@pytest.mark.parametrize("text, expected", [
    ("Permanent", "PERMANENT"), ("Full-time, permanent", "PERMANENT"),
    ("Contract", "CONTRACT"), ("Fixed-term contract", "CONTRACT"),
    ("Temporary", "TEMPORARY"), ("Internship", "INTERNSHIP"),
    ("Full-time", UNKNOWN), (None, UNKNOWN), ("Permanent or contract", UNKNOWN),
])
def test_normalize_employment_type(text, expected):
    assert normalize_employment_type(text) == expected
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/product/test_standing_policy.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'product.standing_policy'`

- [ ] **Step 4: Implement `product/standing_policy.py`**

```python
"""Standing-policy document: restrictive rules, limits and employer lists
(6B spec §5, §12). Rules can only REDUCE_TO, BLOCK or REQUIRE_USER -- the
schema has no granting effect, so a policy cannot raise authority by
construction. Predicates are three-valued; every rule declares its own
on_unknown behaviour.
"""
from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from product.autonomy_contract import UNKNOWN, canonical_hash, is_unknown

STANDING_POLICY_SCHEMA = "standing-policy"
STANDING_POLICY_SCHEMA_VERSION = "standing-policy.v1"

ATTRIBUTE_TYPES: dict[str, str] = {
    "fit.overall_score": "int",
    "fit.verdict": "str",
    "job.employment_type": "str",
    "job.location": "str",
    "job.title": "str",
    "company.key": "str",
    "workspace.id": "str",
    "identity.strength": "str",
}
COMPARISON_OPS = {"lt", "lte", "gt", "gte"}
OPERATORS = {"eq", "ne", "in", "not_in", "contains", "in_list"} | COMPARISON_OPS
EFFECT_TYPES = {"REDUCE_TO", "BLOCK", "REQUIRE_USER"}
ON_UNKNOWN_TYPES = EFFECT_TYPES | {"NO_EFFECT"}
REDUCIBLE_LEVELS = {"NONE", "PREPARE", "FILL"}
LIMIT_KEYS = ("submit_per_day", "submit_per_run", "fill_per_day", "submit_per_employer_30d")
DEFAULT_LIMITS = {"submit_per_day": 3, "submit_per_run": 3, "fill_per_day": 10, "submit_per_employer_30d": 2}
BUDGET_CATEGORIES = ("LLM", "BROWSER", "EXTERNAL_API", "OTHER")
_TOP_KEYS = {"schema_version", "timezone", "rules", "limits", "employer_lists"}
_RULE_KEYS = {"id", "description", "when", "effect", "on_unknown"}


class StandingPolicyError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def default_policy_document(timezone: str) -> dict[str, Any]:
    return {
        "schema_version": STANDING_POLICY_SCHEMA_VERSION,
        "timezone": timezone,
        "rules": [],
        "limits": {**DEFAULT_LIMITS, "budget_currency": "USD", "budgets": {}},
        "employer_lists": {},
    }


def _validate_value(attr: str, op: str, value: Any, path: str, errors: list[str]) -> None:
    kind = ATTRIBUTE_TYPES[attr]
    if op in ("in", "not_in"):
        if not isinstance(value, list) or not value:
            errors.append(f"{path}.value: '{op}' needs a non-empty list")
            return
        for item in value:
            _validate_scalar(kind, item, f"{path}.value[]", errors)
        return
    if op == "in_list":
        if not isinstance(value, str):
            errors.append(f"{path}.value: 'in_list' needs an employer list name")
        return
    if op == "contains":
        if kind != "str" or not isinstance(value, str):
            errors.append(f"{path}: 'contains' needs a string attribute and string value")
        return
    if op in COMPARISON_OPS and kind != "int":
        errors.append(f"{path}: '{op}' needs a numeric attribute")
        return
    _validate_scalar(kind, value, f"{path}.value", errors)


def _validate_scalar(kind: str, value: Any, path: str, errors: list[str]) -> None:
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{path}: must be an integer (no decimals), got {value!r}")
    elif not isinstance(value, str):
        errors.append(f"{path}: must be a string, got {value!r}")


def _validate_predicate(pred: Any, path: str, lists: Mapping[str, Any], errors: list[str]) -> None:
    if not isinstance(pred, dict):
        errors.append(f"{path}: predicate must be an object")
        return
    if set(pred) == {"all"} or set(pred) == {"any"}:
        (key,) = pred
        children = pred[key]
        if not isinstance(children, list) or not children:
            errors.append(f"{path}.{key}: must be a non-empty list")
            return
        for i, child in enumerate(children):
            _validate_predicate(child, f"{path}.{key}[{i}]", lists, errors)
        return
    if set(pred) == {"not"}:
        _validate_predicate(pred["not"], f"{path}.not", lists, errors)
        return
    if set(pred) != {"attr", "op", "value"}:
        errors.append(f"{path}: unknown predicate keys {sorted(pred)}")
        return
    attr, op = pred["attr"], pred["op"]
    if attr not in ATTRIBUTE_TYPES:
        errors.append(f"{path}.attr: unknown attr {attr!r}")
        return
    if op not in OPERATORS:
        errors.append(f"{path}.op: unknown op {op!r}")
        return
    _validate_value(attr, op, pred["value"], path, errors)
    if op == "in_list" and isinstance(pred["value"], str) and pred["value"] not in lists:
        errors.append(f"{path}.value: unknown employer list {pred['value']!r}")


def _validate_effect(effect: Any, path: str, allowed: set[str], errors: list[str]) -> None:
    if not isinstance(effect, dict) or effect.get("type") not in allowed:
        errors.append(f"{path}: effect type must be one of {sorted(allowed)}")
        return
    if effect["type"] == "REDUCE_TO":
        if set(effect) != {"type", "level"} or effect["level"] not in REDUCIBLE_LEVELS:
            errors.append(f"{path}.level: REDUCE_TO level must be one of {sorted(REDUCIBLE_LEVELS)}")
    elif set(effect) != {"type"}:
        errors.append(f"{path}: unexpected keys {sorted(effect)}")


def _validate_decimal(value: Any, path: str, errors: list[str]) -> None:
    try:
        ok = isinstance(value, str) and Decimal(value).is_finite() and Decimal(value) >= 0
    except InvalidOperation:
        ok = False
    if not ok:
        errors.append(f"{path}: budget amount must be a non-negative decimal string")


def validate_standing_policy(doc: Any) -> None:
    errors: list[str] = []
    if not isinstance(doc, dict):
        raise StandingPolicyError(["policy must be an object"])
    unknown = set(doc) - _TOP_KEYS
    if unknown:
        errors.append(f"unknown top-level keys: {sorted(unknown)}")
    if doc.get("schema_version") != STANDING_POLICY_SCHEMA_VERSION:
        errors.append("schema_version: must be standing-policy.v1")
    tz = doc.get("timezone")
    try:
        if not isinstance(tz, str):
            raise zoneinfo.ZoneInfoNotFoundError(tz)
        zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        errors.append(f"timezone: not a valid IANA timezone: {tz!r}")
    lists = doc.get("employer_lists", {})
    if not isinstance(lists, dict) or any(
        not isinstance(v, list) or not all(isinstance(x, str) for x in v) for v in lists.values()
    ):
        errors.append("employer_lists: must map names to lists of employer keys")
        lists = {}
    limits = doc.get("limits")
    if not isinstance(limits, dict):
        errors.append("limits: required object")
    else:
        for key in LIMIT_KEYS:
            value = limits.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errors.append(f"limits.{key}: must be a non-negative integer")
        if not isinstance(limits.get("budget_currency"), str) or len(limits["budget_currency"]) != 3:
            errors.append("limits.budget_currency: must be a 3-letter currency code")
        budgets = limits.get("budgets", {})
        if not isinstance(budgets, dict):
            errors.append("limits.budgets: must be an object")
        else:
            for category, caps in budgets.items():
                if category not in BUDGET_CATEGORIES:
                    errors.append(f"limits.budgets: unknown budget category {category!r}")
                    continue
                if not isinstance(caps, dict) or set(caps) != {"per_day", "per_application"}:
                    errors.append(f"limits.budgets.{category}: budget needs per_day and per_application")
                    continue
                for k, v in caps.items():
                    _validate_decimal(v, f"limits.budgets.{category}.{k}", errors)
        extra = set(limits) - set(LIMIT_KEYS) - {"budget_currency", "budgets"}
        if extra:
            errors.append(f"limits: unknown keys {sorted(extra)}")
    rules = doc.get("rules")
    if not isinstance(rules, list):
        errors.append("rules: must be a list")
        rules = []
    seen: set[str] = set()
    for i, rule in enumerate(rules):
        path = f"rules[{i}]"
        if not isinstance(rule, dict):
            errors.append(f"{path}: must be an object")
            continue
        if set(rule) != _RULE_KEYS:
            errors.append(f"{path}: keys must be exactly {sorted(_RULE_KEYS)} (on_unknown is mandatory)")
            continue
        if not isinstance(rule["id"], str) or not rule["id"]:
            errors.append(f"{path}.id: required")
        elif rule["id"] in seen:
            errors.append(f"{path}.id: duplicate rule id {rule['id']!r}")
        seen.add(rule["id"])
        _validate_predicate(rule["when"], f"{path}.when", lists, errors)
        _validate_effect(rule["effect"], f"{path}.effect", EFFECT_TYPES, errors)
        _validate_effect(rule["on_unknown"], f"{path}.on_unknown", ON_UNKNOWN_TYPES, errors)
        if (
            isinstance(rule["effect"], dict) and rule["effect"].get("type") == "BLOCK"
            and isinstance(rule["on_unknown"], dict) and rule["on_unknown"].get("type") == "NO_EFFECT"
        ):
            errors.append(f"{path}.on_unknown: NO_EFFECT is not allowed on a BLOCK rule")
    if errors:
        raise StandingPolicyError(errors)


def policy_hash(doc: Mapping[str, Any]) -> str:
    return canonical_hash(STANDING_POLICY_SCHEMA, STANDING_POLICY_SCHEMA_VERSION, doc)


def rule_hash(rule: Mapping[str, Any]) -> str:
    return canonical_hash("standing-policy-rule", STANDING_POLICY_SCHEMA_VERSION, rule)


def referenced_attributes(pred: Mapping[str, Any]) -> list[str]:
    if "all" in pred or "any" in pred:
        children = pred.get("all", pred.get("any"))
        return sorted({a for child in children for a in referenced_attributes(child)})
    if "not" in pred:
        return referenced_attributes(pred["not"])
    return [pred["attr"]]


def observed_fingerprint(rule: Mapping[str, Any], attributes: Mapping[str, Any]) -> str:
    observed = {attr: attributes.get(attr, UNKNOWN) for attr in referenced_attributes(rule["when"])}
    return canonical_hash("rule-observation", "v1", observed)


def _leaf(pred: Mapping[str, Any], attributes: Mapping[str, Any], lists: Mapping[str, list[str]]) -> Any:
    attr, op, target = pred["attr"], pred["op"], pred["value"]
    value = attributes.get(attr, UNKNOWN)
    if value is None or is_unknown(value):
        return UNKNOWN
    kind = ATTRIBUTE_TYPES[attr]
    # Rule thresholds are integers; runtime values may be int or Decimal
    # (Job Fit scores can be fractional -- the assembler passes Decimal,
    # never float, so hashing stays exact). Anything else is UNKNOWN.
    if kind == "int" and (isinstance(value, bool) or not isinstance(value, (int, Decimal))):
        return UNKNOWN
    if kind == "str" and not isinstance(value, str):
        return UNKNOWN
    if op == "eq":
        return value == target
    if op == "ne":
        return value != target
    if op == "in":
        return value in target
    if op == "not_in":
        return value not in target
    if op == "contains":
        return target.casefold() in value.casefold()
    if op == "in_list":
        return value in lists.get(target, [])
    return {"lt": value < target, "lte": value <= target, "gt": value > target, "gte": value >= target}[op]


def evaluate_predicate(pred: Mapping[str, Any], attributes: Mapping[str, Any], lists: Mapping[str, list[str]]) -> Any:
    if "all" in pred:
        results = [evaluate_predicate(c, attributes, lists) for c in pred["all"]]
        if any(r is False for r in results):
            return False
        return UNKNOWN if any(is_unknown(r) for r in results) else True
    if "any" in pred:
        results = [evaluate_predicate(c, attributes, lists) for c in pred["any"]]
        if any(r is True for r in results):
            return True
        return UNKNOWN if any(is_unknown(r) for r in results) else False
    if "not" in pred:
        inner = evaluate_predicate(pred["not"], attributes, lists)
        return inner if is_unknown(inner) else not inner
    return _leaf(pred, attributes, lists)


@dataclass(frozen=True)
class RuleOutcome:
    rule_id: str
    rule_hash: str
    observed_fingerprint: str
    applied_effect: dict[str, Any] | None
    via_unknown: bool


def evaluate_rules(doc: Mapping[str, Any], attributes: Mapping[str, Any]) -> tuple[RuleOutcome, ...]:
    lists = doc.get("employer_lists", {})
    outcomes = []
    for rule in doc["rules"]:
        truth = evaluate_predicate(rule["when"], attributes, lists)
        via_unknown = is_unknown(truth)
        if via_unknown:
            effect = None if rule["on_unknown"]["type"] == "NO_EFFECT" else dict(rule["on_unknown"])
        else:
            effect = dict(rule["effect"]) if truth else None
        outcomes.append(RuleOutcome(
            rule_id=rule["id"], rule_hash=rule_hash(rule),
            observed_fingerprint=observed_fingerprint(rule, attributes),
            applied_effect=effect, via_unknown=via_unknown,
        ))
    return tuple(outcomes)


_EMPLOYMENT_PATTERNS = (
    (re.compile(r"\bpermanent\b", re.I), "PERMANENT"),
    (re.compile(r"\b(contract|contractor|fixed[- ]term)\b", re.I), "CONTRACT"),
    (re.compile(r"\b(temporary|temp)\b", re.I), "TEMPORARY"),
    (re.compile(r"\b(internship|intern)\b", re.I), "INTERNSHIP"),
)


def normalize_employment_type(text: str | None) -> Any:
    """Deterministic, conservative mapping; ambiguous or unmatched text is UNKNOWN."""
    if not text:
        return UNKNOWN
    matches = {label for pattern, label in _EMPLOYMENT_PATTERNS if pattern.search(text)}
    if len(matches) != 1:
        return UNKNOWN
    return matches.pop()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/product/test_standing_policy.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add product/standing_policy.py tests/product/test_standing_policy.py
git commit -m "feat(product): add standing-policy schema and three-valued evaluator

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Subject registry extension and subject policy data

**Files:**
- Modify: `product/semantic_subject_registry.py:33-45` (version + new keys)
- Modify: `tests/product/test_semantic_subject_registry.py:19-29` (expected key set, version)
- Create: `product/semantic_subject_policy.v1.json`, `product/semantic_subject_policy.py`
- Test: `tests/product/test_semantic_subject_policy.py`

**Interfaces:**
- Consumes: `SEMANTIC_SUBJECTS`, `canonical_hash`, `Reach`, `REACH_ORDER`.
- Produces: `SUBJECT_POLICY_SCHEMA_VERSION = "semantic-subject-policy.v1"`, `SENSITIVE_CLASSES`, `SubjectPolicyError(errors)`, `load_subject_policy(path=DEFAULT_PATH) -> dict`, `validate_subject_policy(doc) -> None`, `subject_policy_hash(doc) -> str`, `subject_entry(doc, subject) -> dict | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/product/test_semantic_subject_policy.py
from __future__ import annotations

import copy

import pytest

from product.semantic_subject_policy import (
    SubjectPolicyError, load_subject_policy, subject_entry, subject_policy_hash,
    validate_subject_policy,
)
from product.semantic_subject_registry import SEMANTIC_SUBJECTS


def test_policy_covers_exactly_the_registry():
    doc = load_subject_policy()
    assert set(doc["subjects"]) == set(SEMANTIC_SUBJECTS)


def test_baseline_values():
    doc = load_subject_policy()
    salary = subject_entry(doc, "compensation.salary_expectation")
    assert salary["context_keys"] == ["currency", "region", "employment_type"]
    assert salary["freshness_days"] == 60 and salary["max_reach"] == "SEARCH_WORKSPACE"
    assert subject_entry(doc, "work_authorization.right_to_work")["freshness_days"] is None
    assert subject_entry(doc, "motivation.employer_specific")["max_reach"] == "EMPLOYER"
    assert subject_entry(doc, "motivation.role_type")["answer_kind"] == "FREE_TEXT"
    for key in ("legal.attestation", "demographic.eeo", "background.criminal_record", "health.disability"):
        entry = subject_entry(doc, key)
        assert entry["sensitive"] is not None and entry["submit_eligible"] is False
    assert subject_entry(doc, "no.such.subject") is None


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["subjects"].pop("licence.driving"), "missing"),
    (lambda d: d["subjects"].__setitem__("made.up", d["subjects"]["licence.driving"]), "unknown"),
    (lambda d: d["subjects"]["legal.attestation"].update(submit_eligible=True), "sensitive"),
    (lambda d: d["subjects"]["motivation.employer_specific"].update(default_reach="ACCOUNT"), "default_reach"),
    (lambda d: d["subjects"]["licence.driving"].update(freshness_days=0), "freshness_days"),
    (lambda d: d["subjects"]["licence.driving"].update(answer_kind="PROSE"), "answer_kind"),
])
def test_invalid_subject_policy_rejected(mutate, message):
    doc = copy.deepcopy(load_subject_policy())
    mutate(doc)
    with pytest.raises(SubjectPolicyError) as exc:
        validate_subject_policy(doc)
    assert any(message in e for e in exc.value.errors), exc.value.errors


def test_hash_is_stable():
    doc = load_subject_policy()
    assert subject_policy_hash(doc) == subject_policy_hash(copy.deepcopy(doc))
```

Also update `tests/product/test_semantic_subject_registry.py`: replace the expected key set at lines 19-27 with the full 16-key set below and change `== "v1"` to `== "v2"`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/product/test_semantic_subject_policy.py tests/product/test_semantic_subject_registry.py -q`
Expected: FAIL — module not found / key set mismatch.

- [ ] **Step 3: Extend the registry**

In `product/semantic_subject_registry.py` set `SEMANTIC_SUBJECT_REGISTRY_VERSION = "v2"` and extend `SEMANTIC_SUBJECTS` (keep the four existing entries unchanged; `classify_semantic_subject` and its patterns are NOT changed — new subjects are classified from employer forms in 6D):

```python
SEMANTIC_SUBJECT_REGISTRY_VERSION = "v2"

SEMANTIC_SUBJECTS: dict[str, str] = {
    "work_authorization.right_to_work": (
        "Right to work in the target country, including citizenship-based eligibility."
    ),
    "work_authorization.sponsorship_required": (
        "Whether the candidate requires visa/work-permit sponsorship."
    ),
    "employment.notice_period": "The candidate's current notice period.",
    "licence.driving": "Whether the candidate holds a valid driving licence.",
    # Bundle 6B additions (autonomy contract spec §6.2). Additive only.
    "employment.availability_start": "The earliest date the candidate can start.",
    "compensation.salary_expectation": "The candidate's salary expectation for a given context.",
    "mobility.relocation": "Whether the candidate is willing to relocate to a region.",
    "mobility.travel_or_rotation": "Willingness to travel or work a rotation pattern in a region.",
    "motivation.role_type": "Why the candidate is interested in this type of role.",
    "motivation.industry": "What motivates the candidate about this industry.",
    "motivation.job_search_reason": "Why the candidate is looking for a new opportunity.",
    "motivation.employer_specific": "Why the candidate wants to work for this specific employer.",
    "legal.attestation": "A legal declaration or attestation required by the employer.",
    "demographic.eeo": "Demographic / equal-opportunity monitoring questions.",
    "background.criminal_record": "Criminal-record disclosure questions.",
    "health.disability": "Health or disability disclosure questions.",
}
```

- [ ] **Step 4: Create `product/semantic_subject_policy.v1.json`**

```json
{
  "schema_version": "semantic-subject-policy.v1",
  "subjects": {
    "work_authorization.right_to_work": {"answer_kind": "STRUCTURED", "max_reach": "ACCOUNT", "default_reach": "ACCOUNT", "context_keys": ["country"], "freshness_days": null, "submit_eligible": true, "sensitive": null},
    "work_authorization.sponsorship_required": {"answer_kind": "STRUCTURED", "max_reach": "ACCOUNT", "default_reach": "ACCOUNT", "context_keys": ["country"], "freshness_days": null, "submit_eligible": true, "sensitive": null},
    "licence.driving": {"answer_kind": "STRUCTURED", "max_reach": "ACCOUNT", "default_reach": "ACCOUNT", "context_keys": [], "freshness_days": null, "submit_eligible": true, "sensitive": null},
    "employment.notice_period": {"answer_kind": "STRUCTURED", "max_reach": "ACCOUNT", "default_reach": "ACCOUNT", "context_keys": [], "freshness_days": 60, "submit_eligible": true, "sensitive": null},
    "employment.availability_start": {"answer_kind": "STRUCTURED", "max_reach": "ACCOUNT", "default_reach": "ACCOUNT", "context_keys": [], "freshness_days": 30, "submit_eligible": true, "sensitive": null},
    "compensation.salary_expectation": {"answer_kind": "STRUCTURED", "max_reach": "SEARCH_WORKSPACE", "default_reach": "SEARCH_WORKSPACE", "context_keys": ["currency", "region", "employment_type"], "freshness_days": 60, "submit_eligible": true, "sensitive": null},
    "mobility.relocation": {"answer_kind": "STRUCTURED", "max_reach": "SEARCH_WORKSPACE", "default_reach": "SEARCH_WORKSPACE", "context_keys": ["region"], "freshness_days": 180, "submit_eligible": true, "sensitive": null},
    "mobility.travel_or_rotation": {"answer_kind": "STRUCTURED", "max_reach": "SEARCH_WORKSPACE", "default_reach": "SEARCH_WORKSPACE", "context_keys": ["region"], "freshness_days": 180, "submit_eligible": true, "sensitive": null},
    "motivation.role_type": {"answer_kind": "FREE_TEXT", "max_reach": "ACCOUNT", "default_reach": "SEARCH_WORKSPACE", "context_keys": [], "freshness_days": 180, "submit_eligible": true, "sensitive": null},
    "motivation.industry": {"answer_kind": "FREE_TEXT", "max_reach": "ACCOUNT", "default_reach": "SEARCH_WORKSPACE", "context_keys": [], "freshness_days": 180, "submit_eligible": true, "sensitive": null},
    "motivation.job_search_reason": {"answer_kind": "FREE_TEXT", "max_reach": "ACCOUNT", "default_reach": "SEARCH_WORKSPACE", "context_keys": [], "freshness_days": 180, "submit_eligible": true, "sensitive": null},
    "motivation.employer_specific": {"answer_kind": "FREE_TEXT", "max_reach": "EMPLOYER", "default_reach": "EMPLOYER", "context_keys": [], "freshness_days": 365, "submit_eligible": true, "sensitive": null},
    "legal.attestation": {"answer_kind": "STRUCTURED", "max_reach": "EMPLOYER", "default_reach": "EMPLOYER", "context_keys": [], "freshness_days": null, "submit_eligible": false, "sensitive": "legal_attestation"},
    "demographic.eeo": {"answer_kind": "STRUCTURED", "max_reach": "EMPLOYER", "default_reach": "EMPLOYER", "context_keys": [], "freshness_days": null, "submit_eligible": false, "sensitive": "demographic"},
    "background.criminal_record": {"answer_kind": "STRUCTURED", "max_reach": "EMPLOYER", "default_reach": "EMPLOYER", "context_keys": [], "freshness_days": null, "submit_eligible": false, "sensitive": "criminal_record"},
    "health.disability": {"answer_kind": "STRUCTURED", "max_reach": "EMPLOYER", "default_reach": "EMPLOYER", "context_keys": [], "freshness_days": null, "submit_eligible": false, "sensitive": "health"}
  }
}
```

- [ ] **Step 5: Implement `product/semantic_subject_policy.py`**

```python
"""Per-subject policy data for the autonomy contract (6B spec §6). Tuned by
editing semantic_subject_policy.v1.json, never the engine."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from product.autonomy_contract import REACH_ORDER, Reach, canonical_hash
from product.semantic_subject_registry import SEMANTIC_SUBJECTS

SUBJECT_POLICY_SCHEMA = "semantic-subject-policy"
SUBJECT_POLICY_SCHEMA_VERSION = "semantic-subject-policy.v1"
DEFAULT_PATH = Path(__file__).with_name("semantic_subject_policy.v1.json")
SENSITIVE_CLASSES = {"legal_attestation", "demographic", "criminal_record", "health"}
_ENTRY_KEYS = {"answer_kind", "max_reach", "default_reach", "context_keys", "freshness_days", "submit_eligible", "sensitive"}


class SubjectPolicyError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_subject_policy(doc: Any) -> None:
    errors: list[str] = []
    if not isinstance(doc, dict) or doc.get("schema_version") != SUBJECT_POLICY_SCHEMA_VERSION:
        raise SubjectPolicyError(["schema_version: must be semantic-subject-policy.v1"])
    subjects = doc.get("subjects")
    if not isinstance(subjects, dict):
        raise SubjectPolicyError(["subjects: must be an object"])
    for key in sorted(set(SEMANTIC_SUBJECTS) - set(subjects)):
        errors.append(f"subjects: missing registry subject {key!r}")
    for key in sorted(set(subjects) - set(SEMANTIC_SUBJECTS)):
        errors.append(f"subjects: unknown subject {key!r} (add it to the registry first)")
    for key, entry in subjects.items():
        path = f"subjects.{key}"
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            errors.append(f"{path}: keys must be exactly {sorted(_ENTRY_KEYS)}")
            continue
        if entry["answer_kind"] not in ("STRUCTURED", "FREE_TEXT"):
            errors.append(f"{path}.answer_kind: must be STRUCTURED or FREE_TEXT")
        reaches = {r.value for r in Reach}
        if entry["max_reach"] not in reaches or entry["default_reach"] not in reaches:
            errors.append(f"{path}: max_reach/default_reach must be one of {sorted(reaches)}")
        elif REACH_ORDER[Reach(entry["default_reach"])] > REACH_ORDER[Reach(entry["max_reach"])]:
            errors.append(f"{path}.default_reach: must not exceed max_reach")
        if not isinstance(entry["context_keys"], list) or not all(isinstance(k, str) for k in entry["context_keys"]):
            errors.append(f"{path}.context_keys: must be a list of strings")
        days = entry["freshness_days"]
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or days < 1):
            errors.append(f"{path}.freshness_days: must be null or a positive integer")
        if not isinstance(entry["submit_eligible"], bool):
            errors.append(f"{path}.submit_eligible: must be a boolean")
        if entry["sensitive"] is not None:
            if entry["sensitive"] not in SENSITIVE_CLASSES:
                errors.append(f"{path}.sensitive: must be null or one of {sorted(SENSITIVE_CLASSES)}")
            if entry["submit_eligible"] is not False:
                errors.append(f"{path}: sensitive subjects must have submit_eligible=false in v1")
    if errors:
        raise SubjectPolicyError(errors)


def load_subject_policy(path: Path = DEFAULT_PATH) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_subject_policy(doc)
    return doc


def subject_policy_hash(doc: Mapping[str, Any]) -> str:
    return canonical_hash(SUBJECT_POLICY_SCHEMA, SUBJECT_POLICY_SCHEMA_VERSION, doc)


def subject_entry(doc: Mapping[str, Any], subject: str | None) -> dict[str, Any] | None:
    if subject is None:
        return None
    return doc["subjects"].get(subject)
```

- [ ] **Step 6: Run tests (including the whole blocker suite, which uses the registry)**

Run: `.venv/Scripts/python -m pytest tests/product/test_semantic_subject_policy.py tests/product/test_semantic_subject_registry.py tests/webapp/services/test_application_blockers.py tests/webapp/services/test_decision_policy.py -q`
Expected: all PASS (the known `test_latest_valid_answer_governs` flake may fail intermittently — rerun once).

- [ ] **Step 7: Commit**

```bash
git add product/semantic_subject_registry.py product/semantic_subject_policy.py product/semantic_subject_policy.v1.json tests/product/test_semantic_subject_policy.py tests/product/test_semantic_subject_registry.py
git commit -m "feat(product): extend subject registry with autonomy subject policy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Representation transforms and fill-manifest schema

**Files:**
- Create: `product/representation_transforms.py`, `product/fill_manifest.py`
- Test: `tests/product/test_representation_transforms.py`, `tests/product/test_fill_manifest.py`

**Interfaces:**
- Consumes: `canonical_hash`.
- Produces: `TRANSFORM_IDS: frozenset[str]`, `TransformError`, `apply_transform(transform_id, value: str) -> str`; `FILL_MANIFEST_SCHEMA_VERSION = "fill-manifest.v1"`, `FillManifestError(errors)`, `validate_fill_manifest(doc) -> None`, `manifest_hash(doc) -> str`, `value_hash(value) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/product/test_representation_transforms.py
from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from product.representation_transforms import TRANSFORM_IDS, TransformError, apply_transform


def test_closed_set():
    assert TRANSFORM_IDS == frozenset({
        "identity", "whitespace_normalize", "country_name_to_iso2", "country_iso2_to_name",
        "date_iso_to_dmy", "date_iso_to_mdy", "phone_e164",
    })


@pytest.mark.parametrize("tid, value, expected", [
    ("identity", "Yes", "Yes"),
    ("whitespace_normalize", "  three   months ", "three months"),
    ("country_name_to_iso2", "United Kingdom", "GB"),
    ("country_name_to_iso2", "UK", "GB"),
    ("country_iso2_to_name", "GB", "United Kingdom"),
    ("country_name_to_iso2", "United Arab Emirates", "AE"),
    ("date_iso_to_dmy", "2026-10-01", "01/10/2026"),
    ("date_iso_to_mdy", "2026-10-01", "10/01/2026"),
    ("phone_e164", "+44 (0)7700 900-123", "+447700900123"),
    ("phone_e164", "+44 7700 900123", "+447700900123"),
])
def test_transforms(tid, value, expected):
    assert apply_transform(tid, value) == expected


@pytest.mark.parametrize("tid, value", [
    ("country_name_to_iso2", "Atlantis"),
    ("date_iso_to_dmy", "1 Oct 2026"),
    ("phone_e164", "07700 900123"),
    ("no_such_transform", "x"),
])
def test_fail_closed(tid, value):
    with pytest.raises(TransformError):
        apply_transform(tid, value)


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=30))
def test_whitespace_normalize_preserves_value(text):
    try:
        out = apply_transform("whitespace_normalize", text)
    except TransformError:
        return
    assert out.split() == text.split()
```

```python
# tests/product/test_fill_manifest.py
from __future__ import annotations

import copy

import pytest

from product.fill_manifest import FillManifestError, manifest_hash, validate_fill_manifest, value_hash


def _manifest():
    return {
        "schema_version": "fill-manifest.v1",
        "application_workspace_id": "ws_1",
        "adapter_id": "greenhouse", "adapter_version": "1.0.0",
        "pages": [{
            "page_key": "p1",
            "entries": [
                {"page_field_key": "email", "normalized_field_type": "email", "subject": None,
                 "source": {"kind": "EVIDENCE", "ref": "contact.email", "confirmation_id": None},
                 "transform_id": "identity", "value_hash": value_hash("a@b.c"), "required": True},
                {"page_field_key": "notice", "normalized_field_type": None, "subject": "employment.notice_period",
                 "source": {"kind": "APPROVED_ANSWER", "ref": "ans_1", "confirmation_id": "conf_1"},
                 "transform_id": "whitespace_normalize", "value_hash": value_hash("1 month"), "required": True},
            ],
        }],
    }


def test_valid_manifest():
    validate_fill_manifest(_manifest())


def test_hash_ignores_entry_order_within_page_but_not_page_order():
    a = _manifest()
    b = copy.deepcopy(a)
    b["pages"][0]["entries"].reverse()
    assert manifest_hash(a) == manifest_hash(b)
    c = copy.deepcopy(a)
    c["pages"].append({"page_key": "p0", "entries": []})
    d = copy.deepcopy(c)
    d["pages"].reverse()
    assert manifest_hash(c) != manifest_hash(d)


@pytest.mark.parametrize("mutate, message", [
    (lambda m: m["pages"][0]["entries"][1]["source"].update(confirmation_id=None), "confirmation_id"),
    (lambda m: m["pages"][0]["entries"][0].update(transform_id="llm_rewrite"), "transform_id"),
    (lambda m: m["pages"][0]["entries"][0].update(subject="licence.driving"), "exactly one"),
    (lambda m: m["pages"][0]["entries"].append(dict(m["pages"][0]["entries"][0])), "duplicate"),
    (lambda m: m["pages"][0]["entries"][0]["source"].update(kind="GENERATED"), "source.kind"),
    (lambda m: m["pages"][0]["entries"][1].update(subject="made.up"), "subject"),
])
def test_invalid_manifests(mutate, message):
    m = _manifest()
    mutate(m)
    with pytest.raises(FillManifestError) as exc:
        validate_fill_manifest(m)
    assert any(message in e for e in exc.value.errors), exc.value.errors
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/product/test_representation_transforms.py tests/product/test_fill_manifest.py -q`
Expected: FAIL — modules not found.

- [ ] **Step 3: Implement `product/representation_transforms.py`**

```python
"""Closed, deterministic format-only transforms (6B spec §7.2). Each transform
has a canonical form; apply_transform proves the underlying value is
unchanged by round-tripping through it, and fails closed otherwise."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

_COUNTRIES: dict[str, tuple[str, ...]] = {
    "GB": ("United Kingdom", "UK", "Great Britain", "Britain"),
    "US": ("United States", "USA", "United States of America"),
    "AE": ("United Arab Emirates", "UAE"),
    "SA": ("Saudi Arabia", "KSA"),
    "QA": ("Qatar",), "KW": ("Kuwait",), "OM": ("Oman",), "BH": ("Bahrain",),
    "NO": ("Norway",), "NL": ("Netherlands",), "DE": ("Germany",), "FR": ("France",),
    "IE": ("Ireland",), "CA": ("Canada",), "AU": ("Australia",), "NG": ("Nigeria",),
    "IN": ("India",),
}
_COUNTRY_LOOKUP = {code.casefold(): code for code in _COUNTRIES}
_COUNTRY_LOOKUP.update({name.casefold(): code for code, names in _COUNTRIES.items() for name in names})


class TransformError(ValueError):
    pass


def _country_code(value: str) -> str:
    code = _COUNTRY_LOOKUP.get(" ".join(value.split()).casefold())
    if code is None:
        raise TransformError(f"unknown country: {value!r}")
    return code


def _iso_date(value: str) -> date:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return date.fromisoformat(value.strip())
    raise TransformError(f"not an ISO date: {value!r}")


def _date_canonical_dmy(value: str) -> str:
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", value.strip())
    if m:
        return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
    return _iso_date(value).isoformat()


def _date_canonical_mdy(value: str) -> str:
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", value.strip())
    if m:
        return date(int(m[3]), int(m[1]), int(m[2])).isoformat()
    return _iso_date(value).isoformat()


def _phone_canonical(value: str) -> str:
    text = value.strip()
    if not text.startswith("+"):
        raise TransformError("phone must be in international format starting with '+'")
    text = text.replace("(0)", "")
    digits = re.sub(r"[\s\-().]", "", text[1:])
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        raise TransformError(f"not a valid international phone number: {value!r}")
    return "+" + digits


@dataclass(frozen=True)
class _Transform:
    apply: Callable[[str], str]
    canonical: Callable[[str], str]


_TRANSFORMS: dict[str, _Transform] = {
    "identity": _Transform(lambda v: v, lambda v: v),
    "whitespace_normalize": _Transform(lambda v: " ".join(v.split()), lambda v: " ".join(v.split())),
    "country_name_to_iso2": _Transform(_country_code, _country_code),
    "country_iso2_to_name": _Transform(lambda v: _COUNTRIES[_country_code(v)][0], _country_code),
    "date_iso_to_dmy": _Transform(lambda v: _iso_date(v).strftime("%d/%m/%Y"), _date_canonical_dmy),
    "date_iso_to_mdy": _Transform(lambda v: _iso_date(v).strftime("%m/%d/%Y"), _date_canonical_mdy),
    "phone_e164": _Transform(_phone_canonical, _phone_canonical),
}
TRANSFORM_IDS = frozenset(_TRANSFORMS)


def apply_transform(transform_id: str, value: str) -> str:
    transform = _TRANSFORMS.get(transform_id)
    if transform is None:
        raise TransformError(f"unknown transform: {transform_id!r}")
    if not isinstance(value, str):
        raise TransformError("transforms apply to strings only")
    result = transform.apply(value)
    if transform.canonical(result) != transform.canonical(value):
        raise TransformError(f"{transform_id} changed the underlying value")
    return result
```

- [ ] **Step 4: Implement `product/fill_manifest.py`**

```python
"""Fill-manifest schema (6B spec §8.3): exactly what Job Pipeline authorizes
an executor to place into an employer form. 6B defines schema, validation and
hashing; 6D builds manifests from live pages."""
from __future__ import annotations

from typing import Any, Mapping

from product.autonomy_contract import canonical_hash
from product.representation_transforms import TRANSFORM_IDS
from product.semantic_subject_registry import SEMANTIC_SUBJECTS

FILL_MANIFEST_SCHEMA = "fill-manifest"
FILL_MANIFEST_SCHEMA_VERSION = "fill-manifest.v1"
SOURCE_KINDS = {"EVIDENCE", "APPROVED_ANSWER", "PACK_DOCUMENT"}
_TOP = {"schema_version", "application_workspace_id", "adapter_id", "adapter_version", "pages"}
_ENTRY = {"page_field_key", "normalized_field_type", "subject", "source", "transform_id", "value_hash", "required"}


class FillManifestError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def value_hash(value: Any) -> str:
    return canonical_hash("fill-value", "v1", value)


def validate_fill_manifest(doc: Any) -> None:
    errors: list[str] = []
    if not isinstance(doc, dict) or set(doc) != _TOP:
        raise FillManifestError([f"manifest keys must be exactly {sorted(_TOP)}"])
    if doc["schema_version"] != FILL_MANIFEST_SCHEMA_VERSION:
        errors.append("schema_version: must be fill-manifest.v1")
    page_keys: set[str] = set()
    for p, page in enumerate(doc["pages"]):
        path = f"pages[{p}]"
        if not isinstance(page, dict) or set(page) != {"page_key", "entries"}:
            errors.append(f"{path}: keys must be page_key and entries")
            continue
        if page["page_key"] in page_keys:
            errors.append(f"{path}.page_key: duplicate page {page['page_key']!r}")
        page_keys.add(page["page_key"])
        field_keys: set[str] = set()
        for e, entry in enumerate(page["entries"]):
            epath = f"{path}.entries[{e}]"
            if not isinstance(entry, dict) or set(entry) != _ENTRY:
                errors.append(f"{epath}: keys must be exactly {sorted(_ENTRY)}")
                continue
            if entry["page_field_key"] in field_keys:
                errors.append(f"{epath}.page_field_key: duplicate field {entry['page_field_key']!r}")
            field_keys.add(entry["page_field_key"])
            if (entry["normalized_field_type"] is None) == (entry["subject"] is None):
                errors.append(f"{epath}: exactly one of normalized_field_type or subject is required")
            if entry["subject"] is not None and entry["subject"] not in SEMANTIC_SUBJECTS:
                errors.append(f"{epath}.subject: unknown subject {entry['subject']!r}")
            source = entry["source"]
            if not isinstance(source, dict) or set(source) != {"kind", "ref", "confirmation_id"}:
                errors.append(f"{epath}.source: keys must be kind, ref, confirmation_id")
            else:
                if source["kind"] not in SOURCE_KINDS:
                    errors.append(f"{epath}.source.kind: must be one of {sorted(SOURCE_KINDS)}")
                if source["kind"] == "APPROVED_ANSWER" and not source["confirmation_id"]:
                    errors.append(f"{epath}.source.confirmation_id: required for APPROVED_ANSWER")
            if entry["transform_id"] not in TRANSFORM_IDS:
                errors.append(f"{epath}.transform_id: unknown transform {entry['transform_id']!r}")
            if not isinstance(entry["value_hash"], str) or not entry["value_hash"].startswith("sha256:"):
                errors.append(f"{epath}.value_hash: must be a sha256 hash")
            if not isinstance(entry["required"], bool):
                errors.append(f"{epath}.required: must be a boolean")
    if errors:
        raise FillManifestError(errors)


def manifest_hash(doc: Mapping[str, Any]) -> str:
    validate_fill_manifest(doc)
    normalized = dict(doc)
    normalized["pages"] = [
        {"page_key": page["page_key"],
         "entries": sorted(page["entries"], key=lambda entry: entry["page_field_key"])}
        for page in doc["pages"]
    ]
    return canonical_hash(FILL_MANIFEST_SCHEMA, FILL_MANIFEST_SCHEMA_VERSION, normalized)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/product/test_representation_transforms.py tests/product/test_fill_manifest.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add product/representation_transforms.py product/fill_manifest.py tests/product/test_representation_transforms.py tests/product/test_fill_manifest.py
git commit -m "feat(product): add format-only transforms and fill-manifest schema

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 5: Authorization gate — authority, reductions, stops, precedence

**Files:**
- Create: `product/autonomy_gate.py`
- Create: `tests/product/autonomy_fixtures.py` (shared context builder, used by Tasks 5–7)
- Test: `tests/product/test_autonomy_gate.py`

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: `evaluate_authorization(ctx: AuthorizationContext) -> AuthorizationDecision`. Reason codes produced (stable strings): `ceiling`, `non_live_mode`, `standing_policy_missing`, `rule_reduce`, `rule_block`, `rule_require_user`, `rule_acknowledged_proceed`, `rule_acknowledged_do_not_proceed`, `rule_acknowledgement_lapsed`, `identity_weak`, `identity_conflict`, `no_apply_target`, `apply_target_tier`, `adapter_not_submit_capable`, `target_mismatch`, `target_unverified`, `employer_key_unknown`, `pack_not_auto_confirmable`, `kill_switch`, `sentinel_present`, `stale_binding`, `governing_auto_reject`, `duplicate_intent`, `duplicate_overridden`, `limit_reached`, `budget_exceeded`, `governing_blocker`, `hard_stop`, `require_user`, `unresolved_silent`, `answer_not_submit_ready`, `optional_omitted`, `invalid_input`. RequireUserItem kinds: `rule`, `apply_target`, `governing_blocker`, `hard_stop`, `unclassified_field`, `sensitive_field`, `contradicted_answer`, `missing_answer`.

- [ ] **Step 1: Create the shared fixture module**

```python
# tests/product/autonomy_fixtures.py
"""Shared AuthorizationContext builder for gate tests. The default context is
fully permitted: ALLOW(SUBMIT), grantable."""
from __future__ import annotations

from datetime import datetime, timezone

from product.autonomy_contract import (
    AnswerCandidate, ApplyTargetFacts, AuthorizationContext, Capability,
    EmployerKeyStrength, IdentityStrength, Mode, ProvenanceTier, Reach,
)
from product.semantic_subject_policy import load_subject_policy
from product.standing_policy import default_policy_document

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
SUBJECT_POLICY = load_subject_policy()


def make_policy(*rules, lists=None):
    doc = default_policy_document("Europe/London")
    doc["rules"] = list(rules)
    if lists:
        doc["employer_lists"] = lists
    return doc


def good_target(**overrides) -> ApplyTargetFacts:
    values = dict(
        provenance=ProvenanceTier.DISCOVERY_VERIFIED, adapter_id="greenhouse",
        adapter_submit_capable=True, landing_within_redirect_set=True,
        tenant_matches_employer=True, ats_job_id_matches=True, unexplained_redirect=False,
    )
    values.update(overrides)
    return ApplyTargetFacts(**values)


def answer(subject, *, answer_id="ans_1", reach=Reach.ACCOUNT, scope_id=None, context=None,
           confirmed_at=NOW, basis_kind="USER_ASSERTION", basis_at=None, basis_now=None,
           contradicted=False) -> AnswerCandidate:
    return AnswerCandidate(
        approved_answer_id=answer_id, subject=subject, reach=reach, scope_id=scope_id,
        context=context or {}, confirmed_at=confirmed_at, basis_kind=basis_kind,
        basis_hash_at_approval=basis_at, basis_hash_current=basis_now, contradicted=contradicted,
    )


def make_ctx(**overrides) -> AuthorizationContext:
    values = dict(
        mode=Mode.LIVE, requested_stage=Capability.SUBMIT, now=NOW,
        account_id="acct_1", application_workspace_id="ws_1", search_workspace_id="sw_1",
        deployment_ceiling=Capability.SUBMIT, account_max=Capability.SUBMIT,
        workspace_ceiling=Capability.SUBMIT, kill_switch_engaged=False, sentinel_present=False,
        standing_policy=make_policy(), subject_policy=SUBJECT_POLICY,
        attributes={"fit.overall_score": 80, "job.employment_type": "PERMANENT",
                    "company.key": "name:acme", "workspace.id": "sw_1"},
        governing_auto_reject=False, unresolved_governing_require_user=(),
        pack_artifact_id="art_pack", pack_auto_confirmable=True, requirements=(),
        apply_target=good_target(), identity_key="source:greenhouse:123",
        identity_strength=IdentityStrength.SOURCE_RECORD, identity_conflict=False,
        existing_intent_state=None, intent_overridden=False,
        employer_key="name:acme", employer_key_strength=EmployerKeyStrength.ATS_TENANT,
        counters=(), budgets=(), rule_acknowledgements=(),
    )
    values.update(overrides)
    return AuthorizationContext(**values)
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/product/test_autonomy_gate.py
from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from product.autonomy_contract import (
    BudgetState, Capability, CounterState, EmployerKeyStrength, IdentityStrength, Mode,
    ProvenanceTier, RequireUserItem, ResultKind, RuleAcknowledgement, UNKNOWN,
)
from product.autonomy_gate import evaluate_authorization
from product.standing_policy import evaluate_rules
from product.autonomy_contract import RepresentationRequirement
from tests.product.autonomy_fixtures import NOW, answer, good_target, make_ctx, make_policy

C = Capability
R = ResultKind

MIN_FIT = {"id": "min-fit", "description": "", "when": {"attr": "fit.overall_score", "op": "lt", "value": 75},
           "effect": {"type": "REDUCE_TO", "level": "PREPARE"}, "on_unknown": {"type": "REDUCE_TO", "level": "PREPARE"}}
PERMANENT_ONLY = {"id": "perm", "description": "", "when": {"attr": "job.employment_type", "op": "ne", "value": "PERMANENT"},
                  "effect": {"type": "REQUIRE_USER"}, "on_unknown": {"type": "REQUIRE_USER"}}
DENY = {"id": "deny", "description": "", "when": {"attr": "company.key", "op": "in_list", "value": "deny"},
        "effect": {"type": "BLOCK"}, "on_unknown": {"type": "REQUIRE_USER"}}


def codes(decision):
    return {r.code for r in decision.reasons}


def test_fully_permitted_context_allows_submit():
    d = evaluate_authorization(make_ctx())
    assert (d.result, d.effective_capability, d.grantable, d.deny_reason) == (R.ALLOW, C.SUBMIT, True, None)
    assert "ceiling" in codes(d)
    assert d.input_fingerprint.startswith("sha256:") and d.policy_version_hash and d.subject_policy_hash


@pytest.mark.parametrize("field", ["deployment_ceiling", "account_max", "workspace_ceiling"])
@pytest.mark.parametrize("level", [C.NONE, C.PREPARE, C.FILL])
def test_ceiling_is_min_of_three(field, level):
    d = evaluate_authorization(make_ctx(**{field: level}))
    assert d.effective_capability == level and d.result is R.ALLOW and not d.grantable


def test_no_authorization_means_none():
    d = evaluate_authorization(make_ctx(account_max=C.NONE, requested_stage=C.PREPARE))
    assert d.effective_capability == C.NONE and not d.grantable


def test_missing_standing_policy_means_none():
    d = evaluate_authorization(make_ctx(standing_policy=None, requested_stage=C.PREPARE))
    assert d.effective_capability == C.NONE and "standing_policy_missing" in codes(d)


@pytest.mark.parametrize("mode", [Mode.SHADOW, Mode.DRY_RUN])
def test_non_live_modes_never_grantable(mode):
    d = evaluate_authorization(make_ctx(mode=mode))
    assert d.result is R.ALLOW and d.effective_capability == C.SUBMIT and not d.grantable
    assert "non_live_mode" in codes(d)


def test_rule_reduce_on_match_and_on_unknown():
    d = evaluate_authorization(make_ctx(standing_policy=make_policy(MIN_FIT), attributes={"fit.overall_score": 60}))
    assert d.effective_capability == C.PREPARE and not d.grantable
    d = evaluate_authorization(make_ctx(standing_policy=make_policy(MIN_FIT), attributes={}))
    assert d.effective_capability == C.PREPARE
    assert any(r.code == "rule_reduce" and ("via", "unknown") in r.params for r in d.reasons)


def test_rule_block_and_require_user():
    policy = make_policy(DENY, lists={"deny": ["name:acme"]})
    assert evaluate_authorization(make_ctx(standing_policy=policy)).result is R.BLOCK
    d = evaluate_authorization(make_ctx(standing_policy=make_policy(PERMANENT_ONLY),
                                        attributes={"job.employment_type": "CONTRACT"}))
    assert d.result is R.REQUIRE_USER and RequireUserItem("rule", "perm") in d.require_user_items


def test_deny_list_unknown_company_requires_user_not_permission():
    policy = make_policy(DENY, lists={"deny": ["name:acme"]})
    d = evaluate_authorization(make_ctx(standing_policy=policy, attributes={"company.key": UNKNOWN}))
    assert d.result is R.REQUIRE_USER


def _ack(policy, attributes, disposition="PROCEED", rule_id="perm"):
    outcome = next(o for o in evaluate_rules(policy, attributes) if o.rule_id == rule_id)
    return RuleAcknowledgement(rule_id, outcome.rule_hash, outcome.observed_fingerprint, disposition)


def test_rule_acknowledgement_proceed_lapse_and_do_not_proceed():
    attrs = {"job.employment_type": "CONTRACT", "fit.overall_score": 80}
    policy = make_policy(PERMANENT_ONLY, MIN_FIT)
    ack = _ack(policy, attrs)
    d = evaluate_authorization(make_ctx(standing_policy=policy, attributes=attrs, rule_acknowledgements=(ack,)))
    assert d.result is R.ALLOW and "rule_acknowledged_proceed" in codes(d)
    # Unrelated rule edited: acknowledgement still valid.
    edited = make_policy(PERMANENT_ONLY, {**MIN_FIT, "when": {**MIN_FIT["when"], "value": 70}})
    d = evaluate_authorization(make_ctx(standing_policy=edited, attributes=attrs, rule_acknowledgements=(ack,)))
    assert d.result is R.ALLOW
    # Acknowledged rule edited: lapses.
    changed_rule = make_policy({**PERMANENT_ONLY, "when": {**PERMANENT_ONLY["when"], "value": "CONTRACT"}}, MIN_FIT)
    d = evaluate_authorization(make_ctx(standing_policy=changed_rule, attributes={**attrs, "job.employment_type": "TEMPORARY"},
                                        rule_acknowledgements=(ack,)))
    assert d.result is R.REQUIRE_USER and "rule_acknowledgement_lapsed" in codes(d)
    # Observed attributes changed: lapses.
    d = evaluate_authorization(make_ctx(standing_policy=policy, attributes={**attrs, "job.employment_type": "TEMPORARY"},
                                        rule_acknowledgements=(ack,)))
    assert d.result is R.REQUIRE_USER
    # DO_NOT_PROCEED blocks.
    no = _ack(policy, attrs, disposition="DO_NOT_PROCEED")
    assert evaluate_authorization(make_ctx(standing_policy=policy, attributes=attrs, rule_acknowledgements=(no,))).result is R.BLOCK


@pytest.mark.parametrize("overrides, code", [
    (dict(identity_strength=IdentityStrength.WEAK), "identity_weak"),
    (dict(identity_key=None), "identity_weak"),
    (dict(identity_conflict=True), "identity_conflict"),
    (dict(employer_key_strength=EmployerKeyStrength.UNKNOWN), "employer_key_unknown"),
    (dict(apply_target=good_target(provenance=ProvenanceTier.USER_SUPPLIED)), "apply_target_tier"),
    (dict(apply_target=good_target(provenance=ProvenanceTier.IMPORTED_SOURCE)), "apply_target_tier"),
    (dict(apply_target=good_target(adapter_submit_capable=False)), "adapter_not_submit_capable"),
    (dict(apply_target=good_target(landing_within_redirect_set=UNKNOWN)), "target_unverified"),
    (dict(apply_target=good_target(unexplained_redirect=UNKNOWN)), "target_unverified"),
])
def test_reductions_cap_at_fill(overrides, code):
    d = evaluate_authorization(make_ctx(**overrides))
    assert d.effective_capability == C.FILL and code in codes(d) and not d.grantable


def test_user_confirmed_target_can_submit_and_ats_job_id_optional():
    d = evaluate_authorization(make_ctx(apply_target=good_target(
        provenance=ProvenanceTier.USER_CONFIRMED_APPLY_TARGET, ats_job_id_matches=None)))
    assert d.grantable


def test_target_mismatch_requires_user():
    d = evaluate_authorization(make_ctx(apply_target=good_target(tenant_matches_employer=False)))
    assert d.result is R.REQUIRE_USER and RequireUserItem("apply_target", "tenant_matches_employer") in d.require_user_items


def test_no_target_and_unconfirmable_pack_cap_at_prepare():
    assert evaluate_authorization(make_ctx(apply_target=good_target(provenance=None))).effective_capability == C.PREPARE
    assert evaluate_authorization(make_ctx(pack_auto_confirmable=False)).effective_capability == C.PREPARE


@pytest.mark.parametrize("stage", [C.PREPARE, C.FILL, C.SUBMIT])
def test_kill_switch_and_sentinel_deny_every_stage(stage):
    for overrides in (dict(kill_switch_engaged=True), dict(sentinel_present=True)):
        d = evaluate_authorization(make_ctx(requested_stage=stage, **overrides))
        assert (d.result, d.deny_reason, d.grantable) == (R.DENY, "kill_switch", False)


def test_stale_binding_denies():
    d = evaluate_authorization(make_ctx(grant_binding_drift=("fill_manifest_hash",)))
    assert (d.result, d.deny_reason) == (R.DENY, "stale_binding")


def test_auto_reject_blocks_and_duplicates_deny():
    assert evaluate_authorization(make_ctx(governing_auto_reject=True)).result is R.BLOCK
    for state in ("CLAIMED", "CONFIRMED"):
        d = evaluate_authorization(make_ctx(existing_intent_state=state, requested_stage=C.PREPARE))
        assert (d.result, d.deny_reason) == (R.DENY, "duplicate")
    d = evaluate_authorization(make_ctx(existing_intent_state="CONFIRMED", intent_overridden=True))
    assert d.grantable and "duplicate_overridden" in codes(d)


def test_limits_and_budgets_are_temporary_with_retry_at():
    retry = NOW + timedelta(hours=12)
    d = evaluate_authorization(make_ctx(counters=(CounterState("submit_per_day", C.SUBMIT, 3, 3, retry),)))
    assert (d.result, d.deny_reason, d.retry_at) == (R.DENY_TEMPORARY, "limit", retry)
    # A counter for another stage is irrelevant.
    assert evaluate_authorization(make_ctx(counters=(CounterState("fill_per_day", C.FILL, 10, 10, retry),))).grantable
    d = evaluate_authorization(make_ctx(budgets=(BudgetState("LLM", "day", Decimal("4.5"), Decimal("0.4"), Decimal("5"), Decimal("0.2"), None),)))
    assert (d.result, d.deny_reason, d.retry_at) == (R.DENY_TEMPORARY, "budget", None)


def test_governing_blockers_and_hard_stops():
    d = evaluate_authorization(make_ctx(unresolved_governing_require_user=("blk_1",), requested_stage=C.PREPARE))
    assert d.result is R.REQUIRE_USER and RequireUserItem("governing_blocker", "blk_1") in d.require_user_items
    d = evaluate_authorization(make_ctx(executor_hard_stops=("captcha",), requested_stage=C.FILL))
    assert RequireUserItem("hard_stop", "captcha") in d.require_user_items
    d = evaluate_authorization(make_ctx(executor_hard_stops=("captcha",), requested_stage=C.PREPARE))
    assert d.result is R.ALLOW


FLAGS = {
    "kill": dict(kill_switch_engaged=True),
    "block": dict(governing_auto_reject=True),
    "duplicate": dict(existing_intent_state="CONFIRMED"),
    "limit": dict(counters=(CounterState("submit_per_day", C.SUBMIT, 3, 3, None),)),
    "require_user": dict(unresolved_governing_require_user=("blk_1",)),
}
EXPECTED = [("kill", R.DENY), ("block", R.BLOCK), ("duplicate", R.DENY), ("limit", R.DENY_TEMPORARY), ("require_user", R.REQUIRE_USER)]


@pytest.mark.parametrize("combo", [c for n in range(1, 6) for c in itertools.combinations(FLAGS, n)])
def test_precedence_and_all_reasons_retained(combo):
    overrides = {}
    for name in combo:
        overrides.update(FLAGS[name])
    d = evaluate_authorization(make_ctx(**overrides))
    expected = next(result for name, result in EXPECTED if name in combo)
    assert d.result is expected
    assert len(d.reasons) >= len(combo) + 1  # every condition leaves a reason, plus the ceiling


@pytest.mark.parametrize("overrides", [
    dict(now=datetime(2026, 9, 24, 12, 0)),
    dict(requested_stage=C.NONE),
    dict(standing_policy={"schema_version": "nope"}),
    dict(subject_policy={"schema_version": "nope"}),
    dict(attributes={"fit.overall_score": 74.5}),
    dict(requirements=(RepresentationRequirement(
        key="notice", subject="employment.notice_period", required=True, evidence_available=False,
        candidates=(answer("employment.notice_period", confirmed_at=datetime(2026, 9, 1)),)),)),
])
def test_invalid_input_fails_closed(overrides):
    d = evaluate_authorization(make_ctx(**overrides))
    assert (d.result, d.deny_reason, d.effective_capability, d.grantable) == (R.DENY, "invalid_input", C.NONE, False)


def test_deterministic_and_fingerprint_tracks_inputs():
    a, b = evaluate_authorization(make_ctx()), evaluate_authorization(make_ctx())
    assert a == b
    assert evaluate_authorization(make_ctx(run_id="run_2")).input_fingerprint != a.input_fingerprint
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/product/test_autonomy_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'product.autonomy_gate'`

- [ ] **Step 4: Implement `product/autonomy_gate.py`**

```python
"""The Bundle 6B authorization gate (spec §9).

evaluate_authorization is pure and deterministic: no I/O, no clock (ctx.now
is an input), no mutation, no limits consumed, no grants created. Every check
runs and leaves reasons (collect-then-resolve); the result is derived by the
fixed precedence of spec §9.4. Only the three ceilings can raise capability;
everything else applies min() or a non-ALLOW outcome.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from product.autonomy_contract import (
    CONTEXT_SCHEMA, CONTEXT_SCHEMA_VERSION, ENGINE_VERSION, REACH_ORDER,
    AnswerCandidate, AuthorizationContext, AuthorizationDecision, CanonicalHashError,
    Capability, EmployerKeyStrength, IdentityStrength, Mode, ProvenanceTier, Reach,
    Reason, RepresentationRequirement, RequireUserItem, ResultKind, canonical_hash,
    is_unknown, reason,
)
from product.semantic_subject_policy import (
    SubjectPolicyError, subject_entry, subject_policy_hash, validate_subject_policy,
)
from product.standing_policy import (
    StandingPolicyError, evaluate_rules, policy_hash, validate_standing_policy,
)

_SUBMIT_TIERS = {ProvenanceTier.DISCOVERY_VERIFIED, ProvenanceTier.USER_CONFIRMED_APPLY_TARGET}


def _aware(value: Any) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


class _Acc:
    def __init__(self, ceiling: Capability) -> None:
        self.cap = ceiling
        self.reasons: list[Reason] = []
        self.items: list[RequireUserItem] = []
        self.denies: set[str] = set()
        self.temporary: list[tuple[str, datetime | None]] = []
        self.blocked = False

    def reduce(self, level: Capability, code: str, **params: Any) -> None:
        self.reasons.append(reason(code, to=level.name, **params))
        self.cap = min(self.cap, level)

    def note(self, code: str, **params: Any) -> None:
        self.reasons.append(reason(code, **params))


def _context_errors(ctx: AuthorizationContext) -> list[str]:
    errors: list[str] = []
    if not _aware(ctx.now):
        errors.append("now_naive")
    if ctx.requested_stage == Capability.NONE:
        errors.append("requested_stage_none")
    for req in ctx.requirements:
        for cand in req.candidates:
            if not _aware(cand.confirmed_at):
                errors.append(f"confirmed_at_naive:{cand.approved_answer_id}")
    for item in (*ctx.counters, *ctx.budgets):
        if item.retry_at is not None and not _aware(item.retry_at):
            errors.append("retry_at_naive")
    try:
        validate_subject_policy(ctx.subject_policy)
    except SubjectPolicyError:
        errors.append("subject_policy_invalid")
    if ctx.standing_policy is not None:
        try:
            validate_standing_policy(ctx.standing_policy)
        except StandingPolicyError:
            errors.append("standing_policy_invalid")
    return errors


def evaluate_authorization(ctx: AuthorizationContext) -> AuthorizationDecision:
    errors = _context_errors(ctx)
    try:
        fingerprint = canonical_hash(CONTEXT_SCHEMA, CONTEXT_SCHEMA_VERSION, ctx)
    except CanonicalHashError:
        errors.append("unhashable_context")
        fingerprint = canonical_hash(CONTEXT_SCHEMA, "invalid", {"errors": sorted(errors)})
    subject_hash = None if "subject_policy_invalid" in errors else subject_policy_hash(ctx.subject_policy)
    policy_version_hash = None
    if ctx.standing_policy is not None and "standing_policy_invalid" not in errors:
        policy_version_hash = policy_hash(ctx.standing_policy)
    if errors:
        return AuthorizationDecision(
            mode=ctx.mode, result=ResultKind.DENY, requested_stage=ctx.requested_stage,
            effective_capability=Capability.NONE, grantable=False, deny_reason="invalid_input",
            reasons=tuple(sorted(reason("invalid_input", detail=e) for e in errors)),
            require_user_items=(), retry_at=None, input_fingerprint=fingerprint,
            engine_version=ENGINE_VERSION, policy_version_hash=policy_version_hash,
            subject_policy_hash=subject_hash,
        )

    acc = _Acc(min(ctx.deployment_ceiling, ctx.account_max, ctx.workspace_ceiling))
    acc.note("ceiling", deployment=ctx.deployment_ceiling.name,
             account=ctx.account_max.name, workspace=ctx.workspace_ceiling.name)
    if ctx.mode is not Mode.LIVE:
        acc.note("non_live_mode", mode=ctx.mode.value)
    _apply_standing_policy(ctx, acc)
    _apply_identity(ctx, acc)
    _apply_target(ctx, acc)
    if ctx.employer_key_strength is EmployerKeyStrength.UNKNOWN or not ctx.employer_key:
        acc.reduce(Capability.FILL, "employer_key_unknown")
    if not ctx.pack_auto_confirmable:
        acc.reduce(Capability.PREPARE, "pack_not_auto_confirmable")
    _apply_requirements(ctx, acc)
    _apply_stops(ctx, acc)
    _apply_questions(ctx, acc)
    return _resolve(ctx, acc, fingerprint, policy_version_hash, subject_hash)


def _apply_standing_policy(ctx: AuthorizationContext, acc: _Acc) -> None:
    if ctx.standing_policy is None:
        acc.reduce(Capability.NONE, "standing_policy_missing")
        return
    acks = {a.rule_id: a for a in ctx.rule_acknowledgements}
    for outcome in evaluate_rules(ctx.standing_policy, ctx.attributes):
        effect = outcome.applied_effect
        if effect is None:
            continue
        via = "unknown" if outcome.via_unknown else "match"
        if effect["type"] == "REDUCE_TO":
            acc.reduce(Capability[effect["level"]], "rule_reduce", rule=outcome.rule_id, via=via)
        elif effect["type"] == "BLOCK":
            acc.blocked = True
            acc.note("rule_block", rule=outcome.rule_id, via=via)
        else:
            ack = acks.get(outcome.rule_id)
            valid = (ack is not None and ack.rule_hash == outcome.rule_hash
                     and ack.observed_fingerprint == outcome.observed_fingerprint)
            if valid and ack.disposition == "DO_NOT_PROCEED":
                acc.blocked = True
                acc.note("rule_acknowledged_do_not_proceed", rule=outcome.rule_id)
            elif valid:
                acc.note("rule_acknowledged_proceed", rule=outcome.rule_id)
            else:
                if ack is not None:
                    acc.note("rule_acknowledgement_lapsed", rule=outcome.rule_id)
                acc.items.append(RequireUserItem("rule", outcome.rule_id))
                acc.note("rule_require_user", rule=outcome.rule_id, via=via)


def _apply_identity(ctx: AuthorizationContext, acc: _Acc) -> None:
    if ctx.identity_key is None or ctx.identity_strength is IdentityStrength.WEAK:
        acc.reduce(Capability.FILL, "identity_weak")
    if ctx.identity_conflict:
        acc.reduce(Capability.FILL, "identity_conflict")


def _negate(value: Any) -> Any:
    return value if is_unknown(value) or not isinstance(value, bool) else not value


def _apply_target(ctx: AuthorizationContext, acc: _Acc) -> None:
    target = ctx.apply_target
    if target.provenance is None:
        acc.reduce(Capability.PREPARE, "no_apply_target")
        return
    if target.provenance not in _SUBMIT_TIERS:
        acc.reduce(Capability.FILL, "apply_target_tier", tier=target.provenance.value)
        return
    if not target.adapter_submit_capable:
        acc.reduce(Capability.FILL, "adapter_not_submit_capable", adapter=target.adapter_id or "none")
        return
    checks = {
        "landing_within_redirect_set": target.landing_within_redirect_set,
        "tenant_matches_employer": target.tenant_matches_employer,
        "no_unexplained_redirect": _negate(target.unexplained_redirect),
    }
    if target.ats_job_id_matches is not None:
        checks["ats_job_id_matches"] = target.ats_job_id_matches
    for name, value in sorted(checks.items()):
        if value is False:
            acc.reduce(Capability.FILL, "target_mismatch", check=name)
            acc.items.append(RequireUserItem("apply_target", name))
        elif value is not True:
            acc.reduce(Capability.FILL, "target_unverified", check=name)


def _in_reach(cand: AnswerCandidate, entry: dict, ctx: AuthorizationContext) -> bool:
    if REACH_ORDER[cand.reach] > REACH_ORDER[Reach(entry["max_reach"])]:
        return False
    if cand.reach is Reach.ACCOUNT:
        return True
    if cand.reach is Reach.SEARCH_WORKSPACE:
        return ctx.search_workspace_id is not None and cand.scope_id == ctx.search_workspace_id
    return (ctx.employer_key_strength is not EmployerKeyStrength.UNKNOWN
            and ctx.employer_key is not None and cand.scope_id == ctx.employer_key)


def _job_value(req: RepresentationRequirement, key: str) -> Any:
    value = req.job_context.get(key)
    return None if value is None or is_unknown(value) else value


def _context_known_different(cand: AnswerCandidate, req: RepresentationRequirement, entry: dict) -> bool:
    for key in entry["context_keys"]:
        mine, job = cand.context.get(key), _job_value(req, key)
        if mine is not None and job is not None and mine != job:
            return True
    return False


def _submit_blocker(cand: AnswerCandidate, req: RepresentationRequirement, entry: dict, now: datetime) -> str | None:
    if not entry["submit_eligible"]:
        return "not_submit_eligible"
    days = entry["freshness_days"]
    if days is not None and now - cand.confirmed_at > timedelta(days=days):
        return "expired"
    if cand.basis_kind != "USER_ASSERTION" and (
        cand.basis_hash_current is None or cand.basis_hash_current != cand.basis_hash_at_approval
    ):
        return "basis_changed"
    for key in entry["context_keys"]:
        if cand.context.get(key) is None or _job_value(req, key) is None:
            return "context_unknown"
    return None


def _apply_requirements(ctx: AuthorizationContext, acc: _Acc) -> None:
    """Field/question items (spec §7, §9.3 step 5). Only for FILL/SUBMIT.
    Contradictions are raised at FILL and SUBMIT; other field items only at
    SUBMIT; any item is raised only if resolving it could reach the
    requested stage (relevance) -- otherwise it is a silent reason."""
    if ctx.requested_stage < Capability.FILL:
        return
    relevant = acc.cap >= ctx.requested_stage
    items: list[tuple[RequireUserItem, bool]] = []
    reductions: list[tuple[str, str]] = []
    for req in sorted(ctx.requirements, key=lambda r: r.key):
        entry = subject_entry(ctx.subject_policy, req.subject)
        if entry is None:
            if req.subject is None and req.evidence_available:
                continue
            if req.required:
                items.append((RequireUserItem("unclassified_field", req.key), False))
            else:
                acc.note("optional_omitted", field=req.key, why="unclassified")
            continue
        if entry["sensitive"] is not None:
            if req.required:
                items.append((RequireUserItem("sensitive_field", req.key), False))
            else:
                acc.note("optional_omitted", field=req.key, why="sensitive")
            continue
        if req.evidence_available:
            continue
        in_reach = [c for c in req.candidates if c.subject == req.subject and _in_reach(c, entry, ctx)]
        if any(c.contradicted for c in in_reach):
            items.append((RequireUserItem("contradicted_answer", req.key), True))
            continue
        usable = [c for c in in_reach if not _context_known_different(c, req, entry)]
        blockers = [_submit_blocker(c, req, entry, ctx.now) for c in usable]
        if any(b is None for b in blockers):
            continue
        if usable:
            reductions.append((req.key, blockers[0]))
            continue
        if req.required:
            items.append((RequireUserItem("missing_answer", req.key), False))
        else:
            acc.note("optional_omitted", field=req.key, why="no_answer")
    for item, raise_at_fill in items:
        applies = ctx.requested_stage == Capability.SUBMIT or raise_at_fill
        if applies and relevant:
            acc.items.append(item)
            acc.note("require_user", kind=item.kind, ref=item.ref)
        else:
            acc.note("unresolved_silent", kind=item.kind, ref=item.ref)
    for field, why in reductions:
        acc.reduce(Capability.FILL, "answer_not_submit_ready", field=field, why=why)


def _apply_stops(ctx: AuthorizationContext, acc: _Acc) -> None:
    if ctx.kill_switch_engaged:
        acc.denies.add("kill_switch")
        acc.note("kill_switch")
    if ctx.sentinel_present:
        acc.denies.add("kill_switch")
        acc.note("sentinel_present")
    for field in sorted(ctx.grant_binding_drift):
        acc.denies.add("stale_binding")
        acc.note("stale_binding", field=field)
    if ctx.governing_auto_reject:
        acc.blocked = True
        acc.note("governing_auto_reject")
    if ctx.existing_intent_state in ("CLAIMED", "CONFIRMED"):
        if ctx.intent_overridden:
            acc.note("duplicate_overridden", state=ctx.existing_intent_state)
        else:
            acc.denies.add("duplicate")
            acc.note("duplicate_intent", state=ctx.existing_intent_state)
    for counter in ctx.counters:
        if counter.stage == ctx.requested_stage and counter.used >= counter.limit:
            acc.temporary.append(("limit", counter.retry_at))
            acc.note("limit_reached", name=counter.name, used=counter.used, limit=counter.limit)
    for budget in ctx.budgets:
        if budget.used + budget.reserved + budget.estimate > budget.cap:
            acc.temporary.append(("budget", budget.retry_at))
            acc.note("budget_exceeded", category=budget.category, window=budget.window)


def _apply_questions(ctx: AuthorizationContext, acc: _Acc) -> None:
    for blocker_id in sorted(ctx.unresolved_governing_require_user):
        acc.items.append(RequireUserItem("governing_blocker", blocker_id))
        acc.note("governing_blocker", blocker=blocker_id)
    if ctx.requested_stage >= Capability.FILL:
        for stop in sorted(set(ctx.executor_hard_stops)):
            acc.items.append(RequireUserItem("hard_stop", stop))
            acc.note("hard_stop", stop=stop)


def _resolve(ctx: AuthorizationContext, acc: _Acc, fingerprint: str,
             policy_version_hash: str | None, subject_hash: str | None) -> AuthorizationDecision:
    items = tuple(sorted(set(acc.items)))
    retry_at = None
    deny_reason = None
    if acc.denies & {"kill_switch", "stale_binding"}:
        result = ResultKind.DENY
        deny_reason = "kill_switch" if "kill_switch" in acc.denies else "stale_binding"
    elif acc.blocked:
        result = ResultKind.BLOCK
    elif "duplicate" in acc.denies:
        result, deny_reason = ResultKind.DENY, "duplicate"
    elif acc.temporary:
        result = ResultKind.DENY_TEMPORARY
        deny_reason = "limit" if any(kind == "limit" for kind, _ in acc.temporary) else "budget"
        times = [t for _, t in acc.temporary]
        retry_at = max(times) if all(t is not None for t in times) else None
    elif items:
        result = ResultKind.REQUIRE_USER
    else:
        result = ResultKind.ALLOW
    grantable = (result is ResultKind.ALLOW and acc.cap >= ctx.requested_stage
                 and ctx.mode is Mode.LIVE)
    return AuthorizationDecision(
        mode=ctx.mode, result=result, requested_stage=ctx.requested_stage,
        effective_capability=acc.cap, grantable=grantable, deny_reason=deny_reason,
        reasons=tuple(sorted(set(acc.reasons))), require_user_items=items, retry_at=retry_at,
        input_fingerprint=fingerprint, engine_version=ENGINE_VERSION,
        policy_version_hash=policy_version_hash, subject_policy_hash=subject_hash,
    )
```

Note on `test_invalid_input_fails_closed` with `fit.overall_score: 74.5`: a float attribute makes the context unhashable (`CanonicalHashError`), so it fails closed. The context assembler (Task 13) converts float scores to `Decimal`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/product/test_autonomy_gate.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add product/autonomy_gate.py tests/product/autonomy_fixtures.py tests/product/test_autonomy_gate.py
git commit -m "feat(product): add pure autonomy authorization gate

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Gate — representation requirements and answer validity

**Files:**
- Test: `tests/product/test_autonomy_gate_representation.py`
- (Implementation already in `product/autonomy_gate.py::_apply_requirements` from Task 5; this task pins its behaviour and fixes anything the tests expose.)

**Interfaces:**
- Consumes: `evaluate_authorization`, fixtures from Task 5.
- Produces: nothing new.

- [ ] **Step 1: Write the tests**

```python
# tests/product/test_autonomy_gate_representation.py
from __future__ import annotations

from datetime import timedelta

from product.autonomy_contract import (
    Capability, EmployerKeyStrength, Reach, RepresentationRequirement, RequireUserItem,
    ResultKind, UNKNOWN,
)
from product.autonomy_gate import evaluate_authorization
from tests.product.autonomy_fixtures import NOW, answer, make_ctx

C, R = Capability, ResultKind


def req(key, subject, *, required=True, evidence=False, job_context=None, candidates=()):
    return RepresentationRequirement(key=key, subject=subject, required=required,
                                     evidence_available=evidence, job_context=job_context or {},
                                     candidates=tuple(candidates))


def run(*requirements, **overrides):
    return evaluate_authorization(make_ctx(requirements=tuple(requirements), **overrides))


def test_evidence_backed_fields_are_ready():
    assert run(req("email", None, evidence=True)).grantable
    assert run(req("rtw", "work_authorization.right_to_work", evidence=True)).grantable


def test_unclassified_required_field_requires_user_at_submit_only():
    d = run(req("q7", None))
    assert d.result is R.REQUIRE_USER and RequireUserItem("unclassified_field", "q7") in d.require_user_items
    d = run(req("q7", None), requested_stage=C.FILL)
    assert d.result is R.ALLOW and d.grantable
    assert run(req("q8", None, required=False)).grantable


def test_sensitive_required_pauses_optional_omitted():
    d = run(req("eeo", "demographic.eeo"))
    assert RequireUserItem("sensitive_field", "eeo") in d.require_user_items
    d = run(req("eeo", "demographic.eeo", required=False))
    assert d.grantable and any(r.code == "optional_omitted" for r in d.reasons)


def test_fresh_account_answer_is_submit_ready():
    d = run(req("notice", "employment.notice_period", candidates=[answer("employment.notice_period")]))
    assert d.grantable


def test_expired_answer_caps_at_fill_without_deleting():
    old = answer("employment.notice_period", confirmed_at=NOW - timedelta(days=61))
    d = run(req("notice", "employment.notice_period", candidates=[old]))
    assert (d.result, d.effective_capability, d.grantable) == (R.ALLOW, C.FILL, False)
    assert any(r.code == "answer_not_submit_ready" and ("why", "expired") in r.params for r in d.reasons)
    assert run(req("notice", "employment.notice_period", candidates=[old]), requested_stage=C.FILL).grantable


def test_no_expiry_subject_but_basis_change_still_matters():
    ancient = answer("work_authorization.right_to_work", confirmed_at=NOW - timedelta(days=3650),
                     context={"country": "GB"}, basis_kind="EVIDENCE", basis_at="sha256:a", basis_now="sha256:a")
    job = {"country": "GB"}
    assert run(req("rtw", "work_authorization.right_to_work", job_context=job, candidates=[ancient])).grantable
    changed = answer("work_authorization.right_to_work", context={"country": "GB"},
                     basis_kind="EVIDENCE", basis_at="sha256:a", basis_now="sha256:b")
    d = run(req("rtw", "work_authorization.right_to_work", job_context=job, candidates=[changed]))
    assert d.effective_capability == C.FILL and not d.grantable
    gone = answer("work_authorization.right_to_work", context={"country": "GB"},
                  basis_kind="EVIDENCE", basis_at="sha256:a", basis_now=None)
    assert run(req("rtw", "work_authorization.right_to_work", job_context=job, candidates=[gone])).effective_capability == C.FILL


def test_contradiction_requires_user_even_at_fill():
    bad = answer("employment.notice_period", contradicted=True)
    for stage in (C.FILL, C.SUBMIT):
        d = run(req("notice", "employment.notice_period", candidates=[bad]), requested_stage=stage)
        assert d.result is R.REQUIRE_USER
        assert RequireUserItem("contradicted_answer", "notice") in d.require_user_items


def test_reach_rules():
    other_ws = answer("mobility.relocation", reach=Reach.SEARCH_WORKSPACE, scope_id="sw_other",
                      context={"region": "ME"})
    d = run(req("reloc", "mobility.relocation", job_context={"region": "ME"}, candidates=[other_ws]))
    assert RequireUserItem("missing_answer", "reloc") in d.require_user_items
    same_ws = answer("mobility.relocation", reach=Reach.SEARCH_WORKSPACE, scope_id="sw_1", context={"region": "ME"})
    assert run(req("reloc", "mobility.relocation", job_context={"region": "ME"}, candidates=[same_ws])).grantable
    too_wide = answer("motivation.employer_specific", reach=Reach.ACCOUNT)
    d = run(req("why_us", "motivation.employer_specific", candidates=[too_wide]))
    assert RequireUserItem("missing_answer", "why_us") in d.require_user_items


def test_employer_bound_answer_and_generic_never_substitutes():
    wood = answer("motivation.employer_specific", reach=Reach.EMPLOYER, scope_id="name:acme")
    assert run(req("why_us", "motivation.employer_specific", candidates=[wood])).grantable
    generic = answer("motivation.role_type", reach=Reach.ACCOUNT)
    d = run(req("why_us", "motivation.employer_specific", candidates=[generic]))
    assert RequireUserItem("missing_answer", "why_us") in d.require_user_items
    # Unknown employer key: employer-bound answers unusable, and the per-employer
    # limit cannot be evaluated, so capability is capped at FILL; the question
    # cannot unlock SUBMIT, so it stays silent (relevance).
    d = run(req("why_us", "motivation.employer_specific", candidates=[wood]),
            employer_key_strength=EmployerKeyStrength.UNKNOWN)
    assert (d.result, d.effective_capability, d.grantable) == (R.ALLOW, C.FILL, False)


def test_context_keys():
    ctx = {"currency": "GBP", "region": "UK", "employment_type": "PERMANENT"}
    sal = answer("compensation.salary_expectation", reach=Reach.SEARCH_WORKSPACE, scope_id="sw_1", context=ctx)
    assert run(req("salary", "compensation.salary_expectation", job_context=ctx, candidates=[sal])).grantable
    unknown_region = {**ctx, "region": UNKNOWN}
    d = run(req("salary", "compensation.salary_expectation", job_context=unknown_region, candidates=[sal]))
    assert d.effective_capability == C.FILL
    usd = {**ctx, "currency": "USD"}
    d = run(req("salary", "compensation.salary_expectation", job_context=usd, candidates=[sal]))
    assert RequireUserItem("missing_answer", "salary") in d.require_user_items


def test_relevance_questions_that_cannot_unlock_anything_stay_silent():
    d = run(req("q7", None), workspace_ceiling=C.FILL)
    assert d.result is R.ALLOW and d.effective_capability == C.FILL and d.require_user_items == ()
    assert any(r.code == "unresolved_silent" for r in d.reasons)


def test_prepare_requests_ignore_fields():
    assert run(req("q7", None), requested_stage=C.PREPARE).grantable
```

- [ ] **Step 2: Run the tests**

Run: `.venv/Scripts/python -m pytest tests/product/test_autonomy_gate_representation.py -q`
Expected: all PASS. If any fails, fix `_apply_requirements` (not the test) so behaviour matches spec §7 and §9.3, rerun, and rerun Task 5's tests.

- [ ] **Step 3: Commit**

```bash
git add tests/product/test_autonomy_gate_representation.py product/autonomy_gate.py
git commit -m "test(product): pin gate representation and answer-validity rules

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Gate property tests (Hypothesis)

**Files:**
- Test: `tests/product/test_autonomy_gate_properties.py`

**Interfaces:**
- Consumes: `evaluate_authorization`, fixtures.
- Produces: nothing new.

**Precise form of the spec §17 properties.** Because of the relevance rule (§9.3 step 5), tightening can turn `REQUIRE_USER` into a non-grantable `ALLOW(lower)` — a question that can no longer unlock anything is dropped. The monotonicity property is therefore stated over what matters for safety: **tightening never raises `effective_capability` and never turns a non-grantable decision into a grantable one.** Unknown-safety is stated for policies whose rules declare `on_unknown` at least as restrictive as `effect` (a rule declaring `NO_EFFECT` is the user's explicit statement that absence is safe).

- [ ] **Step 1: Write the tests**

```python
# tests/product/test_autonomy_gate_properties.py
from __future__ import annotations

import dataclasses
from datetime import timedelta

from hypothesis import given, settings, strategies as st

from product.autonomy_contract import (
    Capability, CounterState, IdentityStrength, Mode, ProvenanceTier, UNKNOWN,
)
from product.autonomy_gate import evaluate_authorization
from product.autonomy_contract import RepresentationRequirement
from tests.product.autonomy_fixtures import NOW, answer, good_target, make_ctx, make_policy

C = Capability
levels = st.sampled_from(list(C))
stages = st.sampled_from([C.PREPARE, C.FILL, C.SUBMIT])
EFFECTS = [{"type": "REDUCE_TO", "level": "NONE"}, {"type": "REDUCE_TO", "level": "PREPARE"},
           {"type": "REDUCE_TO", "level": "FILL"}, {"type": "REQUIRE_USER"}, {"type": "BLOCK"}]
ATTRS = ["fit.overall_score", "job.employment_type", "company.key"]


@st.composite
def rules(draw):
    count = draw(st.integers(0, 3))
    out = []
    for i in range(count):
        attr = draw(st.sampled_from(ATTRS))
        pred = ({"attr": attr, "op": "lt", "value": draw(st.integers(0, 100))} if attr == "fit.overall_score"
                else {"attr": attr, "op": "eq", "value": draw(st.sampled_from(["PERMANENT", "CONTRACT", "name:acme"]))})
        effect = draw(st.sampled_from(EFFECTS))
        out.append({"id": f"r{i}", "description": "", "when": pred, "effect": effect, "on_unknown": effect})
    return out


@st.composite
def contexts(draw):
    notice = draw(st.sampled_from([None, 0, 59, 61]))
    requirements = ()
    if notice is not None:
        requirements = (RepresentationRequirement(
            key="notice", subject="employment.notice_period", required=True, evidence_available=False,
            candidates=(answer("employment.notice_period", confirmed_at=NOW - timedelta(days=notice)),)),)
    return make_ctx(
        requested_stage=draw(stages),
        deployment_ceiling=draw(levels), account_max=draw(levels), workspace_ceiling=draw(levels),
        standing_policy=make_policy(*draw(rules())),
        attributes={"fit.overall_score": draw(st.integers(0, 100)),
                    "job.employment_type": draw(st.sampled_from(["PERMANENT", "CONTRACT"])),
                    "company.key": "name:acme"},
        identity_strength=draw(st.sampled_from(list(IdentityStrength))),
        apply_target=good_target(provenance=draw(st.sampled_from([None, *ProvenanceTier]))),
        pack_auto_confirmable=draw(st.booleans()),
        requirements=requirements,
    )


def tighten_ops(ctx):
    lower = lambda c: C(max(0, c - 1))
    yield dataclasses.replace(ctx, deployment_ceiling=lower(ctx.deployment_ceiling))
    yield dataclasses.replace(ctx, account_max=lower(ctx.account_max))
    yield dataclasses.replace(ctx, workspace_ceiling=lower(ctx.workspace_ceiling))
    yield dataclasses.replace(ctx, kill_switch_engaged=True)
    yield dataclasses.replace(ctx, sentinel_present=True)
    yield dataclasses.replace(ctx, identity_strength=IdentityStrength.WEAK)
    yield dataclasses.replace(ctx, identity_conflict=True)
    yield dataclasses.replace(ctx, pack_auto_confirmable=False)
    yield dataclasses.replace(ctx, existing_intent_state="CONFIRMED")
    yield dataclasses.replace(ctx, governing_auto_reject=True)
    yield dataclasses.replace(ctx, apply_target=good_target(provenance=ProvenanceTier.IMPORTED_SOURCE))
    yield dataclasses.replace(ctx, counters=(CounterState("x", ctx.requested_stage, 1, 1, None),))
    yield dataclasses.replace(ctx, unresolved_governing_require_user=("blk",))
    yield dataclasses.replace(ctx, standing_policy=make_policy(
        *ctx.standing_policy["rules"],
        {"id": "extra", "description": "", "when": {"attr": "fit.overall_score", "op": "lt", "value": 101},
         "effect": {"type": "REDUCE_TO", "level": "PREPARE"}, "on_unknown": {"type": "REDUCE_TO", "level": "PREPARE"}}))
    for attr in ATTRS:
        yield dataclasses.replace(ctx, attributes={**ctx.attributes, attr: UNKNOWN})


@settings(max_examples=300, deadline=None)
@given(contexts())
def test_monotonicity_and_unknown_safety(ctx):
    base = evaluate_authorization(ctx)
    for tighter in tighten_ops(ctx):
        d = evaluate_authorization(tighter)
        assert d.effective_capability <= base.effective_capability
        assert not (d.grantable and not base.grantable)


@settings(max_examples=200, deadline=None)
@given(contexts(), st.randoms())
def test_rule_order_independence(ctx, rnd):
    rules_ = list(ctx.standing_policy["rules"])
    rnd.shuffle(rules_)
    shuffled = dataclasses.replace(ctx, standing_policy=make_policy(*rules_))
    a, b = evaluate_authorization(ctx), evaluate_authorization(shuffled)
    assert (a.result, a.effective_capability, a.grantable, a.reasons, a.require_user_items) == \
           (b.result, b.effective_capability, b.grantable, b.reasons, b.require_user_items)


@settings(max_examples=200, deadline=None)
@given(contexts())
def test_determinism_and_authority_source(ctx):
    a, b = evaluate_authorization(ctx), evaluate_authorization(ctx)
    assert a == b
    assert a.effective_capability <= min(ctx.deployment_ceiling, ctx.account_max, ctx.workspace_ceiling)


@settings(max_examples=100, deadline=None)
@given(contexts(), st.sampled_from([Mode.SHADOW, Mode.DRY_RUN]))
def test_non_live_is_never_grantable(ctx, mode):
    assert not evaluate_authorization(dataclasses.replace(ctx, mode=mode)).grantable
```

- [ ] **Step 2: Run the tests**

Run: `.venv/Scripts/python -m pytest tests/product/test_autonomy_gate_properties.py -q`
Expected: all PASS. A Hypothesis failure prints a minimal counterexample: fix the gate (never weaken the property), rerun Tasks 5–7 tests.

- [ ] **Step 3: Commit**

```bash
git add tests/product/test_autonomy_gate_properties.py product/autonomy_gate.py
git commit -m "test(product): add property-based tests for the authorization gate

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 8: Migration `016_autonomy_contract`

**Files:**
- Modify: `webapp/persistence/migrations.py` (add ID constant after line 33, register in `apply_migrations`' tuple, add `_migrate_autonomy_contract`)
- Test: `tests/webapp/persistence/test_autonomy_migration.py`

**Interfaces:**
- Consumes: existing tables `accounts`, `workspaces`, `blocker_resolutions`, `application_blockers`, `workflow_events`.
- Produces: all §15 tables (column lists below are the contract for Tasks 9–11), `AUTONOMY_CONTRACT_MIGRATION_ID = "016_autonomy_contract"`, `AUTONOMY_APPEND_ONLY_TABLES` (tuple of table names).

- [ ] **Step 1: Write the failing test**

```python
# tests/webapp/persistence/test_autonomy_migration.py
from __future__ import annotations

import sqlite3

import pytest

from webapp.persistence.db import connect, init_db
from webapp.persistence.migrations import AUTONOMY_APPEND_ONLY_TABLES

EXPECTED = {
    "autonomy_authorizations", "autonomy_kill_switch", "autonomy_control_events", "autonomy_runs",
    "autonomy_run_ends", "standing_policy_versions", "approved_answers", "answer_confirmations",
    "proposed_answers", "rule_acknowledgements", "apply_target_confirmations", "autonomy_decisions",
    "autonomy_grants", "autonomy_grant_events", "limit_reservations", "submission_intents",
    "intent_overrides", "submission_attempts", "submission_attempt_events",
    "dry_run_submission_cases", "dry_run_case_agreements", "autonomy_queue_items",
}


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "t.sqlite3"
    init_db(db)
    c = connect(db)
    yield c
    c.close()


def test_tables_exist_and_migration_is_idempotent(conn, tmp_path):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED <= names
    init_db(tmp_path / "t.sqlite3")  # second run is a no-op
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE id='016_autonomy_contract'").fetchone()[0] == 1


def test_append_only_tables_have_seq_and_reject_update_delete(conn):
    for table in AUTONOMY_APPEND_ONLY_TABLES:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert cols[0] == "seq", table
    conn.execute(
        "INSERT INTO autonomy_kill_switch (id, account_id, engaged, reason, actor, created_at) "
        "VALUES ('k1', 'account_local', 1, 'test', 'me', '2026-09-24T12:00:00.000000+00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE autonomy_kill_switch SET engaged = 0")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM autonomy_kill_switch")


def test_live_intent_unique_index_ignores_released_and_overridden(conn):
    conn.execute(
        "INSERT INTO workspaces (id, kind, company, title, workflow_status, created_at, updated_at, account_id) "
        "VALUES ('ws1', 'job', 'Acme', 'Eng', NULL, 'x', 'x', 'account_local')"
    )
    row = ("account_local", "source:x:1", "ws1", "AUTONOMOUS", "t", "t")
    sql = ("INSERT INTO submission_intents (id, account_id, job_identity_key, application_workspace_id, state, "
           "source, overridden, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
    conn.execute(sql, ("i1", row[0], row[1], row[2], "CONFIRMED", row[3], 0, row[4], row[5]))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("i2", row[0], row[1], row[2], "CLAIMED", row[3], 0, row[4], row[5]))
    conn.execute("UPDATE submission_intents SET overridden = 1 WHERE id = 'i1'")
    conn.execute(sql, ("i3", row[0], row[1], row[2], "CLAIMED", row[3], 0, row[4], row[5]))
    conn.execute(sql, ("i4", row[0], row[1], row[2], "RELEASED", row[3], 0, row[4], row[5]))
```

(`'account_local'` is `DEFAULT_ACCOUNT_ID`; confirm with `grep -n "DEFAULT_ACCOUNT_ID =" webapp/persistence/accounts.py` and use the literal it defines.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_autonomy_migration.py -q`
Expected: FAIL — `ImportError: cannot import name 'AUTONOMY_APPEND_ONLY_TABLES'`.

- [ ] **Step 3: Implement the migration**

Add after line 33 of `webapp/persistence/migrations.py`:

```python
AUTONOMY_CONTRACT_MIGRATION_ID = "016_autonomy_contract"
AUTONOMY_APPEND_ONLY_TABLES = (
    "autonomy_authorizations", "autonomy_kill_switch", "autonomy_control_events",
    "autonomy_runs", "autonomy_run_ends", "standing_policy_versions", "approved_answers",
    "answer_confirmations", "proposed_answers", "rule_acknowledgements",
    "apply_target_confirmations", "autonomy_decisions", "autonomy_grant_events",
    "intent_overrides", "submission_attempts", "submission_attempt_events",
    "dry_run_submission_cases", "dry_run_case_agreements",
)
```

Register it as the last entry of the `migrations` tuple in `apply_migrations`:

```python
        (AUTONOMY_CONTRACT_MIGRATION_ID, _migrate_autonomy_contract, False),
```

Add the function at the end of the module:

```python
def _migrate_autonomy_contract(conn: sqlite3.Connection) -> None:
    # Bundle 6B autonomy contract (spec §15). Every append-only table carries
    # seq INTEGER PRIMARY KEY AUTOINCREMENT and every "current" projection
    # orders by seq, never created_at (spec §2 invariant 13). Status tables
    # (grants, reservations, intents, queue items) are the only mutable ones.
    _execute_statements(
        conn,
        """
        CREATE TABLE autonomy_authorizations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            scope_type TEXT NOT NULL CHECK (scope_type IN ('ACCOUNT_MAX', 'DEFAULT_WORKSPACE_CEILING', 'WORKSPACE_CEILING')),
            scope_id TEXT NOT NULL,
            capability TEXT NOT NULL CHECK (capability IN ('NONE', 'PREPARE', 'FILL', 'SUBMIT')),
            set_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_autonomy_authorizations_scope
            ON autonomy_authorizations(account_id, scope_type, scope_id);

        CREATE TABLE autonomy_kill_switch (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            engaged INTEGER NOT NULL CHECK (engaged IN (0, 1)),
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_control_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            scope_type TEXT NOT NULL CHECK (scope_type IN ('APPLICATION', 'SEARCH_WORKSPACE', 'ACCOUNT')),
            scope_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('PAUSE', 'RESUME', 'RESUME_ALL')),
            kill_switch_seq_acknowledged INTEGER,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_runs (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            started_by TEXT NOT NULL CHECK (started_by IN ('SCHEDULER', 'USER')),
            started_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_run_ends (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE REFERENCES autonomy_runs(run_id),
            end_reason TEXT NOT NULL,
            ended_at TEXT NOT NULL
        );

        CREATE TABLE standing_policy_versions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            policy_json TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE approved_answers (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            subject TEXT NOT NULL,
            answer_kind TEXT NOT NULL CHECK (answer_kind IN ('STRUCTURED', 'FREE_TEXT')),
            value_json TEXT NOT NULL,
            reach TEXT NOT NULL CHECK (reach IN ('EMPLOYER', 'SEARCH_WORKSPACE', 'ACCOUNT')),
            scope_id TEXT NOT NULL,
            context_json TEXT NOT NULL,
            provenance TEXT NOT NULL CHECK (provenance IN ('USER', 'USER_EDITED_PROPOSAL')),
            basis_json TEXT NOT NULL,
            basis_profile_version_id TEXT,
            supersedes_id TEXT REFERENCES approved_answers(id),
            source_blocker_resolution_id TEXT REFERENCES blocker_resolutions(id),
            approved_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_approved_answers_subject ON approved_answers(account_id, subject);

        CREATE TABLE answer_confirmations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            approved_answer_id TEXT NOT NULL REFERENCES approved_answers(id),
            confirmed_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE proposed_answers (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            blocker_id TEXT NOT NULL REFERENCES application_blockers(id),
            subject TEXT NOT NULL,
            value_json TEXT NOT NULL,
            provenance TEXT NOT NULL CHECK (provenance = 'SYSTEM_PROPOSED'),
            created_at TEXT NOT NULL
        );

        CREATE TABLE rule_acknowledgements (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            rule_id TEXT NOT NULL,
            rule_hash TEXT NOT NULL,
            observed_fingerprint TEXT NOT NULL,
            policy_version_hash TEXT NOT NULL,
            disposition TEXT NOT NULL CHECK (disposition IN ('PROCEED', 'DO_NOT_PROCEED')),
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE apply_target_confirmations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            job_identity_key TEXT,
            canonical_url TEXT NOT NULL,
            confirmed_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_decisions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            run_id TEXT,
            mode TEXT NOT NULL CHECK (mode IN ('LIVE', 'SHADOW', 'DRY_RUN')),
            requested_stage TEXT NOT NULL CHECK (requested_stage IN ('PREPARE', 'FILL', 'SUBMIT', 'NONE')),
            result TEXT NOT NULL CHECK (result IN ('ALLOW', 'REQUIRE_USER', 'BLOCK', 'DENY', 'DENY_TEMPORARY')),
            deny_reason TEXT,
            effective_capability TEXT NOT NULL CHECK (effective_capability IN ('NONE', 'PREPARE', 'FILL', 'SUBMIT')),
            grantable INTEGER NOT NULL CHECK (grantable IN (0, 1)),
            reasons_json TEXT NOT NULL,
            require_user_json TEXT NOT NULL,
            retry_at TEXT,
            inputs_json TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            engine_version TEXT NOT NULL,
            policy_version_hash TEXT,
            subject_policy_hash TEXT,
            grant_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_autonomy_decisions_workspace ON autonomy_decisions(application_workspace_id, seq);

        CREATE TABLE autonomy_grants (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            decision_id TEXT NOT NULL UNIQUE REFERENCES autonomy_decisions(id),
            account_id TEXT NOT NULL REFERENCES accounts(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            stage TEXT NOT NULL CHECK (stage IN ('FILL', 'SUBMIT')),
            nonce TEXT NOT NULL UNIQUE,
            binding_json TEXT NOT NULL,
            binding_fingerprint TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('ISSUED', 'CONSUMED', 'EXPIRED', 'REVOKED')),
            consumed_at TEXT,
            revoked_reason TEXT
        );

        CREATE TABLE autonomy_grant_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            grant_id TEXT NOT NULL REFERENCES autonomy_grants(id),
            status TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE limit_reservations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            counter_name TEXT NOT NULL,
            window_key TEXT NOT NULL,
            amount TEXT NOT NULL,
            grant_id TEXT REFERENCES autonomy_grants(id),
            attempt_id TEXT,
            status TEXT NOT NULL CHECK (status IN ('RESERVED', 'CONSUMED', 'RELEASED')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_limit_reservations_window
            ON limit_reservations(account_id, counter_name, window_key, status);

        CREATE TABLE submission_intents (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            job_identity_key TEXT NOT NULL,
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            state TEXT NOT NULL CHECK (state IN ('CLAIMED', 'CONFIRMED', 'RELEASED')),
            source TEXT NOT NULL CHECK (source IN ('AUTONOMOUS', 'HUMAN_HANDOFF', 'HUMAN_APPLIED')),
            overridden INTEGER NOT NULL DEFAULT 0 CHECK (overridden IN (0, 1)),
            attempt_id TEXT,
            workflow_event_id TEXT REFERENCES workflow_events(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_submission_intents_live
            ON submission_intents(account_id, job_identity_key)
            WHERE state IN ('CLAIMED', 'CONFIRMED') AND overridden = 0;

        CREATE TABLE intent_overrides (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            intent_id TEXT NOT NULL REFERENCES submission_intents(id),
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE submission_attempts (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            grant_id TEXT NOT NULL UNIQUE REFERENCES autonomy_grants(id),
            intent_id TEXT NOT NULL REFERENCES submission_intents(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            run_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE submission_attempt_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT NOT NULL REFERENCES submission_attempts(id),
            state TEXT NOT NULL CHECK (state IN (
                'AUTHORIZED', 'CLICK_DISPATCHED', 'CONFIRMED_SUCCESS', 'SUBMISSION_AMBIGUOUS',
                'SUBMISSION_FAILED', 'EXPIRED_UNCLICKED', 'DUPLICATE_SUPPRESSED'
            )),
            evidence_json TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('SERVER', 'EXECUTOR', 'USER')),
            created_at TEXT NOT NULL
        );

        CREATE TABLE dry_run_submission_cases (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            decision_id TEXT NOT NULL REFERENCES autonomy_decisions(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            adapter_id TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            manifest_hash TEXT NOT NULL,
            verification_result TEXT NOT NULL CHECK (verification_result IN ('MATCH', 'MISMATCH')),
            created_at TEXT NOT NULL
        );

        CREATE TABLE dry_run_case_agreements (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            case_id TEXT NOT NULL REFERENCES dry_run_submission_cases(id),
            agreement TEXT NOT NULL CHECK (agreement IN ('AGREE', 'DISAGREE')),
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_queue_items (
            application_workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),
            account_id TEXT NOT NULL REFERENCES accounts(id),
            next_stage TEXT NOT NULL CHECK (next_stage IN ('PREPARE', 'FILL', 'SUBMIT')),
            next_eligible_at TEXT,
            lease_holder TEXT,
            lease_expires_at TEXT,
            paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
            updated_at TEXT NOT NULL
        )
        """,
    )
    for table in AUTONOMY_APPEND_ONLY_TABLES:
        for action in ("UPDATE", "DELETE"):
            conn.execute(
                f"CREATE TRIGGER {table}_append_only_{action.lower()} "
                f"BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only audit history'); END"
            )
```

- [ ] **Step 4: Run tests (migration test plus existing migration suites)**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/persistence/migrations.py tests/webapp/persistence/test_autonomy_migration.py
git commit -m "feat(persistence): add autonomy contract schema migration

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Persistence — authority, kill switch, controls, policy versions, runs

**Files:**
- Create: `webapp/persistence/autonomy_authority.py`
- Create: `tests/webapp/persistence/autonomy_db.py` (shared DB fixture helpers for Tasks 9–18)
- Test: `tests/webapp/persistence/test_autonomy_authority.py`

**Interfaces:**
- Consumes: Task 8 tables; `Capability`, `to_utc_iso`, `canonical_json`; `validate_standing_policy`, `policy_hash`.
- Produces:
  - `record_authorization(conn, *, account_id, scope_type, scope_id, capability: Capability, set_by, now, commit=True) -> dict`
  - `current_capability(conn, *, account_id, scope_type, scope_id) -> Capability | None`
  - `resolve_authority(conn, *, account_id, search_workspace_id: str | None) -> tuple[Capability, Capability]` — `(account_max, workspace_ceiling)`
  - `record_kill_switch(conn, *, account_id, engaged: bool, reason, actor, now, commit=True) -> dict`
  - `kill_switch_state(conn, account_id) -> dict` — `{"engaged": bool, "latest_engage_seq": int | None, "resume_acknowledged_seq": int | None, "halted": bool}` where `halted = engaged or latest_engage_seq > resume_acknowledged_seq`
  - `record_control_event(conn, *, account_id, scope_type, scope_id, action, actor, reason, now, kill_switch_seq_acknowledged=None, commit=True) -> dict`
  - `is_paused(conn, *, account_id, scope_type, scope_id) -> bool`
  - `save_policy_version(conn, *, account_id, doc, created_by, now, commit=True) -> dict` (validates; raises `StandingPolicyError`)
  - `current_policy(conn, account_id) -> dict | None` — `{"id", "doc", "policy_hash", "seq"}`
  - `start_run(conn, *, account_id, started_by, now, run_id=None, commit=True) -> dict`, `end_run(conn, *, run_id, end_reason, now, commit=True) -> dict`, `get_run(conn, run_id) -> dict | None` (with `ended_at`)

- [ ] **Step 1: Create shared test helpers**

```python
# tests/webapp/persistence/autonomy_db.py
"""Shared fixtures for autonomy persistence/service tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID
from webapp.persistence.db import connect, init_db
from webapp.persistence.workspaces import create_workspace

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
ACCOUNT = DEFAULT_ACCOUNT_ID


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "autonomy.sqlite3"
    init_db(db)
    c = connect(db)
    yield c
    c.close()


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "autonomy.sqlite3"
    init_db(db)
    return db


def make_workspace(conn, company="Acme", title="Drilling Fluids Engineer", workspace_id=None):
    return create_workspace(conn, company=company, title=title, workspace_id=workspace_id)["id"]
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/webapp/persistence/test_autonomy_authority.py
from __future__ import annotations

from datetime import timedelta

import pytest

from product.autonomy_contract import Capability
from product.standing_policy import StandingPolicyError, default_policy_document, policy_hash
from webapp.persistence.autonomy_authority import (
    current_capability, current_policy, end_run, get_run, is_paused, kill_switch_state,
    record_authorization, record_control_event, record_kill_switch, resolve_authority,
    save_policy_version, start_run,
)
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn  # noqa: F401


def test_no_records_means_none(conn):
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id="sw_1") == (Capability.NONE, Capability.NONE)
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id=None) == (Capability.NONE, Capability.NONE)


def test_latest_by_seq_even_with_identical_timestamps(conn):
    for cap in (Capability.SUBMIT, Capability.PREPARE):
        record_authorization(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT,
                             capability=cap, set_by="user", now=NOW)
    assert current_capability(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT) == Capability.PREPARE


def test_workspace_ceiling_falls_back_to_explicit_default(conn):
    record_authorization(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT, capability=Capability.SUBMIT, set_by="u", now=NOW)
    record_authorization(conn, account_id=ACCOUNT, scope_type="DEFAULT_WORKSPACE_CEILING", scope_id=ACCOUNT, capability=Capability.PREPARE, set_by="u", now=NOW)
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id="sw_1") == (Capability.SUBMIT, Capability.PREPARE)
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id=None) == (Capability.SUBMIT, Capability.PREPARE)
    record_authorization(conn, account_id=ACCOUNT, scope_type="WORKSPACE_CEILING", scope_id="sw_1", capability=Capability.SUBMIT, set_by="u", now=NOW)
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id="sw_1") == (Capability.SUBMIT, Capability.SUBMIT)


def test_kill_switch_release_does_not_resume_without_resume_all(conn):
    assert kill_switch_state(conn, ACCOUNT)["halted"] is False
    engaged = record_kill_switch(conn, account_id=ACCOUNT, engaged=True, reason="r", actor="u", now=NOW)
    record_kill_switch(conn, account_id=ACCOUNT, engaged=False, reason="r", actor="u", now=NOW)
    state = kill_switch_state(conn, ACCOUNT)
    assert state["engaged"] is False and state["halted"] is True
    record_control_event(conn, account_id=ACCOUNT, scope_type="ACCOUNT", scope_id=ACCOUNT, action="RESUME_ALL",
                         actor="u", reason="ok", now=NOW, kill_switch_seq_acknowledged=engaged["seq"])
    assert kill_switch_state(conn, ACCOUNT)["halted"] is False


def test_pause_resume_by_seq_and_resume_all(conn):
    kw = dict(account_id=ACCOUNT, scope_type="APPLICATION", scope_id="ws_1", actor="u", reason="r", now=NOW)
    assert not is_paused(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id="ws_1")
    record_control_event(conn, action="PAUSE", **kw)
    assert is_paused(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id="ws_1")
    record_control_event(conn, action="RESUME", **kw)
    assert not is_paused(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id="ws_1")
    record_control_event(conn, action="PAUSE", **kw)
    record_control_event(conn, account_id=ACCOUNT, scope_type="ACCOUNT", scope_id=ACCOUNT, action="RESUME_ALL",
                         actor="u", reason="r", now=NOW)
    assert not is_paused(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id="ws_1")


def test_policy_versions(conn):
    assert current_policy(conn, ACCOUNT) is None
    doc = default_policy_document("Europe/London")
    saved = save_policy_version(conn, account_id=ACCOUNT, doc=doc, created_by="u", now=NOW)
    current = current_policy(conn, ACCOUNT)
    assert current["doc"] == doc and current["policy_hash"] == policy_hash(doc) == saved["policy_hash"]
    with pytest.raises(StandingPolicyError):
        save_policy_version(conn, account_id=ACCOUNT, doc={**doc, "timezone": "Nowhere/Void"}, created_by="u", now=NOW)
    assert current_policy(conn, ACCOUNT)["id"] == saved["id"]


def test_runs(conn):
    run = start_run(conn, account_id=ACCOUNT, started_by="SCHEDULER", now=NOW)
    assert get_run(conn, run["run_id"])["ended_at"] is None
    end_run(conn, run_id=run["run_id"], end_reason="done", now=NOW + timedelta(minutes=5))
    assert get_run(conn, run["run_id"])["ended_at"] is not None
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_autonomy_authority.py -q`
Expected: FAIL — module not found.

- [ ] **Step 4: Implement `webapp/persistence/autonomy_authority.py`**

```python
"""Autonomy authority records (6B spec §4, §11.4, §15): explicit user
authorizations, kill switch, pause/resume control events, standing-policy
versions and autonomous runs. All append-only; "current" is always by seq."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from product.autonomy_contract import Capability, canonical_json, to_utc_iso
from product.standing_policy import policy_hash, validate_standing_policy


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, Any], commit: bool) -> dict[str, Any]:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    row = conn.execute(f"SELECT * FROM {table} WHERE seq = ?", (cur.lastrowid,)).fetchone()
    if commit:
        conn.commit()
    return dict(row)


def record_authorization(conn, *, account_id: str, scope_type: str, scope_id: str,
                         capability: Capability, set_by: str, now: datetime, commit: bool = True) -> dict[str, Any]:
    return _insert(conn, "autonomy_authorizations", {
        "id": _id("auth"), "account_id": account_id, "scope_type": scope_type, "scope_id": scope_id,
        "capability": Capability(capability).name, "set_by": set_by, "created_at": to_utc_iso(now),
    }, commit)


def current_capability(conn, *, account_id: str, scope_type: str, scope_id: str) -> Capability | None:
    row = conn.execute(
        "SELECT capability FROM autonomy_authorizations "
        "WHERE account_id = ? AND scope_type = ? AND scope_id = ? ORDER BY seq DESC LIMIT 1",
        (account_id, scope_type, scope_id),
    ).fetchone()
    return Capability[row["capability"]] if row else None


def resolve_authority(conn, *, account_id: str, search_workspace_id: str | None) -> tuple[Capability, Capability]:
    account_max = current_capability(conn, account_id=account_id, scope_type="ACCOUNT_MAX", scope_id=account_id)
    ceiling = None
    if search_workspace_id is not None:
        ceiling = current_capability(conn, account_id=account_id, scope_type="WORKSPACE_CEILING",
                                     scope_id=search_workspace_id)
    if ceiling is None:
        ceiling = current_capability(conn, account_id=account_id, scope_type="DEFAULT_WORKSPACE_CEILING",
                                     scope_id=account_id)
    return (account_max or Capability.NONE, ceiling or Capability.NONE)


def record_kill_switch(conn, *, account_id: str, engaged: bool, reason: str, actor: str,
                       now: datetime, commit: bool = True) -> dict[str, Any]:
    return _insert(conn, "autonomy_kill_switch", {
        "id": _id("kill"), "account_id": account_id, "engaged": 1 if engaged else 0,
        "reason": reason, "actor": actor, "created_at": to_utc_iso(now),
    }, commit)


def kill_switch_state(conn, account_id: str) -> dict[str, Any]:
    latest = conn.execute(
        "SELECT engaged FROM autonomy_kill_switch WHERE account_id = ? ORDER BY seq DESC LIMIT 1", (account_id,),
    ).fetchone()
    engage = conn.execute(
        "SELECT MAX(seq) AS s FROM autonomy_kill_switch WHERE account_id = ? AND engaged = 1", (account_id,),
    ).fetchone()["s"]
    acked = conn.execute(
        "SELECT MAX(kill_switch_seq_acknowledged) AS s FROM autonomy_control_events "
        "WHERE account_id = ? AND action = 'RESUME_ALL'", (account_id,),
    ).fetchone()["s"]
    engaged = bool(latest and latest["engaged"])
    halted = engaged or (engage is not None and (acked is None or engage > acked))
    return {"engaged": engaged, "latest_engage_seq": engage, "resume_acknowledged_seq": acked, "halted": halted}


def record_control_event(conn, *, account_id: str, scope_type: str, scope_id: str, action: str, actor: str,
                         reason: str, now: datetime, kill_switch_seq_acknowledged: int | None = None,
                         commit: bool = True) -> dict[str, Any]:
    return _insert(conn, "autonomy_control_events", {
        "id": _id("ctl"), "account_id": account_id, "scope_type": scope_type, "scope_id": scope_id,
        "action": action, "kill_switch_seq_acknowledged": kill_switch_seq_acknowledged,
        "actor": actor, "reason": reason, "created_at": to_utc_iso(now),
    }, commit)


def is_paused(conn, *, account_id: str, scope_type: str, scope_id: str) -> bool:
    pause = conn.execute(
        "SELECT MAX(seq) AS s FROM autonomy_control_events WHERE account_id = ? AND scope_type = ? "
        "AND scope_id = ? AND action = 'PAUSE'", (account_id, scope_type, scope_id),
    ).fetchone()["s"]
    if pause is None:
        return False
    resume = conn.execute(
        "SELECT MAX(seq) AS s FROM autonomy_control_events WHERE account_id = ? AND ("
        "(scope_type = ? AND scope_id = ? AND action = 'RESUME') OR action = 'RESUME_ALL')",
        (account_id, scope_type, scope_id),
    ).fetchone()["s"]
    return resume is None or pause > resume


def save_policy_version(conn, *, account_id: str, doc: dict[str, Any], created_by: str,
                        now: datetime, commit: bool = True) -> dict[str, Any]:
    validate_standing_policy(doc)
    return _insert(conn, "standing_policy_versions", {
        "id": _id("pol"), "account_id": account_id, "policy_json": canonical_json(doc),
        "policy_hash": policy_hash(doc), "created_by": created_by, "created_at": to_utc_iso(now),
    }, commit)


def current_policy(conn, account_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM standing_policy_versions WHERE account_id = ? ORDER BY seq DESC LIMIT 1", (account_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "doc": json.loads(row["policy_json"]), "policy_hash": row["policy_hash"], "seq": row["seq"]}


def start_run(conn, *, account_id: str, started_by: str, now: datetime, run_id: str | None = None,
              commit: bool = True) -> dict[str, Any]:
    return _insert(conn, "autonomy_runs", {
        "run_id": run_id or _id("run"), "account_id": account_id, "started_by": started_by,
        "started_at": to_utc_iso(now),
    }, commit)


def end_run(conn, *, run_id: str, end_reason: str, now: datetime, commit: bool = True) -> dict[str, Any]:
    return _insert(conn, "autonomy_run_ends", {
        "run_id": run_id, "end_reason": end_reason, "ended_at": to_utc_iso(now),
    }, commit)


def get_run(conn, run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT r.*, e.ended_at, e.end_reason FROM autonomy_runs r "
        "LEFT JOIN autonomy_run_ends e ON e.run_id = r.run_id WHERE r.run_id = ?", (run_id,),
    ).fetchone()
    return dict(row) if row else None
```

Note: `json.loads(canonical_json(doc)) == doc` holds for policy documents because they contain only strings, integers, lists and objects (floats are rejected by validation and hashing).

- [ ] **Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_autonomy_authority.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add webapp/persistence/autonomy_authority.py tests/webapp/persistence/autonomy_db.py tests/webapp/persistence/test_autonomy_authority.py
git commit -m "feat(persistence): add autonomy authority, kill switch, controls, policy and runs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Persistence — approved answers, confirmations, proposals, acknowledgements, target confirmations

**Files:**
- Create: `webapp/persistence/autonomy_answers.py`
- Test: `tests/webapp/persistence/test_autonomy_answers.py`

**Interfaces:**
- Consumes: Task 8 tables; `Reach`, `REACH_ORDER`, `to_utc_iso`, `canonical_json`; `load_subject_policy`, `subject_entry`.
- Produces:
  - `AnswerValidationError(ValueError)`
  - `approve_answer(conn, *, account_id, subject, value, reach: Reach, scope_id: str | None, context: dict, basis: dict, approved_by, now, provenance="USER", basis_profile_version_id=None, supersedes_id=None, source_blocker_resolution_id=None, subject_policy=None, commit=True) -> dict` — writes the answer **and** its first confirmation in one transaction. `basis` is `{"kind": "USER_ASSERTION"}` or `{"kind": "EVIDENCE", "evidence_ids": [...], "value_hash": "sha256:..."}`. For `ACCOUNT` reach, `scope_id` is stored as `account_id`.
  - `confirm_answer(conn, *, approved_answer_id, confirmed_by, now, commit=True) -> dict`
  - `current_approved_answers(conn, *, account_id, subject) -> list[dict]` — non-superseded rows, each with parsed `value`, `context`, `basis`, and `latest_confirmation_at` (by confirmation `seq`)
  - `save_proposed_answer(conn, *, blocker_id, subject, value, now, commit=True) -> dict`
  - `record_rule_acknowledgement(conn, *, account_id, application_workspace_id, rule_id, rule_hash, observed_fingerprint, policy_version_hash, disposition, actor, now, commit=True) -> dict`
  - `current_rule_acknowledgements(conn, application_workspace_id) -> list[dict]` (latest per `rule_id` by `seq`)
  - `confirm_apply_target(conn, *, application_workspace_id, job_identity_key, canonical_url, confirmed_by, now, commit=True) -> dict`
  - `current_apply_target_confirmation(conn, application_workspace_id) -> dict | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/webapp/persistence/test_autonomy_answers.py
from __future__ import annotations

from datetime import timedelta

import pytest

from product.autonomy_contract import Reach
from webapp.persistence.autonomy_answers import (
    AnswerValidationError, approve_answer, confirm_answer, confirm_apply_target,
    current_apply_target_confirmation, current_approved_answers, current_rule_acknowledgements,
    record_rule_acknowledgement,
)
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401

ASSERT = {"kind": "USER_ASSERTION"}


def test_approve_writes_first_confirmation(conn):
    a = approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                       reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW)
    (current,) = current_approved_answers(conn, account_id=ACCOUNT, subject="employment.notice_period")
    assert current["id"] == a["id"] and current["value"] == "1 month"
    assert current["latest_confirmation_at"] == "2026-09-24T12:00:00.000000+00:00"
    confirm_answer(conn, approved_answer_id=a["id"], confirmed_by="u", now=NOW + timedelta(days=30))
    (current,) = current_approved_answers(conn, account_id=ACCOUNT, subject="employment.notice_period")
    assert current["latest_confirmation_at"].startswith("2026-10-24")


def test_supersede_hides_old_row(conn):
    a = approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                       reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW)
    b = approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="3 months",
                       reach=Reach.ACCOUNT, scope_id=None, context={}, basis=ASSERT, approved_by="u", now=NOW,
                       supersedes_id=a["id"])
    assert [r["id"] for r in current_approved_answers(conn, account_id=ACCOUNT, subject="employment.notice_period")] == [b["id"]]


@pytest.mark.parametrize("kwargs, message", [
    (dict(subject="made.up", reach=Reach.ACCOUNT, scope_id=None, context={}), "subject"),
    (dict(subject="motivation.employer_specific", reach=Reach.ACCOUNT, scope_id=None, context={}), "reach"),
    (dict(subject="motivation.employer_specific", reach=Reach.EMPLOYER, scope_id=None, context={}), "scope_id"),
    (dict(subject="demographic.eeo", reach=Reach.EMPLOYER, scope_id="name:acme", context={}), "sensitive"),
    (dict(subject="mobility.relocation", reach=Reach.SEARCH_WORKSPACE, scope_id="sw_1", context={"planet": "Mars"}), "context"),
])
def test_validation(conn, kwargs, message):
    with pytest.raises(AnswerValidationError, match=message):
        approve_answer(conn, account_id=ACCOUNT, value="x", basis=ASSERT, approved_by="u", now=NOW, **kwargs)


def test_rule_acknowledgements_latest_per_rule(conn):
    ws = make_workspace(conn)
    kw = dict(account_id=ACCOUNT, application_workspace_id=ws, rule_hash="sha256:r", observed_fingerprint="sha256:o",
              policy_version_hash="sha256:p", actor="u", now=NOW)
    record_rule_acknowledgement(conn, rule_id="perm", disposition="PROCEED", **kw)
    record_rule_acknowledgement(conn, rule_id="perm", disposition="DO_NOT_PROCEED", **kw)
    record_rule_acknowledgement(conn, rule_id="other", disposition="PROCEED", **kw)
    acks = {a["rule_id"]: a["disposition"] for a in current_rule_acknowledgements(conn, ws)}
    assert acks == {"perm": "DO_NOT_PROCEED", "other": "PROCEED"}


def test_apply_target_confirmation(conn):
    ws = make_workspace(conn)
    assert current_apply_target_confirmation(conn, ws) is None
    confirm_apply_target(conn, application_workspace_id=ws, job_identity_key="source:x:1",
                         canonical_url="https://boards.greenhouse.io/acme/jobs/1", confirmed_by="u", now=NOW)
    assert current_apply_target_confirmation(conn, ws)["canonical_url"].endswith("/jobs/1")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_autonomy_answers.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `webapp/persistence/autonomy_answers.py`**

```python
"""Approved-answer library and related user acts (6B spec §7.5, §8.1, §9.3).
Approved answers are immutable; editing supersedes. Freshness is anchored at
the latest answer_confirmations row by seq."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Mapping

from product.autonomy_contract import REACH_ORDER, Reach, canonical_json, to_utc_iso
from product.semantic_subject_policy import load_subject_policy, subject_entry


class AnswerValidationError(ValueError):
    pass


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _insert(conn, table: str, values: dict[str, Any]) -> dict[str, Any]:
    cols, marks = ", ".join(values), ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    return dict(conn.execute(f"SELECT * FROM {table} WHERE seq = ?", (cur.lastrowid,)).fetchone())


def _validate(subject: str, reach: Reach, scope_id: str | None, context: Mapping[str, Any],
              basis: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    entry = subject_entry(policy, subject)
    if entry is None:
        raise AnswerValidationError(f"unknown subject {subject!r}")
    if entry["sensitive"] is not None:
        raise AnswerValidationError(f"sensitive subject {subject!r} cannot have a standing answer in v1")
    if REACH_ORDER[reach] > REACH_ORDER[Reach(entry["max_reach"])]:
        raise AnswerValidationError(f"reach {reach.value} exceeds max_reach {entry['max_reach']} for {subject}")
    if reach is not Reach.ACCOUNT and not scope_id:
        raise AnswerValidationError(f"scope_id is required for reach {reach.value}")
    extra = set(context) - set(entry["context_keys"])
    if extra:
        raise AnswerValidationError(f"context keys {sorted(extra)} are not declared for {subject}")
    if basis.get("kind") == "USER_ASSERTION":
        if set(basis) != {"kind"}:
            raise AnswerValidationError("USER_ASSERTION basis takes no other keys")
    elif basis.get("kind") == "EVIDENCE":
        if set(basis) != {"kind", "evidence_ids", "value_hash"} or not basis["evidence_ids"]:
            raise AnswerValidationError("EVIDENCE basis needs evidence_ids and value_hash")
    else:
        raise AnswerValidationError("basis kind must be USER_ASSERTION or EVIDENCE")
    return entry


def approve_answer(conn: sqlite3.Connection, *, account_id: str, subject: str, value: Any, reach: Reach,
                   scope_id: str | None, context: dict[str, Any], basis: dict[str, Any], approved_by: str,
                   now: datetime, provenance: str = "USER", basis_profile_version_id: str | None = None,
                   supersedes_id: str | None = None, source_blocker_resolution_id: str | None = None,
                   subject_policy: Mapping[str, Any] | None = None, commit: bool = True) -> dict[str, Any]:
    entry = _validate(subject, Reach(reach), scope_id, context, basis, subject_policy or load_subject_policy())
    try:
        answer = _insert(conn, "approved_answers", {
            "id": _id("ans"), "account_id": account_id, "subject": subject,
            "answer_kind": entry["answer_kind"], "value_json": canonical_json(value),
            "reach": Reach(reach).value, "scope_id": account_id if Reach(reach) is Reach.ACCOUNT else scope_id,
            "context_json": canonical_json(context), "provenance": provenance,
            "basis_json": canonical_json(basis), "basis_profile_version_id": basis_profile_version_id,
            "supersedes_id": supersedes_id, "source_blocker_resolution_id": source_blocker_resolution_id,
            "approved_by": approved_by, "created_at": to_utc_iso(now),
        })
        _insert(conn, "answer_confirmations", {
            "id": _id("conf"), "approved_answer_id": answer["id"], "confirmed_by": approved_by,
            "created_at": to_utc_iso(now),
        })
        if commit:
            conn.commit()
    except Exception:
        if commit:
            conn.rollback()
        raise
    return answer


def confirm_answer(conn, *, approved_answer_id: str, confirmed_by: str, now: datetime,
                   commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "answer_confirmations", {
        "id": _id("conf"), "approved_answer_id": approved_answer_id, "confirmed_by": confirmed_by,
        "created_at": to_utc_iso(now),
    })
    if commit:
        conn.commit()
    return row


def current_approved_answers(conn, *, account_id: str, subject: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT a.*, ("
        "  SELECT c.created_at FROM answer_confirmations c WHERE c.approved_answer_id = a.id "
        "  ORDER BY c.seq DESC LIMIT 1) AS latest_confirmation_at, ("
        "  SELECT c.id FROM answer_confirmations c WHERE c.approved_answer_id = a.id "
        "  ORDER BY c.seq DESC LIMIT 1) AS latest_confirmation_id "
        "FROM approved_answers a WHERE a.account_id = ? AND a.subject = ? "
        "AND NOT EXISTS (SELECT 1 FROM approved_answers s WHERE s.supersedes_id = a.id) "
        "ORDER BY a.seq",
        (account_id, subject),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["value"] = json.loads(item["value_json"])
        item["context"] = json.loads(item["context_json"])
        item["basis"] = json.loads(item["basis_json"])
        out.append(item)
    return out


def save_proposed_answer(conn, *, blocker_id: str, subject: str, value: Any, now: datetime,
                         commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "proposed_answers", {
        "id": _id("prop"), "blocker_id": blocker_id, "subject": subject,
        "value_json": canonical_json(value), "provenance": "SYSTEM_PROPOSED", "created_at": to_utc_iso(now),
    })
    if commit:
        conn.commit()
    return row


def record_rule_acknowledgement(conn, *, account_id: str, application_workspace_id: str, rule_id: str,
                                rule_hash: str, observed_fingerprint: str, policy_version_hash: str,
                                disposition: str, actor: str, now: datetime, commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "rule_acknowledgements", {
        "id": _id("ack"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "rule_id": rule_id, "rule_hash": rule_hash, "observed_fingerprint": observed_fingerprint,
        "policy_version_hash": policy_version_hash, "disposition": disposition, "actor": actor,
        "created_at": to_utc_iso(now),
    })
    if commit:
        conn.commit()
    return row


def current_rule_acknowledgements(conn, application_workspace_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.* FROM rule_acknowledgements r WHERE r.application_workspace_id = ? AND r.seq = ("
        "  SELECT MAX(x.seq) FROM rule_acknowledgements x "
        "  WHERE x.application_workspace_id = r.application_workspace_id AND x.rule_id = r.rule_id) "
        "ORDER BY r.rule_id",
        (application_workspace_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def confirm_apply_target(conn, *, application_workspace_id: str, job_identity_key: str | None,
                         canonical_url: str, confirmed_by: str, now: datetime, commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "apply_target_confirmations", {
        "id": _id("atc"), "application_workspace_id": application_workspace_id,
        "job_identity_key": job_identity_key, "canonical_url": canonical_url,
        "confirmed_by": confirmed_by, "created_at": to_utc_iso(now),
    })
    if commit:
        conn.commit()
    return row


def current_apply_target_confirmation(conn, application_workspace_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM apply_target_confirmations WHERE application_workspace_id = ? ORDER BY seq DESC LIMIT 1",
        (application_workspace_id,),
    ).fetchone()
    return dict(row) if row else None
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_autonomy_answers.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/persistence/autonomy_answers.py tests/webapp/persistence/test_autonomy_answers.py
git commit -m "feat(persistence): add approved-answer library and user acknowledgements

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Persistence — decision ledger, grants, reservations, intents, attempts

**Files:**
- Create: `webapp/persistence/autonomy_ledger.py`
- Test: `tests/webapp/persistence/test_autonomy_ledger.py`

**Interfaces:**
- Consumes: Task 8 tables; `AuthorizationContext`, `AuthorizationDecision`, `canonical_json`, `canonical_hash`, `to_utc_iso`, `parse_utc`, `standing_policy.policy_hash`, `semantic_subject_policy.subject_policy_hash`.
- Produces:
  - `insert_decision(conn, *, ctx, decision, grant_id=None, commit=True) -> dict`; `list_decisions(conn, application_workspace_id) -> list[dict]` (by `seq`, with parsed `reasons`, `require_user_items`)
  - `insert_grant(conn, *, decision_id, account_id, application_workspace_id, stage: Capability, binding: dict, issued_at, expires_at, commit=True) -> dict`; `binding_fingerprint(binding) -> str`; `get_grant(conn, grant_id) -> dict | None`
  - `consume_grant(conn, *, grant_id, now) -> bool` (no commit; caller owns the transaction)
  - `revoke_grant(conn, *, grant_id, reason, now) -> bool` (no commit); `revoke_issued_grants(conn, *, account_id, reason, now) -> int` (no commit); `expire_grants(conn, *, now) -> int` (no commit)
  - `count_usage(conn, *, account_id, counter_name, window_key, since=None) -> int`; `budget_usage(conn, *, account_id, counter_name, window_key) -> Decimal`
  - `try_reserve(conn, *, account_id, counter_name, window_key, limit: int, now, since=None, amount=1, grant_id=None, attempt_id=None) -> str | None` (no commit; `None` when it would exceed `limit`)
  - `reserve_budget(conn, *, account_id, counter_name, window_key, amount: Decimal, grant_id, now) -> str` (no commit); `set_reservation_status(conn, *, reservation_id, status, now) -> None`
  - `workspace_identity(conn, workspace_id) -> tuple[str | None, IdentityStrength, bool]` — `(strong_key, strength, conflict)`; strong key is `source_record_key`, else `canonical_url_key`, else `None` with `WEAK` (latest identity row by `rowid`)
  - `live_intent(conn, *, account_id, job_identity_key) -> dict | None` (state CLAIMED/CONFIRMED, not overridden); `overridden_confirmed_intent(conn, *, account_id, job_identity_key) -> dict | None`
  - `IntentConflict(Exception)`; `claim_intent(conn, *, account_id, job_identity_key, application_workspace_id, source, now, state="CLAIMED", workflow_event_id=None) -> dict` (no commit; raises `IntentConflict`)
  - `set_intent_state(conn, *, intent_id, state, now, attempt_id=None) -> None`; `add_intent_override(conn, *, intent_id, actor, reason, now, commit=True) -> dict`
  - `ATTEMPT_TRANSITIONS: dict[str | None, set[str]]`, `InvalidAttemptTransition(Exception)`, `create_attempt(conn, *, grant_id, intent_id, application_workspace_id, run_id, now) -> dict` (no commit; writes `AUTHORIZED` event), `append_attempt_event(conn, *, attempt_id, state, source, evidence: dict, now) -> dict` (no commit), `attempt_state(conn, attempt_id) -> str | None`, `list_attempt_events(conn, attempt_id) -> list[dict]`, `attempts_for_workspace(conn, application_workspace_id) -> list[dict]`
  - `record_dry_run_case(conn, *, decision_id, application_workspace_id, adapter_id, adapter_version, manifest_hash, verification_result, now, commit=True) -> dict`; `record_dry_run_agreement(conn, *, case_id, agreement, actor, now, commit=True) -> dict`
  - `upsert_queue_item(conn, *, application_workspace_id, account_id, next_stage: Capability, next_eligible_at, now) -> None` (no commit); `wake_queue_items(conn, *, account_id, now) -> int` (no commit)

- [ ] **Step 1: Write the failing tests**

```python
# tests/webapp/persistence/test_autonomy_ledger.py
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from product.autonomy_contract import Capability
from product.autonomy_gate import evaluate_authorization
from webapp.persistence.autonomy_ledger import (
    IntentConflict, InvalidAttemptTransition, add_intent_override, append_attempt_event,
    attempt_state, budget_usage, claim_intent, consume_grant, count_usage, create_attempt,
    expire_grants, get_grant, insert_decision, insert_grant, list_decisions, live_intent,
    reserve_budget, revoke_issued_grants, set_intent_state, try_reserve,
)
from tests.product.autonomy_fixtures import make_ctx
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401


def _decision(conn, ws, **overrides):
    ctx = make_ctx(account_id=ACCOUNT, application_workspace_id=ws, **overrides)
    row = insert_decision(conn, ctx=ctx, decision=evaluate_authorization(ctx))
    return row


def _grant(conn, ws, *, stage=Capability.SUBMIT, ttl=timedelta(seconds=120)):
    decision = _decision(conn, ws)
    return insert_grant(conn, decision_id=decision["id"], account_id=ACCOUNT, application_workspace_id=ws,
                        stage=stage, binding={"pack": "art_pack"}, issued_at=NOW, expires_at=NOW + ttl)


def test_decisions_recorded_in_seq_order_with_reasons_and_hashed_inputs(conn):
    ws = make_workspace(conn)
    _decision(conn, ws)
    _decision(conn, ws, kill_switch_engaged=True)
    rows = list_decisions(conn, ws)
    assert [r["result"] for r in rows] == ["ALLOW", "DENY"]
    assert rows[1]["deny_reason"] == "kill_switch"
    assert any(r["code"] == "kill_switch" for r in rows[1]["reasons"])
    assert '"subject_policy":"sha256:' in rows[0]["inputs_json"]  # policies stored by hash, not inline


def test_grant_consumed_exactly_once_and_not_after_expiry(conn):
    ws = make_workspace(conn)
    g = _grant(conn, ws)
    assert consume_grant(conn, grant_id=g["id"], now=NOW + timedelta(seconds=10)) is True
    assert consume_grant(conn, grant_id=g["id"], now=NOW + timedelta(seconds=11)) is False
    g2 = _grant(conn, ws)
    assert consume_grant(conn, grant_id=g2["id"], now=NOW + timedelta(seconds=121)) is False
    assert expire_grants(conn, now=NOW + timedelta(seconds=121)) == 1
    assert get_grant(conn, g2["id"])["status"] == "EXPIRED"


def test_revoke_issued_only(conn):
    ws = make_workspace(conn)
    consumed, issued = _grant(conn, ws), _grant(conn, ws)
    consume_grant(conn, grant_id=consumed["id"], now=NOW)
    assert revoke_issued_grants(conn, account_id=ACCOUNT, reason="kill_switch", now=NOW) == 1
    assert get_grant(conn, consumed["id"])["status"] == "CONSUMED"
    assert get_grant(conn, issued["id"])["status"] == "REVOKED"


def test_reservations_respect_limit_and_window(conn):
    kw = dict(account_id=ACCOUNT, counter_name="submit_per_day", window_key="2026-09-24", limit=2, now=NOW)
    assert try_reserve(conn, **kw) and try_reserve(conn, **kw)
    assert try_reserve(conn, **kw) is None
    assert count_usage(conn, account_id=ACCOUNT, counter_name="submit_per_day", window_key="2026-09-24") == 2
    assert try_reserve(conn, **{**kw, "window_key": "2026-09-25"})
    old = dict(account_id=ACCOUNT, counter_name="submit_per_employer_30d", window_key="name:acme", limit=1)
    assert try_reserve(conn, now=NOW - timedelta(days=31), since=NOW - timedelta(days=61), **old)
    assert try_reserve(conn, now=NOW, since=NOW - timedelta(days=30), **old)  # 31-day-old one is outside the window
    ws = make_workspace(conn)
    g = _grant(conn, ws)
    reserve_budget(conn, account_id=ACCOUNT, counter_name="budget:LLM:day", window_key="2026-09-24",
                   amount=Decimal("0.25"), grant_id=g["id"], now=NOW)
    assert budget_usage(conn, account_id=ACCOUNT, counter_name="budget:LLM:day", window_key="2026-09-24") == Decimal("0.25")


def test_intents_unique_override_and_release(conn):
    ws = make_workspace(conn)
    kw = dict(account_id=ACCOUNT, job_identity_key="source:x:1", application_workspace_id=ws, now=NOW)
    first = claim_intent(conn, source="AUTONOMOUS", **kw)
    with pytest.raises(IntentConflict):
        claim_intent(conn, source="AUTONOMOUS", **kw)
    set_intent_state(conn, intent_id=first["id"], state="CONFIRMED", now=NOW)
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key="source:x:1")["state"] == "CONFIRMED"
    add_intent_override(conn, intent_id=first["id"], actor="u", reason="apply again", now=NOW)
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key="source:x:1") is None
    second = claim_intent(conn, source="AUTONOMOUS", **kw)
    set_intent_state(conn, intent_id=second["id"], state="RELEASED", now=NOW)
    claim_intent(conn, source="AUTONOMOUS", **kw)


def test_workspace_identity_strength(conn):
    from product.autonomy_contract import IdentityStrength
    from webapp.persistence.application_identity import save_application_identity
    from webapp.persistence.autonomy_ledger import workspace_identity
    ws = make_workspace(conn)
    assert workspace_identity(conn, ws) == (None, IdentityStrength.WEAK, False)
    save_application_identity(conn, application_workspace_id=ws, source_record={
        "company": "Acme", "title": "Eng", "location": "UK", "source_url": "https://boards.greenhouse.io/acme/jobs/1"})
    key, strength, conflict = workspace_identity(conn, ws)
    assert key.startswith("url:") and strength is IdentityStrength.CANONICAL_URL and conflict is False


def test_attempt_lifecycle_transitions(conn):
    ws = make_workspace(conn)
    g = _grant(conn, ws)
    intent = claim_intent(conn, account_id=ACCOUNT, job_identity_key="source:x:1", application_workspace_id=ws,
                          source="AUTONOMOUS", now=NOW)
    attempt = create_attempt(conn, grant_id=g["id"], intent_id=intent["id"], application_workspace_id=ws,
                             run_id=None, now=NOW)
    assert attempt_state(conn, attempt["id"]) == "AUTHORIZED"
    with pytest.raises(InvalidAttemptTransition):
        append_attempt_event(conn, attempt_id=attempt["id"], state="CONFIRMED_SUCCESS", source="EXECUTOR", evidence={}, now=NOW)
    append_attempt_event(conn, attempt_id=attempt["id"], state="CLICK_DISPATCHED", source="EXECUTOR", evidence={}, now=NOW)
    append_attempt_event(conn, attempt_id=attempt["id"], state="SUBMISSION_AMBIGUOUS", source="SERVER", evidence={}, now=NOW)
    with pytest.raises(InvalidAttemptTransition):
        append_attempt_event(conn, attempt_id=attempt["id"], state="CLICK_DISPATCHED", source="EXECUTOR", evidence={}, now=NOW)
    append_attempt_event(conn, attempt_id=attempt["id"], state="CONFIRMED_SUCCESS", source="USER", evidence={}, now=NOW)
    assert attempt_state(conn, attempt["id"]) == "CONFIRMED_SUCCESS"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_autonomy_ledger.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `webapp/persistence/autonomy_ledger.py`**

```python
"""Decision ledger, grants, reservations, intents and attempts (6B spec §10,
§12.3, §15). Functions documented "no commit" run inside a transaction owned
by the caller (the pre-click transaction in webapp/services/autonomy.py)."""
from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from product.autonomy_contract import (
    AuthorizationContext, AuthorizationDecision, Capability, IdentityStrength, canonical_hash,
    canonical_json, to_utc_iso,
)
from product.semantic_subject_policy import subject_policy_hash
from product.standing_policy import policy_hash


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _insert(conn, table: str, values: dict[str, Any]) -> dict[str, Any]:
    cols, marks = ", ".join(values), ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    return dict(conn.execute(f"SELECT * FROM {table} WHERE seq = ?", (cur.lastrowid,)).fetchone())


# ---- decisions -------------------------------------------------------------

def _inputs_payload(ctx: AuthorizationContext) -> dict[str, Any]:
    payload = {name: getattr(ctx, name) for name in ctx.__dataclass_fields__}
    payload["standing_policy"] = policy_hash(ctx.standing_policy) if ctx.standing_policy is not None else None
    payload["subject_policy"] = subject_policy_hash(ctx.subject_policy)
    return payload


def insert_decision(conn, *, ctx: AuthorizationContext, decision: AuthorizationDecision,
                    grant_id: str | None = None, commit: bool = True) -> dict[str, Any]:
    try:
        inputs_json = canonical_json(_inputs_payload(ctx))
    except Exception:  # an invalid context (e.g. naive datetime) is still recorded
        inputs_json = json.dumps({"unserializable": True})
    row = _insert(conn, "autonomy_decisions", {
        "id": _id("dec"), "account_id": ctx.account_id,
        "application_workspace_id": ctx.application_workspace_id, "run_id": ctx.run_id,
        "mode": decision.mode.value, "requested_stage": decision.requested_stage.name,
        "result": decision.result.value, "deny_reason": decision.deny_reason,
        "effective_capability": decision.effective_capability.name,
        "grantable": 1 if decision.grantable else 0,
        "reasons_json": json.dumps([[r.code, dict(r.params)] for r in decision.reasons]),
        "require_user_json": json.dumps([[i.kind, i.ref] for i in decision.require_user_items]),
        "retry_at": to_utc_iso(decision.retry_at) if decision.retry_at else None,
        "inputs_json": inputs_json, "input_fingerprint": decision.input_fingerprint,
        "engine_version": decision.engine_version, "policy_version_hash": decision.policy_version_hash,
        "subject_policy_hash": decision.subject_policy_hash, "grant_id": grant_id,
        "created_at": to_utc_iso(ctx.now) if ctx.now.tzinfo else to_utc_iso(datetime.now().astimezone()),
    })
    if commit:
        conn.commit()
    return row


def _parse_decision(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["reasons"] = [{"code": c, "params": p} for c, p in json.loads(item["reasons_json"])]
    item["require_user_items"] = [{"kind": k, "ref": r} for k, r in json.loads(item["require_user_json"])]
    return item


def list_decisions(conn, application_workspace_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM autonomy_decisions WHERE application_workspace_id = ? ORDER BY seq",
        (application_workspace_id,),
    ).fetchall()
    return [_parse_decision(r) for r in rows]


def get_decision(conn, decision_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM autonomy_decisions WHERE id = ?", (decision_id,)).fetchone()
    return _parse_decision(row) if row else None


# ---- grants ----------------------------------------------------------------

def binding_fingerprint(binding: dict[str, Any]) -> str:
    return canonical_hash("autonomy-grant-binding", "v1", binding)


def _grant_event(conn, grant_id: str, status: str, reason: str | None, now: datetime) -> None:
    _insert(conn, "autonomy_grant_events", {
        "grant_id": grant_id, "status": status, "reason": reason, "created_at": to_utc_iso(now),
    })


def insert_grant(conn, *, decision_id: str, account_id: str, application_workspace_id: str,
                 stage: Capability, binding: dict[str, Any], issued_at: datetime, expires_at: datetime,
                 commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "autonomy_grants", {
        "id": _id("grant"), "decision_id": decision_id, "account_id": account_id,
        "application_workspace_id": application_workspace_id, "stage": Capability(stage).name,
        "nonce": secrets.token_hex(16), "binding_json": canonical_json(binding),
        "binding_fingerprint": binding_fingerprint(binding), "issued_at": to_utc_iso(issued_at),
        "expires_at": to_utc_iso(expires_at), "status": "ISSUED", "consumed_at": None, "revoked_reason": None,
    })
    _grant_event(conn, row["id"], "ISSUED", None, issued_at)
    if commit:
        conn.commit()
    return row


def get_grant(conn, grant_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM autonomy_grants WHERE id = ?", (grant_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["binding"] = json.loads(item["binding_json"])
    return item


def consume_grant(conn, *, grant_id: str, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE autonomy_grants SET status = 'CONSUMED', consumed_at = ? "
        "WHERE id = ? AND status = 'ISSUED' AND expires_at > ?",
        (to_utc_iso(now), grant_id, to_utc_iso(now)),
    )
    if cur.rowcount == 1:
        _grant_event(conn, grant_id, "CONSUMED", None, now)
        return True
    return False


def revoke_grant(conn, *, grant_id: str, reason: str, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE autonomy_grants SET status = 'REVOKED', revoked_reason = ? WHERE id = ? AND status = 'ISSUED'",
        (reason, grant_id),
    )
    if cur.rowcount == 1:
        _grant_event(conn, grant_id, "REVOKED", reason, now)
        return True
    return False


def revoke_issued_grants(conn, *, account_id: str, reason: str, now: datetime) -> int:
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM autonomy_grants WHERE account_id = ? AND status = 'ISSUED' ORDER BY seq", (account_id,))]
    return sum(1 for grant_id in ids if revoke_grant(conn, grant_id=grant_id, reason=reason, now=now))


def expire_grants(conn, *, now: datetime) -> int:
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM autonomy_grants WHERE status = 'ISSUED' AND expires_at <= ? ORDER BY seq",
        (to_utc_iso(now),))]
    for grant_id in ids:
        conn.execute("UPDATE autonomy_grants SET status = 'EXPIRED' WHERE id = ? AND status = 'ISSUED'", (grant_id,))
        _grant_event(conn, grant_id, "EXPIRED", None, now)
    return len(ids)


# ---- reservations ----------------------------------------------------------

def count_usage(conn, *, account_id: str, counter_name: str, window_key: str,
                since: datetime | None = None) -> int:
    sql = ("SELECT COALESCE(SUM(CAST(amount AS INTEGER)), 0) AS n FROM limit_reservations "
           "WHERE account_id = ? AND counter_name = ? AND window_key = ? AND status IN ('RESERVED', 'CONSUMED')")
    params: list[Any] = [account_id, counter_name, window_key]
    if since is not None:
        sql += " AND created_at >= ?"
        params.append(to_utc_iso(since))
    return int(conn.execute(sql, params).fetchone()["n"])


def budget_usage(conn, *, account_id: str, counter_name: str, window_key: str) -> Decimal:
    rows = conn.execute(
        "SELECT amount FROM limit_reservations WHERE account_id = ? AND counter_name = ? AND window_key = ? "
        "AND status IN ('RESERVED', 'CONSUMED')", (account_id, counter_name, window_key),
    ).fetchall()
    return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))


def _reserve(conn, *, account_id, counter_name, window_key, amount: str, grant_id, attempt_id, now) -> str:
    row = _insert(conn, "limit_reservations", {
        "id": _id("res"), "account_id": account_id, "counter_name": counter_name, "window_key": window_key,
        "amount": amount, "grant_id": grant_id, "attempt_id": attempt_id, "status": "RESERVED",
        "created_at": to_utc_iso(now), "updated_at": to_utc_iso(now),
    })
    return row["id"]


def try_reserve(conn, *, account_id: str, counter_name: str, window_key: str, limit: int, now: datetime,
                since: datetime | None = None, amount: int = 1, grant_id: str | None = None,
                attempt_id: str | None = None) -> str | None:
    used = count_usage(conn, account_id=account_id, counter_name=counter_name, window_key=window_key, since=since)
    if used + amount > limit:
        return None
    return _reserve(conn, account_id=account_id, counter_name=counter_name, window_key=window_key,
                    amount=str(amount), grant_id=grant_id, attempt_id=attempt_id, now=now)


def reserve_budget(conn, *, account_id: str, counter_name: str, window_key: str, amount: Decimal,
                   grant_id: str | None, now: datetime) -> str:
    return _reserve(conn, account_id=account_id, counter_name=counter_name, window_key=window_key,
                    amount=str(amount), grant_id=grant_id, attempt_id=None, now=now)


def set_reservation_status(conn, *, reservation_id: str, status: str, now: datetime) -> None:
    conn.execute("UPDATE limit_reservations SET status = ?, updated_at = ? WHERE id = ?",
                 (status, to_utc_iso(now), reservation_id))


# ---- intents ---------------------------------------------------------------

class IntentConflict(Exception):
    pass


def workspace_identity(conn, workspace_id: str) -> tuple[str | None, IdentityStrength, bool]:
    """The durable job identity used for duplicate prevention. Lives in the
    persistence layer so the workflow 'applied' hook can use it without an
    import cycle. Weak-fallback-only identities have no strong key."""
    row = conn.execute(
        "SELECT source_record_key, canonical_url_key FROM application_workspace_job_identities "
        "WHERE application_workspace_id = ? ORDER BY rowid DESC LIMIT 1", (workspace_id,),
    ).fetchone()
    conflict = conn.execute(
        "SELECT 1 FROM application_job_identity_conflicts WHERE application_workspace_id = ?", (workspace_id,),
    ).fetchone() is not None
    if row and row["source_record_key"]:
        return row["source_record_key"], IdentityStrength.SOURCE_RECORD, conflict
    if row and row["canonical_url_key"]:
        return row["canonical_url_key"], IdentityStrength.CANONICAL_URL, conflict
    return None, IdentityStrength.WEAK, conflict


def live_intent(conn, *, account_id: str, job_identity_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM submission_intents WHERE account_id = ? AND job_identity_key = ? "
        "AND state IN ('CLAIMED', 'CONFIRMED') AND overridden = 0 ORDER BY seq DESC LIMIT 1",
        (account_id, job_identity_key),
    ).fetchone()
    return dict(row) if row else None


def overridden_confirmed_intent(conn, *, account_id: str, job_identity_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM submission_intents WHERE account_id = ? AND job_identity_key = ? "
        "AND state = 'CONFIRMED' AND overridden = 1 ORDER BY seq DESC LIMIT 1",
        (account_id, job_identity_key),
    ).fetchone()
    return dict(row) if row else None


def claim_intent(conn, *, account_id: str, job_identity_key: str, application_workspace_id: str, source: str,
                 now: datetime, state: str = "CLAIMED", workflow_event_id: str | None = None) -> dict[str, Any]:
    try:
        return _insert(conn, "submission_intents", {
            "id": _id("intent"), "account_id": account_id, "job_identity_key": job_identity_key,
            "application_workspace_id": application_workspace_id, "state": state, "source": source,
            "overridden": 0, "attempt_id": None, "workflow_event_id": workflow_event_id,
            "created_at": to_utc_iso(now), "updated_at": to_utc_iso(now),
        })
    except sqlite3.IntegrityError as exc:
        raise IntentConflict(job_identity_key) from exc


def set_intent_state(conn, *, intent_id: str, state: str, now: datetime, attempt_id: str | None = None) -> None:
    conn.execute(
        "UPDATE submission_intents SET state = ?, attempt_id = COALESCE(?, attempt_id), updated_at = ? WHERE id = ?",
        (state, attempt_id, to_utc_iso(now), intent_id),
    )


def add_intent_override(conn, *, intent_id: str, actor: str, reason: str, now: datetime,
                        commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "intent_overrides", {
        "id": _id("ovr"), "intent_id": intent_id, "actor": actor, "reason": reason, "created_at": to_utc_iso(now),
    })
    conn.execute("UPDATE submission_intents SET overridden = 1, updated_at = ? WHERE id = ?",
                 (to_utc_iso(now), intent_id))
    if commit:
        conn.commit()
    return row


# ---- attempts --------------------------------------------------------------

ATTEMPT_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"AUTHORIZED"},
    "AUTHORIZED": {"CLICK_DISPATCHED", "EXPIRED_UNCLICKED"},
    "CLICK_DISPATCHED": {"CONFIRMED_SUCCESS", "SUBMISSION_AMBIGUOUS", "SUBMISSION_FAILED"},
    "SUBMISSION_AMBIGUOUS": {"CONFIRMED_SUCCESS", "SUBMISSION_FAILED"},
}


class InvalidAttemptTransition(Exception):
    pass


def attempt_state(conn, attempt_id: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM submission_attempt_events WHERE attempt_id = ? ORDER BY seq DESC LIMIT 1", (attempt_id,),
    ).fetchone()
    return row["state"] if row else None


def append_attempt_event(conn, *, attempt_id: str, state: str, source: str, evidence: dict[str, Any],
                         now: datetime) -> dict[str, Any]:
    current = attempt_state(conn, attempt_id)
    if state not in ATTEMPT_TRANSITIONS.get(current, set()):
        raise InvalidAttemptTransition(f"{current} -> {state}")
    return _insert(conn, "submission_attempt_events", {
        "attempt_id": attempt_id, "state": state, "evidence_json": canonical_json(evidence),
        "source": source, "created_at": to_utc_iso(now),
    })


def create_attempt(conn, *, grant_id: str, intent_id: str, application_workspace_id: str,
                   run_id: str | None, now: datetime) -> dict[str, Any]:
    row = _insert(conn, "submission_attempts", {
        "id": _id("att"), "grant_id": grant_id, "intent_id": intent_id,
        "application_workspace_id": application_workspace_id, "run_id": run_id, "created_at": to_utc_iso(now),
    })
    append_attempt_event(conn, attempt_id=row["id"], state="AUTHORIZED", source="SERVER", evidence={}, now=now)
    return row


def list_attempt_events(conn, attempt_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM submission_attempt_events WHERE attempt_id = ? ORDER BY seq", (attempt_id,))
    return [dict(r) for r in rows]


def attempts_for_workspace(conn, application_workspace_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM submission_attempts WHERE application_workspace_id = ? ORDER BY seq", (application_workspace_id,))
    return [dict(r) for r in rows]


# ---- dry run & queue -------------------------------------------------------

def record_dry_run_case(conn, *, decision_id: str, application_workspace_id: str, adapter_id: str,
                        adapter_version: str, manifest_hash: str, verification_result: str, now: datetime,
                        commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "dry_run_submission_cases", {
        "id": _id("dry"), "decision_id": decision_id, "application_workspace_id": application_workspace_id,
        "adapter_id": adapter_id, "adapter_version": adapter_version, "manifest_hash": manifest_hash,
        "verification_result": verification_result, "created_at": to_utc_iso(now),
    })
    if commit:
        conn.commit()
    return row


def record_dry_run_agreement(conn, *, case_id: str, agreement: str, actor: str, now: datetime,
                             commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "dry_run_case_agreements", {
        "id": _id("agr"), "case_id": case_id, "agreement": agreement, "actor": actor, "created_at": to_utc_iso(now),
    })
    if commit:
        conn.commit()
    return row


def upsert_queue_item(conn, *, application_workspace_id: str, account_id: str, next_stage: Capability,
                      next_eligible_at: datetime | None, now: datetime) -> None:
    conn.execute(
        "INSERT INTO autonomy_queue_items (application_workspace_id, account_id, next_stage, next_eligible_at, "
        "paused, updated_at) VALUES (?, ?, ?, ?, 0, ?) ON CONFLICT(application_workspace_id) DO UPDATE SET "
        "next_stage = excluded.next_stage, next_eligible_at = excluded.next_eligible_at, updated_at = excluded.updated_at",
        (application_workspace_id, account_id, Capability(next_stage).name,
         to_utc_iso(next_eligible_at) if next_eligible_at else None, to_utc_iso(now)),
    )


def wake_queue_items(conn, *, account_id: str, now: datetime) -> int:
    cur = conn.execute(
        "UPDATE autonomy_queue_items SET next_eligible_at = ?, paused = 0, updated_at = ? WHERE account_id = ?",
        (to_utc_iso(now), to_utc_iso(now), account_id),
    )
    return cur.rowcount
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_autonomy_ledger.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/persistence/autonomy_ledger.py tests/webapp/persistence/test_autonomy_ledger.py
git commit -m "feat(persistence): add autonomy decision ledger, grants, reservations, intents, attempts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 12: Deployment settings and controls (kill switch, sentinel, pause/resume, enable)

**Files:**
- Modify: `webapp/config.py`
- Create: `webapp/services/autonomy_controls.py`
- Test: `tests/webapp/services/test_autonomy_controls.py`

**Interfaces:**
- Consumes: Tasks 9, 11 persistence; `Capability`; `default_policy_document`.
- Produces:
  - `Settings.autonomy_max_capability: str` (env `JOBSEARCH_AUTONOMY_MAX_CAPABILITY`, default `"NONE"`), `Settings.autonomy_submit_capable_adapters: tuple[str, ...]` (env `JOBSEARCH_AUTONOMY_SUBMIT_ADAPTERS`, comma-separated, default empty), `Settings.autonomy_live_submit_daily_cap: int = 1`, `Settings.autonomy_shadow_enabled: bool` (env `JOBSEARCH_AUTONOMY_SHADOW == "1"`), `Settings.autonomy_deployment_ceiling() -> Capability` (invalid value → `NONE`), `Settings.autonomy_sentinel_path -> Path` (`db_path.parent / "AUTONOMY_HALT"`)
  - `AutonomyHalted(Exception)`
  - `sentinel_present(path) -> bool`
  - `observe_sentinel(conn, *, account_id, sentinel_path, now) -> bool` — if present and not engaged, engages the kill switch (actor `"sentinel"`)
  - `engage_kill_switch(conn, *, account_id, actor, reason, now) -> dict` — `{"engaged": True, "revoked": int, "recorded": bool}`; `engage_kill_switch_in_transaction(...)` — same, inside a caller-held transaction (no commit)
  - `release_kill_switch(conn, *, account_id, actor, reason, now) -> dict` — `{"recorded": bool}`; never resumes
  - `resume_all(conn, *, account_id, actor, reason, now, sentinel_path) -> dict` — raises `AutonomyHalted` while the sentinel exists
  - `pause(conn, *, account_id, scope_type, scope_id, actor, reason, now) -> dict`, `resume(conn, *, account_id, scope_type, scope_id, actor, reason, now) -> dict`
  - `set_capability(conn, *, account_id, scope_type, scope_id, capability, actor, now) -> dict`
  - `enable_autonomous_preparation(conn, *, account_id, actor, timezone, now) -> dict`

- [ ] **Step 1: Write the failing tests**

```python
# tests/webapp/services/test_autonomy_controls.py
from __future__ import annotations

import pytest

from product.autonomy_contract import Capability
from webapp.config import Settings
from webapp.persistence.autonomy_authority import (
    current_policy, is_paused, kill_switch_state, resolve_authority,
)
from webapp.persistence.autonomy_ledger import get_grant, insert_decision, insert_grant
from webapp.services.autonomy_controls import (
    AutonomyHalted, enable_autonomous_preparation, engage_kill_switch, observe_sentinel,
    pause, release_kill_switch, resume, resume_all, set_capability,
)
from product.autonomy_gate import evaluate_authorization
from tests.product.autonomy_fixtures import make_ctx
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401


def test_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("JOBSEARCH_AUTONOMY_MAX_CAPABILITY", raising=False)
    s = Settings(db_path=tmp_path / "x" / "db.sqlite3")
    assert s.autonomy_deployment_ceiling() == Capability.NONE
    assert s.autonomy_sentinel_path == tmp_path / "x" / "AUTONOMY_HALT"
    assert s.autonomy_live_submit_daily_cap == 1
    monkeypatch.setenv("JOBSEARCH_AUTONOMY_MAX_CAPABILITY", "FILL")
    monkeypatch.setenv("JOBSEARCH_AUTONOMY_SUBMIT_ADAPTERS", "greenhouse,lever")
    s = Settings(db_path=tmp_path / "db.sqlite3")
    assert s.autonomy_deployment_ceiling() == Capability.FILL
    assert s.autonomy_submit_capable_adapters == ("greenhouse", "lever")
    monkeypatch.setenv("JOBSEARCH_AUTONOMY_MAX_CAPABILITY", "EVERYTHING")
    assert Settings(db_path=tmp_path / "db.sqlite3").autonomy_deployment_ceiling() == Capability.NONE


def _issued_grant(conn, ws):
    ctx = make_ctx(account_id=ACCOUNT, application_workspace_id=ws)
    d = insert_decision(conn, ctx=ctx, decision=evaluate_authorization(ctx))
    return insert_grant(conn, decision_id=d["id"], account_id=ACCOUNT, application_workspace_id=ws,
                        stage=Capability.FILL, binding={}, issued_at=NOW, expires_at=NOW.replace(year=2027))


def test_engage_revokes_issued_grants_and_is_idempotent(conn):
    ws = make_workspace(conn)
    g = _issued_grant(conn, ws)
    first = engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    assert first == {"engaged": True, "revoked": 1, "recorded": True}
    assert get_grant(conn, g["id"])["status"] == "REVOKED"
    second = engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop again", now=NOW)
    assert second["recorded"] is False and second["revoked"] == 0


def test_release_never_resumes_and_release_when_not_engaged_is_noop(conn):
    assert release_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="x", now=NOW) == {"recorded": False}
    engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    assert release_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="x", now=NOW) == {"recorded": True}
    assert kill_switch_state(conn, ACCOUNT)["halted"] is True


def test_sentinel_engages_and_blocks_resume_all_until_removed(conn, tmp_path):
    sentinel = tmp_path / "AUTONOMY_HALT"
    assert observe_sentinel(conn, account_id=ACCOUNT, sentinel_path=sentinel, now=NOW) is False
    sentinel.write_text("halt")
    assert observe_sentinel(conn, account_id=ACCOUNT, sentinel_path=sentinel, now=NOW) is True
    assert kill_switch_state(conn, ACCOUNT)["engaged"] is True
    with pytest.raises(AutonomyHalted):
        resume_all(conn, account_id=ACCOUNT, actor="u", reason="go", now=NOW, sentinel_path=sentinel)
    sentinel.unlink()
    assert kill_switch_state(conn, ACCOUNT)["halted"] is True  # removal alone never resumes
    resume_all(conn, account_id=ACCOUNT, actor="u", reason="go", now=NOW, sentinel_path=sentinel)
    assert kill_switch_state(conn, ACCOUNT)["halted"] is False


def test_pause_and_resume_are_recorded(conn):
    pause(conn, account_id=ACCOUNT, scope_type="SEARCH_WORKSPACE", scope_id="sw_1", actor="u", reason="r", now=NOW)
    assert is_paused(conn, account_id=ACCOUNT, scope_type="SEARCH_WORKSPACE", scope_id="sw_1")
    resume(conn, account_id=ACCOUNT, scope_type="SEARCH_WORKSPACE", scope_id="sw_1", actor="u", reason="r", now=NOW)
    assert not is_paused(conn, account_id=ACCOUNT, scope_type="SEARCH_WORKSPACE", scope_id="sw_1")


def test_enable_preparation_writes_explicit_records_and_never_lowers(conn):
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id="sw_new") == (Capability.PREPARE, Capability.PREPARE)
    assert current_policy(conn, ACCOUNT)["doc"]["timezone"] == "Europe/London"
    set_capability(conn, account_id=ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=ACCOUNT,
                   capability=Capability.SUBMIT, actor="u", now=NOW)
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    assert resolve_authority(conn, account_id=ACCOUNT, search_workspace_id=None)[0] == Capability.SUBMIT
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_controls.py -q`
Expected: FAIL — missing settings attributes / module.

- [ ] **Step 3: Extend `webapp/config.py`**

Add the import `from product.autonomy_contract import Capability` at the top, and these fields after `cv_quality_v2_enabled`:

```python
    # Bundle 6B deployment ceiling (spec §14.1). Operator settings can only
    # LOWER autonomy; they never grant anything beyond the user's explicit
    # authorization. Everything is off by default.
    autonomy_max_capability: str = field(
        default_factory=lambda: os.environ.get("JOBSEARCH_AUTONOMY_MAX_CAPABILITY", "NONE")
    )
    autonomy_submit_capable_adapters: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            a.strip() for a in os.environ.get("JOBSEARCH_AUTONOMY_SUBMIT_ADAPTERS", "").split(",") if a.strip()
        )
    )
    autonomy_live_submit_daily_cap: int = 1
    autonomy_shadow_enabled: bool = field(
        default_factory=lambda: os.environ.get("JOBSEARCH_AUTONOMY_SHADOW") == "1"
    )
```

and these methods after `__post_init__`:

```python
    def autonomy_deployment_ceiling(self) -> Capability:
        try:
            return Capability[self.autonomy_max_capability]
        except KeyError:
            return Capability.NONE  # fail closed on a mistyped setting

    @property
    def autonomy_sentinel_path(self) -> Path:
        return self.db_path.parent / "AUTONOMY_HALT"
```

- [ ] **Step 4: Implement `webapp/services/autonomy_controls.py`**

```python
"""Autonomy controls (6B spec §4.2, §11.4, §15.2). Every safety-relevant
control is an append-only record. Releasing a halt never resumes: only
resume_all, with the sentinel absent, makes applications eligible for fresh
evaluation."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from product.autonomy_contract import Capability
from product.standing_policy import default_policy_document
from webapp.persistence.autonomy_authority import (
    current_capability, current_policy, kill_switch_state, record_authorization,
    record_control_event, record_kill_switch, save_policy_version,
)
from webapp.persistence.autonomy_ledger import revoke_issued_grants, wake_queue_items


class AutonomyHalted(Exception):
    pass


def sentinel_present(path: Path | str) -> bool:
    return Path(path).exists()


def _in_transaction(conn: sqlite3.Connection, work):
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = work()
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def engage_kill_switch_in_transaction(conn, *, account_id: str, actor: str, reason: str,
                                     now: datetime) -> dict[str, Any]:
    """Engage inside a transaction the caller already holds (used by the
    pre-click transaction when it observes the sentinel)."""
    recorded = False
    if not kill_switch_state(conn, account_id)["engaged"]:
        record_kill_switch(conn, account_id=account_id, engaged=True, reason=reason, actor=actor, now=now, commit=False)
        recorded = True
    revoked = revoke_issued_grants(conn, account_id=account_id, reason="kill_switch", now=now)
    return {"engaged": True, "revoked": revoked, "recorded": recorded}


def engage_kill_switch(conn, *, account_id: str, actor: str, reason: str, now: datetime) -> dict[str, Any]:
    return _in_transaction(conn, lambda: engage_kill_switch_in_transaction(
        conn, account_id=account_id, actor=actor, reason=reason, now=now))


def observe_sentinel(conn, *, account_id: str, sentinel_path: Path, now: datetime) -> bool:
    present = sentinel_present(sentinel_path)
    if present and not kill_switch_state(conn, account_id)["engaged"]:
        engage_kill_switch(conn, account_id=account_id, actor="sentinel", reason=f"sentinel file present: {sentinel_path}", now=now)
    return present


def release_kill_switch(conn, *, account_id: str, actor: str, reason: str, now: datetime) -> dict[str, Any]:
    def work():
        if not kill_switch_state(conn, account_id)["engaged"]:
            return {"recorded": False}
        record_kill_switch(conn, account_id=account_id, engaged=False, reason=reason, actor=actor, now=now, commit=False)
        return {"recorded": True}
    return _in_transaction(conn, work)


def resume_all(conn, *, account_id: str, actor: str, reason: str, now: datetime, sentinel_path: Path) -> dict[str, Any]:
    if sentinel_present(sentinel_path):
        raise AutonomyHalted(f"remove the sentinel file first: {sentinel_path}")

    def work():
        state = kill_switch_state(conn, account_id)
        if state["engaged"]:
            record_kill_switch(conn, account_id=account_id, engaged=False, reason=reason, actor=actor, now=now, commit=False)
        event = record_control_event(
            conn, account_id=account_id, scope_type="ACCOUNT", scope_id=account_id, action="RESUME_ALL",
            actor=actor, reason=reason, now=now, kill_switch_seq_acknowledged=state["latest_engage_seq"], commit=False,
        )
        woken = wake_queue_items(conn, account_id=account_id, now=now)
        return {"event_id": event["id"], "woken": woken}
    return _in_transaction(conn, work)


def _control(conn, action: str, *, account_id, scope_type, scope_id, actor, reason, now) -> dict[str, Any]:
    def work():
        event = record_control_event(conn, account_id=account_id, scope_type=scope_type, scope_id=scope_id,
                                     action=action, actor=actor, reason=reason, now=now, commit=False)
        if scope_type == "APPLICATION":
            conn.execute("UPDATE autonomy_queue_items SET paused = ? WHERE application_workspace_id = ?",
                         (1 if action == "PAUSE" else 0, scope_id))
        return event
    return _in_transaction(conn, work)


def pause(conn, **kwargs) -> dict[str, Any]:
    return _control(conn, "PAUSE", **kwargs)


def resume(conn, **kwargs) -> dict[str, Any]:
    return _control(conn, "RESUME", **kwargs)


def set_capability(conn, *, account_id: str, scope_type: str, scope_id: str, capability: Capability,
                   actor: str, now: datetime) -> dict[str, Any]:
    return record_authorization(conn, account_id=account_id, scope_type=scope_type, scope_id=scope_id,
                                capability=capability, set_by=actor, now=now)


def enable_autonomous_preparation(conn, *, account_id: str, actor: str, timezone: str, now: datetime) -> dict[str, Any]:
    """The explicit enabling act (spec §4.2): writes ACCOUNT_MAX and
    DEFAULT_WORKSPACE_CEILING = PREPARE where they are absent or lower, and an
    initial standing policy if none exists. Never lowers an existing grant."""
    def work():
        written = []
        for scope_type in ("ACCOUNT_MAX", "DEFAULT_WORKSPACE_CEILING"):
            current = current_capability(conn, account_id=account_id, scope_type=scope_type, scope_id=account_id)
            if current is None or current < Capability.PREPARE:
                record_authorization(conn, account_id=account_id, scope_type=scope_type, scope_id=account_id,
                                     capability=Capability.PREPARE, set_by=actor, now=now, commit=False)
                written.append(scope_type)
        if current_policy(conn, account_id) is None:
            save_policy_version(conn, account_id=account_id, doc=default_policy_document(timezone),
                                created_by=actor, now=now, commit=False)
            written.append("standing_policy")
        return {"written": written}
    return _in_transaction(conn, work)
```

- [ ] **Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_controls.py tests/webapp/test_app_factory.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add webapp/config.py webapp/services/autonomy_controls.py tests/webapp/services/test_autonomy_controls.py
git commit -m "feat(services): add autonomy deployment ceiling, kill switch, sentinel and controls

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Context assembly

**Files:**
- Create: `webapp/services/autonomy_context.py`
- Test: `tests/webapp/services/test_autonomy_context.py`

**Interfaces:**
- Consumes: all persistence modules; `resolve_apply_target` (`webapp/services/workspace_view.py:584`), `get_search_workspace_for_application` (`webapp/persistence/application_identity.py:126`), `current_policy_decisions` / `current_application_blockers` (`webapp/services/decision_policy.py:362,398`), `get_effective_resolution`, `list_application_blockers` (`webapp/persistence/application_blockers.py`), `check_staleness` (`webapp/services/staleness.py:51`), `get_current_artifact`, `get_profile_workspace_id` (`webapp/persistence/workspaces.py:79`), `job_identity` (`product/job_identity.py:54`).
- Produces:
  - `RequirementSpec(key, subject, required, evidence_available, job_context=MappingProxy)` (frozen dataclass)
  - `ApplyTargetObservation(adapter_id, adapter_version, landing_within_redirect_set: bool, tenant_key: str | None, tenant_matches_employer: bool, ats_job_id_matches: bool | None, unexplained_redirect: bool)` (frozen dataclass; supplied by an executor from 6D)
  - `GOVERNING_ARTIFACT_TYPES`
  - `evidence_values_hash(profile_payload, evidence_ids) -> str | None`
  - `canonical_target_url(url) -> str | None`
  - `day_window(now, tz_name) -> tuple[str, datetime]` — `(local_date_iso, next_local_midnight_utc)`
  - `pack_readiness(conn, *, workspace_id, account_id, extensions_dir, unresolved: tuple) -> tuple[str | None, bool]`
  - `build_context(conn, *, settings, account_id, application_workspace_id, requested_stage, mode, now, sentinel_present, requirements=(), observation=None, executor_hard_stops=(), run_id=None, grant_binding_drift=(), cost_estimates=None) -> AuthorizationContext`

- [ ] **Step 1: Write the failing tests**

```python
# tests/webapp/services/test_autonomy_context.py
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from product.autonomy_contract import (
    UNKNOWN, Capability, EmployerKeyStrength, IdentityStrength, Mode, ProvenanceTier, Reach,
)
from webapp.config import Settings
from webapp.persistence.application_identity import save_application_identity
from webapp.persistence.artifacts import save_artifact
from webapp.persistence.autonomy_answers import approve_answer, confirm_apply_target
from webapp.persistence.autonomy_ledger import claim_intent, try_reserve
from webapp.services import autonomy_context
from webapp.services.autonomy_context import (
    RequirementSpec, build_context, canonical_target_url, day_window, evidence_values_hash,
)
from webapp.services.autonomy_controls import enable_autonomous_preparation, set_capability
from webapp.services.workspace_view import ApplyTarget
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401

URL = "https://boards.greenhouse.io/acme/jobs/123"


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "autonomy.sqlite3", autonomy_max_capability="SUBMIT",
                    autonomy_submit_capable_adapters=("greenhouse",))


def seed_workspace(conn, *, record_id="123", company="Acme"):
    ws = make_workspace(conn, company=company)
    save_artifact(conn, workspace_id=ws, artifact_type="job_posting_snapshot", payload={
        "schema_version": "job-posting-snapshot.v0", "job_id": f"jobsrc_{record_id}", "source": "greenhouse",
        "captured_at": "2026-09-20T00:00:00Z", "company": company, "title": "Drilling Fluids Engineer",
        "location": "Aberdeen, UK", "employment_type": "Permanent", "source_url": URL,
        "requirements": [], "responsibilities": [],
    })
    save_artifact(conn, workspace_id=ws, artifact_type="job_fit_result", payload={
        "overall_score": 82.5, "verdict": {"id": "strong", "display_name": "Strong", "score": 82.5},
    })
    save_application_identity(conn, application_workspace_id=ws, source_record={
        "source": "greenhouse", "source_record_id": record_id, "source_url": URL,
        "company": company, "title": "Drilling Fluids Engineer", "location": "Aberdeen, UK",
    })
    conn.commit()
    return ws


def patch_external_reads(monkeypatch):
    """Discovery origins, ATS URL provenance and pack staleness have their own
    suites; here they are fixed so these tests exercise assembly only."""
    monkeypatch.setattr(autonomy_context, "get_search_workspace_for_application", lambda c, w: "sw_1")
    monkeypatch.setattr(autonomy_context, "resolve_apply_target",
                        lambda c, workspace_id, account_id: ApplyTarget(url=URL, provenance="discovery_verified"))
    monkeypatch.setattr(autonomy_context, "pack_readiness", lambda c, **kw: ("art_pack", True))


@pytest.fixture
def seeded(conn, monkeypatch):
    patch_external_reads(monkeypatch)
    return seed_workspace(conn)


def _ctx(conn, settings, ws, **kw):
    params = dict(settings=settings, account_id=ACCOUNT, application_workspace_id=ws,
                  requested_stage=Capability.SUBMIT, mode=Mode.LIVE, now=NOW, sentinel_present=False)
    params.update(kw)
    return build_context(conn, **params)


def test_no_configuration_means_none_and_no_policy(conn, settings, seeded):
    ctx = _ctx(conn, settings, seeded)
    assert (ctx.account_max, ctx.workspace_ceiling, ctx.standing_policy) == (Capability.NONE, Capability.NONE, None)
    assert ctx.deployment_ceiling == Capability.SUBMIT


def test_attributes_identity_and_target(conn, settings, seeded):
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    ctx = _ctx(conn, settings, seeded)
    assert ctx.attributes["fit.overall_score"] == Decimal("82.5")
    assert ctx.attributes["fit.verdict"] == "strong"
    assert ctx.attributes["job.employment_type"] == "PERMANENT"
    assert ctx.attributes["company.key"] == "name:acme" and ctx.attributes["workspace.id"] == "sw_1"
    assert ctx.identity_strength is IdentityStrength.SOURCE_RECORD and ctx.identity_key.startswith("source:")
    assert ctx.employer_key_strength is EmployerKeyStrength.NORMALIZED_NAME
    assert ctx.apply_target.provenance is ProvenanceTier.DISCOVERY_VERIFIED
    assert ctx.apply_target.adapter_submit_capable is False  # no executor observation in 6B
    assert ctx.pack_auto_confirmable is True


def test_user_confirmed_target_upgrades_only_exact_url_and_identity(conn, settings, seeded, monkeypatch):
    monkeypatch.setattr(autonomy_context, "resolve_apply_target",
                        lambda c, workspace_id, account_id: ApplyTarget(url=URL, provenance="user_supplied"))
    assert _ctx(conn, settings, seeded).apply_target.provenance is ProvenanceTier.USER_SUPPLIED
    ident = _ctx(conn, settings, seeded).identity_key
    confirm_apply_target(conn, application_workspace_id=seeded, job_identity_key=ident,
                         canonical_url=canonical_target_url(URL), confirmed_by="u", now=NOW)
    assert _ctx(conn, settings, seeded).apply_target.provenance is ProvenanceTier.USER_CONFIRMED_APPLY_TARGET
    confirm_apply_target(conn, application_workspace_id=seeded, job_identity_key=ident,
                         canonical_url=canonical_target_url(URL + "0"), confirmed_by="u", now=NOW)
    assert _ctx(conn, settings, seeded).apply_target.provenance is ProvenanceTier.USER_SUPPLIED


def test_duplicate_intent_visible(conn, settings, seeded):
    ident = _ctx(conn, settings, seeded).identity_key
    claim_intent(conn, account_id=ACCOUNT, job_identity_key=ident, application_workspace_id=seeded,
                 source="HUMAN_APPLIED", state="CONFIRMED", now=NOW)
    conn.commit()
    assert _ctx(conn, settings, seeded).existing_intent_state == "CONFIRMED"


def test_counters_use_account_timezone_day_and_canary_cap(conn, settings, seeded):
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    day, _ = day_window(NOW, "Europe/London")
    try_reserve(conn, account_id=ACCOUNT, counter_name="submit_per_day", window_key=day, limit=9, now=NOW)
    conn.commit()
    counters = {c.name: c for c in _ctx(conn, settings, seeded).counters}
    assert counters["submit_per_day"].used == 1 and counters["submit_per_day"].limit == 1  # canary cap wins
    assert counters["submit_per_employer_30d"].limit == 2


@pytest.mark.parametrize("now, day, midnight_utc", [
    # BST: local midnight is 23:00 UTC the previous day.
    (datetime(2026, 9, 24, 22, 30, tzinfo=timezone.utc), "2026-09-24", datetime(2026, 9, 24, 23, 0, tzinfo=timezone.utc)),
    (datetime(2026, 9, 24, 23, 30, tzinfo=timezone.utc), "2026-09-25", datetime(2026, 9, 25, 23, 0, tzinfo=timezone.utc)),
    # Clocks go back 2026-10-25 01:00 UTC: next midnight after that is 00:00 UTC.
    (datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc), "2026-10-25", datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)),
    # Clocks go forward 2026-03-29 01:00 UTC.
    (datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc), "2026-03-28", datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)),
    (datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc), "2026-03-29", datetime(2026, 3, 29, 23, 0, tzinfo=timezone.utc)),
])
def test_day_window_across_dst(now, day, midnight_utc):
    assert day_window(now, "Europe/London") == (day, midnight_utc)


def test_requirements_get_candidates_with_basis_and_contradiction(conn, settings, seeded):
    approve_answer(conn, account_id=ACCOUNT, subject="employment.notice_period", value="1 month",
                   reach=Reach.ACCOUNT, scope_id=None, context={}, basis={"kind": "USER_ASSERTION"},
                   approved_by="u", now=NOW)
    ctx = _ctx(conn, settings, seeded, requirements=(
        RequirementSpec(key="notice", subject="employment.notice_period", required=True, evidence_available=False),))
    (req,) = ctx.requirements
    (cand,) = req.candidates
    assert cand.basis_kind == "USER_ASSERTION" and cand.contradicted is False
    assert cand.confirmed_at == NOW


def test_evidence_values_hash():
    payload = {"claims": [{"id": "ev_1", "text": "Right to work: UK"}, {"id": "ev_2", "years": 2.5}]}
    h = evidence_values_hash(payload, ["ev_1"])
    assert h and h == evidence_values_hash({"x": [{"id": "ev_1", "text": "Right to work: UK"}]}, ["ev_1"])
    assert evidence_values_hash(payload, ["ev_2"])  # floats tolerated (converted to Decimal)
    assert evidence_values_hash(payload, ["missing"]) is None
    assert evidence_values_hash(payload, ["ev_1", "missing"]) != h
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_context.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `webapp/services/autonomy_context.py`**

```python
"""Assemble an immutable AuthorizationContext from the database (6B spec
§9.2). Reads only; the gate decides. Callers that need a consistent snapshot
(grant issuance, the pre-click transaction) call this inside BEGIN IMMEDIATE."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from product.autonomy_contract import (
    UNKNOWN, AnswerCandidate, ApplyTargetFacts, AuthorizationContext, BudgetState, Capability,
    CounterState, EmployerKeyStrength, IdentityStrength, Mode, ProvenanceTier, Reach,
    RepresentationRequirement, RuleAcknowledgement, canonical_hash, normalized_employer_key, parse_utc,
)
from product.job_identity import job_identity
from product.semantic_subject_policy import load_subject_policy
from product.standing_policy import normalize_employment_type
from webapp.config import Settings
from webapp.persistence.application_blockers import get_effective_resolution, list_application_blockers
from webapp.persistence.application_identity import get_search_workspace_for_application
from webapp.persistence.artifacts import get_current_artifact
from webapp.persistence.autonomy_answers import (
    current_apply_target_confirmation, current_approved_answers, current_rule_acknowledgements,
)
from webapp.persistence.autonomy_authority import current_policy, kill_switch_state, resolve_authority
from webapp.persistence.autonomy_ledger import (
    budget_usage, count_usage, live_intent, overridden_confirmed_intent, workspace_identity,
)
from webapp.persistence.workspaces import get_profile_workspace_id, get_workspace
from webapp.services.decision_policy import current_application_blockers, current_policy_decisions
from webapp.services.staleness import check_staleness
from webapp.services.workspace_view import resolve_apply_target

GOVERNING_ARTIFACT_TYPES = ("job_understanding_result", "job_fit_result", "application_intelligence_result")


@dataclass(frozen=True)
class RequirementSpec:
    key: str
    subject: str | None
    required: bool
    evidence_available: bool
    job_context: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class ApplyTargetObservation:
    adapter_id: str
    adapter_version: str
    landing_within_redirect_set: bool
    tenant_key: str | None
    tenant_matches_employer: bool
    ats_job_id_matches: bool | None
    unexplained_redirect: bool


def _float_safe(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _float_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_float_safe(v) for v in value]
    return value


def _find_items(payload: Any, ids: set[str], found: dict[str, Any]) -> None:
    if isinstance(payload, dict):
        if payload.get("id") in ids and payload["id"] not in found:
            found[payload["id"]] = payload
        for value in payload.values():
            _find_items(value, ids, found)
    elif isinstance(payload, list):
        for value in payload:
            _find_items(value, ids, found)


def evidence_values_hash(profile_payload: Any, evidence_ids: Sequence[str]) -> str | None:
    found: dict[str, Any] = {}
    _find_items(profile_payload, set(evidence_ids), found)
    if not found:
        return None
    return canonical_hash("answer-basis", "v1", _float_safe({i: found.get(i) for i in evidence_ids}))


def canonical_target_url(url: str | None) -> str | None:
    return job_identity({"source_url": url}).canonical_url_key if url else None


def day_window(now: datetime, tz_name: str) -> tuple[str, datetime]:
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    midnight = datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=tz)
    return local.date().isoformat(), midnight.astimezone(timezone.utc)


def pack_readiness(conn, *, workspace_id: str, account_id: str, extensions_dir, unresolved: tuple) -> tuple[str | None, bool]:
    """(pack_artifact_id, auto_confirmable). Auto-confirmable means: a current
    application_pack exists, it is not stale, and no governing REQUIRE_USER
    is unresolved. Unsupported claims never enter a pack (Ticket 9 invariant
    8), so an existing current pack is grounded by construction."""
    pack = get_current_artifact(conn, workspace_id, "application_pack")
    if pack is None:
        return None, False
    stale = check_staleness(conn, workspace_id, "application_pack", extensions_dir=extensions_dir,
                            account_id=account_id)["stale"]
    return pack["id"], (not stale and not unresolved)


def _score(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return UNKNOWN
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return UNKNOWN


def _governing(conn, workspace_id: str) -> tuple[bool, tuple[str, ...]]:
    auto_reject, open_ids = False, set()
    for artifact_type in GOVERNING_ARTIFACT_TYPES:
        artifact = get_current_artifact(conn, workspace_id, artifact_type)
        if artifact is None:
            continue
        decisions = current_policy_decisions(conn, workspace_id, artifact["id"])
        auto_reject = auto_reject or any(d["outcome"] == "AUTO_REJECT" for d in decisions)
        open_ids |= {b["id"] for b in current_application_blockers(conn, workspace_id, artifact["id"])
                     if b["status"] == "open"}
    return auto_reject, tuple(sorted(open_ids))


def _contradicted(conn, workspace_id: str, answer: dict[str, Any]) -> bool:
    for blocker in list_application_blockers(conn, workspace_id):
        if blocker.get("semantic_subject_key") != answer["subject"]:
            continue
        resolution = get_effective_resolution(conn, blocker["id"])
        if resolution is None or resolution["id"] == answer["source_blocker_resolution_id"]:
            continue
        if resolution["answer_value"] != answer["value"]:
            return True
    return False


def _candidates(conn, *, account_id: str, workspace_id: str, subject: str,
                profile_payload: Any) -> tuple[AnswerCandidate, ...]:
    out = []
    for answer in current_approved_answers(conn, account_id=account_id, subject=subject):
        basis = answer["basis"]
        if basis["kind"] == "EVIDENCE":
            at_approval, current = basis["value_hash"], evidence_values_hash(profile_payload, basis["evidence_ids"])
        else:
            at_approval = current = None
        out.append(AnswerCandidate(
            approved_answer_id=answer["id"], subject=subject, reach=Reach(answer["reach"]),
            scope_id=answer["scope_id"], context=answer["context"],
            confirmed_at=parse_utc(answer["latest_confirmation_at"]), basis_kind=basis["kind"],
            basis_hash_at_approval=at_approval, basis_hash_current=current,
            contradicted=_contradicted(conn, workspace_id, answer),
        ))
    return tuple(out)


def _counters(conn, *, settings: Settings, doc: dict, account_id: str, stage: Capability, now: datetime,
              run_id: str | None, employer_key: str | None) -> tuple[CounterState, ...]:
    limits = doc["limits"]
    day, midnight = day_window(now, doc["timezone"])
    if stage == Capability.FILL:
        used = count_usage(conn, account_id=account_id, counter_name="fill_per_day", window_key=day)
        return (CounterState("fill_per_day", stage, used, limits["fill_per_day"], midnight),)
    if stage != Capability.SUBMIT:
        return ()
    counters = [CounterState(
        "submit_per_day", stage,
        count_usage(conn, account_id=account_id, counter_name="submit_per_day", window_key=day),
        min(limits["submit_per_day"], settings.autonomy_live_submit_daily_cap), midnight,
    )]
    if run_id is not None:
        counters.append(CounterState(
            "submit_per_run", stage,
            count_usage(conn, account_id=account_id, counter_name="submit_per_run", window_key=run_id),
            limits["submit_per_run"], None,
        ))
    if employer_key is not None:
        since = now - timedelta(days=30)
        used = count_usage(conn, account_id=account_id, counter_name="submit_per_employer_30d",
                           window_key=employer_key, since=since)
        oldest = conn.execute(
            "SELECT MIN(created_at) AS t FROM limit_reservations WHERE account_id = ? AND counter_name = "
            "'submit_per_employer_30d' AND window_key = ? AND status IN ('RESERVED', 'CONSUMED') AND created_at >= ?",
            (account_id, employer_key, since.astimezone(timezone.utc).isoformat(timespec="microseconds")),
        ).fetchone()["t"]
        retry = parse_utc(oldest) + timedelta(days=30) if oldest else None
        counters.append(CounterState("submit_per_employer_30d", stage, used, limits["submit_per_employer_30d"], retry))
    return tuple(counters)


def _budgets(conn, *, doc: dict, account_id: str, workspace_id: str, now: datetime,
             estimates: Mapping[str, Decimal]) -> tuple[BudgetState, ...]:
    out = []
    day, midnight = day_window(now, doc["timezone"])
    for category, estimate in sorted(estimates.items()):
        caps = doc["limits"]["budgets"].get(category)
        if caps is None:
            continue
        for window, key, cap, retry in (("day", day, caps["per_day"], midnight),
                                        ("application", workspace_id, caps["per_application"], None)):
            used = budget_usage(conn, account_id=account_id, counter_name=f"budget:{category}:{window}", window_key=key)
            out.append(BudgetState(category, window, used, Decimal("0"), Decimal(cap), Decimal(estimate), retry))
    return tuple(out)


def build_context(conn: sqlite3.Connection, *, settings: Settings, account_id: str, application_workspace_id: str,
                  requested_stage: Capability, mode: Mode, now: datetime, sentinel_present: bool,
                  requirements: Sequence[RequirementSpec] = (), observation: ApplyTargetObservation | None = None,
                  executor_hard_stops: Sequence[str] = (), run_id: str | None = None,
                  grant_binding_drift: Sequence[str] = (),
                  cost_estimates: Mapping[str, Decimal] | None = None) -> AuthorizationContext:
    ws = application_workspace_id
    search_ws = get_search_workspace_for_application(conn, ws)
    account_max, workspace_ceiling = resolve_authority(conn, account_id=account_id, search_workspace_id=search_ws)
    policy = current_policy(conn, account_id)
    doc = policy["doc"] if policy else None

    posting_artifact = get_current_artifact(conn, ws, "job_posting_snapshot")
    posting = posting_artifact["payload"] if posting_artifact else {}
    fit_artifact = get_current_artifact(conn, ws, "job_fit_result")
    fit = fit_artifact["payload"] if fit_artifact else {}
    workspace = get_workspace(conn, ws, account_id=account_id) or {}

    identity_key, identity_strength, identity_conflict = workspace_identity(conn, ws)
    if observation is not None and observation.tenant_key:
        employer_key = f"tenant:{observation.adapter_id}:{observation.tenant_key}"
        employer_strength = EmployerKeyStrength.ATS_TENANT
    else:
        employer_key = normalized_employer_key(posting.get("company") or workspace.get("company"))
        employer_strength = EmployerKeyStrength.NORMALIZED_NAME if employer_key else EmployerKeyStrength.UNKNOWN

    verdict = fit.get("verdict")
    attributes = {
        "fit.overall_score": _score(fit.get("overall_score")),
        "fit.verdict": verdict.get("id") if isinstance(verdict, dict) and verdict.get("id") else UNKNOWN,
        "job.employment_type": normalize_employment_type(posting.get("employment_type")),
        "job.location": posting.get("location") or UNKNOWN,
        "job.title": posting.get("title") or workspace.get("title") or UNKNOWN,
        "company.key": employer_key or UNKNOWN,
        "workspace.id": search_ws or UNKNOWN,
        "identity.strength": identity_strength.value,
    }

    auto_reject, unresolved = _governing(conn, ws)
    pack_id, pack_ok = pack_readiness(conn, workspace_id=ws, account_id=account_id,
                                      extensions_dir=settings.extensions_dir, unresolved=unresolved)

    target = resolve_apply_target(conn, workspace_id=ws, account_id=account_id)
    provenance = None
    if target is not None:
        provenance = ProvenanceTier(target.provenance)
        confirmation = current_apply_target_confirmation(conn, ws)
        if (confirmation and identity_key and confirmation["job_identity_key"] == identity_key
                and confirmation["canonical_url"] == canonical_target_url(target.url)):
            provenance = ProvenanceTier.USER_CONFIRMED_APPLY_TARGET
    if observation is None:
        apply_target = ApplyTargetFacts(provenance=provenance)
    else:
        apply_target = ApplyTargetFacts(
            provenance=provenance, adapter_id=observation.adapter_id,
            adapter_submit_capable=observation.adapter_id in settings.autonomy_submit_capable_adapters,
            landing_within_redirect_set=observation.landing_within_redirect_set,
            tenant_matches_employer=observation.tenant_matches_employer,
            ats_job_id_matches=observation.ats_job_id_matches,
            unexplained_redirect=observation.unexplained_redirect,
        )

    intent_state, overridden = None, False
    if identity_key:
        live = live_intent(conn, account_id=account_id, job_identity_key=identity_key)
        if live:
            intent_state = live["state"]
        elif overridden_confirmed_intent(conn, account_id=account_id, job_identity_key=identity_key):
            intent_state, overridden = "CONFIRMED", True

    profile_ws = get_profile_workspace_id(conn, account_id)
    profile_artifact = get_current_artifact(conn, profile_ws, "profile_snapshot") if profile_ws else None
    profile_payload = profile_artifact["payload"] if profile_artifact else {}
    reqs = tuple(
        RepresentationRequirement(
            key=spec.key, subject=spec.subject, required=spec.required,
            evidence_available=spec.evidence_available, job_context=dict(spec.job_context),
            candidates=(_candidates(conn, account_id=account_id, workspace_id=ws, subject=spec.subject,
                                    profile_payload=profile_payload) if spec.subject else ()),
        )
        for spec in requirements
    )

    acks = tuple(
        RuleAcknowledgement(a["rule_id"], a["rule_hash"], a["observed_fingerprint"], a["disposition"])
        for a in current_rule_acknowledgements(conn, ws)
    )
    counters = budgets = ()
    if doc is not None:
        counters = _counters(conn, settings=settings, doc=doc, account_id=account_id, stage=requested_stage,
                             now=now, run_id=run_id, employer_key=employer_key)
        budgets = _budgets(conn, doc=doc, account_id=account_id, workspace_id=ws, now=now,
                           estimates=cost_estimates or {})

    return AuthorizationContext(
        mode=mode, requested_stage=requested_stage, now=now, account_id=account_id,
        application_workspace_id=ws, search_workspace_id=search_ws,
        deployment_ceiling=settings.autonomy_deployment_ceiling(), account_max=account_max,
        workspace_ceiling=workspace_ceiling,
        kill_switch_engaged=kill_switch_state(conn, account_id)["halted"], sentinel_present=sentinel_present,
        standing_policy=doc, subject_policy=load_subject_policy(), attributes=attributes,
        governing_auto_reject=auto_reject, unresolved_governing_require_user=unresolved,
        pack_artifact_id=pack_id, pack_auto_confirmable=pack_ok, requirements=reqs,
        apply_target=apply_target, identity_key=identity_key, identity_strength=identity_strength,
        identity_conflict=identity_conflict, existing_intent_state=intent_state, intent_overridden=overridden,
        employer_key=employer_key, employer_key_strength=employer_strength, counters=counters,
        budgets=budgets, rule_acknowledgements=acks, executor_hard_stops=tuple(executor_hard_stops),
        grant_binding_drift=tuple(grant_binding_drift), run_id=run_id,
    )
```


- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_context.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/services/autonomy_context.py tests/webapp/services/test_autonomy_context.py
git commit -m "feat(services): assemble autonomy authorization context from the database

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: Decide-and-record and grant issuance

**Files:**
- Create: `webapp/services/autonomy.py`
- Test: `tests/webapp/services/test_autonomy_decide.py`

**Interfaces:**
- Consumes: `build_context`, `RequirementSpec`, `ApplyTargetObservation` (Task 13); `evaluate_authorization`; ledger (Task 11); `observe_sentinel` (Task 12); `validate_fill_manifest`, `manifest_hash`.
- Produces:
  - `GrantOutcome(decision: AuthorizationDecision, decision_row: dict, grant: dict | None)` (frozen dataclass)
  - `build_binding(ctx, *, stage: Capability, fill_manifest: dict | None) -> dict`
  - `binding_drift(expected: dict, actual: dict) -> tuple[str, ...]`
  - `decide_and_record(conn, *, settings, account_id, application_workspace_id, requested_stage, mode, now, **context_kwargs) -> tuple[AuthorizationDecision, dict]` (own transaction)
  - `request_grant(conn, *, settings, account_id, application_workspace_id, stage: Capability, now, fill_manifest: dict | None, requirements=(), observation=None, run_id=None, cost_estimates=None) -> GrantOutcome` (own `BEGIN IMMEDIATE`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/webapp/services/test_autonomy_decide.py
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from product.autonomy_contract import Capability, Mode, ResultKind
from product.fill_manifest import value_hash
from webapp.persistence.autonomy_ledger import count_usage, get_grant, list_decisions
from webapp.services.autonomy import binding_drift, decide_and_record, request_grant
from webapp.services.autonomy_context import ApplyTargetObservation, day_window
from webapp.services.autonomy_controls import (
    enable_autonomous_preparation, engage_kill_switch, set_capability,
)
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401
from tests.webapp.services.test_autonomy_context import URL, seeded, settings  # noqa: F401

OBS = ApplyTargetObservation(adapter_id="greenhouse", adapter_version="1.0.0", landing_within_redirect_set=True,
                             tenant_key=None, tenant_matches_employer=True, ats_job_id_matches=True,
                             unexplained_redirect=False)


def manifest(ws):
    return {"schema_version": "fill-manifest.v1", "application_workspace_id": ws, "adapter_id": "greenhouse",
            "adapter_version": "1.0.0", "pages": [{"page_key": "p1", "entries": [
                {"page_field_key": "email", "normalized_field_type": "email", "subject": None,
                 "source": {"kind": "EVIDENCE", "ref": "contact.email", "confirmation_id": None},
                 "transform_id": "identity", "value_hash": value_hash("a@b.c"), "required": True}]}]}


def authorize_all(conn):
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    for scope_type, scope_id in (("ACCOUNT_MAX", ACCOUNT), ("WORKSPACE_CEILING", "sw_1")):
        set_capability(conn, account_id=ACCOUNT, scope_type=scope_type, scope_id=scope_id,
                       capability=Capability.SUBMIT, actor="u", now=NOW)


def test_every_evaluation_is_recorded_including_denials(conn, settings, seeded):
    decide_and_record(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                      requested_stage=Capability.PREPARE, mode=Mode.SHADOW, now=NOW)
    engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    decide_and_record(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                      requested_stage=Capability.PREPARE, mode=Mode.LIVE, now=NOW)
    rows = list_decisions(conn, seeded)
    assert [(r["mode"], r["result"]) for r in rows] == [("SHADOW", "ALLOW"), ("LIVE", "DENY")]


def test_fill_grant_issued_with_ttl_and_reservation(conn, settings, seeded):
    authorize_all(conn)
    out = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                        stage=Capability.FILL, now=NOW, fill_manifest=manifest(seeded), observation=OBS)
    assert out.decision.grantable and out.grant["stage"] == "FILL"
    assert out.grant["expires_at"].startswith("2026-09-24T12:30:00")
    day, _ = day_window(NOW, "Europe/London")
    assert count_usage(conn, account_id=ACCOUNT, counter_name="fill_per_day", window_key=day) == 1


def test_no_grant_when_not_grantable(conn, settings, seeded):
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    out = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                        stage=Capability.FILL, now=NOW, fill_manifest=manifest(seeded), observation=OBS)
    assert out.grant is None and out.decision.effective_capability == Capability.PREPARE


def test_submit_grant_binds_manifest_target_identity_and_policy(conn, settings, seeded):
    authorize_all(conn)
    out = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                        stage=Capability.SUBMIT, now=NOW, fill_manifest=manifest(seeded), observation=OBS)
    assert out.decision.grantable, out.decision.reasons
    binding = get_grant(conn, out.grant["id"])["binding"]
    assert set(binding) >= {"stage", "pack_artifact_id", "fill_manifest", "fill_manifest_hash", "answers",
                            "apply_target", "identity_key", "policy_version_hash", "subject_policy_hash",
                            "engine_version", "account_id", "application_workspace_id"}
    assert out.grant["expires_at"].startswith("2026-09-24T12:02:00")


def test_submit_requires_manifest(conn, settings, seeded):
    authorize_all(conn)
    with pytest.raises(ValueError):
        request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                      stage=Capability.SUBMIT, now=NOW, fill_manifest=None, observation=OBS)


def test_binding_drift_lists_changed_keys():
    assert binding_drift({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == ("b", "c")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_decide.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the first half of `webapp/services/autonomy.py`**

```python
"""Autonomy orchestration (6B spec §9.6, §10). Decisions are always recorded
(including denials); grants are issued only for grantable LIVE decisions and
are bound to the exact action inputs; the pre-click transaction (below) is
the authorization point of no return."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from product.autonomy_contract import (
    CLICK_DISPATCH_TTL, ENGINE_VERSION, FILL_SESSION_TTL, SUBMIT_GRANT_TTL, AuthorizationContext,
    AuthorizationDecision, Capability, Mode, canonical_json, to_utc_iso,
)
from product.autonomy_gate import evaluate_authorization
from product.fill_manifest import manifest_hash, validate_fill_manifest
from product.semantic_subject_policy import subject_policy_hash
from product.standing_policy import policy_hash
from webapp.config import Settings
from webapp.persistence.autonomy_ledger import insert_decision, insert_grant, reserve_budget, try_reserve
from webapp.services.autonomy_context import (
    ApplyTargetObservation, RequirementSpec, build_context, day_window,
)
from webapp.services.autonomy_controls import observe_sentinel


@dataclass(frozen=True)
class GrantOutcome:
    decision: AuthorizationDecision
    decision_row: dict[str, Any]
    grant: dict[str, Any] | None


def build_binding(ctx: AuthorizationContext, *, stage: Capability, fill_manifest: dict | None) -> dict[str, Any]:
    target = ctx.apply_target
    return {
        "stage": stage.name,
        "account_id": ctx.account_id,
        "application_workspace_id": ctx.application_workspace_id,
        "pack_artifact_id": ctx.pack_artifact_id,
        "fill_manifest": fill_manifest,
        "fill_manifest_hash": manifest_hash(fill_manifest) if fill_manifest is not None else None,
        "answers": sorted(
            [c.approved_answer_id, to_utc_iso(c.confirmed_at)]
            for r in ctx.requirements for c in r.candidates
        ),
        "apply_target": {
            "provenance": target.provenance.value if target.provenance else None,
            "adapter_id": target.adapter_id,
        },
        "employer_key": ctx.employer_key,
        "identity_key": ctx.identity_key,
        "policy_version_hash": None if ctx.standing_policy is None else policy_hash(ctx.standing_policy),
        "subject_policy_hash": subject_policy_hash(ctx.subject_policy),
        "engine_version": ENGINE_VERSION,
    }


def binding_drift(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> tuple[str, ...]:
    keys = set(expected) | set(actual)
    return tuple(sorted(k for k in keys if canonical_json(expected.get(k)) != canonical_json(actual.get(k))))


def _transaction(conn: sqlite3.Connection, work):
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = work()
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def decide_and_record(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                      requested_stage: Capability, mode: Mode, now: datetime,
                      **context_kwargs: Any) -> tuple[AuthorizationDecision, dict[str, Any]]:
    sentinel = observe_sentinel(conn, account_id=account_id, sentinel_path=settings.autonomy_sentinel_path, now=now)

    def work():
        ctx = build_context(conn, settings=settings, account_id=account_id,
                            application_workspace_id=application_workspace_id, requested_stage=requested_stage,
                            mode=mode, now=now, sentinel_present=sentinel, **context_kwargs)
        decision = evaluate_authorization(ctx)
        return decision, insert_decision(conn, ctx=ctx, decision=decision, commit=False)
    return _transaction(conn, work)


def request_grant(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                  stage: Capability, now: datetime, fill_manifest: dict | None,
                  requirements: Sequence[RequirementSpec] = (), observation: ApplyTargetObservation | None = None,
                  run_id: str | None = None, cost_estimates: Mapping[str, Decimal] | None = None) -> GrantOutcome:
    if stage not in (Capability.FILL, Capability.SUBMIT):
        raise ValueError("grants exist only for FILL and SUBMIT")
    if stage == Capability.SUBMIT and fill_manifest is None:
        raise ValueError("a SUBMIT grant must bind a fill manifest")
    if fill_manifest is not None:
        validate_fill_manifest(fill_manifest)
    sentinel = observe_sentinel(conn, account_id=account_id, sentinel_path=settings.autonomy_sentinel_path, now=now)

    def work():
        ctx = build_context(conn, settings=settings, account_id=account_id,
                            application_workspace_id=application_workspace_id, requested_stage=stage,
                            mode=Mode.LIVE, now=now, sentinel_present=sentinel, requirements=requirements,
                            observation=observation, run_id=run_id, cost_estimates=cost_estimates)
        decision = evaluate_authorization(ctx)
        row = insert_decision(conn, ctx=ctx, decision=decision, commit=False)
        if not decision.grantable:
            return GrantOutcome(decision, row, None)
        ttl = FILL_SESSION_TTL if stage == Capability.FILL else SUBMIT_GRANT_TTL
        grant = insert_grant(conn, decision_id=row["id"], account_id=account_id,
                             application_workspace_id=application_workspace_id, stage=stage,
                             binding=build_binding(ctx, stage=stage, fill_manifest=fill_manifest),
                             issued_at=now, expires_at=now + ttl, commit=False)
        if stage == Capability.FILL:
            day, _ = day_window(now, ctx.standing_policy["timezone"])
            reserved = try_reserve(conn, account_id=account_id, counter_name="fill_per_day", window_key=day,
                                   limit=ctx.standing_policy["limits"]["fill_per_day"], now=now, grant_id=grant["id"])
            if reserved is None:  # impossible under BEGIN IMMEDIATE unless the gate is wrong
                raise RuntimeError("fill_per_day reservation failed after an ALLOW decision")
        for budget in ctx.budgets:
            key = day_window(now, ctx.standing_policy["timezone"])[0] if budget.window == "day" else application_workspace_id
            reserve_budget(conn, account_id=account_id, counter_name=f"budget:{budget.category}:{budget.window}",
                           window_key=key, amount=budget.estimate, grant_id=grant["id"], now=now)
        return GrantOutcome(decision, row, grant)
    return _transaction(conn, work)
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_decide.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/services/autonomy.py tests/webapp/services/test_autonomy_decide.py
git commit -m "feat(services): record autonomy decisions and issue bound grants

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 15: Pre-click transaction, dispatch, expiry and attempt results

**Files:**
- Modify: `webapp/services/autonomy.py` (append)
- Test: `tests/webapp/services/test_autonomy_preclick.py`, `tests/webapp/services/test_autonomy_preclick_concurrency.py`

**Interfaces:**
- Consumes: Tasks 11–14.
- Produces:
  - `PreClickResult(authorized: bool, attempt_id: str | None, decision_id: str, reason: str | None)` (frozen dataclass)
  - `pre_click_commit(conn, *, settings, grant_id, verification: Mapping[str, str], now, requirements=(), observation=None, run_id=None) -> PreClickResult` — `verification` maps `page_field_key -> value_hash` as read back from the page
  - `record_click_dispatched(conn, *, attempt_id, now) -> bool` — the server acknowledgement; `False` means **do not click**
  - `expire_unclicked(conn, *, now) -> int`
  - `mark_stale_dispatches_ambiguous(conn, *, now, result_timeout: timedelta) -> int`
  - `record_submission_result(conn, *, attempt_id, state, source, evidence: dict, now) -> str` — returns the state actually recorded
  - `resolve_ambiguous(conn, *, attempt_id, submitted: bool, actor, now) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/webapp/services/test_autonomy_preclick.py
from __future__ import annotations

from datetime import timedelta

import pytest

from product.autonomy_contract import Capability
from product.standing_policy import default_policy_document
from webapp.persistence.autonomy_authority import save_policy_version
from webapp.persistence.autonomy_ledger import (
    attempt_state, count_usage, get_grant, list_decisions, live_intent,
)
from webapp.services.autonomy import (
    expire_unclicked, mark_stale_dispatches_ambiguous, pre_click_commit, record_click_dispatched,
    record_submission_result, request_grant, resolve_ambiguous,
)
from webapp.services.autonomy_controls import engage_kill_switch
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401
from tests.webapp.services.test_autonomy_context import seeded, settings  # noqa: F401
from tests.webapp.services.test_autonomy_decide import OBS, authorize_all, manifest

T = NOW + timedelta(seconds=30)


def verification(ws):
    return {e["page_field_key"]: e["value_hash"] for p in manifest(ws)["pages"] for e in p["entries"]}


def submit_grant(conn, settings, ws):
    authorize_all(conn)
    out = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=ws,
                        stage=Capability.SUBMIT, now=NOW, fill_manifest=manifest(ws), observation=OBS)
    assert out.grant, out.decision.reasons
    return out.grant["id"]


def click(conn, settings, ws, grant_id, *, now=T, verify=None):
    return pre_click_commit(conn, settings=settings, grant_id=grant_id,
                            verification=verify if verify is not None else verification(ws),
                            now=now, observation=OBS)


def test_happy_path_authorizes_once(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    result = click(conn, settings, seeded, gid)
    assert result.authorized and result.attempt_id
    assert attempt_state(conn, result.attempt_id) == "AUTHORIZED"
    assert get_grant(conn, gid)["status"] == "CONSUMED"
    ident = list_decisions(conn, seeded)[-1]
    assert ident["grant_id"] == gid
    again = click(conn, settings, seeded, gid)
    assert not again.authorized and again.reason == "duplicate"


def test_verification_mismatch_revokes(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    result = click(conn, settings, seeded, gid, verify={"email": "sha256:other"})
    assert (result.authorized, result.reason) == (False, "stale_binding")
    assert get_grant(conn, gid)["status"] == "REVOKED"


def test_policy_change_after_grant_is_drift(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    doc = default_policy_document("Europe/London")
    doc["limits"]["fill_per_day"] = 9
    save_policy_version(conn, account_id=ACCOUNT, doc=doc, created_by="u", now=NOW)
    assert click(conn, settings, seeded, gid).reason == "stale_binding"


def test_kill_switch_before_commit_means_no_click(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    assert click(conn, settings, seeded, gid).reason == "kill_switch"


def test_sentinel_checked_synchronously(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    settings.autonomy_sentinel_path.write_text("halt")
    assert click(conn, settings, seeded, gid).reason == "kill_switch"


def test_expired_grant_not_consumable(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    assert click(conn, settings, seeded, gid, now=NOW + timedelta(seconds=121)).reason == "grant_not_consumable"


def test_dispatch_ack_and_unclicked_expiry(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = click(conn, settings, seeded, gid).attempt_id
    assert record_click_dispatched(conn, attempt_id=attempt, now=T + timedelta(seconds=61)) is False
    assert attempt_state(conn, attempt) == "EXPIRED_UNCLICKED"
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key="source:greenhouse:123") is None
    assert count_usage(conn, account_id=ACCOUNT, counter_name="submit_per_day", window_key="2026-09-24") == 0


def test_expire_unclicked_sweep(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = click(conn, settings, seeded, gid).attempt_id
    assert expire_unclicked(conn, now=T + timedelta(seconds=30)) == 0
    assert expire_unclicked(conn, now=T + timedelta(seconds=60)) == 1
    assert attempt_state(conn, attempt) == "EXPIRED_UNCLICKED"


def test_kill_switch_after_commit_lets_authorized_click_complete(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = click(conn, settings, seeded, gid).attempt_id
    engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=T + timedelta(seconds=1))
    assert record_click_dispatched(conn, attempt_id=attempt, now=T + timedelta(seconds=2)) is True


def test_results_and_ambiguity(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = click(conn, settings, seeded, gid).attempt_id
    record_click_dispatched(conn, attempt_id=attempt, now=T)
    recorded = record_submission_result(conn, attempt_id=attempt, state="SUBMISSION_FAILED", source="EXECUTOR",
                                        evidence={"page": "timeout"}, now=T)
    assert recorded == "SUBMISSION_AMBIGUOUS"  # failure without proof is never retried
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key="source:greenhouse:123")["state"] == "CLAIMED"
    assert resolve_ambiguous(conn, attempt_id=attempt, submitted=True, actor="u", now=T) == "CONFIRMED_SUCCESS"
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key="source:greenhouse:123")["state"] == "CONFIRMED"
    assert count_usage(conn, account_id=ACCOUNT, counter_name="submit_per_day", window_key="2026-09-24") == 1


def test_proven_failure_releases(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = click(conn, settings, seeded, gid).attempt_id
    record_click_dispatched(conn, attempt_id=attempt, now=T)
    assert record_submission_result(conn, attempt_id=attempt, state="SUBMISSION_FAILED", source="EXECUTOR",
                                    evidence={"proven_not_submitted": True, "errors": ["phone required"]}, now=T) == "SUBMISSION_FAILED"
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key="source:greenhouse:123") is None


def test_dispatched_without_result_becomes_ambiguous(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = click(conn, settings, seeded, gid).attempt_id
    record_click_dispatched(conn, attempt_id=attempt, now=T)
    assert mark_stale_dispatches_ambiguous(conn, now=T + timedelta(minutes=5), result_timeout=timedelta(minutes=10)) == 0
    assert mark_stale_dispatches_ambiguous(conn, now=T + timedelta(minutes=11), result_timeout=timedelta(minutes=10)) == 1
    assert attempt_state(conn, attempt) == "SUBMISSION_AMBIGUOUS"
```

```python
# tests/webapp/services/test_autonomy_preclick_concurrency.py
"""Two workers, one authorization: exactly one may proceed (spec §17)."""
from __future__ import annotations

import threading

from product.autonomy_contract import Capability
from webapp.persistence.db import connect
from webapp.services.autonomy import pre_click_commit, request_grant
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn  # noqa: F401
from tests.webapp.services.test_autonomy_context import patch_external_reads, seed_workspace, settings  # noqa: F401
from tests.webapp.services.test_autonomy_decide import OBS, authorize_all, manifest
from tests.webapp.services.test_autonomy_preclick import T, verification


def race(settings, calls):
    barrier = threading.Barrier(len(calls))
    results = [None] * len(calls)

    def run(i, grant_id, ws):
        c = connect(settings.db_path)
        try:
            barrier.wait()
            results[i] = pre_click_commit(c, settings=settings, grant_id=grant_id,
                                          verification=verification(ws), now=T, observation=OBS)
        finally:
            c.close()

    threads = [threading.Thread(target=run, args=(i, g, ws)) for i, (g, ws) in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def _grant(conn, settings, ws):
    return request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=ws,
                         stage=Capability.SUBMIT, now=NOW, fill_manifest=manifest(ws), observation=OBS).grant["id"]


def test_two_workers_one_grant(conn, settings, monkeypatch):
    patch_external_reads(monkeypatch)
    ws = seed_workspace(conn)
    authorize_all(conn)
    gid = _grant(conn, settings, ws)
    results = race(settings, [(gid, ws), (gid, ws)])
    assert sorted(r.authorized for r in results) == [False, True]


def test_two_workers_last_daily_slot(conn, settings, monkeypatch):
    patch_external_reads(monkeypatch)
    ws1 = seed_workspace(conn, record_id="1", company="Acme")
    ws2 = seed_workspace(conn, record_id="2", company="Globex")
    authorize_all(conn)
    g1, g2 = _grant(conn, settings, ws1), _grant(conn, settings, ws2)
    results = race(settings, [(g1, ws1), (g2, ws2)])  # canary cap = 1/day
    assert sorted(r.authorized for r in results) == [False, True]
    loser = next(r for r in results if not r.authorized)
    assert loser.reason == "limit"


def test_kill_switch_racing_pre_click_is_never_lost(conn, settings, monkeypatch):
    """Either the kill switch committed first (no click) or the attempt
    committed first and the later engagement is recorded after it."""
    from webapp.services.autonomy_controls import engage_kill_switch
    patch_external_reads(monkeypatch)
    ws = seed_workspace(conn)
    authorize_all(conn)
    gid = _grant(conn, settings, ws)
    barrier = threading.Barrier(2)
    out = {}

    def click():
        c = connect(settings.db_path)
        try:
            barrier.wait()
            out["click"] = pre_click_commit(c, settings=settings, grant_id=gid, verification=verification(ws),
                                            now=T, observation=OBS)
        finally:
            c.close()

    def kill():
        c = connect(settings.db_path)
        try:
            barrier.wait()
            engage_kill_switch(c, account_id=ACCOUNT, actor="u", reason="race", now=T)
        finally:
            c.close()

    threads = [threading.Thread(target=click), threading.Thread(target=kill)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    result = out["click"]
    kill_seq = conn.execute("SELECT MAX(seq) FROM autonomy_kill_switch WHERE engaged = 1").fetchone()[0]
    assert kill_seq is not None
    if result.authorized:
        attempt_seq = conn.execute("SELECT seq FROM submission_attempts WHERE id = ?",
                                   (result.attempt_id,)).fetchone()[0]
        assert attempt_seq is not None  # the attempt exists and keeps its lifecycle
    else:
        assert result.reason in ("kill_switch", "grant_not_consumable")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_preclick.py tests/webapp/services/test_autonomy_preclick_concurrency.py -q`
Expected: FAIL — `ImportError` for `pre_click_commit`.

- [ ] **Step 3: Append to `webapp/services/autonomy.py`**

Add these imports to the module's import block:

```python
import dataclasses
from datetime import timedelta

from product.autonomy_contract import parse_utc
from webapp.persistence.autonomy_authority import kill_switch_state
from webapp.persistence.autonomy_ledger import (
    append_attempt_event, attempt_state, claim_intent, consume_grant, create_attempt, get_grant,
    revoke_grant, set_intent_state, set_reservation_status,
)
from webapp.services.autonomy_controls import engage_kill_switch_in_transaction, sentinel_present
```

Then append:

```python
@dataclass(frozen=True)
class PreClickResult:
    authorized: bool
    attempt_id: str | None
    decision_id: str
    reason: str | None


def _verification_matches(fill_manifest: dict, verification: Mapping[str, str]) -> bool:
    expected = {e["page_field_key"]: e["value_hash"] for page in fill_manifest["pages"] for e in page["entries"]}
    return dict(verification) == expected


def pre_click_commit(conn, *, settings: Settings, grant_id: str, verification: Mapping[str, str], now: datetime,
                     requirements: Sequence[RequirementSpec] = (), observation: ApplyTargetObservation | None = None,
                     run_id: str | None = None) -> PreClickResult:
    """Spec §10.3: one BEGIN IMMEDIATE transaction re-checks the kill switch and
    sentinel, re-evaluates against current state, compares with the grant
    binding and the verification snapshot, reserves limits, consumes the
    single-use grant, claims the intent and creates the AUTHORIZED attempt.
    Commit is the authorization point of no return -- not proof of submission."""
    initial = get_grant(conn, grant_id)
    if initial is None or initial["stage"] != "SUBMIT":
        raise ValueError(f"{grant_id!r} is not a SUBMIT grant")
    account_id, ws = initial["account_id"], initial["application_workspace_id"]

    def work() -> PreClickResult:
        sentinel = sentinel_present(settings.autonomy_sentinel_path)
        if sentinel and not kill_switch_state(conn, account_id)["engaged"]:
            engage_kill_switch_in_transaction(conn, account_id=account_id, actor="sentinel",
                                              reason="sentinel file present at pre-click", now=now)
        grant = get_grant(conn, grant_id)
        fill_manifest = grant["binding"]["fill_manifest"]
        ctx = build_context(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                            requested_stage=Capability.SUBMIT, mode=Mode.LIVE, now=now, sentinel_present=sentinel,
                            requirements=requirements, observation=observation, run_id=run_id)
        drift = list(binding_drift(grant["binding"], build_binding(ctx, stage=Capability.SUBMIT,
                                                                    fill_manifest=fill_manifest)))
        if not _verification_matches(fill_manifest, verification):
            drift.append("verification_snapshot")
        ctx = dataclasses.replace(ctx, grant_binding_drift=tuple(drift))
        decision = evaluate_authorization(ctx)
        row = insert_decision(conn, ctx=ctx, decision=decision, grant_id=grant_id, commit=False)
        if not decision.grantable:
            reason = decision.deny_reason or decision.result.value
            revoke_grant(conn, grant_id=grant_id, reason=f"pre_click:{reason}", now=now)
            return PreClickResult(False, None, row["id"], reason)
        if not consume_grant(conn, grant_id=grant_id, now=now):
            return PreClickResult(False, None, row["id"], "grant_not_consumable")
        doc = ctx.standing_policy
        day, _ = day_window(now, doc["timezone"])
        reservations = [("submit_per_day", day, min(doc["limits"]["submit_per_day"],
                                                     settings.autonomy_live_submit_daily_cap), None)]
        if run_id is not None:
            reservations.append(("submit_per_run", run_id, doc["limits"]["submit_per_run"], None))
        reservations.append(("submit_per_employer_30d", ctx.employer_key,
                             doc["limits"]["submit_per_employer_30d"], now - timedelta(days=30)))
        for name, key, limit, since in reservations:
            if try_reserve(conn, account_id=account_id, counter_name=name, window_key=key, limit=limit,
                           now=now, since=since, grant_id=grant_id) is None:
                raise RuntimeError(f"{name} reservation failed after an ALLOW decision")
        intent = claim_intent(conn, account_id=account_id, job_identity_key=ctx.identity_key,
                              application_workspace_id=ws, source="AUTONOMOUS", now=now)
        attempt = create_attempt(conn, grant_id=grant_id, intent_id=intent["id"], application_workspace_id=ws,
                                 run_id=run_id, now=now)
        set_intent_state(conn, intent_id=intent["id"], state="CLAIMED", now=now, attempt_id=attempt["id"])
        return PreClickResult(True, attempt["id"], row["id"], None)

    return _transaction(conn, work)


def _attempt(conn, attempt_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM submission_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if row is None:
        raise LookupError(attempt_id)
    return dict(row)


def _settle_reservations(conn, grant_id: str, status: str, now: datetime) -> None:
    for row in conn.execute("SELECT id FROM limit_reservations WHERE grant_id = ? AND status = 'RESERVED'",
                            (grant_id,)).fetchall():
        set_reservation_status(conn, reservation_id=row["id"], status=status, now=now)


def _release(conn, attempt: dict[str, Any], now: datetime) -> None:
    set_intent_state(conn, intent_id=attempt["intent_id"], state="RELEASED", now=now)
    _settle_reservations(conn, attempt["grant_id"], "RELEASED", now)


def _confirm(conn, attempt: dict[str, Any], now: datetime) -> None:
    set_intent_state(conn, intent_id=attempt["intent_id"], state="CONFIRMED", now=now)
    _settle_reservations(conn, attempt["grant_id"], "CONSUMED", now)


def record_click_dispatched(conn, *, attempt_id: str, now: datetime) -> bool:
    """Server acknowledgement that CLICK_DISPATCHED is durable. The executor
    must not click unless this returns True."""
    def work() -> bool:
        attempt = _attempt(conn, attempt_id)
        if attempt_state(conn, attempt_id) != "AUTHORIZED":
            return False
        if now - parse_utc(attempt["created_at"]) >= CLICK_DISPATCH_TTL:
            append_attempt_event(conn, attempt_id=attempt_id, state="EXPIRED_UNCLICKED", source="SERVER",
                                 evidence={"reason": "dispatch_ttl_elapsed"}, now=now)
            _release(conn, attempt, now)
            return False
        append_attempt_event(conn, attempt_id=attempt_id, state="CLICK_DISPATCHED", source="EXECUTOR",
                             evidence={}, now=now)
        return True
    return _transaction(conn, work)


def _attempts_in_state(conn, state: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT a.* FROM submission_attempts a WHERE ("
        "  SELECT e.state FROM submission_attempt_events e WHERE e.attempt_id = a.id ORDER BY e.seq DESC LIMIT 1"
        ") = ? ORDER BY a.seq", (state,),
    ).fetchall()
    return [dict(r) for r in rows]


def expire_unclicked(conn, *, now: datetime) -> int:
    def work() -> int:
        expired = 0
        for attempt in _attempts_in_state(conn, "AUTHORIZED"):
            if now - parse_utc(attempt["created_at"]) >= CLICK_DISPATCH_TTL:
                append_attempt_event(conn, attempt_id=attempt["id"], state="EXPIRED_UNCLICKED", source="SERVER",
                                     evidence={"reason": "dispatch_ttl_elapsed"}, now=now)
                _release(conn, attempt, now)
                expired += 1
        return expired
    return _transaction(conn, work)


def mark_stale_dispatches_ambiguous(conn, *, now: datetime, result_timeout: timedelta) -> int:
    def work() -> int:
        marked = 0
        for attempt in _attempts_in_state(conn, "CLICK_DISPATCHED"):
            dispatched_at = conn.execute(
                "SELECT created_at FROM submission_attempt_events WHERE attempt_id = ? AND state = 'CLICK_DISPATCHED' "
                "ORDER BY seq DESC LIMIT 1", (attempt["id"],),
            ).fetchone()["created_at"]
            if now - parse_utc(dispatched_at) >= result_timeout:
                append_attempt_event(conn, attempt_id=attempt["id"], state="SUBMISSION_AMBIGUOUS", source="SERVER",
                                     evidence={"reason": "no_result_within_timeout"}, now=now)
                marked += 1
        return marked
    return _transaction(conn, work)


def record_submission_result(conn, *, attempt_id: str, state: str, source: str, evidence: dict[str, Any],
                             now: datetime) -> str:
    if state not in ("CONFIRMED_SUCCESS", "SUBMISSION_AMBIGUOUS", "SUBMISSION_FAILED"):
        raise ValueError(state)

    def work() -> str:
        attempt = _attempt(conn, attempt_id)
        recorded = state
        if state == "SUBMISSION_FAILED" and evidence.get("proven_not_submitted") is not True:
            recorded = "SUBMISSION_AMBIGUOUS"  # spec §10.4: anything short of proof is ambiguous
        append_attempt_event(conn, attempt_id=attempt_id, state=recorded, source=source, evidence=evidence, now=now)
        if recorded == "CONFIRMED_SUCCESS":
            _confirm(conn, attempt, now)
        elif recorded == "SUBMISSION_FAILED":
            _release(conn, attempt, now)
        return recorded
    return _transaction(conn, work)


def resolve_ambiguous(conn, *, attempt_id: str, submitted: bool, actor: str, now: datetime) -> str:
    def work() -> str:
        attempt = _attempt(conn, attempt_id)
        if attempt_state(conn, attempt_id) != "SUBMISSION_AMBIGUOUS":
            raise ValueError("only an ambiguous attempt can be resolved by the user")
        state = "CONFIRMED_SUCCESS" if submitted else "SUBMISSION_FAILED"
        append_attempt_event(conn, attempt_id=attempt_id, state=state, source="USER",
                             evidence={"attested_by": actor}, now=now)
        (_confirm if submitted else _release)(conn, attempt, now)
        return state
    return _transaction(conn, work)
```

Why the second `click` in `test_happy_path_authorizes_once` reports `duplicate`: after the first commit the identity has a `CLAIMED` intent, so re-evaluation denies `duplicate` before the (already consumed) grant is even tried.

Why the last-slot race loser reports `limit`: `BEGIN IMMEDIATE` serializes the two transactions; the second builds its context after the first committed, sees `submit_per_day` used 1 of 1, and the gate returns `DENY_TEMPORARY(limit)`.

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_preclick.py tests/webapp/services/test_autonomy_preclick_concurrency.py -q`
Expected: all PASS. Run the concurrency file 20 times to check for flakiness: `for i in $(seq 20); do .venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_preclick_concurrency.py -q -p no:randomly || break; done`
Expected: 20 passes.

- [ ] **Step 5: Commit**

```bash
git add webapp/services/autonomy.py tests/webapp/services/test_autonomy_preclick.py tests/webapp/services/test_autonomy_preclick_concurrency.py
git commit -m "feat(services): add atomic pre-click transaction and attempt lifecycle

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 16: Human-path intents and backfill of pre-6B applications

**Files:**
- Modify: `webapp/persistence/autonomy_ledger.py` (add `record_human_intent`)
- Modify: `webapp/persistence/workflow.py:144-156` (hook on `applied`)
- Modify: `webapp/services/handoff.py:533-539` (hook on submission confirmation)
- Modify: `webapp/persistence/migrations.py` (migration `017_autonomy_human_intent_backfill`)
- Test: `tests/webapp/services/test_autonomy_human_intents.py`

**Interfaces:**
- Consumes: `workspace_identity`, `live_intent`, `claim_intent` (Task 11).
- Produces: `record_human_intent(conn, *, workspace_id, account_id, source, workflow_event_id=None, now=None) -> dict | None` (no commit; `None` when the workspace has no strong identity); `AUTONOMY_HUMAN_INTENT_BACKFILL_MIGRATION_ID = "017_autonomy_human_intent_backfill"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/webapp/services/test_autonomy_human_intents.py
from __future__ import annotations

from webapp.persistence.application_identity import save_application_identity
from webapp.persistence.autonomy_ledger import live_intent, workspace_identity
from webapp.persistence.migrations import apply_migrations
from webapp.persistence.workflow import record_status_change
from tests.webapp.persistence.autonomy_db import ACCOUNT, conn, make_workspace  # noqa: F401

RECORD = {"source": "greenhouse", "source_record_id": "77", "company": "Acme", "title": "Eng", "location": "UK"}


def _ws(conn, record=RECORD):
    ws = make_workspace(conn)
    save_application_identity(conn, application_workspace_id=ws, source_record=record)
    conn.commit()
    return ws


def test_marking_applied_creates_confirmed_human_intent(conn):
    ws = _ws(conn)
    record_status_change(conn, workspace_id=ws, new_status="applied", effective_date="2026-09-24")
    key, _, _ = workspace_identity(conn, ws)
    intent = live_intent(conn, account_id=ACCOUNT, job_identity_key=key)
    assert (intent["state"], intent["source"]) == ("CONFIRMED", "HUMAN_APPLIED")


def test_applied_twice_is_idempotent_and_weak_identity_is_skipped(conn):
    ws = _ws(conn)
    record_status_change(conn, workspace_id=ws, new_status="applied", effective_date="2026-09-24")
    record_status_change(conn, workspace_id=ws, new_status="interview", effective_date="2026-09-25")
    weak = _ws(conn, {"company": "Weak Co", "title": "Eng", "location": "UK"})
    record_status_change(conn, workspace_id=weak, new_status="applied", effective_date="2026-09-24")
    assert conn.execute("SELECT COUNT(*) FROM submission_intents").fetchone()[0] == 1


def test_backfill_covers_applications_applied_before_6b(conn):
    ws = _ws(conn)
    conn.execute(
        "INSERT INTO workflow_events (id, workspace_id, previous_status, new_status, effective_date, created_at) "
        "VALUES ('evt_old', ?, NULL, 'applied', '2026-08-01', '2026-08-01T00:00:00+00:00')", (ws,))
    conn.execute("DELETE FROM schema_migrations WHERE id = '017_autonomy_human_intent_backfill'")
    conn.commit()
    apply_migrations(conn)
    key, _, _ = workspace_identity(conn, ws)
    intent = live_intent(conn, account_id=ACCOUNT, job_identity_key=key)
    assert (intent["state"], intent["workflow_event_id"]) == ("CONFIRMED", "evt_old")
    conn.execute("DELETE FROM schema_migrations WHERE id = '017_autonomy_human_intent_backfill'")
    conn.commit()
    apply_migrations(conn)  # re-running never duplicates
    assert conn.execute("SELECT COUNT(*) FROM submission_intents").fetchone()[0] == 1
```

Append this test to the same file (handoff confirmations before 6B are backfilled too; the live hook in `confirm_handoff_submission` is exercised by the existing handoff suites in Step 7):

```python
def test_backfill_covers_handoff_confirmations(conn):
    from webapp.persistence.artifacts import save_artifact
    ws = _ws(conn)
    pack = save_artifact(conn, workspace_id=ws, artifact_type="application_pack", payload={"schema_version": "x"})
    conn.execute(
        "INSERT INTO handoff_sessions (id, account_id, workspace_id, pack_artifact_id, target_url, target_domain, "
        "ats_adapter_id, ats_adapter_version, started_at, status) VALUES "
        "('hs1', ?, ?, ?, 'https://boards.greenhouse.io/acme/jobs/77', 'boards.greenhouse.io', 'greenhouse', '1', "
        "'2026-08-01T00:00:00+00:00', 'submitted')", (ACCOUNT, ws, pack["id"]))
    conn.execute("INSERT INTO submission_confirmations (id, handoff_session_id, created_at) "
                 "VALUES ('sc1', 'hs1', '2026-08-01T00:00:00+00:00')")
    conn.execute("DELETE FROM schema_migrations WHERE id = '017_autonomy_human_intent_backfill'")
    conn.commit()
    apply_migrations(conn)
    key, _, _ = workspace_identity(conn, ws)
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key=key)["source"] == "HUMAN_HANDOFF"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_human_intents.py -q`
Expected: FAIL — no intent created.

- [ ] **Step 3: Add `record_human_intent` to `webapp/persistence/autonomy_ledger.py`**

```python
def record_human_intent(conn, *, workspace_id: str, account_id: str, source: str,
                        workflow_event_id: str | None = None, now: datetime | None = None) -> dict[str, Any] | None:
    """A human submission (handoff confirmation or 'applied') is a CONFIRMED
    intent, so duplicate prevention spans both paths (spec §10.2). Idempotent:
    an existing live intent for the identity is returned unchanged."""
    key, _, _ = workspace_identity(conn, workspace_id)
    if key is None:
        return None
    live = live_intent(conn, account_id=account_id, job_identity_key=key)
    if live is not None:
        return live
    return claim_intent(conn, account_id=account_id, job_identity_key=key, application_workspace_id=workspace_id,
                        source=source, state="CONFIRMED", workflow_event_id=workflow_event_id,
                        now=now or datetime.now(timezone.utc))
```

- [ ] **Step 4: Hook `applied` in `webapp/persistence/workflow.py`**

Import at the top: `from webapp.persistence.autonomy_ledger import record_human_intent`. Inside the `try:` block of `_record_status_change_reserved`, directly after the `UPDATE workspaces …` execute and before `if commit:`:

```python
        if new_status == "applied":
            # Bundle 6B: a human submission blocks autonomous re-application
            # to the same durable job identity (spec §10.2).
            record_human_intent(conn, workspace_id=workspace_id, account_id=account_id,
                                source="HUMAN_APPLIED", workflow_event_id=event_id)
```

- [ ] **Step 5: Hook handoff confirmations in `webapp/services/handoff.py`**

Import `record_human_intent`. In `confirm_handoff_submission`, between the `create_submission_confirmation(...)` call and `conn.commit()`:

```python
    record_human_intent(conn, workspace_id=session["workspace_id"], account_id=session["account_id"],
                        source="HUMAN_HANDOFF",
                        workflow_event_id=workflow_event["id"] if workflow_event else None)
```

- [ ] **Step 6: Add migration 017**

In `webapp/persistence/migrations.py`: add `AUTONOMY_HUMAN_INTENT_BACKFILL_MIGRATION_ID = "017_autonomy_human_intent_backfill"`, register `(AUTONOMY_HUMAN_INTENT_BACKFILL_MIGRATION_ID, _migrate_autonomy_human_intent_backfill, False)` after 016, import `record_human_intent` from `webapp.persistence.autonomy_ledger`, and add:

```python
def _migrate_autonomy_human_intent_backfill(conn: sqlite3.Connection) -> None:
    # Applications the user marked applied (or confirmed via handoff) before
    # Bundle 6B must block autonomous re-application too.
    now = datetime.now(timezone.utc)
    applied = conn.execute(
        "SELECT we.id AS event_id, we.workspace_id, w.account_id FROM workflow_events we "
        "JOIN workspaces w ON w.id = we.workspace_id WHERE we.new_status = 'applied' ORDER BY we.rowid"
    ).fetchall()
    for row in applied:
        record_human_intent(conn, workspace_id=row["workspace_id"], account_id=row["account_id"],
                            source="HUMAN_APPLIED", workflow_event_id=row["event_id"], now=now)
    handoffs = conn.execute(
        "SELECT s.workspace_id, s.account_id FROM submission_confirmations c "
        "JOIN handoff_sessions s ON s.id = c.handoff_session_id ORDER BY c.rowid"
    ).fetchall()
    for row in handoffs:
        record_human_intent(conn, workspace_id=row["workspace_id"], account_id=row["account_id"],
                            source="HUMAN_HANDOFF", now=now)
```

- [ ] **Step 7: Run tests (new + workflow + handoff suites)**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_human_intents.py tests/webapp/persistence/test_workflow.py tests/webapp/services/test_handoff.py tests/webapp/services/test_application_handoff.py -q`
Expected: all PASS (the known `test_record_status_change_tracks_previous_status` Windows flake may need one rerun).

- [ ] **Step 8: Commit**

```bash
git add webapp/persistence/autonomy_ledger.py webapp/persistence/workflow.py webapp/services/handoff.py webapp/persistence/migrations.py tests/webapp/services/test_autonomy_human_intents.py
git commit -m "feat(autonomy): record human submissions as confirmed intents and backfill history

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 17: Shadow-mode decisions at existing workflow points

**Files:**
- Create: `webapp/services/autonomy_shadow.py`
- Modify: `webapp/api/workspaces.py:123-138` (`post_fit`), `webapp/api/review.py:122-140` (`post_application_pack`)
- Test: `tests/webapp/services/test_autonomy_shadow.py`

**Interfaces:**
- Consumes: `decide_and_record` (Task 14).
- Produces: `record_shadow_decision(conn, *, settings, account_id, workspace_id, stage: Capability, now=None) -> dict | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/webapp/services/test_autonomy_shadow.py
from __future__ import annotations

import dataclasses

from product.autonomy_contract import Capability
from webapp.persistence.autonomy_ledger import get_grant, list_decisions
from webapp.services import autonomy_shadow
from webapp.services.autonomy_shadow import record_shadow_decision
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn  # noqa: F401
from tests.webapp.services.test_autonomy_context import seeded, settings  # noqa: F401
from tests.webapp.services.test_autonomy_decide import authorize_all


def test_disabled_by_default(conn, settings, seeded):
    assert record_shadow_decision(conn, settings=settings, account_id=ACCOUNT, workspace_id=seeded,
                                  stage=Capability.PREPARE, now=NOW) is None
    assert list_decisions(conn, seeded) == []


def test_records_non_executable_shadow_decision(conn, settings, seeded):
    authorize_all(conn)
    on = dataclasses.replace(settings, autonomy_shadow_enabled=True)
    row = record_shadow_decision(conn, settings=on, account_id=ACCOUNT, workspace_id=seeded,
                                 stage=Capability.FILL, now=NOW)
    assert row["mode"] == "SHADOW" and row["grantable"] == 0
    assert conn.execute("SELECT COUNT(*) FROM autonomy_grants").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM limit_reservations").fetchone()[0] == 0


def test_shadow_failures_never_break_the_user_flow(conn, settings, seeded, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("shadow bug")
    monkeypatch.setattr(autonomy_shadow, "decide_and_record", boom)
    on = dataclasses.replace(settings, autonomy_shadow_enabled=True)
    assert record_shadow_decision(conn, settings=on, account_id=ACCOUNT, workspace_id=seeded,
                                  stage=Capability.PREPARE, now=NOW) is None
    assert "shadow" in caplog.text.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_shadow.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `webapp/services/autonomy_shadow.py`**

```python
"""Shadow mode (6B spec §14.2 stage 1): evaluate and record what autonomy
WOULD decide at existing workflow points. Never creates executable
authority. A shadow failure is logged and swallowed -- it must never break the
user's real workflow."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from product.autonomy_contract import Capability, Mode
from webapp.config import Settings
from webapp.services.autonomy import decide_and_record

logger = logging.getLogger(__name__)


def record_shadow_decision(conn: sqlite3.Connection, *, settings: Settings, account_id: str, workspace_id: str,
                           stage: Capability, now: datetime | None = None) -> dict[str, Any] | None:
    if not settings.autonomy_shadow_enabled:
        return None
    try:
        _, row = decide_and_record(conn, settings=settings, account_id=account_id,
                                   application_workspace_id=workspace_id, requested_stage=stage,
                                   mode=Mode.SHADOW, now=now or datetime.now(timezone.utc))
        return row
    except Exception:
        logger.exception("autonomy shadow evaluation failed for workspace %s", workspace_id)
        return None
```

- [ ] **Step 4: Hook the two workflow points**

In `webapp/api/workspaces.py` `post_fit`, replace the `return {"artifact": fit_job(...)}` with:

```python
        artifact = fit_job(
            conn, workspace_id, _semantic_adapter(request), request_id=body.request_id,
            extension_ids=body.extension_ids, extensions_dir=extensions_dir,
            account_id=scope.account_id,
        )
        record_shadow_decision(conn, settings=request.app.state.settings, account_id=scope.account_id,
                               workspace_id=workspace_id, stage=Capability.PREPARE)
        return {"artifact": artifact}
```

In `webapp/api/review.py` `post_application_pack`, add `request: Request` to the signature (import `Request` from `fastapi`), and replace the `return confirm_job_application_pack(...)` with:

```python
        result = confirm_job_application_pack(
            conn, workspace_id, effective_date=body.effective_date,
            documents_root=documents_root, extensions_dir=extensions_dir,
            account_id=scope.account_id,
            document_selection_revisions=body.document_selection_revisions,
        )
        record_shadow_decision(conn, settings=request.app.state.settings, account_id=scope.account_id,
                               workspace_id=workspace_id, stage=Capability.FILL)
        return result
```

Both files: `from product.autonomy_contract import Capability` and `from webapp.services.autonomy_shadow import record_shadow_decision`.

- [ ] **Step 5: Run tests (new + the two route suites)**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_shadow.py tests/webapp/api/test_workspace_routes.py tests/webapp/api/test_review_routes.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add webapp/services/autonomy_shadow.py webapp/api/workspaces.py webapp/api/review.py tests/webapp/services/test_autonomy_shadow.py
git commit -m "feat(autonomy): record shadow decisions after fit and pack confirmation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 18: Application dossier

**Files:**
- Create: `webapp/services/autonomy_dossier.py`
- Test: `tests/webapp/services/test_autonomy_dossier.py`

**Interfaces:**
- Consumes: ledger, answers, authority persistence; `list_application_blockers`, `list_blocker_resolution_history`; `get_workspace`.
- Produces: `DOSSIER_SCHEMA_VERSION = "autonomy-dossier.v1"`; `build_dossier(conn, *, account_id, application_workspace_id) -> dict` (raises `LookupError` when the workspace is not the account's).

- [ ] **Step 1: Write the failing test**

```python
# tests/webapp/services/test_autonomy_dossier.py
from __future__ import annotations

import json

import pytest

from webapp.services.autonomy import pre_click_commit, record_click_dispatched, record_submission_result
from webapp.services.autonomy_dossier import build_dossier
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401
from tests.webapp.services.test_autonomy_context import seeded, settings  # noqa: F401
from tests.webapp.services.test_autonomy_decide import OBS
from tests.webapp.services.test_autonomy_preclick import T, submit_grant, verification


def test_dossier_reconstructs_a_scripted_submission(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = pre_click_commit(conn, settings=settings, grant_id=gid, verification=verification(seeded),
                               now=T, observation=OBS).attempt_id
    record_click_dispatched(conn, attempt_id=attempt, now=T)
    record_submission_result(conn, attempt_id=attempt, state="CONFIRMED_SUCCESS", source="EXECUTOR",
                             evidence={"confirmation_text": "Thanks for applying"}, now=T)
    dossier = build_dossier(conn, account_id=ACCOUNT, application_workspace_id=seeded)
    json.dumps(dossier)  # exportable
    assert dossier["schema_version"] == "autonomy-dossier.v1"
    assert [d["requested_stage"] for d in dossier["decisions"]] == ["SUBMIT", "SUBMIT"]
    (grant,) = dossier["grants"]
    assert grant["status"] == "CONSUMED" and grant["binding"]["fill_manifest"]["pages"][0]["entries"][0]["page_field_key"] == "email"
    assert [e["state"] for e in grant["events"]] == ["ISSUED", "CONSUMED"]
    (att,) = dossier["attempts"]
    assert [e["state"] for e in att["events"]] == ["AUTHORIZED", "CLICK_DISPATCHED", "CONFIRMED_SUCCESS"]
    assert att["events"][-1]["evidence"] == {"confirmation_text": "Thanks for applying"}
    assert [i["state"] for i in dossier["intents"]] == ["CONFIRMED"]


def test_dossier_is_account_scoped(conn):
    ws = make_workspace(conn)
    with pytest.raises(LookupError):
        build_dossier(conn, account_id="someone_else", application_workspace_id=ws)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_dossier.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `webapp/services/autonomy_dossier.py`**

```python
"""Application dossier (6B spec §13): what did autonomy do for this
application, why, what exactly did it send, where, and what happened.
Read-only and derived; never edited."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from webapp.persistence.application_blockers import list_application_blockers, list_blocker_resolution_history
from webapp.persistence.autonomy_ledger import list_attempt_events, list_decisions
from webapp.persistence.workspaces import get_workspace

DOSSIER_SCHEMA_VERSION = "autonomy-dossier.v1"


def _rows(conn, sql: str, params: tuple) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def build_dossier(conn: sqlite3.Connection, *, account_id: str, application_workspace_id: str) -> dict[str, Any]:
    ws = application_workspace_id
    workspace = get_workspace(conn, ws, account_id=account_id)
    if workspace is None:
        raise LookupError(ws)
    decisions = [
        {k: v for k, v in d.items() if k not in ("reasons_json", "require_user_json")}
        | {"inputs": json.loads(d["inputs_json"])}
        for d in list_decisions(conn, ws)
    ]
    for d in decisions:
        d.pop("inputs_json", None)
    grants = []
    for g in _rows(conn, "SELECT * FROM autonomy_grants WHERE application_workspace_id = ? ORDER BY seq", (ws,)):
        g["binding"] = json.loads(g.pop("binding_json"))
        g["events"] = _rows(conn, "SELECT status AS state, reason, created_at FROM autonomy_grant_events "
                                  "WHERE grant_id = ? ORDER BY seq", (g["id"],))
        grants.append(g)
    attempts = []
    for a in _rows(conn, "SELECT * FROM submission_attempts WHERE application_workspace_id = ? ORDER BY seq", (ws,)):
        a["events"] = [
            {"state": e["state"], "source": e["source"], "created_at": e["created_at"],
             "evidence": json.loads(e["evidence_json"])}
            for e in list_attempt_events(conn, a["id"])
        ]
        attempts.append(a)
    exceptions = [
        {**b, "resolutions": list_blocker_resolution_history(conn, b["id"])}
        for b in list_application_blockers(conn, ws)
    ]
    return {
        "schema_version": DOSSIER_SCHEMA_VERSION,
        "application_workspace_id": ws,
        "company": workspace.get("company"),
        "title": workspace.get("title"),
        "decisions": decisions,
        "grants": grants,
        "attempts": attempts,
        "intents": _rows(conn, "SELECT * FROM submission_intents WHERE application_workspace_id = ? ORDER BY seq", (ws,)),
        "rule_acknowledgements": _rows(conn, "SELECT * FROM rule_acknowledgements WHERE application_workspace_id = ? "
                                             "ORDER BY seq", (ws,)),
        "apply_target_confirmations": _rows(conn, "SELECT * FROM apply_target_confirmations "
                                                  "WHERE application_workspace_id = ? ORDER BY seq", (ws,)),
        "dry_run_cases": _rows(conn, "SELECT * FROM dry_run_submission_cases WHERE application_workspace_id = ? "
                                     "ORDER BY seq", (ws,)),
        "exceptions": exceptions,
        "kill_switch_events": _rows(conn, "SELECT engaged, reason, actor, created_at FROM autonomy_kill_switch "
                                          "WHERE account_id = ? ORDER BY seq", (account_id,)),
    }
```


- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_autonomy_dossier.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/services/autonomy_dossier.py tests/webapp/services/test_autonomy_dossier.py
git commit -m "feat(autonomy): add application dossier

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 19: API and UI — authority settings, standing policy, kill switch, target confirmation, acknowledgements, dossier

**Files:**
- Create: `webapp/api/autonomy.py`, `webapp/templates/autonomy.html`, `webapp/templates/autonomy_dossier.html`
- Modify: `webapp/app.py` (include router), `webapp/templates/base.html:21` (nav link "Autonomy")
- Test: `tests/webapp/api/test_autonomy_routes.py`

**Interfaces:**
- Consumes: controls (Task 12), persistence (Tasks 9–10), `build_context` + `evaluate_rules`/`observed_fingerprint`/`rule_hash`/`policy_hash`, `resolve_apply_target`, `canonical_target_url`, `build_dossier`.
- Produces (HTTP):
  - `GET /api/autonomy` → `{"account_max", "default_workspace_ceiling", "deployment_ceiling", "kill_switch": {...}, "sentinel_path", "sentinel_present", "policy": doc | null, "policy_hash"}`
  - `POST /api/autonomy/enable-preparation` `{"timezone"}`
  - `POST /api/autonomy/capability` `{"scope_type", "scope_id", "capability"}` (`scope_type` ∈ `ACCOUNT_MAX`, `DEFAULT_WORKSPACE_CEILING`, `WORKSPACE_CEILING`; a `WORKSPACE_CEILING` `scope_id` must be the caller's own search workspace)
  - `PUT /api/autonomy/policy` `{"doc"}` → `200 {"policy_hash"}` or `422 {"errors": [...]}`
  - `POST /api/autonomy/kill-switch/engage|release` `{"reason"}`; `POST /api/autonomy/resume-all` `{"reason"}` → `409` while the sentinel exists
  - `POST /api/autonomy/pause|resume` `{"scope_type", "scope_id", "reason"}`
  - `POST /api/workspaces/{workspace_id}/autonomy/apply-target/confirm` `{"url"}` → `409` unless `url` canonicalizes to the workspace's resolved apply target
  - `POST /api/workspaces/{workspace_id}/autonomy/rule-acknowledgements` `{"rule_id", "disposition"}` → server computes `rule_hash`, `observed_fingerprint`, `policy_version_hash` from the current context; `404` for an unknown rule id
  - `GET /api/workspaces/{workspace_id}/autonomy/dossier` → dossier JSON; `404` if not the caller's
  - `GET /autonomy` (HTML), `GET /workspaces/{workspace_id}/autonomy` (HTML dossier)

- [ ] **Step 1: Write the failing tests**

```python
# tests/webapp/api/test_autonomy_routes.py
from __future__ import annotations

from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.db import connect
from webapp.persistence.workspaces import create_workspace


def client_for(tmp_path, **settings_kwargs):
    settings = Settings(db_path=tmp_path / "db.sqlite3", **settings_kwargs)
    client = TestClient(create_app(settings))
    client.__enter__()
    return client, settings


def test_settings_start_at_none_and_enable_is_explicit(tmp_path):
    client, _ = client_for(tmp_path)
    body = client.get("/api/autonomy").json()
    assert (body["account_max"], body["default_workspace_ceiling"], body["policy"]) == ("NONE", "NONE", None)
    assert client.post("/api/autonomy/enable-preparation", json={"timezone": "Europe/London"}).status_code == 200
    body = client.get("/api/autonomy").json()
    assert (body["account_max"], body["default_workspace_ceiling"]) == ("PREPARE", "PREPARE")
    assert body["policy"]["timezone"] == "Europe/London"


def test_policy_validation_errors_are_422_not_500(tmp_path):
    client, _ = client_for(tmp_path)
    client.post("/api/autonomy/enable-preparation", json={"timezone": "Europe/London"})
    doc = client.get("/api/autonomy").json()["policy"]
    doc["rules"] = [{"id": "fit", "description": "", "when": {"attr": "fit.overall_score", "op": "lt", "value": 75.5},
                     "effect": {"type": "REDUCE_TO", "level": "PREPARE"}, "on_unknown": {"type": "REDUCE_TO", "level": "PREPARE"}}]
    response = client.put("/api/autonomy/policy", json={"doc": doc})
    assert response.status_code == 422
    assert any("integer" in e for e in response.json()["errors"])


def test_kill_switch_sentinel_and_resume_all(tmp_path):
    client, settings = client_for(tmp_path)
    assert client.post("/api/autonomy/kill-switch/engage", json={"reason": "stop"}).status_code == 200
    assert client.get("/api/autonomy").json()["kill_switch"]["halted"] is True
    client.post("/api/autonomy/kill-switch/release", json={"reason": "ok"})
    assert client.get("/api/autonomy").json()["kill_switch"]["halted"] is True
    settings.autonomy_sentinel_path.write_text("halt")
    assert client.post("/api/autonomy/resume-all", json={"reason": "go"}).status_code == 409
    settings.autonomy_sentinel_path.unlink()
    assert client.post("/api/autonomy/resume-all", json={"reason": "go"}).status_code == 200
    assert client.get("/api/autonomy").json()["kill_switch"]["halted"] is False


def test_capability_rejects_foreign_workspace_and_unknown_values(tmp_path):
    client, _ = client_for(tmp_path)
    r = client.post("/api/autonomy/capability", json={"scope_type": "WORKSPACE_CEILING", "scope_id": "sw_not_mine",
                                                      "capability": "SUBMIT"})
    assert r.status_code == 404
    r = client.post("/api/autonomy/capability", json={"scope_type": "ACCOUNT_MAX", "scope_id": "x", "capability": "GODMODE"})
    assert r.status_code == 422


def test_dossier_and_pages(tmp_path):
    client, settings = client_for(tmp_path)
    conn = connect(settings.db_path)
    ws = create_workspace(conn, company="Acme", title="Eng")["id"]
    conn.close()
    assert client.get(f"/api/workspaces/{ws}/autonomy/dossier").json()["schema_version"] == "autonomy-dossier.v1"
    assert client.get("/api/workspaces/ws_missing/autonomy/dossier").status_code == 404
    assert client.get("/autonomy").status_code == 200
    assert client.get(f"/workspaces/{ws}/autonomy").status_code == 200


def test_apply_target_confirmation_requires_the_resolved_target(tmp_path):
    client, settings = client_for(tmp_path)
    conn = connect(settings.db_path)
    ws = create_workspace(conn, company="Acme", title="Eng")["id"]
    conn.close()
    r = client.post(f"/api/workspaces/{ws}/autonomy/apply-target/confirm", json={"url": "https://evil.example/jobs/1"})
    assert r.status_code == 409
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/api/test_autonomy_routes.py -q`
Expected: FAIL — 404s (router missing).

- [ ] **Step 3: Implement `webapp/api/autonomy.py`**

```python
"""HTTP adapters for the Bundle 6B autonomy contract. Thin routes; every
state change is an explicit, attributed user action (actor = account id)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from product.autonomy_contract import Capability, Mode
from product.standing_policy import StandingPolicyError, evaluate_rules, policy_hash
from webapp.api.dependencies import get_account_scope, get_conn
from webapp.persistence.autonomy_answers import confirm_apply_target, record_rule_acknowledgement
from webapp.persistence.autonomy_authority import (
    current_capability, current_policy, kill_switch_state, save_policy_version,
)
from webapp.persistence.autonomy_ledger import workspace_identity
from webapp.persistence.workspaces import get_workspace
from webapp.services.autonomy_context import build_context, canonical_target_url
from webapp.services.autonomy_controls import (
    AutonomyHalted, enable_autonomous_preparation, engage_kill_switch, pause, release_kill_switch, resume,
    resume_all, sentinel_present, set_capability,
)
from webapp.services.autonomy_dossier import build_dossier
from webapp.services.ownership import AccountScope, OwnedResourceNotFound
from webapp.services.workspace_view import resolve_apply_target

router = APIRouter(tags=["autonomy"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimezoneBody(_Body):
    timezone: str


class CapabilityBody(_Body):
    scope_type: Literal["ACCOUNT_MAX", "DEFAULT_WORKSPACE_CEILING", "WORKSPACE_CEILING"]
    scope_id: str
    capability: Literal["NONE", "PREPARE", "FILL", "SUBMIT"]


class PolicyBody(_Body):
    doc: dict[str, Any]


class ReasonBody(_Body):
    reason: str


class ScopeBody(_Body):
    scope_type: Literal["APPLICATION", "SEARCH_WORKSPACE", "ACCOUNT"]
    scope_id: str
    reason: str


class UrlBody(_Body):
    url: str


class AckBody(_Body):
    rule_id: str
    disposition: Literal["PROCEED", "DO_NOT_PROCEED"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_workspace(conn, workspace_id: str, account_id: str) -> dict[str, Any]:
    workspace = get_workspace(conn, workspace_id, account_id=account_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return workspace


@router.get("/api/autonomy")
def get_autonomy(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    settings = request.app.state.settings
    acct = scope.account_id
    policy = current_policy(conn, acct)
    cap = lambda st: (current_capability(conn, account_id=acct, scope_type=st, scope_id=acct) or Capability.NONE).name
    return {
        "account_max": cap("ACCOUNT_MAX"),
        "default_workspace_ceiling": cap("DEFAULT_WORKSPACE_CEILING"),
        "deployment_ceiling": settings.autonomy_deployment_ceiling().name,
        "kill_switch": kill_switch_state(conn, acct),
        "sentinel_path": str(settings.autonomy_sentinel_path),
        "sentinel_present": sentinel_present(settings.autonomy_sentinel_path),
        "policy": policy["doc"] if policy else None,
        "policy_hash": policy["policy_hash"] if policy else None,
    }


@router.post("/api/autonomy/enable-preparation")
def post_enable(body: TimezoneBody, conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    try:
        return enable_autonomous_preparation(conn, account_id=scope.account_id, actor=scope.account_id,
                                             timezone=body.timezone, now=_now())
    except StandingPolicyError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc


@router.post("/api/autonomy/capability")
def post_capability(body: CapabilityBody, conn: sqlite3.Connection = Depends(get_conn),
                    scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    scope_id = body.scope_id
    if body.scope_type == "WORKSPACE_CEILING":
        try:
            scope.require_search_workspace(conn, scope_id)
        except OwnedResourceNotFound as exc:
            raise HTTPException(status_code=404, detail="search workspace not found") from exc
    else:
        scope_id = scope.account_id
    row = set_capability(conn, account_id=scope.account_id, scope_type=body.scope_type, scope_id=scope_id,
                         capability=Capability[body.capability], actor=scope.account_id, now=_now())
    return {"id": row["id"]}


@router.put("/api/autonomy/policy")
def put_policy(body: PolicyBody, conn: sqlite3.Connection = Depends(get_conn),
               scope: AccountScope = Depends(get_account_scope)):
    try:
        row = save_policy_version(conn, account_id=scope.account_id, doc=body.doc, created_by=scope.account_id,
                                  now=_now())
    except StandingPolicyError as exc:
        return JSONResponse(status_code=422, content={"errors": exc.errors})
    return {"policy_hash": row["policy_hash"]}


@router.post("/api/autonomy/kill-switch/engage")
def post_engage(body: ReasonBody, conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return engage_kill_switch(conn, account_id=scope.account_id, actor=scope.account_id, reason=body.reason, now=_now())


@router.post("/api/autonomy/kill-switch/release")
def post_release(body: ReasonBody, conn: sqlite3.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return release_kill_switch(conn, account_id=scope.account_id, actor=scope.account_id, reason=body.reason, now=_now())


@router.post("/api/autonomy/resume-all")
def post_resume_all(body: ReasonBody, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                    scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    try:
        return resume_all(conn, account_id=scope.account_id, actor=scope.account_id, reason=body.reason, now=_now(),
                          sentinel_path=request.app.state.settings.autonomy_sentinel_path)
    except AutonomyHalted as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/autonomy/pause")
def post_pause(body: ScopeBody, conn: sqlite3.Connection = Depends(get_conn),
               scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return pause(conn, account_id=scope.account_id, scope_type=body.scope_type, scope_id=body.scope_id,
                 actor=scope.account_id, reason=body.reason, now=_now())


@router.post("/api/autonomy/resume")
def post_resume(body: ScopeBody, conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return resume(conn, account_id=scope.account_id, scope_type=body.scope_type, scope_id=body.scope_id,
                  actor=scope.account_id, reason=body.reason, now=_now())


@router.post("/api/workspaces/{workspace_id}/autonomy/apply-target/confirm")
def post_confirm_target(workspace_id: str, body: UrlBody, conn: sqlite3.Connection = Depends(get_conn),
                        scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _require_workspace(conn, workspace_id, scope.account_id)
    target = resolve_apply_target(conn, workspace_id=workspace_id, account_id=scope.account_id)
    wanted = canonical_target_url(body.url)
    if target is None or wanted is None or wanted != canonical_target_url(target.url):
        raise HTTPException(status_code=409, detail="URL is not this application's resolved apply target")
    key, _, _ = workspace_identity(conn, workspace_id)
    row = confirm_apply_target(conn, application_workspace_id=workspace_id, job_identity_key=key,
                               canonical_url=wanted, confirmed_by=scope.account_id, now=_now())
    return {"id": row["id"]}


@router.post("/api/workspaces/{workspace_id}/autonomy/rule-acknowledgements")
def post_ack(workspace_id: str, body: AckBody, request: Request, conn: sqlite3.Connection = Depends(get_conn),
             scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    _require_workspace(conn, workspace_id, scope.account_id)
    policy = current_policy(conn, scope.account_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="no standing policy")
    ctx = build_context(conn, settings=request.app.state.settings, account_id=scope.account_id,
                        application_workspace_id=workspace_id, requested_stage=Capability.PREPARE,
                        mode=Mode.SHADOW, now=_now(), sentinel_present=False)
    outcome = next((o for o in evaluate_rules(policy["doc"], ctx.attributes) if o.rule_id == body.rule_id), None)
    if outcome is None:
        raise HTTPException(status_code=404, detail="unknown rule")
    row = record_rule_acknowledgement(
        conn, account_id=scope.account_id, application_workspace_id=workspace_id, rule_id=body.rule_id,
        rule_hash=outcome.rule_hash, observed_fingerprint=outcome.observed_fingerprint,
        policy_version_hash=policy_hash(policy["doc"]), disposition=body.disposition,
        actor=scope.account_id, now=_now(),
    )
    return {"id": row["id"]}


@router.get("/api/workspaces/{workspace_id}/autonomy/dossier")
def get_dossier(workspace_id: str, conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    try:
        return build_dossier(conn, account_id=scope.account_id, application_workspace_id=workspace_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc


@router.get("/autonomy")
def autonomy_page(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                  scope: AccountScope = Depends(get_account_scope)):
    return request.app.state.templates.TemplateResponse(
        request, "autonomy.html", {"autonomy": get_autonomy(request, conn, scope)})


@router.get("/workspaces/{workspace_id}/autonomy")
def dossier_page(workspace_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)):
    return request.app.state.templates.TemplateResponse(
        request, "autonomy_dossier.html", {"dossier": get_dossier(workspace_id, conn, scope)})
```


- [ ] **Step 4: Register the router and add the nav link**

In `webapp/app.py`: `from webapp.api.autonomy import router as autonomy_router` and `app.include_router(autonomy_router)` **before** `app.include_router(views_router)` (so `/workspaces/{id}/autonomy` is not shadowed by a views catch-all).

In `webapp/templates/base.html` line 21, add `<a href="/autonomy">Autonomy</a>` before the `How it works` link.

- [ ] **Step 5: Create the templates**

`webapp/templates/autonomy.html`:

```html
{% extends "base.html" %}
{% block title %}Autonomy · Job Search Workspace{% endblock %}
{% block content %}
<section class="hero compact"><div><p class="eyebrow">Standing authorization</p><h1>Autonomy</h1>
  <p>Autonomy is pre-authorized, not unrestricted. Nothing runs above what you explicitly allow here.</p></div></section>
<section class="panel">
  <div class="panel-heading"><h2>Authority</h2></div>
  <dl class="facts">
    <dt>Account maximum</dt><dd data-autonomy="account-max">{{ autonomy.account_max }}</dd>
    <dt>Default for new searches</dt><dd>{{ autonomy.default_workspace_ceiling }}</dd>
    <dt>Deployment ceiling</dt><dd>{{ autonomy.deployment_ceiling }}</dd>
  </dl>
  {% if autonomy.account_max == "NONE" %}
  <form id="autonomy-enable-form"><label>Time zone<input name="timezone" value="Europe/London" required></label>
    <button class="button" type="submit">Enable autonomous preparation</button></form>
  {% endif %}
</section>
<section class="panel">
  <div class="panel-heading"><h2>Kill switch</h2></div>
  <p data-autonomy="halted">{% if autonomy.kill_switch.halted %}Halted{% else %}Running{% endif %}
    {% if autonomy.sentinel_present %} · sentinel file present{% endif %}</p>
  <p>Emergency stop without the UI: create <code>{{ autonomy.sentinel_path }}</code>.</p>
  <button class="button" type="button" data-autonomy-action="kill-switch/engage">Stop all autonomy</button>
  <button class="button secondary" type="button" data-autonomy-action="kill-switch/release">Release kill switch</button>
  <button class="button secondary" type="button" data-autonomy-action="resume-all">Resume all</button>
</section>
<section class="panel">
  <div class="panel-heading"><h2>Standing policy</h2>{% if autonomy.policy_hash %}<p><code>{{ autonomy.policy_hash[:19] }}…</code></p>{% endif %}</div>
  <form id="autonomy-policy-form"><textarea name="doc" rows="18" spellcheck="false">{{ autonomy.policy | tojson(indent=2) if autonomy.policy else "" }}</textarea>
    <button class="button" type="submit">Save policy</button></form>
  <ul id="autonomy-policy-errors" class="errors"></ul>
</section>
<script>
(function () {
  async function post(path, body, method) {
    const r = await fetch("/api/autonomy/" + path, {method: method || "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    return r;
  }
  document.querySelectorAll("[data-autonomy-action]").forEach(function (button) {
    button.addEventListener("click", async function () {
      const reason = window.prompt("Reason") || "no reason given";
      const r = await post(button.dataset.autonomyAction, {reason: reason});
      if (!r.ok) { window.alert((await r.json()).detail || "Failed"); }
      window.location.reload();
    });
  });
  const enable = document.getElementById("autonomy-enable-form");
  if (enable) enable.addEventListener("submit", async function (e) {
    e.preventDefault();
    await post("enable-preparation", {timezone: enable.timezone.value});
    window.location.reload();
  });
  const policy = document.getElementById("autonomy-policy-form");
  policy.addEventListener("submit", async function (e) {
    e.preventDefault();
    const list = document.getElementById("autonomy-policy-errors");
    list.innerHTML = "";
    let doc;
    try { doc = JSON.parse(policy.doc.value); } catch (err) { list.innerHTML = "<li>Not valid JSON</li>"; return; }
    const r = await post("policy", {doc: doc}, "PUT");
    if (r.status === 422) {
      (await r.json()).errors.forEach(function (msg) { const li = document.createElement("li"); li.textContent = msg; list.appendChild(li); });
      return;
    }
    window.location.reload();
  });
})();
</script>
{% endblock %}
```

`webapp/templates/autonomy_dossier.html`:

```html
{% extends "base.html" %}
{% block title %}Autonomy dossier · {{ dossier.company }}{% endblock %}
{% block content %}
<section class="hero compact"><div><p class="eyebrow">Application dossier</p>
  <h1>{{ dossier.title }} — {{ dossier.company }}</h1>
  <p>Everything autonomy decided and did for this application. <a href="/api/workspaces/{{ dossier.application_workspace_id }}/autonomy/dossier">Export JSON</a></p></div></section>
<section class="panel"><div class="panel-heading"><h2>Decisions</h2></div>
  {% for d in dossier.decisions %}<article class="evidence-row">
    <div><strong>{{ d.requested_stage }} · {{ d.mode }} → {{ d.result }}{% if d.deny_reason %} ({{ d.deny_reason }}){% endif %}</strong>
      <p>Effective capability {{ d.effective_capability }} · {{ d.created_at }}</p>
      <ul>{% for r in d.reasons %}<li><code>{{ r.code }}</code> {{ r.params }}</li>{% endfor %}</ul></div>
  </article>{% else %}<p>No autonomy decisions yet.</p>{% endfor %}
</section>
<section class="panel"><div class="panel-heading"><h2>Submissions</h2></div>
  {% for a in dossier.attempts %}<article class="evidence-row"><div><strong>Attempt {{ a.id }}</strong>
    <ol>{% for e in a.events %}<li>{{ e.state }} · {{ e.source }} · {{ e.created_at }}</li>{% endfor %}</ol></div></article>
  {% else %}<p>No submission attempts.</p>{% endfor %}
</section>
<section class="panel"><div class="panel-heading"><h2>What was authorized for the form</h2></div>
  {% for g in dossier.grants %}<article class="evidence-row"><div><strong>{{ g.stage }} grant · {{ g.status }}</strong>
    {% if g.binding.fill_manifest %}<ul>{% for page in g.binding.fill_manifest.pages %}{% for entry in page.entries %}
      <li><code>{{ entry.page_field_key }}</code> ← {{ entry.source.kind }} {{ entry.source.ref }} ({{ entry.transform_id }})</li>
    {% endfor %}{% endfor %}</ul>{% endif %}</div></article>
  {% else %}<p>No grants issued.</p>{% endfor %}
</section>
{% endblock %}
```

- [ ] **Step 6: Run tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/api/test_autonomy_routes.py tests/webapp/api/test_views.py tests/webapp/test_app_factory.py -q`
Expected: all PASS.

- [ ] **Step 7: Look at the pages in a real browser**

Run the app (`.venv/Scripts/python -m webapp` or the command in `README.md`), open `/autonomy`, enable preparation, save an invalid policy (`"value": 75.5`) and confirm the error list shows the integer message, engage and release the kill switch and confirm the page still says "Halted" until Resume all. Open `/workspaces/<id>/autonomy` for an existing application. Fix any rendering errors before committing.

- [ ] **Step 8: Commit**

```bash
git add webapp/api/autonomy.py webapp/app.py webapp/templates/autonomy.html webapp/templates/autonomy_dossier.html webapp/templates/base.html tests/webapp/api/test_autonomy_routes.py
git commit -m "feat(autonomy): add authority, policy, kill switch and dossier UI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 20: End-to-end acceptance and full-suite verification

**Files:**
- Test: `tests/webapp/test_autonomy_acceptance.py`

**Interfaces:**
- Consumes: `_build_chain` (`tests/webapp/test_full_journey_acceptance.py:160`), everything above.
- Produces: nothing new.

- [ ] **Step 1: Write the acceptance test**

```python
# tests/webapp/test_autonomy_acceptance.py
"""Bundle 6B acceptance: on the real workflow, with shadow mode on and no
autonomy granted, the product behaves exactly as before, records shadow
decisions, and never creates executable authority."""
from __future__ import annotations

from webapp.persistence.autonomy_ledger import list_decisions
from webapp.persistence.db import connect
from tests.webapp.fixtures.acceptance.fixtures import completion_ready_content_units
from tests.webapp.test_full_journey_acceptance import _build_chain, _close


def test_shadow_mode_on_real_workflow_is_non_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSEARCH_AUTONOMY_SHADOW", "1")
    client, app, settings, workspace_id = _build_chain(tmp_path, ai_units=completion_ready_content_units())
    try:
        conn = connect(settings.db_path)
        try:
            decisions = list_decisions(conn, workspace_id)
            assert decisions, "fit should have recorded a shadow PREPARE decision"
            assert all(d["mode"] == "SHADOW" and d["grantable"] == 0 for d in decisions)
            assert all(d["effective_capability"] == "NONE" for d in decisions)  # nothing authorized
            for table in ("autonomy_grants", "limit_reservations", "submission_attempts"):
                assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
        finally:
            conn.close()
        dossier = client.get(f"/api/workspaces/{workspace_id}/autonomy/dossier").json()
        assert len(dossier["decisions"]) == len(decisions)
    finally:
        _close(client)
```


- [ ] **Step 2: Run the acceptance test**

Run: `.venv/Scripts/python -m pytest tests/webapp/test_autonomy_acceptance.py -q`
Expected: PASS.

- [ ] **Step 3: Run the full suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all PASS except, at most, the three known Windows timestamp flakes listed in Global Constraints (rerun any of those individually to confirm they pass). Any other failure is a regression: fix it before continuing.

- [ ] **Step 4: Verify spec coverage by grep**

Run: `grep -rn "created_at DESC" webapp/persistence/autonomy_*.py webapp/services/autonomy*.py`
Expected: no output (invariant 13).
Run: `grep -rn "from webapp" product/`
Expected: no output (layering).

- [ ] **Step 5: Commit**

```bash
git add tests/webapp/test_autonomy_acceptance.py
git commit -m "test(autonomy): add Bundle 6B shadow-mode acceptance on the real workflow

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
