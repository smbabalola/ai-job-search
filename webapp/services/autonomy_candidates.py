"""Candidate enqueue, admission, evaluation, screening and revalidating
promotion (6C spec §6.1, §7). The scheduler owns reservations, attempt rows
and settlement for the paid EVALUATE call; nothing here reserves budget for
it. Floats never reach a hashed payload: scores are normalized to Decimal."""
from __future__ import annotations

import dataclasses
from datetime import datetime
from decimal import Decimal
from typing import Any

from product.autonomy_contract import UNKNOWN, Capability, IdentityStrength, normalized_employer_key
from product.candidate_promotion import (
    ELIGIBLE_STATES, ENGINE_VERSION, FINISHED_RUNS, CandidateContext, ScreeningOutcome, admit_candidate_evaluation,
    candidate_input_fingerprint, evaluate_candidate_promotion,
)
from product.job_identity import job_identity
from product.standing_policy import normalize_employment_type
from webapp.config import Settings
from webapp.persistence import autonomy_prepare as ap
from webapp.persistence.autonomy_authority import current_policy, is_paused, kill_switch_state, resolve_authority
from webapp.persistence.autonomy_ledger import (
    budget_usage, count_usage, live_intent, set_reservation_status, try_reserve,
)
from webapp.persistence.discovery import (
    get_current_discovery_fit, get_discovery_candidate, set_discovery_candidate_status,
)
from webapp.persistence.search_workspaces import get_search_workspace
from webapp.services.autonomy_context import day_window
from webapp.services.autonomy_controls import run_immediate, sentinel_present
from webapp.services.autonomy_providers import ProviderSet

__all__ = [
    "CandidatePromotionRefused", "ProviderSet", "admit_candidate_evaluation", "build_candidate_context",
    "candidate_identity", "candidate_next_action", "enqueue_run_candidates", "existing_application_for",
    "existing_intent_for", "promote_candidate", "resolve_candidate_question", "run_candidate_evaluation",
    "screen_candidate",
]


class CandidatePromotionRefused(Exception):
    pass


class _Abort(Exception):
    """Roll back a promotion savepoint without failing the caller."""


# ---- identity ---------------------------------------------------------------

def candidate_identity(record: dict[str, Any]) -> tuple[str | None, IdentityStrength]:
    ident = job_identity(record)
    if ident.source_record_key:
        return ident.source_record_key, IdentityStrength.SOURCE_RECORD
    if ident.canonical_url_key:
        return ident.canonical_url_key, IdentityStrength.CANONICAL_URL
    return None, IdentityStrength.WEAK


def _strong_keys(record: dict[str, Any]) -> list[str]:
    ident = job_identity(record)
    return [k for k in (ident.source_record_key, ident.canonical_url_key) if k]


def existing_application_for(conn, *, account_id: str, record: dict[str, Any]) -> str | None:
    keys = _strong_keys(record)
    if not keys:
        return None
    marks = ",".join("?" for _ in keys)
    row = conn.execute(
        "SELECT i.application_workspace_id FROM application_workspace_job_identities i "
        "JOIN workspaces w ON w.id = i.application_workspace_id WHERE w.account_id = ? "
        f"AND (i.source_record_key IN ({marks}) OR i.canonical_url_key IN ({marks})) ORDER BY i.rowid LIMIT 1",
        (account_id, *keys, *keys),
    ).fetchone()
    return row[0] if row else None


def existing_intent_for(conn, *, account_id: str, record: dict[str, Any]) -> bool:
    return any(live_intent(conn, account_id=account_id, job_identity_key=k) for k in _strong_keys(record))


# ---- enqueue ----------------------------------------------------------------

def _prepare_ceiling(conn, *, account_id: str, search_workspace_id: str) -> Capability:
    account_max, workspace_ceiling = resolve_authority(conn, account_id=account_id,
                                                       search_workspace_id=search_workspace_id)
    return min(account_max, workspace_ceiling)


def enqueue_run_candidates(conn, *, run_id: str, account_id: str, search_workspace_id: str, now: datetime) -> int:
    """No commit. Only new/saved candidates of a finished run, in an active
    search workspace whose ceiling is at least PREPARE."""
    run = conn.execute("SELECT status FROM discovery_runs WHERE id = ?", (run_id,)).fetchone()
    workspace = get_search_workspace(conn, search_workspace_id, account_id=account_id)
    if run is None or run["status"] not in FINISHED_RUNS or workspace is None or workspace["status"] != "active":
        return 0
    if _prepare_ceiling(conn, account_id=account_id, search_workspace_id=search_workspace_id) < Capability.PREPARE:
        return 0
    rows = conn.execute(
        "SELECT DISTINCT c.id FROM discovery_occurrences o JOIN discovery_candidates c ON c.id = o.candidate_id "
        "WHERE o.run_id = ? AND c.search_workspace_id = ? AND c.lifecycle_status IN ('new', 'saved') ORDER BY c.id",
        (run_id, search_workspace_id),
    ).fetchall()
    for row in rows:
        ap.enqueue_candidate(conn, candidate_id=row["id"], account_id=account_id,
                             search_workspace_id=search_workspace_id, now=now)
    return len(rows)


