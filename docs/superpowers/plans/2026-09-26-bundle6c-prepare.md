# Bundle 6C — Prepare Implementation Plan

> **Execution mode:** native, in the main session, sequentially task by task (superpowers:executing-plans). No `Agent(...)` subagents or background agents unless the user explicitly asks. One commit per task; focused tests after every task; the full suite after Task 1 and in Task 15; stop only for a genuine spec contradiction, an unsafe change, or a production change outside the approved boundary. One independent end-of-bundle review after Task 15. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Operate the PREPARE stage unattended inside the user's standing authority: evaluate and screen discovered candidates, auto-promote eligible ones, and prepare applications through to a genuinely system-confirmed Application Pack or an accurately derived stop.

**Architecture:** One `run_tick()` engine (sweeps → fair lease selection → derive next step from authoritative artifacts → authorize at the PREPARE boundary → re-check controls → reserve cost → run one step with no write lock → fenced finalize), driven by an in-app thread and a CLI worker. Pure decision logic lives in `product/` (`candidate_promotion.py`, `prepare_steps.py`); persistence in `webapp/persistence/autonomy_prepare.py`; orchestration in `webapp/services/autonomy_*`. History is append-only; queues/leases are mutable coordination state only.

**Tech Stack:** Python 3.13, SQLite (WAL), FastAPI + Jinja2, pytest, Hypothesis 6.168.1, Playwright (browser checks), existing 6B autonomy modules.

**Spec:** `docs/superpowers/specs/2026-09-26-bundle6c-prepare-design.md` (approved for implementation planning, 2026-09-26). Every 6B spec invariant and user ruling A–P still applies.

## Global Constraints

- Branch `bundle6/6c-prepare` from `master@20979b9`. Never touch `master` or tags. No push/PR until asked.
- Run tests with `.venv/Scripts/python -m pytest …` from the repo root (Windows, Git Bash).
- Commit per task; message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- `product/` never imports `webapp`.
- "Current" is never chosen by timestamp in new code: derive by `seq` or an explicit pointer (6B invariant 13).
- New history/event tables are append-only (UPDATE/DELETE triggers). `autonomy_candidate_queue` and `autonomy_queue_items` are mutable coordination state and get no trigger.
- 6C code never calls `request_grant` or `pre_click_commit` (a structural test enforces it). No FILL/SUBMIT work.
- 6C uses whichever application-document path is currently enabled; CV-v2 is never a dependency (legacy at base).
- Unattended paid work fails closed without an explicit LLM budget and a per-step cost envelope.
- A driver never overlaps its own ticks; concurrency only via independent drivers coordinated by fenced leases.
- Retries: 3 automatic retries / 4 attempts per cycle, delays (60, 300, 900) s ±20 % jitter.
- Clearing a halt never resumes: halt → explicit resume-all → fresh PREPARE authorization.
- Known pre-existing Windows timestamp-tie flakes (coarse `datetime.now()` + `created_at` ordering on master): `tests/webapp/services/test_application_blockers.py::test_latest_valid_answer_governs`, `tests/webapp/persistence/test_artifacts.py::test_list_artifact_history_newest_first`, `tests/webapp/persistence/test_workflow.py::test_record_status_change_tracks_previous_status`. If one of these exact tests fails, rerun it once and report the rerun. Any other failure is a regression.

## Review Focus

- **A provider call that outlives its lease** (slow model, laptop sleep) — the late worker must not commit anything; recovery settles the attempt as `ABANDONED` at the hard maximum. Pinned in Tasks 5 and 13 (`test_expired_lease_cannot_finalize_even_if_not_retaken`).
- **A user answering a judgment item while a tick is mid-review** — the user's decision must win and no system decision may be written for that item. Pinned in Task 11 (`test_system_review_never_overrides_or_duplicates_a_user_decision`).
- **The same job rediscovered by a second run or source after the first was promoted** — must screen `NOT_ELIGIBLE(existing_application)`, never a second application. Pinned in Task 10 (`test_rediscovered_identity_is_not_promoted_twice`).
- **Standing policy saved mid-pipeline** — the next step must use a fresh PREPARE decision, not the reused one. Pinned in Task 9 (`test_policy_change_invalidates_reuse`).
- **The app restarted with policy/extension files changed on disk** — dormant items must re-derive on driver start. Pinned in Task 13 (`test_driver_start_wakes_all_enrolled_and_candidates`).

## File Map

| File | Responsibility |
|---|---|
| `webapp/persistence/db.py` | WAL + busy timeout (Task 1) |
| `webapp/persistence/migrations.py` | migration `018_autonomy_prepare` (Task 2) |
| `webapp/config.py` | 6C operator settings (Task 3) |
| `webapp/persistence/autonomy_prepare.py` | 6C history tables, queues, fenced leases (Tasks 4–5) |
| `webapp/persistence/autonomy_ledger.py` | reservation subjects, capped reservation, exactly-once settlement, public decision-inputs payload (Task 6) |
| `webapp/persistence/review.py`, `webapp/services/application_pack.py` | review provenance, structured outstanding items, system Gate 4 variant (Task 7) |
| `product/prepare_steps.py` | pure step derivation, mechanical review, pack revision, retry policy (Task 8) |
| `product/candidate_promotion.py` | pure admission + screening (Task 9) |
| `webapp/services/autonomy_providers.py` | provider set + cost meter (Task 9) |
| `webapp/services/autonomy_prepare_auth.py` | material fingerprint, validity horizon, PREPARE authorization reuse (Task 9) |
| `webapp/services/autonomy_candidates.py` | candidate enqueue, admission, evaluation, screening, promotion, exceptions (Task 10) |
| `webapp/services/autonomy_prepare.py` | snapshot, paid steps, system review, system Gate 4, latch, enrolment (Task 11) |
| `webapp/services/autonomy_inbox.py` | inbox, notifier, reconciliation, propagation, wake hooks (Task 12) |
| `webapp/services/autonomy_scheduler.py` | `run_tick` (Task 13) |
| existing services | wake hooks (Tasks 10–13) |
| `webapp/autonomy_worker.py`, `webapp/app.py` | drivers (Task 13) |
| `webapp/services/autonomy_dossier.py` | dossier additions (Task 14) |
| `webapp/api/autonomy.py`, templates, `webapp/static/app.js` | API + inbox UI + badge (Task 14) |
| `tests/webapp/services/test_autonomy_6c_concurrency.py` | concurrency (Task 15) |
| `tests/webapp/test_autonomy_prepare_acceptance.py` | acceptance (Task 15) |
| final verification | browser, migrations, suite, greps (Task 15) |

## Shared test fixtures (created in Task 4, extended later)

`tests/webapp/services/autonomy_6c_fixtures.py` is the one place the 6C tests build worlds. Every later task imports from it; its full content is given in Task 4 and extended (never rewritten) in Tasks 7 and 10–13.

---

### Task 1: SQLite WAL and busy timeout

**Files:**
- Modify: `webapp/persistence/db.py` (`connect`)
- Test: `tests/webapp/persistence/test_db_connection_settings.py`

**Interfaces:**
- Produces: every connection from `connect(db_path)` runs in WAL journal mode with `busy_timeout = 30000`.

- [ ] **Step 1: Write the failing test**

```python
# tests/webapp/persistence/test_db_connection_settings.py
from __future__ import annotations

import threading

from webapp.persistence.db import connect, init_db


def test_connections_use_wal_and_a_long_busy_timeout(tmp_path):
    db = tmp_path / "x.sqlite3"
    init_db(db)
    conn = connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_reader_is_not_blocked_by_an_open_writer(tmp_path):
    db = tmp_path / "x.sqlite3"
    init_db(db)
    writer, reader = connect(db), connect(db)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO accounts (id, display_name, created_at) VALUES ('a2', 'x', '2026-01-01')")
        result = {}
        t = threading.Thread(target=lambda: result.setdefault(
            "n", reader.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]))
        t.start()
        t.join(timeout=5)
        assert "n" in result  # WAL: readers see the last committed state while a writer is open
        writer.rollback()
    finally:
        writer.close()
        reader.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_db_connection_settings.py -q`
Expected: FAIL — journal mode is `delete`.

- [ ] **Step 3: Implement**

In `webapp/persistence/db.py`, inside `connect`, directly after `conn.execute("PRAGMA foreign_keys = ON")`:

```python
    # Bundle 6C: the UI, the in-app scheduler thread and a CLI worker may
    # write concurrently. WAL lets readers proceed while a writer is open;
    # the busy timeout makes contending writers wait instead of failing with
    # "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
```

- [ ] **Step 4: Run the focused test, then the FULL suite (this is an app-wide change)**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_db_connection_settings.py -q` → PASS.
Then the full suite in foreground chunks (each well under 10 minutes):
```bash
.venv/Scripts/python -m pytest tests --ignore=tests/webapp -q -p no:cacheprovider
.venv/Scripts/python -m pytest tests/webapp/persistence tests/webapp/api -q -p no:cacheprovider
.venv/Scripts/python -m pytest tests/webapp/services -q -p no:cacheprovider
.venv/Scripts/python -m pytest $(ls tests/webapp/test_*.py | grep -v browser) -q -p no:cacheprovider
.venv/Scripts/python -m pytest $(ls tests/webapp/test_*browser*.py | sort | head -7) -q -p no:cacheprovider
.venv/Scripts/python -m pytest $(ls tests/webapp/test_*browser*.py | sort | tail -n +8) -q -p no:cacheprovider
```
Expected: all pass (only the 1 live-OpenAI skip). Record the totals in the commit message body.

- [ ] **Step 5: Commit**

```bash
git add webapp/persistence/db.py tests/webapp/persistence/test_db_connection_settings.py
git commit -m "feat(persistence): run SQLite in WAL mode with a 30s busy timeout

Full suite: <paste totals>.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Migration `018_autonomy_prepare`

**Files:**
- Modify: `webapp/persistence/migrations.py` (constant, registration, `_migrate_autonomy_prepare`, extend `AUTONOMY_APPEND_ONLY_TABLES`-style trigger creation)
- Modify: `tests/webapp/persistence/test_accounts_migration.py`, `tests/webapp/persistence/test_search_workspace_migration.py`, `tests/webapp/persistence/test_handoff.py` (add the 018 id exactly as Task 16 of 6B added 017)
- Test: `tests/webapp/persistence/test_autonomy_prepare_migration.py`

**Interfaces:**
- Produces: `AUTONOMY_PREPARE_MIGRATION_ID = "018_autonomy_prepare"`; `AUTONOMY_6C_APPEND_ONLY_TABLES` (tuple of table names); tables and columns exactly as in spec §4.

- [ ] **Step 1: Write the failing test**

```python
# tests/webapp/persistence/test_autonomy_prepare_migration.py
from __future__ import annotations

import sqlite3

import pytest

from webapp.persistence.db import connect, init_db
from webapp.persistence.migrations import AUTONOMY_6C_APPEND_ONLY_TABLES

HISTORY = {
    "autonomy_candidate_screenings", "autonomy_candidate_promotions", "autonomy_candidate_exceptions",
    "autonomy_candidate_exception_resolutions", "autonomy_prepare_steps", "autonomy_review_latches",
    "autonomy_enrolments", "autonomy_retry_requests", "autonomy_notification_events",
}


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "t.sqlite3"
    init_db(db)
    c = connect(db)
    yield c
    c.close()


def _cols(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def test_tables_exist_and_history_tables_are_append_only(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert HISTORY | {"autonomy_candidate_queue"} <= names
    assert set(AUTONOMY_6C_APPEND_ONLY_TABLES) == HISTORY
    for table in HISTORY:
        assert _cols(conn, table)[0] == "seq", table
    triggers = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    for table in HISTORY:
        assert {f"{table}_append_only_update", f"{table}_append_only_delete"} <= triggers, table
    assert not any(t.startswith("autonomy_candidate_queue_append_only") for t in triggers)
    conn.execute("INSERT INTO autonomy_retry_requests (id, account_id, subject_type, subject_id, step_kind, "
                 "input_fingerprint, actor, created_at) VALUES ('rr1', 'account_local', 'APPLICATION', 'w', 'FIT', "
                 "'fp', 'u', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE autonomy_retry_requests SET actor = 'v'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM autonomy_retry_requests")


def test_additive_columns(conn):
    assert "lease_generation" in _cols(conn, "autonomy_queue_items")
    assert "lease_generation" in _cols(conn, "autonomy_candidate_queue")
    assert {"decision_provenance", "system_basis_json"} <= set(_cols(conn, "review_decisions"))
    assert {"subject_type", "subject_id", "settled_amount", "settlement_ref"} <= set(_cols(conn, "limit_reservations"))
    assert {"retry_request_id", "authorization_decision_id", "input_fingerprint"} <= set(_cols(conn, "autonomy_prepare_steps"))


def test_review_provenance_defaults_to_user_and_is_checked(conn):
    from tests.webapp.persistence.autonomy_db import make_workspace
    from webapp.persistence.artifacts import save_artifact
    ws = make_workspace(conn)
    art = save_artifact(conn, workspace_id=ws, artifact_type="job_fit_result", payload={"x": 1})
    insert = ("INSERT INTO review_decisions (id, workspace_id, review_item_type, source_artifact_id, "
              "domain_item_id, disposition, note, created_at{extra}) VALUES (?, ?, 't', ?, NULL, "
              "'acknowledged_and_proceed', NULL, 'x'{marks})")
    conn.execute(insert.format(extra="", marks=""), ("r0", ws, art["id"]))
    assert conn.execute("SELECT decision_provenance FROM review_decisions WHERE id='r0'").fetchone()[0] == "USER"
    with pytest.raises(sqlite3.IntegrityError):  # valid FKs; only the CHECK can fail
        conn.execute(insert.format(extra=", decision_provenance", marks=", 'ROBOT'"), ("r1", ws, art["id"]))


def test_settlement_ref_is_unique_only_when_set(conn):
    base = ("INSERT INTO limit_reservations (id, account_id, counter_name, window_key, amount, status, created_at, "
            "updated_at, settlement_ref) VALUES (?, 'account_local', 'c', 'w', '1', 'RESERVED', 'x', 'x', ?)")
    conn.execute(base, ("r1", None))
    conn.execute(base, ("r2", None))
    conn.execute(base, ("r3", "s1"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(base, ("r4", "s1"))


def test_subject_columns_are_both_or_neither_on_insert_and_update(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO limit_reservations (id, account_id, counter_name, window_key, amount, status, "
                     "created_at, updated_at, subject_type) VALUES ('r9', 'account_local', 'c', 'w', '1', "
                     "'RESERVED', 'x', 'x', 'CANDIDATE')")
    conn.execute("INSERT INTO limit_reservations (id, account_id, counter_name, window_key, amount, status, "
                 "created_at, updated_at, subject_type, subject_id) VALUES ('r8', 'account_local', 'c', 'w', '1', "
                 "'RESERVED', 'x', 'x', 'CANDIDATE', 'cand_1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE limit_reservations SET subject_id = NULL WHERE id = 'r8'")


def test_system_basis_is_required_exactly_for_system_decisions(conn):
    from tests.webapp.persistence.autonomy_db import make_workspace
    from webapp.persistence.artifacts import save_artifact
    ws = make_workspace(conn)
    art = save_artifact(conn, workspace_id=ws, artifact_type="job_fit_result", payload={"x": 1})
    insert = ("INSERT INTO review_decisions (id, workspace_id, review_item_type, source_artifact_id, domain_item_id, "
              "disposition, note, created_at, decision_provenance, system_basis_json) "
              "VALUES (?, ?, 't', ?, NULL, 'acknowledged_and_proceed', NULL, 'x', ?, ?)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("s1", ws, art["id"], "SYSTEM_AUTO_CONFIRMED", None))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("s2", ws, art["id"], "USER", '{"reason": "x"}'))
    conn.execute(insert, ("s3", ws, art["id"], "SYSTEM_AUTO_CONFIRMED", '{"reason": "x"}'))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE review_decisions SET system_basis_json = NULL WHERE id = 's3'")


def test_rerun_is_a_noop(conn, tmp_path):
    init_db(tmp_path / "t.sqlite3")
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE id='018_autonomy_prepare'").fetchone()[0] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_autonomy_prepare_migration.py -q`
Expected: FAIL — `ImportError: AUTONOMY_6C_APPEND_ONLY_TABLES`.

- [ ] **Step 3: Implement the migration**

In `webapp/persistence/migrations.py`: add `AUTONOMY_PREPARE_MIGRATION_ID = "018_autonomy_prepare"` next to the 017 constant; register `(AUTONOMY_PREPARE_MIGRATION_ID, _migrate_autonomy_prepare, False)` after 017; add:

