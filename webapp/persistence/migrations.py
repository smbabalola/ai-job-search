from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
import json

from product.job_identity import (
    ApplicationIdentityResolution,
    compare_job_identities,
    job_identity,
)
from webapp.persistence.search_workspaces import DEFAULT_SEARCH_WORKSPACE_ID
from webapp.persistence.accounts import (
    DEFAULT_ACCOUNT_DISPLAY_NAME,
    DEFAULT_ACCOUNT_ID,
)


SEARCH_WORKSPACES_MIGRATION_ID = "001_search_workspaces"
PROFILE_MANAGER_MIGRATION_ID = "002_evidence_profile_manager"
ACCOUNTS_OWNERSHIP_MIGRATION_ID = "003_accounts_ownership"
APPLICATION_DOCUMENTS_MIGRATION_ID = "004_application_documents"
HANDOFF_SESSIONS_MIGRATION_ID = "005_handoff_sessions"
ONBOARDING_WALKTHROUGHS_MIGRATION_ID = "006_onboarding_walkthroughs"
PAIRING_SECRETS_MIGRATION_ID = "007_pairing_secrets"
HANDOFF_SESSION_TOKENS_MIGRATION_ID = "008_handoff_session_tokens"
HANDOFF_SESSION_ACTIVITY_MIGRATION_ID = "009_handoff_session_activity"
POLICY_DECISIONS_MIGRATION_ID = "010_policy_decisions"
APPLICATION_BLOCKERS_MIGRATION_ID = "011_application_blockers"
BLOCKER_RESOLUTION_HISTORY_MIGRATION_ID = "012_blocker_resolution_history"
SEMANTIC_SUBJECT_KEY_MIGRATION_ID = "013_semantic_subject_key"
DISCOVERY_SOURCE_REGISTRY_MIGRATION_ID = "014_discovery_source_registry"
AIRSWIFT_DISCOVERY_SOURCE_MIGRATION_ID = "015_airswift_discovery_source"
AUTONOMY_CONTRACT_MIGRATION_ID = "016_autonomy_contract"
AUTONOMY_APPEND_ONLY_TABLES = (
    "autonomy_authorizations", "autonomy_kill_switch", "autonomy_control_events",
    "autonomy_runs", "autonomy_run_ends", "standing_policy_versions", "approved_answers",
    "answer_confirmations", "proposed_answers", "rule_acknowledgements",
    "apply_target_confirmations", "autonomy_decisions", "autonomy_grant_events",
    "intent_overrides", "submission_attempts", "submission_attempt_events",
    "dry_run_submission_cases", "dry_run_case_agreements",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execute_statements(conn: sqlite3.Connection, script: str) -> None:
    """Execute this migration's simple DDL without executescript's implicit commit."""

    for statement in script.split(";"):
        if statement.strip():
            conn.execute(statement)


def apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    migrations = (
        (SEARCH_WORKSPACES_MIGRATION_ID, _migrate_search_workspaces, True),
        (PROFILE_MANAGER_MIGRATION_ID, _migrate_evidence_profile_manager, False),
        (ACCOUNTS_OWNERSHIP_MIGRATION_ID, _migrate_accounts_ownership, False),
        (APPLICATION_DOCUMENTS_MIGRATION_ID, _migrate_application_documents, False),
        (HANDOFF_SESSIONS_MIGRATION_ID, _migrate_handoff_sessions, False),
        (ONBOARDING_WALKTHROUGHS_MIGRATION_ID, _migrate_onboarding_walkthroughs, False),
        (PAIRING_SECRETS_MIGRATION_ID, _migrate_pairing_secrets, False),
        (HANDOFF_SESSION_TOKENS_MIGRATION_ID, _migrate_handoff_session_tokens, False),
        (HANDOFF_SESSION_ACTIVITY_MIGRATION_ID, _migrate_handoff_session_activity, False),
        (POLICY_DECISIONS_MIGRATION_ID, _migrate_policy_decisions, False),
        (APPLICATION_BLOCKERS_MIGRATION_ID, _migrate_application_blockers, False),
        (BLOCKER_RESOLUTION_HISTORY_MIGRATION_ID, _migrate_blocker_resolution_history, False),
        (SEMANTIC_SUBJECT_KEY_MIGRATION_ID, _migrate_semantic_subject_key, False),
        (DISCOVERY_SOURCE_REGISTRY_MIGRATION_ID, _migrate_discovery_source_registry, False),
        (AIRSWIFT_DISCOVERY_SOURCE_MIGRATION_ID, _migrate_airswift_discovery_source, False),
        (AUTONOMY_CONTRACT_MIGRATION_ID, _migrate_autonomy_contract, False),
    )
    for migration_id, operation, disable_foreign_keys in migrations:
        if conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = ?", (migration_id,)
        ).fetchone():
            continue
        if disable_foreign_keys:
            conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            operation(conn)
            conn.execute(
                "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
                (migration_id, _now()),
            )
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"migration {migration_id} created foreign-key violations: {violations!r}"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if disable_foreign_keys:
                conn.execute("PRAGMA foreign_keys = ON")


