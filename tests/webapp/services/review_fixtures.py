"""Shared worlds for the Bundle 6D-A review tests. Later tasks APPEND helpers;
never rewrite existing ones."""
from __future__ import annotations

from product.autonomy_contract import EmployerKeyStrength, Reach
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401
from webapp.persistence.application_blockers import save_application_blocker
from webapp.persistence.artifacts import save_artifact
from webapp.persistence.autonomy_answers import approve_answer
from webapp.persistence.policy_decisions import save_policy_decision

ASSERT = {"kind": "USER_ASSERTION"}
EMPLOYER = "name:acme"
SEARCH_WS = "search_default"


def blocker(conn, ws, subject, question="Question?", *, artifact=None):
    """A governing blocker for a semantic subject (the production path). Pass
    `artifact` to add several blockers to one current governing artifact."""
    art = artifact or save_artifact(conn, workspace_id=ws, artifact_type="job_fit_result", payload={"subject": subject})
    decision = save_policy_decision(
        conn, workspace_id=ws, stage="fit", source_artifact_id=art["id"], review_item_type="gate_flag",
        subject_key=subject, domain_item_id=subject, outcome="REQUIRE_USER",
        policy_version="application-decision-policy.v0", policy_fingerprint="appdecpolicy_abc123", evidence_ids=[],
        supported_facts=[], recorded_gaps=[], reason_code="requires_user", reason="Requires user answer",
        confidence=None, blocking=True)
    return save_application_blocker(
        conn, workspace_id=ws, policy_decision_id=decision["id"], source_artifact_id=art["id"], stage="fit",
        blocker_type="gate_flag", subject_key=subject, question=question, resume_stage="fit",
        allowed_scopes=["APPLICATION_ONLY"], semantic_subject_key=subject)


def answer(conn, subject, value, *, reach=Reach.ACCOUNT, scope_id=None, basis=None, now=NOW, supersedes_id=None):
    return approve_answer(conn, account_id=ACCOUNT, subject=subject, value=value, reach=reach, scope_id=scope_id,
                          context={}, basis=basis or ASSERT, approved_by="u", now=now, supersedes_id=supersedes_id)


def claim(claim_id, field, value, *, placeholder=False, concept_id=None):
    return {"id": claim_id, "concept_id": concept_id or f"cpt_{claim_id}", "category": "identity", "field": field,
            "value": value, "placeholder": placeholder}


def profile(*claims, conflicts=()):
    return {"claims": list(claims), "conflicts": [{"concept_id": c} for c in conflicts]}


def fields(conn, ws, *, profile_payload=None, now=NOW):
    from webapp.services.review_fields import planned_fields
    return planned_fields(conn, account_id=ACCOUNT, application_workspace_id=ws,
                          profile_payload=profile_payload or profile(), employer_key=EMPLOYER,
                          employer_key_strength=EmployerKeyStrength.NORMALIZED_NAME, search_workspace_id=SEARCH_WS,
                          now=now)


def by_key(planned):
    return {f.answer_key: f for f in planned[0]}


def warning_types(planned):
    return sorted(w.key.split(":", 1)[0] for w in planned[1])
