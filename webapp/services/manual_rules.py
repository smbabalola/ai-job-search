"""Rules in manual mode (Bundle 7 spec §16.3): the standing policy evaluated as
advisories on every plan. A BLOCK needs the user's acknowledgement for this
application before approval (disposition PROCEED, bound to the rule hash and
the observed values); other effects are displayed only. Rules never grant.

Approvals bind their snapshot: a rule added or changed after an approval
never invalidates it. The fill-start check therefore refuses only a BLOCK
whose rule already existed when the approval was made and that was never
acknowledged (defense in depth for approvals that bypassed the check)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from product.standing_policy import Advisory, evaluate_manual, policy_hash, rule_hash, upgrade_policy
from webapp.persistence import dbapi
from webapp.persistence.autonomy_answers import current_rule_acknowledgements, record_rule_acknowledgement
from webapp.persistence.autonomy_authority import current_policy

__all__ = ["acknowledge", "advisories", "fill_start_refusal", "unacknowledged_blocks"]


def _policy(conn: dbapi.Connection, account_id: str) -> dict[str, Any] | None:
    policy = current_policy(conn, account_id)
    return None if policy is None else upgrade_policy(policy["doc"])


def job_attributes(conn: dbapi.Connection, *, account_id: str, workspace_id: str,
                   policy_doc: dict[str, Any] | None) -> dict[str, Any]:
    """The same attributes the automated gate reads (6B v1 + the v2 job attributes)."""
    from product.autonomy_contract import UNKNOWN
    from product.standing_policy import normalize_employment_type
    from webapp.persistence.application_identity import get_search_workspace_for_application
    from webapp.persistence.artifacts import get_current_artifact
    from webapp.persistence.workspaces import get_workspace
    from webapp.persistence.autonomy_ledger import workspace_identity
    from webapp.services.autonomy_context import _score, employer_identity, v2_attributes
    posting = (get_current_artifact(conn, workspace_id, "job_posting_snapshot") or {}).get("payload") or {}
    fit = (get_current_artifact(conn, workspace_id, "job_fit_result") or {}).get("payload") or {}
    workspace = get_workspace(conn, workspace_id, account_id=account_id) or {}
    _identity_key, identity_strength, _conflict = workspace_identity(conn, workspace_id)
    employer_key, _strength = employer_identity(posting, workspace)
    verdict = fit.get("verdict")
    return {
        "fit.overall_score": _score(fit.get("overall_score")),
        "fit.verdict": verdict.get("id") if isinstance(verdict, dict) and verdict.get("id") else UNKNOWN,
        "job.employment_type": normalize_employment_type(posting.get("employment_type")),
        "job.location": posting.get("location") or UNKNOWN,
        "job.title": posting.get("title") or workspace.get("title") or UNKNOWN,
        "company.key": employer_key or UNKNOWN,
        "workspace.id": get_search_workspace_for_application(conn, workspace_id) or UNKNOWN,
        "identity.strength": identity_strength.value,
        **v2_attributes(conn, account_id=account_id, workspace_id=workspace_id, posting=posting,
                        workspace=workspace, policy_doc=policy_doc),
    }


def advisories(conn: dbapi.Connection, *, account_id: str, workspace_id: str) -> list[Advisory]:
    doc = _policy(conn, account_id)
    if doc is None:
        return []
    return evaluate_manual(doc, job_attributes(conn, account_id=account_id, workspace_id=workspace_id,
                                               policy_doc=doc))


def _acknowledged(conn: dbapi.Connection, workspace_id: str, advisory: Advisory) -> bool:
    for row in current_rule_acknowledgements(conn, workspace_id):
        if (row["rule_id"] == advisory.rule_id and row["disposition"] == "PROCEED"
                and row["rule_hash"] == advisory.rule_hash
                and row["observed_fingerprint"] == advisory.observed_fingerprint):
            return True
    return False


def unacknowledged_blocks(conn: dbapi.Connection, *, account_id: str, workspace_id: str) -> list[Advisory]:
    return [a for a in advisories(conn, account_id=account_id, workspace_id=workspace_id)
            if a.effect == "BLOCK" and not _acknowledged(conn, workspace_id, a)]


def acknowledge(conn: dbapi.Connection, *, account_id: str, workspace_id: str, rule_id: str, actor: str,
                now: datetime) -> dict[str, Any]:
    """The user proceeds with this application despite the rule (no commit)."""
    doc = _policy(conn, account_id)
    match = next((a for a in advisories(conn, account_id=account_id, workspace_id=workspace_id)
                  if a.rule_id == rule_id), None)
    if doc is None or match is None:
        raise LookupError("no such rule advisory for this application")
    return record_rule_acknowledgement(
        conn, account_id=account_id, application_workspace_id=workspace_id, rule_id=rule_id,
        rule_hash=match.rule_hash, observed_fingerprint=match.observed_fingerprint,
        policy_version_hash=policy_hash(doc), disposition="PROCEED", actor=actor, now=now, commit=False)


def fill_start_refusal(conn: dbapi.Connection, *, account_id: str, workspace_id: str, now: datetime) -> str | None:
    from webapp.persistence import review_approval as ra
    approval = ra.latest_approval(conn, workspace_id)
    if approval is None:
        return None
    blocks = unacknowledged_blocks(conn, account_id=account_id, workspace_id=workspace_id)
    if not blocks:
        return None
    row = conn.execute("SELECT policy_json FROM standing_policy_versions WHERE account_id = ? AND created_at <= ? "
                       "ORDER BY seq DESC LIMIT 1", (account_id, approval["created_at"])).fetchone()
    if row is None:
        return None
    import json
    existed = {rule_hash(r) for r in json.loads(row[0]).get("rules", [])}
    return "rule_acknowledgement_required" if any(b.rule_hash in existed for b in blocks) else None


def rule_conflict_items(conn: dbapi.Connection, scope: Any, *, settings: Any, now: datetime) -> list[Any]:
    """The inbox source: open applications with an unacknowledged BLOCK rule conflict."""
    from webapp.services.inbox import ActionItem
    if current_policy(conn, scope.account_id) is None:
        return []
    rows = conn.execute("SELECT id FROM workspaces WHERE account_id = ? AND kind = 'job' "
                        "AND (workflow_status IS NULL OR workflow_status = 'drafted') ORDER BY updated_at DESC LIMIT 200",
                        (scope.account_id,)).fetchall()
    items = []
    for row in rows:
        if unacknowledged_blocks(conn, account_id=scope.account_id, workspace_id=row[0]):
            items.append(ActionItem("rule_conflict", "rule_conflict", f"/workspaces/{row[0]}/review", "workspace",
                                    row[0]))
    return items


def _register_inbox_source() -> None:
    from webapp.services import inbox
    if rule_conflict_items not in inbox.SOURCES:
        inbox.SOURCES.append(rule_conflict_items)


_register_inbox_source()
