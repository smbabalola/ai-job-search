"""Tenant table registry (Bundle 7 spec H8, §10.5).

Every table is classified by how its rows reach their owning account, what
account purge does to them, and whether data export includes them. The
registry drives purge (§20.4), export (§20.5), the local->hosted import
(§23.3) and the isolation harness; a test fails if any table in either dialect
is missing here, so every new table must be classified when it is created.

``owner`` is either the name of the column holding the account id, a
``(parent_table, column)`` pair meaning ``column`` references
``parent_table.id`` (or ``(parent_table, column, parent_key)`` when the
parent is keyed by another column), ``("USER", column)`` for rows keyed by a
user (reached through that user's account membership), or ``"GLOBAL"`` for
rows owned by no account.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Owner = "str | tuple[str, str]"


@dataclass(frozen=True)
class TableSpec:
    owner: Any
    purge: Literal["DELETE", "PSEUDONYMIZE", "RETAIN", "GLOBAL"] = "DELETE"
    retain_class: str | None = None
    pseudonymize: tuple[str, ...] = ()
    export: bool = True


A = TableSpec("account_id")
_ws = lambda column: TableSpec(("workspaces", column))  # noqa: E731
_sws = lambda column: TableSpec(("search_workspaces", column))  # noqa: E731
_run = TableSpec(("fill_runs", "fill_run_id"))
_session = TableSpec(("handoff_sessions", "handoff_session_id"))
_attempt = TableSpec(("submission_attempts", "attempt_id"))
_internal = lambda owner: TableSpec(owner, export=False)  # noqa: E731  (secrets, leases, queues)

TENANT_TABLES: dict[str, TableSpec] = {
    # identity and ownership
    "accounts": TableSpec("id", purge="PSEUDONYMIZE", pseudonymize=("display_name",)),
    "account_profiles": A,
    "workspaces": A,
    "search_workspaces": A,
    "profile_source_settings": A,
    "profile_source_entries": A,
    "profile_source_revisions": A,
    "onboarding_progress": A,
    # search preferences and discovery
    "user_profile_versions": A,
    "search_workspace_user_profiles": _sws("search_workspace_id"),
    "search_workspace_user_profile_history": _sws("search_workspace_id"),
    "current_user_profile": TableSpec(("user_profile_versions", "version_id"), purge="RETAIN",
                                      retain_class="LOCAL_ONLY", export=False),
    "discovery_runs": _sws("search_workspace_id"),
    "discovery_candidates": _sws("search_workspace_id"),
    "discovery_candidate_keys": _sws("search_workspace_id"),
    "discovery_occurrences": _sws("search_workspace_id"),
    "discovery_fit_results": _sws("search_workspace_id"),
    "current_discovery_fits": _sws("search_workspace_id"),
    # job workspaces, artifacts, review
    "artifacts": _ws("workspace_id"),
    "current_artifacts": _ws("workspace_id"),
    "dependency_fingerprints": TableSpec(("artifacts", "artifact_id")),
    "provider_audits": _ws("workspace_id"),
    "policy_decisions": _ws("workspace_id"),
    "review_decisions": _ws("workspace_id"),
    "application_blockers": _ws("workspace_id"),
    "blocker_resolutions": _ws("workspace_id"),
    "proposed_answers": TableSpec(("application_blockers", "blocker_id")),
    "workflow_events": _ws("workspace_id"),
    "application_workspace_job_identities": _ws("application_workspace_id"),
    "application_job_identity_conflicts": _ws("application_workspace_id"),
    "application_workspace_origins": _ws("application_workspace_id"),
    "apply_target_confirmations": _ws("application_workspace_id"),
    # documents
    "application_document_versions": A,
    "application_document_selections": A,
    "reusable_application_documents": A,
    # review approval (6D-A)
    "application_approvals": A,
    "application_review_events": A,
    "review_deltas": A,
    "application_field_dispositions": A,
    "delta_classification_proposals": A,
    "delta_classification_confirmations": A,
    # handoff and extension
    "handoff_sessions": A,
    "handoff_events": _session,
    "handoff_session_tokens": _internal(("handoff_sessions", "handoff_session_id")),
    "submission_confirmations": _session,
    "extension_credentials": _internal("account_id"),
    "pairing_secrets": _internal("account_id"),
    # fill (6D-B)
    "fill_observations": A,
    "fill_plans": A,
    "fill_plan_mapping_choices": A,
    "fill_plan_confirmations": A,
    "fill_runs": A,
    "fill_run_events": _run,
    "fill_run_grant_bindings": _run,
    "fill_run_leases": _internal(("fill_runs", "fill_run_id")),
    "fill_action_events": _run,
    "fill_quarantine_events": _run,
    "fill_detection_events": _run,
    "fill_results": _run,
    "active_fill_runs": _internal(("workspaces", "application_workspace_id")),
    # human submit (6E-A)
    "human_submit_authorizations": A,
    "submit_reobservation_requests": A,
    "submit_observations": A,
    "submit_events": _attempt,
    "submission_results": _attempt,
    # autonomy (6B/6C)
    "autonomy_authorizations": A,
    "autonomy_kill_switch": A,
    "autonomy_control_events": A,
    "autonomy_runs": A,
    "autonomy_run_ends": TableSpec(("autonomy_runs", "run_id", "run_id")),
    "standing_policy_versions": A,
    "approved_answers": A,
    "answer_confirmations": TableSpec(("approved_answers", "approved_answer_id")),
    "rule_acknowledgements": A,
    "autonomy_decisions": A,
    "autonomy_grants": A,
    "autonomy_grant_events": TableSpec(("autonomy_grants", "grant_id")),
    "intent_overrides": TableSpec(("submission_intents", "intent_id")),
    "submission_intents": A,
    "submission_attempts": _ws("application_workspace_id"),
    "submission_attempt_events": _attempt,
    "dry_run_submission_cases": _ws("application_workspace_id"),
    "dry_run_case_agreements": TableSpec(("dry_run_submission_cases", "case_id")),
    "limit_reservations": A,
    "autonomy_queue_items": _internal("account_id"),
    "autonomy_enrolments": A,
    "autonomy_prepare_steps": TableSpec(("autonomy_decisions", "authorization_decision_id")),
    "autonomy_retry_requests": A,
    "autonomy_notification_events": A,
    "autonomy_review_latches": _ws("application_workspace_id"),
    "autonomy_candidate_queue": _internal("account_id"),
    "autonomy_candidate_screenings": A,
    "autonomy_candidate_exceptions": A,
    "autonomy_candidate_exception_resolutions": TableSpec(("autonomy_candidate_exceptions", "exception_id")),
    "autonomy_candidate_promotions": _sws("search_workspace_id"),
    # identity (Bundle 7, 023_identity)
    "users": TableSpec(("USER", "id"), purge="PSEUDONYMIZE",
                       pseudonymize=("email_normalized", "email_display", "display_name")),
    "user_identities": _internal(("USER", "user_id")),
    "account_memberships": A,
    "web_sessions": _internal(("USER", "user_id")),
    "email_tokens": _internal(("USER", "user_id")),
    "staff_totp": _internal(("USER", "user_id")),
    "platform_role_assignments": TableSpec(("USER", "user_id"), purge="RETAIN", retain_class="SECURITY_AUDIT",
                                           export=False),
    "legal_acceptances": TableSpec(("USER", "user_id"), purge="RETAIN", retain_class="CONSENT_PROOF"),
    "legal_documents": TableSpec("GLOBAL", purge="GLOBAL", export=False),
    "rate_limit_buckets": TableSpec("GLOBAL", purge="GLOBAL", export=False),
    # extension devices (Bundle 7, 025_extension_devices)
    "extension_devices": A,
    "extension_refresh_tokens": _internal(("extension_devices", "device_id")),
    "extension_access_tokens": _internal(("extension_devices", "device_id")),
    "pairing_codes": _internal("account_id"),
    "handoff_tickets": _internal("account_id"),
    # plans and entitlements (Bundle 7, 026_entitlements)
    "entitlement_grants": A,
    "plan_catalog_versions": TableSpec("GLOBAL", purge="GLOBAL", export=False),
    "platform_controls": TableSpec("GLOBAL", purge="GLOBAL", export=False),
    # billing (Bundle 7, 027_billing)
    "billing_customers": TableSpec("account_id", purge="RETAIN", retain_class="BILLING_FINANCIAL"),
    "subscriptions": TableSpec("account_id", purge="RETAIN", retain_class="BILLING_FINANCIAL"),
    "subscription_events": TableSpec("account_id", purge="RETAIN", retain_class="BILLING_FINANCIAL"),
    "checkout_sessions": TableSpec("account_id", purge="RETAIN", retain_class="BILLING_FINANCIAL"),
    # provider inbox: rows are reached through the provider ids they carry, not an account
    "billing_webhook_events": TableSpec("GLOBAL", purge="GLOBAL", export=False),
    # usage (Bundle 7, 028_usage)
    "usage_reservations": A,
    "ai_cost_events": TableSpec("account_id", export=False),
    "metered_actions": TableSpec("account_id", export=False),
    # worker (Bundle 7, 029_jobs): account_id is NULL for platform jobs (sweeps, ticks)
    "jobs": _internal("account_id"),
    # operations (Bundle 7)
    "audit_log": TableSpec("account_id", purge="RETAIN", retain_class="SECURITY_AUDIT"),
    # global
    "schema_migrations": TableSpec("GLOBAL", purge="GLOBAL", export=False),
    "discovery_source_settings": TableSpec("GLOBAL", purge="GLOBAL", export=False),
}

MAX_OWNER_HOPS = 4


def owner_chain(table: str) -> list[tuple[str, str, str]]:
    """[(table, column, parent_key), ...] joins from ``table`` to the table
    holding the account column; the last element's column is the account
    column (its parent_key is unused)."""
    chain: list[tuple[str, str, str]] = []
    current = table
    for _ in range(MAX_OWNER_HOPS + 1):
        owner = TENANT_TABLES[current].owner
        if owner == "GLOBAL":
            raise ValueError(f"{table} is global")
        if isinstance(owner, str):
            chain.append((current, owner, ""))
            return chain
        if owner[0] == "USER":
            chain.append((current, owner[1], "USER"))
            return chain
        parent, column, *key = owner
        chain.append((current, column, key[0] if key else "id"))
        current = parent
    raise ValueError(f"{table} does not reach an account within {MAX_OWNER_HOPS} hops")


def account_rows_sql(table: str) -> tuple[str, list[str]]:
    """``SELECT t0.* ...`` for one account's rows of ``table`` (one ``?`` = account id).
    Returns (sql, joined table names)."""
    chain = owner_chain(table)
    aliases = [f"t{i}" for i in range(len(chain))]
    sql = f'SELECT t0.* FROM "{chain[0][0]}" t0'
    for i in range(1, len(chain)):
        parent_key = chain[i - 1][2]
        sql += f' JOIN "{chain[i][0]}" {aliases[i]} ON {aliases[i]}."{parent_key}" = {aliases[i - 1]}."{chain[i - 1][1]}"'
    if chain[-1][2] == "USER":  # user-keyed rows: through the user's account membership
        sql += (f' JOIN account_memberships m ON m.user_id = {aliases[-1]}."{chain[-1][1]}"'
                " WHERE m.account_id = ?")
    else:
        sql += f' WHERE {aliases[-1]}."{chain[-1][1]}" = ?'
    return sql, [link[0] for link in chain]


def purge_order(conn: Any) -> list[str]:
    """Registry tables ordered children-before-parents by foreign keys."""
    from webapp.persistence.schema_catalog import schema_catalog

    catalog = schema_catalog(conn)
    parents = {t: {fk[1] for fk in spec["foreign_keys"] if fk[1] != t} for t, spec in catalog.items()}
    order: list[str] = []
    remaining = {t for t in TENANT_TABLES if t in catalog}
    while remaining:
        # a table is ready when no remaining table references it
        ready = sorted(t for t in remaining if not any(t in parents.get(o, ()) for o in remaining if o != t))
        if not ready:
            raise ValueError(f"foreign-key cycle among {sorted(remaining)}")
        order.extend(ready)
        remaining -= set(ready)
    return order
