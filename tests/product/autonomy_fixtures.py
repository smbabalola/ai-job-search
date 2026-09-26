# tests/product/autonomy_fixtures.py
"""Shared AuthorizationContext builder for gate tests. The default context is
fully permitted: ALLOW(SUBMIT), grantable."""
from __future__ import annotations

from datetime import datetime, timezone

from product.autonomy_contract import (
    AnswerCandidate, ApplyTargetFacts, AuthorizationContext, Capability,
    EmployerKeyStrength, IdentityStrength, Mode, ProvenanceTier, Reach,
)
from product.semantic_subject_policy import load_subject_policy
from product.standing_policy import default_policy_document

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
SUBJECT_POLICY = load_subject_policy()


def make_policy(*rules, lists=None):
    doc = default_policy_document("Europe/London")
    doc["rules"] = list(rules)
    if lists:
        doc["employer_lists"] = lists
    return doc


def good_target(**overrides) -> ApplyTargetFacts:
    values = dict(
        provenance=ProvenanceTier.DISCOVERY_VERIFIED, adapter_id="greenhouse",
        adapter_submit_capable=True, landing_within_redirect_set=True,
        tenant_matches_employer=True, ats_job_id_matches=True, unexplained_redirect=False,
    )
    values.update(overrides)
    return ApplyTargetFacts(**values)


def answer(subject, *, answer_id="ans_1", reach=Reach.ACCOUNT, scope_id=None, context=None,
           confirmed_at=NOW, basis_kind="USER_ASSERTION", basis_at=None, basis_now=None,
           contradicted=False) -> AnswerCandidate:
    return AnswerCandidate(
        approved_answer_id=answer_id, subject=subject, reach=reach, scope_id=scope_id,
        context=context or {}, confirmed_at=confirmed_at, basis_kind=basis_kind,
        basis_hash_at_approval=basis_at, basis_hash_current=basis_now, contradicted=contradicted,
    )


def make_ctx(**overrides) -> AuthorizationContext:
    values = dict(
        mode=Mode.LIVE, requested_stage=Capability.SUBMIT, now=NOW,
        account_id="acct_1", application_workspace_id="ws_1", search_workspace_id="sw_1",
        deployment_ceiling=Capability.SUBMIT, account_max=Capability.SUBMIT,
        workspace_ceiling=Capability.SUBMIT, kill_switch_engaged=False, sentinel_present=False,
        standing_policy=make_policy(), subject_policy=SUBJECT_POLICY,
        attributes={"fit.overall_score": 80, "job.employment_type": "PERMANENT",
                    "company.key": "name:acme", "workspace.id": "sw_1"},
        governing_auto_reject=False, unresolved_governing_require_user=(),
        pack_artifact_id="art_pack", pack_auto_confirmable=True, requirements=(),
        apply_target=good_target(), identity_key="source:greenhouse:123",
        identity_strength=IdentityStrength.SOURCE_RECORD, identity_conflict=False,
        existing_intent_state=None, intent_overridden=False,
        employer_key="name:acme", employer_key_strength=EmployerKeyStrength.ATS_TENANT,
        counters=(), budgets=(), rule_acknowledgements=(),
    )
    values.update(overrides)
    return AuthorizationContext(**values)