```python
AUTONOMY_6C_APPEND_ONLY_TABLES = (
    "autonomy_candidate_screenings", "autonomy_candidate_promotions", "autonomy_candidate_exceptions",
    "autonomy_candidate_exception_resolutions", "autonomy_prepare_steps", "autonomy_review_latches",
    "autonomy_enrolments", "autonomy_retry_requests", "autonomy_notification_events",
)


def _migrate_autonomy_prepare(conn: sqlite3.Connection) -> None:
    # Bundle 6C (spec §4). History/event tables are append-only; the candidate
    # queue is mutable coordination state (no trigger).
    _execute_statements(
        conn,
        """
        CREATE TABLE autonomy_candidate_screenings (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            search_workspace_id TEXT NOT NULL REFERENCES search_workspaces(id),
            candidate_id TEXT NOT NULL,
            discovery_run_id TEXT,
            discovery_fit_id TEXT,
            outcome TEXT NOT NULL CHECK (outcome IN ('PROMOTE', 'REQUIRE_USER', 'BLOCK', 'NOT_ELIGIBLE', 'DENY', 'DENY_TEMPORARY')),
            reason_code TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            require_user_json TEXT NOT NULL,
            could_unlock INTEGER NOT NULL CHECK (could_unlock IN (0, 1)),
            retry_at TEXT,
            input_fingerprint TEXT NOT NULL,
            authority_json TEXT NOT NULL,
            policy_version_hash TEXT,
            subject_policy_hash TEXT,
            engine_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_candidate_screenings_candidate ON autonomy_candidate_screenings(candidate_id, seq);

        CREATE TABLE autonomy_candidate_promotions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            screening_id TEXT REFERENCES autonomy_candidate_screenings(id),
            candidate_id TEXT NOT NULL,
            search_workspace_id TEXT NOT NULL REFERENCES search_workspaces(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            actor_type TEXT NOT NULL CHECK (actor_type IN ('SCHEDULER', 'USER')),
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_candidate_exceptions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            search_workspace_id TEXT NOT NULL REFERENCES search_workspaces(id),
            candidate_id TEXT NOT NULL,
            screening_id TEXT NOT NULL REFERENCES autonomy_candidate_screenings(id),
            items_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_candidate_exception_resolutions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            exception_id TEXT NOT NULL REFERENCES autonomy_candidate_exceptions(id),
            resolution TEXT NOT NULL CHECK (resolution IN ('PROMOTE', 'DISMISS')),
            actor TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_prepare_steps (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            attempt_id TEXT NOT NULL,
            subject_type TEXT NOT NULL CHECK (subject_type IN ('APPLICATION', 'CANDIDATE')),
            subject_id TEXT NOT NULL,
            step_kind TEXT NOT NULL CHECK (step_kind IN ('EVALUATE', 'UNDERSTAND', 'FIT', 'INTELLIGENCE', 'SYSTEM_REVIEW', 'GATE4')),
            attempt_no INTEGER NOT NULL,
            event TEXT NOT NULL CHECK (event IN ('STARTED', 'SUCCEEDED', 'REUSED', 'FAILED', 'ABANDONED')),
            input_fingerprint TEXT NOT NULL,
            authorization_decision_id TEXT REFERENCES autonomy_decisions(id),
            retry_request_id TEXT,
            lease_generation INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            run_id TEXT,
            artifact_refs_json TEXT NOT NULL DEFAULT '[]',
            reservation_ids_json TEXT NOT NULL DEFAULT '[]',
            cost_json TEXT NOT NULL DEFAULT '{}',
            error_class TEXT CHECK (error_class IS NULL OR error_class IN ('TRANSIENT', 'HUMAN_FIXABLE', 'INTERNAL')),
            error_code TEXT,
            error_detail TEXT,
            created_at TEXT NOT NULL,
            CHECK (subject_type = 'CANDIDATE' OR authorization_decision_id IS NOT NULL),
            CHECK (subject_type = 'APPLICATION' OR authorization_decision_id IS NULL)
        );
        CREATE INDEX idx_prepare_steps_subject ON autonomy_prepare_steps(subject_type, subject_id, seq);
        CREATE INDEX idx_prepare_steps_attempt ON autonomy_prepare_steps(attempt_id, seq);

        CREATE TABLE autonomy_review_latches (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            pack_revision TEXT NOT NULL,
            reason TEXT NOT NULL CHECK (reason IN ('EXPLICIT_REVIEW', 'REOPENED_CONFIRMED')),
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_enrolments (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            action TEXT NOT NULL CHECK (action IN ('ENROL', 'UNENROL')),
            actor_type TEXT NOT NULL CHECK (actor_type IN ('USER', 'SCHEDULER')),
            actor TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_retry_requests (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            subject_type TEXT NOT NULL CHECK (subject_type IN ('APPLICATION', 'CANDIDATE')),
            subject_id TEXT NOT NULL,
            step_kind TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomy_notification_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            notification_key TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('NEEDS_USER', 'CANDIDATE_QUESTION', 'PREPARED', 'OPERATIONAL_ERROR', 'BLOCKED')),
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            event TEXT NOT NULL CHECK (event IN ('CREATED', 'SEEN', 'RESOLVED')),
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_notification_events_key ON autonomy_notification_events(account_id, notification_key, seq);

        CREATE TABLE autonomy_candidate_queue (
            candidate_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            search_workspace_id TEXT NOT NULL REFERENCES search_workspaces(id),
            next_eligible_at TEXT,
            lease_holder TEXT,
            lease_generation INTEGER NOT NULL DEFAULT 0,
            lease_expires_at TEXT,
            updated_at TEXT NOT NULL
        );

        """,
    )

    # Column additions are idempotent: the "upgrade from 004" test re-runs
    # 016-018 while review_decisions / limit_reservations survive.
    def add_column(table: str, name: str, ddl: str) -> None:
        if name not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    add_column("autonomy_queue_items", "lease_generation", "lease_generation INTEGER NOT NULL DEFAULT 0")
    add_column("review_decisions", "decision_provenance",
               "decision_provenance TEXT NOT NULL DEFAULT 'USER' "
               "CHECK (decision_provenance IN ('USER', 'SYSTEM_AUTO_CONFIRMED'))")
    add_column("review_decisions", "system_basis_json", "system_basis_json TEXT")
    add_column("limit_reservations", "subject_type",
               "subject_type TEXT CHECK (subject_type IS NULL OR subject_type IN ('APPLICATION', 'CANDIDATE'))")
    add_column("limit_reservations", "subject_id", "subject_id TEXT")
    add_column("limit_reservations", "settled_amount", "settled_amount TEXT")
    add_column("limit_reservations", "settlement_ref", "settlement_ref TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_limit_reservations_settlement_ref "
                 "ON limit_reservations(settlement_ref) WHERE settlement_ref IS NOT NULL")
    for action in ("INSERT", "UPDATE"):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS limit_reservations_subject_pair_{action.lower()} "
                     f"BEFORE {action} ON limit_reservations "
                     "WHEN (NEW.subject_type IS NULL) != (NEW.subject_id IS NULL) "
                     "BEGIN SELECT RAISE(ABORT, 'subject_type and subject_id must both be set or both be NULL'); END")
        # A system review decision always carries its basis; a user decision never does.
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS review_decisions_provenance_basis_{action.lower()} "
                     f"BEFORE {action} ON review_decisions "
                     "WHEN (NEW.decision_provenance = 'SYSTEM_AUTO_CONFIRMED') != (NEW.system_basis_json IS NOT NULL) "
                     "BEGIN SELECT RAISE(ABORT, 'system_basis_json is required exactly for SYSTEM_AUTO_CONFIRMED'); END")
    for table in AUTONOMY_6C_APPEND_ONLY_TABLES:  # same form as 016
        for action in ("UPDATE", "DELETE"):
            conn.execute(
                f"CREATE TRIGGER {table}_append_only_{action.lower()} "
                f"BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only audit history'); END"
            )
```

- [ ] **Step 4: Update the three existing id-list tests**

Exactly as 6B commit `e782e37` did for 017: add `"018_autonomy_prepare"` after `"017_autonomy_human_intent_backfill"` in `test_accounts_migration.py` (1 place) and `test_search_workspace_migration.py` (2 places); in `test_handoff.py` import `AUTONOMY_PREPARE_MIGRATION_ID`, add it to the DELETE id tuple (and add one `?` placeholder), drop the new tables in the "upgrade from 004" simulation before deleting ids (add the nine history tables and `autonomy_candidate_queue` to its drop loop — all empty there), and assert the 018 row exists after re-migration. Note: that test drops 016 tables; since 018 adds columns to 016's `autonomy_queue_items` and `limit_reservations`, dropping and recreating them via 016 then 018 is consistent.

- [ ] **Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add webapp/persistence/migrations.py tests/webapp/persistence
git commit -m "feat(persistence): add migration 018 for autonomy prepare

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 6C operator settings

**Files:**
- Modify: `webapp/config.py`
- Test: `tests/webapp/test_config_autonomy_6c.py`

**Interfaces:**
- Produces on `Settings`: `autonomy_scheduler_enabled: bool` (env `JOBSEARCH_AUTONOMY_SCHEDULER == "1"`), `autonomy_tick_interval: float = 30.0`, `autonomy_max_items_per_tick: int = 4`, `autonomy_step_timeout: float = 600.0`, `autonomy_lease_margin: float = 120.0`, `autonomy_step_cost_max: dict[str, Decimal]` (env `JOBSEARCH_AUTONOMY_STEP_COST_MAX`, JSON object of step kind → positive decimal string; anything invalid → `{}`), `autonomy_max_promotions_per_day: int = 5`, `autonomy_dispatch_result_timeout: float = 600.0`, `autonomy_retry_delays: tuple[float, ...] = (60.0, 300.0, 900.0)`; method `autonomy_step_envelope(step_kind: str) -> Decimal | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/webapp/test_config_autonomy_6c.py
from __future__ import annotations

from decimal import Decimal

from webapp.config import Settings


def test_defaults_are_off_and_fail_closed(monkeypatch, tmp_path):
    for var in ("JOBSEARCH_AUTONOMY_SCHEDULER", "JOBSEARCH_AUTONOMY_STEP_COST_MAX"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(db_path=tmp_path / "db.sqlite3")
    assert s.autonomy_scheduler_enabled is False
    assert s.autonomy_step_cost_max == {}
    assert s.autonomy_step_envelope("FIT") is None
    assert (s.autonomy_tick_interval, s.autonomy_max_items_per_tick) == (30.0, 4)
    assert (s.autonomy_step_timeout, s.autonomy_lease_margin) == (600.0, 120.0)
    assert s.autonomy_max_promotions_per_day == 5
    assert s.autonomy_retry_delays == (60.0, 300.0, 900.0)


def test_envelopes_parse_and_invalid_input_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBSEARCH_AUTONOMY_SCHEDULER", "1")
    monkeypatch.setenv("JOBSEARCH_AUTONOMY_STEP_COST_MAX", '{"EVALUATE": "0.05", "FIT": "0.10"}')
    s = Settings(db_path=tmp_path / "db.sqlite3")
    assert s.autonomy_scheduler_enabled is True
    assert s.autonomy_step_envelope("FIT") == Decimal("0.10")
    assert s.autonomy_step_envelope("UNDERSTAND") is None
    for bad in ("not json", '{"FIT": "-1"}', '{"FIT": "NaN"}', '{"FIT": 0.1}', '["FIT"]', '{"BOGUS": "1"}'):
        monkeypatch.setenv("JOBSEARCH_AUTONOMY_STEP_COST_MAX", bad)
        assert Settings(db_path=tmp_path / "db.sqlite3").autonomy_step_cost_max == {}, bad
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/test_config_autonomy_6c.py -q`
Expected: FAIL — `AttributeError: autonomy_scheduler_enabled`.

- [ ] **Step 3: Implement**

In `webapp/config.py` add `import json` and `from decimal import Decimal, InvalidOperation`, then this module-level helper above `Settings`:

```python
_STEP_KINDS = ("EVALUATE", "UNDERSTAND", "FIT", "INTELLIGENCE")


def _parse_step_cost_max(raw: str | None) -> dict[str, Decimal]:
    """Bundle 6C per-step hard cost envelopes. Anything malformed yields {}
    so unattended paid work fails closed (spec §11.2)."""
    if not raw:
        return {}
    try:
        doc = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(doc, dict):
        return {}
    out: dict[str, Decimal] = {}
    for key, value in doc.items():
        if key not in _STEP_KINDS or not isinstance(value, str):
            return {}
        try:
            amount = Decimal(value)
        except InvalidOperation:
            return {}
        if not amount.is_finite() or amount <= 0:
            return {}
        out[key] = amount
    return out
```

Fields, after `autonomy_shadow_enabled`:

```python
    # Bundle 6C scheduler (spec §14). Operator execution gates: they never
    # grant capability; the scheduler is off unless explicitly enabled.
    autonomy_scheduler_enabled: bool = field(
        default_factory=lambda: os.environ.get("JOBSEARCH_AUTONOMY_SCHEDULER") == "1"
    )
    autonomy_tick_interval: float = 30.0
    autonomy_max_items_per_tick: int = 4
    autonomy_step_timeout: float = 600.0
    autonomy_lease_margin: float = 120.0
    autonomy_step_cost_max: dict = field(
        default_factory=lambda: _parse_step_cost_max(os.environ.get("JOBSEARCH_AUTONOMY_STEP_COST_MAX"))
    )
    autonomy_max_promotions_per_day: int = 5
    autonomy_dispatch_result_timeout: float = 600.0
    autonomy_retry_delays: tuple = (60.0, 300.0, 900.0)
```

Method, after `autonomy_sentinel_path`:

```python
    def autonomy_step_envelope(self, step_kind: str) -> Decimal | None:
        return self.autonomy_step_cost_max.get(step_kind)
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/test_config_autonomy_6c.py tests/webapp/test_app_factory.py tests/webapp/services/test_autonomy_controls.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/config.py tests/webapp/test_config_autonomy_6c.py
git commit -m "feat(config): add 6C scheduler settings and fail-closed step cost envelopes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 4: Persistence — 6C history records

**Files:**
- Create: `webapp/persistence/autonomy_prepare.py`
- Create: `tests/webapp/services/autonomy_6c_fixtures.py`
- Test: `tests/webapp/persistence/test_autonomy_prepare_history.py`

**Interfaces:**
- Consumes: migration 018 (Task 2); `canonical_json`, `to_utc_iso` from `product.autonomy_contract`.
- Produces (all **no commit** — callers own the transaction):
  - `record_enrolment(conn, *, account_id, application_workspace_id, action, actor_type, actor, reason, now) -> dict`; `current_enrolment(conn, application_workspace_id) -> str | None`; `is_enrolled(conn, application_workspace_id) -> bool`; `enrolled_applications(conn, account_id) -> list[str]`
  - `record_retry_request(conn, *, account_id, subject_type, subject_id, step_kind, input_fingerprint, actor, now) -> dict`; `latest_retry_request(conn, *, subject_type, subject_id, step_kind, input_fingerprint) -> dict | None`
  - `record_latch(conn, *, application_workspace_id, pack_revision, reason, actor, now) -> dict`; `has_latch(conn, application_workspace_id, pack_revision) -> bool`
  - `create_notification(conn, *, account_id, key, kind, subject_type, subject_id, detail, now) -> bool`; `mark_seen(conn, *, account_id, key, now) -> bool`; `resolve_notification(conn, *, account_id, key, now) -> bool`; `open_notifications(conn, account_id) -> list[dict]` (each: `key, kind, subject_type, subject_id, detail, created_at, seen`)
  - `insert_screening(conn, **fields) -> dict`; `latest_screening(conn, candidate_id) -> dict | None`; `get_screening(conn, screening_id) -> dict | None`
  - `insert_promotion(conn, *, screening_id, candidate_id, search_workspace_id, application_workspace_id, actor_type, actor, now) -> dict`; `promotion_for_candidate(conn, candidate_id) -> dict | None`
  - `open_candidate_exception(conn, *, account_id, search_workspace_id, candidate_id, screening_id, items, now) -> dict`; `resolve_candidate_exception(conn, *, exception_id, resolution, actor, reason, now) -> dict`; `get_candidate_exception(conn, exception_id) -> dict | None` (with `resolution`); `current_candidate_exception(conn, candidate_id) -> dict | None`; `open_candidate_exceptions(conn, account_id) -> list[dict]`
  - `start_attempt(conn, *, subject_type, subject_id, step_kind, attempt_no, input_fingerprint, authorization_decision_id, retry_request_id, lease_generation, worker_id, reservation_ids, now, run_id=None) -> str` (attempt id); `finish_attempt(conn, *, attempt_id, event, now, artifact_refs=(), cost=None, error_class=None, error_code=None, error_detail=None) -> dict`; `attempt_rows(conn, subject_type, subject_id) -> list[dict]`; `orphaned_attempts(conn, subject_type, subject_id) -> list[dict]`; `cycle_failures(conn, *, subject_type, subject_id, step_kind, input_fingerprint, retry_request_id) -> int`; `next_attempt_no(conn, subject_type, subject_id, step_kind) -> int`

- [ ] **Step 1: Create the shared fixtures module**

```python
# tests/webapp/services/autonomy_6c_fixtures.py
"""Shared world-building for Bundle 6C tests. Later tasks APPEND helpers here;
never rewrite existing ones."""
from __future__ import annotations

from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, db_path, make_workspace  # noqa: F401
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/webapp/persistence/test_autonomy_prepare_history.py
from __future__ import annotations

from datetime import timedelta

from webapp.persistence import autonomy_prepare as ap
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, NOW, conn, make_workspace  # noqa: F401


def test_enrolment_is_derived_from_latest_event(conn):
    ws = make_workspace(conn)
    assert ap.current_enrolment(conn, ws) is None and not ap.is_enrolled(conn, ws)
    ap.record_enrolment(conn, account_id=ACCOUNT, application_workspace_id=ws, action="ENROL",
                        actor_type="USER", actor="u", reason=None, now=NOW)
    assert ap.is_enrolled(conn, ws) and ap.enrolled_applications(conn, ACCOUNT) == [ws]
    ap.record_enrolment(conn, account_id=ACCOUNT, application_workspace_id=ws, action="UNENROL",
                        actor_type="USER", actor="u", reason="stop", now=NOW)
    assert not ap.is_enrolled(conn, ws) and ap.enrolled_applications(conn, ACCOUNT) == []


def test_retry_request_lookup_is_exact(conn):
    ws = make_workspace(conn)
    kw = dict(subject_type="APPLICATION", subject_id=ws, step_kind="FIT", input_fingerprint="fp1")
    assert ap.latest_retry_request(conn, **kw) is None
    first = ap.record_retry_request(conn, account_id=ACCOUNT, actor="u", now=NOW, **kw)
    second = ap.record_retry_request(conn, account_id=ACCOUNT, actor="u", now=NOW, **kw)
    assert ap.latest_retry_request(conn, **kw)["id"] == second["id"] != first["id"]
    assert ap.latest_retry_request(conn, **{**kw, "input_fingerprint": "fp2"}) is None


def test_latch_is_per_revision(conn):
    ws = make_workspace(conn)
    ap.record_latch(conn, application_workspace_id=ws, pack_revision="rev1", reason="EXPLICIT_REVIEW",
                    actor="u", now=NOW)
    assert ap.has_latch(conn, ws, "rev1") and not ap.has_latch(conn, ws, "rev2")


def test_notifications_dedupe_by_occurrence_and_seen_is_not_resolved(conn):
    kw = dict(account_id=ACCOUNT, key="needs_user:ws1:fp1", kind="NEEDS_USER", subject_type="APPLICATION",
              subject_id="ws1", detail={"n": 1})
    assert ap.create_notification(conn, now=NOW, **kw) is True
    assert ap.create_notification(conn, now=NOW, **kw) is False  # still open: no spam
    assert ap.mark_seen(conn, account_id=ACCOUNT, key=kw["key"], now=NOW) is True
    (item,) = ap.open_notifications(conn, ACCOUNT)
    assert item["seen"] is True  # seen, but still open
    assert ap.resolve_notification(conn, account_id=ACCOUNT, key=kw["key"], now=NOW) is True
    assert ap.open_notifications(conn, ACCOUNT) == []
    assert ap.create_notification(conn, now=NOW, **kw) is True  # recurrence after resolution notifies again
    assert ap.open_notifications(conn, ACCOUNT)[0]["seen"] is False


def test_candidate_exception_resolution_is_separate_and_current_by_seq(conn):
    from tests.webapp.services.autonomy_6c_fixtures import make_screening_row
    screening = make_screening_row(conn, candidate_id="cand_1", outcome="REQUIRE_USER", could_unlock=True)
    exc = ap.open_candidate_exception(conn, account_id=ACCOUNT, search_workspace_id="search_default",
                                      candidate_id="cand_1", screening_id=screening["id"],
                                      items=[{"kind": "rule", "ref": "fit_unknown"}], now=NOW)
    assert [e["id"] for e in ap.open_candidate_exceptions(conn, ACCOUNT)] == [exc["id"]]
    ap.resolve_candidate_exception(conn, exception_id=exc["id"], resolution="DISMISS", actor="u", reason=None, now=NOW)
    assert ap.open_candidate_exceptions(conn, ACCOUNT) == []
    assert ap.get_candidate_exception(conn, exc["id"])["resolution"]["resolution"] == "DISMISS"


def test_attempt_log_and_cycle_failure_counting(conn):
    ws = make_workspace(conn)
    kw = dict(subject_type="APPLICATION", subject_id=ws, step_kind="FIT", input_fingerprint="fp1")

    def attempt(event, error_class=None, retry_request_id=None, fingerprint="fp1"):
        attempt_id = ap.start_attempt(
            conn, subject_type="APPLICATION", subject_id=ws, step_kind="FIT",
            attempt_no=ap.next_attempt_no(conn, "APPLICATION", ws, "FIT"), input_fingerprint=fingerprint,
            authorization_decision_id=_decision(conn, ws), retry_request_id=retry_request_id,
            lease_generation=1, worker_id="w", reservation_ids=[], now=NOW)
        ap.finish_attempt(conn, attempt_id=attempt_id, event=event, error_class=error_class, now=NOW)
        return attempt_id

    attempt("FAILED", "TRANSIENT")
    attempt("FAILED", "TRANSIENT")
    assert ap.cycle_failures(conn, retry_request_id=None, **kw) == 2
    attempt("FAILED", "TRANSIENT", fingerprint="fp2")  # a different fingerprint is a different cycle
    assert ap.cycle_failures(conn, retry_request_id=None, **kw) == 2
    attempt("SUCCEEDED")
    assert ap.cycle_failures(conn, retry_request_id=None, **kw) == 0
    attempt("FAILED", "TRANSIENT", retry_request_id="rr_1")
    assert ap.cycle_failures(conn, retry_request_id="rr_1", **kw) == 1
    assert ap.cycle_failures(conn, retry_request_id=None, **kw) == 0


def test_orphaned_started_attempt_is_detected_and_terminal_rows_copy_subject(conn):
    ws = make_workspace(conn)
    attempt_id = ap.start_attempt(conn, subject_type="APPLICATION", subject_id=ws, step_kind="UNDERSTAND",
                                  attempt_no=1, input_fingerprint="fp", authorization_decision_id=_decision(conn, ws),
                                  retry_request_id=None, lease_generation=3, worker_id="w1",
                                  reservation_ids=["res_1"], now=NOW)
    (orphan,) = ap.orphaned_attempts(conn, "APPLICATION", ws)
    assert orphan["attempt_id"] == attempt_id and orphan["reservation_ids"] == ["res_1"]
    row = ap.finish_attempt(conn, attempt_id=attempt_id, event="ABANDONED", now=NOW + timedelta(minutes=20))
    assert (row["subject_id"], row["lease_generation"], row["event"]) == (ws, 3, "ABANDONED")
    assert ap.orphaned_attempts(conn, "APPLICATION", ws) == []


