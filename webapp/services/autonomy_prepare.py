"""Preparation of enrolled applications (6C spec §5, §8): the snapshot the
pure step derivation reads, the paid steps, mechanical system review, system
Gate 4 with its in-transaction rechecks, the human-review latch and
enrolment. The scheduler owns leases, reservations and attempt rows."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from product.autonomy_contract import Capability, Mode
from product.autonomy_gate import evaluate_authorization
from product.prepare_steps import (
    ErrorClass, PrepareSnapshot, StepKind, grounded_claim_ids, item_content_hash, mechanical_review, pack_revision,
)
from webapp.config import Settings
from webapp.persistence import autonomy_prepare as ap
from webapp.persistence.artifacts import get_artifact, get_current_artifact
from webapp.persistence.autonomy_authority import is_paused, kill_switch_state
from webapp.persistence.review import SYSTEM_AUTO_CONFIRMED, list_review_decisions, save_review_decision
from webapp.persistence.workspaces import get_profile_workspace_id, get_workspace
from webapp.services.application_pack import (
    SYSTEM_GATE4_NOTE, list_outstanding_review_items, system_confirm_application_pack,
)
from webapp.services.autonomy_context import build_context
from webapp.services.autonomy_controls import run_immediate, sentinel_present
from webapp.services.autonomy_fence import LeaseLost
from webapp.services.autonomy_providers import ProviderSet
from webapp.services.pipeline import PipelineError
from webapp.services.staleness import check_staleness

class StepStateChanged(PipelineError):
    """A local step (system review / Gate 4) refused because the state it was
    derived from changed: the scheduler re-derives, it never retries."""


class StepIntegrityError(PipelineError):
    """A local step found an invariant/contract violation: INTERNAL, never
    retried automatically."""


_STATE_PROBLEMS = frozenset({"prepare_authority", "controls", "revision_changed", "human_review_latched"})

_UNDERSTANDING, _FIT, _INTELLIGENCE, _PACK = (
    "job_understanding_result", "job_fit_result", "application_intelligence_result", "application_pack")


# ---- enrolment ----------------------------------------------------------------

def _require_owned(conn, account_id: str, application_workspace_id: str) -> dict[str, Any]:
    workspace = get_workspace(conn, application_workspace_id, account_id=account_id)
    if workspace is None or workspace.get("kind") != "job":
        raise LookupError(application_workspace_id)
    return workspace


def enrol(conn, *, account_id: str, application_workspace_id: str, actor: str, now: datetime) -> None:
    def work():
        _require_owned(conn, account_id, application_workspace_id)
        ap.record_enrolment(conn, account_id=account_id, application_workspace_id=application_workspace_id,
                            action="ENROL", actor_type="USER", actor=actor, reason="prepare autonomously", now=now)
        ap.enqueue_application(conn, application_workspace_id=application_workspace_id, account_id=account_id,
                               now=now)
    run_immediate(conn, work)


def unenrol(conn, *, account_id: str, application_workspace_id: str, actor: str, now: datetime) -> None:
    def work():
        _require_owned(conn, account_id, application_workspace_id)
        ap.record_enrolment(conn, account_id=account_id, application_workspace_id=application_workspace_id,
                            action="UNENROL", actor_type="USER", actor=actor, reason="stop preparing", now=now)
        ap.set_dormant(conn, queue="APPLICATION", item_id=application_workspace_id, now=now)
    run_immediate(conn, work)


# ---- snapshot -----------------------------------------------------------------

def _current(conn, ws: str, artifact_type: str, settings: Settings, account_id: str) -> dict[str, Any] | None:
    artifact = get_current_artifact(conn, ws, artifact_type)
    if artifact is None:
        return None
    stale = check_staleness(conn, ws, artifact_type, extensions_dir=settings.extensions_dir,
                            account_id=account_id)["stale"]
    return None if stale else artifact


def _profile(conn, account_id: str) -> dict[str, Any] | None:
    profile_ws = get_profile_workspace_id(conn, account_id)
    return get_current_artifact(conn, profile_ws, "profile_snapshot") if profile_ws else None


def pack_sources(pack_payload: dict[str, Any]) -> dict[str, Any]:
    """The source artifacts a pack binds, for either document path: v1 packs
    carry them directly, v2 packs inside their reviewed generation basis."""
    if pack_payload.get("schema_version") == "application-pack.v2":
        basis = (pack_payload.get("generation_basis") or {}).get("reviewed_application_pack") or {}
        return basis.get("source_artifacts") or {}
    return pack_payload.get("source_artifacts") or {}


def _revision_of_pack(pack_payload: dict[str, Any]) -> str | None:
    sources = pack_sources(pack_payload)
    try:
        return pack_revision(sources["profile_snapshot"]["content_id"], sources["job_fit_result"]["content_id"],
                             sources["application_intelligence_result"]["content_id"])
    except (KeyError, TypeError):
        return None


def prepare_snapshot(conn, *, settings: Settings, account_id: str,
                     application_workspace_id: str) -> tuple[PrepareSnapshot, dict[str, Any]]:
    ws = application_workspace_id
    workspace = get_workspace(conn, ws, account_id=account_id) or {}
    profile = _profile(conn, account_id)
    understanding = _current(conn, ws, _UNDERSTANDING, settings, account_id)
    fit = _current(conn, ws, _FIT, settings, account_id)
    intelligence = _current(conn, ws, _INTELLIGENCE, settings, account_id)
    revision = (pack_revision(profile["content_id"], fit["content_id"], intelligence["content_id"])
                if profile and fit and intelligence else None)
    latched = revision is not None and ap.has_latch(conn, ws, revision)
    mechanical: list[dict[str, Any]] = []
    judgment: list[dict[str, Any]] = []
    pack_current = False
    if profile and understanding and fit and intelligence:
        pack = _current(conn, ws, _PACK, settings, account_id)
        pack_current = (pack is not None and workspace.get("workflow_status") == "drafted"
                        and _revision_of_pack(pack["payload"]) == revision)
        if not pack_current:
            try:
                items = list_outstanding_review_items(conn, ws, extensions_dir=settings.extensions_dir,
                                                      account_id=account_id)
            except PipelineError:
                items = []
            for item in items:
                verdict = None if latched else mechanical_review(item, profile["payload"])
                (mechanical if verdict else judgment).append(item)
    snapshot = PrepareSnapshot(
        workflow_status=workspace.get("workflow_status"), profile_available=profile is not None,
        understanding_current=understanding is not None, fit_current=fit is not None,
        intelligence_current=intelligence is not None, pack_current=pack_current,
        mechanically_acceptable=len(mechanical), judgment_outstanding=len(judgment), latched=latched,
        document_selection_required=bool(settings.cv_quality_v2_enabled),
    )
    detail = {"profile": profile, "pack_revision": revision, "mechanical": mechanical, "judgment": judgment,
              "content_ids": {"understanding": (understanding or {}).get("content_id"),
                              "fit": (fit or {}).get("content_id"),
                              "intelligence": (intelligence or {}).get("content_id")}}
    return snapshot, detail


# ---- paid steps ---------------------------------------------------------------

def _current_fit_extension_ids(conn, ws: str) -> list[str]:
    request = get_current_artifact(conn, ws, "job_fit_request")
    extensions = (request or {}).get("payload", {}).get("active_extensions") or []
    return [e["id"] for e in extensions if isinstance(e, dict) and e.get("id")]


def run_paid_step(conn, *, settings: Settings, providers: ProviderSet, step: StepKind, account_id: str,
                  application_workspace_id: str, request_id: str) -> list[dict[str, Any]]:
    """The model call for one preparation step, through the existing services
    (whichever document path is current). Autonomy never chooses extensions:
    FIT keeps the ones the current fit request used, else none."""
    from webapp.services import http_api
    ws = application_workspace_id
    if step is StepKind.UNDERSTAND:
        artifact = http_api.understand_job(conn, ws, providers.understanding, request_id=request_id,
                                           account_id=account_id)
    elif step is StepKind.FIT:
        artifact = http_api.fit_job(conn, ws, providers.semantic_adapter, request_id=request_id,
                                    extension_ids=_current_fit_extension_ids(conn, ws),
                                    extensions_dir=settings.extensions_dir, account_id=account_id)
    elif step is StepKind.INTELLIGENCE:
        artifact = http_api.generate_application_intelligence(conn, ws, providers.intelligence,
                                                              request_id=request_id, account_id=account_id)
    else:
        raise ValueError(f"{step} is not a paid preparation step")
    return [{"artifact_id": artifact["id"], "artifact_type": artifact["artifact_type"],
             "content_id": artifact.get("content_id")}]


# ---- system review ------------------------------------------------------------

def _has_decision(conn, ws: str, item: dict[str, Any]) -> bool:
    return any(d["review_item_type"] == item["item_type"] and d["domain_item_id"] == item["item_id"]
               for d in list_review_decisions(conn, ws, item["source_artifact_id"]))


def run_system_review(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                      now: datetime, fence: Callable[[], bool] | None = None) -> int:
    """One transaction; items are re-read inside it, so a user decision that
    landed after the snapshot always wins (never overridden or duplicated)."""
    ws = application_workspace_id

    def work() -> int:
        if fence is not None and not fence():
            raise LeaseLost("system review lost its lease")
        snapshot, detail = prepare_snapshot(conn, settings=settings, account_id=account_id,
                                            application_workspace_id=ws)
        if snapshot.latched or detail["profile"] is None or detail["pack_revision"] is None:
            return 0
        written = 0
        for item in detail["mechanical"]:
            if _has_decision(conn, ws, item):
                continue
            verdict = mechanical_review(item, detail["profile"]["payload"])
            if verdict is None:
                continue
            save_review_decision(
                conn, workspace_id=ws, review_item_type=item["item_type"],
                source_artifact_id=item["source_artifact_id"], domain_item_id=item["item_id"],
                disposition="acknowledged_and_proceed", commit=False, decision_provenance=SYSTEM_AUTO_CONFIRMED,
                system_basis={"reason": verdict.reason, "item_content_hash": verdict.item_content_hash,
                              "pack_revision": detail["pack_revision"]})
            written += 1
        return written
    return run_immediate(conn, work)


# ---- system Gate 4 --------------------------------------------------------------

def _units(pack: dict[str, Any]) -> list[dict[str, Any]]:
    return list(pack.get("cv_content", [])) + list(pack.get("cover_letter_content", []))


def system_gate4(conn, *, settings: Settings, account_id: str, application_workspace_id: str, authorization,
                 expected_revision: str, now: datetime, fence: Callable[[], bool] | None = None) -> dict[str, Any]:
    """spec §8.3: every recheck runs inside the confirmation's BEGIN
    IMMEDIATE transaction; any failure raises and nothing is written."""
    ws = application_workspace_id
    if settings.cv_quality_v2_enabled:  # never confirm the legacy path while v2 is the enabled one
        raise StepIntegrityError("document_selection_required: the enabled CV v2 path needs the user's files")

    def precheck(pack: dict[str, Any], profile_artifact: dict[str, Any]) -> None:
        if fence is not None and not fence():
            raise LeaseLost("system Gate 4 lost its lease")
        problems: list[str] = []
        ctx = build_context(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                            requested_stage=Capability.PREPARE, mode=Mode.LIVE, now=now,
                            sentinel_present=sentinel_present(settings.autonomy_sentinel_path))
        decision = evaluate_authorization(ctx)
        if not (authorization.permitted and decision.grantable
                and decision.effective_capability >= Capability.PREPARE):
            problems.append("prepare_authority")
        if (is_paused(conn, account_id=account_id, scope_type="APPLICATION", scope_id=ws)
                or kill_switch_state(conn, account_id)["halted"] or sentinel_present(settings.autonomy_sentinel_path)
                or not ap.is_enrolled(conn, ws) or not settings.autonomy_scheduler_enabled):
            problems.append("controls")
        revision = _revision_of_pack(pack)
        if revision != expected_revision:
            problems.append("revision_changed")
        if revision is None or ap.has_latch(conn, ws, revision):
            problems.append("human_review_latched")
        intelligence_id = pack["source_artifacts"]["application_intelligence_result"]["artifact_id"]
        intelligence = get_artifact(conn, intelligence_id)
        by_unit = {u.get("unit_id"): u for u in _units(intelligence["payload"])} if intelligence else {}
        for decision_row in list_review_decisions(conn, ws, intelligence_id):
            if decision_row["decision_provenance"] != SYSTEM_AUTO_CONFIRMED:
                continue
            latest_for_item = next(d for d in list_review_decisions(conn, ws, intelligence_id)
                                   if (d["review_item_type"], d["domain_item_id"])
                                   == (decision_row["review_item_type"], decision_row["domain_item_id"]))
            if latest_for_item["id"] != decision_row["id"]:
                continue  # a newer decision governs this item
            basis = json.loads(decision_row["system_basis_json"])
            unit = by_unit.get(decision_row["domain_item_id"])
            item = {"item_type": decision_row["review_item_type"], "item_id": decision_row["domain_item_id"],
                    "source_artifact_id": intelligence_id, "source": unit}
            verdict = mechanical_review(item, profile_artifact["payload"]) if unit else None
            if (verdict is None or basis.get("pack_revision") != revision
                    or basis.get("item_content_hash") != item_content_hash(item)):
                problems.append(f"system_basis:{decision_row['domain_item_id']}")
        grounded = grounded_claim_ids(profile_artifact["payload"])
        for unit in _units(pack):
            if not set(unit.get("profile_evidence_ids") or []) <= grounded:
                problems.append(f"unsupported:{unit.get('unit_id')}")
        if pack.get("completion_status") != "READY":
            problems.append("completion")
        if problems:
            message = "system Gate 4 refused: " + ", ".join(sorted(set(problems)))
            if set(problems) <= _STATE_PROBLEMS:
                raise StepStateChanged(message)
            raise StepIntegrityError(message)

    return system_confirm_application_pack(
        conn, ws, effective_date=now.date().isoformat(), documents_root=settings.documents_root,
        extensions_dir=settings.extensions_dir, account_id=account_id, precheck=precheck)


def system_confirmed_revision(conn, application_workspace_id: str) -> str | None:
    """The revision of the current drafted pack if the system confirmed it:
    the latest drafted workflow event (by rowid, never by timestamp)."""
    row = conn.execute(
        "SELECT note, submitted_pack_artifact_id FROM workflow_events WHERE workspace_id = ? AND "
        "new_status = 'drafted' ORDER BY rowid DESC LIMIT 1", (application_workspace_id,)).fetchone()
    if row is None or row["note"] != SYSTEM_GATE4_NOTE or not row["submitted_pack_artifact_id"]:
        return None
    pack = get_artifact(conn, row["submitted_pack_artifact_id"])
    return _revision_of_pack(pack["payload"]) if pack else None


# ---- latch --------------------------------------------------------------------

def _current_revision(conn, application_workspace_id: str, account_id: str) -> str | None:
    profile = _profile(conn, account_id)
    fit = get_current_artifact(conn, application_workspace_id, _FIT)
    intelligence = get_current_artifact(conn, application_workspace_id, _INTELLIGENCE)
    if not (profile and fit and intelligence):
        return None
    return pack_revision(profile["content_id"], fit["content_id"], intelligence["content_id"])


def request_pack_review(conn, *, account_id: str, application_workspace_id: str, actor: str, now: datetime) -> str:
    def work() -> str:
        _require_owned(conn, account_id, application_workspace_id)
        revision = _current_revision(conn, application_workspace_id, account_id)
        if revision is None:
            raise ValueError("there is no current pack revision to review")
        if not ap.has_latch(conn, application_workspace_id, revision):
            ap.record_latch(conn, application_workspace_id=application_workspace_id, pack_revision=revision,
                            reason="EXPLICIT_REVIEW", actor=actor, now=now)
        ap.wake(conn, queue="APPLICATION", item_id=application_workspace_id, now=now)
        return revision
    return run_immediate(conn, work)


def on_user_review_decision(conn, *, workspace_id: str, account_id: str, now: datetime) -> None:
    """After a USER review decision: latch only if the current revision was
    already system-confirmed; otherwise just wake the application."""
    def work() -> None:
        confirmed = system_confirmed_revision(conn, workspace_id)
        current = _current_revision(conn, workspace_id, account_id)
        if confirmed is not None and confirmed == current and not ap.has_latch(conn, workspace_id, confirmed):
            ap.record_latch(conn, application_workspace_id=workspace_id, pack_revision=confirmed,
                            reason="REOPENED_CONFIRMED", actor=account_id, now=now)
        ap.wake(conn, queue="APPLICATION", item_id=workspace_id, now=now)
    run_immediate(conn, work)


# ---- error classes --------------------------------------------------------------

_TRANSIENT_NAMES = {"APITimeoutError", "RateLimitError", "APIConnectionError", "InternalServerError",
                    "ServiceUnavailableError", "Timeout", "ReadTimeout", "ConnectTimeout"}
_HUMAN_NAMES = {"AuthenticationError", "PermissionDeniedError"}
_HUMAN_PIPELINE_HINTS = ("evidence profile", "profile refresh", "set up user profile", "api key", "budget",
                         "not_installed")


def classify_error(exc: BaseException) -> tuple[ErrorClass, str]:
    """Classify by the most specific known cause: the pipeline wraps provider
    exceptions (e.g. PipelineError from a TimeoutError), so the cause chain
    is inspected before falling back to INTERNAL."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        error_class, code = _classify_one(current)
        if error_class is not ErrorClass.INTERNAL:
            return error_class, code
        current = current.__cause__ or current.__context__
    return ErrorClass.INTERNAL, type(exc).__name__


def _classify_one(exc: BaseException) -> tuple[ErrorClass, str]:
    name = type(exc).__name__
    if isinstance(exc, StepIntegrityError):
        return ErrorClass.INTERNAL, "integrity"
    if isinstance(exc, (TimeoutError, ConnectionError)) or name in _TRANSIENT_NAMES \
            or (getattr(exc, "status_code", 0) or 0) >= 500:
        return ErrorClass.TRANSIENT, name
    if name in _HUMAN_NAMES:
        return ErrorClass.HUMAN_FIXABLE, name
    if isinstance(exc, PipelineError) and any(h in str(exc).lower() for h in _HUMAN_PIPELINE_HINTS):
        return ErrorClass.HUMAN_FIXABLE, "PipelineError"
    return ErrorClass.INTERNAL, name


def retry_after_seconds(exc: BaseException) -> float | None:
    value = getattr(exc, "retry_after", None)
    if value is None:
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        value = headers.get("retry-after") if hasattr(headers, "get") else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