# ---- context ----------------------------------------------------------------

def _fit_state(conn, *, candidate_id: str, search_workspace_id: str, account_id: str) -> tuple[dict | None, bool]:
    fit = get_current_discovery_fit(conn, candidate_id, search_workspace_id=search_workspace_id)
    if fit is None:
        return None, False
    from webapp.services.discovery import discovery_fit_is_stale
    return fit, discovery_fit_is_stale(conn, candidate_id, search_workspace_id=search_workspace_id,
                                       account_id=account_id) is False


def _score(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return UNKNOWN
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        number = Decimal(str(value))
        return number if number.is_finite() else UNKNOWN
    return UNKNOWN


def _run_info(conn, candidate: dict[str, Any]) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT r.id, r.status FROM discovery_occurrences o JOIN discovery_runs r ON r.id = o.run_id WHERE o.id = ?",
        (candidate["canonical_occurrence_id"],)).fetchone()
    return (row["id"], row["status"]) if row else (None, None)


def build_candidate_context(conn, *, settings: Settings, account_id: str, search_workspace_id: str,
                            candidate_id: str, now: datetime) -> CandidateContext:
    candidate = get_discovery_candidate(conn, candidate_id, search_workspace_id=search_workspace_id)
    if candidate is None:
        raise LookupError(candidate_id)
    record = candidate["canonical_source_record"]
    workspace = get_search_workspace(conn, search_workspace_id, account_id=account_id)
    active = workspace is not None and workspace["status"] == "active"
    account_max, workspace_ceiling = resolve_authority(conn, account_id=account_id,
                                                       search_workspace_id=search_workspace_id)
    policy = current_policy(conn, account_id)
    doc = policy["doc"] if policy else None
    key, strength = candidate_identity(record)
    fit, fresh = (_fit_state(conn, candidate_id=candidate_id, search_workspace_id=search_workspace_id,
                             account_id=account_id) if active else (None, False))
    result = (fit or {}).get("result") or {}
    verdict = result.get("verdict")
    attributes = {
        "fit.overall_score": _score(result.get("overall_score")),
        "fit.verdict": verdict.get("id") if isinstance(verdict, dict) and verdict.get("id") else UNKNOWN,
        "job.employment_type": normalize_employment_type(record.get("employment_type")),
        "job.location": record.get("location") or UNKNOWN,
        "job.title": record.get("title") or UNKNOWN,
        "company.key": normalized_employer_key(record.get("company")) or UNKNOWN,
        "workspace.id": search_workspace_id,
        "identity.strength": strength.value,
    }
    llm = ((doc or {}).get("limits", {}).get("budgets", {}) or {}).get("LLM")
    envelope = settings.autonomy_step_envelope("EVALUATE")
    budget_ok, promos_ok, midnight = False, True, None
    if doc is not None:
        day, midnight = day_window(now, doc["timezone"])
        if llm and envelope is not None:
            used = budget_usage(conn, account_id=account_id, counter_name="budget:LLM:day", window_key=day)
            budget_ok = used + envelope <= Decimal(str(llm["per_day"]))
        promos_ok = count_usage(conn, account_id=account_id, counter_name="promotions:day",
                                window_key=day) < settings.autonomy_max_promotions_per_day
    _, run_status = _run_info(conn, candidate)
    return CandidateContext(
        now=now, account_id=account_id, search_workspace_id=search_workspace_id, candidate_id=candidate_id,
        scheduler_enabled=settings.autonomy_scheduler_enabled,
        kill_switch_engaged=kill_switch_state(conn, account_id)["halted"],
        sentinel_present=sentinel_present(settings.autonomy_sentinel_path),
        search_workspace_active=active,
        paused=is_paused(conn, account_id=account_id, scope_type="SEARCH_WORKSPACE", scope_id=search_workspace_id),
        deployment_ceiling=settings.autonomy_deployment_ceiling(), account_max=account_max,
        workspace_ceiling=workspace_ceiling, candidate_state=candidate["lifecycle_status"], run_status=run_status,
        identity_key=key, identity_strength=strength,
        existing_application=existing_application_for(conn, account_id=account_id, record=record) is not None,
        existing_intent=existing_intent_for(conn, account_id=account_id, record=record),
        fit_present=fit is not None, fit_fresh=bool(fresh), discovery_fit_id=(fit or {}).get("id"),
        standing_policy=doc, attributes=attributes, llm_budget_configured=bool(llm and llm.get("per_day")),
        evaluate_envelope_present=envelope is not None, budget_available=budget_ok,
        budget_retry_at=None if budget_ok else midnight, promotions_available=promos_ok,
        promotions_retry_at=None if promos_ok else midnight,
    )


