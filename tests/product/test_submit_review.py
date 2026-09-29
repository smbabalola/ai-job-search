"""6E-A spec §7: submission-review.v1 — the exact reviewed version, hashed
over stable values only (E18): a fresh observation of an unchanged page
reproduces the hash; any material change alters it."""
from __future__ import annotations

import dataclasses

import pytest

from product.submit_review import ReviewInputs, build_review_snapshot, review_hash


def inputs(**overrides) -> ReviewInputs:
    base = dict(
        account_id="account_local", application_workspace_id="ws_1", identity_key="idk", employer_key="acme",
        canonical_url="http://127.0.0.1:8430/acme/jobs/123", origin="http://127.0.0.1:8430",
        adapter_id="greenhouse", adapter_version="greenhouse@2", tenant_key="acme", ats_job_id="123",
        certification_id="greenhouse@2/submit@1", certification_status="FIXTURE_CERTIFIED",
        fill_run_id="fr_1", plan_hash="sha256:" + "a" * 64, plan_confirmation_id="fpc_1",
        fill_result_hash="sha256:" + "b" * 64, final_observation_fingerprint="sha256:" + "c" * 64,
        ruleset_hash_total="sha256:" + "d" * 64,
        executor_instance_id="ex_1", browser_session_id="bs_1", execution_tab_id=7,
        observation_fingerprint="sha256:" + "c" * 64, structure_fingerprint="sha256:" + "e" * 64,
        submit_control_fingerprint="sha256:" + "f" * 64,
        approval_id="appr_1", approval_binding_hash="sha256:" + "1" * 64,
        answers=(("ans_2", "conf_2", "sha256:" + "2" * 64), ("ans_1", "conf_1", "sha256:" + "3" * 64)),
        documents=(("resume", "cv.pdf", "4" * 64), ("cover_letter", "cl.pdf", "5" * 64)),
        engine_version="autonomy-gate.v1", subject_policy_hash="sha256:" + "6" * 64, control_epoch=3,
    )
    base.update(overrides)
    return ReviewInputs(**base)


def test_equal_inputs_give_an_equal_hash_and_the_schema_is_stamped():
    snap = build_review_snapshot(inputs())
    assert snap["schema"] == "submission-review" and snap["schema_version"] == "v1"
    assert snap["policy"]["human_submit_contract"] == "human-submit.v1"
    assert snap["policy"]["submit_timing_version"] == "submit-timing.v1"
    assert review_hash(snap) == review_hash(build_review_snapshot(inputs()))
    assert review_hash(snap).startswith("sha256:")


@pytest.mark.parametrize("field", [f.name for f in dataclasses.fields(ReviewInputs)])
def test_changing_any_single_field_changes_the_hash(field):
    current = getattr(inputs(), field)
    if isinstance(current, int):
        changed = current + 1
    elif isinstance(current, tuple):
        changed = current[:-1]
    else:
        changed = current + "x"
    assert review_hash(build_review_snapshot(inputs(**{field: changed}))) != review_hash(build_review_snapshot(inputs()))


def test_answer_and_document_order_does_not_matter():
    a, b = inputs(), inputs(answers=tuple(reversed(inputs().answers)), documents=tuple(reversed(inputs().documents)))
    assert review_hash(build_review_snapshot(a)) == review_hash(build_review_snapshot(b))


def _keys(value, out):
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(value, list):
        for v in value:
            _keys(v, out)
    return out


def test_no_volatile_key_and_no_cleartext_key_in_the_snapshot():
    keys = _keys(build_review_snapshot(inputs()), set())
    assert not {"created_at", "observation_id", "requested_at", "expires_at", "now"} & keys
    assert not {"value", "rendered_value", "answer_value", "cleartext", "text"} & keys