def _decision(conn, ws):
    from product.autonomy_gate import evaluate_authorization
    from tests.product.autonomy_fixtures import make_ctx
    from webapp.persistence.autonomy_ledger import insert_decision
    ctx = make_ctx(account_id=ACCOUNT, application_workspace_id=ws)
    return insert_decision(conn, ctx=ctx, decision=evaluate_authorization(ctx), commit=False)["id"]
```

Append to `tests/webapp/services/autonomy_6c_fixtures.py`:

```python
def make_screening_row(conn, *, candidate_id, outcome="PROMOTE", could_unlock=False, fingerprint="fp",
                       search_workspace_id="search_default", now=NOW):
    from webapp.persistence.autonomy_prepare import insert_screening
    return insert_screening(
        conn, account_id=ACCOUNT, search_workspace_id=search_workspace_id, candidate_id=candidate_id,
        discovery_run_id=None, discovery_fit_id=None, outcome=outcome, reason_code=outcome.lower(),
        reasons=[], require_user=[], could_unlock=could_unlock, retry_at=None, input_fingerprint=fingerprint,
        authority={"deployment": "PREPARE", "account": "PREPARE", "workspace": "PREPARE"},
        policy_version_hash=None, subject_policy_hash=None, engine_version="candidate-promotion.v1", now=now)
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_autonomy_prepare_history.py -q`
Expected: FAIL — module `webapp.persistence.autonomy_prepare` not found.

- [ ] **Step 4: Implement `webapp/persistence/autonomy_prepare.py` (history part)**

```python
"""Bundle 6C persistence (spec §4). History/event tables are append-only and
"current" is always derived by seq. The queue/lease helpers (below, Task 5)
mutate coordination state only. Nothing here commits: callers own the
transaction (usually autonomy_controls.run_immediate)."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Iterable

from product.autonomy_contract import canonical_json, to_utc_iso


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> dict[str, Any]:
    cols, marks = ", ".join(values), ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    return dict(conn.execute(f"SELECT * FROM {table} WHERE seq = ?", (cur.lastrowid,)).fetchone())


# ---- enrolment --------------------------------------------------------------

def record_enrolment(conn, *, account_id: str, application_workspace_id: str, action: str, actor_type: str,
                     actor: str, reason: str | None, now: datetime) -> dict[str, Any]:
    return _insert(conn, "autonomy_enrolments", {
        "id": _id("enrol"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "action": action, "actor_type": actor_type, "actor": actor, "reason": reason, "created_at": to_utc_iso(now),
    })


def current_enrolment(conn, application_workspace_id: str) -> str | None:
    row = conn.execute(
        "SELECT action FROM autonomy_enrolments WHERE application_workspace_id = ? ORDER BY seq DESC LIMIT 1",
        (application_workspace_id,),
    ).fetchone()
    return row["action"] if row else None


def is_enrolled(conn, application_workspace_id: str) -> bool:
    return current_enrolment(conn, application_workspace_id) == "ENROL"


def enrolled_applications(conn, account_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT e.application_workspace_id FROM autonomy_enrolments e WHERE e.account_id = ? AND e.action = 'ENROL' "
        "AND e.seq = (SELECT MAX(x.seq) FROM autonomy_enrolments x "
        "             WHERE x.application_workspace_id = e.application_workspace_id) ORDER BY e.seq",
        (account_id,),
    ).fetchall()
    return [r["application_workspace_id"] for r in rows]


# ---- explicit retries -------------------------------------------------------

def record_retry_request(conn, *, account_id: str, subject_type: str, subject_id: str, step_kind: str,
                         input_fingerprint: str, actor: str, now: datetime) -> dict[str, Any]:
    return _insert(conn, "autonomy_retry_requests", {
        "id": _id("retry"), "account_id": account_id, "subject_type": subject_type, "subject_id": subject_id,
        "step_kind": step_kind, "input_fingerprint": input_fingerprint, "actor": actor,
        "created_at": to_utc_iso(now),
    })


def latest_retry_request(conn, *, subject_type: str, subject_id: str, step_kind: str,
                         input_fingerprint: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM autonomy_retry_requests WHERE subject_type = ? AND subject_id = ? AND step_kind = ? "
        "AND input_fingerprint = ? ORDER BY seq DESC LIMIT 1",
        (subject_type, subject_id, step_kind, input_fingerprint),
    ).fetchone()
    return dict(row) if row else None


# ---- human-review latches ---------------------------------------------------

def record_latch(conn, *, application_workspace_id: str, pack_revision: str, reason: str, actor: str,
                 now: datetime) -> dict[str, Any]:
    return _insert(conn, "autonomy_review_latches", {
        "id": _id("latch"), "application_workspace_id": application_workspace_id, "pack_revision": pack_revision,
        "reason": reason, "actor": actor, "created_at": to_utc_iso(now),
    })


def has_latch(conn, application_workspace_id: str, pack_revision: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM autonomy_review_latches WHERE application_workspace_id = ? AND pack_revision = ?",
        (application_workspace_id, pack_revision),
    ).fetchone() is not None


# ---- notifications (append-only event history) -----------------------------

def _notification_events(conn, account_id: str, key: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM autonomy_notification_events WHERE account_id = ?"
    params: list[Any] = [account_id]
    if key is not None:
        sql += " AND notification_key = ?"
        params.append(key)
    return conn.execute(sql + " ORDER BY seq", params).fetchall()


def _fold(events: Iterable[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    """Latest CREATED cycle per key; open until a later RESOLVED."""
    state: dict[str, dict[str, Any]] = {}
    for e in events:
        key = e["notification_key"]
        if e["event"] == "CREATED":
            state[key] = {"key": key, "kind": e["kind"], "subject_type": e["subject_type"],
                          "subject_id": e["subject_id"], "detail": json.loads(e["detail_json"]),
                          "created_at": e["created_at"], "seen": False, "open": True}
        elif key in state and e["event"] == "SEEN":
            state[key]["seen"] = True
        elif key in state and e["event"] == "RESOLVED":
            state[key]["open"] = False
    return state


def _event(conn, *, account_id: str, key: str, kind: str, subject_type: str, subject_id: str, event: str,
           detail: dict[str, Any], now: datetime) -> None:
    _insert(conn, "autonomy_notification_events", {
        "id": _id("note"), "account_id": account_id, "notification_key": key, "kind": kind,
        "subject_type": subject_type, "subject_id": subject_id, "event": event,
        "detail_json": canonical_json(detail), "created_at": to_utc_iso(now),
    })


def create_notification(conn, *, account_id: str, key: str, kind: str, subject_type: str, subject_id: str,
                        detail: dict[str, Any], now: datetime) -> bool:
    current = _fold(_notification_events(conn, account_id, key)).get(key)
    if current is not None and current["open"]:
        return False
    _event(conn, account_id=account_id, key=key, kind=kind, subject_type=subject_type, subject_id=subject_id,
           event="CREATED", detail=detail, now=now)
    return True


def _transition(conn, *, account_id: str, key: str, event: str, now: datetime) -> bool:
    current = _fold(_notification_events(conn, account_id, key)).get(key)
    if current is None or not current["open"] or (event == "SEEN" and current["seen"]):
        return False
    _event(conn, account_id=account_id, key=key, kind=current["kind"], subject_type=current["subject_type"],
           subject_id=current["subject_id"], event=event, detail={}, now=now)
    return True


def mark_seen(conn, *, account_id: str, key: str, now: datetime) -> bool:
    return _transition(conn, account_id=account_id, key=key, event="SEEN", now=now)


def resolve_notification(conn, *, account_id: str, key: str, now: datetime) -> bool:
    return _transition(conn, account_id=account_id, key=key, event="RESOLVED", now=now)


def open_notifications(conn, account_id: str) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in item.items() if k != "open"}
        for item in _fold(_notification_events(conn, account_id)).values() if item["open"]
    ]


# ---- candidate screenings, promotions, exceptions --------------------------

def _parse_screening(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for field in ("reasons", "require_user", "authority"):
        item[field] = json.loads(item.pop(f"{field}_json"))
    item["could_unlock"] = bool(item["could_unlock"])
    return item


def insert_screening(conn, *, account_id: str, search_workspace_id: str, candidate_id: str,
                     discovery_run_id: str | None, discovery_fit_id: str | None, outcome: str, reason_code: str,
                     reasons: list, require_user: list, could_unlock: bool, retry_at: datetime | None,
                     input_fingerprint: str, authority: dict, policy_version_hash: str | None,
                     subject_policy_hash: str | None, engine_version: str, now: datetime) -> dict[str, Any]:
    row = _insert(conn, "autonomy_candidate_screenings", {
        "id": _id("scr"), "account_id": account_id, "search_workspace_id": search_workspace_id,
        "candidate_id": candidate_id, "discovery_run_id": discovery_run_id, "discovery_fit_id": discovery_fit_id,
        "outcome": outcome, "reason_code": reason_code, "reasons_json": canonical_json(reasons),
        "require_user_json": canonical_json(require_user), "could_unlock": 1 if could_unlock else 0,
        "retry_at": to_utc_iso(retry_at) if retry_at else None, "input_fingerprint": input_fingerprint,
        "authority_json": canonical_json(authority), "policy_version_hash": policy_version_hash,
        "subject_policy_hash": subject_policy_hash, "engine_version": engine_version,
        "created_at": to_utc_iso(now),
    })
    return _parse_screening(conn.execute("SELECT * FROM autonomy_candidate_screenings WHERE seq = ?",
                                         (row["seq"],)).fetchone())


def latest_screening(conn, candidate_id: str) -> dict[str, Any] | None:
    return _parse_screening(conn.execute(
        "SELECT * FROM autonomy_candidate_screenings WHERE candidate_id = ? ORDER BY seq DESC LIMIT 1",
        (candidate_id,)).fetchone())


def get_screening(conn, screening_id: str) -> dict[str, Any] | None:
    return _parse_screening(conn.execute(
        "SELECT * FROM autonomy_candidate_screenings WHERE id = ?", (screening_id,)).fetchone())


def insert_promotion(conn, *, screening_id: str | None, candidate_id: str, search_workspace_id: str,
                     application_workspace_id: str, actor_type: str, actor: str, now: datetime) -> dict[str, Any]:
    return _insert(conn, "autonomy_candidate_promotions", {
        "id": _id("promo"), "screening_id": screening_id, "candidate_id": candidate_id,
        "search_workspace_id": search_workspace_id, "application_workspace_id": application_workspace_id,
        "actor_type": actor_type, "actor": actor, "created_at": to_utc_iso(now),
    })


def promotion_for_candidate(conn, candidate_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM autonomy_candidate_promotions WHERE candidate_id = ? ORDER BY seq DESC LIMIT 1",
                       (candidate_id,)).fetchone()
    return dict(row) if row else None


def open_candidate_exception(conn, *, account_id: str, search_workspace_id: str, candidate_id: str,
                             screening_id: str, items: list, now: datetime) -> dict[str, Any]:
    return _insert(conn, "autonomy_candidate_exceptions", {
        "id": _id("cexc"), "account_id": account_id, "search_workspace_id": search_workspace_id,
        "candidate_id": candidate_id, "screening_id": screening_id, "items_json": canonical_json(items),
        "created_at": to_utc_iso(now),
    })


def resolve_candidate_exception(conn, *, exception_id: str, resolution: str, actor: str, reason: str | None,
                                now: datetime) -> dict[str, Any]:
    return _insert(conn, "autonomy_candidate_exception_resolutions", {
        "id": _id("cres"), "exception_id": exception_id, "resolution": resolution, "actor": actor,
        "reason": reason, "created_at": to_utc_iso(now),
    })


def _with_resolution(conn, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["items"] = json.loads(item.pop("items_json"))
    res = conn.execute("SELECT * FROM autonomy_candidate_exception_resolutions WHERE exception_id = ? "
                       "ORDER BY seq DESC LIMIT 1", (item["id"],)).fetchone()
    item["resolution"] = dict(res) if res else None
    return item


def get_candidate_exception(conn, exception_id: str) -> dict[str, Any] | None:
    return _with_resolution(conn, conn.execute("SELECT * FROM autonomy_candidate_exceptions WHERE id = ?",
                                               (exception_id,)).fetchone())


def current_candidate_exception(conn, candidate_id: str) -> dict[str, Any] | None:
    return _with_resolution(conn, conn.execute(
        "SELECT * FROM autonomy_candidate_exceptions WHERE candidate_id = ? ORDER BY seq DESC LIMIT 1",
        (candidate_id,)).fetchone())


def open_candidate_exceptions(conn, account_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT e.* FROM autonomy_candidate_exceptions e WHERE e.account_id = ? AND NOT EXISTS ("
        "  SELECT 1 FROM autonomy_candidate_exception_resolutions r WHERE r.exception_id = e.id) ORDER BY e.seq",
        (account_id,),
    ).fetchall()
    return [_with_resolution(conn, r) for r in rows]


# ---- step-attempt log (observational only) ---------------------------------

_TERMINAL = ("SUCCEEDED", "REUSED", "FAILED", "ABANDONED")


def _parse_attempt(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["artifact_refs"] = json.loads(item.pop("artifact_refs_json"))
    item["reservation_ids"] = json.loads(item.pop("reservation_ids_json"))
    item["cost"] = json.loads(item.pop("cost_json"))
    return item


def next_attempt_no(conn, subject_type: str, subject_id: str, step_kind: str) -> int:
    row = conn.execute(
        "SELECT MAX(attempt_no) AS n FROM autonomy_prepare_steps WHERE subject_type = ? AND subject_id = ? "
        "AND step_kind = ?", (subject_type, subject_id, step_kind)).fetchone()
    return (row["n"] or 0) + 1


def start_attempt(conn, *, subject_type: str, subject_id: str, step_kind: str, attempt_no: int,
                  input_fingerprint: str, authorization_decision_id: str | None, retry_request_id: str | None,
                  lease_generation: int, worker_id: str, reservation_ids: list[str], now: datetime,
                  run_id: str | None = None) -> str:
    attempt_id = _id("att6c")
    _insert(conn, "autonomy_prepare_steps", {
        "id": _id("step"), "attempt_id": attempt_id, "subject_type": subject_type, "subject_id": subject_id,
        "step_kind": step_kind, "attempt_no": attempt_no, "event": "STARTED", "input_fingerprint": input_fingerprint,
        "authorization_decision_id": authorization_decision_id, "retry_request_id": retry_request_id,
        "lease_generation": lease_generation, "worker_id": worker_id, "run_id": run_id,
        "artifact_refs_json": "[]", "reservation_ids_json": canonical_json(list(reservation_ids)),
        "cost_json": "{}", "error_class": None, "error_code": None, "error_detail": None,
        "created_at": to_utc_iso(now),
    })
    return attempt_id


def finish_attempt(conn, *, attempt_id: str, event: str, now: datetime, artifact_refs=(), cost=None,
                   error_class: str | None = None, error_code: str | None = None,
                   error_detail: str | None = None) -> dict[str, Any]:
    if event not in _TERMINAL:
        raise ValueError(f"not a terminal attempt event: {event}")
    started = conn.execute("SELECT * FROM autonomy_prepare_steps WHERE attempt_id = ? AND event = 'STARTED'",
                           (attempt_id,)).fetchone()
    if started is None:
        raise LookupError(attempt_id)
    row = _insert(conn, "autonomy_prepare_steps", {
        "id": _id("step"), "attempt_id": attempt_id, "subject_type": started["subject_type"],
        "subject_id": started["subject_id"], "step_kind": started["step_kind"], "attempt_no": started["attempt_no"],
        "event": event, "input_fingerprint": started["input_fingerprint"],
        "authorization_decision_id": started["authorization_decision_id"],
        "retry_request_id": started["retry_request_id"], "lease_generation": started["lease_generation"],
        "worker_id": started["worker_id"], "run_id": started["run_id"],
        "artifact_refs_json": canonical_json(list(artifact_refs)),
        "reservation_ids_json": started["reservation_ids_json"], "cost_json": canonical_json(cost or {}),
        "error_class": error_class, "error_code": error_code, "error_detail": error_detail,
        "created_at": to_utc_iso(now),
    })
    return _parse_attempt(conn.execute("SELECT * FROM autonomy_prepare_steps WHERE seq = ?",
                                       (row["seq"],)).fetchone())


def attempt_rows(conn, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM autonomy_prepare_steps WHERE subject_type = ? AND subject_id = ? ORDER BY seq",
                        (subject_type, subject_id)).fetchall()
    return [_parse_attempt(r) for r in rows]


def orphaned_attempts(conn, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.* FROM autonomy_prepare_steps s WHERE s.subject_type = ? AND s.subject_id = ? "
        "AND s.event = 'STARTED' AND NOT EXISTS (SELECT 1 FROM autonomy_prepare_steps t "
        "  WHERE t.attempt_id = s.attempt_id AND t.event != 'STARTED') ORDER BY s.seq",
        (subject_type, subject_id)).fetchall()
    return [_parse_attempt(r) for r in rows]


def cycle_failures(conn, *, subject_type: str, subject_id: str, step_kind: str, input_fingerprint: str,
                   retry_request_id: str | None) -> int:
    """Consecutive TRANSIENT failures (incl. ABANDONED) of this step + input
    fingerprint within the current cycle (same retry_request_id), newest
    first, stopping at the first other terminal outcome."""
    rows = conn.execute(
        "SELECT event, error_class, retry_request_id FROM autonomy_prepare_steps WHERE subject_type = ? "
        "AND subject_id = ? AND step_kind = ? AND input_fingerprint = ? AND event != 'STARTED' ORDER BY seq DESC",
        (subject_type, subject_id, step_kind, input_fingerprint)).fetchall()
    count = 0
    for r in rows:
        if r["retry_request_id"] != retry_request_id:
            break
        if r["event"] == "ABANDONED" or (r["event"] == "FAILED" and r["error_class"] == "TRANSIENT"):
            count += 1
            continue
        break
    return count
```

- [ ] **Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_autonomy_prepare_history.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add webapp/persistence/autonomy_prepare.py tests/webapp/services/autonomy_6c_fixtures.py tests/webapp/persistence/test_autonomy_prepare_history.py
git commit -m "feat(persistence): add 6C enrolment, retry, latch, notification, screening and attempt records

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Persistence — queues and fenced leases

**Files:**
- Modify: `webapp/persistence/autonomy_prepare.py` (append)
- Test: `tests/webapp/persistence/test_autonomy_prepare_queues.py`

**Interfaces:**
- Consumes: 6B `upsert_queue_item` (`webapp.persistence.autonomy_ledger`).
- Produces (no commit): `QUEUES = {"APPLICATION": ("autonomy_queue_items", "application_workspace_id"), "CANDIDATE": ("autonomy_candidate_queue", "candidate_id")}`; `enqueue_application(conn, *, application_workspace_id, account_id, now)`; `enqueue_candidate(conn, *, candidate_id, account_id, search_workspace_id, now)`; `wake(conn, *, queue, item_id, now) -> bool`; `wake_account(conn, *, account_id, now, queues=("APPLICATION", "CANDIDATE")) -> int`; `set_dormant(conn, *, queue, item_id, now)`; `due_items(conn, *, queue, now, limit) -> list[dict]` (`item_id`, `account_id`); `acquire_lease(conn, *, queue, item_id, worker_id, now, ttl) -> int | None` (generation); `lease_is_held(conn, *, queue, item_id, worker_id, generation, now) -> bool`; `finalize_lease(conn, *, queue, item_id, worker_id, generation, now, next_eligible_at) -> bool`; `release_lease(conn, *, queue, item_id, worker_id, generation, now) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/webapp/persistence/test_autonomy_prepare_queues.py
from __future__ import annotations

from datetime import timedelta

from webapp.persistence import autonomy_prepare as ap
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, NOW, conn, make_workspace  # noqa: F401

TTL = timedelta(minutes=12)


def _app(conn):
    ws = make_workspace(conn)
    ap.enqueue_application(conn, application_workspace_id=ws, account_id=ACCOUNT, now=NOW)
    return ws


def test_due_and_lease_generation_increments(conn):
    ws = _app(conn)
    assert [i["item_id"] for i in ap.due_items(conn, queue="APPLICATION", now=NOW, limit=10)] == [ws]
    gen = ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", now=NOW, ttl=TTL)
    assert gen == 1
    assert ap.due_items(conn, queue="APPLICATION", now=NOW, limit=10) == []  # leased
    assert ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w2", now=NOW, ttl=TTL) is None


def test_finalize_requires_holder_generation_and_unexpired_lease(conn):
    ws = _app(conn)
    gen = ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", now=NOW, ttl=TTL)
    assert ap.finalize_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w2", generation=gen, now=NOW,
                             next_eligible_at=None) is False
    assert ap.finalize_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", generation=gen + 1, now=NOW,
                             next_eligible_at=None) is False
    assert ap.finalize_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", generation=gen, now=NOW,
                             next_eligible_at=None) is True
    assert ap.due_items(conn, queue="APPLICATION", now=NOW + timedelta(days=1), limit=10) == []  # dormant


def test_expired_lease_cannot_finalize_even_if_not_retaken(conn):
    ws = _app(conn)
    gen = ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", now=NOW, ttl=TTL)
    late = NOW + TTL + timedelta(seconds=1)
    assert ap.lease_is_held(conn, queue="APPLICATION", item_id=ws, worker_id="w1", generation=gen, now=late) is False
    assert ap.finalize_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", generation=gen, now=late,
                             next_eligible_at=None) is False


def test_expired_lease_is_retaken_with_a_new_generation(conn):
    ws = _app(conn)
    g1 = ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", now=NOW, ttl=TTL)
    g2 = ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w2", now=NOW + TTL + timedelta(seconds=1),
                          ttl=TTL)
    assert g2 == g1 + 1
    assert ap.finalize_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", generation=g1,
                             now=NOW + TTL + timedelta(seconds=2), next_eligible_at=None) is False


def test_wake_reactivates_dormant_items_in_both_queues(conn):
    ws = _app(conn)
    ap.enqueue_candidate(conn, candidate_id="cand_1", account_id=ACCOUNT, search_workspace_id="search_default", now=NOW)
    ap.set_dormant(conn, queue="APPLICATION", item_id=ws, now=NOW)
    ap.set_dormant(conn, queue="CANDIDATE", item_id="cand_1", now=NOW)
    assert ap.due_items(conn, queue="APPLICATION", now=NOW, limit=5) == []
    assert ap.wake_account(conn, account_id=ACCOUNT, now=NOW) == 2
    assert [i["item_id"] for i in ap.due_items(conn, queue="CANDIDATE", now=NOW, limit=5)] == ["cand_1"]


def test_release_keeps_eligibility(conn):
    ws = _app(conn)
    gen = ap.acquire_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", now=NOW, ttl=TTL)
    assert ap.release_lease(conn, queue="APPLICATION", item_id=ws, worker_id="w1", generation=gen, now=NOW) is True
    assert [i["item_id"] for i in ap.due_items(conn, queue="APPLICATION", now=NOW, limit=5)] == [ws]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_autonomy_prepare_queues.py -q`
Expected: FAIL — `AttributeError: enqueue_application`.

- [ ] **Step 3: Implement (append to `webapp/persistence/autonomy_prepare.py`)**

Add `from datetime import timedelta` to the imports and `from product.autonomy_contract import Capability` and `from webapp.persistence.autonomy_ledger import upsert_queue_item`.

```python
# ---- queues and fenced leases (mutable coordination state) -----------------

QUEUES = {
    "APPLICATION": ("autonomy_queue_items", "application_workspace_id"),
    "CANDIDATE": ("autonomy_candidate_queue", "candidate_id"),
}


def enqueue_application(conn, *, application_workspace_id: str, account_id: str, now: datetime) -> None:
    upsert_queue_item(conn, application_workspace_id=application_workspace_id, account_id=account_id,
                      next_stage=Capability.PREPARE, next_eligible_at=now, now=now)


def enqueue_candidate(conn, *, candidate_id: str, account_id: str, search_workspace_id: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO autonomy_candidate_queue (candidate_id, account_id, search_workspace_id, next_eligible_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(candidate_id) DO UPDATE SET "
        "next_eligible_at = excluded.next_eligible_at, updated_at = excluded.updated_at",
        (candidate_id, account_id, search_workspace_id, to_utc_iso(now), to_utc_iso(now)),
    )


def wake(conn, *, queue: str, item_id: str, now: datetime) -> bool:
    table, key = QUEUES[queue]
    cur = conn.execute(f"UPDATE {table} SET next_eligible_at = ?, updated_at = ? WHERE {key} = ?",
                       (to_utc_iso(now), to_utc_iso(now), item_id))
    return cur.rowcount == 1


def wake_account(conn, *, account_id: str, now: datetime, queues=("APPLICATION", "CANDIDATE")) -> int:
    total = 0
    for queue in queues:
        table, _ = QUEUES[queue]
        total += conn.execute(f"UPDATE {table} SET next_eligible_at = ?, updated_at = ? WHERE account_id = ?",
                              (to_utc_iso(now), to_utc_iso(now), account_id)).rowcount
    return total


def set_dormant(conn, *, queue: str, item_id: str, now: datetime) -> None:
    table, key = QUEUES[queue]
    conn.execute(f"UPDATE {table} SET next_eligible_at = NULL, updated_at = ? WHERE {key} = ?",
                 (to_utc_iso(now), item_id))


def due_items(conn, *, queue: str, now: datetime, limit: int) -> list[dict[str, Any]]:
    table, key = QUEUES[queue]
    t = to_utc_iso(now)
    rows = conn.execute(
        f"SELECT {key} AS item_id, account_id FROM {table} WHERE next_eligible_at IS NOT NULL "
        f"AND next_eligible_at <= ? AND (lease_holder IS NULL OR lease_expires_at <= ?) "
        f"ORDER BY next_eligible_at, {key} LIMIT ?", (t, t, limit)).fetchall()
    return [dict(r) for r in rows]


def acquire_lease(conn, *, queue: str, item_id: str, worker_id: str, now: datetime, ttl: timedelta) -> int | None:
    table, key = QUEUES[queue]
    t = to_utc_iso(now)
    cur = conn.execute(
        f"UPDATE {table} SET lease_holder = ?, lease_generation = lease_generation + 1, lease_expires_at = ?, "
        f"updated_at = ? WHERE {key} = ? AND next_eligible_at IS NOT NULL AND next_eligible_at <= ? "
        f"AND (lease_holder IS NULL OR lease_expires_at <= ?)",
        (worker_id, to_utc_iso(now + ttl), t, item_id, t, t))
    if cur.rowcount != 1:
        return None
    return conn.execute(f"SELECT lease_generation FROM {table} WHERE {key} = ?", (item_id,)).fetchone()[0]


def lease_is_held(conn, *, queue: str, item_id: str, worker_id: str, generation: int, now: datetime) -> bool:
    table, key = QUEUES[queue]
    return conn.execute(
        f"SELECT 1 FROM {table} WHERE {key} = ? AND lease_holder = ? AND lease_generation = ? AND lease_expires_at > ?",
        (item_id, worker_id, generation, to_utc_iso(now))).fetchone() is not None


def finalize_lease(conn, *, queue: str, item_id: str, worker_id: str, generation: int, now: datetime,
                   next_eligible_at: datetime | None) -> bool:
    table, key = QUEUES[queue]
    cur = conn.execute(
        f"UPDATE {table} SET lease_holder = NULL, lease_expires_at = NULL, next_eligible_at = ?, updated_at = ? "
        f"WHERE {key} = ? AND lease_holder = ? AND lease_generation = ? AND lease_expires_at > ?",
        (to_utc_iso(next_eligible_at) if next_eligible_at else None, to_utc_iso(now), item_id, worker_id,
         generation, to_utc_iso(now)))
    return cur.rowcount == 1


def release_lease(conn, *, queue: str, item_id: str, worker_id: str, generation: int, now: datetime) -> bool:
    table, key = QUEUES[queue]
    cur = conn.execute(
        f"UPDATE {table} SET lease_holder = NULL, lease_expires_at = NULL, updated_at = ? "
        f"WHERE {key} = ? AND lease_holder = ? AND lease_generation = ? AND lease_expires_at > ?",
        (to_utc_iso(now), item_id, worker_id, generation, to_utc_iso(now)))
    return cur.rowcount == 1
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_autonomy_prepare_queues.py tests/webapp/persistence/test_autonomy_prepare_history.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/persistence/autonomy_prepare.py tests/webapp/persistence/test_autonomy_prepare_queues.py
git commit -m "feat(persistence): add 6C queues with fenced, expiry-aware leases

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Ledger — reservation subjects, capped admission, exactly-once settlement

**Files:**
- Modify: `webapp/persistence/autonomy_ledger.py`
- Test: `tests/webapp/persistence/test_autonomy_ledger_6c.py`

**Interfaces:**
- Produces:
  - `decision_inputs_payload(ctx) -> dict` (public name for the existing `_inputs_payload`; keep `_inputs_payload = decision_inputs_payload`)
  - `try_reserve(..., subject_type=None, subject_id=None)` and `reserve_budget(..., subject_type=None, subject_id=None)` (new keyword args, default unchanged behaviour)
  - `reserve_within_cap(conn, *, account_id, counter_name, window_key, cap: Decimal, amount: Decimal, subject_type, subject_id, now, grant_id=None) -> str | None`
  - `budget_usage` now counts `RESERVED` at `amount` and `CONSUMED` at `COALESCE(settled_amount, amount)`
  - `settlement_ref_for(attempt_id, reservation_id) -> str`
  - `settle_reservation(conn, *, reservation_id, attempt_id, amount: Decimal, now) -> bool`
  - `get_reservation(conn, reservation_id) -> dict | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/webapp/persistence/test_autonomy_ledger_6c.py
from __future__ import annotations

from decimal import Decimal

import pytest

from webapp.persistence.autonomy_ledger import (
    budget_usage, get_reservation, reserve_within_cap, settle_reservation, settlement_ref_for, try_reserve,
)
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, NOW, conn  # noqa: F401

DAY = dict(account_id=ACCOUNT, counter_name="budget:LLM:day", window_key="2026-09-24")


def test_capped_admission_counts_unsettled_reservations_at_full_amount(conn):
    r1 = reserve_within_cap(conn, cap=Decimal("1.00"), amount=Decimal("0.60"), subject_type="CANDIDATE",
                            subject_id="c1", now=NOW, **DAY)
    assert r1 and get_reservation(conn, r1)["subject_type"] == "CANDIDATE"
    assert reserve_within_cap(conn, cap=Decimal("1.00"), amount=Decimal("0.60"), subject_type="CANDIDATE",
                              subject_id="c2", now=NOW, **DAY) is None
    assert budget_usage(conn, **DAY) == Decimal("0.60")


def test_settlement_is_exactly_once_immutable_and_charges_actual(conn):
    r1 = reserve_within_cap(conn, cap=Decimal("1.00"), amount=Decimal("0.60"), subject_type="CANDIDATE",
                            subject_id="c1", now=NOW, **DAY)
    assert settle_reservation(conn, reservation_id=r1, attempt_id="att_1", amount=Decimal("0.10"), now=NOW) is True
    assert budget_usage(conn, **DAY) == Decimal("0.10")  # usage reflects actual after settlement
    assert settle_reservation(conn, reservation_id=r1, attempt_id="att_1", amount=Decimal("0.05"), now=NOW) is False
    row = get_reservation(conn, r1)
    assert (row["settled_amount"], row["status"]) == ("0.10", "CONSUMED")
    assert row["settlement_ref"] == settlement_ref_for("att_1", r1)


def test_two_reservations_of_one_attempt_get_distinct_stable_refs(conn):
    a = reserve_within_cap(conn, cap=Decimal("5"), amount=Decimal("1"), subject_type="APPLICATION",
                           subject_id="ws1", now=NOW, **DAY)
    b = reserve_within_cap(conn, account_id=ACCOUNT, counter_name="budget:LLM:application", window_key="ws1",
                           cap=Decimal("5"), amount=Decimal("1"), subject_type="APPLICATION", subject_id="ws1", now=NOW)
    assert settlement_ref_for("att_9", a) == settlement_ref_for("att_9", a) != settlement_ref_for("att_9", b)
    assert settle_reservation(conn, reservation_id=a, attempt_id="att_9", amount=Decimal("1"), now=NOW)
    assert settle_reservation(conn, reservation_id=b, attempt_id="att_9", amount=Decimal("1"), now=NOW)


def test_overage_is_recorded_truthfully(conn):
    r = reserve_within_cap(conn, cap=Decimal("1"), amount=Decimal("0.20"), subject_type="CANDIDATE", subject_id="c",
                           now=NOW, **DAY)
    assert settle_reservation(conn, reservation_id=r, attempt_id="att", amount=Decimal("0.35"), now=NOW)
    assert budget_usage(conn, **DAY) == Decimal("0.35")  # never clamped to the reservation


def test_existing_try_reserve_is_unchanged_and_accepts_subjects(conn):
    kw = dict(account_id=ACCOUNT, counter_name="promotions:day", window_key="2026-09-24", limit=1, now=NOW)
    first = try_reserve(conn, subject_type="CANDIDATE", subject_id="c1", **kw)
    assert first and try_reserve(conn, subject_type="CANDIDATE", subject_id="c2", **kw) is None
    with pytest.raises(Exception):
        try_reserve(conn, subject_type="CANDIDATE", **{**kw, "window_key": "other"})  # pair enforced by trigger
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_autonomy_ledger_6c.py -q`
Expected: FAIL — `ImportError: reserve_within_cap`.

- [ ] **Step 3: Implement in `webapp/persistence/autonomy_ledger.py`**

1. Rename `_inputs_payload` to `decision_inputs_payload` and add `_inputs_payload = decision_inputs_payload` directly below it (keeps existing callers working).
2. Replace `budget_usage` and `_reserve`, extend `try_reserve`/`reserve_budget`, and add the new functions:

```python
def budget_usage(conn, *, account_id: str, counter_name: str, window_key: str) -> Decimal:
    """RESERVED rows count at their full reserved amount (admission control);
    CONSUMED rows count at their settled actual amount when settled."""
    rows = conn.execute(
        "SELECT amount, settled_amount, status FROM limit_reservations WHERE account_id = ? AND counter_name = ? "
        "AND window_key = ? AND status IN ('RESERVED', 'CONSUMED')", (account_id, counter_name, window_key),
    ).fetchall()
    total = Decimal("0")
    for r in rows:
        value = r["settled_amount"] if r["status"] == "CONSUMED" and r["settled_amount"] is not None else r["amount"]
        total += Decimal(value)
    return total


def _reserve(conn, *, account_id, counter_name, window_key, amount: str, grant_id, attempt_id, now,
             subject_type=None, subject_id=None) -> str:
    row = _insert(conn, "limit_reservations", {
        "id": _id("res"), "account_id": account_id, "counter_name": counter_name, "window_key": window_key,
        "amount": amount, "grant_id": grant_id, "attempt_id": attempt_id, "status": "RESERVED",
        "created_at": to_utc_iso(now), "updated_at": to_utc_iso(now),
        "subject_type": subject_type, "subject_id": subject_id,
    })
    return row["id"]
```

In `try_reserve` add parameters `subject_type: str | None = None, subject_id: str | None = None` and pass them to `_reserve`; same for `reserve_budget`. Then add:

```python
def reserve_within_cap(conn, *, account_id: str, counter_name: str, window_key: str, cap: Decimal, amount: Decimal,
                       subject_type: str, subject_id: str, now: datetime, grant_id: str | None = None) -> str | None:
    """Atomic under the caller's BEGIN IMMEDIATE: admit only if the reserved
    hard maximum fits under the cap."""
    if budget_usage(conn, account_id=account_id, counter_name=counter_name, window_key=window_key) + amount > cap:
        return None
    return _reserve(conn, account_id=account_id, counter_name=counter_name, window_key=window_key,
                    amount=str(amount), grant_id=grant_id, attempt_id=None, now=now,
                    subject_type=subject_type, subject_id=subject_id)


def settlement_ref_for(attempt_id: str, reservation_id: str) -> str:
    return canonical_hash("autonomy-settlement", "v1", {"attempt_id": attempt_id, "reservation_id": reservation_id})


def settle_reservation(conn, *, reservation_id: str, attempt_id: str, amount: Decimal, now: datetime) -> bool:
    """Exactly once; a settled row is immutable (spec §11.3)."""
    cur = conn.execute(
        "UPDATE limit_reservations SET settled_amount = ?, settlement_ref = ?, status = 'CONSUMED', updated_at = ? "
        "WHERE id = ? AND settled_amount IS NULL AND status = 'RESERVED'",
        (str(amount), settlement_ref_for(attempt_id, reservation_id), to_utc_iso(now), reservation_id),
    )
    return cur.rowcount == 1


def get_reservation(conn, reservation_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM limit_reservations WHERE id = ?", (reservation_id,)).fetchone()
    return dict(row) if row else None
```

- [ ] **Step 4: Run tests (new + all 6B ledger/service suites)**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence tests/webapp/services/test_autonomy_decide.py tests/webapp/services/test_autonomy_preclick.py tests/webapp/services/test_autonomy_context.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/persistence/autonomy_ledger.py tests/webapp/persistence/test_autonomy_ledger_6c.py
git commit -m "feat(persistence): add reservation subjects, capped admission and exactly-once settlement

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Review provenance, structured outstanding items, system Gate 4 variant

**Files:**
- Modify: `webapp/persistence/review.py` (`save_review_decision`)
- Modify: `webapp/services/application_pack.py` (outstanding items; confirm refactor)
- Test: `tests/webapp/services/test_application_pack_6c.py`

**Interfaces:**
- Produces:
  - `save_review_decision(conn, *, …, decision_provenance="USER", system_basis=None, commit=True)`; `SYSTEM_AUTO_CONFIRMED = "SYSTEM_AUTO_CONFIRMED"` in `webapp.persistence.review`
  - `class OutstandingReviewItems(PipelineError)` with `.items: list[dict]` (`item_type`, `item_id`, `source_artifact_id`, `source`), same message as before
  - `list_outstanding_review_items(conn, workspace_id, *, extensions_dir, account_id) -> list[dict]`
  - `USER_GATE4_NOTE = "Application pack reviewed and confirmed by user."`, `SYSTEM_GATE4_NOTE = "Application pack system-confirmed under standing PREPARE authority (6B representation rule)."`
  - `system_confirm_application_pack(conn, workspace_id, *, effective_date, documents_root, extensions_dir, account_id, precheck) -> dict` — `precheck(pack: dict, profile_artifact: dict) -> None` runs **inside** the `BEGIN IMMEDIATE` transaction after the pack is assembled and must raise to abort (rollback, nothing written).

- [ ] **Step 1: Write the failing tests**

```python
# tests/webapp/services/test_application_pack_6c.py
from __future__ import annotations

import pytest

from webapp.persistence.review import SYSTEM_AUTO_CONFIRMED, save_review_decision
from webapp.services.application_pack import (
    SYSTEM_GATE4_NOTE, OutstandingReviewItems, list_outstanding_review_items, system_confirm_application_pack,
)
from webapp.services.pipeline import PipelineError
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, conn, make_workspace  # noqa: F401


def test_system_provenance_requires_a_basis_and_acknowledgement(conn):
    from webapp.persistence.artifacts import save_artifact
    ws = make_workspace(conn)
    art = save_artifact(conn, workspace_id=ws, artifact_type="application_intelligence_result", payload={"x": 1})
    common = dict(workspace_id=ws, review_item_type="content_unit", source_artifact_id=art["id"], domain_item_id="u1")
    with pytest.raises(ValueError):
        save_review_decision(conn, disposition="acknowledged_and_proceed", decision_provenance=SYSTEM_AUTO_CONFIRMED,
                             **common)
    with pytest.raises(ValueError):
        save_review_decision(conn, disposition="omit_from_positioning", decision_provenance=SYSTEM_AUTO_CONFIRMED,
                             system_basis={"reason": "grounded_ready_unit", "item_content_hash": "h",
                                           "pack_revision": "r"}, **common)
    with pytest.raises(ValueError):
        save_review_decision(conn, disposition="acknowledged_and_proceed", system_basis={"reason": "x"}, **common)
    row = save_review_decision(conn, disposition="acknowledged_and_proceed", decision_provenance=SYSTEM_AUTO_CONFIRMED,
                               system_basis={"reason": "grounded_ready_unit", "item_content_hash": "h",
                                             "pack_revision": "r"}, **common)
    assert row["decision_provenance"] == SYSTEM_AUTO_CONFIRMED and '"pack_revision"' in row["system_basis_json"]
    assert save_review_decision(conn, disposition="acknowledged_and_proceed", **common)["decision_provenance"] == "USER"


def test_outstanding_items_are_structured_and_subclass_pipeline_error(prepared_chain):
    conn, ws, settings = prepared_chain
    items = list_outstanding_review_items(conn, ws, extensions_dir=settings.extensions_dir, account_id=ACCOUNT)
    assert items and {"item_type", "item_id", "source_artifact_id", "source"} <= set(items[0])
    assert issubclass(OutstandingReviewItems, PipelineError)
    assert any(i["item_type"] == "content_unit" for i in items)


def test_system_confirm_aborts_when_precheck_raises_and_writes_nothing(ready_chain):
    conn, ws, settings = ready_chain
    before = conn.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0]

    def refuse(pack, profile_artifact):
        raise PipelineError("authority changed")
    with pytest.raises(PipelineError):
        system_confirm_application_pack(conn, ws, effective_date="2026-09-24", documents_root=settings.documents_root,
                                        extensions_dir=settings.extensions_dir, account_id=ACCOUNT, precheck=refuse)
    assert conn.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0] == before


def test_system_confirm_writes_system_note(ready_chain):
    conn, ws, settings = ready_chain
    out = system_confirm_application_pack(conn, ws, effective_date="2026-09-24", documents_root=settings.documents_root,
                                          extensions_dir=settings.extensions_dir, account_id=ACCOUNT,
                                          precheck=lambda pack, profile: None)
    assert out["workflow_event"]["note"] == SYSTEM_GATE4_NOTE
    assert out["workflow_event"]["new_status"] == "drafted"
```

Append to `tests/webapp/services/autonomy_6c_fixtures.py` the two chain fixtures (a real job workspace built through the production pipeline with the acceptance fakes, no HTTP):

```python
import pytest


def build_chain(tmp_path, *, ai_units=None, decide=False):
    """A real application workspace driven through understand -> fit ->
    intelligence with the acceptance-suite fakes, returning (client, app,
    settings, workspace_id). decide=True also records USER review decisions
    for every item (the full-journey helper)."""
    from tests.webapp.fixtures.acceptance.fixtures import completion_ready_content_units
    from tests.webapp.test_full_journey_acceptance import _build_chain, _decide_current_review_surface
    client, app, settings, ws = _build_chain(tmp_path, ai_units=ai_units or completion_ready_content_units())
    if decide:
        _decide_current_review_surface(client, ws)
    return client, app, settings, ws


@pytest.fixture
def prepared_chain(tmp_path):
    from webapp.persistence.db import connect
    from tests.webapp.test_full_journey_acceptance import _close
    client, _, settings, ws = build_chain(tmp_path)
    conn = connect(settings.db_path)
    yield conn, ws, settings
    conn.close()
    _close(client)


@pytest.fixture
def ready_chain(tmp_path):
    from webapp.persistence.db import connect
    from tests.webapp.test_full_journey_acceptance import _close
    client, _, settings, ws = build_chain(tmp_path, decide=True)
    conn = connect(settings.db_path)
    yield conn, ws, settings
    conn.close()
    _close(client)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_application_pack_6c.py -q`
Expected: FAIL — `ImportError: SYSTEM_AUTO_CONFIRMED`.

- [ ] **Step 3: Implement provenance in `webapp/persistence/review.py`**

Add `import json`, the constant, and replace `save_review_decision`:

```python
SYSTEM_AUTO_CONFIRMED = "SYSTEM_AUTO_CONFIRMED"
_PROVENANCES = ("USER", SYSTEM_AUTO_CONFIRMED)
_SYSTEM_BASIS_KEYS = {"reason", "item_content_hash", "pack_revision"}


def save_review_decision(
    conn: sqlite3.Connection, *, workspace_id: str, review_item_type: str, source_artifact_id: str,
    domain_item_id: str | None, disposition: str, note: str | None = None,
    commit: bool = True, decision_provenance: str = "USER", system_basis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition: {disposition!r}")
    if decision_provenance not in _PROVENANCES:
        raise ValueError(f"unknown decision provenance: {decision_provenance!r}")
    if decision_provenance == SYSTEM_AUTO_CONFIRMED:
        # Bundle 6C: a system decision only ever accepts a mechanically
        # verified item, and always records why (spec §8.2).
        if disposition != "acknowledged_and_proceed":
            raise ValueError("a system review decision may only acknowledge an item")
        if not isinstance(system_basis, dict) or set(system_basis) != _SYSTEM_BASIS_KEYS:
            raise ValueError(f"a system review decision needs a basis with exactly {sorted(_SYSTEM_BASIS_KEYS)}")
    elif system_basis is not None:
        raise ValueError("a user review decision has no system basis")
    decision_id = f"rev_{uuid.uuid4().hex[:20]}"
    conn.execute(
        "INSERT INTO review_decisions "
        "(id, workspace_id, review_item_type, source_artifact_id, domain_item_id, disposition, note, created_at, "
        "decision_provenance, system_basis_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (decision_id, workspace_id, review_item_type, source_artifact_id, domain_item_id, disposition, note, _now(),
         decision_provenance, json.dumps(system_basis, sort_keys=True) if system_basis is not None else None),
    )
    if commit:
        conn.commit()
    return dict(conn.execute("SELECT * FROM review_decisions WHERE id = ?", (decision_id,)).fetchone())
```

- [ ] **Step 4: Implement structured outstanding items in `webapp/services/application_pack.py`**

Add below the constants:

```python
USER_GATE4_NOTE = "Application pack reviewed and confirmed by user."
SYSTEM_GATE4_NOTE = "Application pack system-confirmed under standing PREPARE authority (6B representation rule)."


class OutstandingReviewItems(PipelineError):
    """Gate 4 cannot proceed: these review items have no usable decision.
    Same message as before; .items carries them structurally (6C)."""

    def __init__(self, workspace_id: str, items: list[dict[str, Any]]):
        self.items = items
        labels = sorted({f"{i['item_type']}:{i['item_id']}" for i in items})
        super().__init__(f"workspace {workspace_id} has outstanding review items: {labels}")
```

In `_build_application_pack_with_profile`:
- change `outstanding: list[str] = []` to `outstanding: list[dict[str, Any]] = []`;
- give `adjudicate` a keyword `source_artifact_id: str` and pass `fit_artifact["id"]` from the gate-flag, question and match calls and `intelligence_artifact["id"]` from `select_units`; give `adjudicate_gate1` `profile_artifact["id"]`;
- replace every `outstanding.append(f"{item_type}:{item_id}")` with
  `outstanding.append({"item_type": item_type, "item_id": item_id, "source_artifact_id": <that artifact id>, "source": source})`;
- in `select_units`, replace `outstanding.append(f"content_unit:{unit_id or '<missing>'}")` with
  `outstanding.append({"item_type": "content_unit", "item_id": unit_id or "<missing>", "source_artifact_id": intelligence_artifact["id"], "source": unit})`;
- replace the final `raise PipelineError(...)` with `raise OutstandingReviewItems(workspace_id, outstanding)`.

Add:

```python
def list_outstanding_review_items(
    conn: sqlite3.Connection, workspace_id: str, *, extensions_dir: Path | str, account_id: str,
) -> list[dict[str, Any]]:
    try:
        _build_application_pack_with_profile(conn, workspace_id, extensions_dir=extensions_dir, account_id=account_id)
    except OutstandingReviewItems as exc:
        return exc.items
    return []
```

- [ ] **Step 5: Refactor confirmation into one inner function with a note and a precheck**

Move the body of the legacy branch of `confirm_application_pack` into:

```python
def _confirm_application_pack_v1(
    conn: sqlite3.Connection, workspace_id: str, *, effective_date: str, documents_root: Path | str,
    extensions_dir: Path | str, account_id: str, note: str,
    precheck: Callable[[dict[str, Any], dict[str, Any]], None] | None,
) -> dict[str, Any]:
```

— identical to the current code except: after `validate_application_pack_v1(pack, source_profile_artifact=profile_artifact)` insert `if precheck is not None: precheck(pack, profile_artifact)`, and the `record_status_change(..., note=note, ...)` uses the parameter. (Add `from typing import Callable`.) Then:

```python
def confirm_application_pack(conn, workspace_id, *, effective_date, documents_root=Path("documents"),
                             extensions_dir=Path("extensions"), account_id=DEFAULT_ACCOUNT_ID,
                             document_selection_revisions=None):
    """Gate 4: the sole webapp route to ``drafted`` and an exact pack binding."""
    if document_selection_revisions is not None:
        return _confirm_application_pack_v2(
            conn, workspace_id, effective_date=effective_date, documents_root=Path(documents_root),
            account_id=account_id, selection_revisions=document_selection_revisions,
        )
    return _confirm_application_pack_v1(
        conn, workspace_id, effective_date=effective_date, documents_root=documents_root,
        extensions_dir=extensions_dir, account_id=account_id, note=USER_GATE4_NOTE, precheck=None,
    )


def system_confirm_application_pack(conn, workspace_id, *, effective_date, documents_root, extensions_dir,
                                    account_id, precheck):
    """Bundle 6C system Gate 4 (spec §8.3): the same pack assembly and
    validation as the user path, a system note, and caller rechecks run
    inside the same BEGIN IMMEDIATE transaction."""
    return _confirm_application_pack_v1(
        conn, workspace_id, effective_date=effective_date, documents_root=documents_root,
        extensions_dir=extensions_dir, account_id=account_id, note=SYSTEM_GATE4_NOTE, precheck=precheck,
    )
```

- [ ] **Step 6: Run tests (new + every pack/review/journey suite)**

Run: `.venv/Scripts/python -m pytest tests/webapp/services/test_application_pack_6c.py tests/webapp/services/test_application_pack.py tests/webapp/services/test_application_pack_staleness_characterization.py tests/webapp/test_full_journey_acceptance.py tests/webapp/api/test_review_routes.py -q -p no:cacheprovider`
Expected: PASS. (Existing assertions on the outstanding-items message still hold because the message format is unchanged.)

- [ ] **Step 7: Commit**

```bash
git add webapp/persistence/review.py webapp/services/application_pack.py tests/webapp/services/test_application_pack_6c.py tests/webapp/services/autonomy_6c_fixtures.py
git commit -m "feat(review): add review provenance, structured outstanding items and a system Gate 4 variant

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 8: Pure preparation logic — step derivation, mechanical review, pack revision, retry policy

**Files:**
- Create: `product/prepare_steps.py`
- Test: `tests/product/test_prepare_steps.py`

**Interfaces:**
- Consumes: `canonical_hash` from `product.autonomy_contract`.
- Produces:
  - `StepKind(str, Enum)`: `EVALUATE, UNDERSTAND, FIT, INTELLIGENCE, SYSTEM_REVIEW, GATE4`; `PAID_STEPS`; `POST_DRAFT_STATUSES`
  - `PrepareSnapshot(workflow_status, profile_available, understanding_current, fit_current, intelligence_current, pack_current, mechanically_acceptable, judgment_outstanding, latched)` (frozen)
  - `NextStep(kind: str, step: StepKind | None = None, reason: str = "")` — `kind` ∈ `RUN | DONE | PREPARED | NEEDS_USER`
  - `next_prepare_step(snapshot) -> NextStep`
  - `SystemReviewVerdict(item_type, item_id, source_artifact_id, reason, item_content_hash)`; `item_content_hash(item) -> str`; `grounded_claim_ids(profile) -> frozenset[str]`; `mechanical_review(item, profile) -> SystemReviewVerdict | None`
  - `pack_revision(profile_content_id, fit_content_id, intelligence_content_id) -> str`
  - `ErrorClass(str, Enum)`: `TRANSIENT, HUMAN_FIXABLE, INTERNAL`; `MAX_ATTEMPTS_PER_CYCLE = 4`; `retry_delay_seconds(failures_in_cycle, delays, rng, retry_after=None) -> float | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/product/test_prepare_steps.py
from __future__ import annotations

import random
from decimal import Decimal

import pytest
from hypothesis import given, settings, strategies as st

from product.prepare_steps import (
    MAX_ATTEMPTS_PER_CYCLE, NextStep, PrepareSnapshot, StepKind, grounded_claim_ids, item_content_hash,
    mechanical_review, next_prepare_step, pack_revision, retry_delay_seconds,
)

BASE = dict(workflow_status=None, profile_available=True, understanding_current=True, fit_current=True,
            intelligence_current=True, pack_current=False, mechanically_acceptable=0, judgment_outstanding=0,
            latched=False)


def snap(**kw):
    return PrepareSnapshot(**{**BASE, **kw})


@pytest.mark.parametrize("kw, expected", [
    (dict(workflow_status="applied", profile_available=False), NextStep("DONE", None, "submitted")),
    (dict(profile_available=False), NextStep("NEEDS_USER", None, "profile_missing")),
    (dict(understanding_current=False, fit_current=False), NextStep("RUN", StepKind.UNDERSTAND)),
    (dict(fit_current=False, intelligence_current=False), NextStep("RUN", StepKind.FIT)),
    (dict(intelligence_current=False), NextStep("RUN", StepKind.INTELLIGENCE)),
    (dict(pack_current=True, mechanically_acceptable=2), NextStep("PREPARED")),
    (dict(mechanically_acceptable=2, judgment_outstanding=1), NextStep("RUN", StepKind.SYSTEM_REVIEW)),
    (dict(judgment_outstanding=1), NextStep("NEEDS_USER", None, "pack_review")),
    (dict(latched=True, mechanically_acceptable=2), NextStep("NEEDS_USER", None, "human_review_latched")),
    (dict(latched=True), NextStep("NEEDS_USER", None, "human_review_latched")),
    (dict(), NextStep("RUN", StepKind.GATE4)),
])
def test_next_step_order(kw, expected):
    got = next_prepare_step(snap(**kw))
    assert (got.kind, got.step) == (expected.kind, expected.step)
    if expected.reason:
        assert got.reason == expected.reason


def test_done_is_checked_before_profile():
    for status in ("applied", "interview", "offer", "hired", "rejected", "no_response", "offer_declined", "withdrawn"):
        assert next_prepare_step(snap(workflow_status=status, profile_available=False)).kind == "DONE"
    assert next_prepare_step(snap(workflow_status="drafted")).kind == "RUN"  # drafted is still preparable


PROFILE = {
    "claims": [
        {"id": "clm_ok", "concept_id": "cpt_a", "placeholder": False},
        {"id": "clm_ph", "concept_id": "cpt_b", "placeholder": True},
        {"id": "clm_cf", "concept_id": "cpt_c", "placeholder": False},
    ],
    "conflicts": [{"id": "cf1", "concept_id": "cpt_c"}],
}


def unit(**kw):
    src = {"unit_id": "u1", "status": "READY", "text": "Led drilling fluids programmes.",
           "profile_evidence_ids": ["clm_ok"], **kw}
    return {"item_type": "content_unit", "item_id": "u1", "source_artifact_id": "art_i", "source": src}


def test_grounded_claims_exclude_placeholders_and_conflicts():
    assert grounded_claim_ids(PROFILE) == frozenset({"clm_ok"})


def test_only_grounded_ready_units_are_accepted():
    verdict = mechanical_review(unit(), PROFILE)
    assert verdict and verdict.reason == "grounded_ready_unit"
    assert verdict.item_content_hash == item_content_hash(unit())
    for bad in (unit(status="NEEDS_REVIEW"), unit(text=""), unit(profile_evidence_ids=[]),
                unit(profile_evidence_ids=["clm_ph"]), unit(profile_evidence_ids=["clm_cf"]),
                unit(profile_evidence_ids=["clm_ok", "clm_missing"])):
        assert mechanical_review(bad, PROFILE) is None


@settings(max_examples=200, deadline=None, derandomize=True)
@given(item_type=st.sampled_from(["functionally_equivalent_match", "transferable_match", "gate_flag",
                                  "human_judgment_question", "profile_conflict", "profile_placeholder"]))
def test_judgment_item_types_are_never_accepted(item_type):
    item = {**unit(), "item_type": item_type}
    assert mechanical_review(item, PROFILE) is None


@settings(max_examples=200, deadline=None, derandomize=True)
@given(text=st.text(min_size=1, max_size=40))
def test_any_content_change_changes_the_hash(text):
    if text == unit()["source"]["text"]:
        return
    assert item_content_hash(unit(text=text)) != item_content_hash(unit())


def test_item_hash_tolerates_floats():
    assert item_content_hash(unit(score=0.5)) == item_content_hash(unit(score=Decimal("0.5")))


def test_pack_revision_binds_all_three_sources():
    base = pack_revision("p1", "f1", "i1")
    assert base != pack_revision("p2", "f1", "i1") != pack_revision("p1", "f2", "i1") != pack_revision("p1", "f1", "i2")
    assert base == pack_revision("p1", "f1", "i1")


def test_retry_cycle_is_three_retries_four_attempts():
    delays = (60.0, 300.0, 900.0)
    rng = random.Random(7)
    got = [retry_delay_seconds(n, delays, rng) for n in (1, 2, 3, 4)]
    assert got[3] is None and MAX_ATTEMPTS_PER_CYCLE == 4
    for value, base in zip(got[:3], delays):
        assert 0.8 * base <= value <= 1.2 * base
    assert [retry_delay_seconds(n, delays, random.Random(7)) for n in (1, 2, 3)] == got[:3]  # deterministic


def test_retry_after_never_shortens_the_scheduled_delay():
    rng = random.Random(1)
    assert retry_delay_seconds(1, (60.0,), rng, retry_after=500.0) == 500.0
    assert retry_delay_seconds(1, (60.0,), random.Random(1), retry_after=1.0) >= 48.0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/product/test_prepare_steps.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `product/prepare_steps.py`**

```python
"""Pure Bundle 6C preparation logic (spec §8.1, §8.2, §10.2). No IO: the
webapp builds the snapshot from authoritative artifacts and decisions."""
from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

from product.autonomy_contract import canonical_hash


class StepKind(str, Enum):
    EVALUATE = "EVALUATE"
    UNDERSTAND = "UNDERSTAND"
    FIT = "FIT"
    INTELLIGENCE = "INTELLIGENCE"
    SYSTEM_REVIEW = "SYSTEM_REVIEW"
    GATE4 = "GATE4"


PAID_STEPS = frozenset({StepKind.EVALUATE, StepKind.UNDERSTAND, StepKind.FIT, StepKind.INTELLIGENCE})
POST_DRAFT_STATUSES = frozenset({"applied", "interview", "offer", "hired", "rejected", "no_response",
                                 "offer_declined", "withdrawn"})


@dataclass(frozen=True)
class PrepareSnapshot:
    workflow_status: str | None
    profile_available: bool
    understanding_current: bool
    fit_current: bool
    intelligence_current: bool
    pack_current: bool
    mechanically_acceptable: int
    judgment_outstanding: int
    latched: bool


@dataclass(frozen=True)
class NextStep:
    kind: str  # RUN | DONE | PREPARED | NEEDS_USER
    step: StepKind | None = None
    reason: str = ""


def next_prepare_step(s: PrepareSnapshot) -> NextStep:
    """First match wins (spec §8.1, with DONE before the profile check)."""
    if s.workflow_status in POST_DRAFT_STATUSES:
        return NextStep("DONE", reason="submitted")
    if not s.profile_available:
        return NextStep("NEEDS_USER", reason="profile_missing")
    if not s.understanding_current:
        return NextStep("RUN", StepKind.UNDERSTAND)
    if not s.fit_current:
        return NextStep("RUN", StepKind.FIT)
    if not s.intelligence_current:
        return NextStep("RUN", StepKind.INTELLIGENCE)
    if s.pack_current:
        return NextStep("PREPARED")
    if s.latched:
        return NextStep("NEEDS_USER", reason="human_review_latched")
    if s.mechanically_acceptable:
        return NextStep("RUN", StepKind.SYSTEM_REVIEW)
    if s.judgment_outstanding:
        return NextStep("NEEDS_USER", reason="pack_review")
    return NextStep("RUN", StepKind.GATE4)


# ---- mechanical review ------------------------------------------------------

@dataclass(frozen=True)
class SystemReviewVerdict:
    item_type: str
    item_id: str
    source_artifact_id: str
    reason: str
    item_content_hash: str


def _float_safe(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {k: _float_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_float_safe(v) for v in value]
    return value


def item_content_hash(item: Mapping[str, Any]) -> str:
    return canonical_hash("autonomy-review-item", "v1", _float_safe({
        "item_type": item["item_type"], "item_id": item["item_id"],
        "source_artifact_id": item["source_artifact_id"], "source": item["source"],
    }))


def grounded_claim_ids(profile: Mapping[str, Any]) -> frozenset[str]:
    conflicted = {c.get("concept_id") for c in profile.get("conflicts", [])}
    return frozenset(
        claim["id"] for claim in profile.get("claims", [])
        if not claim.get("placeholder") and claim.get("concept_id") not in conflicted
    )


def mechanical_review(item: Mapping[str, Any], profile: Mapping[str, Any]) -> SystemReviewVerdict | None:
    """Only a READY content unit whose every cited claim is in the current
    bound profile, not a placeholder and not conflicted, is system-confirmable.
    Everything else is a judgment item and stays undecided (returns None)."""
    if item.get("item_type") != "content_unit":
        return None
    source = item.get("source") or {}
    evidence = source.get("profile_evidence_ids") or []
    if source.get("status") != "READY" or not source.get("text") or not evidence:
        return None
    if not set(evidence) <= grounded_claim_ids(profile):
        return None
    return SystemReviewVerdict(
        item_type=item["item_type"], item_id=item["item_id"], source_artifact_id=item["source_artifact_id"],
        reason="grounded_ready_unit", item_content_hash=item_content_hash(item),
    )


def pack_revision(profile_content_id: str, fit_content_id: str, intelligence_content_id: str) -> str:
    return canonical_hash("autonomy-pack-revision", "v1", {
        "profile_snapshot": profile_content_id, "job_fit_result": fit_content_id,
        "application_intelligence_result": intelligence_content_id,
    })


# ---- retry policy -------------------------------------------------------------

class ErrorClass(str, Enum):
    TRANSIENT = "TRANSIENT"
    HUMAN_FIXABLE = "HUMAN_FIXABLE"
    INTERNAL = "INTERNAL"


MAX_ATTEMPTS_PER_CYCLE = 4  # the initial attempt plus 3 automatic retries


def retry_delay_seconds(failures_in_cycle: int, delays: Sequence[float], rng: random.Random,
                        retry_after: float | None = None) -> float | None:
    """Delay before the next attempt after `failures_in_cycle` consecutive
    TRANSIENT failures, or None when the cycle is exhausted (escalate).
    ±20 % jitter from the injected rng; Retry-After never shortens it."""
    if failures_in_cycle < 1 or failures_in_cycle > len(delays):
        return None
    jittered = delays[failures_in_cycle - 1] * (0.8 + 0.4 * rng.random())
    return max(jittered, retry_after or 0.0)
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/product/test_prepare_steps.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add product/prepare_steps.py tests/product/test_prepare_steps.py
git commit -m "feat(product): add pure 6C step derivation, mechanical review and retry policy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Pure candidate admission and screening; providers; PREPARE authorization reuse

**Files:**
- Create: `product/candidate_promotion.py`
- Create: `webapp/services/autonomy_providers.py`
- Create: `webapp/services/autonomy_prepare_auth.py`
- Test: `tests/product/test_candidate_promotion.py`, `tests/webapp/services/test_autonomy_prepare_auth.py`

**Interfaces:**
- Consumes: `Capability`, `IdentityStrength`, `canonical_hash`, `Reason`-style dict reasons; `evaluate_rules`, `policy_hash` (`product.standing_policy`); 6B `build_context`, `evaluate_authorization`, `decide_and_record`, `decision_inputs_payload` (Task 6), `day_window`.
- Produces:
  - `product.candidate_promotion`: `ENGINE_VERSION = "candidate-promotion.v1"`; `ScreeningOutcome`; `CandidateContext` (fields listed in the code); `AdmissionResult(admitted, reasons)`; `ScreeningResult(outcome, reason_code, reasons, require_user, could_unlock, retry_at, input_fingerprint, policy_version_hash, authority)`; `candidate_input_fingerprint(ctx)`; `admit_candidate_evaluation(ctx)`; `evaluate_candidate_promotion(ctx)`
  - `webapp.services.autonomy_providers`: `ProviderSet(understanding, semantic_adapter, intelligence)`; `default_providers()`; `providers_from_app_state(state)`; `CostMeter` protocol `actual_cost(step_kind: str, *, reserved: Decimal) -> Decimal | None`; `NoCostEvidence`
  - `webapp.services.autonomy_prepare_auth`: `PrepareAuthorization(permitted, decision_id, result, effective_capability, reused, deny_reason, require_user_items, reasons)`; `material_fingerprint(inputs: Mapping) -> str`; `validity_horizon(created_at, ctx: AuthorizationContext) -> datetime | None` (earliest temporal boundary; `None` = no reuse); `authorize_prepare(conn, *, settings, account_id, application_workspace_id, now) -> PrepareAuthorization` (raises 6B `AutonomyPaused` when paused)

- [ ] **Step 1: Write the failing pure tests**

```python
# tests/product/test_candidate_promotion.py
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings, strategies as st

from product.autonomy_contract import Capability, IdentityStrength
from product.candidate_promotion import (
    CandidateContext, ScreeningOutcome, admit_candidate_evaluation, candidate_input_fingerprint,
    evaluate_candidate_promotion,
)
from product.standing_policy import default_policy_document

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
O = ScreeningOutcome


def policy(*rules):
    doc = default_policy_document("Europe/London")
    doc["rules"] = list(rules)
    return doc


def ctx(**kw) -> CandidateContext:
    values = dict(
        now=NOW, account_id="acct", search_workspace_id="sw", candidate_id="cand",
        scheduler_enabled=True, kill_switch_engaged=False, sentinel_present=False,
        search_workspace_active=True, paused=False,
        deployment_ceiling=Capability.PREPARE, account_max=Capability.PREPARE, workspace_ceiling=Capability.PREPARE,
        candidate_state="new", run_status="completed",
        identity_key="source:greenhouse:1", identity_strength=IdentityStrength.SOURCE_RECORD,
        existing_application=False, existing_intent=False,
        fit_present=True, fit_fresh=True, discovery_fit_id="dsfit_1",
        standing_policy=policy(), attributes={"fit.overall_score": 80, "company.key": "name:acme",
                                              "workspace.id": "sw", "job.employment_type": "PERMANENT"},
        llm_budget_configured=True, evaluate_envelope_present=True,
        budget_available=True, budget_retry_at=None,
        promotions_available=True, promotions_retry_at=None,
    )
    values.update(kw)
    return CandidateContext(**values)


def test_eligible_candidate_promotes_and_fingerprint_ignores_now():
    result = evaluate_candidate_promotion(ctx())
    assert result.outcome is O.PROMOTE and result.could_unlock is False
    assert candidate_input_fingerprint(ctx()) == candidate_input_fingerprint(ctx(now=NOW + timedelta(hours=3)))
    assert candidate_input_fingerprint(ctx()) != candidate_input_fingerprint(ctx(candidate_state="saved"))


def test_precedence():
    assert evaluate_candidate_promotion(ctx(kill_switch_engaged=True, existing_application=True)).outcome is O.DENY
    block = {"id": "deny", "description": "", "when": {"attr": "company.key", "op": "eq", "value": "name:acme"},
             "effect": {"type": "BLOCK"}, "on_unknown": {"type": "BLOCK"}}
    assert evaluate_candidate_promotion(ctx(standing_policy=policy(block), existing_application=True)).outcome is O.BLOCK
    r = evaluate_candidate_promotion(ctx(existing_application=True, promotions_available=False))
    assert (r.outcome, r.reason_code) == (O.NOT_ELIGIBLE, "existing_application")
    r = evaluate_candidate_promotion(ctx(promotions_available=False, promotions_retry_at=NOW + timedelta(hours=5)))
    assert (r.outcome, r.retry_at) == (O.DENY_TEMPORARY, NOW + timedelta(hours=5))


def _ask_when_unknown():
    return {"id": "fit_unknown", "description": "", "when": {"attr": "fit.overall_score", "op": "lt", "value": 70},
            "effect": {"type": "REDUCE_TO", "level": "NONE"}, "on_unknown": {"type": "REQUIRE_USER"}}


def test_require_user_could_unlock_only_when_it_is_the_sole_obstacle():
    from product.autonomy_contract import UNKNOWN
    rule = {"id": "ask", "description": "", "when": {"attr": "job.employment_type", "op": "eq", "value": "CONTRACT"},
            "effect": {"type": "REQUIRE_USER"}, "on_unknown": {"type": "REQUIRE_USER"}}
    unknown = {"fit.overall_score": 80, "company.key": "name:acme", "workspace.id": "sw",
               "job.employment_type": UNKNOWN}
    r = evaluate_candidate_promotion(ctx(standing_policy=policy(rule), attributes=unknown))
    assert (r.outcome, r.could_unlock) == (O.REQUIRE_USER, True)
    assert r.require_user == ({"kind": "rule", "ref": "ask"},)
    # a structural obstacle as well -> not surfaced
    r = evaluate_candidate_promotion(ctx(standing_policy=policy(rule), attributes=unknown, identity_key=None,
                                         identity_strength=IdentityStrength.WEAK))
    assert (r.outcome, r.could_unlock) == (O.NOT_ELIGIBLE, False)
    # ruling N: REDUCE_TO(NONE) with REQUIRE_USER on unknown keeps the cap -> structurally below PREPARE
    r = evaluate_candidate_promotion(ctx(standing_policy=policy(_ask_when_unknown()),
                                         attributes={**unknown, "fit.overall_score": UNKNOWN}))
    assert (r.outcome, r.reason_code, r.could_unlock) == (O.NOT_ELIGIBLE, "capability_below_prepare", False)


def test_no_standing_policy_and_no_budget_are_structural():
    assert evaluate_candidate_promotion(ctx(standing_policy=None)).reason_code == "standing_policy_missing"
    assert evaluate_candidate_promotion(ctx(llm_budget_configured=False)).reason_code == "llm_budget_missing"


def test_admission_requires_every_condition():
    assert admit_candidate_evaluation(ctx(fit_present=False)).admitted is True
    for field, value, reason in [
        ("scheduler_enabled", False, "scheduler_disabled"), ("kill_switch_engaged", True, "halted"),
        ("sentinel_present", True, "halted"), ("search_workspace_active", False, "search_workspace_inactive"),
        ("paused", True, "paused"), ("workspace_ceiling", Capability.NONE, "capability_below_prepare"),
        ("candidate_state", "dismissed", "candidate_state"), ("run_status", "failed", "run_not_finished"),
        ("identity_strength", IdentityStrength.WEAK, "weak_identity"),
        ("existing_application", True, "existing_application"), ("existing_intent", True, "existing_intent"),
        ("llm_budget_configured", False, "llm_budget_missing"),
        ("evaluate_envelope_present", False, "evaluate_envelope_missing"),
        ("budget_available", False, "budget_exhausted"),
    ]:
        result = admit_candidate_evaluation(ctx(**{field: value}))
        assert result.admitted is False and reason in result.reasons, field


RESTRICTIONS = [
    ("kill_switch_engaged", True), ("sentinel_present", True), ("search_workspace_active", False),
    ("workspace_ceiling", Capability.NONE), ("account_max", Capability.NONE), ("deployment_ceiling", Capability.NONE),
    ("candidate_state", "promoted"), ("run_status", "failed"), ("identity_strength", IdentityStrength.WEAK),
    ("existing_application", True), ("existing_intent", True), ("fit_fresh", False), ("fit_present", False),
    ("promotions_available", False), ("llm_budget_configured", False), ("standing_policy", None),
]
RANK = {O.PROMOTE: 5, O.REQUIRE_USER: 4, O.DENY_TEMPORARY: 3, O.NOT_ELIGIBLE: 2, O.BLOCK: 1, O.DENY: 0}


@settings(max_examples=300, deadline=None, derandomize=True)
@given(st.lists(st.sampled_from(range(len(RESTRICTIONS))), unique=True))
def test_restricting_any_input_never_yields_a_more_permissive_outcome(indices):
    base = ctx()
    restricted = dataclasses.replace(base, **{RESTRICTIONS[i][0]: RESTRICTIONS[i][1] for i in indices})
    assert RANK[evaluate_candidate_promotion(restricted).outcome] <= RANK[evaluate_candidate_promotion(base).outcome]
    if indices:
        assert evaluate_candidate_promotion(restricted).outcome is not O.PROMOTE
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/product/test_candidate_promotion.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `product/candidate_promotion.py`**

```python
"""Pure pre-application admission and promotion screening (6C spec §7).
Reuses 6B authority and standing-policy semantics over candidate
attributes; never fabricates an application workspace or a 6B decision."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from product.autonomy_contract import Capability, IdentityStrength, canonical_hash
from product.standing_policy import evaluate_rules, policy_hash

