# tests/product/test_autonomy_gate_properties.py
from __future__ import annotations

import dataclasses
from datetime import timedelta

from hypothesis import given, settings, strategies as st

from product.autonomy_contract import (
    Capability, CounterState, IdentityStrength, Mode, ProvenanceTier, UNKNOWN,
)
from product.autonomy_gate import evaluate_authorization
from product.autonomy_contract import RepresentationRequirement
from tests.product.autonomy_fixtures import NOW, answer, good_target, make_ctx, make_policy

C = Capability
levels = st.sampled_from(list(C))
stages = st.sampled_from([C.PREPARE, C.FILL, C.SUBMIT])
EFFECTS = [{"type": "REDUCE_TO", "level": "NONE"}, {"type": "REDUCE_TO", "level": "PREPARE"},
           {"type": "REDUCE_TO", "level": "FILL"}, {"type": "REQUIRE_USER"}, {"type": "BLOCK"}]
ATTRS = ["fit.overall_score", "job.employment_type", "company.key"]


@st.composite
def rules(draw):
    count = draw(st.integers(0, 3))
    out = []
    for i in range(count):
        attr = draw(st.sampled_from(ATTRS))
        pred = ({"attr": attr, "op": "lt", "value": draw(st.integers(0, 100))} if attr == "fit.overall_score"
                else {"attr": attr, "op": "eq", "value": draw(st.sampled_from(["PERMANENT", "CONTRACT", "name:acme"]))})
        effect = draw(st.sampled_from(EFFECTS))
        out.append({"id": f"r{i}", "description": "", "when": pred, "effect": effect, "on_unknown": effect})
    return out


@st.composite
def contexts(draw):
    notice = draw(st.sampled_from([None, 0, 59, 61]))
    requirements = ()
    if notice is not None:
        requirements = (RepresentationRequirement(
            key="notice", subject="employment.notice_period", required=True, evidence_available=False,
            candidates=(answer("employment.notice_period", confirmed_at=NOW - timedelta(days=notice)),)),)
    return make_ctx(
        requested_stage=draw(stages),
        deployment_ceiling=draw(levels), account_max=draw(levels), workspace_ceiling=draw(levels),
        standing_policy=make_policy(*draw(rules())),
        attributes={"fit.overall_score": draw(st.integers(0, 100)),
                    "job.employment_type": draw(st.sampled_from(["PERMANENT", "CONTRACT"])),
                    "company.key": "name:acme"},
        identity_strength=draw(st.sampled_from(list(IdentityStrength))),
        apply_target=good_target(provenance=draw(st.sampled_from([None, *ProvenanceTier]))),
        pack_auto_confirmable=draw(st.booleans()),
        requirements=requirements,
    )


def tighten_ops(ctx):
    lower = lambda c: C(max(0, c - 1))
    yield dataclasses.replace(ctx, deployment_ceiling=lower(ctx.deployment_ceiling))
    yield dataclasses.replace(ctx, account_max=lower(ctx.account_max))
    yield dataclasses.replace(ctx, workspace_ceiling=lower(ctx.workspace_ceiling))
    yield dataclasses.replace(ctx, kill_switch_engaged=True)
    yield dataclasses.replace(ctx, sentinel_present=True)
    yield dataclasses.replace(ctx, identity_strength=IdentityStrength.WEAK)
    yield dataclasses.replace(ctx, identity_conflict=True)
    yield dataclasses.replace(ctx, pack_auto_confirmable=False)
    yield dataclasses.replace(ctx, existing_intent_state="CONFIRMED")
    yield dataclasses.replace(ctx, governing_auto_reject=True)
    # §8.1: "no target" already caps at PREPARE, stricter than imported_source's FILL cap,
    # so substituting IMPORTED_SOURCE is only a valid tightening when a target already exists.
    if ctx.apply_target.provenance is not None:
        yield dataclasses.replace(ctx, apply_target=good_target(provenance=ProvenanceTier.IMPORTED_SOURCE))
    yield dataclasses.replace(ctx, counters=(CounterState("x", ctx.requested_stage, 1, 1, None),))
    yield dataclasses.replace(ctx, unresolved_governing_require_user=("blk",))
    yield dataclasses.replace(ctx, standing_policy=make_policy(
        *ctx.standing_policy["rules"],
        {"id": "extra", "description": "", "when": {"attr": "fit.overall_score", "op": "lt", "value": 101},
         "effect": {"type": "REDUCE_TO", "level": "PREPARE"}, "on_unknown": {"type": "REDUCE_TO", "level": "PREPARE"}}))
    for attr in ATTRS:
        yield dataclasses.replace(ctx, attributes={**ctx.attributes, attr: UNKNOWN})


@settings(max_examples=300, deadline=None)
@given(contexts())
def test_monotonicity_and_unknown_safety(ctx):
    base = evaluate_authorization(ctx)
    for tighter in tighten_ops(ctx):
        d = evaluate_authorization(tighter)
        assert d.effective_capability <= base.effective_capability
        assert not (d.grantable and not base.grantable)


@settings(max_examples=200, deadline=None)
@given(contexts(), st.randoms())
def test_rule_order_independence(ctx, rnd):
    rules_ = list(ctx.standing_policy["rules"])
    rnd.shuffle(rules_)
    shuffled = dataclasses.replace(ctx, standing_policy=make_policy(*rules_))
    a, b = evaluate_authorization(ctx), evaluate_authorization(shuffled)
    assert (a.result, a.effective_capability, a.grantable, a.reasons, a.require_user_items) == \
           (b.result, b.effective_capability, b.grantable, b.reasons, b.require_user_items)


@settings(max_examples=200, deadline=None)
@given(contexts())
def test_determinism_and_authority_source(ctx):
    a, b = evaluate_authorization(ctx), evaluate_authorization(ctx)
    assert a == b
    assert a.effective_capability <= min(ctx.deployment_ceiling, ctx.account_max, ctx.workspace_ceiling)


@settings(max_examples=100, deadline=None)
@given(contexts(), st.sampled_from([Mode.SHADOW, Mode.DRY_RUN]))
def test_non_live_is_never_grantable(ctx, mode):
    assert not evaluate_authorization(dataclasses.replace(ctx, mode=mode)).grantable
