"""6E-A spec §8.2: the gate's HUMAN_SUBMIT authority. A human's one-time
authorization replaces the standing policy, the account/workspace autonomy
ceilings and the autonomous counters/budgets; every other check applies
unchanged. Existing (STANDING_POLICY) context fingerprints are byte-identical."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from product.autonomy_contract import (
    CONTEXT_SCHEMA, CONTEXT_SCHEMA_VERSION, AuthorityKind, BudgetState, Capability, CompletionBlocker,
    CounterState, IdentityStrength, ResultKind, canonical_hash,
)
from product.autonomy_gate import evaluate_authorization
from tests.product.autonomy_fixtures import NOW, good_target, make_ctx

C, R, H = Capability, ResultKind, AuthorityKind.HUMAN_SUBMIT

# Pinned before AuthorityKind existed (Task 4, acceptance 18), over the 6E-A
# subject policy: the context schema is unchanged.
PINNED = {
    "submit": "sha256:4a406b3e94e5eacf9d2c9bd09172b9ba6bd5e2d4a20e556ea83bbe47aa331803",
    "fill": "sha256:12a6d58326860fd56568927915a983dad2f0b15dcbd89aeebd15c0cb7ab9c78d",
}
# Bundle 7 Task 24 added these subjects to the policy (data, part of the
# context): the same contexts over the current policy.
BUNDLE7_SUBJECTS = ("contact.email", "contact.phone", "location.current")
PINNED_CURRENT = {
    "submit": "sha256:9294eed8d5b41f8823e8dc99aab3e818a5ac4c283e3213a1a400fc63e998715e",
    "fill": "sha256:ece6ced4eac600d75220e9d2a6de69eea11307590ab7770651a6e81610f2d276",
}


def codes(decision):
    return {r.code for r in decision.reasons}


def human(**overrides):
    return make_ctx(authority=H, **overrides)


def _without_bundle7_subjects(ctx):
    policy = dict(ctx.subject_policy)
    policy["subjects"] = {k: v for k, v in policy["subjects"].items() if k not in BUNDLE7_SUBJECTS}
    return replace(ctx, subject_policy=policy)


def test_existing_context_fingerprints_are_unchanged():
    for key, stage in (("submit", C.SUBMIT), ("fill", C.FILL)):
        ctx = make_ctx(requested_stage=stage)
        legacy = _without_bundle7_subjects(ctx)
        assert canonical_hash(CONTEXT_SCHEMA, CONTEXT_SCHEMA_VERSION, legacy) == PINNED[key]
        assert evaluate_authorization(legacy).input_fingerprint == PINNED[key]
        assert canonical_hash(CONTEXT_SCHEMA, CONTEXT_SCHEMA_VERSION, ctx) == PINNED_CURRENT[key]
        assert evaluate_authorization(ctx).input_fingerprint == PINNED_CURRENT[key]


def test_human_authority_changes_the_fingerprint():
    assert evaluate_authorization(human()).input_fingerprint != PINNED["submit"]


def test_human_authority_requires_the_submit_stage():
    d = evaluate_authorization(human(requested_stage=C.FILL))
    assert (d.result, d.deny_reason, d.grantable) == (R.DENY, "invalid_input", False)
    assert ("detail", "human_authority_stage") in {p for r in d.reasons for p in r.params}
    d = evaluate_authorization(make_ctx(authority="HUMAN"))
    assert d.deny_reason == "invalid_input"
    assert ("detail", "authority_invalid") in {p for r in d.reasons for p in r.params}


def test_human_authority_ignores_a_missing_standing_policy():
    assert evaluate_authorization(make_ctx(standing_policy=None)).effective_capability == C.NONE
    d = evaluate_authorization(human(standing_policy=None))
    assert (d.result, d.effective_capability, d.grantable) == (R.ALLOW, C.SUBMIT, True)
    assert "standing_policy_not_applied" in codes(d)


def test_human_authority_ignores_the_autonomy_ceilings_of_account_and_workspace():
    d = evaluate_authorization(human(account_max=C.FILL, workspace_ceiling=C.PREPARE))
    assert (d.effective_capability, d.grantable) == (C.SUBMIT, True)


def test_the_deployment_ceiling_still_caps_human_authority():
    d = evaluate_authorization(human(deployment_ceiling=C.FILL))
    assert d.effective_capability == C.FILL and not d.grantable and "human_submit_disabled" in codes(d)


def test_human_authority_ignores_autonomous_counters_and_budgets():
    at_limit = (CounterState(name="submit_per_day", stage=C.SUBMIT, used=1, limit=1, retry_at=NOW + timedelta(hours=1)),)
    over_budget = (BudgetState(category="llm", window="day", used=Decimal("10"), reserved=Decimal("0"),
                               estimate=Decimal("1"), cap=Decimal("5"), retry_at=NOW + timedelta(hours=1)),)
    assert not evaluate_authorization(make_ctx(counters=at_limit)).grantable
    d = evaluate_authorization(human(counters=at_limit, budgets=over_budget))
    assert d.grantable and d.result is R.ALLOW


def test_human_authority_still_denies_on_every_safety_check():
    assert evaluate_authorization(human(kill_switch_engaged=True)).deny_reason == "kill_switch"
    assert evaluate_authorization(human(sentinel_present=True)).deny_reason == "kill_switch"
    assert evaluate_authorization(human(existing_intent_state="CLAIMED")).deny_reason == "duplicate"
    assert evaluate_authorization(human(existing_intent_state="CONFIRMED")).deny_reason == "duplicate"
    assert evaluate_authorization(human(grant_binding_drift=("answers",))).deny_reason == "stale_binding"
    d = evaluate_authorization(human(apply_target=replace(good_target(), adapter_submit_capable=False)))
    assert not d.grantable and "adapter_not_submit_capable" in codes(d)
    d = evaluate_authorization(human(identity_strength=IdentityStrength.WEAK))
    assert not d.grantable and d.effective_capability == C.FILL
    d = evaluate_authorization(human(governing_auto_reject=True))
    assert d.result is R.BLOCK