def _migrate_accounts_ownership(conn: sqlite3.Connection) -> None:
    now = _now()
    _execute_statements(
        conn,
        """
        CREATE TABLE accounts (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """,
    )
    conn.execute(
        "INSERT INTO accounts (id, display_name, created_at) VALUES (?, ?, ?)",
        (DEFAULT_ACCOUNT_ID, DEFAULT_ACCOUNT_DISPLAY_NAME, now),
    )
    # SQLite cannot add a REFERENCES column with a non-NULL default to the
    # already-populated Search Workspace table. Keep the required/defaulted
    # column for lossless legacy inserts and enforce the account reference with
    # FK-equivalent triggers below. account_profiles additionally uses a real
    # composite foreign key to prove profile/workspace owner consistency.
    conn.execute(
        "ALTER TABLE workspaces ADD COLUMN account_id TEXT NOT NULL "
        "DEFAULT 'account_local'"
    )
    conn.execute(
        "ALTER TABLE search_workspaces ADD COLUMN account_id TEXT NOT NULL "
        "DEFAULT 'account_local'"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_workspaces_id_account "
        "ON workspaces(id, account_id)"
    )
    conn.execute(
        "CREATE INDEX idx_workspaces_account_kind "
        "ON workspaces(account_id, kind, updated_at)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_search_workspaces_id_account "
        "ON search_workspaces(id, account_id)"
    )
    conn.execute(
        "CREATE INDEX idx_search_workspaces_account_status "
        "ON search_workspaces(account_id, status, updated_at)"
    )
    _execute_statements(
        conn,
        """
        CREATE TABLE account_profiles (
            account_id TEXT PRIMARY KEY REFERENCES accounts(id),
            workspace_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY (workspace_id, account_id)
                REFERENCES workspaces(id, account_id)
        );

        DROP INDEX idx_profile_source_entries_lookup;
        ALTER TABLE profile_source_settings RENAME TO profile_source_settings_pre_accounts;
        ALTER TABLE profile_source_entries RENAME TO profile_source_entries_pre_accounts;

        CREATE TABLE profile_source_settings (
            account_id TEXT NOT NULL REFERENCES accounts(id),
            source_path TEXT NOT NULL,
            included INTEGER NOT NULL CHECK (included IN (0, 1)),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account_id, source_path)
        );

        CREATE TABLE profile_source_entries (
            account_id TEXT NOT NULL REFERENCES accounts(id),
            entry_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            entry_kind TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            occurrence INTEGER NOT NULL CHECK (occurrence >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account_id, entry_id),
            UNIQUE (account_id, source_path, entry_kind, fingerprint, occurrence)
        );

        CREATE INDEX idx_profile_source_entries_lookup
            ON profile_source_entries(
                account_id, source_path, entry_kind, fingerprint, occurrence
            );

        INSERT INTO profile_source_settings
            (account_id, source_path, included, updated_at)
        SELECT 'account_local', source_path, included, updated_at
        FROM profile_source_settings_pre_accounts;

        INSERT INTO profile_source_entries
            (account_id, entry_id, source_path, entry_kind, fingerprint,
             occurrence, created_at, updated_at)
        SELECT 'account_local', entry_id, source_path, entry_kind, fingerprint,
               occurrence, created_at, updated_at
        FROM profile_source_entries_pre_accounts;

        DROP TABLE profile_source_settings_pre_accounts;
        DROP TABLE profile_source_entries_pre_accounts;
        """,
    )
    for trigger in (
        """CREATE TRIGGER workspaces_account_insert
        BEFORE INSERT ON workspaces
        WHEN NOT EXISTS (SELECT 1 FROM accounts WHERE id = NEW.account_id)
        BEGIN SELECT RAISE(ABORT, 'workspace account does not exist'); END""",
        """CREATE TRIGGER workspaces_account_update
        BEFORE UPDATE OF account_id ON workspaces
        WHEN NEW.account_id <> OLD.account_id
        BEGIN SELECT RAISE(ABORT, 'workspace ownership is immutable'); END""",
        """CREATE TRIGGER search_workspaces_account_insert
        BEFORE INSERT ON search_workspaces
        WHEN NOT EXISTS (SELECT 1 FROM accounts WHERE id = NEW.account_id)
        BEGIN SELECT RAISE(ABORT, 'search workspace account does not exist'); END""",
        """CREATE TRIGGER search_workspaces_account_update
        BEFORE UPDATE OF account_id ON search_workspaces
        WHEN NEW.account_id <> OLD.account_id
        BEGIN SELECT RAISE(ABORT, 'search workspace ownership is immutable'); END""",
        """CREATE TRIGGER accounts_owned_aggregate_delete
        BEFORE DELETE ON accounts
        WHEN EXISTS (SELECT 1 FROM workspaces WHERE account_id = OLD.id)
          OR EXISTS (SELECT 1 FROM search_workspaces WHERE account_id = OLD.id)
        BEGIN SELECT RAISE(ABORT, 'account owns application data'); END""",
    ):
        conn.execute(trigger)
    conn.execute(
        "INSERT INTO account_profiles (account_id, workspace_id, created_at) "
        "SELECT ?, id, ? FROM workspaces "
        "WHERE id = 'profile' AND kind = 'profile' AND account_id = ?",
        (DEFAULT_ACCOUNT_ID, now, DEFAULT_ACCOUNT_ID),
    )


def _migrate_handoff_sessions(conn: sqlite3.Connection) -> None:
    _execute_statements(
        conn,
        """
        CREATE TABLE extension_credentials (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            secret_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT
        );

        CREATE INDEX idx_extension_credentials_account
            ON extension_credentials(account_id);

        CREATE TABLE handoff_sessions (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            pack_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
            target_url TEXT NOT NULL,
            target_domain TEXT NOT NULL,
            ats_adapter_id TEXT NOT NULL,
            ats_adapter_version TEXT NOT NULL,
            started_at TEXT NOT NULL,
            status TEXT NOT NULL,
            user_confirmed_submitted_at TEXT
        );

        CREATE INDEX idx_handoff_sessions_discovery
            ON handoff_sessions(account_id, workspace_id, target_domain);

        CREATE TABLE handoff_events (
            handoff_session_id TEXT NOT NULL REFERENCES handoff_sessions(id),
            event_id TEXT NOT NULL,
            server_sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            normalized_field_type TEXT,
            page_field_key TEXT,
            event_json TEXT NOT NULL,
            observed_at TEXT,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (handoff_session_id, event_id),
            UNIQUE (handoff_session_id, server_sequence)
        );

        CREATE TABLE submission_confirmations (
            id TEXT PRIMARY KEY,
            handoff_session_id TEXT NOT NULL REFERENCES handoff_sessions(id),
            workflow_event_id TEXT REFERENCES workflow_events(id),
            created_at TEXT NOT NULL
        );
        """,
    )


def _migrate_onboarding_walkthroughs(conn: sqlite3.Connection) -> None:
    _execute_statements(
        conn,
        """
        CREATE TABLE onboarding_progress (
            account_id TEXT NOT NULL REFERENCES accounts(id),
            walkthrough_id TEXT NOT NULL,
            walkthrough_version INTEGER NOT NULL CHECK (walkthrough_version >= 1),
            status TEXT NOT NULL CHECK (
                status IN ('not_started', 'in_progress', 'completed', 'skipped')
            ),
            current_step_index INTEGER NOT NULL CHECK (current_step_index >= 0),
            dismissal_reason TEXT CHECK (
                dismissal_reason IS NULL
                OR dismissal_reason IN ('skip', 'dont_show_again', 'close')
            ),
            started_at TEXT,
            last_interacted_at TEXT NOT NULL,
            completed_at TEXT,
            times_completed INTEGER NOT NULL DEFAULT 0 CHECK (times_completed >= 0),
            times_started INTEGER NOT NULL DEFAULT 0 CHECK (times_started >= 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account_id, walkthrough_id)
        );

        CREATE INDEX idx_onboarding_progress_account_status
            ON onboarding_progress(account_id, status);
        """,
    )


def _migrate_pairing_secrets(conn: sqlite3.Connection) -> None:
    _execute_statements(
        conn,
        """
        CREATE TABLE pairing_secrets (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            secret_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        );

        CREATE INDEX idx_pairing_secrets_hash ON pairing_secrets(secret_hash);
        """,
    )


