"""Bundle 6D-B plan builder, manifest derivation and G4 coverage (spec §8,
§11.2.1). Pure.

build_plan binds every observed element to exactly one action (WRITE,
ATTACH_LOCAL, OMIT, IGNORE_NON_APPLICATION) against the effective 6D-A
binding, or reports why it can't: deltas (6D-A kinds), review rows
(approved content, no deterministic mapping), unsupported causes, or a
precondition stop. A plan is produced only when none of those exist.

`values` (answer_key -> approved cleartext) is used ONLY to compute
rendered_value_hash; no cleartext enters the plan."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from product.autonomy_contract import canonical_hash
from product.fill_certification import (
    CATALOGUE, AdapterCertification, deterministic_classification, deterministic_mapping, is_declaration,
)
from product.fill_hash import UnpairedSurrogateError, fill_value_hash
from product.fill_manifest import FILL_MANIFEST_SCHEMA_VERSION, validate_fill_manifest, value_hash
from product.fill_observation import observation_fingerprint, structure_fingerprint, unsupported_causes
from product.job_identity import job_identity
from product.representation_transforms import TransformError, apply_transform

PLAN_SCHEMA = "fill-plan.v1"
TEXT_KINDS = frozenset({"text", "email", "tel", "url", "number", "date", "textarea"})
# Checkbox/radio need group semantics no certified adapter uses yet (Task 6
# ruling): an approved ANSWER on them is refused as an unsupported widget.
_UNWRITABLE_KINDS = frozenset({"checkbox", "radio", "hidden", "custom"})
_OPTION_TRANSFORMS = ("identity", "whitespace_normalize", "country_iso2_to_name", "country_name_to_iso2")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_MIME_BY_EXT = {".pdf": "application/pdf", ".doc": "application/msword",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".txt": "text/plain", ".rtf": "application/rtf", ".odt": "application/vnd.oasis.opendocument.text"}
_ACCEPT_TOKEN = re.compile(r"^(\.[a-z0-9]+|[a-z]+/([a-z0-9.+-]+|\*))$")
DECLARATION_PROPOSAL_SUBJECT = "legal.attestation"


@dataclass(frozen=True)
class DeltaSpec:
    kind: str
    answer_key: str | None
    subject: str | None
    required: bool
    question: str
    observed: dict[str, Any]
    proposal_subject: str | None = None


@dataclass(frozen=True)
class ReviewRow:
    page_field_key: str
    field_fingerprint: str
    question: str
    control_kind: str
    candidates: tuple[str, ...]


@dataclass
class PlanResult:
    plan: dict[str, Any] | None
    deltas: list[DeltaSpec] = field(default_factory=list)
    needs_review: list[ReviewRow] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    stop: tuple[str, dict[str, Any]] | None = None


class _Refuse(Exception):
    def __init__(self, kind: str, detail: Any = None):
        self.kind, self.detail = kind, detail


def _question(element: Mapping[str, Any]) -> str:
    identity = element["identity"]
    return identity["question"] or identity["label"] or identity["name"] or element["page_field_key"]


def _options_hash(element: Mapping[str, Any]) -> str:
    return canonical_hash("fill-options", "v1", element["identity"]["options"])


def _observed(element: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    return {"field_key": element["page_field_key"], "field_fingerprint": element["field_fingerprint"],
            "question": _question(element), "required": bool(element["identity"]["required"]),
            "options_hash": _options_hash(element), **extra}


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _match_option(value: str, options: list[dict[str, str]], permitted: list[str]) -> tuple[str, str]:
    """(transform_id, option_value) for exactly one matching option; never the
    first of several (Review Focus 3)."""
    for transform_id in (t for t in _OPTION_TRANSFORMS if t in permitted):
        try:
            rendered = apply_transform(transform_id, value)
        except TransformError:
            continue
        exact = [o for o in options if o["option_value"] == rendered and o["option_value"] != ""]
        if len(exact) > 1:
            raise _Refuse("TRANSFORM_FAILURE", "ambiguous_option")
        if len(exact) == 1:
            return transform_id, exact[0]["option_value"]
        loose = [o for o in options if o["option_value"] != "" and
                 (_norm(o["option_value"]) == _norm(rendered) or _norm(o["option_text"]) == _norm(rendered))]
        if len(loose) > 1:
            raise _Refuse("TRANSFORM_FAILURE", "ambiguous_option")
        if len(loose) == 1:
            return transform_id, loose[0]["option_value"]
    raise _Refuse("TRANSFORM_FAILURE", "no_matching_option")


def _render_text(element: Mapping[str, Any], value: str, permitted: list[str]) -> tuple[str, str]:
    if "identity" not in permitted:
        raise _Refuse("TRANSFORM_FAILURE", "identity_not_permitted")
    identity, kind = element["identity"], element["control_kind"]
    if identity["maxlength"] is not None and len(value) > int(identity["maxlength"]):
        raise _Refuse("TRANSFORM_FAILURE", "maxlength")
    if kind == "number" and not _NUMBER.match(value):
        raise _Refuse("TRANSFORM_FAILURE", "not_a_number")
    if kind == "date" and not _ISO_DATE.match(value):
        raise _Refuse("TRANSFORM_FAILURE", "not_an_iso_date")
    if kind in ("number", "date"):
        for bound, cmp in (("min", lambda v, b: v < b), ("max", lambda v, b: v > b)):
            limit = identity[bound]
            if limit is not None:
                v, b = (float(value), float(limit)) if kind == "number" else (value, str(limit))
                if cmp(v, b):
                    raise _Refuse("TRANSFORM_FAILURE", bound)
    if identity["pattern"]:
        try:
            if re.fullmatch(identity["pattern"], value) is None:
                raise _Refuse("TRANSFORM_FAILURE", "pattern")
        except re.error as exc:  # an HTML pattern Python can't evaluate: fail closed
            raise _Refuse("TRANSFORM_FAILURE", "pattern_unverifiable") from exc
    return "identity", value


def _render(element: Mapping[str, Any], value: Any, permitted: list[str]) -> tuple[str, str]:
    if not isinstance(value, str):
        raise _Refuse("TRANSFORM_FAILURE", "not_a_string")
    if element["control_kind"] == "select":
        return _match_option(value, element["identity"]["options"], permitted)
    return _render_text(element, value, permitted)


def _accepts(accept: str | None, document: Mapping[str, Any]) -> bool:
    """True/False for a clear answer; raises _Refuse('UNPARSEABLE') otherwise."""
    tokens = [t.strip().lower() for t in (accept or "").split(",") if t.strip()]
    if not tokens:
        return True
    if not all(_ACCEPT_TOKEN.match(t) for t in tokens):
        raise _Refuse("UNPARSEABLE")
    ext = "." + document["filename"].rsplit(".", 1)[-1].lower() if "." in document["filename"] else ""
    media = document["media_type"].lower()
    return ext in tokens or media in tokens or f"{media.split('/')[0]}/*" in tokens


def _required_media_type(accept: str) -> str:
    for token in (t.strip().lower() for t in accept.split(",") if t.strip()):
        if token.startswith("."):
            if token in _MIME_BY_EXT:
                return _MIME_BY_EXT[token]
        elif not token.endswith("/*"):
            return token
    return accept


def _blank_action(element: Mapping[str, Any]) -> dict[str, Any]:
    return {"page_field_key": element["page_field_key"], "field_fingerprint": element["field_fingerprint"],
            "action_kind": None, "answer_key": None, "document_kind": None, "source_ref": None, "value_hash": None,
            "transform_id": None, "rendered_value_hash": None, "document": None, "mapping_basis": None,
            "preconditions": None, "required": bool(element["identity"]["required"])}


def _compatible(element: Mapping[str, Any], fields: Mapping[str, Mapping[str, Any]], values: Mapping[str, Any],
                documents: Mapping[str, Any]) -> tuple[str, ...]:
    """Approved answer keys that could be placed in this element without a
    transform failure (spec §8.4: never all approved answers)."""
    if element["control_kind"] == "file":
        return tuple(sorted(f"document:{kind}" for kind in documents))
    if element["control_kind"] not in TEXT_KINDS | {"select"}:
        return ()
    out = []
    for key, f in fields.items():
        if f["disposition"] == "OMIT":
            out.append(key)
        elif f["disposition"] == "ANSWER" and key in values:
            try:
                _render(element, values[key], f["permitted_transforms"])
            except _Refuse:
                continue
            out.append(key)
    return tuple(sorted(out))


def plan_hash(plan: Mapping[str, Any]) -> str:
    return canonical_hash("fill-plan", "v1", {k: v for k, v in plan.items() if k != "plan_hash"})


def build_plan(*, observation: Mapping[str, Any], binding: Mapping[str, Any], approval_id: str, binding_hash: str,
               catalogue_entry: AdapterCertification | None, mapping_choices: Mapping[str, Mapping[str, Any]],
               values: Mapping[str, Any], documents: Mapping[str, Mapping[str, Any]],
               delta_fields: Mapping[str, Mapping[str, Any]] | None = None) -> PlanResult:
    """delta_fields: page_field_key -> {answer_key, field_fingerprint, delta_id}
    for 6D-A deltas this bundle opened for an exact observed field. Once the
    user has answered one, that same field (same fingerprint) maps to it."""
    unsupported = unsupported_causes(observation, CATALOGUE)
    if catalogue_entry is None and "UNCERTIFIED_ADAPTER" not in unsupported:
        unsupported.append("UNCERTIFIED_ADAPTER")
    if unsupported:
        return PlanResult(None, unsupported=unsupported)
    entry = catalogue_entry
    # The page must be the approved apply target (spec §8.3): same canonical
    # key as the 6D-A binding (product.job_identity canonicalization).
    observed_target = job_identity({"source_url": observation["context"]["canonical_url"]}).canonical_url_key
    if binding["apply_target"]["canonical_url"] != observed_target:
        return PlanResult(None, deltas=[DeltaSpec(
            "TARGET_CHANGE", None, None, True, "The application page is not the approved apply target",
            {"canonical_url": observed_target, "approved_canonical_url": binding["apply_target"]["canonical_url"],
             "origin": observation["context"]["origin"], "tenant_key": observation["context"]["tenant_key"]})])
    fields = {f["answer_key"]: f for f in binding["fields"]}
    bound_docs = {d["kind"]: d for d in binding["documents"]}
    documents = {k: v for k, v in documents.items() if k in bound_docs}
    result = PlanResult(None)
    actions: list[dict[str, Any] | None] = []
    targets: dict[int, tuple[str, str, str]] = {}  # index -> (kind, target, basis)
    unresolved: list[int] = []

    # Pass 1: user choices (still valid for this exact field) and deterministic rules.
    for i, element in enumerate(observation["elements"]):
        actions.append(None)
        if element["classification"] == "NON_APPLICATION":
            action = _blank_action(element)
            action.update(action_kind="IGNORE_NON_APPLICATION",
                          mapping_basis=f"NON_APPLICATION_PROOF({element['proof']['kind']})")
            actions[i] = action
            continue
        key = element["page_field_key"]
        answered = (delta_fields or {}).get(key)
        if answered and answered["field_fingerprint"] == element["field_fingerprint"]                 and answered["answer_key"] in fields:
            targets[i] = ("answer_key", answered["answer_key"], f"USER_CONFIRMED(delta:{answered['delta_id']})")
            continue
        choice = mapping_choices.get(key)
        if choice and choice.get("field_fingerprint") == element["field_fingerprint"]:
            if choice["choice"] == "NEW_QUESTION":
                targets[i] = ("new_question", "", f"USER_CONFIRMED({choice['id']})")
                continue
            answer_key = choice.get("answer_key") or ""
            if answer_key in _compatible(element, fields, values, documents):
                kind = "document_kind" if answer_key.startswith("document:") else "answer_key"
                target = answer_key.split(":", 1)[1] if kind == "document_kind" else answer_key
                targets[i] = (kind, target, f"USER_CONFIRMED({choice['id']})")
                continue
        rule = deterministic_mapping(entry, element)
        if rule is not None:
            targets[i] = (rule.target[0], rule.target[1], f"ADAPTER_RULE({rule.rule_id})")
            continue
        classified = deterministic_classification(entry, _question(element)) \
            if element["control_kind"] != "file" else None
        if classified is not None:
            if f"subject:{classified.subject}" in fields:
                targets[i] = ("answer_key", f"subject:{classified.subject}", f"ADAPTER_RULE({classified.rule_id})")
            else:  # a known meaning that is not approved: a new (classified) question
                targets[i] = ("new_question", "", "")
            continue
        unresolved.append(i)

    placed_docs = {t[1] for t in targets.values() if t[0] == "document_kind"}
    placed_keys = {t[1] for t in targets.values() if t[0] == "answer_key"}

    # Pass 2: anything unplaced is a review row (compatible candidates) or a delta.
    for i in unresolved:
        element = observation["elements"][i]
        question = _question(element)
        if is_declaration(entry, question):
            result.deltas.append(DeltaSpec("DECLARATION", None, None, bool(element["identity"]["required"]),
                                           question, _observed(element), DECLARATION_PROPOSAL_SUBJECT))
            continue
        if element["control_kind"] == "file":
            free = tuple(sorted(f"document:{k}" for k in documents if k not in placed_docs))
            if free:
                result.needs_review.append(ReviewRow(element["page_field_key"], element["field_fingerprint"],
                                                     question, "file", free))
            else:
                result.deltas.append(DeltaSpec("NEW_UPLOAD", None, None, bool(element["identity"]["required"]),
                                               question, _observed(element)))
            continue
        # Only approved fields not already placed could be this element; an
        # unknown question with none left is a new question, not a guess.
        candidates = tuple(k for k in _compatible(element, fields, values, documents) if k not in placed_keys)
        if candidates:
            result.needs_review.append(ReviewRow(element["page_field_key"], element["field_fingerprint"], question,
                                                 element["control_kind"], candidates))
        else:
            targets[i] = ("new_question", "", "")

    # Actions for every placed element.
    for i, (kind, target, basis) in sorted(targets.items()):
        element = observation["elements"][i]
        question = _question(element)
        if kind == "new_question":
            classified = deterministic_classification(entry, question)
            result.deltas.append(DeltaSpec("NEW_QUESTION", None, classified.subject if classified else None,
                                           bool(element["identity"]["required"]), question, _observed(element)))
            continue
        action = _blank_action(element)
        action["mapping_basis"] = basis
        state = element["value_state"]
        if kind == "document_kind":
            document = documents.get(target)
            if document is None:
                result.deltas.append(DeltaSpec("NEW_UPLOAD", None, None, bool(element["identity"]["required"]),
                                               question, _observed(element)))
                continue
            try:
                accepted = _accepts(element["identity"]["accept"], document)
            except _Refuse:
                result.unsupported.append("UNSUPPORTED_REQUIRED_WIDGET")
                continue
            if not accepted:
                result.deltas.append(DeltaSpec("DOCUMENT_CONVERSION", None, None, bool(element["identity"]["required"]),
                                               question, _observed(element, kind=target, required_media_type=
                                                                   _required_media_type(element["identity"]["accept"]))))
                continue
            rendered_hash = fill_value_hash(document["sha256"])
            if state["state"] == "NONBLANK" and state["current_value_hash"] != rendered_hash:
                result.stop = result.stop or ("PREFILLED_VALUE_CONFLICT", {"page_field_key": element["page_field_key"]})
                continue
            action.update(action_kind="ATTACH_LOCAL", document_kind=target, source_ref=document["document_version_id"],
                          value_hash=value_hash(document["sha256"]), transform_id="identity",
                          rendered_value_hash=rendered_hash, document=dict(document), preconditions="BLANK_OR_EQUAL")
            actions[i] = action
            continue
        f = fields.get(target)
        if f is None:
            result.deltas.append(DeltaSpec("NEW_QUESTION", None, None, bool(element["identity"]["required"]), question,
                                           _observed(element)))
            continue
        action["answer_key"] = target
        if f["disposition"] == "OMIT":
            if element["identity"]["required"]:
                result.deltas.append(DeltaSpec("OMIT_FIELD_REQUIRED", target, None, True, question, _observed(element)))
                continue
            if state["state"] != "BLANK":
                result.stop = result.stop or ("OMIT_FIELD_NOT_BLANK", {"page_field_key": element["page_field_key"]})
                continue
            action.update(action_kind="OMIT", preconditions="BLANK")
            actions[i] = action
            continue
        if f["disposition"] != "ANSWER":
            result.needs_review.append(ReviewRow(element["page_field_key"], element["field_fingerprint"], question,
                                                 element["control_kind"], (target,)))
            continue
        if element["control_kind"] in _UNWRITABLE_KINDS or element["control_kind"] not in entry.supported_control_kinds:
            result.unsupported.append("UNSUPPORTED_REQUIRED_WIDGET")
            continue
        if target not in values:
            raise ValueError(f"no approved value supplied for {target!r}")
        try:
            transform_id, rendered = _render(element, values[target], f["permitted_transforms"])
            rendered_hash = fill_value_hash(rendered)
        except (_Refuse, UnpairedSurrogateError) as exc:
            result.deltas.append(DeltaSpec("TRANSFORM_FAILURE", target, None, bool(element["identity"]["required"]),
                                           question, _observed(element, value_hash=f["value_hash"],
                                                               cause=getattr(exc, "detail", "unpaired_surrogate"))))
            continue
        if state["state"] == "NONBLANK" and state["current_value_hash"] != rendered_hash:
            result.stop = result.stop or ("PREFILLED_VALUE_CONFLICT", {"page_field_key": element["page_field_key"]})
            continue
        action.update(action_kind="WRITE", source_ref=f["source_ref"], value_hash=f["value_hash"],
                      transform_id=transform_id, rendered_value_hash=rendered_hash, preconditions="BLANK_OR_EQUAL")
        actions[i] = action

    result.unsupported = sorted(set(result.unsupported))
    if result.deltas or result.needs_review or result.unsupported or result.stop or any(a is None for a in actions):
        return result
    context = observation["context"]
    plan = {
        "schema_version": PLAN_SCHEMA, "account_id": binding["account_id"],
        "application_workspace_id": binding["application_workspace_id"], "approval_id": approval_id,
        "approval_binding_hash": binding_hash, "canonical_target_url": context["canonical_url"],
        "target_origin": context["origin"], "adapter_id": context["adapter_id"],
        "adapter_version": context["adapter_version"], "tenant_key": context["tenant_key"],
        "ats_job_id": context["ats_job_id"], "structure_fingerprint": structure_fingerprint(observation),
        "observation_fingerprint": observation_fingerprint(observation), "actions": actions,
    }
    plan["plan_hash"] = plan_hash(plan)
    result.plan = plan
    return result


# ---- fill-manifest v1 derivation and G4 coverage (spec §11.2.1) ------------------------

def _manifest_entry(action: Mapping[str, Any], fields: Mapping[str, Mapping[str, Any]],
                    confirmation_ids: Mapping[str, str]) -> dict[str, Any]:
    if action["action_kind"] == "ATTACH_LOCAL":
        return {"page_field_key": action["page_field_key"], "normalized_field_type": action["document_kind"],
                "subject": None, "source": {"kind": "PACK_DOCUMENT", "ref": action["document"]["sha256"],
                                            "confirmation_id": None},
                "transform_id": "identity", "value_hash": value_hash(action["document"]["sha256"]),
                "required": action["required"]}
    f = fields[action["answer_key"]]
    prefix, name = action["answer_key"].split(":", 1)
    return {"page_field_key": action["page_field_key"],
            "normalized_field_type": name if prefix == "contact" else None,
            "subject": name if prefix == "subject" else None,
            "source": {"kind": f["source_kind"], "ref": f["source_ref"],
                       "confirmation_id": confirmation_ids.get(action["answer_key"])
                       if f["source_kind"] == "APPROVED_ANSWER" else None},
            "transform_id": action["transform_id"], "value_hash": f["value_hash"], "required": action["required"]}


def derive_manifest(plan: Mapping[str, Any], *, binding: Mapping[str, Any],
                    confirmation_ids: Mapping[str, str]) -> dict[str, Any]:
    fields = {f["answer_key"]: f for f in binding["fields"]}
    entries = [_manifest_entry(a, fields, confirmation_ids) for a in plan["actions"]
               if a["action_kind"] in ("WRITE", "ATTACH_LOCAL")]
    manifest = {"schema_version": FILL_MANIFEST_SCHEMA_VERSION,
                "application_workspace_id": plan["application_workspace_id"], "adapter_id": plan["adapter_id"],
                "adapter_version": plan["adapter_version"],
                "pages": [{"page_key": plan["canonical_target_url"], "entries": entries}]}
    validate_fill_manifest(manifest)
    return manifest


def g4_violations(manifest: Mapping[str, Any], plan: Mapping[str, Any], binding: Mapping[str, Any], *,
                  binding_hash_value: str) -> list[str]:
    """The six rules of spec §11.2.1. Empty means covered."""
    violations: list[str] = []
    if plan["approval_binding_hash"] != binding_hash_value:
        violations.append("rule6: the binding is not the run's approval binding")
    fields = {f["answer_key"]: f for f in binding["fields"]}
    docs = {d["kind"]: d for d in binding["documents"]}
    actions = {a["page_field_key"]: a for a in plan["actions"]}
    writers = {k for k, a in actions.items() if a["action_kind"] in ("WRITE", "ATTACH_LOCAL")}
    entries = [e for page in manifest["pages"] for e in page["entries"]]
    keys = [e["page_field_key"] for e in entries]
    if len(keys) != len(set(keys)) or set(keys) != writers:
        violations.append("rule5: manifest entries are not exactly the plan's WRITE/ATTACH_LOCAL actions")
    omitted = {k for k, f in fields.items() if f["disposition"] == "OMIT"}
    for e in entries:
        action = actions.get(e["page_field_key"])
        if action is None:
            continue
        kind = e["source"]["kind"]
        if kind == "PACK_DOCUMENT":
            doc = docs.get(e["normalized_field_type"])
            if doc is None or doc["sha256"] != e["source"]["ref"] or action["document"] is None or \
                    action["document"]["document_version_id"] != doc["document_version_id"]:
                violations.append(f"rule3: {e['page_field_key']} document is not the approved version/sha256")
            continue
        answer_key = action["answer_key"]
        if answer_key in omitted:
            violations.append(f"rule4: {e['page_field_key']} writes a field bound OMIT")
            continue
        f = fields.get(answer_key)
        rule = "rule1" if kind == "APPROVED_ANSWER" else "rule2"
        if f is None or f["disposition"] != "ANSWER" or f["source_kind"] != kind or \
                f["source_ref"] != e["source"]["ref"] or f["value_hash"] != e["value_hash"] or \
                e["transform_id"] not in f["permitted_transforms"] or e["transform_id"] != action["transform_id"]:
            violations.append(f"{rule}: {e['page_field_key']} does not match an approved {kind} field")
    for key, action in actions.items():
        if action["action_kind"] in ("WRITE", "ATTACH_LOCAL") and action["answer_key"] in omitted:
            violations.append(f"rule4: {key} is a write to a field bound OMIT")
    return sorted(set(violations))
