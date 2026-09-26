"""The Bundle 6C scheduler engine (spec §6, §10, §11).

One tick: sweeps (always, even halted) -> gates -> fair 1:1 selection ->
per item: fenced lease -> derive -> authorize -> re-check controls ->
reserve (paid steps, in the same transaction as the STARTED row) -> run the
step with no write transaction held -> fenced finalize. The scheduler is the
single owner of reservations, attempt rows, retries and settlement for every
paid step. It never calls request_grant or pre_click_commit."""
from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from product.autonomy_contract import canonical_hash, parse_utc
from product.candidate_promotion import candidate_input_fingerprint
from product.prepare_steps import (
    MAX_ATTEMPTS_PER_CYCLE, PAID_STEPS, ErrorClass, StepKind, next_prepare_step, retry_delay_seconds,
)
from webapp.config import Settings
from webapp.persistence import autonomy_prepare as ap
from webapp.persistence.application_identity import get_search_workspace_for_application
from webapp.persistence.autonomy_authority import current_policy, is_paused, kill_switch_state
from webapp.persistence.autonomy_ledger import (
    expire_grants, get_reservation, reserve_within_cap, settle_reservation,
)
from webapp.services import autonomy_candidates
from webapp.services.autonomy import AutonomyPaused, expire_unclicked, mark_stale_dispatches_ambiguous
from webapp.services.autonomy_context import day_window
from webapp.services.autonomy_controls import run_immediate, sentinel_present
from webapp.services.autonomy_inbox import notify_outcome, reconcile_notifications
from webapp.services.autonomy_prepare import (
    classify_error, prepare_snapshot, retry_after_seconds, run_paid_step, run_system_review, system_gate4,
)
from webapp.services.autonomy_prepare_auth import authorize_prepare
from webapp.services.autonomy_providers import NoCostEvidence, ProviderSet
from webapp.services.pipeline import PipelineError

logger = logging.getLogger(__name__)


@dataclass
class TickReport:
    sweeps: dict[str, Any] = field(default_factory=dict)
    processed: list[dict[str, Any]] = field(default_factory=list)


class _LostLease(Exception):
    pass


# ---- helpers ------------------------------------------------------------------

def _halted(conn, settings: Settings, account_id: str) -> bool:
    return kill_switch_state(conn, account_id)["halted"] or sentinel_present(settings.autonomy_sentinel_path)


def _app_controls_ok(conn, settings: Settings, account_id: str, ws: str) -> bool:
    search_ws = get_search_workspace_for_application(conn, ws)
    return (settings.autonomy_scheduler_enabled and not _halted(conn, settings, account_id)
            and ap.is_enrolled(conn, ws)
            and not is_paused(conn, account_id=account_id, scope_type="APPLICATION", scope_id=ws)
            and not (search_ws and is_paused(conn, account_id=account_id, scope_type="SEARCH_WORKSPACE",
                                             scope_id=search_ws)))


