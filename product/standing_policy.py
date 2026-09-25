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
        if isinstance(rule["effect"], dict) and rule["effect"].get("type") == "BLOCK":
            # Missing information must never weaken an explicit prohibition into
            # permission (e.g. "unknown company key" on a deny-list rule must
            # not be read as "not on the list") -- REDUCE_TO and NO_EFFECT are
            # rejected on a BLOCK rule's on_unknown; only BLOCK/REQUIRE_USER
            # preserve the prohibition when the predicate can't be evaluated.
            on_unknown_type = rule["on_unknown"].get("type") if isinstance(rule["on_unknown"], dict) else None
            if on_unknown_type not in ("BLOCK", "REQUIRE_USER"):
                errors.append(
                    f"{path}.on_unknown: a BLOCK rule's on_unknown must be BLOCK or "
                    "REQUIRE_USER, not REDUCE_TO or NO_EFFECT"
                )
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


def _observed_value(attr: str, value: Any) -> Any:
    """Return value if valid for attr's type, else UNKNOWN. Ensures consistency with _leaf."""
    if value is None or is_unknown(value):
        return UNKNOWN
    kind = ATTRIBUTE_TYPES[attr]
    # int attributes: int or finite Decimal, not bool
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            return UNKNOWN
        if isinstance(value, Decimal) and not value.is_finite():
            return UNKNOWN
        return value
    # str attributes: must be str
    if kind == "str" and not isinstance(value, str):
        return UNKNOWN
    return value


def referenced_lists(pred: Mapping[str, Any]) -> list[str]:
    """Employer-list names a predicate tree references via in_list (the only
    way a rule references an employer list, spec §5.2)."""
    if "all" in pred or "any" in pred:
        children = pred.get("all", pred.get("any"))
        return sorted({name for child in children for name in referenced_lists(child)})
    if "not" in pred:
        return referenced_lists(pred["not"])
    if pred.get("op") == "in_list":
        return [pred["value"]]
    return []


def observed_fingerprint(
    rule: Mapping[str, Any], attributes: Mapping[str, Any],
    employer_lists: Mapping[str, list[str]] | None = None,
) -> str:
    """The fingerprint a rule acknowledgement is bound to (spec §9.3 step 5
    "Rule acknowledgements"): the observed attribute values the rule's
    predicate reads, plus -- for any employer list it references via
    in_list -- that list's sorted contents. A referenced-but-absent list is
    recorded as UNKNOWN, distinct from an empty list. Editing a referenced
    list's contents therefore changes the fingerprint (and so lapses any
    acknowledgement bound to the old one), even though it never changes
    referenced_attributes. A rule that references no employer list is
    unaffected by employer_lists at all: its fingerprint's equality
    relations (same attributes -> same fingerprint, different attributes ->
    different fingerprint) are preserved whether or not employer_lists is
    supplied -- not that the fingerprint *value* itself is unchanged from
    before this parameter existed, since the hashed payload's shape changed."""
    observed = {attr: _observed_value(attr, attributes.get(attr, UNKNOWN)) for attr in referenced_attributes(rule["when"])}
    lists = employer_lists or {}
    list_names = referenced_lists(rule["when"])
    observed_lists: dict[str, Any] = {}
    for name in list_names:
        if name in lists:
            observed_lists[name] = sorted(lists[name])
        else:
            observed_lists[name] = UNKNOWN
    payload = {"attributes": observed, "lists": observed_lists}
    return canonical_hash("rule-observation", "v1", payload)


def _leaf(pred: Mapping[str, Any], attributes: Mapping[str, Any], lists: Mapping[str, list[str]]) -> Any:
    attr, op, target = pred["attr"], pred["op"], pred["value"]
    value = attributes.get(attr, UNKNOWN)
    # Use _observed_value to validate type, ensuring consistency with observed_fingerprint
    value = _observed_value(attr, value)
    if is_unknown(value):
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
            observed_fingerprint=observed_fingerprint(rule, attributes, lists),
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
