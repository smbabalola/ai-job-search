"""Assemble an immutable AuthorizationContext from the database (6B spec
§9.2). Reads only; the gate decides. Callers that need a consistent snapshot
(grant issuance, the pre-click transaction) call this inside BEGIN IMMEDIATE.

Deviations from the task-13 brief (fail-closed hardening):
  1. _score maps non-finite floats (NaN/inf) to UNKNOWN instead of a
     non-finite Decimal.
  2. _contradicted does not trust get_effective_resolution's pre-6B
     created_at/random-id tie-break: every blocker resolution tied for the
     latest created_at is compared, and any disagreement is a contradiction.
     Blocker answers are stored as {"type", "value"}; the wrapped value is
     compared as well as the raw one, so an agreeing answer in that shape is
     not a spurious contradiction.
  3. The employer 30-day window bound uses to_utc_iso.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from product.autonomy_contract import (
    UNKNOWN, AnswerCandidate, ApplyTargetFacts, AuthorityKind, AuthorizationContext, BudgetState, Capability,
    CounterState, EmployerKeyStrength, IdentityStrength, Mode, ProvenanceTier, Reach,
    RepresentationRequirement, RuleAcknowledgement, canonical_hash, normalized_employer_key, parse_utc,
    to_utc_iso,
)
from product.job_identity import job_identity
from product.submit_certification import submission_permitted, submit_certified
from product.semantic_subject_policy import load_subject_policy
from product.standing_policy import normalize_employment_type
from webapp.config import Settings
from webapp.persistence.application_blockers import list_application_blockers
from webapp.persistence.application_identity import get_search_workspace_for_application
from webapp.persistence.artifacts import get_current_artifact
from webapp.persistence.autonomy_answers import (
    current_apply_target_confirmation, current_approved_answers, current_rule_acknowledgements,
)
from webapp.persistence.autonomy_authority import current_policy, kill_switch_state, resolve_authority
from webapp.persistence.autonomy_ledger import (
    budget_usage, count_usage, live_intent, overridden_confirmed_intent, workspace_identity,
)
from webapp.persistence.workspaces import get_profile_workspace_id, get_workspace
from webapp.services.decision_policy import current_application_blockers, current_policy_decisions
from webapp.services.staleness import check_staleness
from webapp.services.workspace_view import resolve_apply_target
from webapp.persistence import dbapi

GOVERNING_ARTIFACT_TYPES = ("job_understanding_result", "job_fit_result", "application_intelligence_result")


@dataclass(frozen=True)
class RequirementSpec:
    key: str
    subject: str | None
    required: bool
    evidence_available: bool
    job_context: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class ApplyTargetObservation:
    adapter_id: str
    adapter_version: str
    landing_within_redirect_set: bool
    tenant_key: str | None
    tenant_matches_employer: bool
    ats_job_id_matches: bool | None
    unexplained_redirect: bool


def _float_safe(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _float_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_float_safe(v) for v in value]
    return value


def _find_items(payload: Any, ids: set[str], found: dict[str, Any]) -> None:
    if isinstance(payload, dict):
        if payload.get("id") in ids and payload["id"] not in found:
            found[payload["id"]] = payload
        for value in payload.values():
            _find_items(value, ids, found)
    elif isinstance(payload, list):
        for value in payload:
            _find_items(value, ids, found)


def evidence_values_hash(profile_payload: Any, evidence_ids: Sequence[str]) -> str | None:
    found: dict[str, Any] = {}
    _find_items(profile_payload, set(evidence_ids), found)
    if not found:
        return None
    return canonical_hash("answer-basis", "v1", _float_safe({i: found.get(i) for i in evidence_ids}))


def canonical_target_url(url: str | None) -> str | None:
    return job_identity({"source_url": url}).canonical_url_key if url else None


def apply_target_url(conn, *, workspace_id: str, account_id: str) -> str | None:
    """Canonical URL of the current apply target, for grant bindings."""
    target = resolve_apply_target(conn, workspace_id=workspace_id, account_id=account_id)
    return canonical_target_url(target.url) if target is not None else None


def employer_identity(posting: Mapping[str, Any], workspace: Mapping[str, Any],
                      observation: "ApplyTargetObservation | None" = None) -> tuple[str | None, EmployerKeyStrength]:
    """The employer key and its strength (shared by the authorization context
    and Review, 6D-A): an ATS tenant when observed, else the normalized name."""
    if observation is not None and observation.tenant_key:
        return f"tenant:{observation.adapter_id}:{observation.tenant_key}", EmployerKeyStrength.ATS_TENANT
    key = normalized_employer_key(posting.get("company") or workspace.get("company"))
    return key, EmployerKeyStrength.NORMALIZED_NAME if key else EmployerKeyStrength.UNKNOWN


def apply_target_state(conn, *, workspace_id: str, account_id: str,
                       identity_key: str | None) -> tuple[str | None, "ProvenanceTier | None"]:
    """(canonical target URL, provenance tier) of the current apply target,
    upgraded to user_confirmed_apply_target by a matching confirmation
    (shared by the authorization context and Review, 6D-A)."""
    target = resolve_apply_target(conn, workspace_id=workspace_id, account_id=account_id)
    if target is None:
        return None, None
    canonical = canonical_target_url(target.url)
    provenance = ProvenanceTier(target.provenance)
    confirmation = current_apply_target_confirmation(conn, workspace_id)
    if (confirmation and identity_key and confirmation["job_identity_key"] == identity_key
            and confirmation["canonical_url"] == canonical):
        provenance = ProvenanceTier.USER_CONFIRMED_APPLY_TARGET
    return canonical, provenance


def day_window(now: datetime, tz_name: str) -> tuple[str, datetime]:
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    midnight = datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=tz)
    return local.date().isoformat(), midnight.astimezone(timezone.utc)


def pack_readiness(conn, *, workspace_id: str, account_id: str, extensions_dir, unresolved: tuple) -> tuple[str | None, bool]:
    """(pack_artifact_id, auto_confirmable). Auto-confirmable means: a current
    application_pack exists, it is not stale, and no governing REQUIRE_USER
    is unresolved. Unsupported claims never enter a pack (Ticket 9 invariant
    8), so an existing current pack is grounded by construction."""
    pack = get_current_artifact(conn, workspace_id, "application_pack")
    if pack is None:
        return None, False
    stale = check_staleness(conn, workspace_id, "application_pack", extensions_dir=extensions_dir,
                            account_id=account_id)["stale"]
    return pack["id"], (not stale and not unresolved)


def _score(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return UNKNOWN
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        number = Decimal(str(value))
        return number if number.is_finite() else UNKNOWN
    return UNKNOWN


def current_governing_blockers(conn, workspace_id: str) -> list[dict[str, Any]]:
    """Blockers tied to the workspace's current governing artifacts, any
    status. Blockers of superseded artifacts stay in audit history only
    (shared by the authorization context and Review, 6D-A)."""
    out: list[dict[str, Any]] = []
    for artifact_type in GOVERNING_ARTIFACT_TYPES:
        artifact = get_current_artifact(conn, workspace_id, artifact_type)
        if artifact is not None:
            out.extend(current_application_blockers(conn, workspace_id, artifact["id"]))
    return out


def _governing(conn, workspace_id: str) -> tuple[bool, tuple[str, ...]]:
    auto_reject = False
    for artifact_type in GOVERNING_ARTIFACT_TYPES:
        artifact = get_current_artifact(conn, workspace_id, artifact_type)
        if artifact is None:
            continue
        decisions = current_policy_decisions(conn, workspace_id, artifact["id"])
        auto_reject = auto_reject or any(d["outcome"] == "AUTO_REJECT" for d in decisions)
    open_ids = {b["id"] for b in current_governing_blockers(conn, workspace_id) if b["status"] == "open"}
    return auto_reject, tuple(sorted(open_ids))


def _latest_resolutions(conn, blocker_id: str) -> list[dict[str, Any]]:
    """Every resolution tied for the latest created_at -- never a single row
    picked by a random-id tie-break."""
    rows = conn.execute(
        "SELECT id, answer_value FROM blocker_resolutions WHERE blocker_id = ? AND created_at = ("
        "  SELECT MAX(created_at) FROM blocker_resolutions WHERE blocker_id = ?)",
        (blocker_id, blocker_id),
    ).fetchall()
    return [{"id": r["id"], "answer_value": json.loads(r["answer_value"])} for r in rows]


def _agrees(resolution_value: Any, answer_value: Any) -> bool:
    if resolution_value == answer_value:
        return True
    return (isinstance(resolution_value, dict) and set(resolution_value) == {"type", "value"}
            and resolution_value["value"] == answer_value)


def _contradicted(conn, workspace_id: str, answer: dict[str, Any]) -> bool:
    for blocker in list_application_blockers(conn, workspace_id):
        if blocker.get("semantic_subject_key") != answer["subject"]:
            continue
        for resolution in _latest_resolutions(conn, blocker["id"]):
            if resolution["id"] == answer["source_blocker_resolution_id"]:
                continue
            if not _agrees(resolution["answer_value"], answer["value"]):
                return True
    return False


def _candidates(conn, *, account_id: str, workspace_id: str, subject: str,
                profile_payload: Any) -> tuple[AnswerCandidate, ...]:
    out = []
    for answer in current_approved_answers(conn, account_id=account_id, subject=subject):
        basis = answer["basis"]
        if basis["kind"] == "EVIDENCE":
            at_approval, current = basis["value_hash"], evidence_values_hash(profile_payload, basis["evidence_ids"])
        else:
            at_approval = current = None
        out.append(AnswerCandidate(
            approved_answer_id=answer["id"], subject=subject, reach=Reach(answer["reach"]),
            scope_id=answer["scope_id"], context=answer["context"],
            confirmed_at=parse_utc(answer["latest_confirmation_at"]), basis_kind=basis["kind"],
            basis_hash_at_approval=at_approval, basis_hash_current=current,
            contradicted=_contradicted(conn, workspace_id, answer),
        ))
    return tuple(out)



# Bundle 6D-A: Review resolves candidates exactly as the authorization context does.
answer_candidates = _candidates

def _counters(conn, *, settings: Settings, doc: dict, account_id: str, stage: Capability, now: datetime,
              run_id: str | None, employer_key: str | None) -> tuple[CounterState, ...]:
    limits = doc["limits"]
    day, midnight = day_window(now, doc["timezone"])
    if stage == Capability.FILL:
        used = count_usage(conn, account_id=account_id, counter_name="fill_per_day", window_key=day)
        return (CounterState("fill_per_day", stage, used, limits["fill_per_day"], midnight),)
    if stage != Capability.SUBMIT:
        return ()
    counters = [CounterState(
        "submit_per_day", stage,
        count_usage(conn, account_id=account_id, counter_name="submit_per_day", window_key=day),
        min(limits["submit_per_day"], settings.autonomy_live_submit_daily_cap), midnight,
    )]
    if run_id is not None:
        counters.append(CounterState(
            "submit_per_run", stage,
            count_usage(conn, account_id=account_id, counter_name="submit_per_run", window_key=run_id),
            limits["submit_per_run"], None,
        ))
    if employer_key is not None:
        since = now - timedelta(days=30)
        used = count_usage(conn, account_id=account_id, counter_name="submit_per_employer_30d",
                           window_key=employer_key, since=since)
        oldest = conn.execute(
            "SELECT MIN(created_at) AS t FROM limit_reservations WHERE account_id = ? AND counter_name = "
            "'submit_per_employer_30d' AND window_key = ? AND status IN ('RESERVED', 'CONSUMED') AND created_at >= ?",
            (account_id, employer_key, to_utc_iso(since)),
        ).fetchone()["t"]
        retry = parse_utc(oldest) + timedelta(days=30) if oldest else None
        counters.append(CounterState("submit_per_employer_30d", stage, used, limits["submit_per_employer_30d"], retry))
    return tuple(counters)


def _budgets(conn, *, doc: dict, account_id: str, workspace_id: str, now: datetime,
             estimates: Mapping[str, Decimal]) -> tuple[BudgetState, ...]:
    out = []
    day, midnight = day_window(now, doc["timezone"])
    for category, estimate in sorted(estimates.items()):
        caps = doc["limits"]["budgets"].get(category)
        if caps is None:
            continue
        for window, key, cap, retry in (("day", day, caps["per_day"], midnight),
                                        ("application", workspace_id, caps["per_application"], None)):
            used = budget_usage(conn, account_id=account_id, counter_name=f"budget:{category}:{window}", window_key=key)
            out.append(BudgetState(category, window, used, Decimal("0"), Decimal(cap), Decimal(estimate), retry))
    return tuple(out)


def build_context(conn: dbapi.Connection, *, settings: Settings, account_id: str, application_workspace_id: str,
                  requested_stage: Capability, mode: Mode, now: datetime, sentinel_present: bool,
                  requirements: Sequence[RequirementSpec] = (), observation: ApplyTargetObservation | None = None,
                  executor_hard_stops: Sequence[str] = (), run_id: str | None = None,
                  grant_binding_drift: Sequence[str] = (),
                  cost_estimates: Mapping[str, Decimal] | None = None,
                  authority: AuthorityKind = AuthorityKind.STANDING_POLICY,
                  submit_origin: str | None = None) -> AuthorizationContext:
    """authority=HUMAN_SUBMIT (6E-A spec §8.2) takes the deployment ceiling
    from settings.human_submit_ceiling() and the adapter's submit capability
    from the submit certification for `submit_origin` (§9.5), never from the
    6B JOBSEARCH_AUTONOMY_SUBMIT_ADAPTERS list."""
    ws = application_workspace_id
    human = authority is AuthorityKind.HUMAN_SUBMIT
    search_ws = get_search_workspace_for_application(conn, ws)
    account_max, workspace_ceiling = resolve_authority(conn, account_id=account_id, search_workspace_id=search_ws)
    policy = current_policy(conn, account_id)
    doc = policy["doc"] if policy else None

    posting_artifact = get_current_artifact(conn, ws, "job_posting_snapshot")
    posting = posting_artifact["payload"] if posting_artifact else {}
    fit_artifact = get_current_artifact(conn, ws, "job_fit_result")
    fit = fit_artifact["payload"] if fit_artifact else {}
    workspace = get_workspace(conn, ws, account_id=account_id) or {}

    identity_key, identity_strength, identity_conflict = workspace_identity(conn, ws)
    employer_key, employer_strength = employer_identity(posting, workspace, observation)

    verdict = fit.get("verdict")
    attributes = {
        "fit.overall_score": _score(fit.get("overall_score")),
        "fit.verdict": verdict.get("id") if isinstance(verdict, dict) and verdict.get("id") else UNKNOWN,
        "job.employment_type": normalize_employment_type(posting.get("employment_type")),
        "job.location": posting.get("location") or UNKNOWN,
        "job.title": posting.get("title") or workspace.get("title") or UNKNOWN,
        "company.key": employer_key or UNKNOWN,
        "workspace.id": search_ws or UNKNOWN,
        "identity.strength": identity_strength.value,
        **v2_attributes(conn, account_id=account_id, workspace_id=ws, posting=posting, workspace=workspace,
                        policy_doc=doc),
    }

    auto_reject, unresolved = _governing(conn, ws)
    pack_id, pack_ok = pack_readiness(conn, workspace_id=ws, account_id=account_id,
                                      extensions_dir=settings.extensions_dir, unresolved=unresolved)

    _, provenance = apply_target_state(conn, workspace_id=ws, account_id=account_id, identity_key=identity_key)
    if observation is None:
        apply_target = ApplyTargetFacts(provenance=provenance)
    else:
        apply_target = ApplyTargetFacts(
            provenance=provenance, adapter_id=observation.adapter_id,
            adapter_submit_capable=(
                submission_permitted(submit_certified(observation.adapter_id, observation.adapter_version),
                                     submit_origin or "", fixture_origins_enabled=settings.submit_fixture_origins_enabled)[0]
                if human else observation.adapter_id in settings.autonomy_submit_capable_adapters),
            landing_within_redirect_set=observation.landing_within_redirect_set,
            tenant_matches_employer=observation.tenant_matches_employer,
            ats_job_id_matches=observation.ats_job_id_matches,
            unexplained_redirect=observation.unexplained_redirect,
        )

    intent_state, overridden = None, False
    if identity_key:
        live = live_intent(conn, account_id=account_id, job_identity_key=identity_key)
        if live:
            intent_state = live["state"]
        elif overridden_confirmed_intent(conn, account_id=account_id, job_identity_key=identity_key):
            intent_state, overridden = "CONFIRMED", True

    profile_ws = get_profile_workspace_id(conn, account_id)
    profile_artifact = get_current_artifact(conn, profile_ws, "profile_snapshot") if profile_ws else None
    profile_payload = profile_artifact["payload"] if profile_artifact else {}
    reqs = tuple(
        RepresentationRequirement(
            key=spec.key, subject=spec.subject, required=spec.required,
            evidence_available=spec.evidence_available, job_context=dict(spec.job_context),
            candidates=(_candidates(conn, account_id=account_id, workspace_id=ws, subject=spec.subject,
                                    profile_payload=profile_payload) if spec.subject else ()),
        )
        for spec in requirements
    )

    acks = tuple(
        RuleAcknowledgement(a["rule_id"], a["rule_hash"], a["observed_fingerprint"], a["disposition"])
        for a in current_rule_acknowledgements(conn, ws)
    )
    counters = budgets = ()
    if doc is not None:
        counters = _counters(conn, settings=settings, doc=doc, account_id=account_id, stage=requested_stage,
                             now=now, run_id=run_id, employer_key=employer_key)
        budgets = _budgets(conn, doc=doc, account_id=account_id, workspace_id=ws, now=now,
                           estimates=cost_estimates or {})

    kill_switch = kill_switch_state(conn, account_id)
    return AuthorizationContext(
        mode=mode, requested_stage=requested_stage, now=now, account_id=account_id,
        application_workspace_id=ws, search_workspace_id=search_ws,
        deployment_ceiling=settings.human_submit_ceiling() if human else settings.autonomy_deployment_ceiling(),
        account_max=account_max, workspace_ceiling=workspace_ceiling,
        kill_switch_engaged=kill_switch["halted"], control_epoch=kill_switch["latest_engage_seq"],
        sentinel_present=sentinel_present,
        standing_policy=doc, subject_policy=load_subject_policy(), attributes=attributes,
        governing_auto_reject=auto_reject, unresolved_governing_require_user=unresolved,
        pack_artifact_id=pack_id, pack_auto_confirmable=pack_ok, requirements=reqs,
        apply_target=apply_target, identity_key=identity_key, identity_strength=identity_strength,
        identity_conflict=identity_conflict, existing_intent_state=intent_state, intent_overridden=overridden,
        employer_key=employer_key, employer_key_strength=employer_strength, counters=counters,
        budgets=budgets, rule_acknowledgements=acks, executor_hard_stops=tuple(executor_hard_stops),
        grant_binding_drift=tuple(grant_binding_drift), run_id=run_id, authority=authority,
    )


def v2_attributes(conn, *, account_id: str, workspace_id: str, posting: Mapping[str, Any],
                  workspace: Mapping[str, Any], policy_doc: Mapping[str, Any] | None) -> dict[str, Any]:
    """Bundle 7 standing-policy.v2 job attributes (spec 16.2), derived
    deterministically; anything underivable is UNKNOWN (never a guess)."""
    from product.job_families import classify
    from product.rule_attributes import (
        annual_compensation_max, country_from, family_attribute, remote_mode_from, rotation_from_text,
    )
    from webapp.services.cv_strategy import current_job_families
    understanding_artifact = get_current_artifact(conn, workspace_id, "job_understanding_result")
    understanding = (understanding_artifact or {}).get("payload") or {}
    title = understanding.get("title") or posting.get("title") or workspace.get("title") or ""
    seniority = understanding.get("seniority") if isinstance(understanding.get("seniority"), str) else None
    families, _ = current_job_families(conn, account_id)
    text = " ".join(str(posting.get(k) or "") for k in ("description", "raw_text", "title"))
    compensation = understanding.get("compensation") or posting.get("compensation")
    currency = (policy_doc or {}).get("currency")
    return {
        "job.family": family_attribute(classify(families, title=title, seniority=seniority).family_id),
        "job.compensation_max_annual": annual_compensation_max(compensation, currency),
        "job.country": country_from(understanding) if understanding else country_from(posting),
        "job.remote_mode": remote_mode_from(understanding) if understanding else remote_mode_from(posting),
        "job.rotation": rotation_from_text(text),
    }
