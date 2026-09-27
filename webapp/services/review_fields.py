"""Bundle 6D-A: the planned field set (spec §8.1, §8.2, §8.4). Pure read.

Every known field gets a disposition, a source and a value hash. Candidate
applicability and answer readiness are the 6B gate's own rules
(usable_answer_candidates / answer_readiness), so Review and authorization
can never disagree about which answer applies or whether it is stale."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from product.autonomy_contract import REACH_ORDER, EmployerKeyStrength, Reach, RepresentationRequirement
from product.autonomy_gate import answer_readiness, context_state, usable_answer_candidates
from product.fill_manifest import value_hash
from product.representation_transforms import TRANSFORM_IDS
from product.review_contract import (
    FIELD_DELTA_KINDS, PlannedField, ProvenanceLabel, ReviewWarning, WarningLevel, effective_delta_key, warning_key,
)
from product.semantic_subject_policy import load_subject_policy, subject_entry
from webapp.persistence import review_approval as ra
from webapp.persistence.autonomy_answers import current_approved_answers
from webapp.services.autonomy_context import answer_candidates, current_governing_blockers

CONTACT_FIELDS = ("full_name", "email", "phone", "location")
EXPIRING_WITHIN = timedelta(days=7)
_TRANSFORMS = tuple(sorted(TRANSFORM_IDS))


def _requirements(conn, ws: str) -> dict[str, dict[str, Any]]:
    """Governing blockers and field deltas, merged by key (required if any source is)."""
    reqs: dict[str, dict[str, Any]] = {}

    def add(key: str, subject: str | None, required: bool, question: str,
            job_context: Mapping[str, Any]) -> dict[str, Any]:
        r = reqs.setdefault(key, {"subject": subject, "required": False, "questions": [], "blocker_ids": [],
                                  "unclassified": subject is None, "job_context": {}, "declaration": False})
        r["required"] = r["required"] or required
        if question not in r["questions"]:
            r["questions"].append(question)
        r["job_context"].update({k: v for k, v in (job_context or {}).items() if v is not None})
        return r

    # Only blockers of the current governing artifacts govern (the 6B rule);
    # older rows remain audit history.
    for b in sorted(current_governing_blockers(conn, ws), key=lambda b: b["id"]):
        if b.get("semantic_subject_key") and b["status"] in ("open", "resolved"):
            add(f"subject:{b['semantic_subject_key']}", b["semantic_subject_key"], True, b["question"],
                (b.get("context") or {}).get("job_context") or {})["blocker_ids"].append(b["id"])
    classified_away = {e["detail"].get("delta_id") for e in ra.events(conn, ws)
                       if e["event"] == "DELTA_RESOLVED" and e["detail"].get("reason") == "classified"}
    for d in ra.list_deltas(conn, ws):
        if d["kind"] not in FIELD_DELTA_KINDS or d["id"] in classified_away:
            continue
        required = bool(d["required"]) or d["kind"] == "OMIT_FIELD_REQUIRED"
        r = add(effective_delta_key(d), d["subject"], required, d["question"], d["observed"].get("job_context") or {})
        r["declaration"] = r["declaration"] or d["kind"] == "DECLARATION"
    return reqs


def requires_application_answer(conn, ws: str, answer_key: str) -> bool:
    """Spec R4: a requirement that includes a DECLARATION delta needs an
    explicit answer for this application; no standing answer satisfies it."""
    return bool(_requirements(conn, ws).get(answer_key, {}).get("declaration"))


def _this_application_only(candidates, ws: str):
    return tuple(c for c in candidates if c.reach is Reach.APPLICATION and c.scope_id == ws)


def pending_proposals(conn, ws: str) -> list[dict[str, Any]]:
    """System proposals for the current governing requirements that no
    PROPOSAL_ACCEPTED event names: the only ones a user may accept."""
    accepted = {e["detail"].get("proposal_id") for e in ra.events(conn, ws) if e["event"] == "PROPOSAL_ACCEPTED"}
    blocker_ids = [b for r in _requirements(conn, ws).values() for b in r["blocker_ids"]]
    if not blocker_ids:
        return []
    rows = conn.execute(f"SELECT * FROM proposed_answers WHERE blocker_id IN ({','.join('?' for _ in blocker_ids)}) "
                        "ORDER BY seq", tuple(blocker_ids)).fetchall()
    return [dict(r) for r in rows if r["id"] not in accepted]


def _optional_disposition(stored: str | None, has_value: bool) -> str | None:
    if stored == "OMIT":
        return "OMIT"
    if stored == "ANSWER" and has_value:
        return "ANSWER"
    return None


def _contact_fields(profile_payload: Mapping[str, Any], reqs: dict[str, dict[str, Any]], dispositions: dict[str, str],
                    warnings: list[ReviewWarning]) -> list[PlannedField]:
    claims = profile_payload.get("claims") or []
    conflicted = {c.get("concept_id") for c in profile_payload.get("conflicts") or []}
    out = []
    for name in CONTACT_FIELDS:
        found = sorted((c for c in claims if c.get("field") == name and not c.get("placeholder")
                        and c.get("concept_id") not in conflicted), key=lambda c: c["id"])
        if not found:
            continue  # any requirement for this subject stays a (blocking) field of its own
        requirement = reqs.pop(f"subject:{name}", None)  # a requirement naming the contact subject makes it required
        key = f"contact:{name}"
        required = bool(requirement and requirement["required"])
        question = " / ".join(requirement["questions"]) if requirement else name.replace("_", " ").capitalize()
        if len({repr(c.get("value")) for c in found}) > 1:
            # Several different values: blocking, but the known field stays planned with no source.
            warnings.append(ReviewWarning(warning_key("ambiguous_answer", key, {"claim_ids": [c["id"] for c in found]}),
                                          WarningLevel.BLOCKING, f"{name}: several different profile values"))
            out.append(PlannedField(key, name, required, question,
                                    None if required else _optional_disposition(dispositions.get(key), False),
                                    None, None, None, _TRANSFORMS, None, None, reach="EVIDENCE", freshness=None))
            continue
        claim = found[0]
        disposition = "ANSWER" if required else _optional_disposition(dispositions.get(key), True)
        answering = disposition == "ANSWER"
        out.append(PlannedField(
            answer_key=key, subject=name, required=required,
            question=question, disposition=disposition, source_kind="EVIDENCE" if answering else None,
            source_ref=claim["id"] if answering else None,
            value_hash=value_hash(claim.get("value")) if answering else None,
            permitted_transforms=_TRANSFORMS, display_value=str(claim.get("value")),
            provenance_label=ProvenanceLabel.PROFILE_EVIDENCE, reach="EVIDENCE", freshness="no expiry"))
    return out


def _label(row: Mapping[str, Any]) -> ProvenanceLabel:
    if row["basis"].get("kind") == "EVIDENCE":
        return ProvenanceLabel.PROFILE_EVIDENCE
    return ProvenanceLabel.USER_SUPPLIED


def planned_fields(conn, *, account_id: str, application_workspace_id: str, profile_payload: Mapping[str, Any],
                   employer_key: str | None, employer_key_strength: EmployerKeyStrength,
                   search_workspace_id: str | None,
                   now: datetime) -> tuple[tuple[PlannedField, ...], tuple[ReviewWarning, ...]]:
    ws = application_workspace_id
    policy = load_subject_policy()
    reqs = _requirements(conn, ws)
    dispositions = ra.current_dispositions(conn, ws)
    pending = pending_proposals(conn, ws)
    warnings: list[ReviewWarning] = []
    out = _contact_fields(profile_payload, reqs, dispositions, warnings)

    for key, r in sorted(reqs.items()):
        subject, required, question = r["subject"], r["required"], " / ".join(r["questions"])
        entry = subject_entry(policy, subject) if subject else None
        if entry is None:  # unclassified (or unknown to the registry): it can't be answered
            if required:
                warnings.append(ReviewWarning(warning_key("unclassified_required_question", key, {"key": key}),
                                              WarningLevel.BLOCKING, f"{question}: not yet classified"))
            out.append(PlannedField(key, subject, required, question,
                                    None if required else ("OMIT" if dispositions.get(key) == "OMIT" else None),
                                    None, None, None, _TRANSFORMS, None, None))
            continue
        for p in pending:
            if p["blocker_id"] in r["blocker_ids"]:
                warnings.append(ReviewWarning(warning_key("proposal_unaccepted", key, {"proposal_id": p["id"]}),
                                              WarningLevel.BLOCKING, f"{question}: a proposed answer awaits you"))
        cands = answer_candidates(conn, account_id=account_id, workspace_id=ws, subject=subject,
                                  profile_payload=profile_payload)
        if r["declaration"]:  # R4: standing answers never satisfy a declaration
            cands = _this_application_only(cands, ws)
        req = RepresentationRequirement(key=key, subject=subject, required=required, evidence_available=False,
                                        job_context=dict(r["job_context"]), candidates=cands)
        usable, contradicted = usable_answer_candidates(
            req, entry, application_workspace_id=ws, search_workspace_id=search_workspace_id,
            employer_key=employer_key, employer_key_strength=employer_key_strength)
        if entry["context_keys"]:
            # Fail closed: a standing answer binds only on a proven context match
            # (an APPLICATION-reach answer was given for this application itself).
            unproven = [c for c in usable if c.reach is not Reach.APPLICATION
                        and context_state(c, req, entry) != "match"]
            if unproven:
                usable = [c for c in usable if c not in unproven]
                if not usable:
                    missing = sorted(k for k in entry["context_keys"] if r["job_context"].get(k) is None)
                    warnings.append(ReviewWarning(
                        warning_key("answer_context_unknown", key, {"missing_job_context": missing,
                                                                    "candidates": sorted(c.approved_answer_id
                                                                                         for c in unproven)}),
                        WarningLevel.BLOCKING, f"{question}: the job's {', '.join(missing) or 'context'} is unknown"))
        if contradicted:
            warnings.append(ReviewWarning(warning_key("contradicted_answer", key,
                                                      {"candidates": sorted(c.approved_answer_id for c in cands)}),
                                          WarningLevel.BLOCKING, f"{question}: your answers contradict each other"))
        chosen = None
        if usable:
            narrowest = min(REACH_ORDER[c.reach] for c in usable)
            at = sorted({c.approved_answer_id: c for c in usable if REACH_ORDER[c.reach] == narrowest}.values(),
                        key=lambda c: c.approved_answer_id)
            if len(at) > 1:
                warnings.append(ReviewWarning(
                    warning_key("ambiguous_answer", key, {"candidates": [c.approved_answer_id for c in at]}),
                    WarningLevel.BLOCKING, f"{question}: several answers apply equally"))
            else:
                chosen = at[0]
        row = None
        freshness = None
        if chosen is not None:
            row = next(a for a in current_approved_answers(conn, account_id=account_id, subject=subject)
                       if a["id"] == chosen.approved_answer_id)
            readiness = answer_readiness(chosen, entry, now)
            vh = value_hash(row["value"])
            expires = readiness.expires_at.isoformat() if readiness.expires_at else None
            if readiness.expired:
                freshness = "expired"
                warnings.append(ReviewWarning(warning_key("answer_expired", key, {
                    "source_ref": chosen.approved_answer_id, "value_hash": vh, "expires_at": expires}),
                    WarningLevel.BLOCKING, f"{question}: your answer has expired"))
            else:
                freshness = f"valid until {readiness.expires_at.date().isoformat()}" if readiness.expires_at \
                    else "no expiry"
                if readiness.expires_at and readiness.expires_at - now < EXPIRING_WITHIN:
                    warnings.append(ReviewWarning(warning_key("answer_expiring", key, {
                        "source_ref": chosen.approved_answer_id, "value_hash": vh, "expires_at": expires}),
                        WarningLevel.ATTENTION, f"{question}: your answer expires soon"))
            if chosen.basis_kind == "EVIDENCE" and readiness.basis != "ok":
                warnings.append(ReviewWarning(warning_key("answer_basis_stale", key, {
                    "source_ref": chosen.approved_answer_id, "basis_hash_at_approval": chosen.basis_hash_at_approval,
                    "basis_hash_current": chosen.basis_hash_current}),
                    WarningLevel.BLOCKING, f"{question}: the profile evidence behind this answer changed"))
            if entry["sensitive"] is not None and not (chosen.reach is Reach.APPLICATION and chosen.scope_id == ws):
                warnings.append(ReviewWarning(
                    warning_key("sensitive_needs_this_application", key, {"source_ref": chosen.approved_answer_id}),
                    WarningLevel.BLOCKING, f"{question}: answer this for this application"))
        has_value = row is not None
        disposition = ("ANSWER" if has_value else None) if required \
            else _optional_disposition(dispositions.get(key), has_value)
        answering = disposition == "ANSWER"
        out.append(PlannedField(
            answer_key=key, subject=subject, required=required, question=question, disposition=disposition,
            source_kind="APPROVED_ANSWER" if answering else None,
            source_ref=row["id"] if answering else None,
            value_hash=value_hash(row["value"]) if answering else None,
            permitted_transforms=_TRANSFORMS, display_value=str(row["value"]) if has_value else None,
            provenance_label=_label(row) if has_value else None,
            reach=chosen.reach.value if chosen is not None else None, freshness=freshness))
    return tuple(sorted(out, key=lambda f: f.answer_key)), tuple(sorted(warnings, key=lambda w: w.key))
