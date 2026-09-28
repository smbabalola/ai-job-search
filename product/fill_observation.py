"""Bundle 6D-B observation v1 (spec §7). Pure: validation, canonical field
and structure fingerprints, unsupported causes and identity ambiguity.

Wire format (emitted by the extension observer, Task 11):

    {"schema_version": "fill-observation.v1",
     "context": {"canonical_url", "origin", "adapter_id", "adapter_version", "tenant_key", "ats_job_id",
                 "frames": [{"frame_path", "origin"}], "application_root_found", "multi_step_indicators": [..]},
     "elements": [{"page_field_key", "control_kind", "identity": {...}, "field_fingerprint",
                   "classification": "APPLICATION" | "NON_APPLICATION",
                   "proof": null | {"kind", "rule"},
                   "value_state": {"state": "BLANK"} | {"state": "NONBLANK", "current_value_hash"}}],
     "submit_controls": [{"control_fingerprint"}]}

No cleartext value is ever present: value state is a hash (product.fill_hash),
and page options use option_value/option_text (page data, not candidate data).
The server recomputes every field_fingerprint and refuses a mismatch."""
from __future__ import annotations

import re
from typing import Any, Mapping

from product.autonomy_contract import canonical_hash
from product.fill_vocab import NON_APPLICATION_PROOFS

OBSERVATION_SCHEMA = "fill-observation.v1"
CONTROL_KINDS = ("text", "email", "tel", "url", "number", "date", "textarea", "select", "checkbox", "radio", "file",
                 "hidden", "custom")
MULTI_STEP_INDICATORS = ("NEXT_BUTTON", "STEP_INDICATOR", "PAGINATED_FORM")
_TOP = {"schema_version", "context", "elements", "submit_controls"}
_CONTEXT = {"canonical_url", "origin", "adapter_id", "adapter_version", "tenant_key", "ats_job_id", "frames",
            "application_root_found", "multi_step_indicators"}
_ELEMENT = {"page_field_key", "control_kind", "identity", "field_fingerprint", "classification", "proof",
            "value_state"}
_IDENTITY = {"tag", "type", "name", "id", "form_owner", "label", "question", "aria", "required", "disabled",
             "readonly", "visible", "options", "accept", "multiple", "maxlength", "pattern", "min", "max",
             "frame_path"}
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class ObservationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def field_fingerprint(identity: Mapping[str, Any]) -> str:
    return canonical_hash("fill-field", "v1", dict(identity))


def _element_structure(element: Mapping[str, Any]) -> dict[str, Any]:
    return {"page_field_key": element["page_field_key"], "control_kind": element["control_kind"],
            "field_fingerprint": element["field_fingerprint"], "classification": element["classification"],
            "proof": element["proof"]}


def structure_fingerprint(doc: Mapping[str, Any]) -> str:
    """Everything except mutable value state (spec §7.2, D11)."""
    return canonical_hash("fill-structure", "v1", {
        "context": doc["context"],
        "elements": [_element_structure(e) for e in doc["elements"]],
        "submit_controls": doc["submit_controls"],
    })


def value_states(doc: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["page_field_key"]: e["value_state"] for e in doc["elements"] if e["classification"] == "APPLICATION"}


def observation_fingerprint(doc: Mapping[str, Any]) -> str:
    return canonical_hash("fill-observation", "v1", {"structure_fingerprint": structure_fingerprint(doc),
                                                     "value_states": value_states(doc)})


def _check_value_state(state: Any, path: str, errors: list[str]) -> None:
    if state == {"state": "BLANK"}:
        return
    if (isinstance(state, dict) and set(state) == {"state", "current_value_hash"} and state["state"] == "NONBLANK"
            and isinstance(state["current_value_hash"], str) and _HASH.match(state["current_value_hash"])):
        return
    errors.append(f"{path}.value_state: must be BLANK or NONBLANK with a sha256 current_value_hash")


def _check_proof(element: Mapping[str, Any], path: str, errors: list[str]) -> None:
    proof = element["proof"]
    if element["classification"] == "APPLICATION":
        if proof is not None:
            errors.append(f"{path}.proof: an APPLICATION element carries no proof")
        return
    if not (isinstance(proof, dict) and set(proof) == {"kind", "rule"} and proof["kind"] in NON_APPLICATION_PROOFS):
        errors.append(f"{path}.proof: NON_APPLICATION needs one of {list(NON_APPLICATION_PROOFS)}")
        return
    if proof["kind"] == "ADAPTER_NON_APPLICATION_RULE" and not (isinstance(proof["rule"], str) and "@" in proof["rule"]):
        errors.append(f"{path}.proof.rule: an adapter rule proof names a versioned rule")
    if proof["kind"] != "ADAPTER_NON_APPLICATION_RULE" and proof["rule"] is not None:
        errors.append(f"{path}.proof.rule: only adapter rule proofs name a rule")


