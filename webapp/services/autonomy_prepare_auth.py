"""PREPARE authorization at the 6C boundary (spec §6.5). A still-valid
recorded LIVE PREPARE decision is reused only when its material inputs
(the recorded inputs minus `now` and `run_id`), policy/subject-policy/engine
hashes and validity horizon still hold; otherwise a fresh decision is
recorded through 6B decide_and_record."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from product.autonomy_contract import (
    AuthorizationContext, Capability, Mode, canonical_hash, canonical_json, parse_utc,
)
from product.autonomy_gate import evaluate_authorization
from webapp.config import Settings
from webapp.persistence.autonomy_ledger import decision_inputs_payload
from webapp.services.autonomy import decide_and_record
from webapp.services.autonomy_context import build_context, day_window
from webapp.services.autonomy_controls import sentinel_present

_NON_MATERIAL = ("now", "run_id")


@dataclass(frozen=True)
class PrepareAuthorization:
    permitted: bool
    decision_id: str
    result: str
    effective_capability: str
    reused: bool
    deny_reason: str | None
    require_user_items: tuple[dict[str, str], ...]
    reasons: tuple[dict[str, Any], ...]


def material_fingerprint(inputs: Mapping[str, Any]) -> str:
    return canonical_hash("autonomy-material-context", "v1",
                          {k: v for k, v in inputs.items() if k not in _NON_MATERIAL})


def validity_horizon(created_at: datetime, ctx: AuthorizationContext) -> datetime | None:
    """The earliest temporal boundary of any time-sensitive PREPARE input
    (spec §6.5): the budget/limit window end (next local midnight after the
    decision), every counter/budget retry_at, and every answer candidate's
    freshness expiry (confirmed_at + the subject's freshness_days). If any
    boundary cannot be computed reliably (no standing policy, an answer whose
    subject is not in the subject policy), return None -> no reuse."""
    if ctx.standing_policy is None:
        return None
    bounds = [day_window(created_at, ctx.standing_policy["timezone"])[1]]
    bounds += [c.retry_at for c in ctx.counters if c.retry_at is not None]
    bounds += [b.retry_at for b in ctx.budgets if b.retry_at is not None]
    for requirement in ctx.requirements:
        for candidate in requirement.candidates:
            entry = ctx.subject_policy["subjects"].get(candidate.subject)
            if entry is None:
                return None
            if entry["freshness_days"] is not None:
                bounds.append(candidate.confirmed_at + timedelta(days=entry["freshness_days"]))
    return min(bounds)


def _latest_live_prepare(conn, application_workspace_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM autonomy_decisions WHERE application_workspace_id = ? AND requested_stage = 'PREPARE' "
        "AND mode = 'LIVE' ORDER BY seq DESC LIMIT 1", (application_workspace_id,)).fetchone()
    return dict(row) if row else None


def _from_row(row: Mapping[str, Any], *, reused: bool) -> PrepareAuthorization:
    permitted = (row["result"] == "ALLOW" and bool(row["grantable"])
                 and Capability[row["effective_capability"]] >= Capability.PREPARE)
    return PrepareAuthorization(
        permitted=permitted, decision_id=row["id"], result=row["result"],
        effective_capability=row["effective_capability"], reused=reused, deny_reason=row["deny_reason"],
        require_user_items=tuple({"kind": k, "ref": r} for k, r in json.loads(row["require_user_json"])),
        reasons=tuple({"code": c, "params": p} for c, p in json.loads(row["reasons_json"])),
    )


def authorize_prepare(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                      now: datetime) -> PrepareAuthorization:
    ctx = build_context(conn, settings=settings, account_id=account_id,
                        application_workspace_id=application_workspace_id, requested_stage=Capability.PREPARE,
                        mode=Mode.LIVE, now=now, sentinel_present=sentinel_present(settings.autonomy_sentinel_path))
    fresh = evaluate_authorization(ctx)
    current = material_fingerprint(json.loads(canonical_json(decision_inputs_payload(ctx))))
    latest = _latest_live_prepare(conn, application_workspace_id)
    if latest is not None and latest["engine_version"] == fresh.engine_version \
            and latest["policy_version_hash"] == fresh.policy_version_hash \
            and latest["subject_policy_hash"] == fresh.subject_policy_hash \
            and material_fingerprint(json.loads(latest["inputs_json"])) == current:
        horizon = validity_horizon(parse_utc(latest["created_at"]), ctx)
        if horizon is not None and now < horizon:
            return _from_row(latest, reused=True)
    _, row = decide_and_record(conn, settings=settings, account_id=account_id,
                               application_workspace_id=application_workspace_id,
                               requested_stage=Capability.PREPARE, mode=Mode.LIVE, now=now)
    return _from_row(row, reused=False)