ENGINE_VERSION = "candidate-promotion.v1"
ELIGIBLE_STATES = frozenset({"new", "saved"})
FINISHED_RUNS = frozenset({"completed", "partial"})


class ScreeningOutcome(str, Enum):
    PROMOTE = "PROMOTE"
    REQUIRE_USER = "REQUIRE_USER"
    BLOCK = "BLOCK"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    DENY = "DENY"
    DENY_TEMPORARY = "DENY_TEMPORARY"


@dataclass(frozen=True)
class CandidateContext:
    now: datetime
    account_id: str
    search_workspace_id: str
    candidate_id: str
    scheduler_enabled: bool
    kill_switch_engaged: bool
    sentinel_present: bool
    search_workspace_active: bool
    paused: bool
    deployment_ceiling: Capability
    account_max: Capability
    workspace_ceiling: Capability
    candidate_state: str
    run_status: str | None
    identity_key: str | None
    identity_strength: IdentityStrength
    existing_application: bool
    existing_intent: bool
    fit_present: bool
    fit_fresh: bool
    discovery_fit_id: str | None
    standing_policy: Mapping[str, Any] | None
    attributes: Mapping[str, Any]
    llm_budget_configured: bool
    evaluate_envelope_present: bool
    budget_available: bool
    budget_retry_at: datetime | None
    promotions_available: bool
    promotions_retry_at: datetime | None