def _migrate_policy_decisions(conn: sqlite3.Connection) -> None:
    # Durable, append-only ledger of every automatic (or future human)
    # classification product/application_decision_policy.py produces for a
    # review item, keyed to the exact source artifact and policy version
    # that produced it. Rows are never updated or deleted: when an upstream
    # artifact reruns, decisions tied to the old artifact_id remain
    # permanently queryable as audit history -- selecting which decision
    # currently governs a workspace's workflow state is Phase 4 read
    # logic, not something this table itself decides.
    #
    # subject_key is deliberately NOT NULL (unlike review_decisions.
    # domain_item_id, which is nullable): a stage-level decision with no
    # natural per-item identity must still supply a stable literal (e.g.
    # "stage") rather than NULL, because SQLite's UNIQUE constraint permits
    # unlimited rows sharing a NULL in an indexed column -- a NULLable
    # subject_key would silently defeat the idempotency guarantee below.
    _execute_statements(
        conn,
        """
        CREATE TABLE policy_decisions (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            stage TEXT NOT NULL CHECK (
                stage IN ('understanding', 'fit', 'application_intelligence', 'content')
            ),
            source_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
            review_item_type TEXT NOT NULL,
            subject_key TEXT NOT NULL,
            domain_item_id TEXT,
            outcome TEXT NOT NULL CHECK (
                outcome IN (
                    'AUTO_PROCEED', 'AUTO_PROCEED_WITH_GAPS', 'AUTO_OMIT',
                    'AUTO_REJECT', 'REQUIRE_USER', 'NOT_APPLICABLE'
                )
            ),
            policy_version TEXT NOT NULL,
            policy_fingerprint TEXT NOT NULL,
            evidence_ids TEXT NOT NULL,
            supported_facts TEXT NOT NULL,
            recorded_gaps TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            reason TEXT NOT NULL,
            confidence TEXT,
            blocking INTEGER NOT NULL CHECK (blocking IN (0, 1)),
            created_at TEXT NOT NULL,
            UNIQUE (
                workspace_id, stage, source_artifact_id, review_item_type,
                subject_key, policy_fingerprint
            )
        );

        CREATE INDEX idx_policy_decisions_workspace_artifact
            ON policy_decisions(workspace_id, source_artifact_id);
        """,
    )
    conn.execute(
        "CREATE TRIGGER policy_decisions_immutable_update "
        "BEFORE UPDATE ON policy_decisions "
        "BEGIN SELECT RAISE(ABORT, 'policy decisions are immutable, append-only audit history'); END"
    )
    conn.execute(
        "CREATE TRIGGER policy_decisions_immutable_delete "
        "BEFORE DELETE ON policy_decisions "
        "BEGIN SELECT RAISE(ABORT, 'policy decisions are immutable, append-only audit history'); END"
    )
    # Additive-only: existing review_decisions rows remain valid with both
    # new columns NULL (interpreted as "resolved by a human, before this
    # column existed" -- no backfill, no reinterpretation of historical
    # rows).
    conn.execute("ALTER TABLE review_decisions ADD COLUMN resolved_by TEXT")
    conn.execute(
        "ALTER TABLE review_decisions ADD COLUMN policy_decision_id "
        "TEXT REFERENCES policy_decisions(id)"
    )


def _migrate_application_blockers(conn: sqlite3.Connection) -> None:
    # A REQUIRE_USER policy decision pauses an application; it is not a
    # failure. Three separate, deliberately non-overlapping concepts:
    #   policy_decisions (010)  -- immutable explanation of why policy
    #                              stopped. Never mutated by this migration
    #                              or anything built on top of it.
    #   application_blockers    -- the current actionable question created
    #                              from a governing REQUIRE_USER decision.
    #   blocker_resolutions     -- what the user answered, with an explicit,
    #                              never-silently-widened reuse scope.
    #
    # application_blockers.policy_decision_id is UNIQUE: this is the
    # idempotency key. save_policy_decision is itself idempotent (a retry
    # returns the same policy_decision id), so a blocker keyed 1:1 on that
    # id is automatically idempotent too -- no separate applicability
    # tuple needs reinventing here.
    #
    # "Superseded" is deliberately NOT a column value this migration
    # writes. Whether a blocker still governs is derived at read time by
    # comparing its source_artifact_id against the workspace's current
    # artifact for that stage (see
    # webapp/services/decision_policy.py::current_application_blockers) --
    # re-running an upstream stage produces a new policy decision, and
    # therefore a new, separate blocker row, while the old row is left
    # completely alone as permanent audit history. status only ever
    # transitions open -> resolved, driven by an actual user answer.
    _execute_statements(
        conn,
        """
        CREATE TABLE application_blockers (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            policy_decision_id TEXT NOT NULL UNIQUE REFERENCES policy_decisions(id),
            source_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
            stage TEXT NOT NULL CHECK (
                stage IN ('understanding', 'fit', 'application_intelligence', 'content')
            ),
            blocker_type TEXT NOT NULL,
            subject_key TEXT NOT NULL,
            question TEXT NOT NULL,
            context TEXT NOT NULL,
            resume_stage TEXT NOT NULL CHECK (
                resume_stage IN ('understanding', 'fit', 'application_intelligence', 'content')
            ),
            allowed_scopes TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'superseded')) DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );

        CREATE INDEX idx_application_blockers_workspace
            ON application_blockers(workspace_id, status);
        CREATE INDEX idx_application_blockers_source_artifact
            ON application_blockers(source_artifact_id);

        CREATE TABLE blocker_resolutions (
            id TEXT PRIMARY KEY,
            blocker_id TEXT NOT NULL UNIQUE REFERENCES application_blockers(id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            policy_decision_id TEXT NOT NULL REFERENCES policy_decisions(id),
            answer_value TEXT NOT NULL,
            answer_scope TEXT NOT NULL CHECK (
                answer_scope IN ('APPLICATION_ONLY', 'SEARCH_WORKSPACE', 'CANDIDATE_FACT')
            ),
            resolved_by TEXT NOT NULL,
            promoted_evidence_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX idx_blocker_resolutions_workspace
            ON blocker_resolutions(workspace_id);
        """,
    )
    conn.execute(
        "CREATE TRIGGER application_blockers_status_immutable_once_resolved "
        "BEFORE UPDATE OF question, context, policy_decision_id, source_artifact_id, "
        "created_at ON application_blockers "
        "BEGIN SELECT RAISE(ABORT, 'application blocker identity fields are immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER application_blockers_no_delete "
        "BEFORE DELETE ON application_blockers "
        "BEGIN SELECT RAISE(ABORT, 'application blockers are permanent audit history'); END"
    )
    conn.execute(
        "CREATE TRIGGER blocker_resolutions_immutable_update "
        "BEFORE UPDATE ON blocker_resolutions "
        "BEGIN SELECT RAISE(ABORT, 'blocker resolutions are immutable, append-only audit history'); END"
    )
    conn.execute(
        "CREATE TRIGGER blocker_resolutions_no_delete "
        "BEFORE DELETE ON blocker_resolutions "
        "BEGIN SELECT RAISE(ABORT, 'blocker resolutions are permanent audit history'); END"
    )


