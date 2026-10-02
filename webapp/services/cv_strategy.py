"""CV strategy and per-application CV resolution (Bundle 7 spec §14.2-§14.4).

At prepare (after job understanding) ``resolve_for_workspace`` classifies the
job's family, looks up the rule and records why a CV was chosen:
FIXED_VERSION → that version; LATEST_VERSION → the item's newest visible
version; TAILOR_FROM → TAILOR_REQUESTED when the plan includes ai.cv_tailor
(fulfilled after the intelligence stage by ``fulfil_tailoring`` under a
cv.tailor reservation), else the item's latest with
``rule_json.fallback = TAILOR_NOT_IN_PLAN``; no CV → NEEDS_USER_CHOICE, which
the review shows as a blocking required choice. The chosen version becomes
the workspace's CV selection through the existing selection path; a user
override appends a resolution with ``overridden_by_user = 1``. Resolutions
are append-only, so the history of why each CV was used is kept.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from product.cv_strategy import CvStrategyInvalid, ItemView, default_strategy, normalize_cv_strategy, resolve_rule
from product.cv_templates import CV_TEMPLATES
from product.job_families import JobFamiliesInvalid, classify, normalize_job_families
from product.review_contract import ReviewWarning, WarningLevel, warning_key
from webapp.persistence import account_documents as docs
from webapp.persistence import cv_library as library_rows
from webapp.persistence import dbapi
from webapp.persistence.artifacts import get_current_artifact
from webapp.persistence.workspaces import get_workspace

__all__ = [
    "CvStrategyInvalid", "JobFamiliesInvalid", "choose_cv", "current_cv_strategy", "current_job_families",
    "cv_choice_warnings", "cv_used_label", "fulfil_tailoring", "resolve_for_workspace", "save_cv_strategy",
    "save_job_families",
]

logger = logging.getLogger("webapp.cv_strategy")

ORIGIN_WORDS = {"USER_UPLOAD": "uploaded", "IMPORTED_LEGACY": "imported", "AI_GENERATED": "generated",
                "AI_TAILORED": "tailored"}


# ---- documents ------------------------------------------------------------------------------

def current_job_families(conn: dbapi.Connection, account_id: str) -> tuple[dict, str | None]:
    row = docs.current_account_document(conn, account_id=account_id, doc_type="job-families")
    return (row["doc"], row["doc_hash"]) if row else ({"families": []}, None)


def current_cv_strategy(conn: dbapi.Connection, account_id: str) -> tuple[dict, str | None]:
    row = docs.current_account_document(conn, account_id=account_id, doc_type="cv-strategy")
    return (row["doc"], row["doc_hash"]) if row else (default_strategy(), None)


def _item_views(conn: dbapi.Connection, account_id: str) -> dict[str, ItemView]:
    views = {}
    for item in library_rows.list_items(conn, account_id=account_id):
        versions = library_rows.list_versions(conn, account_id=account_id, item_id=item["id"], include_hidden=True)
        views[item["id"]] = ItemView(item["id"], item["status"], tuple(v["id"] for v in versions))
    return views


def save_job_families(conn: dbapi.Connection, scope: Any, doc: Any, *, now: datetime) -> dict[str, Any]:
    """No commit. Raises JobFamiliesInvalid."""
    normalized = normalize_job_families(doc)
    return docs.save_account_document(conn, account_id=scope.account_id, doc_type="job-families", doc=normalized,
                                      created_by=getattr(scope, "user_id", None) or scope.account_id, now=now)


def save_cv_strategy(conn: dbapi.Connection, scope: Any, doc: Any, *, now: datetime) -> dict[str, Any]:
    """No commit. Raises CvStrategyInvalid (a foreign or archived CV, an unknown template, no default)."""
    normalized = normalize_cv_strategy(doc, account_items=_item_views(conn, scope.account_id), templates=CV_TEMPLATES)
    return docs.save_account_document(conn, account_id=scope.account_id, doc_type="cv-strategy", doc=normalized,
                                      created_by=getattr(scope, "user_id", None) or scope.account_id, now=now)


# ---- resolution ---------------------------------------------------------------------------

def _job_context(conn: dbapi.Connection, account_id: str, workspace_id: str) -> tuple[str, str | None]:
    workspace = get_workspace(conn, workspace_id, account_id=account_id)
    if workspace is None:
        raise LookupError("workspace not found")
    understanding = get_current_artifact(conn, workspace_id, "job_understanding_result")
    posting = get_current_artifact(conn, workspace_id, "job_posting_snapshot")
    payload = (understanding or {}).get("payload") or {}
    title = payload.get("title") or ((posting or {}).get("payload") or {}).get("title") or workspace["title"]
    seniority = payload.get("seniority") if isinstance(payload.get("seniority"), str) else None
    return str(title), seniority


def _select(conn: dbapi.Connection, scope: Any, workspace_id: str, document_version_id: str) -> None:
    from webapp.persistence.application_documents import get_selection
    from webapp.services.application_documents import apply_selection
    workspace = get_workspace(conn, workspace_id, account_id=scope.account_id)
    if workspace is None or workspace.get("workflow_status") not in (None, "drafted"):
        return  # a closed application's documents never change; the resolution is still recorded
    current = get_selection(conn, workspace_id, "cv", account_id=scope.account_id)
    if current is not None and current["document_version_id"] == document_version_id:
        return
    apply_selection(conn, workspace_id, kind="cv", document_version_id=document_version_id,
                    expected_revision=current["revision"] if current else 0, account_id=scope.account_id)


def _tailor_entitled(conn: dbapi.Connection, scope: Any, metering: Any, now: datetime) -> bool:
    if metering is None or not metering.enforced:
        return True  # the local operator account is unmetered
    return metering.gate.entitlements(conn, scope, now=now).has("ai.cv_tailor")


def resolve_for_workspace(conn: dbapi.Connection, scope: Any, *, workspace_id: str, metering: Any,
                          now: datetime) -> dict[str, Any]:
    """No commit. Writes one application_cv_resolutions row (and the CV selection when resolved)."""
    families, families_hash = current_job_families(conn, scope.account_id)
    strategy, strategy_hash = current_cv_strategy(conn, scope.account_id)
    title, seniority = _job_context(conn, scope.account_id, workspace_id)
    match = classify(families, title=title, seniority=seniority)
    rule = resolve_rule(strategy, match.family_id)
    record = dict(account_id=scope.account_id, workspace_id=workspace_id, job_families_hash=families_hash,
                  cv_strategy_hash=strategy_hash, family_id=match.family_id,
                  family_match={"family_id": match.family_id, "matched": match.matched, "reason": match.reason,
                                "title": title, "seniority": seniority}, now=now)
    version = None
    if rule["mode"] == "FIXED_VERSION":
        version = library_rows.get_version(conn, account_id=scope.account_id, version_id=rule["version_id"])
    elif rule["item_id"] is not None:
        item = library_rows.get_item(conn, account_id=scope.account_id, item_id=rule["item_id"])
        if item is not None and item["status"] == "ACTIVE":
            version = library_rows.latest_visible_version(conn, account_id=scope.account_id, item_id=item["id"])
            if version is not None and rule["mode"] == "TAILOR_FROM":
                if _tailor_entitled(conn, scope, metering, now):
                    return docs.insert_resolution(conn, **record, rule=rule, outcome="TAILOR_REQUESTED",
                                                  item_id=item["id"], version_id=None, overridden_by_user=False)
                rule = {**rule, "fallback": "TAILOR_NOT_IN_PLAN"}
    if version is None:
        return docs.insert_resolution(conn, **record, rule=rule, outcome="NEEDS_USER_CHOICE",
                                      item_id=rule.get("item_id"), version_id=None, overridden_by_user=False)
    _select(conn, scope, workspace_id, version["document_version_id"])
    return docs.insert_resolution(conn, **record, rule=rule, outcome="RESOLVED_VERSION", item_id=version["item_id"],
                                  version_id=version["id"], overridden_by_user=False)


Generator = Callable[..., str]  # (conn, scope, *, workspace_id, base_version, template_id, documents_root) -> doc id


def fulfil_tailoring(conn: dbapi.Connection, scope: Any, *, workspace_id: str, metering: Any, generator: Generator,
                     documents_root: Any, now: datetime) -> dict[str, Any] | None:
    """After the intelligence stage: a pending TAILOR_REQUESTED becomes an
    AI_TAILORED library version (library_visible 0, parent = the item's
    latest) under a cv.tailor reservation. An exhausted allowance falls back
    to the latest version (rule_json.fallback = TAILOR_ALLOWANCE_EXHAUSTED)."""
    from webapp.services.usage import AllowanceExhausted
    pending = docs.latest_resolution(conn, workspace_id)
    if pending is None or pending["outcome"] != "TAILOR_REQUESTED":
        return None
    import json
    rule = json.loads(pending["rule_json"])
    base = library_rows.latest_visible_version(conn, account_id=scope.account_id, item_id=rule["item_id"])
    carried = dict(account_id=scope.account_id, workspace_id=workspace_id,
                   job_families_hash=pending["job_families_hash"], cv_strategy_hash=pending["cv_strategy_hash"],
                   family_id=pending["family_id"], family_match=json.loads(pending["family_match_json"]))

    def work() -> dict[str, Any]:
        document_id = generator(conn, scope, workspace_id=workspace_id, base_version=base,
                                template_id=rule["template_id"], documents_root=documents_root)
        tailored = library_rows.insert_version(
            conn, account_id=scope.account_id, item_id=rule["item_id"], document_version_id=document_id,
            origin="AI_TAILORED", parent_version_id=base["id"], template_id=rule["template_id"],
            library_visible=False, note=f"Tailored for {workspace_id}", created_by="cv_strategy", now=now)
        _select(conn, scope, workspace_id, document_id)
        return docs.insert_resolution(conn, **carried, now=now, rule={**rule, "tailored": True},
                                      outcome="RESOLVED_VERSION", item_id=rule["item_id"], version_id=tailored["id"],
                                      overridden_by_user=False)

    if metering is None or not metering.enforced:
        result = work()
        conn.commit()
        return result
    try:
        return metering.metered(conn, scope, feature="ai.cv_tailor", allowance="cv.tailor",
                                subject_type="workspace", subject_id=workspace_id,
                                key=lambda window_key: f"tailor:{workspace_id}:{window_key}",
                                action=f"tailor:{workspace_id}", work=work)
    except AllowanceExhausted:
        if conn.in_transaction:
            conn.rollback()
        _select(conn, scope, workspace_id, base["document_version_id"])
        result = docs.insert_resolution(conn, **carried, now=now, rule={**rule, "fallback": "TAILOR_ALLOWANCE_EXHAUSTED"},
                                        outcome="RESOLVED_VERSION", item_id=rule["item_id"], version_id=base["id"],
                                        overridden_by_user=False)
        conn.commit()
        return result


def choose_cv(conn: dbapi.Connection, scope: Any, *, workspace_id: str, version_id: str, actor: str,
              now: datetime) -> dict[str, Any]:
    """The user's choice (or override) for this application. No commit."""
    from webapp.persistence import review_approval as ra
    version = library_rows.get_version(conn, account_id=scope.account_id, version_id=version_id)
    if version is None:
        raise LookupError("CV version not found")
    previous = docs.latest_resolution(conn, workspace_id)
    _select(conn, scope, workspace_id, version["document_version_id"])
    ra.record_event(conn, account_id=scope.account_id, application_workspace_id=workspace_id,
                    event="SELECTION_CHANGED", binding_hash=None,
                    detail={"kind": "cv", "document_version_id": version["document_version_id"],
                            "cv_library_version_id": version_id, "source": "cv_choice"}, actor=actor, now=now)
    import json
    return docs.insert_resolution(
        conn, account_id=scope.account_id, workspace_id=workspace_id,
        job_families_hash=previous["job_families_hash"] if previous else None,
        cv_strategy_hash=previous["cv_strategy_hash"] if previous else None,
        family_id=previous["family_id"] if previous else "UNKNOWN",
        family_match=json.loads(previous["family_match_json"]) if previous else {},
        rule={"mode": "USER_CHOICE", "version_id": version_id}, outcome="RESOLVED_VERSION",
        item_id=version["item_id"], version_id=version_id, overridden_by_user=True, now=now)


def cv_choice_warnings(conn: dbapi.Connection, *, account_id: str, workspace_id: str) -> list[ReviewWarning]:
    """A pending NEEDS_USER_CHOICE is a blocking review item (§14.3 step 5)."""
    latest = docs.latest_resolution(conn, workspace_id)
    if latest is None or latest["outcome"] != "NEEDS_USER_CHOICE":
        return []
    return [ReviewWarning(warning_key("cv_choice_required", "documents", {"resolution_id": latest["id"]}),
                          WarningLevel.BLOCKING, "Choose which CV to use for this application")]


def cv_used_label(conn: dbapi.Connection, *, account_id: str, workspace_id: str) -> str | None:
    """"<item title> v<n> (<origin>)" for the CV this application uses (§14.4)."""
    latest = docs.latest_resolution(conn, workspace_id)
    if latest is None or latest["version_id"] is None:
        return None
    version = library_rows.get_version(conn, account_id=account_id, version_id=latest["version_id"])
    item = library_rows.get_item(conn, account_id=account_id, item_id=version["item_id"]) if version else None
    if version is None or item is None:
        return None
    return f"{item['title']} v{version['version_no']} ({ORIGIN_WORDS.get(version['origin'], version['origin'])})"
