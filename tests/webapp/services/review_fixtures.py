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


# ---- Task 6: a real v2 chain (generate -> select -> user v2 confirm) --------------

import dataclasses  # noqa: E402

import pytest  # noqa: E402

V2_ACCOUNT = "account_local"


class V2World:
    def __init__(self, conn, ws, settings, generated):
        self.conn, self.ws, self.settings, self.generated = conn, ws, settings, generated

    def state(self, now=NOW, **settings_overrides):
        from webapp.services.review_application import review_state
        s = dataclasses.replace(self.settings, **settings_overrides) if settings_overrides else self.settings
        return review_state(self.conn, settings=s, account_id=V2_ACCOUNT, application_workspace_id=self.ws, now=now)

    def reviewable(self, now=NOW):
        from webapp.services.review_application import build_reviewable
        return build_reviewable(self.conn, settings=self.settings, account_id=V2_ACCOUNT,
                                application_workspace_id=self.ws, now=now)

    def set_target(self, url="https://jobs.example.test/acme/123", provenance="user_supplied"):
        """A new current job posting snapshot carrying an apply target URL."""
        from webapp.persistence.artifacts import get_current_artifact, save_artifact
        current = get_current_artifact(self.conn, self.ws, "job_posting_snapshot")
        posting = dict(current["payload"])
        posting["source_url"] = url
        posting["metadata"] = {"ingestion": {"source_url_provenance": provenance}}
        # Same content id: only the target metadata changes, the upstream chain stays current.
        save_artifact(self.conn, workspace_id=self.ws, artifact_type="job_posting_snapshot", payload=posting,
                      content_id=current["content_id"])

    def selection(self, kind):
        from webapp.persistence.application_documents import get_selection
        return get_selection(self.conn, self.ws, kind, account_id=V2_ACCOUNT)


@pytest.fixture
def v2_chain(tmp_path):
    from tests.webapp.services.test_application_pack import _seed_completion_ready, _workspace
    from webapp.config import Settings
    from webapp.services.application_documents import generate_application_documents, select_application_document
    from webapp.services.application_pack import confirm_application_pack
    conn, ws = _workspace(tmp_path)
    _seed_completion_ready(conn, ws)
    settings = Settings(db_path=tmp_path / "jobsearch.sqlite3", documents_root=tmp_path / "docs",
                        extensions_dir=tmp_path / "extensions", cv_quality_v2_enabled=True)
    generated = generate_application_documents(conn, ws, documents_root=settings.documents_root,
                                               extensions_dir=settings.extensions_dir, account_id=V2_ACCOUNT)
    revisions = {}
    for row in generated["documents"]:
        selection = select_application_document(conn, ws, kind=row["document_kind"], document_version_id=row["id"],
                                                expected_revision=0, account_id=V2_ACCOUNT)
        revisions[row["document_kind"]] = selection["revision"]
    confirm_application_pack(conn, ws, effective_date="2026-09-24", documents_root=settings.documents_root,
                             account_id=V2_ACCOUNT, document_selection_revisions=revisions)
    world = V2World(conn, ws, settings, generated)
    world.set_target()
    yield world
    conn.close()


def docx_bytes(text):
    from io import BytesIO
    from docx import Document
    stream = BytesIO()
    document = Document()
    document.add_paragraph(text)
    document.save(stream)
    return stream.getvalue()


def table_counts(conn):
    """Row counts of every table (a DB-diff for write-contract tests)."""
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                        "AND name NOT LIKE 'sqlite_%'")]
    return {n: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}


def diff_counts(before, after):
    return {k: after[k] - before.get(k, 0) for k in after if after[k] != before.get(k, 0)}


# ---- Task 8: bring the v2 world to an approvable state, and approve -----------------

def _make_approvable(self, now=NOW):
    """Explicit user choices, recorded like the UI would (persistence level):
    leave undecided optional fields blank and acknowledge ATTENTION warnings."""
    from product.review_contract import WarningLevel
    from webapp.persistence import review_approval as ra
    r = self.reviewable(now=now)
    for f in r.fields:
        if not f.required and f.disposition is None:
            ra.set_disposition(self.conn, account_id=V2_ACCOUNT, application_workspace_id=self.ws,
                               answer_key=f.answer_key, disposition="OMIT", actor="u", now=now)
    for w in self.reviewable(now=now).warnings:
        if w.level is WarningLevel.ATTENTION and not w.acknowledged:
            ra.record_event(self.conn, account_id=V2_ACCOUNT, application_workspace_id=self.ws,
                            event="WARNING_ACKNOWLEDGED", binding_hash=None, detail={"warning_key": w.key},
                            actor="u", now=now)
    self.conn.commit()
    return self.state(now=now)


def _approve(self, displayed=None, now=NOW, batch_id=None):
    from webapp.services.review_approval import approve
    return approve(self.conn, settings=self.settings, account_id=V2_ACCOUNT, application_workspace_id=self.ws,
                   displayed_binding_hash=displayed if displayed is not None else self.state(now=now).binding_hash,
                   actor="u", now=now, batch_id=batch_id)


V2World.make_approvable = _make_approvable
V2World.approve = _approve


# ---- final-review corrections: a profile contact claim and an optional question ----

def add_contact_claim(world, field="location", value="London"):
    """A new current profile snapshot carrying one contact claim."""
    from webapp.persistence.artifacts import get_current_artifact, save_artifact
    from webapp.persistence.workspaces import get_profile_workspace_id
    profile_ws = get_profile_workspace_id(world.conn, V2_ACCOUNT)
    current = get_current_artifact(world.conn, profile_ws, "profile_snapshot")
    payload = dict(current["payload"])
    payload["claims"] = [*payload.get("claims", []), {"id": f"clm_contact_{field}", "concept_id": f"cpt_contact_{field}",
                                                      "category": "identity", "field": field, "value": value}]
    save_artifact(world.conn, workspace_id=profile_ws, artifact_type="profile_snapshot", payload=payload)
    world.conn.commit()


def open_delta(world, *, kind="NEW_QUESTION", subject="employment.availability_start", required=False,
               field_key="q1"):
    from webapp.services.review_approval import open_review_delta
    return open_review_delta(world.conn, account_id=V2_ACCOUNT, application_workspace_id=world.ws, kind=kind,
                             answer_key=None, subject=subject, required=required, question=f"{subject}?",
                             observed={"field_key": field_key}, source="FILL_SESSION:s1", now=NOW)