def _migrate_blocker_resolution_history(conn: sqlite3.Connection) -> None:
    # Corrective pass on 011: blocker_resolutions.blocker_id was UNIQUE,
    # permitting exactly one lifetime answer per blocker. A user must be
    # able to correct an answer (e.g. "$55,000" -> "$58,000") before
    # resuming, without destroying the original answer's audit trail.
    #
    # Rebuilt without that UNIQUE constraint: multiple resolution rows may
    # now exist for one blocker_id, each still fully immutable and
    # append-only (the existing update/delete triggers are recreated
    # as-is). A new request_id column plus UNIQUE(blocker_id, request_id)
    # is the idempotency key instead -- a retried resolve request with the
    # same request_id is a safe no-op; a genuinely new correction (a new
    # request_id) always creates a new row. The EFFECTIVE resolution for a
    # blocker is derived, not stored: the most recently created row for
    # that blocker_id (see
    # webapp.persistence.application_blockers.get_effective_resolution).
    # This mirrors how "governing" is already derived elsewhere in this
    # design (current_policy_decisions, current_application_blockers)
    # rather than reinventing a second, stored notion of "current".
    _execute_statements(
        conn,
        """
        CREATE TABLE blocker_resolutions_new (
            id TEXT PRIMARY KEY,
            blocker_id TEXT NOT NULL REFERENCES application_blockers(id),
            request_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            policy_decision_id TEXT NOT NULL REFERENCES policy_decisions(id),
            answer_value TEXT NOT NULL,
            answer_scope TEXT NOT NULL CHECK (
                answer_scope IN ('APPLICATION_ONLY', 'SEARCH_WORKSPACE', 'CANDIDATE_FACT')
            ),
            resolved_by TEXT NOT NULL,
            promoted_evidence_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (blocker_id, request_id)
        );

        INSERT INTO blocker_resolutions_new
            (id, blocker_id, request_id, workspace_id, policy_decision_id, answer_value,
             answer_scope, resolved_by, promoted_evidence_id, created_at)
        SELECT id, blocker_id, id, workspace_id, policy_decision_id, answer_value,
               answer_scope, resolved_by, promoted_evidence_id, created_at
        FROM blocker_resolutions;

        DROP TABLE blocker_resolutions;
        ALTER TABLE blocker_resolutions_new RENAME TO blocker_resolutions;

        CREATE INDEX idx_blocker_resolutions_workspace
            ON blocker_resolutions(workspace_id);
        CREATE INDEX idx_blocker_resolutions_blocker_created
            ON blocker_resolutions(blocker_id, created_at);
        """,
    )
    conn.execute(
        "CREATE TRIGGER blocker_resolutions_immutable_update "
        "BEFORE UPDATE ON blocker_resolutions "
        "BEGIN SELECT RAISE(ABORT, 'blocker resolutions are immutable, append-only audit history'); END"
    )
    conn.execute(
        "CREATE TRIGGER blocker_resolutions_no_delete "
        "BEFORE DELETE ON blocker_resolutions "
        "BEGIN SELECT RAISE(ABORT, 'blocker resolutions are permanent audit history'); END"
    )
    # application_blockers.status already allows 'superseded' (011's
    # CHECK constraint); this migration adds no new column there. What
    # changes is behavioral, at the service layer: a successful upstream
    # rerun now actively marks prior-artifact open blockers superseded at
    # the mutation boundary (see
    # webapp.services.decision_policy.execute_job_fit_policy), rather than
    # leaving them physically 'open' forever while only being excluded
    # from governing queries by artifact comparison.
    conn.execute(
        "ALTER TABLE application_blockers ADD COLUMN superseded_at TEXT"
    )


def _migrate_semantic_subject_key(conn: sqlite3.Connection) -> None:
    # Phase 4C spec §3: a nullable classification of WHICH stable,
    # cross-application-reusable candidate fact a gate blocker is about
    # (drawn from product/semantic_subject_registry.py's closed
    # vocabulary), distinct from the existing subject_key (which is
    # artifact-instance-scoped and never meaningfully comparable across
    # two different applications' own Job Fit artifacts). NULL means
    # "never eligible for cross-application reuse via this mechanism" --
    # legacy rows stay NULL forever; there is no backfill.
    conn.execute(
        "ALTER TABLE application_blockers ADD COLUMN semantic_subject_key TEXT"
    )


def _migrate_discovery_source_registry(conn: sqlite3.Connection) -> None:
    # Backend-controlled enable/disable for discovery sources that are
    # already implemented in code (product/discovery_search.py's
    # SOURCE_CLI_PATHS). This table is never, by itself, sufficient to make
    # a source runnable -- runtime availability is always the intersection
    # of SOURCE_CLI_PATHS.keys() and the enabled rows here (see
    # product/discovery_search.py's available_discovery_source_ids). A row
    # for a source with no matching code-level adapter is inert.
    _execute_statements(
        conn,
        """
        CREATE TABLE discovery_source_settings (
            source_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            updated_at TEXT NOT NULL
        );
        """,
    )
    now = _now()
    for source_id, display_name in (
        ("freehire-search", "Freehire"),
        ("linkedin-search", "LinkedIn"),
        ("energy-jobline-search", "Energy Jobline"),
    ):
        conn.execute(
            "INSERT INTO discovery_source_settings "
            "(source_id, display_name, enabled, updated_at) VALUES (?, ?, 1, ?)",
            (source_id, display_name, now),
        )


def _migrate_airswift_discovery_source(conn: sqlite3.Connection) -> None:
    # Registers airswift-search in the discovery_source_settings table
    # created by migration 014, enabled by default. Deliberately a new,
    # sequential migration rather than an edit to 014 -- existing databases
    # that already applied 014 must pick this row up as an additive change
    # on upgrade, not depend on an edited historical migration.
    conn.execute(
        "INSERT INTO discovery_source_settings "
        "(source_id, display_name, enabled, updated_at) VALUES (?, ?, 1, ?)",
        ("airswift-search", "Airswift", _now()),
    )


def _migrate_handoff_session_tokens(conn: sqlite3.Connection) -> None:
    _execute_statements(
        conn,
        """
        CREATE TABLE handoff_session_tokens (
            id TEXT PRIMARY KEY,
            handoff_session_id TEXT NOT NULL REFERENCES handoff_sessions(id),
            token_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT
        );

        CREATE INDEX idx_handoff_session_tokens_hash ON handoff_session_tokens(token_hash);
        CREATE INDEX idx_handoff_session_tokens_session ON handoff_session_tokens(handoff_session_id);
        """,
    )