@dataclass(frozen=True)
class AdmissionResult:
    admitted: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ScreeningResult:
    outcome: ScreeningOutcome
    reason_code: str
    reasons: tuple[dict[str, Any], ...]
    require_user: tuple[dict[str, str], ...]
    could_unlock: bool
    retry_at: datetime | None
    input_fingerprint: str
    policy_version_hash: str | None
    authority: dict[str, str]


def candidate_input_fingerprint(ctx: CandidateContext) -> str:
    payload = {f.name: getattr(ctx, f.name) for f in dataclasses.fields(ctx) if f.name != "now"}
    payload["standing_policy"] = policy_hash(ctx.standing_policy) if ctx.standing_policy is not None else None
    return canonical_hash("autonomy-candidate-context", "v1", payload)


def _ceiling(ctx: CandidateContext) -> Capability:
    return min(ctx.deployment_ceiling, ctx.account_max, ctx.workspace_ceiling)


def _strong(ctx: CandidateContext) -> bool:
    return bool(ctx.identity_key) and ctx.identity_strength is not IdentityStrength.WEAK


def admit_candidate_evaluation(ctx: CandidateContext) -> AdmissionResult:
    """Fresh admission before a paid EVALUATE (spec §7.1). Not the screening."""
    checks = [
        (ctx.scheduler_enabled, "scheduler_disabled"),
        (not (ctx.kill_switch_engaged or ctx.sentinel_present), "halted"),
        (ctx.search_workspace_active, "search_workspace_inactive"),
        (not ctx.paused, "paused"),
        (_ceiling(ctx) >= Capability.PREPARE, "capability_below_prepare"),
        (ctx.candidate_state in ELIGIBLE_STATES, "candidate_state"),
        (ctx.run_status in FINISHED_RUNS, "run_not_finished"),
        (_strong(ctx), "weak_identity"),
        (not ctx.existing_application, "existing_application"),
        (not ctx.existing_intent, "existing_intent"),
        (ctx.llm_budget_configured, "llm_budget_missing"),
        (ctx.evaluate_envelope_present, "evaluate_envelope_missing"),
        (ctx.budget_available, "budget_exhausted"),
    ]
    reasons = tuple(reason for ok, reason in checks if not ok)
    return AdmissionResult(admitted=not reasons, reasons=reasons)