def validate_observation(doc: Any, catalogue: Mapping[Any, Any] | None = None) -> None:
    """Structural validation. With a catalogue, adapter non-application rule
    proofs are also re-verified server-side (spec §7.3)."""
    errors: list[str] = []
    if not isinstance(doc, dict) or set(doc) != _TOP:
        raise ObservationError([f"observation keys must be exactly {sorted(_TOP)}"])
    if doc["schema_version"] != OBSERVATION_SCHEMA:
        errors.append(f"schema_version must be {OBSERVATION_SCHEMA}")
    context = doc["context"]
    if not isinstance(context, dict) or set(context) != _CONTEXT:
        raise ObservationError(errors + [f"context keys must be exactly {sorted(_CONTEXT)}"])
    if not all(isinstance(m, str) and m in MULTI_STEP_INDICATORS for m in context["multi_step_indicators"]):
        errors.append("context.multi_step_indicators: unknown indicator")
    frame_paths = set()
    for frame in context["frames"]:
        if not (isinstance(frame, dict) and set(frame) == {"frame_path", "origin"}):
            errors.append("context.frames: each frame has frame_path and origin")
        else:
            frame_paths.add(frame["frame_path"])
    keys: set[str] = set()
    for i, element in enumerate(doc["elements"]):
        path = f"elements[{i}]"
        if not isinstance(element, dict) or set(element) != _ELEMENT:
            errors.append(f"{path}: keys must be exactly {sorted(_ELEMENT)}")
            continue
        if element["page_field_key"] in keys:
            errors.append(f"{path}.page_field_key: duplicate {element['page_field_key']!r}")
        keys.add(element["page_field_key"])
        if element["control_kind"] not in CONTROL_KINDS:
            errors.append(f"{path}.control_kind: unknown {element['control_kind']!r}")
        identity = element["identity"]
        if not isinstance(identity, dict) or set(identity) != _IDENTITY:
            errors.append(f"{path}.identity: keys must be exactly {sorted(_IDENTITY)}")
        else:
            if field_fingerprint(identity) != element["field_fingerprint"]:
                errors.append(f"{path}.field_fingerprint: does not match the identity")
            if identity["frame_path"] not in frame_paths:
                errors.append(f"{path}.identity.frame_path: not in context.frames")
            for option in identity["options"]:
                if not (isinstance(option, dict) and set(option) == {"option_value", "option_text"}):
                    errors.append(f"{path}.identity.options: each option has option_value and option_text")
        if element["classification"] not in ("APPLICATION", "NON_APPLICATION"):
            errors.append(f"{path}.classification: must be APPLICATION or NON_APPLICATION")
        else:
            _check_proof(element, path, errors)
        _check_value_state(element["value_state"], path, errors)
    for i, control in enumerate(doc["submit_controls"]):
        if not (isinstance(control, dict) and set(control) == {"control_fingerprint"}
                and _HASH.match(str(control["control_fingerprint"]))):
            errors.append(f"submit_controls[{i}]: must be a control_fingerprint sha256")
    if not errors and catalogue is not None:
        from product.fill_certification import certified, verify_non_application_rule
        entry = certified(context["adapter_id"], context["adapter_version"])
        for i, element in enumerate(doc["elements"]):
            proof = element["proof"]
            if proof and proof["kind"] == "ADAPTER_NON_APPLICATION_RULE" and (
                    entry is None or not verify_non_application_rule(entry, proof["rule"], element)):
                errors.append(f"elements[{i}].proof: adapter rule {proof['rule']!r} does not hold for this element")
    if errors:
        raise ObservationError(errors)


def ambiguous_identities(doc: Mapping[str, Any]) -> list[list[str]]:
    """Groups of APPLICATION elements with indistinguishable identity. Never
    resolved by DOM order (spec §7.2)."""
    groups: dict[str, list[str]] = {}
    for element in doc["elements"]:
        if element["classification"] == "APPLICATION":
            groups.setdefault(element["field_fingerprint"], []).append(element["page_field_key"])
    return [keys for keys in groups.values() if len(keys) > 1]


def unsupported_causes(doc: Mapping[str, Any], catalogue: Mapping[Any, Any]) -> list[str]:
    """Closed §14 causes decidable from the observation alone (the widget
    cause needs the approval and is decided by the plan builder)."""
    from product.fill_certification import certified
    context = doc["context"]
    causes: list[str] = []
    if certified(context["adapter_id"], context["adapter_version"]) is None:
        causes.append("UNCERTIFIED_ADAPTER")
    if not context["application_root_found"]:
        causes.append("NO_APPLICATION_ROOT")
    if context["multi_step_indicators"]:
        causes.append("MULTI_STEP")
    origins = {f["frame_path"]: f["origin"] for f in context["frames"]}
    if any(e["classification"] == "APPLICATION" and origins.get(e["identity"]["frame_path"]) != context["origin"]
           for e in doc["elements"]):
        causes.append("CROSS_ORIGIN_APPLICATION_FRAME")
    if ambiguous_identities(doc):
        causes.append("AMBIGUOUS_FIELD_IDENTITY")
    return causes
