"""Bundle 6D-B fill-plan services (spec §8, §9, §16.1).

- propose_plan: builds fill-plan.v1 from an observation against the EFFECTIVE
  6D-A approval; opens new content as 6D-A deltas (one transaction, no
  duplicates), stores classification proposals, and stores a complete plan
  (which still needs one explicit confirmation).
- record_mapping_choice: a user's mapping for a field that needs review;
  only compatible approved answers are accepted.
- fill_plan_presentation: ONE read snapshot for the confirmation page; the
  only place cleartext rendered values are derived (never stored).
- confirm_plan: BEGIN IMMEDIATE; recompute; confirm only the displayed hash.

Approved cleartext is read only to render hashes and the page view."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping

from product.fill_certification import certified
from product.fill_hash import fill_value_hash
from product.fill_plan import PlanResult, _compatible, _render, build_plan
from product.review_contract import FIELD_DELTA_KINDS, effective_delta_key
from product.semantic_subject_registry import SEMANTIC_SUBJECTS
from webapp.config import Settings
from webapp.persistence import fill as f
from webapp.persistence import review_approval as ra
from webapp.persistence.application_documents import get_document_version
from webapp.services.autonomy_controls import run_immediate
from webapp.services.review_application import ReviewRefused, _profile_payload, review_state
from webapp.services.review_approval import open_review_delta_in_transaction


@dataclass
class ProposeOutcome:
    state: str  # PLAN_PROPOSED | PLAN_NEEDS_REVIEW | DELTAS_OPENED | UNSUPPORTED_FORM | STOPPED | APPROVAL_NOT_EFFECTIVE
    plan_hash: str | None = None
    confirmed: bool = False
    details: dict[str, Any] = field(default_factory=dict)


# ---- approval context ------------------------------------------------------------------

def approval_context(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                     now: datetime) -> dict[str, Any] | None:
    """The effective 6D-A approval (binding and hash), or None."""
    state = review_state(conn, settings=settings, account_id=account_id,
                         application_workspace_id=application_workspace_id, now=now)
    if not state.approval_effective or state.binding_hash is None:
        return None
    latest = ra.latest_approval(conn, application_workspace_id)
    return {"approval_id": latest["id"], "binding": state.binding, "binding_hash": state.binding_hash}


def approved_values(conn, account_id: str, binding: Mapping[str, Any]) -> dict[str, Any]:
    """answer_key -> approved cleartext, for rendering only (never stored)."""
    claims = {c["id"]: c for c in (_profile_payload(conn, account_id).get("claims") or [])}
    out: dict[str, Any] = {}
    for fld in binding["fields"]:
        if fld["disposition"] != "ANSWER":
            continue
        if fld["source_kind"] == "APPROVED_ANSWER":
            row = conn.execute("SELECT value_json FROM approved_answers WHERE id = ? AND account_id = ?",
                               (fld["source_ref"], account_id)).fetchone()
            if row is not None:
                out[fld["answer_key"]] = json.loads(row["value_json"])
        elif fld["source_kind"] == "EVIDENCE" and fld["source_ref"] in claims:
            out[fld["answer_key"]] = claims[fld["source_ref"]].get("value")
    return out


def approved_documents(conn, account_id: str, binding: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for doc in binding["documents"]:
        row = get_document_version(conn, doc["document_version_id"], account_id=account_id)
        if row is not None:
            out[doc["kind"]] = {"document_version_id": row["id"], "sha256": row["sha256"],
                                "byte_length": row["byte_length"], "filename": row["original_filename"],
                                "media_type": row["media_type"]}
    return out


def delta_fields(conn, application_workspace_id: str) -> dict[str, dict[str, Any]]:
    """page_field_key -> the answer key of the latest field delta 6D-B opened
    for that exact observed field (a classification successor supersedes)."""
    out: dict[str, dict[str, Any]] = {}
    for delta in ra.list_deltas(conn, application_workspace_id):
        observed = delta["observed"]
        if delta["kind"] in FIELD_DELTA_KINDS and str(delta["source"]).startswith("FILL_") \
                and observed.get("field_key") and observed.get("field_fingerprint"):
            out[observed["field_key"]] = {"answer_key": effective_delta_key(delta), "delta_id": delta["id"],
                                          "field_fingerprint": observed["field_fingerprint"]}
    return out


# ---- heuristic classification proposals (never authoritative, spec §9) --------------------

_STOP = frozenset({"your", "have", "with", "this", "that", "what", "when", "will", "would", "from", "they", "their",
                   "there", "which", "about", "does", "must", "give", "long", "much", "please", "candidate",
                   "candidates", "whether", "given"})


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 4 and w not in _STOP}


def heuristic_subject(question: str) -> str | None:
    """The single best registered subject by word overlap, or None (a tie or
    no overlap proposes nothing)."""
    words = _tokens(question)
    scores = {subject: len(words & (_tokens(subject.replace(".", " ").replace("_", " ")) | _tokens(desc)))
              for subject, desc in SEMANTIC_SUBJECTS.items()}
    best = max(scores.values(), default=0)
    winners = [s for s, score in scores.items() if score == best]
    return winners[0] if best > 0 and len(winners) == 1 else None


# ---- building ----------------------------------------------------------------------------

def _build(conn, *, settings: Settings, account_id: str, application_workspace_id: str, observation_id: str,
           now: datetime) -> tuple[dict[str, Any] | None, dict[str, Any] | None, PlanResult | None]:
    ws = application_workspace_id
    obs = f.get_observation(conn, observation_id)
    if obs is None or obs["application_workspace_id"] != ws or obs["account_id"] != account_id:
        raise LookupError(observation_id)
    ctx = approval_context(conn, settings=settings, account_id=account_id, application_workspace_id=ws, now=now)
    if ctx is None:
        return None, obs, None
    doc = obs["observation"]
    result = build_plan(observation=doc, binding=ctx["binding"], approval_id=ctx["approval_id"],
                        binding_hash=ctx["binding_hash"],
                        catalogue_entry=certified(doc["context"]["adapter_id"], doc["context"]["adapter_version"]),
                        mapping_choices=f.current_mapping_choices(conn, observation_id),
                        values=approved_values(conn, account_id, ctx["binding"]),
                        documents=approved_documents(conn, account_id, ctx["binding"]),
                        delta_fields=delta_fields(conn, ws))
    return ctx, obs, result


def _already_open(open_deltas: list[dict[str, Any]], spec) -> bool:
    for d in open_deltas:
        if d["kind"] != spec.kind:
            continue
        if spec.kind == "TARGET_CHANGE":
            if d["observed"].get("canonical_url") == spec.observed.get("canonical_url"):
                return True
        elif d["observed"].get("field_key") == spec.observed.get("field_key") and \
                d["observed"].get("field_fingerprint") == spec.observed.get("field_fingerprint"):
            return True
    return False


def _open_deltas(conn, *, account_id: str, ws: str, specs, source: str, now: datetime) -> list[str]:
    opened: list[str] = []
    current = ra.open_deltas(conn, ws)
    for spec in specs:
        if _already_open(current, spec):
            continue
        delta = open_review_delta_in_transaction(
            conn, account_id=account_id, application_workspace_id=ws, kind=spec.kind,
            answer_key=spec.answer_key, subject=spec.subject, required=spec.required, question=spec.question,
            observed=dict(spec.observed), source=source, now=now)
        opened.append(delta["id"])
        current.append(delta)
        if spec.kind in FIELD_DELTA_KINDS and spec.subject is None and spec.answer_key is None:
            proposal = spec.proposal_subject or heuristic_subject(spec.question)
            if proposal is not None:
                f.insert_classification_proposal(conn, account_id=account_id, application_workspace_id=ws,
                                                 delta_id=delta["id"], subject=proposal, basis="HEURISTIC", now=now)
    return opened


def propose_plan(conn, *, settings: Settings, account_id: str, application_workspace_id: str, observation_id: str,
                 now: datetime) -> ProposeOutcome:
    return run_immediate(conn, lambda: propose_plan_in_transaction(
        conn, settings=settings, account_id=account_id, application_workspace_id=application_workspace_id,
        observation_id=observation_id, now=now))


def propose_plan_in_transaction(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                                observation_id: str, now: datetime) -> ProposeOutcome:
    ws = application_workspace_id
    ctx, obs, result = _build(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                              observation_id=observation_id, now=now)
    if ctx is None:
        return ProposeOutcome("APPROVAL_NOT_EFFECTIVE")
    if result.unsupported:
        return ProposeOutcome("UNSUPPORTED_FORM", details={"causes": list(result.unsupported)})
    if result.deltas:
        source = f"FILL_RUN:{obs['fill_run_id']}" if obs["fill_run_id"] else f"FILL_OBSERVATION:{obs['id']}"
        opened = _open_deltas(conn, account_id=account_id, ws=ws, specs=result.deltas, source=source, now=now)
        return ProposeOutcome("DELTAS_OPENED", details={"delta_ids": opened,
                                                        "delta_kinds": sorted({d.kind for d in result.deltas})})
    if result.stop:
        reason, detail = result.stop
        return ProposeOutcome("STOPPED", details={"reason": reason, **detail})
    if result.needs_review:
        return ProposeOutcome("PLAN_NEEDS_REVIEW", details={"needs_review": [asdict(r) for r in result.needs_review]})
    plan = result.plan
    f.insert_plan(conn, account_id=account_id, application_workspace_id=ws, plan=plan, plan_hash=plan["plan_hash"],
                  approval_id=ctx["approval_id"], approval_binding_hash=ctx["binding_hash"],
                  observation_id=observation_id, now=now)
    return ProposeOutcome("PLAN_PROPOSED", plan["plan_hash"],
                          f.plan_confirmed(conn, plan["plan_hash"], ctx["approval_id"], ctx["binding_hash"]))


def record_mapping_choice(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                          observation_id: str, page_field_key: str, answer_key: str | None, choice: str, actor: str,
                          now: datetime) -> dict[str, Any]:
    ws = application_workspace_id
    if choice not in ("MAP", "NEW_QUESTION"):
        raise ReviewRefused("invalid_choice")

    def work() -> dict[str, Any]:
        obs = f.get_observation(conn, observation_id)
        if obs is None or obs["application_workspace_id"] != ws or obs["account_id"] != account_id:
            raise LookupError(observation_id)
        element = next((e for e in obs["observation"]["elements"]
                        if e["page_field_key"] == page_field_key and e["classification"] == "APPLICATION"), None)
        if element is None:
            raise ReviewRefused("unknown_field")
        ctx = approval_context(conn, settings=settings, account_id=account_id, application_workspace_id=ws, now=now)
        if ctx is None:
            raise ReviewRefused("approval_not_effective")
        if choice == "MAP":
            fields = {x["answer_key"]: x for x in ctx["binding"]["fields"]}
            compatible = _compatible(element, fields, approved_values(conn, account_id, ctx["binding"]),
                                     approved_documents(conn, account_id, ctx["binding"]))
            if answer_key not in compatible:
                raise ReviewRefused("incompatible_mapping")
        return f.insert_mapping_choice(conn, account_id=account_id, application_workspace_id=ws,
                                       observation_id=observation_id, page_field_key=page_field_key,
                                       field_fingerprint=element["field_fingerprint"],
                                       answer_key=answer_key if choice == "MAP" else None, choice=choice, actor=actor,
                                       now=now)
    return run_immediate(conn, work)


def fill_plan_presentation(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                           observation_id: str, now: datetime) -> dict[str, Any]:
    """ONE read snapshot (never writes). WRITE rows carry the cleartext
    rendered value for the user to confirm; it must hash to the plan's
    rendered_value_hash."""
    owns = not conn.in_transaction
    if owns:
        conn.execute("BEGIN")
    try:
        ctx, obs, result = _build(conn, settings=settings, account_id=account_id,
                                  application_workspace_id=application_workspace_id, observation_id=observation_id,
                                  now=now)
        if ctx is None:
            return {"state": "APPROVAL_NOT_EFFECTIVE", "rows": [], "displayed_plan_hash": None, "confirmed": False}
        view: dict[str, Any] = {
            "state": "PLAN_PROPOSED" if result.plan else (
                "UNSUPPORTED_FORM" if result.unsupported else "DELTAS_PENDING" if result.deltas else
                "STOPPED" if result.stop else "PLAN_NEEDS_REVIEW"),
            "unsupported": list(result.unsupported), "needs_review": [asdict(r) for r in result.needs_review],
            "pending_delta_kinds": sorted({d.kind for d in result.deltas}), "stop": result.stop,
            "rows": [], "displayed_plan_hash": None, "confirmed": False,
        }
        if result.plan is None:
            return view
        plan = result.plan
        elements = {e["page_field_key"]: e for e in obs["observation"]["elements"]}
        fields = {x["answer_key"]: x for x in ctx["binding"]["fields"]}
        values = approved_values(conn, account_id, ctx["binding"])
        for action in plan["actions"]:
            element = elements[action["page_field_key"]]
            row = {"page_field_key": action["page_field_key"], "question": element["identity"]["question"] or
                   element["identity"]["label"] or element["identity"]["name"], "control_kind": element["control_kind"],
                   "action_kind": action["action_kind"], "answer_key": action["answer_key"],
                   "mapping_basis": action["mapping_basis"], "rendered_value": None,
                   "rendered_value_hash": action["rendered_value_hash"], "document": None,
                   "proof": element["proof"]}
            if action["action_kind"] == "WRITE":
                _, rendered = _render(element, values[action["answer_key"]],
                                      fields[action["answer_key"]]["permitted_transforms"])
                if fill_value_hash(rendered) != action["rendered_value_hash"]:
                    raise AssertionError("presentation value does not match the plan hash")
                row["rendered_value"] = rendered
            elif action["action_kind"] == "ATTACH_LOCAL":
                row["document"] = {"filename": action["document"]["filename"],
                                   "sha256": action["document"]["sha256"], "kind": action["document_kind"]}
            view["rows"].append(row)
        view["displayed_plan_hash"] = plan["plan_hash"]
        view["confirmed"] = f.plan_confirmed(conn, plan["plan_hash"], ctx["approval_id"], ctx["binding_hash"])
        return view
    finally:
        if owns:
            conn.rollback()


def confirm_plan(conn, *, settings: Settings, account_id: str, application_workspace_id: str, observation_id: str,
                 displayed_plan_hash: str, actor: str, now: datetime) -> dict[str, Any]:
    ws = application_workspace_id

    def work() -> dict[str, Any]:
        ctx, _, result = _build(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                                observation_id=observation_id, now=now)
        if ctx is None or result.plan is None or result.plan["plan_hash"] != displayed_plan_hash:
            raise ReviewRefused("stale_plan")
        plan = result.plan
        f.insert_plan(conn, account_id=account_id, application_workspace_id=ws, plan=plan, plan_hash=plan["plan_hash"],
                      approval_id=ctx["approval_id"], approval_binding_hash=ctx["binding_hash"],
                      observation_id=observation_id, now=now)
        if not f.plan_confirmed(conn, plan["plan_hash"], ctx["approval_id"], ctx["binding_hash"]):
            f.insert_plan_confirmation(conn, account_id=account_id, application_workspace_id=ws,
                                       plan_hash=plan["plan_hash"], approval_id=ctx["approval_id"],
                                       approval_binding_hash=ctx["binding_hash"], actor=actor, now=now)
        return {"plan_hash": plan["plan_hash"], "approval_id": ctx["approval_id"]}
    return run_immediate(conn, work)