def evaluate_candidate_promotion(ctx: CandidateContext) -> ScreeningResult:
    fingerprint = candidate_input_fingerprint(ctx)
    ceiling = _ceiling(ctx)
    authority = {"deployment": ctx.deployment_ceiling.name, "account": ctx.account_max.name,
                 "workspace": ctx.workspace_ceiling.name}
    p_hash = policy_hash(ctx.standing_policy) if ctx.standing_policy is not None else None
    reasons: list[dict[str, Any]] = [{"code": "ceiling", "params": authority}]

    def result(outcome, code, *, require=(), could_unlock=False, retry_at=None):
        return ScreeningResult(outcome, code, tuple(reasons), tuple(require), could_unlock, retry_at,
                               fingerprint, p_hash, authority)

    if ctx.kill_switch_engaged or ctx.sentinel_present:
        reasons.append({"code": "kill_switch", "params": {}})
        return result(ScreeningOutcome.DENY, "kill_switch")

    cap, blocked, require = ceiling, False, []
    if ctx.standing_policy is not None:
        for outcome in evaluate_rules(ctx.standing_policy, ctx.attributes):
            effect = outcome.applied_effect
            if effect is None:
                continue
            via = "unknown" if outcome.via_unknown else "match"
            if (outcome.via_unknown and outcome.declared_effect.get("type") == "REDUCE_TO"
                    and effect["type"] in ("REQUIRE_USER", "BLOCK")):
                cap = min(cap, Capability[outcome.declared_effect["level"]])  # ruling N
            if effect["type"] == "REDUCE_TO":
                cap = min(cap, Capability[effect["level"]])
                reasons.append({"code": "rule_reduce", "params": {"rule": outcome.rule_id, "via": via}})
            elif effect["type"] == "BLOCK":
                blocked = True
                reasons.append({"code": "rule_block", "params": {"rule": outcome.rule_id, "via": via}})
            else:
                require.append({"kind": "rule", "ref": outcome.rule_id})
                reasons.append({"code": "rule_require_user", "params": {"rule": outcome.rule_id, "via": via}})
    if blocked:
        return result(ScreeningOutcome.BLOCK, "rule_block")

    structural = [code for ok, code in [
        (ctx.standing_policy is not None, "standing_policy_missing"),
        (ctx.search_workspace_active, "search_workspace_inactive"),
        (ctx.candidate_state in ELIGIBLE_STATES, "candidate_state"),
        (ctx.run_status in FINISHED_RUNS, "run_not_finished"),
        (_strong(ctx), "weak_identity"),
        (not ctx.existing_application, "existing_application"),
        (not ctx.existing_intent, "existing_intent"),
        (ctx.fit_present, "fit_missing"),
        (ctx.fit_fresh, "fit_stale"),
        (cap >= Capability.PREPARE, "capability_below_prepare"),
        (ctx.llm_budget_configured, "llm_budget_missing"),
    ] if not ok]
    for code in structural:
        reasons.append({"code": code, "params": {}})
    if structural:
        return result(ScreeningOutcome.NOT_ELIGIBLE, structural[0], require=require)

    if not ctx.promotions_available:
        reasons.append({"code": "promotions_cap", "params": {}})
        return result(ScreeningOutcome.DENY_TEMPORARY, "promotions_cap", require=require,
                      retry_at=ctx.promotions_retry_at)
    if require:
        return result(ScreeningOutcome.REQUIRE_USER, "rule_require_user", require=require, could_unlock=True)
    return result(ScreeningOutcome.PROMOTE, "promote")
```

Screening deliberately does not look at `scheduler_enabled`, `paused` or the evaluation budget: the tick never screens while those fail, and promotion revalidation (Task 10) re-checks pause, halt and the promotions cap inside its transaction.

- [ ] **Step 4: Run the pure tests**

Run: `.venv/Scripts/python -m pytest tests/product/test_candidate_promotion.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing service tests (providers + authorization reuse)**

