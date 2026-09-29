"""Bundle 6E-A submission-review.v1 (spec §7). Pure.

The snapshot is the exact version a human authorizes. It is built only from
stable values -- fingerprints, hashes and durable identities, never an
observation row id or a timestamp (E18) -- so a fresh observation of an
unchanged page reproduces review_hash exactly, and any material change
(page content, answers, documents, approval, plan, target, context, policy)
changes it."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from product.autonomy_contract import canonical_hash
from product.submit_constants import HUMAN_SUBMIT_CONTRACT, SUBMIT_TIMING_VERSION

SCHEMA = "submission-review"
SCHEMA_VERSION = "v1"


@dataclass(frozen=True)
class ReviewInputs:
    account_id: str
    application_workspace_id: str
    identity_key: str
    employer_key: str
    canonical_url: str
    origin: str
    adapter_id: str
    adapter_version: str
    tenant_key: str
    ats_job_id: str
    certification_id: str
    certification_status: str
    fill_run_id: str
    plan_hash: str
    plan_confirmation_id: str
    fill_result_hash: str
    final_observation_fingerprint: str
    ruleset_hash_total: str
    executor_instance_id: str
    browser_session_id: str
    execution_tab_id: int
    observation_fingerprint: str
    structure_fingerprint: str
    submit_control_fingerprint: str
    approval_id: str
    approval_binding_hash: str
    answers: tuple[tuple[str, str, str], ...]  # (approved_answer_id, confirmation_id, rendered_value_hash)
    documents: tuple[tuple[str, str, str], ...]  # (document_kind, filename, sha256)
    engine_version: str
    subject_policy_hash: str
    control_epoch: int


def build_review_snapshot(i: ReviewInputs) -> dict[str, Any]:
    return {
        "schema": SCHEMA, "schema_version": SCHEMA_VERSION,
        "application": {"account_id": i.account_id, "application_workspace_id": i.application_workspace_id,
                        "identity_key": i.identity_key, "employer_key": i.employer_key},
        "target": {"canonical_url": i.canonical_url, "origin": i.origin, "adapter_id": i.adapter_id,
                   "adapter_version": i.adapter_version, "tenant_key": i.tenant_key, "ats_job_id": i.ats_job_id},
        "certification": {"certification_id": i.certification_id, "status": i.certification_status},
        "fill": {"fill_run_id": i.fill_run_id, "plan_hash": i.plan_hash,
                 "plan_confirmation_id": i.plan_confirmation_id, "fill_result_hash": i.fill_result_hash,
                 "final_observation_fingerprint": i.final_observation_fingerprint,
                 "ruleset_hash_total": i.ruleset_hash_total},
        "context": {"executor_instance_id": i.executor_instance_id, "browser_session_id": i.browser_session_id,
                    "execution_tab_id": i.execution_tab_id},
        "observation": {"observation_fingerprint": i.observation_fingerprint,
                        "structure_fingerprint": i.structure_fingerprint},
        "submit_control": {"control_fingerprint": i.submit_control_fingerprint},
        "approval": {"approval_id": i.approval_id, "binding_hash": i.approval_binding_hash},
        "answers": sorted([list(a) for a in i.answers]),
        "documents": sorted([list(d) for d in i.documents]),
        "policy": {"engine_version": i.engine_version, "human_submit_contract": HUMAN_SUBMIT_CONTRACT,
                   "subject_policy_hash": i.subject_policy_hash, "control_epoch": i.control_epoch,
                   "submit_timing_version": SUBMIT_TIMING_VERSION},
    }


def review_hash(snapshot: dict[str, Any]) -> str:
    return canonical_hash(SCHEMA, SCHEMA_VERSION, snapshot)
