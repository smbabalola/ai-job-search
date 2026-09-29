"""Bundle 6D-B change classification (spec §13). Pure.

A re-observation (pre-action, post-action, final) is diffed against the
observation the plan was built from. Content changes become 6D-A deltas,
all opened together. Other structural changes stop as STRUCTURE_CHANGED.
Value states must follow the expected evolution: completed actions hold
their rendered hash, pending writes are blank or already equal, and OMIT
fields stay blank. Deltas take precedence; the run stops before the next
action either way."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from product.fill_certification import AdapterCertification, deterministic_classification, is_declaration
from product.fill_plan import DECLARATION_PROPOSAL_SUBJECT, DeltaSpec, _observed, _question

_WORDING = ("label", "question", "options", "required")
_TARGET = ("canonical_url", "origin", "tenant_key", "ats_job_id")


@dataclass
class ChangeResult:
    deltas: list[DeltaSpec] = field(default_factory=list)
    stop_reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def _new_field_delta(element: Mapping[str, Any], entry: AdapterCertification) -> DeltaSpec:
    question = _question(element)
    required = bool(element["identity"]["required"])
    if is_declaration(entry, question):
        return DeltaSpec("DECLARATION", None, None, required, question, _observed(element), DECLARATION_PROPOSAL_SUBJECT)
    if element["control_kind"] == "file":
        return DeltaSpec("NEW_UPLOAD", None, None, required, question, _observed(element))
    classified = deterministic_classification(entry, question)
    return DeltaSpec("NEW_QUESTION", None, classified.subject if classified else None, required, question,
                     _observed(element))


def classify_changes(base: Mapping[str, Any], new: Mapping[str, Any], *, plan: Mapping[str, Any],
                     completed: set[int], entry: AdapterCertification) -> ChangeResult:
    result = ChangeResult()
    structural: list[str] = []
    base_ctx, new_ctx = base["context"], new["context"]
    if any(base_ctx[k] != new_ctx[k] for k in _TARGET):
        result.deltas.append(DeltaSpec("TARGET_CHANGE", None, None, True, "The application target changed",
                                       {"canonical_url": new_ctx["canonical_url"], "origin": new_ctx["origin"],
                                        "tenant_key": new_ctx["tenant_key"]}))
    if any(base_ctx[k] != new_ctx[k] for k in base_ctx if k not in _TARGET):
        structural.append("context")
    if base["submit_controls"] != new["submit_controls"]:
        structural.append("submit_controls")

    actions = {a["page_field_key"]: (i, a) for i, a in enumerate(plan["actions"])}
    base_by_key = {e["page_field_key"]: e for e in base["elements"]}
    new_by_key = {e["page_field_key"]: e for e in new["elements"]}
    for key in base_by_key.keys() - new_by_key.keys():
        structural.append(f"removed:{key}")
    for element in new["elements"]:
        key = element["page_field_key"]
        before = base_by_key.get(key)
        if before is None:
            if element["classification"] == "APPLICATION":
                result.deltas.append(_new_field_delta(element, entry))
            else:
                structural.append(f"added:{key}")
            continue
        if before["field_fingerprint"] == element["field_fingerprint"] and \
                before["classification"] == element["classification"]:
            continue
        old_id, new_id = before["identity"], element["identity"]
        changed = {k for k in old_id if old_id[k] != new_id[k]}
        action = actions.get(key, (None, {}))[1]
        answer_key = action.get("answer_key")
        question = _question(element)
        if before["classification"] != element["classification"] or not changed <= set(_WORDING):
            structural.append(f"changed:{key}")
        elif action.get("action_kind") == "OMIT" and changed == {"required"} and new_id["required"]:
            result.deltas.append(DeltaSpec("OMIT_FIELD_REQUIRED", answer_key, None, True, question, _observed(element)))
        else:
            result.deltas.append(DeltaSpec("CHANGED_QUESTION", answer_key, None, bool(new_id["required"]), question,
                                           _observed(element)))

    if result.deltas:
        result.stop_reason = "DELTA_OPENED"
        result.detail = {"delta_kinds": sorted(d.kind for d in result.deltas), "structural": sorted(structural)}
        return result
    if structural:
        result.stop_reason, result.detail = "STRUCTURE_CHANGED", {"changes": sorted(structural)}
        return result

    for key, (index, action) in actions.items():
        element = new_by_key.get(key)
        if element is None or element["classification"] != "APPLICATION":
            continue
        state = element["value_state"]
        current = state.get("current_value_hash")
        if action["action_kind"] == "OMIT":
            if state["state"] != "BLANK":
                return ChangeResult([], "OMIT_FIELD_NOT_BLANK", {"page_field_key": key})
        elif action["action_kind"] in ("WRITE", "ATTACH_LOCAL"):
            if index in completed:
                if current != action["rendered_value_hash"]:
                    return ChangeResult([], "FIELD_VALUE_REVERTED", {"page_field_key": key})
            elif state["state"] != "BLANK" and current != action["rendered_value_hash"]:
                return ChangeResult([], "PREFILLED_VALUE_CONFLICT", {"page_field_key": key})
    return result