```python
# tests/webapp/services/test_autonomy_prepare_auth.py
from __future__ import annotations

from datetime import timedelta

import pytest

from product.standing_policy import default_policy_document
from webapp.persistence.autonomy_authority import save_policy_version
from webapp.persistence.autonomy_ledger import list_decisions
from webapp.services.autonomy import AutonomyPaused
from webapp.services.autonomy_prepare_auth import authorize_prepare, material_fingerprint
from webapp.services.autonomy_providers import NoCostEvidence, ProviderSet, providers_from_app_state
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, NOW, conn  # noqa: F401
from tests.webapp.services.test_autonomy_context import seeded, settings  # noqa: F401
from tests.webapp.services.test_autonomy_decide import authorize_all


def _auth(conn, settings, ws, now=NOW):
    return authorize_prepare(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=ws, now=now)


def test_permitted_decision_is_reused_while_inputs_are_unchanged(conn, settings, seeded):
    authorize_all(conn)
    first = _auth(conn, settings, seeded)
    assert first.permitted and not first.reused
    second = _auth(conn, settings, seeded, now=NOW + timedelta(minutes=10))
    assert second.reused and second.decision_id == first.decision_id
    assert len([d for d in list_decisions(conn, seeded) if d["requested_stage"] == "PREPARE"]) == 1


def test_policy_change_invalidates_reuse(conn, settings, seeded):
    authorize_all(conn)
    first = _auth(conn, settings, seeded)
    doc = default_policy_document("Europe/London")
    doc["limits"]["fill_per_day"] = 7
    save_policy_version(conn, account_id=ACCOUNT, doc=doc, created_by="u", now=NOW)
    second = _auth(conn, settings, seeded, now=NOW + timedelta(minutes=1))
    assert not second.reused and second.decision_id != first.decision_id


def test_validity_horizon_forces_fresh_evaluation_after_local_midnight(conn, settings, seeded):
    authorize_all(conn)
    first = _auth(conn, settings, seeded)
    later = _auth(conn, settings, seeded, now=NOW + timedelta(hours=13))  # past Europe/London midnight
    assert not later.reused and later.decision_id != first.decision_id


def test_horizon_is_the_earliest_temporal_boundary_not_just_midnight():
    from datetime import timedelta
    from product.autonomy_contract import AnswerCandidate, Reach, RepresentationRequirement
    from tests.product.autonomy_fixtures import make_ctx
    from webapp.services.autonomy_prepare_auth import validity_horizon
    ctx = make_ctx()
    assert validity_horizon(NOW, ctx) is not None  # next local midnight
    expiring = AnswerCandidate(approved_answer_id="a", subject="employment.availability_start", reach=Reach.ACCOUNT,
                               scope_id=None, context={}, confirmed_at=NOW - timedelta(days=30) + timedelta(hours=1),
                               basis_kind="USER_ASSERTION", basis_hash_at_approval=None, basis_hash_current=None,
                               contradicted=False)
    req = RepresentationRequirement(key="start", subject="employment.availability_start", required=True,
                                    evidence_available=False, candidates=(expiring,))
    assert validity_horizon(NOW, make_ctx(requirements=(req,))) == NOW + timedelta(hours=1)  # freshness beats midnight
    unknown = RepresentationRequirement(key="x", subject="not.a.subject", required=True, evidence_available=False,
                                        candidates=(expiring.__class__(**{**expiring.__dict__, "subject": "not.a.subject"}),))
    assert validity_horizon(NOW, make_ctx(requirements=(unknown,))) is None  # unreliable -> no reuse
    assert validity_horizon(NOW, make_ctx(standing_policy=None)) is None


def test_allow_none_is_never_permission(conn, settings, seeded):
    decision = _auth(conn, settings, seeded)  # no authority configured -> NONE
    assert decision.permitted is False and decision.effective_capability == "NONE"


def test_pause_raises_and_records_nothing(conn, settings, seeded):
    from webapp.services.autonomy_controls import pause
    authorize_all(conn)
    pause(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id=seeded, actor="u", reason="r", now=NOW)
    with pytest.raises(AutonomyPaused):
        _auth(conn, settings, seeded)
    assert list_decisions(conn, seeded) == []


def test_material_fingerprint_ignores_now_and_run_id():
    base = {"now": "2026-09-24T12:00:00.000000+00:00", "run_id": None, "account_id": "a", "x": 1}
    assert material_fingerprint(base) == material_fingerprint({**base, "now": "2027-01-01T00:00:00.000000+00:00",
                                                               "run_id": "run_9"})
    assert material_fingerprint(base) != material_fingerprint({**base, "x": 2})


def test_provider_set_honours_app_state_overrides():
    class State:
        job_understanding_provider = "U"
        semantic_adapter = "S"
        application_intelligence_provider = "I"
    assert providers_from_app_state(State()) == ProviderSet("U", "S", "I")
    from decimal import Decimal
    assert NoCostEvidence().actual_cost("FIT", reserved=Decimal("1")) is None
```

- [ ] **Step 6: Implement `webapp/services/autonomy_providers.py`**

```python
"""Model providers and cost evidence for the 6C scheduler. The in-app driver
uses app.state overrides exactly like the HTTP routes; the CLI uses the
production providers."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderSet:
    understanding: Any
    semantic_adapter: Any
    intelligence: Any


def default_providers() -> ProviderSet:
    from product.openai_application_intelligence_provider import OpenAIApplicationIntelligenceProvider
    from product.openai_job_understanding_provider import OpenAIJobUnderstandingProvider
    from webapp.services.openai_semantic_proposer_client import OpenAISemanticProposerClient
    from webapp.services.semantic_proposal_adapter import SemanticProposalAdapter
    return ProviderSet(OpenAIJobUnderstandingProvider(), SemanticProposalAdapter(OpenAISemanticProposerClient()),
                       OpenAIApplicationIntelligenceProvider())


def providers_from_app_state(state: Any) -> ProviderSet:
    defaults: ProviderSet | None = None

    def pick(name: str, field: str):
        nonlocal defaults
        override = getattr(state, name, None)
        if override is not None:
            return override
        defaults = defaults or default_providers()
        return getattr(defaults, field)
    return ProviderSet(pick("job_understanding_provider", "understanding"),
                       pick("semantic_adapter", "semantic_adapter"),
                       pick("application_intelligence_provider", "intelligence"))


class CostMeter(Protocol):
    def actual_cost(self, step_kind: str, *, reserved: Decimal) -> Decimal | None: ...


class NoCostEvidence:
    """No reliable per-call cost evidence yet: every settlement uses the
    reserved hard maximum (spec §11.3)."""

    def actual_cost(self, step_kind: str, *, reserved: Decimal) -> Decimal | None:
        return None
```

Check the three provider import paths once with `grep -n "^class OpenAI" product/openai_*provider.py webapp/services/openai_semantic_proposer_client.py`; they are the ones `webapp/api/workspaces.py` already imports.

- [ ] **Step 7: Implement `webapp/services/autonomy_prepare_auth.py`**

```python
"""PREPARE authorization at the 6C boundary (spec §6.5). A still-valid
recorded LIVE PREPARE decision is reused only when its material inputs
(the recorded inputs minus `now` and `run_id`), policy/subject-policy/engine
hashes and validity horizon still hold; otherwise a fresh decision is
recorded through 6B decide_and_record."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from product.autonomy_contract import (
    AuthorizationContext, Capability, Mode, canonical_hash, canonical_json, parse_utc,
)
from product.autonomy_gate import evaluate_authorization
from webapp.config import Settings
from webapp.persistence.autonomy_ledger import decision_inputs_payload
from webapp.services.autonomy import decide_and_record
from webapp.services.autonomy_context import build_context, day_window
from webapp.services.autonomy_controls import sentinel_present

_NON_MATERIAL = ("now", "run_id")


@dataclass(frozen=True)
class PrepareAuthorization:
    permitted: bool
    decision_id: str
    result: str
    effective_capability: str
    reused: bool
    deny_reason: str | None
    require_user_items: tuple[dict[str, str], ...]
    reasons: tuple[dict[str, Any], ...]


def material_fingerprint(inputs: Mapping[str, Any]) -> str:
    return canonical_hash("autonomy-material-context", "v1",
                          {k: v for k, v in inputs.items() if k not in _NON_MATERIAL})


def validity_horizon(created_at: datetime, ctx: AuthorizationContext) -> datetime | None:
    """The earliest temporal boundary of any time-sensitive PREPARE input
    (spec §6.5): the budget/limit window end (next local midnight after the
    decision), every counter/budget retry_at, and every answer candidate's
    freshness expiry (confirmed_at + the subject's freshness_days). If any
    boundary cannot be computed reliably (no standing policy, an answer whose
    subject is not in the subject policy), return None -> no reuse."""
    if ctx.standing_policy is None:
        return None
    bounds = [day_window(created_at, ctx.standing_policy["timezone"])[1]]
    bounds += [c.retry_at for c in ctx.counters if c.retry_at is not None]
    bounds += [b.retry_at for b in ctx.budgets if b.retry_at is not None]
    for requirement in ctx.requirements:
        for candidate in requirement.candidates:
            entry = ctx.subject_policy["subjects"].get(candidate.subject)
            if entry is None:
                return None
            if entry["freshness_days"] is not None:
                bounds.append(candidate.confirmed_at + timedelta(days=entry["freshness_days"]))
    return min(bounds)


def _latest_live_prepare(conn, application_workspace_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM autonomy_decisions WHERE application_workspace_id = ? AND requested_stage = 'PREPARE' "
        "AND mode = 'LIVE' ORDER BY seq DESC LIMIT 1", (application_workspace_id,)).fetchone()
    return dict(row) if row else None


def _from_row(row: Mapping[str, Any], *, reused: bool) -> PrepareAuthorization:
    permitted = (row["result"] == "ALLOW" and bool(row["grantable"])
                 and Capability[row["effective_capability"]] >= Capability.PREPARE)
    return PrepareAuthorization(
        permitted=permitted, decision_id=row["id"], result=row["result"],
        effective_capability=row["effective_capability"], reused=reused, deny_reason=row["deny_reason"],
        require_user_items=tuple({"kind": k, "ref": r} for k, r in json.loads(row["require_user_json"])),
        reasons=tuple({"code": c, "params": p} for c, p in json.loads(row["reasons_json"])),
    )


def authorize_prepare(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                      now: datetime) -> PrepareAuthorization:
    ctx = build_context(conn, settings=settings, account_id=account_id,
                        application_workspace_id=application_workspace_id, requested_stage=Capability.PREPARE,
                        mode=Mode.LIVE, now=now, sentinel_present=sentinel_present(settings.autonomy_sentinel_path))
    fresh = evaluate_authorization(ctx)
    current = material_fingerprint(json.loads(canonical_json(decision_inputs_payload(ctx))))
    latest = _latest_live_prepare(conn, application_workspace_id)
    if latest is not None and latest["engine_version"] == fresh.engine_version \
            and latest["policy_version_hash"] == fresh.policy_version_hash \
            and latest["subject_policy_hash"] == fresh.subject_policy_hash \
            and material_fingerprint(json.loads(latest["inputs_json"])) == current:
        horizon = validity_horizon(parse_utc(latest["created_at"]), ctx)
        if horizon is not None and now < horizon:
            return _from_row(latest, reused=True)
    _, row = decide_and_record(conn, settings=settings, account_id=account_id,
                               application_workspace_id=application_workspace_id,
                               requested_stage=Capability.PREPARE, mode=Mode.LIVE, now=now)
    return _from_row(row, reused=False)
```

`decide_and_record` checks pause inside its transaction and raises `AutonomyPaused` (writing nothing); `authorize_prepare` lets it propagate.

- [ ] **Step 8: Run tests**

