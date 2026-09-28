"""6D-B G4 manifest coverage (spec §11.2.1, acceptance 19): each of the six
rules has a failing case that produces exactly its violation; OMIT and
IGNORE never yield manifest entries."""
from __future__ import annotations

import copy

import pytest

from product.fill_manifest import value_hash
from product.fill_plan import derive_manifest, g4_violations
from product.review_contract import binding_hash
from tests.product.fill_plan_fixtures import DOC_SHA, binding, build

CONFIRMATIONS = {"subject:employment.notice_period": "conf_notice", "subject:mobility.relocation": "conf_reloc"}


@pytest.fixture
def world():
    bind = binding()
    plan = build(bind=bind).plan
    manifest = derive_manifest(plan, binding=bind, confirmation_ids=CONFIRMATIONS)
    return bind, plan, manifest


def _entry(manifest, key):
    return next(e for p in manifest["pages"] for e in p["entries"] if e["page_field_key"] == key)


def _violations(bind, plan, manifest):
    return g4_violations(manifest, plan, bind, binding_hash_value=binding_hash(bind))


def test_the_derived_manifest_is_covered(world):
    assert _violations(*world) == []


def test_manifest_derivation_follows_the_table(world):
    _, _, manifest = world
    notice = _entry(manifest, "gh:notice")
    assert notice["source"] == {"kind": "APPROVED_ANSWER", "ref": "ref_employment.notice_period",
                                "confirmation_id": "conf_notice"}
    assert notice["value_hash"] == value_hash("1 month")  # the APPROVED hash, not the rendered one
    assert notice["subject"] == "employment.notice_period" and notice["normalized_field_type"] is None
    email = _entry(manifest, "gh:email")
    assert email["source"]["kind"] == "EVIDENCE" and email["source"]["confirmation_id"] is None
    assert email["normalized_field_type"] == "email" and email["subject"] is None
    resume = _entry(manifest, "gh:resume")
    assert resume["source"] == {"kind": "PACK_DOCUMENT", "ref": DOC_SHA["cv"], "confirmation_id": None}
    assert resume["value_hash"] == value_hash(DOC_SHA["cv"]) and resume["transform_id"] == "identity"


def test_omit_and_ignore_never_produce_manifest_entries():
    bind = binding(omit=("subject:mobility.relocation",))
    plan = build(bind=bind).plan
    manifest = derive_manifest(plan, binding=bind, confirmation_ids=CONFIRMATIONS)
    keys = {e["page_field_key"] for p in manifest["pages"] for e in p["entries"]}
    assert "gh:relocate" not in keys and "gh:csrf" not in keys


@pytest.mark.parametrize("mutate,rule", [
    (lambda b, p, m: _entry(m, "gh:notice").update(value_hash="sha256:" + "9" * 64), "rule1"),
    (lambda b, p, m: _entry(m, "gh:notice")["source"].update(ref="ref_other"), "rule1"),
    (lambda b, p, m: _entry(m, "gh:notice").update(transform_id="phone_e164"), "rule1"),
    (lambda b, p, m: _entry(m, "gh:email")["source"].update(ref="clm_not_in_binding"), "rule2"),
    (lambda b, p, m: _entry(m, "gh:resume")["source"].update(ref="d" * 64), "rule3"),
    (lambda b, p, m: next(a for a in p["actions"] if a["page_field_key"] == "gh:resume")["document"].update(
        document_version_id="docv_other"), "rule3"),
    (lambda b, p, m: m["pages"][0]["entries"].append(copy.deepcopy(_entry(m, "gh:email"))), "rule5"),
    (lambda b, p, m: m["pages"][0]["entries"].remove(_entry(m, "gh:email")), "rule5"),
])
def test_each_rule_violation_is_reported(world, mutate, rule):
    bind, plan, manifest = copy.deepcopy(world)
    mutate(bind, plan, manifest)
    violations = _violations(bind, plan, manifest)
    assert violations and all(v.startswith(rule) for v in violations), violations


def test_rule4_a_write_to_a_field_bound_omit(world):
    bind, plan, manifest = copy.deepcopy(world)
    for f in bind["fields"]:
        if f["answer_key"] == "subject:employment.notice_period":
            f.update(disposition="OMIT", source_kind=None, source_ref=None, value_hash=None)
    plan["approval_binding_hash"] = binding_hash(bind)  # same approval, so only rule 4 is at stake
    violations = g4_violations(manifest, plan, bind, binding_hash_value=binding_hash(bind))
    assert violations and all(v.startswith("rule4") for v in violations), violations


def test_rule6_a_binding_other_than_the_runs(world):
    bind, plan, manifest = world
    assert g4_violations(manifest, plan, bind, binding_hash_value="sha256:" + "0" * 64) == [
        "rule6: the binding is not the run's approval binding"]