def _migrate_handoff_session_activity(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE handoff_sessions ADD COLUMN last_activity_at TEXT")
    conn.execute("UPDATE handoff_sessions SET last_activity_at = started_at")


def _migrate_evidence_profile_manager(conn: sqlite3.Connection) -> None:
    _execute_statements(
        conn,
        """
        CREATE TABLE profile_source_settings (
            source_path TEXT PRIMARY KEY,
            included INTEGER NOT NULL CHECK (included IN (0, 1)),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE profile_source_entries (
            entry_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            entry_kind TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            occurrence INTEGER NOT NULL CHECK (occurrence >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (source_path, entry_kind, fingerprint, occurrence)
        );

        CREATE INDEX idx_profile_source_entries_lookup
            ON profile_source_entries(source_path, entry_kind, fingerprint, occurrence);
        """,
    )


def _migrate_application_documents(conn: sqlite3.Connection) -> None:
    _execute_statements(
        conn,
        """
        CREATE TABLE application_document_versions (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            source_workspace_id TEXT NOT NULL,
            document_kind TEXT NOT NULL CHECK (document_kind IN ('cv', 'cover_letter')),
            origin TEXT NOT NULL CHECK (origin IN ('ai_generated', 'user_uploaded')),
            original_filename TEXT NOT NULL,
            media_type TEXT NOT NULL,
            byte_length INTEGER NOT NULL CHECK (byte_length > 0),
            sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
            storage_key TEXT NOT NULL,
            source_generation_artifact_id TEXT REFERENCES artifacts(id),
            created_at TEXT NOT NULL,
            UNIQUE (id, account_id),
            UNIQUE (id, account_id, document_kind),
            FOREIGN KEY (source_workspace_id, account_id)
                REFERENCES workspaces(id, account_id),
            CHECK ((origin = 'ai_generated' AND source_generation_artifact_id IS NOT NULL)
                OR (origin = 'user_uploaded' AND source_generation_artifact_id IS NULL))
        );

        CREATE INDEX idx_application_documents_workspace
            ON application_document_versions(account_id, source_workspace_id, created_at);

        CREATE TABLE application_document_selections (
            workspace_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            document_kind TEXT NOT NULL CHECK (document_kind IN ('cv', 'cover_letter')),
            document_version_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            selected_at TEXT NOT NULL,
            PRIMARY KEY (workspace_id, document_kind),
            FOREIGN KEY (workspace_id, account_id) REFERENCES workspaces(id, account_id),
            FOREIGN KEY (document_version_id, account_id, document_kind)
                REFERENCES application_document_versions(id, account_id, document_kind)
        );

        CREATE TABLE reusable_application_documents (
            account_id TEXT NOT NULL REFERENCES accounts(id),
            document_version_id TEXT NOT NULL,
            label TEXT,
            saved_at TEXT NOT NULL,
            PRIMARY KEY (account_id, document_version_id),
            FOREIGN KEY (document_version_id, account_id)
                REFERENCES application_document_versions(id, account_id)
        );
        """,
    )
    conn.execute(
        "CREATE TRIGGER application_document_versions_immutable_update "
        "BEFORE UPDATE ON application_document_versions "
        "BEGIN SELECT RAISE(ABORT, 'application document versions are immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER application_document_versions_immutable_delete "
        "BEFORE DELETE ON application_document_versions "
        "BEGIN SELECT RAISE(ABORT, 'application document versions are immutable'); END"
    )
    conn.execute("DROP TRIGGER accounts_owned_aggregate_delete")
    conn.execute(
        "CREATE TRIGGER accounts_owned_aggregate_delete BEFORE DELETE ON accounts "
        "WHEN EXISTS (SELECT 1 FROM workspaces WHERE account_id = OLD.id) "
        "OR EXISTS (SELECT 1 FROM search_workspaces WHERE account_id = OLD.id) "
        "OR EXISTS (SELECT 1 FROM application_document_versions WHERE account_id = OLD.id) "
        "OR EXISTS (SELECT 1 FROM reusable_application_documents WHERE account_id = OLD.id) "
        "BEGIN SELECT RAISE(ABORT, 'account owns application data'); END"
    )


def _migrate_search_workspaces(conn: sqlite3.Connection) -> None:
    now = _now()
    _execute_statements(conn,
        """
        CREATE TABLE search_workspaces (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT
        );

        CREATE TABLE search_workspace_user_profiles (
            search_workspace_id TEXT PRIMARY KEY REFERENCES search_workspaces(id),
            current_version_id TEXT NOT NULL REFERENCES user_profile_versions(id),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE search_workspace_user_profile_history (
            id TEXT PRIMARY KEY,
            search_workspace_id TEXT NOT NULL REFERENCES search_workspaces(id),
            version_id TEXT NOT NULL REFERENCES user_profile_versions(id),
            previous_version_id TEXT REFERENCES user_profile_versions(id),
            assigned_at TEXT NOT NULL
        );

        CREATE INDEX idx_search_workspace_profile_history
            ON search_workspace_user_profile_history(search_workspace_id, assigned_at);
        """
    )
    conn.execute(
        "INSERT INTO search_workspaces "
        "(id, name, status, revision, created_at, updated_at, archived_at) "
        "VALUES (?, 'Default search', 'active', 1, ?, ?, NULL)",
        (DEFAULT_SEARCH_WORKSPACE_ID, now, now),
    )
    legacy_pointer = conn.execute(
        "SELECT version_id, updated_at FROM current_user_profile WHERE id = 'current'"
    ).fetchone()
    if legacy_pointer:
        conn.execute(
            "INSERT INTO search_workspace_user_profiles "
            "(search_workspace_id, current_version_id, revision, updated_at) "
            "VALUES (?, ?, 1, ?)",
            (
                DEFAULT_SEARCH_WORKSPACE_ID,
                legacy_pointer["version_id"],
                legacy_pointer["updated_at"],
            ),
        )
        conn.execute(
            "INSERT INTO search_workspace_user_profile_history "
            "(id, search_workspace_id, version_id, previous_version_id, assigned_at) "
            "VALUES ('swuph_default_initial', ?, ?, NULL, ?)",
            (
                DEFAULT_SEARCH_WORKSPACE_ID,
                legacy_pointer["version_id"],
                legacy_pointer["updated_at"],
            ),
        )

    _rebuild_discovery_tables(conn)
    _execute_statements(conn,
        """
        CREATE TABLE application_workspace_job_identities (
            id TEXT PRIMARY KEY,
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            source_record_key TEXT,
            canonical_url_key TEXT,
            weak_fallback_key TEXT NOT NULL,
            source_record_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (
                application_workspace_id, source_record_key,
                canonical_url_key, weak_fallback_key
            )
        );

        CREATE INDEX idx_application_identity_source_record
            ON application_workspace_job_identities(source_record_key);
        CREATE INDEX idx_application_identity_url
            ON application_workspace_job_identities(canonical_url_key);
        CREATE INDEX idx_application_identity_weak
            ON application_workspace_job_identities(weak_fallback_key);

        CREATE TABLE application_job_identity_conflicts (
            identity_key TEXT NOT NULL,
            key_type TEXT NOT NULL,
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            detected_at TEXT NOT NULL,
            PRIMARY KEY (identity_key, application_workspace_id)
        );

        CREATE TABLE application_workspace_origins (
            id TEXT PRIMARY KEY,
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            search_workspace_id TEXT NOT NULL REFERENCES search_workspaces(id),
            discovery_candidate_id TEXT NOT NULL,
            discovery_occurrence_id TEXT NOT NULL,
            discovery_run_id TEXT,
            promoted_at TEXT NOT NULL,
            UNIQUE (application_workspace_id, discovery_candidate_id),
            FOREIGN KEY (discovery_candidate_id, search_workspace_id)
                REFERENCES discovery_candidates(id, search_workspace_id),
            FOREIGN KEY (discovery_occurrence_id, search_workspace_id)
                REFERENCES discovery_occurrences(id, search_workspace_id),
            FOREIGN KEY (discovery_run_id, search_workspace_id)
                REFERENCES discovery_runs(id, search_workspace_id)
        );
        """
    )
    _backfill_application_identities(conn, now)
    _backfill_application_origins(conn, now)


def _rebuild_discovery_tables(conn: sqlite3.Connection) -> None:
    _execute_statements(conn,
        """
        CREATE TABLE discovery_runs_new (
            id TEXT PRIMARY KEY,
            search_workspace_id TEXT NOT NULL REFERENCES search_workspaces(id),
            user_profile_version_id TEXT NOT NULL REFERENCES user_profile_versions(id),
            user_profile_content_id TEXT NOT NULL,
            request_json TEXT NOT NULL,
            source_status_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE (id, search_workspace_id)
        );
        INSERT INTO discovery_runs_new
        SELECT id, 'search_default', user_profile_version_id, user_profile_content_id,
               request_json, source_status_json, status, created_at, completed_at
        FROM discovery_runs;

        CREATE TABLE discovery_candidates_new (
            id TEXT PRIMARY KEY,
            search_workspace_id TEXT NOT NULL REFERENCES search_workspaces(id),
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            location TEXT,
            lifecycle_status TEXT NOT NULL,
            canonical_occurrence_id TEXT,
            promoted_workspace_id TEXT REFERENCES workspaces(id),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (id, search_workspace_id)
        );
        INSERT INTO discovery_candidates_new
        SELECT id, 'search_default', company, title, location, lifecycle_status,
               canonical_occurrence_id, promoted_workspace_id, first_seen_at,
               last_seen_at, updated_at
        FROM discovery_candidates;

        CREATE TABLE discovery_occurrences_new (
            id TEXT PRIMARY KEY,
            search_workspace_id TEXT NOT NULL REFERENCES search_workspaces(id),
            candidate_id TEXT NOT NULL,
            run_id TEXT,
            source TEXT NOT NULL,
            source_record_id TEXT,
            source_url TEXT,
            source_record_json TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (id, search_workspace_id),
            FOREIGN KEY (candidate_id, search_workspace_id)
                REFERENCES discovery_candidates_new(id, search_workspace_id),
            FOREIGN KEY (run_id, search_workspace_id)
                REFERENCES discovery_runs_new(id, search_workspace_id)
        );
        INSERT INTO discovery_occurrences_new
        SELECT id, 'search_default', candidate_id, run_id, source, source_record_id,
               source_url, source_record_json, captured_at, created_at
        FROM discovery_occurrences;

        CREATE TABLE discovery_candidate_keys_new (
            search_workspace_id TEXT NOT NULL REFERENCES search_workspaces(id),
            identity_key TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            key_type TEXT NOT NULL,
            PRIMARY KEY (search_workspace_id, identity_key),
            FOREIGN KEY (candidate_id, search_workspace_id)
                REFERENCES discovery_candidates_new(id, search_workspace_id)
        );
        INSERT INTO discovery_candidate_keys_new
        SELECT 'search_default', identity_key, candidate_id, key_type
        FROM discovery_candidate_keys;

        CREATE TABLE discovery_fit_results_new (
            id TEXT PRIMARY KEY,
            search_workspace_id TEXT NOT NULL REFERENCES search_workspaces(id),
            candidate_id TEXT NOT NULL,
            occurrence_id TEXT NOT NULL,
            request_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            fingerprints_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (id, search_workspace_id),
            FOREIGN KEY (candidate_id, search_workspace_id)
                REFERENCES discovery_candidates_new(id, search_workspace_id),
            FOREIGN KEY (occurrence_id, search_workspace_id)
                REFERENCES discovery_occurrences_new(id, search_workspace_id)
        );
        INSERT INTO discovery_fit_results_new
        SELECT id, 'search_default', candidate_id, occurrence_id, request_json,
               result_json, fingerprints_json, created_at
        FROM discovery_fit_results;

        CREATE TABLE current_discovery_fits_new (
            search_workspace_id TEXT NOT NULL REFERENCES search_workspaces(id),
            candidate_id TEXT PRIMARY KEY,
            fit_id TEXT NOT NULL,
            FOREIGN KEY (candidate_id, search_workspace_id)
                REFERENCES discovery_candidates_new(id, search_workspace_id),
            FOREIGN KEY (fit_id, search_workspace_id)
                REFERENCES discovery_fit_results_new(id, search_workspace_id)
        );
        INSERT INTO current_discovery_fits_new
        SELECT 'search_default', candidate_id, fit_id FROM current_discovery_fits;

        DROP TABLE current_discovery_fits;
        DROP TABLE discovery_fit_results;
        DROP TABLE discovery_candidate_keys;
        DROP TABLE discovery_occurrences;
        DROP TABLE discovery_candidates;
        DROP TABLE discovery_runs;

        ALTER TABLE discovery_runs_new RENAME TO discovery_runs;
        ALTER TABLE discovery_candidates_new RENAME TO discovery_candidates;
        ALTER TABLE discovery_occurrences_new RENAME TO discovery_occurrences;
        ALTER TABLE discovery_candidate_keys_new RENAME TO discovery_candidate_keys;
        ALTER TABLE discovery_fit_results_new RENAME TO discovery_fit_results;
        ALTER TABLE current_discovery_fits_new RENAME TO current_discovery_fits;

        CREATE INDEX idx_discovery_runs_workspace
            ON discovery_runs(search_workspace_id, created_at);
        CREATE INDEX idx_discovery_occurrences_candidate
            ON discovery_occurrences(search_workspace_id, candidate_id, created_at);
        CREATE INDEX idx_discovery_candidates_status
            ON discovery_candidates(search_workspace_id, lifecycle_status, updated_at);
        """
    )


def _source_record_from_job_snapshot(snapshot: dict) -> dict:
    ingestion = snapshot.get("metadata", {}).get("ingestion", {})
    record = {
        "source": snapshot.get("source", ""),
        "company": snapshot.get("company", ""),
        "title": snapshot.get("title", ""),
        "location": snapshot.get("location", ""),
    }
    if ingestion.get("source_record_id"):
        record["source_record_id"] = ingestion["source_record_id"]
    if snapshot.get("source_url"):
        record["source_url"] = snapshot["source_url"]
    # Preserve the fields used by the conservative weak-only identity
    # fingerprint.  Older application workspaces retain the normalized job
    # snapshot rather than the original source record, so omitting these
    # values would make an exact post-migration resubmission look ambiguous.
    for field in ("description", "raw_text", "employment_type"):
        if snapshot.get(field) is not None:
            record[field] = snapshot[field]
    return record


def _backfill_application_identities(conn: sqlite3.Connection, detected_at: str) -> None:
    rows = conn.execute(
        "SELECT w.id AS workspace_id, a.payload_json "
        "FROM workspaces w "
        "JOIN current_artifacts c ON c.workspace_id = w.id "
        "JOIN artifacts a ON a.id = c.artifact_id "
        "WHERE w.kind = 'job' AND c.artifact_type = 'job_posting_snapshot' "
        "ORDER BY w.created_at, w.id"
    ).fetchall()
    identities: list[tuple[str, dict, object]] = []
    for row in rows:
        source_record = _source_record_from_job_snapshot(json.loads(row["payload_json"]))
        identity = job_identity(source_record)
        identities.append((row["workspace_id"], source_record, identity))
        conn.execute(
            "INSERT INTO application_workspace_job_identities "
            "(id, application_workspace_id, source_record_key, canonical_url_key, "
            "weak_fallback_key, source_record_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"appident_migrated_{row['workspace_id']}",
                row["workspace_id"],
                identity.source_record_key,
                identity.canonical_url_key,
                identity.weak_fallback_key,
                json.dumps(source_record, ensure_ascii=False, sort_keys=True),
                detected_at,
            ),
        )

    for index, (left_workspace, _, left) in enumerate(identities):
        for right_workspace, _, right in identities[index + 1 :]:
            if compare_job_identities(left, right) is not ApplicationIdentityResolution.SAME:
                continue
            if left.source_record_key and left.source_record_key == right.source_record_key:
                key_type, identity_key = "source_record", left.source_record_key
            elif left.canonical_url_key and left.canonical_url_key == right.canonical_url_key:
                key_type, identity_key = "canonical_url", left.canonical_url_key
            else:
                key_type, identity_key = "normalized_fallback", left.weak_fallback_key
            for workspace_id in (left_workspace, right_workspace):
                conn.execute(
                    "INSERT OR IGNORE INTO application_job_identity_conflicts "
                    "(identity_key, key_type, application_workspace_id, detected_at) "
                    "VALUES (?, ?, ?, ?)",
                    (identity_key, key_type, workspace_id, detected_at),
                )


