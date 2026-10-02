"""CV strategy documents (Bundle 7 spec §14.2): which CV each job family uses.

`cv-strategy.v1` = {default: Rule, by_family: {family_id: Rule}} with
Rule = {mode: FIXED_VERSION, version_id} | {mode: LATEST_VERSION, item_id}
     | {mode: TAILOR_FROM, item_id, template_id}.
Every referenced item or version belongs to the account and is ACTIVE; a
template is in the registry; the default is required. The initial default
{LATEST_VERSION, item_id: null} resolves to NEEDS_USER_CHOICE."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SCHEMA_VERSION = "cv-strategy.v1"
MODES = {"FIXED_VERSION": {"mode", "version_id"}, "LATEST_VERSION": {"mode", "item_id"},
         "TAILOR_FROM": {"mode", "item_id", "template_id"}}


class CvStrategyInvalid(ValueError):
    pass


@dataclass(frozen=True)
class ItemView:
    id: str
    status: str
    version_ids: tuple[str, ...]


def default_strategy() -> dict[str, Any]:
    return {"default": {"mode": "LATEST_VERSION", "item_id": None}, "by_family": {}}


def _rule(rule: Any, where: str, items: Mapping[str, ItemView], templates: Mapping[str, Any], *,
          allow_unset: bool) -> dict[str, Any]:
    if not isinstance(rule, Mapping) or rule.get("mode") not in MODES:
        raise CvStrategyInvalid(f"{where}: unknown mode")
    mode = rule["mode"]
    if set(rule) != MODES[mode]:
        raise CvStrategyInvalid(f"{where}: {mode} has the fields {', '.join(sorted(MODES[mode]))}")
    if mode == "FIXED_VERSION":
        owner = next((i for i in items.values() if rule["version_id"] in i.version_ids), None)
        if owner is None:
            raise CvStrategyInvalid(f"{where}: that CV version is not one of your CVs")
        if owner.status != "ACTIVE":
            raise CvStrategyInvalid(f"{where}: that CV is archived")
        return {"mode": mode, "version_id": rule["version_id"]}
    item_id = rule["item_id"]
    if item_id is None and allow_unset and mode == "LATEST_VERSION":
        return {"mode": mode, "item_id": None}
    item = items.get(item_id)
    if item is None:
        raise CvStrategyInvalid(f"{where}: that CV is not one of your CVs")
    if item.status != "ACTIVE":
        raise CvStrategyInvalid(f"{where}: that CV is archived")
    if mode == "TAILOR_FROM":
        if rule["template_id"] not in templates:
            raise CvStrategyInvalid(f"{where}: unknown template {rule['template_id']}")
        return {"mode": mode, "item_id": item_id, "template_id": rule["template_id"]}
    return {"mode": mode, "item_id": item_id}


def normalize_cv_strategy(doc: Any, *, account_items: Mapping[str, ItemView],
                          templates: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(doc, Mapping) or set(doc) - {"default", "by_family", "schema_version"}:
        raise CvStrategyInvalid("a cv-strategy document has default and by_family")
    if "default" not in doc:
        raise CvStrategyInvalid("the default rule is required")
    by_family = doc.get("by_family", {})
    if not isinstance(by_family, Mapping) or len(by_family) > 50:
        raise CvStrategyInvalid("by_family maps at most 50 family ids to rules")
    return {"default": _rule(doc["default"], "default", account_items, templates, allow_unset=True),
            "by_family": {str(fid): _rule(rule, f"family {fid}", account_items, templates, allow_unset=False)
                          for fid, rule in sorted(by_family.items())}}


def resolve_rule(strategy: Mapping[str, Any], family_id: str) -> dict[str, Any]:
    return dict(strategy.get("by_family", {}).get(family_id) or strategy["default"])
