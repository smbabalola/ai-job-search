"""Bundle 6D-B adapter certification catalogue (spec §7.4, D15). Pure.

Only an adapter VERSION listed here may run safe FILL. Generic and unknown
adapters never appear. Each entry declares:
- network_model = NO_UNCONTAINED_PERSISTENT_CHANNELS (§10.8): its pages use no
  WebRTC and no persistent channel the reset and Q1/Q2 can't contain;
- supported control kinds, which are framework-specific capability
  allowlists from the Task 1 S3 evidence. React-rendered forms don't
  register checkbox/radio without a click, and the executor never clicks,
  so React never lists them;
- deterministic mapping, non-application and classification rules, each
  versioned and carrying positive and negative fixtures.

A rule is deterministic only if it is the ONLY rule that matches."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

NETWORK_MODEL = "NO_UNCONTAINED_PERSISTENT_CHANNELS"
CONTACT_ANSWER_KEYS = frozenset({"contact:full_name", "contact:email", "contact:phone", "contact:location"})

_BASE_KINDS = frozenset({"text", "email", "tel", "url", "number", "date", "textarea", "select", "file"})
FRAMEWORK_CONTROL_KINDS: dict[str, frozenset[str]] = {
    # S3: text/select register without click; checkbox/radio are reverted.
    "react": _BASE_KINDS,
    # S3: checkbox/radio also register without click.
    "vue": _BASE_KINDS | {"checkbox", "radio"},
    # No framework: no S3 evidence for checkbox/radio, so they're not claimed.
    "dom": _BASE_KINDS,
}

# Ported from the extension's legal-patterns.ts (the Phase 3 declaration guard).
DECLARATION_PATTERNS = (r"\bcertify\b", r"\battest\b", r"\belectronic\s*signature\b", r"\be-?sign(ature)?\b",
                        r"\bunder\s*penalty\s*of\s*perjury\b", r"\bI\s*agree\b")

# Ported from the extension's safe-catalog.ts guards.
_THIRD_PARTY = re.compile(r"\b(reference|referee|recruiter|hiring\s*manager|manager|supervisor|contact|emergency|"
                          r"third[\s-]*party)\b", re.I)
_EMPLOYER_CONTEXT = re.compile(r"\b(employer|company|institution|university|school|organization)\b", re.I)
_WORK_LOCATION = re.compile(r"\b(work|office)[\s-]*location\b", re.I)


def _text(element: Mapping[str, Any]) -> str:
    identity = element["identity"]
    return identity["label"] or identity["question"] or ""


@dataclass(frozen=True)
class MappingRule:
    rule_id: str
    target: tuple[str, str]  # ("answer_key", key) | ("document_kind", kind)
    predicate: Callable[[Mapping[str, Any]], bool]
    positives: tuple[Mapping[str, Any], ...]
    negatives: tuple[Mapping[str, Any], ...]

    def matches(self, element: Mapping[str, Any]) -> bool:
        return self.predicate(element)


@dataclass(frozen=True)
class NonApplicationRule:
    rule_id: str
    predicate: Callable[[Mapping[str, Any]], bool]
    positives: tuple[Mapping[str, Any], ...]
    negatives: tuple[Mapping[str, Any], ...]

    def matches(self, element: Mapping[str, Any]) -> bool:
        return self.predicate(element)


@dataclass(frozen=True)
class ClassificationRule:
    rule_id: str
    subject: str
    pattern: str
    positives: tuple[str, ...]
    negatives: tuple[str, ...]

    @property
    def target(self) -> str:
        return self.subject

    def matches(self, question: str) -> bool:
        return re.search(self.pattern, question, re.I) is not None


@dataclass(frozen=True)
class AdapterCertification:
    adapter_id: str
    adapter_version: str
    network_model: str
    framework: str
    supported_control_kinds: frozenset[str]
    mapping_rules: tuple[MappingRule, ...]
    non_application_rules: tuple[NonApplicationRule, ...]
    classification_rules: tuple[ClassificationRule, ...]
    declaration_patterns: tuple[str, ...]


def _el(label: str, kind: str = "text", name: str = "q", type_: str = "text", visible: bool = True) -> dict[str, Any]:
    """A minimal element fixture for rule positives/negatives."""
    return {"control_kind": kind, "identity": {"label": label, "question": label, "name": name, "type": type_,
                                               "visible": visible}}


def _contact(pattern: str, *, kinds=("text", "email", "tel"), location_guard: bool = False):
    regex = re.compile(pattern, re.I)

    def predicate(element: Mapping[str, Any]) -> bool:
        text = _text(element)
        if element["control_kind"] not in kinds or not regex.search(text) or _THIRD_PARTY.search(text):
            return False
        return not (location_guard and (_EMPLOYER_CONTEXT.search(text) or _WORK_LOCATION.search(text)))
    return predicate


def _document(pattern: str):
    regex = re.compile(pattern, re.I)
    return lambda element: element["control_kind"] == "file" and regex.search(_text(element)) is not None


def _hidden_names(names: frozenset[str]):
    return lambda element: element["control_kind"] == "hidden" and element["identity"]["name"] in names


def _mapping_rules(prefix: str) -> tuple[MappingRule, ...]:
    return (
        MappingRule(f"{prefix}.contact_full_name@1", ("answer_key", "contact:full_name"),
                    _contact(r"^\s*(full\s*name|your\s*name|name)\s*\*?\s*$"),
                    (_el("Full name"), _el("Name"), _el("Your name *")),
                    (_el("First Name"), _el("Reference name"), _el("Company name"))),
        MappingRule(f"{prefix}.contact_email@1", ("answer_key", "contact:email"),
                    _contact(r"\be-?mail\b"),
                    (_el("Email"), _el("E-mail address", kind="email")),
                    (_el("Reference email"), _el("Email", kind="file", type_="file"))),
        MappingRule(f"{prefix}.contact_phone@1", ("answer_key", "contact:phone"),
                    _contact(r"\b(phone|mobile|telephone)\b"),
                    (_el("Phone"), _el("Mobile number", kind="tel")),
                    (_el("Emergency contact phone"), _el("Phone", kind="select"))),
        MappingRule(f"{prefix}.contact_location@1", ("answer_key", "contact:location"),
                    _contact(r"\b(location|city)\b", location_guard=True),
                    (_el("Location (City)"), _el("City")),
                    (_el("Company location"), _el("Work location"), _el("Relocation"))),
        MappingRule(f"{prefix}.document_cv@1", ("document_kind", "cv"),
                    _document(r"\bresume\b|\bcv\b"),
                    (_el("Resume/CV", kind="file", type_="file"),),
                    (_el("Resume/CV"), _el("Cover Letter", kind="file", type_="file"))),
        MappingRule(f"{prefix}.document_cover_letter@1", ("document_kind", "cover_letter"),
                    _document(r"\bcover\s*letter\b"),
                    (_el("Cover Letter", kind="file", type_="file"),),
                    (_el("Cover Letter"), _el("Resume/CV", kind="file", type_="file"))),
    )


def _classification_rules(prefix: str) -> tuple[ClassificationRule, ...]:
    return (
        ClassificationRule(f"{prefix}.notice_period@1", "employment.notice_period", r"\bnotice\s+period\b",
                           ("What is your notice period?",), ("How did you hear about us?",)),
        ClassificationRule(f"{prefix}.availability_start@1", "employment.availability_start",
                           r"\b(start\s+date|when\s+can\s+you\s+start|earliest\s+(possible\s+)?start)\b",
                           ("What is your earliest start date?", "When can you start?"), ("Notice period",)),
        ClassificationRule(f"{prefix}.right_to_work@1", "work_authorization.right_to_work",
                           r"\b(authori[sz]ed|eligible|right)\s+to\s+work\b",
                           ("Are you legally authorized to work in the UK?", "Do you have the right to work?"),
                           ("Will you require visa sponsorship?",)),
        ClassificationRule(f"{prefix}.sponsorship@1", "work_authorization.sponsorship_required", r"\bsponsor(ship)?\b",
                           ("Will you now or in the future require visa sponsorship?",),
                           ("Are you authorized to work in the UK?",)),
        ClassificationRule(f"{prefix}.salary@1", "compensation.salary_expectation",
                           r"\bsalary\b|\bcompensation\s+expectations?\b",
                           ("What are your salary expectations?",), ("What is your notice period?",)),
        ClassificationRule(f"{prefix}.relocation@1", "mobility.relocation", r"\brelocat(e|ion)\b",
                           ("Are you willing to relocate?",), ("Where are you located?",)),
    )


def _entry(adapter_id: str, version: str, framework: str, hidden_names: frozenset[str]) -> AdapterCertification:
    prefix = adapter_id
    return AdapterCertification(
        adapter_id=adapter_id, adapter_version=version, network_model=NETWORK_MODEL, framework=framework,
        supported_control_kinds=FRAMEWORK_CONTROL_KINDS[framework],
        mapping_rules=_mapping_rules(prefix),
        non_application_rules=(NonApplicationRule(
            f"{prefix}.site_state_hidden@1", _hidden_names(hidden_names),
            tuple(_el("", kind="hidden", name=n, type_="hidden", visible=False) for n in sorted(hidden_names)),
            (_el("", kind="hidden", name="custom_question_4", type_="hidden", visible=False),
             _el("Email", name=sorted(hidden_names)[0]))),),
        classification_rules=_classification_rules(prefix),
        declaration_patterns=DECLARATION_PATTERNS,
    )


# greenhouse@2 / lever@2 are the fill-capable adapter versions (the Phase 3
# greenhouse@1 / lever@1 autofill adapters are not certified).
CATALOGUE: dict[tuple[str, str], AdapterCertification] = {
    ("greenhouse", "greenhouse@2"): _entry("greenhouse", "greenhouse@2", "react",
                                           frozenset({"authenticity_token", "utf8", "_method"})),
    ("lever", "lever@2"): _entry("lever", "lever@2", "dom", frozenset({"_csrf"})),
}


def certified(adapter_id: str, adapter_version: str) -> AdapterCertification | None:
    return CATALOGUE.get((adapter_id, adapter_version))


def deterministic_mapping(entry: AdapterCertification, element: Mapping[str, Any]) -> MappingRule | None:
    matches = [rule for rule in entry.mapping_rules if rule.matches(element)]
    return matches[0] if len(matches) == 1 else None


def deterministic_classification(entry: AdapterCertification, question: str) -> ClassificationRule | None:
    matches = [rule for rule in entry.classification_rules if rule.matches(question)]
    return matches[0] if len(matches) == 1 else None


def is_declaration(entry: AdapterCertification, text: str) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in entry.declaration_patterns)


def verify_non_application_rule(entry: AdapterCertification, rule_id: str, element: Mapping[str, Any]) -> bool:
    rule = next((r for r in entry.non_application_rules if r.rule_id == rule_id), None)
    return rule is not None and rule.matches(element)