# ---- next action, evaluation, screening --------------------------------------

def candidate_next_action(conn, ctx: CandidateContext) -> str:
    if ctx.candidate_state not in ELIGIBLE_STATES or ap.promotion_for_candidate(conn, ctx.candidate_id):
        return "DONE"
    if not ctx.fit_present or not ctx.fit_fresh:
        return "EVALUATE"
    latest = ap.latest_screening(conn, ctx.candidate_id)
    if latest is not None and latest["input_fingerprint"] == candidate_input_fingerprint(ctx):
        return "PROMOTE" if latest["outcome"] == ScreeningOutcome.PROMOTE.value else "DONE"
    return "SCREEN"


def run_candidate_evaluation(conn, *, settings: Settings, providers: ProviderSet, ctx: CandidateContext,
                             request_id: str) -> dict[str, Any]:
    """Only the paid model call; the scheduler reserves, records and settles."""
    from webapp.services import discovery
    return discovery.evaluate_discovery_candidate(
        conn, ctx.candidate_id, providers.semantic_adapter, search_workspace_id=ctx.search_workspace_id,
        request_id=request_id, understanding_provider=providers.understanding, active_extensions=[],
        account_id=ctx.account_id,
    )


def screen_candidate(conn, *, ctx: CandidateContext, now: datetime) -> tuple[dict[str, Any], datetime | None]:
    """No commit. Persists the immutable screening; opens a candidate question
    only when answering it could unlock promotion. Returns the screening and
    the queue's next eligibility (the scheduler finalizes the lease with it)."""
    result = evaluate_candidate_promotion(ctx)
    candidate = get_discovery_candidate(conn, ctx.candidate_id, search_workspace_id=ctx.search_workspace_id)
    run_id, _ = _run_info(conn, candidate) if candidate else (None, None)
    row = ap.insert_screening(
        conn, account_id=ctx.account_id, search_workspace_id=ctx.search_workspace_id,
        candidate_id=ctx.candidate_id, discovery_run_id=run_id, discovery_fit_id=ctx.discovery_fit_id,
        outcome=result.outcome.value, reason_code=result.reason_code, reasons=list(result.reasons),
        require_user=list(result.require_user), could_unlock=result.could_unlock, retry_at=result.retry_at,
        input_fingerprint=result.input_fingerprint, authority=result.authority,
        policy_version_hash=result.policy_version_hash, subject_policy_hash=None, engine_version=ENGINE_VERSION,
        now=now,
    )
    if result.outcome is ScreeningOutcome.REQUIRE_USER and result.could_unlock:
        current = ap.current_candidate_exception(conn, ctx.candidate_id)
        if current is None or current["resolution"] is not None:
            ap.open_candidate_exception(conn, account_id=ctx.account_id, search_workspace_id=ctx.search_workspace_id,
                                        candidate_id=ctx.candidate_id, screening_id=row["id"],
                                        items=list(result.require_user), now=now)
        ap.create_notification(
            conn, account_id=ctx.account_id,
            key=f"CANDIDATE_QUESTION:CANDIDATE:{ctx.candidate_id}:{result.reason_code}:{result.input_fingerprint}",
            kind="CANDIDATE_QUESTION", subject_type="CANDIDATE", subject_id=ctx.candidate_id,
            detail={"items": list(result.require_user)}, now=now)
    if result.outcome is ScreeningOutcome.PROMOTE:
        return row, now
    if result.outcome is ScreeningOutcome.DENY_TEMPORARY:
        return row, result.retry_at
    return row, None


# ---- promotion ----------------------------------------------------------------