def _backfill_application_origins(conn: sqlite3.Connection, promoted_at: str) -> None:
    rows = conn.execute(
        "SELECT c.id AS candidate_id, c.promoted_workspace_id, "
        "c.canonical_occurrence_id, o.run_id "
        "FROM discovery_candidates c "
        "JOIN discovery_occurrences o ON o.id = c.canonical_occurrence_id "
        "WHERE c.promoted_workspace_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT OR IGNORE INTO application_workspace_origins "
            "(id, application_workspace_id, search_workspace_id, discovery_candidate_id, "
            "discovery_occurrence_id, discovery_run_id, promoted_at) "
            "VALUES (?, ?, 'search_default', ?, ?, ?, ?)",
            (
                f"apporigin_migrated_{row['candidate_id']}",
                row["promoted_workspace_id"],
                row["candidate_id"],
                row["canonical_occurrence_id"],
                row["run_id"],
                promoted_at,
            ),
        )


def _migrate_autonomy_contract(conn: sqlite3.Connection) -> None:
    # Bundle 6B autonomy contract (spec section 15). Every append-only table
    # carries seq INTEGER PRIMARY KEY AUTOINCREMENT and every "current"
    # projection orders by seq, never created_at (spec section 2 invariant
    # 13). Status tables (grants, reservations, intents, queue items) are the
    # only mutable ones.
    #
    # Deviations from the task-8 brief, per user rulings made after the brief
    # was written (these override the brief SQL for autonomy_decisions):
    #   1. mode / requested_stage are nullable -- an invalid_input decision
    #      (product/autonomy_gate.py) may carry no valid mode/stage. A
    #      table-level CHECK restricts NULL to deny_reason = invalid_input.
    #   2. completion_blockers_json (NOT NULL) added -- AuthorizationDecision
    #      .completion_blockers, spec section 9.5.
    #   3. decision_fingerprint (NOT NULL) added -- spec section 9.5/15.1.
    #   4. retryable (NOT NULL, 0/1) added -- AuthorizationDecision.retryable,
    #      spec section 9.5.
    _execute_statements(
        conn,
        """
        CREATE TABLE autonomy_authorizations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            scope_type TEXT NOT NULL CHECK (scope_type IN ('ACCOUNT_MAX', 'DEFAULT_WORKSPACE_CEILING', 'WORKSPACE_CEILING')),
            scope_id TEXT NOT NULL,
            capability TEXT NOT NULL CHECK (capability IN ('NONE', 'PREPARE', 'FILL', 'SUBMIT')),
            set_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_autonomy_authorizations_scope
            ON autonomy_authorizations(account_id, scope_type, scope_id);

        CREATE TABLE autonomy_kill_switch (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            engaged INTEGER NOT NULL CHECK (engaged IN (0, 1)),
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_control_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            scope_type TEXT NOT NULL CHECK (scope_type IN ('APPLICATION', 'SEARCH_WORKSPACE', 'ACCOUNT')),
            scope_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('PAUSE', 'RESUME', 'RESUME_ALL')),
            kill_switch_seq_acknowledged INTEGER,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_runs (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            started_by TEXT NOT NULL CHECK (started_by IN ('SCHEDULER', 'USER')),
            started_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_run_ends (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE REFERENCES autonomy_runs(run_id),
            end_reason TEXT NOT NULL,
            ended_at TEXT NOT NULL
        );

        CREATE TABLE standing_policy_versions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            policy_json TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE approved_answers (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            subject TEXT NOT NULL,
            answer_kind TEXT NOT NULL CHECK (answer_kind IN ('STRUCTURED', 'FREE_TEXT')),
            value_json TEXT NOT NULL,
            reach TEXT NOT NULL CHECK (reach IN ('EMPLOYER', 'SEARCH_WORKSPACE', 'ACCOUNT')),
            scope_id TEXT NOT NULL,
            context_json TEXT NOT NULL,
            provenance TEXT NOT NULL CHECK (provenance IN ('USER', 'USER_EDITED_PROPOSAL')),
            basis_json TEXT NOT NULL,
            basis_profile_version_id TEXT,
            supersedes_id TEXT REFERENCES approved_answers(id),
            source_blocker_resolution_id TEXT REFERENCES blocker_resolutions(id),
            approved_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_approved_answers_subject ON approved_answers(account_id, subject);

        CREATE TABLE answer_confirmations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            approved_answer_id TEXT NOT NULL REFERENCES approved_answers(id),
            confirmed_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE proposed_answers (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            blocker_id TEXT NOT NULL REFERENCES application_blockers(id),
            subject TEXT NOT NULL,
            value_json TEXT NOT NULL,
            provenance TEXT NOT NULL CHECK (provenance = 'SYSTEM_PROPOSED'),
            created_at TEXT NOT NULL
        );

        CREATE TABLE rule_acknowledgements (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            rule_id TEXT NOT NULL,
            rule_hash TEXT NOT NULL,
            observed_fingerprint TEXT NOT NULL,
            policy_version_hash TEXT NOT NULL,
            disposition TEXT NOT NULL CHECK (disposition IN ('PROCEED', 'DO_NOT_PROCEED')),
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE apply_target_confirmations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            job_identity_key TEXT,
            canonical_url TEXT NOT NULL,
            confirmed_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_decisions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            run_id TEXT,
            mode TEXT CHECK (mode IS NULL OR mode IN ('LIVE', 'SHADOW', 'DRY_RUN')),
            requested_stage TEXT CHECK (requested_stage IS NULL OR requested_stage IN ('PREPARE', 'FILL', 'SUBMIT', 'NONE')),
            result TEXT NOT NULL CHECK (result IN ('ALLOW', 'REQUIRE_USER', 'BLOCK', 'DENY', 'DENY_TEMPORARY')),
            deny_reason TEXT,
            effective_capability TEXT NOT NULL CHECK (effective_capability IN ('NONE', 'PREPARE', 'FILL', 'SUBMIT')),
            grantable INTEGER NOT NULL CHECK (grantable IN (0, 1)),
            reasons_json TEXT NOT NULL,
            require_user_json TEXT NOT NULL,
            completion_blockers_json TEXT NOT NULL,
            retry_at TEXT,
            retryable INTEGER NOT NULL CHECK (retryable IN (0, 1)),
            inputs_json TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            decision_fingerprint TEXT NOT NULL,
            engine_version TEXT NOT NULL,
            policy_version_hash TEXT,
            subject_policy_hash TEXT,
            grant_id TEXT,
            created_at TEXT NOT NULL,
            CHECK (
                (mode IS NOT NULL AND requested_stage IS NOT NULL)
                OR deny_reason = 'invalid_input'
            )
        );
        CREATE INDEX idx_autonomy_decisions_workspace ON autonomy_decisions(application_workspace_id, seq);

        CREATE TABLE autonomy_grants (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            decision_id TEXT NOT NULL UNIQUE REFERENCES autonomy_decisions(id),
            account_id TEXT NOT NULL REFERENCES accounts(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            stage TEXT NOT NULL CHECK (stage IN ('FILL', 'SUBMIT')),
            nonce TEXT NOT NULL UNIQUE,
            binding_json TEXT NOT NULL,
            binding_fingerprint TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('ISSUED', 'CONSUMED', 'EXPIRED', 'REVOKED')),
            consumed_at TEXT,
            revoked_reason TEXT
        );

        CREATE TABLE autonomy_grant_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            grant_id TEXT NOT NULL REFERENCES autonomy_grants(id),
            status TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE limit_reservations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            counter_name TEXT NOT NULL,
            window_key TEXT NOT NULL,
            amount TEXT NOT NULL,
            grant_id TEXT REFERENCES autonomy_grants(id),
            attempt_id TEXT,
            status TEXT NOT NULL CHECK (status IN ('RESERVED', 'CONSUMED', 'RELEASED')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_limit_reservations_window
            ON limit_reservations(account_id, counter_name, window_key, status);

        CREATE TABLE submission_intents (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            job_identity_key TEXT NOT NULL,
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            state TEXT NOT NULL CHECK (state IN ('CLAIMED', 'CONFIRMED', 'RELEASED')),
            source TEXT NOT NULL CHECK (source IN ('AUTONOMOUS', 'HUMAN_HANDOFF', 'HUMAN_APPLIED')),
            overridden INTEGER NOT NULL DEFAULT 0 CHECK (overridden IN (0, 1)),
            attempt_id TEXT,
            workflow_event_id TEXT REFERENCES workflow_events(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_submission_intents_live
            ON submission_intents(account_id, job_identity_key)
            WHERE state IN ('CLAIMED', 'CONFIRMED') AND overridden = 0;

        CREATE TABLE intent_overrides (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            intent_id TEXT NOT NULL REFERENCES submission_intents(id),
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE submission_attempts (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            grant_id TEXT NOT NULL UNIQUE REFERENCES autonomy_grants(id),
            intent_id TEXT NOT NULL REFERENCES submission_intents(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            run_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE submission_attempt_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT NOT NULL REFERENCES submission_attempts(id),
            state TEXT NOT NULL CHECK (state IN (
                'AUTHORIZED', 'CLICK_DISPATCHED', 'CONFIRMED_SUCCESS', 'SUBMISSION_AMBIGUOUS',
                'SUBMISSION_FAILED', 'EXPIRED_UNCLICKED', 'DUPLICATE_SUPPRESSED'
            )),
            evidence_json TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('SERVER', 'EXECUTOR', 'USER')),
            created_at TEXT NOT NULL
        );

        CREATE TABLE dry_run_submission_cases (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            decision_id TEXT NOT NULL REFERENCES autonomy_decisions(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            adapter_id TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            manifest_hash TEXT NOT NULL,
            verification_result TEXT NOT NULL CHECK (verification_result IN ('MATCH', 'MISMATCH')),
            created_at TEXT NOT NULL
        );

        CREATE TABLE dry_run_case_agreements (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            case_id TEXT NOT NULL REFERENCES dry_run_submission_cases(id),
            agreement TEXT NOT NULL CHECK (agreement IN ('AGREE', 'DISAGREE')),
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_queue_items (
            application_workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),
            account_id TEXT NOT NULL REFERENCES accounts(id),
            next_stage TEXT NOT NULL CHECK (next_stage IN ('PREPARE', 'FILL', 'SUBMIT')),
            next_eligible_at TEXT,
            lease_holder TEXT,
            lease_expires_at TEXT,
            paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
            updated_at TEXT NOT NULL
        )
        """,
    )
    for table in AUTONOMY_APPEND_ONLY_TABLES:
        for action in ("UPDATE", "DELETE"):
            conn.execute(
                f"CREATE TRIGGER {table}_append_only_{action.lower()} "
                f"BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only audit history'); END"
            )