def _llm_caps(conn, account_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    policy = current_policy(conn, account_id)
    doc = policy["doc"] if policy else None
    llm = ((doc or {}).get("limits", {}).get("budgets", {}) or {}).get("LLM")
    return doc, llm


def _cycle_id(conn, subject_type: str, subject_id: str, step: str, fingerprint: str) -> str | None:
    request = ap.latest_retry_request(conn, subject_type=subject_type, subject_id=subject_id, step_kind=step,
                                      input_fingerprint=fingerprint)
    return request["id"] if request else None


def _overage_blocked(conn, step: str, envelope: Decimal) -> bool:
    import json
    rows = conn.execute("SELECT cost_json FROM autonomy_prepare_steps WHERE step_kind = ? AND error_code = "
                        "'cost_overage'", (step,)).fetchall()
    return any(json.loads(r["cost_json"]).get("envelope") == str(envelope) for r in rows)


def _finalize(conn, *, queue: str, item_id: str, worker_id: str, generation: int, now: datetime,
              next_eligible_at: datetime | None) -> None:
    run_immediate(conn, lambda: ap.finalize_lease(conn, queue=queue, item_id=item_id, worker_id=worker_id,
                                                  generation=generation, now=now,
                                                  next_eligible_at=next_eligible_at))


def _release(conn, *, queue: str, item_id: str, worker_id: str, generation: int, now: datetime) -> None:
    run_immediate(conn, lambda: ap.release_lease(conn, queue=queue, item_id=item_id, worker_id=worker_id,
                                                 generation=generation, now=now))


def _dormant_with(conn, *, queue: str, item_id: str, worker_id: str, generation: int, now: datetime,
                  account_id: str, subject_type: str, kind: str, reason: str, fingerprint: str,
                  detail: dict[str, Any]) -> None:
    def work():
        if ap.finalize_lease(conn, queue=queue, item_id=item_id, worker_id=worker_id, generation=generation,
                             now=now, next_eligible_at=None):
            notify_outcome(conn, account_id=account_id, subject_type=subject_type, subject_id=item_id, kind=kind,
                           reason=reason, fingerprint=fingerprint, detail=detail, now=now)
    run_immediate(conn, work)


# ---- sweeps ---------------------------------------------------------------------

def _recover_orphans(conn, *, now: datetime, meter) -> int:
    recovered = 0
    for subject_type, (table, key) in ap.QUEUES.items():
        rows = conn.execute(f"SELECT {key} AS item_id, lease_holder, lease_generation, lease_expires_at "
                            f"FROM {table}").fetchall()
        for row in rows:
            for orphan in ap.orphaned_attempts(conn, subject_type, row["item_id"]):
                lease_free = row["lease_holder"] is None or (row["lease_expires_at"] and
                                                              parse_utc(row["lease_expires_at"]) <= now)
                if not (lease_free or row["lease_generation"] > orphan["lease_generation"]):
                    continue

                def work(orphan=orphan):
                    settled = []
                    for reservation_id in orphan["reservation_ids"]:
                        reservation = get_reservation(conn, reservation_id)
                        reserved = Decimal(reservation["amount"])
                        actual = meter.actual_cost(orphan["step_kind"], reserved=reserved)
                        amount = actual if actual is not None else reserved
                        settle_reservation(conn, reservation_id=reservation_id, attempt_id=orphan["attempt_id"],
                                           amount=amount, now=now)
                        settled.append(str(amount))
                    ap.finish_attempt(conn, attempt_id=orphan["attempt_id"], event="ABANDONED", now=now,
                                      cost={"settled": settled, "source": "recovery"})
                run_immediate(conn, work)
                recovered += 1
    return recovered


def _sweeps(conn, settings: Settings, now: datetime, meter) -> dict[str, Any]:
    out = {"expired_grants": run_immediate(conn, lambda: expire_grants(conn, now=now)),
           "expired_unclicked": expire_unclicked(conn, now=now),
           "stale_dispatches": mark_stale_dispatches_ambiguous(
               conn, now=now, result_timeout=timedelta(seconds=settings.autonomy_dispatch_result_timeout)),
           "abandoned": _recover_orphans(conn, now=now, meter=meter)}
    out["resolved_notifications"] = sum(
        reconcile_notifications(conn, account_id=row["id"], now=now)
        for row in conn.execute("SELECT id FROM accounts").fetchall())
    return out


# ---- paid-step finalization (shared by applications and candidates) ------------

def _finish_paid(conn, *, queue: str, item_id: str, subject_type: str, account_id: str, worker_id: str,
                 generation: int, attempt_id: str, reservation_ids: list[str], step: str, envelope: Decimal,
                 fingerprint: str, retry_request_id: str | None, refs: list[dict] | None,
                 error: BaseException | None, settings: Settings, rng: random.Random, meter,
                 now: datetime) -> None:
    def work():
        if not ap.lease_is_held(conn, queue=queue, item_id=item_id, worker_id=worker_id, generation=generation,
                                now=now):
            raise _LostLease()
        notify = None
        next_at: datetime | None = now
        if error is None:
            actual = meter.actual_cost(step, reserved=envelope)
            amount = actual if actual is not None else envelope
            for reservation_id in reservation_ids:
                settle_reservation(conn, reservation_id=reservation_id, attempt_id=attempt_id, amount=amount, now=now)
            cost = {"reserved": str(envelope), "settled": str(amount), "envelope": str(envelope),
                    "source": "meter" if actual is not None else "reserved_maximum"}
            if actual is not None and actual > envelope:
                ap.finish_attempt(conn, attempt_id=attempt_id, event="FAILED", now=now, artifact_refs=refs or [],
                                  cost=cost, error_class="INTERNAL", error_code="cost_overage",
                                  error_detail=f"actual {actual} exceeded the {envelope} envelope")
                next_at, notify = None, ("OPERATIONAL_ERROR", "cost_overage")
            else:
                ap.finish_attempt(conn, attempt_id=attempt_id, event="SUCCEEDED", now=now,
                                  artifact_refs=refs or [], cost=cost)
        else:
            error_class, code = classify_error(error)
            for reservation_id in reservation_ids:  # the call may have spent: settle at the maximum
                settle_reservation(conn, reservation_id=reservation_id, attempt_id=attempt_id, amount=envelope,
                                   now=now)
            ap.finish_attempt(conn, attempt_id=attempt_id, event="FAILED", now=now,
                              cost={"reserved": str(envelope), "settled": str(envelope), "envelope": str(envelope)},
                              error_class=error_class.value, error_code=code, error_detail=str(error)[:500])
            if error_class is ErrorClass.TRANSIENT:
                failures = ap.cycle_failures(conn, subject_type=subject_type, subject_id=item_id, step_kind=step,
                                             input_fingerprint=fingerprint, retry_request_id=retry_request_id)
                delay = retry_delay_seconds(failures, settings.autonomy_retry_delays, rng, retry_after_seconds(error))
                if delay is None:
                    next_at, notify = None, ("OPERATIONAL_ERROR", "transient_exhausted")
                else:
                    next_at = now + timedelta(seconds=delay)
            elif error_class is ErrorClass.HUMAN_FIXABLE:
                next_at, notify = None, ("NEEDS_USER", f"human_fixable:{code}")
            else:
                next_at, notify = None, ("OPERATIONAL_ERROR", f"internal:{code}")
        ap.finalize_lease(conn, queue=queue, item_id=item_id, worker_id=worker_id, generation=generation, now=now,
                          next_eligible_at=next_at)
        if notify:
            notify_outcome(conn, account_id=account_id, subject_type=subject_type, subject_id=item_id,
                           kind=notify[0], reason=notify[1], fingerprint=fingerprint,
                           detail={"step": step, "error": str(error)[:200] if error else None}, now=now)
    try:
        run_immediate(conn, work)
    except _LostLease:
        logger.info("lease lost for %s %s; the attempt is left for recovery", subject_type, item_id)


# ---- applications ---------------------------------------------------------------

def _step_fingerprint(step: StepKind, detail: dict[str, Any]) -> str:
    return canonical_hash("autonomy-step-input", "v1", {
        "step": step.value, "content_ids": detail["content_ids"],
        "profile": (detail["profile"] or {}).get("content_id"), "pack_revision": detail["pack_revision"]})


def _process_application(conn, *, item: dict[str, Any], settings: Settings, providers: ProviderSet,
                         now: datetime, rng: random.Random, worker_id: str, meter,
                         clock: Callable[[], datetime]) -> dict[str, Any]:
    ws, account_id = item["item_id"], item["account_id"]
    report = {"subject_type": "APPLICATION", "subject_id": ws, "action": "skipped"}
    if _halted(conn, settings, account_id):
        return report
    ttl = timedelta(seconds=settings.autonomy_step_timeout + settings.autonomy_lease_margin)
    generation = run_immediate(conn, lambda: ap.acquire_lease(conn, queue="APPLICATION", item_id=ws,
                                                              worker_id=worker_id, now=now, ttl=ttl))
    if generation is None:
        return report
    lease = dict(queue="APPLICATION", item_id=ws, worker_id=worker_id, generation=generation)
    if not _app_controls_ok(conn, settings, account_id, ws):
        _release(conn, now=now, **lease)
        report["action"] = "released"
        return report
    snapshot, detail = prepare_snapshot(conn, settings=settings, account_id=account_id, application_workspace_id=ws)
    nxt = next_prepare_step(snapshot)
    fingerprint = canonical_hash("autonomy-outcome", "v1", {"revision": detail["pack_revision"],
                                                             "content_ids": detail["content_ids"]})
    if nxt.kind == "DONE":
        _finalize(conn, now=now, next_eligible_at=None, **lease)
        report["action"] = "done"
        return report
    if nxt.kind in ("PREPARED", "NEEDS_USER"):
        kind = "PREPARED" if nxt.kind == "PREPARED" else "NEEDS_USER"
        items = [f"{i['item_type']}:{i['item_id']}" for i in detail["judgment"]]
        _dormant_with(conn, now=now, account_id=account_id, subject_type="APPLICATION", kind=kind,
                      reason=nxt.reason or "prepared", fingerprint=fingerprint, detail={"items": items}, **lease)
        report["action"] = kind.lower()
        return report
    step = nxt.step
    try:
        authorization = authorize_prepare(conn, settings=settings, account_id=account_id,
                                          application_workspace_id=ws, now=now)
    except AutonomyPaused:
        _release(conn, now=now, **lease)
        report["action"] = "released"
        return report
    if not authorization.permitted:
        if authorization.result == "REQUIRE_USER":
            _dormant_with(conn, now=now, account_id=account_id, subject_type="APPLICATION", kind="NEEDS_USER",
                          reason="require_user", fingerprint=authorization.decision_id,
                          detail={"items": list(authorization.require_user_items)}, **lease)
        elif authorization.result == "BLOCK":
            _dormant_with(conn, now=now, account_id=account_id, subject_type="APPLICATION", kind="BLOCKED",
                          reason="blocked", fingerprint=authorization.decision_id, detail={}, **lease)
        elif authorization.result == "DENY_TEMPORARY":
            row = conn.execute("SELECT retry_at FROM autonomy_decisions WHERE id = ?",
                               (authorization.decision_id,)).fetchone()
            retry = parse_utc(row["retry_at"]) if row and row["retry_at"] else now + timedelta(hours=1)
            _finalize(conn, now=now, next_eligible_at=retry, **lease)
        elif authorization.deny_reason == "kill_switch":
            _release(conn, now=now, **lease)
        else:
            _finalize(conn, now=now, next_eligible_at=None, **lease)  # e.g. capability below PREPARE
        report["action"] = f"not_permitted:{authorization.result}"
        return report
    if not _app_controls_ok(conn, settings, account_id, ws):  # re-check right before the step
        _release(conn, now=now, **lease)
        report["action"] = "released"
        return report
    step_fp = _step_fingerprint(step, detail)
    cycle = _cycle_id(conn, "APPLICATION", ws, step.value, step_fp)
    if ap.cycle_failures(conn, subject_type="APPLICATION", subject_id=ws, step_kind=step.value,
                         input_fingerprint=step_fp, retry_request_id=cycle) >= MAX_ATTEMPTS_PER_CYCLE:
        _finalize(conn, now=now, next_eligible_at=None, **lease)  # escalated: needs new inputs or a Retry
        report["action"] = "escalated"
        return report
    paid = step in PAID_STEPS
    envelope = settings.autonomy_step_envelope(step.value) if paid else Decimal("0")
    if paid:
        doc, llm = _llm_caps(conn, account_id)
        if not llm or not llm.get("per_day"):
            _dormant_with(conn, now=now, account_id=account_id, subject_type="APPLICATION", kind="NEEDS_USER",
                          reason="budget_missing", fingerprint="budget", detail={"step": step.value}, **lease)
            report["action"] = "budget_missing"
            return report
        if envelope is None:
            _dormant_with(conn, now=now, account_id=account_id, subject_type="APPLICATION", kind="NEEDS_USER",
                          reason="envelope_missing", fingerprint=step.value, detail={"step": step.value}, **lease)
            report["action"] = "envelope_missing"
            return report
        if _overage_blocked(conn, step.value, envelope):
            _dormant_with(conn, now=now, account_id=account_id, subject_type="APPLICATION",
                          kind="OPERATIONAL_ERROR", reason="cost_overage", fingerprint=str(envelope),
                          detail={"step": step.value}, **lease)
            report["action"] = "overage_blocked"
            return report

    def begin():
        if not ap.lease_is_held(conn, now=now, **{k: lease[k] for k in ("queue", "item_id", "worker_id",
                                                                           "generation")}):
            raise _LostLease()
        reservations: list[str] = []
        if paid:
            day, _ = day_window(now, doc["timezone"])
            for counter, window, cap in (("budget:LLM:day", day, llm["per_day"]),
                                         ("budget:LLM:application", ws, llm.get("per_application"))):
                if cap is None:
                    continue
                reservation = reserve_within_cap(conn, account_id=account_id, counter_name=counter,
                                                 window_key=window, cap=Decimal(str(cap)), amount=envelope,
                                                 subject_type="APPLICATION", subject_id=ws, now=now)
                if reservation is None:
                    raise _BudgetWait()
                reservations.append(reservation)
        attempt_id = ap.start_attempt(
            conn, subject_type="APPLICATION", subject_id=ws, step_kind=step.value,
            attempt_no=ap.next_attempt_no(conn, "APPLICATION", ws, step.value), input_fingerprint=step_fp,
            authorization_decision_id=authorization.decision_id, retry_request_id=cycle,
            lease_generation=generation, worker_id=worker_id, reservation_ids=reservations, now=now)
        return attempt_id, reservations
    try:
        attempt_id, reservations = run_immediate(conn, begin)
    except _BudgetWait:
        midnight = day_window(now, doc["timezone"])[1]
        _finalize(conn, now=now, next_eligible_at=midnight, **lease)
        report["action"] = "budget_wait"
        return report
    except _LostLease:
        return report

    refs, error = None, None
    try:
        if step is StepKind.SYSTEM_REVIEW:
            run_system_review(conn, settings=settings, account_id=account_id, application_workspace_id=ws, now=now)
            refs = []
        elif step is StepKind.GATE4:
            out = system_gate4(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                               authorization=authorization, expected_revision=detail["pack_revision"], now=now)
            refs = [{"artifact_id": out["artifact"]["id"], "artifact_type": "application_pack"}]
        else:
            refs = run_paid_step(conn, settings=settings, providers=providers, step=step, account_id=account_id,
                                 application_workspace_id=ws,
                                 request_id=f"auto-{step.value.lower()}-{ws}-{step_fp[-12:]}")
    except Exception as exc:  # classified at finalize
        error = exc
        if isinstance(exc, PipelineError) and step in (StepKind.SYSTEM_REVIEW, StepKind.GATE4):
            error = TimeoutError(f"re-derive after refusal: {exc}")  # bounded by the retry cycle
    finish_now = clock()
    _finish_paid(conn, queue="APPLICATION", item_id=ws, subject_type="APPLICATION", account_id=account_id,
                 worker_id=worker_id, generation=generation, attempt_id=attempt_id, reservation_ids=reservations,
                 step=step.value, envelope=envelope, fingerprint=step_fp, retry_request_id=cycle, refs=refs,
                 error=error, settings=settings, rng=rng, meter=meter if paid else NoCostEvidence(), now=finish_now)
    report["action"] = f"ran:{step.value}"
    return report


class _BudgetWait(Exception):
    pass


# ---- candidates -----------------------------------------------------------------

def _process_candidate(conn, *, item: dict[str, Any], settings: Settings, providers: ProviderSet, now: datetime,
                       rng: random.Random, worker_id: str, meter, clock: Callable[[], datetime]) -> dict[str, Any]:
    cid, account_id = item["item_id"], item["account_id"]
    report = {"subject_type": "CANDIDATE", "subject_id": cid, "action": "skipped"}
    row = conn.execute("SELECT search_workspace_id FROM autonomy_candidate_queue WHERE candidate_id = ?",
                       (cid,)).fetchone()
    if row is None or _halted(conn, settings, account_id):
        return report
    sw = row["search_workspace_id"]
    ttl = timedelta(seconds=settings.autonomy_step_timeout + settings.autonomy_lease_margin)
    generation = run_immediate(conn, lambda: ap.acquire_lease(conn, queue="CANDIDATE", item_id=cid,
                                                              worker_id=worker_id, now=now, ttl=ttl))
    if generation is None:
        return report
    lease = dict(queue="CANDIDATE", item_id=cid, worker_id=worker_id, generation=generation)
    if is_paused(conn, account_id=account_id, scope_type="SEARCH_WORKSPACE", scope_id=sw):
        _release(conn, now=now, **lease)
        report["action"] = "released"
        return report
    try:
        ctx = autonomy_candidates.build_candidate_context(conn, settings=settings, account_id=account_id,
                                                          search_workspace_id=sw, candidate_id=cid, now=now)
    except LookupError:
        _finalize(conn, now=now, next_eligible_at=None, **lease)
        return report
    action = autonomy_candidates.candidate_next_action(conn, ctx)
    report["action"] = action.lower()
    if action == "DONE":
        _finalize(conn, now=now, next_eligible_at=None, **lease)
    elif action == "SCREEN":
        def screen():
            if not ap.lease_is_held(conn, now=now, queue="CANDIDATE", item_id=cid, worker_id=worker_id,
                                    generation=generation):
                raise _LostLease()
            _, next_at = autonomy_candidates.screen_candidate(conn, ctx=ctx, now=now)
            ap.finalize_lease(conn, now=now, next_eligible_at=next_at, **lease)
        try:
            run_immediate(conn, screen)
        except _LostLease:
            pass
    elif action == "PROMOTE":
        latest = ap.latest_screening(conn, cid)
        promoted = autonomy_candidates.promote_candidate(
            conn, settings=settings, account_id=account_id, search_workspace_id=sw, candidate_id=cid,
            screening_id=latest["id"] if latest else None, actor_type="SCHEDULER", actor=worker_id, now=now)
        _finalize(conn, now=now, next_eligible_at=None if promoted else now + timedelta(seconds=60), **lease)
    else:  # EVALUATE: admission re-run inside the reservation/STARTED transaction
        envelope = settings.autonomy_step_envelope("EVALUATE")

        def begin():
            if not ap.lease_is_held(conn, now=now, queue="CANDIDATE", item_id=cid, worker_id=worker_id,
                                    generation=generation):
                raise _LostLease()
            fresh = autonomy_candidates.build_candidate_context(conn, settings=settings, account_id=account_id,
                                                                search_workspace_id=sw, candidate_id=cid, now=now)
            admission = autonomy_candidates.admit_candidate_evaluation(fresh)
            if not admission.admitted:
                return ("REFUSED", admission.reasons, fresh)
            day, _ = day_window(now, fresh.standing_policy["timezone"])
            llm = fresh.standing_policy["limits"]["budgets"]["LLM"]
            reservation = reserve_within_cap(conn, account_id=account_id, counter_name="budget:LLM:day",
                                             window_key=day, cap=Decimal(str(llm["per_day"])), amount=envelope,
                                             subject_type="CANDIDATE", subject_id=cid, now=now)
            if reservation is None:
                return ("REFUSED", ("budget_exhausted",), fresh)
            fingerprint = candidate_input_fingerprint(fresh)
            cycle = _cycle_id(conn, "CANDIDATE", cid, "EVALUATE", fingerprint)
            attempt_id = ap.start_attempt(
                conn, subject_type="CANDIDATE", subject_id=cid, step_kind="EVALUATE",
                attempt_no=ap.next_attempt_no(conn, "CANDIDATE", cid, "EVALUATE"), input_fingerprint=fingerprint,
                authorization_decision_id=None, retry_request_id=cycle, lease_generation=generation,
                worker_id=worker_id, reservation_ids=[reservation], now=now)
            return ("RUN", (attempt_id, reservation, fingerprint, cycle), fresh)
        try:
            status, payload, fresh = run_immediate(conn, begin)
        except _LostLease:
            return report
        if status == "REFUSED":
            reasons = set(payload)
            if reasons <= {"budget_exhausted"} and fresh.standing_policy is not None:
                _finalize(conn, now=now, next_eligible_at=day_window(now, fresh.standing_policy["timezone"])[1],
                          **lease)
            elif reasons & {"halted", "paused", "scheduler_disabled"}:
                _release(conn, now=now, **lease)
            else:
                def screen_refused():
                    _, _next = autonomy_candidates.screen_candidate(conn, ctx=fresh, now=now)
                    ap.finalize_lease(conn, now=now, next_eligible_at=None, **lease)
                run_immediate(conn, screen_refused)
            report["action"] = "evaluate_refused"
            return report
        attempt_id, reservation, fingerprint, cycle = payload
        refs, error = None, None
        try:
            result = autonomy_candidates.run_candidate_evaluation(
                conn, settings=settings, providers=providers, ctx=fresh,
                request_id=f"auto-eval-{cid}-{fingerprint[-12:]}")
            refs = [{"discovery_fit_id": (result or {}).get("id")}]
        except Exception as exc:
            error = exc
        _finish_paid(conn, queue="CANDIDATE", item_id=cid, subject_type="CANDIDATE", account_id=account_id,
                     worker_id=worker_id, generation=generation, attempt_id=attempt_id, reservation_ids=[reservation],
                     step="EVALUATE", envelope=envelope, fingerprint=fingerprint, retry_request_id=cycle, refs=refs,
                     error=error, settings=settings, rng=rng, meter=meter, now=clock())
    return report


# ---- the tick -------------------------------------------------------------------

def _interleave(apps: list[dict], candidates: list[dict], limit: int) -> list[tuple[str, dict]]:
    order: list[tuple[str, dict]] = []
    for i in range(max(len(apps), len(candidates))):
        if i < len(apps):
            order.append(("APPLICATION", apps[i]))
        if i < len(candidates):
            order.append(("CANDIDATE", candidates[i]))
    return order[:limit]


def run_tick(conn, *, settings: Settings, providers: ProviderSet, now: datetime, rng: random.Random,
             worker_id: str, cost_meter=None, clock: Callable[[], datetime] | None = None) -> TickReport:
    meter = cost_meter or NoCostEvidence()
    clock = clock or (lambda: now)
    report = TickReport(sweeps=_sweeps(conn, settings, now, meter))
    if not settings.autonomy_scheduler_enabled:
        return report
    limit = settings.autonomy_max_items_per_tick
    apps = ap.due_items(conn, queue="APPLICATION", now=now, limit=limit)
    candidates = ap.due_items(conn, queue="CANDIDATE", now=now, limit=limit)
    for queue, item in _interleave(apps, candidates, limit):
        handler = _process_application if queue == "APPLICATION" else _process_candidate
        try:
            report.processed.append(handler(conn, item=item, settings=settings, providers=providers, now=now,
                                            rng=rng, worker_id=worker_id, meter=meter, clock=clock))
        except Exception:
            logger.exception("autonomy tick failed for %s %s", queue, item.get("item_id"))
    return report


# ---- drivers ------------------------------------------------------------------------

def wake_all_on_start(conn, *, now: datetime) -> int:
    """Policy/extension files may have changed while no driver ran: every
    queued item re-derives its next step once (wake only)."""
    def work() -> int:
        total = 0
        for table, _ in ap.QUEUES.values():
            total += conn.execute(f"UPDATE {table} SET next_eligible_at = ?, updated_at = ?",
                                  (now.astimezone(timezone.utc).isoformat(timespec="microseconds"),
                                   now.astimezone(timezone.utc).isoformat(timespec="microseconds"))).rowcount
        return total
    return run_immediate(conn, work)


def run_driver(settings: Settings, providers: ProviderSet, *, stop: threading.Event,
               clock: Callable[[], datetime], rng: random.Random, worker_id: str,
               status: dict[str, Any] | None = None) -> None:
    """Run ticks until stopped: one tick to completion, then wait about
    autonomy_tick_interval. A driver never overlaps its own ticks."""
    from webapp.persistence.db import connect
    conn = connect(settings.db_path)
    status = status if status is not None else {}
    status["running"] = True
    try:
        wake_all_on_start(conn, now=clock())
        while not stop.is_set():
            if settings.autonomy_scheduler_enabled:
                try:
                    run_tick(conn, settings=settings, providers=providers, now=clock(), rng=rng,
                             worker_id=worker_id, clock=clock)
                    status["last_tick_at"] = clock().isoformat()
                except Exception:
                    logger.exception("autonomy driver tick failed")
            stop.wait(settings.autonomy_tick_interval)
    finally:
        status["running"] = False
        conn.close()