Run: `.venv/Scripts/python -m pytest tests/product/test_candidate_promotion.py tests/webapp/services/test_autonomy_prepare_auth.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add product/candidate_promotion.py webapp/services/autonomy_providers.py webapp/services/autonomy_prepare_auth.py tests/product/test_candidate_promotion.py tests/webapp/services/test_autonomy_prepare_auth.py
git commit -m "feat(autonomy): add pure candidate screening, provider set and PREPARE authorization reuse

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
> **Format note for Tasks 10–15.** These tasks give the objective, files, required behaviour and invariants, the tests to write first, and the commit boundary. The implementer writes the code test-first against the interfaces already fixed in Tasks 1–9 and the approved spec; exact helper internals are decided inside the task. Each task still follows: write failing tests → run (fail) → implement → run (pass) → commit.

---

### Task 10: Candidate services — enqueue, admission, evaluation, screening, revalidating promotion, human resolution

**Objective:** Everything that happens to a discovered candidate before it becomes an application (spec §6.1, §7).

**Files:**
- Create: `webapp/services/autonomy_candidates.py`
- Modify: `webapp/services/discovery.py` — extract `_promote_candidate_in_transaction(conn, candidate_id, *, search_workspace_id, account_id) -> dict` from `promote_discovery_candidate` (the body between `BEGIN IMMEDIATE` and `commit`, no BEGIN/commit/rollback inside; the public function keeps its transaction and behaviour); after `complete_discovery_run` in `run_discovery_search`, call `enqueue_run_candidates` in the same transaction (pass `commit=False` to `complete_discovery_run` — add that keyword, default `True`).
- Modify: `webapp/persistence/discovery.py` — `complete_discovery_run(..., commit=True)`; add `set_discovery_candidate_status(..., commit=True)` keyword so dismissal can join a caller transaction.
- Test: `tests/webapp/services/test_autonomy_candidates.py`; append world builders to `tests/webapp/services/autonomy_6c_fixtures.py` (`make_candidate(conn, *, record_id, company, run_status="completed", with_fit=True)` creating a user profile, a discovery run, an ingested candidate and optionally a discovery fit row; `enable_prepare(conn)` = enable autonomous preparation + a standing policy with an LLM budget; `settings_6c(tmp_path)` with scheduler on and step envelopes set).

**Interfaces produced:**
- `enqueue_run_candidates(conn, *, run_id, account_id, search_workspace_id, now) -> int`
- `candidate_identity(record) -> tuple[str | None, IdentityStrength]`; `existing_application_for(conn, *, account_id, record) -> str | None`; `existing_intent_for(conn, *, account_id, record) -> bool`
- `build_candidate_context(conn, *, settings, account_id, search_workspace_id, candidate_id, now) -> CandidateContext`
- `candidate_next_action(conn, ctx) -> str` — `EVALUATE | SCREEN | PROMOTE | DONE`
- `run_candidate_evaluation(conn, *, settings, providers, ctx, request_id) -> dict` — **only** the paid model call (wraps `evaluate_discovery_candidate` with `active_extensions=[]`); it never reserves, records attempts or settles
- `screen_candidate(conn, *, ctx, now) -> dict` (persists the screening; opens a candidate exception + `CANDIDATE_QUESTION` notification only when `could_unlock`; sets queue eligibility)
- `promote_candidate(conn, *, settings, account_id, search_workspace_id, candidate_id, screening_id, actor_type, actor, now) -> dict | None`
- `resolve_candidate_question(conn, *, settings, exception_id, resolution, actor, reason, now) -> dict`; `CandidatePromotionRefused(Exception)`

**Required behaviour and invariants:**
1. Enqueue only `new`/`saved` candidates of runs that finished `completed`/`partial`, in active search workspaces whose effective ceiling ≥ PREPARE; `failed` runs enqueue nothing.
2. Identity from `product.job_identity.job_identity(record)`: source key > canonical URL; neither → WEAK. "Existing application" = any job workspace **on the account** whose `application_workspace_job_identities` row has the same source or URL key. "Existing intent" = a live/confirmed intent (6B `live_intent`) for either key.
3. Context attributes: `fit.overall_score` (int, or Decimal via `Decimal(str(float))`; missing/non-finite → `UNKNOWN`), `fit.verdict`, `job.employment_type` (`normalize_employment_type`), `job.location`, `job.title`, `company.key` (`normalized_employer_key`), `workspace.id`, `identity.strength`. **Floats never reach a hashed payload** (canonical hashing rejects floats): normalize to `Decimal` first.
4. `llm_budget_configured` = standing policy has `limits.budgets.LLM.per_day`; `budget_available` = day usage + `EVALUATE` envelope ≤ cap; `promotions_available` = `promotions:day` usage < `settings.autonomy_max_promotions_per_day` (retry at next local midnight).
5. **Paid EVALUATE — single owner:** the scheduler (Task 13) owns reservation, the `STARTED`/terminal attempt rows, retries, finalization and settlement for **all** paid steps, candidate EVALUATE included. This service builds the candidate context and exposes `admit_candidate_evaluation`, which the scheduler re-runs **inside its short reservation/`STARTED` transaction immediately before spend**, and performs the call itself via `run_candidate_evaluation`. Failures reserve and spend nothing (budget-only → dormant until the budget window rolls; halt/pause/scheduler → release; structural → screen and record `NOT_ELIGIBLE`). The attempt row has `authorization_decision_id = NULL` and an input fingerprint that binds the admission inputs. Deterministic request id: `f"auto-eval-{candidate_id}-{fingerprint[-12:]}"`.
6. **Screening** is pure-then-persist: `evaluate_candidate_promotion` returns; this service writes the immutable row. Anti-noise: exception + notification only when `could_unlock`; never a second open exception for the same candidate.
7. **Promotion** (one `run_immediate` transaction): re-check halt/sentinel and search-workspace pause; rebuild the context; SCHEDULER requires `PROMOTE` **and** the same input fingerprint as the screening; USER accepts `PROMOTE` or `REQUIRE_USER` with the promotions cap ignored (a user promotion never consumes it). Then `_promote_candidate_in_transaction`; if it reports `created: False` (an existing workspace), raise to roll back → `None`. Write `autonomy_candidate_promotions`, `autonomy_enrolments` (`SCHEDULER`/`USER`, `ENROL`), enqueue the application (`next_eligible_at = now`), consume one `promotions:day` unit (SCHEDULER only; a lost cap race rolls everything back), make the candidate queue row dormant.
8. **Human resolution:** `PROMOTE` runs the USER promotion in the same transaction and records the resolution only if it succeeds (else `CandidatePromotionRefused`, nothing written); `DISMISS` sets the candidate `dismissed` and records the resolution. Resolving an already-resolved exception is an error.

**Tests (write first):**
- enqueue: completed/partial enqueued, failed not, below-PREPARE workspace not, promoted/dismissed not.
- `test_rediscovered_identity_is_not_promoted_twice` (Review Focus): promote once; a second candidate with the same source key from another run screens `NOT_ELIGIBLE(existing_application)`.
- admission is pure and side-effect free for each failing reason; `run_candidate_evaluation` itself writes no reservation or attempt row (the scheduler-owned reservation/attempt/settlement path is tested in Task 13, including `authorization_decision_id IS NULL` for candidate attempts).
- screening persistence, anti-noise (structural + REQUIRE_USER → no exception), single open exception.
- promotion revalidation: change each of policy, ceiling, kill switch, pause, identity/dedupe, candidate state, promotions cap between screening and promotion → no promotion, no workspace, no enrolment, no reservation.
- user Promote: creates `USER/ENROL`, wakes the application, does **not** consume `promotions:day`; refused when a structural obstacle appeared; Dismiss sets `dismissed`.
- `run_discovery_search` with a fake runner enqueues the run's candidates in the same transaction as completion.
- Monkeypatch only the model call and the fit-staleness read (`_fit_state`, `run_candidate_evaluation`) in unit tests; Task 15's acceptance exercises the real flow.

**Commit:** `feat(autonomy): add candidate admission, screening and revalidating promotion`

---

### Task 11: Preparation services — snapshot, paid steps, system review, system Gate 4, latch, enrolment

**Objective:** Everything that happens to an enrolled application inside one tick step (spec §5, §8).

**Files:**
- Create: `webapp/services/autonomy_prepare.py`
- Modify: `webapp/services/http_api.py` — after a **USER** `record_review_decision(s)`, call `on_user_review_decision(conn, workspace_id, now)` (latch if the current revision was already system-confirmed); wake the application after manual `understand_job` / `fit_job` / `generate_application_intelligence` when it is enrolled.
- Test: `tests/webapp/services/test_autonomy_prepare_service.py` (uses the `prepared_chain` / `ready_chain` fixtures from Task 7).

**Interfaces produced:**
- `enrol(conn, *, account_id, application_workspace_id, actor, now)` / `unenrol(...)` (USER; own transaction; enrol also enqueues)
- `prepare_snapshot(conn, *, settings, account_id, application_workspace_id) -> tuple[PrepareSnapshot, dict]` (the dict carries profile payload, current artifact content ids, `pack_revision`, outstanding items split into mechanical verdicts and judgment items)
- `run_paid_step(conn, *, settings, providers, step, account_id, application_workspace_id, request_id) -> list[dict]` (artifact refs)
- `run_system_review(conn, *, account_id, application_workspace_id, detail, now) -> int`
- `system_gate4(conn, *, settings, account_id, application_workspace_id, authorization, now) -> dict`
- `request_pack_review(conn, *, account_id, application_workspace_id, actor, now)`; `on_user_review_decision(conn, workspace_id, now)`; `system_confirmed_revision(conn, application_workspace_id) -> str | None`; `classify_error(exc) -> tuple[ErrorClass, str]`

**Required behaviour and invariants:**
1. The snapshot is built only from authoritative state: `check_staleness` for understanding/fit/intelligence/pack; `list_outstanding_review_items` for undecided items; `mechanical_review` against the **current bound profile snapshot**; `has_latch(current pack_revision)`. When latched, `mechanically_acceptable = 0`.
2. Paid steps call the existing `understand_job`, `fit_job` (extension ids reused from the current fit request, else `[]`), `generate_application_intelligence` with deterministic request ids `f"auto-{step}-{workspace_id}-{fingerprint[-12:]}"`; an existing artifact matching the step's input fingerprint is reused (`REUSED`) without a model call. Uses whichever document path is current; when `settings.cv_quality_v2_enabled` is on and the v2 path needs user document selections, GATE4 becomes `NEEDS_USER("document_selection_required")` rather than a failure.
3. System review writes, in one transaction, one `SYSTEM_AUTO_CONFIRMED` `acknowledged_and_proceed` decision per mechanically accepted item with basis `{reason, item_content_hash, pack_revision}` — **never** for an item that already has any decision, never for judgment items (they stay undecided; no synthetic decisions).
4. System Gate 4 calls `system_confirm_application_pack(..., precheck=...)`; the precheck, inside the same `BEGIN IMMEDIATE`, re-checks all eight conditions of spec §8.3: still-valid PREPARE authority (`ALLOW`, `grantable`, effective ≥ PREPARE); not paused/halted, still enrolled, scheduler gate on; no latch for the current revision; exact current profile/artifact/revision hashes; every item resolved; every `SYSTEM_AUTO_CONFIRMED` basis still matching (grounding recomputed independently); zero unsupported/contradicted claims in the pack; `completion_status == READY`. Any failure raises → rollback → nothing written.
5. `system_confirmed_revision`: the latest `drafted` workflow event (by `rowid`, not timestamp) whose note is `SYSTEM_GATE4_NOTE` → its pack's source content ids → `pack_revision`.
6. Latch only on explicit `request_pack_review`, or on a USER review decision recorded against a revision that `system_confirmed_revision` returns; a USER decision on a not-yet-confirmed revision never latches and wakes the application.
7. `classify_error`: timeouts/rate limits/5xx/connection errors → `TRANSIENT` (with `retry_after` when the provider gives one); missing credentials, `PipelineError` about a missing/invalid profile, missing budget/envelope → `HUMAN_FIXABLE`; everything else (incl. invariant/contract errors and cost overage) → `INTERNAL`.

**Tests (write first):**
- snapshot/step derivation on the real chain: fresh chain → SYSTEM_REVIEW available; decided chain → GATE4; confirmed → PREPARED; applied → DONE.
- `test_system_review_never_overrides_or_duplicates_a_user_decision` (Review Focus): a USER decision recorded between snapshot and write wins; no system row for that item.
- judgment items remain undecided (no rows) and yield `NEEDS_USER("pack_review")`.
- fault injection: mutate each of the eight §8.3 conditions just before Gate 4 → refusal and zero new workflow events/pack artifacts.
- latch: explicit review latches; USER decision after system confirmation latches; USER decision resolving an outstanding judgment item on an unconfirmed revision does **not** latch and Gate 4 then completes mechanically.
- `REUSED` path makes no provider call (provider fake counts calls); `classify_error` table.

**Commit:** `feat(autonomy): add preparation steps, mechanical review, system Gate 4 and review latch`

---

### Task 12: Inbox, notifications, propagation, wake hooks and retry eligibility

**Objective:** The user-facing exception surface and every wake-up source (spec §6.2, §9, §10.2).

**Files:**
- Create: `webapp/services/autonomy_inbox.py`
- Modify: `webapp/services/decision_policy.py` (`resolve_blocker` wakes its application in its own transaction; add `answer_blocker(..., reusable: bool, reach, scope_id)` that resolves, optionally approves a reusable 6B answer, propagates and wakes in **one** transaction)
- Modify: `webapp/services/autonomy_controls.py` (`resume`, `resume_all`, `set_capability`, and a new `save_standing_policy` wrapper also wake the candidate queue; capability raises enqueue newly eligible candidates)
- Modify: `webapp/services/pipeline.py` (`refresh_profile` saves with `commit=False`, wakes all enrolled applications and candidate rows of the account, then commits)
- Modify: `webapp/services/pipeline.py::create_job_from_source_record` (a new job posting snapshot for an enrolled application wakes it in the same transaction)
- Test: `tests/webapp/services/test_autonomy_inbox.py`

**Interfaces produced:** `build_inbox(conn, *, settings, account_id, now) -> dict` (`needs_answer`, `informational`, `ready`); `inbox_summary(conn, account_id) -> dict` (`badge`, `actionable`, `informational_unseen`); `mark_inbox_seen(conn, *, account_id, now) -> int`; `reconcile_notifications(conn, *, settings, account_id, now) -> int`; `notify_outcome(conn, *, account_id, subject_type, subject_id, kind, reason, fingerprint, detail, now)`; `propagate_answer(conn, *, account_id, subject, reach, scope_id, now) -> int`; `retry_failure(conn, *, account_id, subject_type, subject_id, step_kind, actor, now) -> dict` with `RetryNotEligible(Exception)`.

**Required behaviour and invariants:**
1. The inbox is derived and read-only except for `SEEN`. Every entry lists all current reasons. Candidate questions (open candidate exceptions), application REQUIRE_USER items from the still-valid PREPARE decision (rules → existing acknowledgement form; blockers → existing answer form with optional reusable answer and any `SYSTEM_PROPOSED` draft), pack review (undecided judgment items of the current revision), operational `NEEDS_USER`; informational: BLOCK, AUTO_REJECT, `OPERATIONAL_ERROR`; ready: `PREPARED`.
2. Notification keys bind the occurrence: `f"{kind}:{subject_type}:{subject_id}:{reason}:{fingerprint}"` — repeated ticks do not re-notify; recurrence after resolution does.
3. **Badge = unresolved actionable + unseen informational**: `NEEDS_USER` (incl. human-fixable failures) and `CANDIDATE_QUESTION` count until resolved; `PREPARED`, `BLOCKED`, `OPERATIONAL_ERROR` count until seen. `SEEN` never implies `RESOLVED`; reconciliation writes `RESOLVED` only when the derived condition is gone.
4. **Propagation is wake-only** and runs in the answer's transaction: ACCOUNT → enrolled applications of the account; SEARCH_WORKSPACE → enrolled applications of that search workspace; EMPLOYER → enrolled applications whose employer key matches; each only if waiting on that semantic subject (open blocker with that `semantic_subject_key`, or a REQUIRE_USER item for it in its current decision). It writes nothing but queue eligibility.
5. **Retry eligibility:** `retry_failure` succeeds only for an exhausted `TRANSIENT` cycle of that step + current input fingerprint with no open cycle for it; direct `INTERNAL`/`INTEGRITY` failures and open cycles raise `RetryNotEligible`. It records the one-shot `autonomy_retry_requests` row and wakes the item.
6. **Wake hooks** (each in the same transaction as its cause): answers/resolutions (own item + propagated siblings), enrolment, retry, resume/resume-all, policy saves, capability changes (both queues), new profile snapshot (all enrolled applications + candidate rows of the account), a new job posting snapshot for an enrolled application, manual reruns (Task 11), discovery completion (Task 10), driver start (Task 13).

**Tests (write first):** inbox grouping and all-reasons listing; badge arithmetic for each kind (seen actionable still counts; seen informational drops); dedupe and recurrence; reconciliation resolves only vanished conditions; propagation per reach with **zero** sibling writes (row counts of blockers, resolutions, review decisions, approved answers unchanged); `answer_blocker` atomicity (a failure after resolution rolls back the resolution, the answer and the wakes); retry eligibility refusals (INTERNAL, open cycle, non-exhausted) and one-shot semantics; each wake hook sets `next_eligible_at` on dormant items in both queues where relevant.

**Commit:** `feat(autonomy): add the 6C inbox, notifications, answer propagation and wake hooks`

---

### Task 13: Scheduler engine and drivers

**Objective:** `run_tick` and its two drivers (spec §6, §10, §11).

**Files:**
- Create: `webapp/services/autonomy_scheduler.py`, `webapp/autonomy_worker.py`
- Modify: `webapp/app.py` (lifespan starts/stops the in-app driver thread when `settings.autonomy_scheduler_enabled`)
- Test: `tests/webapp/services/test_autonomy_scheduler.py`, `tests/webapp/test_autonomy_worker.py`

**Interfaces produced:**
- `TickReport(sweeps: dict, processed: list[dict])`
- `run_tick(conn, *, settings, providers, now, rng, worker_id, cost_meter=None) -> TickReport`
- `run_driver(settings, providers, *, stop: threading.Event, clock, rng, worker_id)` (loop: tick to completion, then wait ≈ `autonomy_tick_interval`; never overlaps itself; wakes all enrolled applications and candidate rows once on start)
- CLI: `python -m webapp.autonomy_worker --once | --loop` (exits 0 doing nothing when the scheduler gate is off)

**Required behaviour and invariants:**
1. **Sweeps first, always (even halted):** `expire_grants`, `expire_unclicked`, `mark_stale_dispatches_ambiguous(result_timeout=settings.autonomy_dispatch_result_timeout)`, notification reconciliation (Task 12), and orphan settlement for expired leases (append `ABANDONED`, settle each reservation once: provider-audited actual if the cost meter proves it, else the reserved maximum).
2. Then stop if the scheduler gate is off or the item's account is halted (kill switch or sentinel): no new paid work, promotion, FILL or SUBMIT.
3. **Fair selection:** alternate application and candidate due items 1:1 (applications first) up to `autonomy_max_items_per_tick`.
4. **Per item:** lease with `ttl = step_timeout + lease_margin` → derive → authorize (applications: `authorize_prepare`, permission requires `grantable` and effective ≥ PREPARE; candidates: screening/promotion per Task 10; for EVALUATE, `admit_candidate_evaluation` is re-run inside the reservation/`STARTED` transaction) → **re-check** pause, enrolment, halt and scheduler gate right before the step (fail → release, no decision, no attempt) → for paid steps reserve the hard maximum within the day cap (and the application cap for application steps) **in the same transaction as the `STARTED` row**, failing closed with `NEEDS_USER` when the budget or envelope is missing → run the step with no write transaction held → finalize in one transaction fenced by holder + generation + unexpired lease (terminal row, settlement per reservation with `settlement_ref_for`, outcome effects, `next_eligible_at`, lease release). A lost fence commits nothing.
5. **Retries:** `TRANSIENT` → `retry_delay_seconds(cycle_failures(...) , settings.autonomy_retry_delays, rng, retry_after)`; `None` (4th failure) → escalate to `NEEDS_USER` when human-fixable in nature, else `OPERATIONAL_ERROR`; `HUMAN_FIXABLE` → `NEEDS_USER` immediately; `INTERNAL` → `OPERATIONAL_ERROR`, non-retrying. A retry request opens exactly one new cycle (attempts carry `retry_request_id`); a used request never reopens another. Cost overage: record and charge the true amount, raise `OPERATIONAL_ERROR`, refuse that step kind until its envelope changes.
6. **Outcomes:** success → `next_eligible_at = now`; `DONE`/`PREPARED`/`NEEDS_USER`/`BLOCKED`/`OPERATIONAL_ERROR` → dormant plus the matching notification (Task 12, `notify_outcome`); `DENY_TEMPORARY` → `retry_at`.
7. **Halt during a step:** the in-flight step finishes and records truthfully; nothing further is scheduled until resume-all and a fresh authorization.
8. The engine never calls `request_grant` or `pre_click_commit`.

**Tests (write first):** one tick per step kind through the real chain with fake providers; `ALLOW(NONE)` runs nothing; race injection (pause, unenrol, halt) between lease and step → nothing starts, and during a step → truthful finish, nothing further; `test_expired_lease_cannot_finalize_even_if_not_retaken` at engine level (slow fake provider + injected clock) → no terminal row from the late worker, recovery writes `ABANDONED` and settles at maximum; retry cycle 60/300/900 then escalation on the 4th failure; one-shot retry request; overage path; missing budget and missing envelope fail closed with zero reservations; fairness with a large candidate backlog (applications still processed); sweeps run while halted; `test_driver_start_wakes_all_enrolled_and_candidates` (Review Focus); driver never overlaps itself (a tick that outlasts the interval delays the next); CLI `--once` returns 0 and does nothing with the gate off.

**Commit:** `feat(autonomy): add the 6C scheduler engine with in-app and CLI drivers`

---

### Task 14: Dossier, API and UI

**Objective:** Operator-facing surfaces (spec §12, §13).

**Files:**
- Modify: `webapp/services/autonomy_dossier.py` (add `pack` section: pack content id, source artifact content ids, document hashes; `system_review` items with reason and basis; latches; enrolment history; originating screening and promotion; the step-attempt timeline with costs; sections labelled **"Current state (derived)"** and **"Attempt history (observational)"**)
- Modify: `webapp/api/autonomy.py` (endpoints below), `webapp/templates/autonomy_dossier.html`, create `webapp/templates/autonomy_inbox.html`, modify `webapp/static/app.js` (badge polling of `/api/autonomy/inbox/summary` on page load and every 60 s), `webapp/templates/base.html` (badge element on the Autonomy link)
- Test: `tests/webapp/api/test_autonomy_6c_routes.py`, extend `tests/webapp/services/test_autonomy_dossier.py`

**Endpoints:** `GET /autonomy/inbox`, `GET /api/autonomy/inbox` (both write `SEEN` for shown entries), `GET /api/autonomy/inbox/summary`, `POST /api/workspaces/{id}/autonomy/enrol` | `/unenrol` (owned job workspace only, else 404), `POST /api/workspaces/{id}/autonomy/review-pack` (latch current revision; 409 if there is no current revision), `POST /api/autonomy/retry` `{subject_type, subject_id, step_kind}` (409 unless retry-eligible), `POST /api/autonomy/candidate-exceptions/{id}/resolve` `{resolution, reason}` (409 on refused promotion or already resolved; 404 if not the caller's), `GET /api/autonomy` extended with `scheduler_enabled`, `driver_running`, `last_tick_at`. All state changes are attributed to the account and owner-scoped.

**Tests (write first):** each endpoint's success, ownership 404, validation 422 and refusal 409; `SEEN` written by inbox views but actionable badge count unchanged; dossier JSON contains the pack/document hashes, system-confirmed items with basis and the two labelled sections, and stays account-scoped; inbox page and dossier page render (TestClient).

**Commit:** `feat(autonomy): add inbox UI, enrolment/retry/review endpoints and dossier pack detail`

---

### Task 15: Final validation — concurrency, acceptance, browser, migrations, full suite, boundary

**Objective:** Prove the closure condition (spec §15–§16) and prepare the branch for review.

**Files:**
- Test: `tests/webapp/services/test_autonomy_6c_concurrency.py`, `tests/webapp/test_autonomy_prepare_acceptance.py`, `tests/webapp/test_autonomy_6c_structure.py`, `tests/webapp/test_autonomy_6c_browser.py`
- Modify: `docs/superpowers/specs/2026-09-26-bundle6c-prepare-design.md` only if an implementation fact contradicts it (report first; no silent spec edits)

**Steps:**
1. **Concurrency** (threads, separate connections, WAL; run the file 20× in a row, all must pass): two workers on one application lease; two on one candidate lease; the last budget unit; the promotions cap; two promotions of the same candidate; the kill switch racing a tick. Each asserts exactly one winner and no partial state.
2. **Acceptance on the real workflow** (fake portal runner, fake LLM providers, real services): a user-triggered discovery run, then ticks until quiescent. Assert: a grounded eligible candidate ends promoted and **system-confirmed `PREPARED`** (workflow note is `SYSTEM_GATE4_NOTE`, never the user note); a judgment candidate → `NEEDS_USER` → answer via the inbox → woken → `PREPARED`; a weak-identity candidate and an already-applied duplicate are not promoted; an older application stays untouched until enrolled; **zero FILL/SUBMIT grants, intents or attempts**; the dossier shows derived state and labelled history; badge counts are right.
   - **Halt/recovery sequence:** halt active → no new work starts → an in-flight step finishes truthfully → remove the halt → still no work → explicit resume-all → a fresh PREPARE decision is recorded → work resumes.
   - **Negative controls:** with the scheduler gate off, deployment ceiling `NONE`, the kill switch engaged, or no budget configured: no new paid/autonomous preparation work, promotion, FILL or SUBMIT occurs, while reduce-only expiry/reconciliation sweeps still run.
3. **Structure test:** no `request_grant`/`pre_click_commit` in 6C modules (AST scan of `webapp/services/autonomy_candidates.py`, `autonomy_prepare.py`, `autonomy_scheduler.py`, `autonomy_inbox.py`, `autonomy_prepare_auth.py`, `webapp/autonomy_worker.py`); no `webapp` import in `product/`; no `ORDER BY ... created_at` in new modules.
4. **Committed browser regression test** `tests/webapp/test_autonomy_6c_browser.py`, following the existing pytest-playwright pattern of `tests/webapp/test_handoff_browser_smoke.py` (live uvicorn server started by the test, headless Chromium): the inbox renders, the badge updates after a notification, enrol/unenrol, candidate Promote/Dismiss, and "Review this pack" writes a latch. A scratchpad script may be used for diagnosis only; acceptance relies on the committed test.
5. **Migrations:** a fresh database gets 18 migrations and re-running is a no-op; a **pre-6C database built by `master@20979b9` code** (`git archive 20979b9 webapp product` into the scratchpad) with enrolled-looking data, pending reservations and review decisions upgrades through 018 with rows preserved, `review_decisions.decision_provenance = 'USER'`, FK and integrity checks clean, re-run no-op.
6. **Full suite** in the six foreground chunks from Task 1; record totals and runtime.
7. **Diff boundary** against `master@20979b9`: only 6C files and the hooks listed in this plan; no 6D/6E code.
8. Report readiness. Do not push or open a PR.

**Commit:** `test(autonomy): add 6C concurrency, acceptance and structural checks`

---

## Self-review

- **Spec coverage:** §2 invariants → Global Constraints + Tasks 11–13, 15; §4 data model → Tasks 2, 4–7; §5 controls/enrolment → Tasks 11–13; §6 queue/tick/leases/authorization reuse → Tasks 5, 9, 13; §7 candidates → Tasks 9–10; §8 preparation/Gate 4/latch → Tasks 7–8, 11; §9 inbox/propagation/notifications → Tasks 4, 12, 14; §10 recovery/retries/sweeps → Tasks 4, 8, 12, 13; §11 budget → Tasks 3, 6, 10, 13; §12 dossier → Task 14; §13 API → Task 14; §14 settings → Task 3; §15 testing → every task + Task 15; §16 closure → Task 15.
- **Carried requirements:** float normalization to `Decimal` before canonical hashing (Tasks 8, 10); WAL as its own commit with a full-suite run (Task 1); one-shot retries and 3 retries / 4 attempts (Tasks 4, 8, 12, 13); lease expiry fence on both queues (Tasks 5, 13); per-reservation settlement refs (Tasks 6, 13); no-overlap drivers (Task 13); single owner of reservations/attempts = the scheduler (Tasks 10, 13); DB-level provenance/basis and subject-pair triggers on INSERT and UPDATE (Task 2); earliest-boundary validity horizon (Task 9); candidate attempts with NULL decision id (Tasks 2, 10); user Promote outside the cap (Task 10).
- **Names:** `run_immediate`, `decide_and_record`, `build_context`, `evaluate_authorization`, `decision_inputs_payload`, `live_intent`, `upsert_queue_item`, `system_confirm_application_pack`, `list_outstanding_review_items`, `mechanical_review`, `next_prepare_step`, `evaluate_candidate_promotion`, `admit_candidate_evaluation`, `authorize_prepare` are used consistently across tasks.