def _promote_in_transaction(conn, *, settings: Settings, account_id: str, search_workspace_id: str,
                            candidate_id: str, screening_id: str | None, actor_type: str, actor: str,
                            now: datetime) -> dict[str, Any] | None:
    """Revalidating promotion inside the caller's transaction (spec §7.3).
    Uses a savepoint so a refused promotion leaves no partial writes."""
    if kill_switch_state(conn, account_id)["halted"] or sentinel_present(settings.autonomy_sentinel_path):
        return None
    if is_paused(conn, account_id=account_id, scope_type="SEARCH_WORKSPACE", scope_id=search_workspace_id):
        return None
    ctx = build_candidate_context(conn, settings=settings, account_id=account_id,
                                  search_workspace_id=search_workspace_id, candidate_id=candidate_id, now=now)
    if actor_type == "USER":  # a user promotion never consumes the auto-promotion cap
        ctx = dataclasses.replace(ctx, promotions_available=True, promotions_retry_at=None)
    result = evaluate_candidate_promotion(ctx)
    if actor_type == "SCHEDULER":
        screening = ap.get_screening(conn, screening_id) if screening_id else None
        if (screening is None or result.outcome is not ScreeningOutcome.PROMOTE
                or screening["input_fingerprint"] != result.input_fingerprint):
            return None
    elif result.outcome not in (ScreeningOutcome.PROMOTE, ScreeningOutcome.REQUIRE_USER):
        return None
    from webapp.services.discovery import _promote_candidate_in_transaction
    conn.execute("SAVEPOINT autonomy_promote")
    try:
        created = _promote_candidate_in_transaction(conn, candidate_id, search_workspace_id=search_workspace_id,
                                                    account_id=account_id)
        if not created["created"]:
            raise _Abort("an application already exists for this candidate")
        workspace_id = created["workspace"]["id"]
        promotion = ap.insert_promotion(conn, screening_id=screening_id, candidate_id=candidate_id,
                                        search_workspace_id=search_workspace_id,
                                        application_workspace_id=workspace_id, actor_type=actor_type, actor=actor,
                                        now=now)
        ap.record_enrolment(conn, account_id=account_id, application_workspace_id=workspace_id, action="ENROL",
                            actor_type=actor_type, actor=actor, reason="promoted", now=now)
        ap.enqueue_application(conn, application_workspace_id=workspace_id, account_id=account_id, now=now)
        if actor_type == "SCHEDULER":
            day, _ = day_window(now, ctx.standing_policy["timezone"])
            reservation = try_reserve(conn, account_id=account_id, counter_name="promotions:day", window_key=day,
                                      limit=settings.autonomy_max_promotions_per_day, now=now,
                                      subject_type="CANDIDATE", subject_id=candidate_id)
            if reservation is None:
                raise _Abort("promotions cap reached")
            set_reservation_status(conn, reservation_id=reservation, status="CONSUMED", now=now)
        ap.set_dormant(conn, queue="CANDIDATE", item_id=candidate_id, now=now)
    except _Abort:
        conn.execute("ROLLBACK TO autonomy_promote")
        conn.execute("RELEASE autonomy_promote")
        return None
    except Exception:
        conn.execute("ROLLBACK TO autonomy_promote")
        conn.execute("RELEASE autonomy_promote")
        raise
    conn.execute("RELEASE autonomy_promote")
    return {"promotion": promotion, "application_workspace_id": workspace_id}


def promote_candidate(conn, *, settings: Settings, account_id: str, search_workspace_id: str, candidate_id: str,
                      screening_id: str | None, actor_type: str, actor: str, now: datetime) -> dict[str, Any] | None:
    return run_immediate(conn, lambda: _promote_in_transaction(
        conn, settings=settings, account_id=account_id, search_workspace_id=search_workspace_id,
        candidate_id=candidate_id, screening_id=screening_id, actor_type=actor_type, actor=actor, now=now))


def resolve_candidate_question(conn, *, settings: Settings, exception_id: str, resolution: str, actor: str,
                               reason: str | None, now: datetime) -> dict[str, Any]:
    if resolution not in ("PROMOTE", "DISMISS"):
        raise ValueError(resolution)

    def work() -> dict[str, Any]:
        exception = ap.get_candidate_exception(conn, exception_id)
        if exception is None:
            raise LookupError(exception_id)
        if exception["resolution"] is not None:
            raise CandidatePromotionRefused("this question has already been answered")
        out: dict[str, Any] = {}
        if resolution == "PROMOTE":
            promoted = _promote_in_transaction(
                conn, settings=settings, account_id=exception["account_id"],
                search_workspace_id=exception["search_workspace_id"], candidate_id=exception["candidate_id"],
                screening_id=exception["screening_id"], actor_type="USER", actor=actor, now=now)
            if promoted is None:
                raise CandidatePromotionRefused("this candidate can no longer be promoted")
            out["application_workspace_id"] = promoted["application_workspace_id"]
        else:
            set_discovery_candidate_status(conn, exception["candidate_id"], "dismissed",
                                           search_workspace_id=exception["search_workspace_id"],
                                           account_id=exception["account_id"], commit=False)
            ap.set_dormant(conn, queue="CANDIDATE", item_id=exception["candidate_id"], now=now)
        out["resolution"] = ap.resolve_candidate_exception(conn, exception_id=exception_id, resolution=resolution,
                                                           actor=actor, reason=reason, now=now)
        return out
    return run_immediate(conn, work)
