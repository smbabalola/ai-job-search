# Ticket 12B — Evidence Profile Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user edit the canonical Evidence Profile Markdown source in-app after initial setup, through a preview-then-confirm pipeline with exact-preview binding, file-content-hash optimistic concurrency, and crash-safe atomic commit — without weakening evidence integrity, without a second source of profile-history truth, and without touching Ticket 7, Lane B, Gate 4, or Search Workspaces.

**Architecture:** A new `product/profile_editing_schema.py` declares, per editable concept, where it lives in the canonical `/setup`-generated Markdown shape and what `(category, field)` it must parse back to via the *existing*, unmodified `product/profile_snapshot.py`. Repeatable concepts (employment, education) are addressed by canonical-order position (`employment[0].job_title`), never by "some claim has this value." A new `webapp/services/profile_editing.py` implements `start_edit` (reading the canonical Markdown directly, never aggregated snapshot claims) → `preview_edit` → `confirm_edit`, backed by a new `profile_edit_changesets` table (added via a generalized, list-driven `apply_migrations`) that records exact-preview-bound prospective bytes and drives four-way crash recovery on startup (resume / roll back / fail-closed conflict / the new `commit_started` pre-write state that closes a crash-window gap found during PM review). A new `webapp/api/profile_editing.py` router exposes this over HTTP following the existing `webapp/api/search_workspaces.py` `409`-on-conflict convention, with a stateless `session_id` and an explicit recovery-status endpoint. `webapp/templates/profile.html` gains Edit and Preview states alongside its existing View-only mode, with automated browser coverage.

**Tech Stack:** Python 3, FastAPI + Pydantic (`StrictBody` pattern), SQLite (stdlib `sqlite3`), Jinja2 templates, existing vanilla-JS `webapp/static/app.js` patterns, Playwright (`test_browser_smoke.py` conventions).

**Spec:** `docs/superpowers/specs/2026-08-22-ticket12b-evidence-profile-manager-design.md` (amended after PM review round 3 — read the amended sections, particularly "Confirm sequence," "Three-way crash recovery," "Decision area 1," and "API surface," before starting; they changed materially from the version this plan was first drafted against).

## Global Constraints

- **Exact-preview binding**: `confirm_edit` takes only a `changeset_id`. It never accepts an edits payload and never re-renders Markdown from one — it writes exactly the bytes stored at preview time, after re-verifying their own hash. (Spec: "Decision area 3", "Confirm sequence" step 3.)
- **No write path outside preview → confirm.** No PATCH/PUT endpoint for profile source files, ever. (Spec: "API surface".)
- **Concurrency is direct file-content SHA-256 hashing** of all three `product.profile_snapshot.SOURCE_PATHS` files plus the current snapshot's `content_id` (via `product.job_fit.profile_snapshot_content_id`) — never a DB revision counter. (Spec: "Decision area 2".)
- **`commit_started` is claimed atomically, before the file write, and is the confirm-race serialization point.** `UPDATE profile_edit_changesets SET status = 'commit_started' WHERE id = ? AND status = 'previewed'` — if the update affects zero rows, another confirm already won; return `409`. This closes a crash-window gap the design's second review round found (a crash between file write and the old `file_written` marker was previously invisible to recovery) and prevents two sibling previews from both writing the file. (Spec: "Confirm sequence" step 5, "Three-way crash recovery".)
- **Recovery scans BOTH `commit_started` and `file_written`**, applying the same three-way file-content comparison to each: equals `new_markdown` → resume; equals `previous_markdown` → roll back; equals neither → `recovery_conflict`, fail closed, never guess, never write. (Spec: "Three-way crash recovery".)
- **`start_edit` parses the canonical Markdown directly — never aggregated snapshot claims.** The current snapshot is consulted only for `conflicts` annotations and `precondition_snapshot_content_id`, never for editable field *values*, since the snapshot aggregates all three sources (including the two read-only ones) and could otherwise surface a read-only or conflict-losing value as if it were editable. (Spec: "Decision area 1", the `start_edit` correction.)
- **Repeatable fields use canonical-order positional addressing** (`employment[0].job_title`, `employment[1].job_title`, ...), sound because the whole-file precondition hash guarantees ordering hasn't shifted between calls. The rewriter and the schema/parser reconciliation check must both operate on the specific targeted record — never "does some record anywhere have the expected value." (Spec: "Decision area 1", the record-addressing correction.)
- **`new_markdown`/`previous_markdown` are transient**, cleared to `NULL` only on reaching `committed`, `rolled_back`, or `expired`. **Never cleared on `recovery_conflict`** — that row still needs its bytes for manual resolution. Abandoned `previewed` rows and commit-invalidated sibling previews must actually expire (bounded-age sweep, `expired` terminal state) — this is a real mechanism, not just a storage-clearing rule, since without it these rows accumulate indefinitely. (Spec: "Storage rule", "Expiry".)
- **Editing schema and snapshot parser are independently versioned and reconciled at preview time**, never assumed consistent. A field whose rewritten Markdown doesn't parse back to its declared `(category, field)` **for the specific targeted record** is a hard preview failure. (Spec: "Decision area 1".)
- **Unmanaged Markdown content is preserved byte-for-byte or the edit fails closed** — never silently discarded, never guessed at. (Spec: "Decision area 1".)
- **Editing schema targets only the canonical `/setup`-generated Markdown shape** (Identity as field-list; Languages/Education as tables; Professional Experience as `### Title - Company (Start - End)` sub-headings with a location line + bullets; Independent Projects/Technical Skills/Publications/Awards/Certifications as flat bulleted lists) — confirmed as the actual current template shape in `.claude/skills/job-application-assistant/01-candidate-profile.md`. A section not in this shape fails closed with an explicit "not in an editable format" error; it remains fully readable (unchanged parser), just not editable via this feature until brought into canonical shape.
- **No changes to `product/profile_snapshot.py`'s parsing/build logic**, Ticket 7, Lane B, Gate 4, or Search Workspaces. Every touchpoint with existing systems (`build_snapshot`, `refresh_profile`, `profile_snapshot_content_id`) is used exactly as `webapp/services/profile_setup.py` already uses them.
- **No `409` response body ever includes the changed source's actual content** — only identity/hash information, per the spec's conflict-response shape.
- **`session_id` is a stateless hash of `editing_schema_version` + the precondition tuple — no server-side session table.** (Spec: "API surface", `POST /api/profile/edit-sessions` correction.)
- **Every route (not only the UI) rejects with `409` (`recovery_pending`) while any `profile_edit_changesets` row is at `commit_started`, `file_written`, or `recovery_conflict`.** Backed by `GET /api/profile/recovery-status`. (Spec: "API surface" correction.)

---

## File Structure

| File | Responsibility |
|---|---|
| `product/profile_editing_schema.py` | New. Declares editable fields (including canonical-order record addressing for repeatable concepts), their canonical-Markdown location pattern, and their expected `(category, field)` in a rebuilt snapshot. `PROFILE_EDITING_SCHEMA_VERSION` constant. |
| `webapp/persistence/profile_edit_changesets.py` | New. Row-level CRUD for the `profile_edit_changesets` table: insert at preview, atomic status-claim transitions, read for confirm/recovery, clear transient columns, list rows by status, expire abandoned/invalidated rows. |
| `webapp/persistence/migrations.py` | Modify. Generalize `apply_migrations` into a list-driven loop; add migration `002_profile_edit_changesets`. |
| `webapp/services/profile_editing.py` | New. `start_edit` (canonical-Markdown-direct), `preview_edit`, `confirm_edit` (with the atomic `commit_started` claim), `recover_interrupted_changesets` (startup recovery over both pre-terminal statuses), `expire_stale_changesets`, hash computation. |
| `webapp/services/profile_markdown_rewrite.py` | New/expanded. Markdown section rewrite with record addressing and unmanaged-content preservation. |
| `webapp/api/profile_editing.py` | New. FastAPI router: `POST /api/profile/edit-sessions`, `POST /api/profile/edit-sessions/{session_id}/preview`, `POST /api/profile/edit-changesets/{id}/confirm`, `GET /api/profile/edit-changesets/{id}`, `GET /api/profile/recovery-status`. Recovery-pending guard on every route. |
| `webapp/app.py` | Modify. Register the new router; call `recover_interrupted_changesets` at startup, after migrations, before the app serves traffic. |
| `webapp/templates/profile.html` | Modify. Add Edit and Preview states; update the two lines of copy that currently claim editing doesn't happen here; recovery banner. |
| `webapp/static/app.js` | Modify. Client-side flow for start/preview/confirm, 409 handling, recovery-banner display via the new status endpoint. |
| Tests | `tests/test_profile_editing_schema.py`, `tests/webapp/persistence/test_migrations.py`, `tests/webapp/persistence/test_profile_edit_changesets.py`, `tests/webapp/services/test_profile_markdown_rewrite.py`, `tests/webapp/services/test_profile_editing.py`, `tests/webapp/api/test_profile_editing_routes.py`, extension to `tests/webapp/test_browser_smoke.py`. |

---

## Task 1: Generalize `apply_migrations` into a list-driven loop (no new table yet)

**Files:**
- Modify: `webapp/persistence/migrations.py`
- Test: `tests/webapp/persistence/test_migrations.py` (create if it doesn't already exist — check first)

**Interfaces:**
- Consumes: existing `_execute_statements`, `_now`, `_migrate_search_workspaces` (all unchanged).
- Produces: `MIGRATIONS: list[tuple[str, Callable[[sqlite3.Connection], None]]]` module constant; `apply_migrations` becomes a loop over it.

- [ ] **Step 1: Check for an existing migrations test file**

Run: `python -c "import pathlib; print(pathlib.Path('tests/webapp/persistence/test_migrations.py').exists())"`

If it exists, read it first and extend it in its existing style. If not, this task creates it.

- [ ] **Step 2: Write the failing test**

```python
import sqlite3

from webapp.persistence.migrations import MIGRATIONS, apply_migrations


def test_migrations_is_an_ordered_list_starting_with_the_existing_migration():
    ids = [migration_id for migration_id, _ in MIGRATIONS]
    assert ids[0] == "001_search_workspaces"
    assert len(ids) == len(set(ids)), "migration ids must be unique"


def test_apply_migrations_records_every_migration_id():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    recorded = {
        row["id"] for row in conn.execute("SELECT id FROM schema_migrations").fetchall()
    }
    expected = {migration_id for migration_id, _ in MIGRATIONS}
    assert recorded == expected


def test_apply_migrations_is_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    apply_migrations(conn)  # must not raise or re-apply
    recorded = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
    assert recorded == len(MIGRATIONS)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/webapp/persistence/test_migrations.py -v`
Expected: FAIL — `MIGRATIONS` doesn't exist yet (the current file only has `MIGRATION_ID`, a single string).

- [ ] **Step 4: Read the current `apply_migrations` in full before editing**

Read `webapp/persistence/migrations.py`'s `MIGRATION_ID` constant and `apply_migrations` function in full — do not assume the shape from this plan; re-read the actual current file, since other work may have touched it between plan-writing and implementation.

- [ ] **Step 5: Implement the generalized loop**

Replace:
```python
MIGRATION_ID = "001_search_workspaces"
```
with:
```python
MIGRATIONS: list[tuple[str, "Callable[[sqlite3.Connection], None]"]] = [
    ("001_search_workspaces", lambda conn: _migrate_search_workspaces(conn)),
]
```
(Add `from typing import Callable` to the imports if not already present via `from __future__ import annotations` — check whether the file already has that future import, which would make the string-quoted type hint unnecessary; match whatever the file's existing style is.)

Replace the body of `apply_migrations` (the part after the `schema_migrations` table creation) with:

```python
def apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    already_applied = {
        row[0] for row in conn.execute("SELECT id FROM schema_migrations").fetchall()
    }
    for migration_id, migrate_fn in MIGRATIONS:
        if migration_id in already_applied:
            continue
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            migrate_fn(conn)
            conn.execute(
                "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
                (migration_id, _now()),
            )
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"migration {migration_id!r} created foreign-key violations: {violations!r}"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
```

This preserves every existing behavior (same `schema_migrations` table, same per-migration transaction, same foreign-key-check pattern, same `PRAGMA foreign_keys` toggle) — it only generalizes "one hardcoded id" into "a loop over a list," so `001_search_workspaces` continues to run exactly as it did before for any database that hasn't yet applied it.

- [ ] **Step 6: Run the test to verify it passes**

Run: `python -m pytest tests/webapp/persistence/test_migrations.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Run the full persistence + migration-dependent test suite for regressions**

Run: `python -m pytest tests/webapp/persistence/ tests/webapp/services/test_pipeline_fit_and_intelligence.py -v`
Expected: PASS, no regressions — this touches shared startup-path code every webapp test that creates a fresh DB depends on.

- [ ] **Step 8: Commit**

```bash
git add webapp/persistence/migrations.py tests/webapp/persistence/test_migrations.py
git commit -m "Generalize apply_migrations into an ordered migration list"
```

---

## Task 2: `profile_edit_changesets` table (migration 002) — includes `commit_started` and `expired`

**Files:**
- Modify: `webapp/persistence/migrations.py`
- Test: `tests/webapp/persistence/test_migrations.py`

**Interfaces:**
- Consumes: `MIGRATIONS` list, `_execute_statements`, `_now` from Task 1.
- Produces: `profile_edit_changesets` table with the full six-state status enum (`previewed`, `commit_started`, `file_written`, `committed`, `rolled_back`, `recovery_conflict`, `expired` — seven, not six; count carefully against the spec's DDL, not from memory), migration id `002_profile_edit_changesets` appended to `MIGRATIONS`.

- [ ] **Step 1: Write the failing test**

```python
def test_profile_edit_changesets_table_exists_after_migration():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(profile_edit_changesets)").fetchall()
    }
    assert columns == {
        "id", "status", "editing_schema_version", "precondition_source_hashes",
        "precondition_snapshot_content_id", "prospective_source_hash", "diff_summary",
        "new_markdown", "previous_markdown", "created_at", "committed_at",
    }


def test_profile_edit_changesets_status_check_constraint_rejects_unknown_status():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO profile_edit_changesets "
            "(id, status, editing_schema_version, precondition_source_hashes, "
            "precondition_snapshot_content_id, prospective_source_hash, diff_summary, "
            "new_markdown, previous_markdown, created_at) "
            "VALUES ('pec_1', 'not_a_real_status', 'v0', '{}', 'x', 'y', '{}', 'm', 'p', 'now')"
        )


def test_profile_edit_changesets_status_check_constraint_accepts_all_seven_states():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    for i, status in enumerate((
        "previewed", "commit_started", "file_written", "committed",
        "rolled_back", "recovery_conflict", "expired",
    )):
        conn.execute(
            "INSERT INTO profile_edit_changesets "
            "(id, status, editing_schema_version, precondition_source_hashes, "
            "precondition_snapshot_content_id, prospective_source_hash, diff_summary, "
            "new_markdown, previous_markdown, created_at) "
            "VALUES (?, ?, 'v0', '{}', 'x', 'y', '{}', 'm', 'p', 'now')",
            (f"pec_{i}", status),
        )  # must not raise
    conn.commit()
```

Add `import pytest` to the test file's imports if not already present.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/webapp/persistence/test_migrations.py -v`
Expected: FAIL — table doesn't exist yet.

- [ ] **Step 3: Add the migration function and DDL**

In `webapp/persistence/migrations.py`, add a new function (placed after `_migrate_search_workspaces` and before `_rebuild_discovery_tables`, matching the file's existing top-to-bottom ordering of migration-content functions):

```python
def _migrate_profile_edit_changesets(conn: sqlite3.Connection) -> None:
    _execute_statements(conn,
        """
        CREATE TABLE profile_edit_changesets (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK (status IN (
                'previewed', 'commit_started', 'file_written', 'committed',
                'rolled_back', 'recovery_conflict', 'expired'
            )),
            editing_schema_version TEXT NOT NULL,
            precondition_source_hashes TEXT NOT NULL,
            precondition_snapshot_content_id TEXT NOT NULL,
            prospective_source_hash TEXT NOT NULL,
            diff_summary TEXT NOT NULL,
            new_markdown TEXT,
            previous_markdown TEXT,
            created_at TEXT NOT NULL,
            committed_at TEXT
        );

        CREATE INDEX idx_profile_edit_changesets_status
            ON profile_edit_changesets(status);
        """
    )
```

Note the seven-value status enum: `previewed`, **`commit_started`** (new — the atomic pre-write claim state), `file_written`, `committed`, `rolled_back`, `recovery_conflict`, **`expired`** (new — the terminal state for abandoned/invalidated rows). Both new values are part of the spec's round-3 amendment; do not implement only the original five.

Append to `MIGRATIONS`:

```python
MIGRATIONS: list[tuple[str, "Callable[[sqlite3.Connection], None]"]] = [
    ("001_search_workspaces", lambda conn: _migrate_search_workspaces(conn)),
    ("002_profile_edit_changesets", lambda conn: _migrate_profile_edit_changesets(conn)),
]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/webapp/persistence/test_migrations.py -v`
Expected: PASS (6 tests total)

- [ ] **Step 5: Run full persistence suite for regressions**

Run: `python -m pytest tests/webapp/persistence/ -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add webapp/persistence/migrations.py tests/webapp/persistence/test_migrations.py
git commit -m "Add profile_edit_changesets table with commit_started/expired states (migration 002)"
```

---

## Task 3: `profile_edit_changesets` persistence layer — atomic claim, seven-state transitions, expiry

**Files:**
- Create: `webapp/persistence/profile_edit_changesets.py`
- Test: `tests/webapp/persistence/test_profile_edit_changesets.py`

**Interfaces:**
- Consumes: `sqlite3.Connection` (matching `webapp/persistence/artifacts.py`'s style: this module itself takes a raw connection).
- Produces:
  - `insert_previewed_changeset(conn, *, changeset_id: str, editing_schema_version: str, precondition_source_hashes: dict[str, str], precondition_snapshot_content_id: str, prospective_source_hash: str, diff_summary: dict, new_markdown: str, previous_markdown: str, created_at: str) -> None`
  - `get_changeset(conn, changeset_id: str) -> dict | None`
  - `try_claim_commit_started(conn, changeset_id: str) -> bool` — **the atomic serialization primitive**: `UPDATE ... SET status='commit_started' WHERE id=? AND status='previewed'`, returns `True` iff exactly one row was updated (i.e. this caller won the race), `False` otherwise (another confirm already claimed it, or the row isn't in a claimable state).
  - `mark_file_written(conn, changeset_id: str) -> None`
  - `mark_committed(conn, changeset_id: str, *, committed_at: str) -> None` (also clears `new_markdown`/`previous_markdown`)
  - `mark_rolled_back(conn, changeset_id: str) -> None` (also clears `new_markdown`/`previous_markdown`)
  - `mark_recovery_conflict(conn, changeset_id: str) -> None` (does **not** clear the transient columns)
  - `mark_expired(conn, changeset_id: str) -> None` (also clears `new_markdown`/`previous_markdown`)
  - `list_pre_terminal_changesets(conn) -> list[dict]` — rows at `commit_started` OR `file_written`, for startup recovery (replaces the round-1 plan's `list_file_written_changesets`, which only scanned one status).
  - `list_expirable_changesets(conn, *, cutoff_created_at: str) -> list[dict]` — rows at `previewed` with `created_at < cutoff_created_at` (abandoned), for the expiry sweep.
  - `is_recovery_pending(conn) -> bool` — `True` iff any row is at `commit_started`, `file_written`, or `recovery_conflict`; backs both the API-layer guard (Task 10) and `GET /api/profile/recovery-status`.

- [ ] **Step 1: Write the failing test**

```python
import json
import sqlite3

import pytest

from webapp.persistence.migrations import apply_migrations
from webapp.persistence.profile_edit_changesets import (
    get_changeset,
    insert_previewed_changeset,
    is_recovery_pending,
    list_expirable_changesets,
    list_pre_terminal_changesets,
    mark_committed,
    mark_expired,
    mark_file_written,
    mark_recovery_conflict,
    mark_rolled_back,
    try_claim_commit_started,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_migrations(c)
    yield c
    c.close()


def _insert(conn, changeset_id="pec_1", created_at="2026-01-01T00:00:00Z"):
    insert_previewed_changeset(
        conn, changeset_id=changeset_id, editing_schema_version="v0",
        precondition_source_hashes={"CLAUDE.md": "aaa"},
        precondition_snapshot_content_id="cps_1",
        prospective_source_hash="bbb",
        diff_summary={"added": []},
        new_markdown="# new", previous_markdown="# old",
        created_at=created_at,
    )


def test_insert_and_get_roundtrip(conn):
    _insert(conn)
    row = get_changeset(conn, "pec_1")
    assert row["status"] == "previewed"
    assert row["new_markdown"] == "# new"
    assert row["previous_markdown"] == "# old"
    assert json.loads(row["precondition_source_hashes"]) == {"CLAUDE.md": "aaa"}


def test_get_missing_returns_none(conn):
    assert get_changeset(conn, "nope") is None


def test_try_claim_commit_started_succeeds_from_previewed(conn):
    _insert(conn)
    assert try_claim_commit_started(conn, "pec_1") is True
    assert get_changeset(conn, "pec_1")["status"] == "commit_started"


def test_try_claim_commit_started_fails_if_not_previewed(conn):
    _insert(conn)
    assert try_claim_commit_started(conn, "pec_1") is True  # first claim wins
    assert try_claim_commit_started(conn, "pec_1") is False  # second claim on the SAME row loses
    assert get_changeset(conn, "pec_1")["status"] == "commit_started"  # unchanged by the loser


def test_try_claim_commit_started_on_unknown_id_returns_false(conn):
    assert try_claim_commit_started(conn, "nope") is False


def test_mark_file_written_transitions_status(conn):
    _insert(conn)
    try_claim_commit_started(conn, "pec_1")
    mark_file_written(conn, "pec_1")
    assert get_changeset(conn, "pec_1")["status"] == "file_written"


def test_mark_committed_clears_transient_columns(conn):
    _insert(conn)
    try_claim_commit_started(conn, "pec_1")
    mark_file_written(conn, "pec_1")
    mark_committed(conn, "pec_1", committed_at="2026-01-01T00:01:00Z")
    row = get_changeset(conn, "pec_1")
    assert row["status"] == "committed"
    assert row["new_markdown"] is None
    assert row["previous_markdown"] is None
    assert row["committed_at"] == "2026-01-01T00:01:00Z"


def test_mark_rolled_back_clears_transient_columns(conn):
    _insert(conn)
    try_claim_commit_started(conn, "pec_1")
    mark_rolled_back(conn, "pec_1")
    row = get_changeset(conn, "pec_1")
    assert row["status"] == "rolled_back"
    assert row["new_markdown"] is None
    assert row["previous_markdown"] is None


def test_mark_recovery_conflict_does_not_clear_transient_columns(conn):
    _insert(conn)
    try_claim_commit_started(conn, "pec_1")
    mark_file_written(conn, "pec_1")
    mark_recovery_conflict(conn, "pec_1")
    row = get_changeset(conn, "pec_1")
    assert row["status"] == "recovery_conflict"
    assert row["new_markdown"] == "# new"
    assert row["previous_markdown"] == "# old"


def test_mark_expired_clears_transient_columns(conn):
    _insert(conn)
    mark_expired(conn, "pec_1")
    row = get_changeset(conn, "pec_1")
    assert row["status"] == "expired"
    assert row["new_markdown"] is None
    assert row["previous_markdown"] is None


def test_list_pre_terminal_changesets_returns_commit_started_and_file_written_only(conn):
    _insert(conn, "pec_1")
    _insert(conn, "pec_2")
    _insert(conn, "pec_3")
    try_claim_commit_started(conn, "pec_1")  # stays at commit_started
    try_claim_commit_started(conn, "pec_2")
    mark_file_written(conn, "pec_2")  # advances to file_written
    # pec_3 stays at previewed -- must NOT be returned
    ids = {row["id"] for row in list_pre_terminal_changesets(conn)}
    assert ids == {"pec_1", "pec_2"}


def test_list_expirable_changesets_only_returns_old_previewed_rows(conn):
    _insert(conn, "pec_old", created_at="2020-01-01T00:00:00Z")
    _insert(conn, "pec_new", created_at="2030-01-01T00:00:00Z")
    ids = {row["id"] for row in list_expirable_changesets(conn, cutoff_created_at="2026-01-01T00:00:00Z")}
    assert ids == {"pec_old"}


def test_list_expirable_changesets_excludes_non_previewed_rows(conn):
    _insert(conn, "pec_1", created_at="2020-01-01T00:00:00Z")
    try_claim_commit_started(conn, "pec_1")  # no longer 'previewed'
    ids = {row["id"] for row in list_expirable_changesets(conn, cutoff_created_at="2026-01-01T00:00:00Z")}
    assert ids == set()


def test_is_recovery_pending_false_when_clean(conn):
    assert is_recovery_pending(conn) is False


def test_is_recovery_pending_true_for_commit_started(conn):
    _insert(conn)
    try_claim_commit_started(conn, "pec_1")
    assert is_recovery_pending(conn) is True


def test_is_recovery_pending_true_for_file_written(conn):
    _insert(conn)
    try_claim_commit_started(conn, "pec_1")
    mark_file_written(conn, "pec_1")
    assert is_recovery_pending(conn) is True


def test_is_recovery_pending_true_for_recovery_conflict(conn):
    _insert(conn)
    try_claim_commit_started(conn, "pec_1")
    mark_file_written(conn, "pec_1")
    mark_recovery_conflict(conn, "pec_1")
    assert is_recovery_pending(conn) is True


def test_is_recovery_pending_false_after_committed(conn):
    _insert(conn)
    try_claim_commit_started(conn, "pec_1")
    mark_file_written(conn, "pec_1")
    mark_committed(conn, "pec_1", committed_at="2026-01-01T00:01:00Z")
    assert is_recovery_pending(conn) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/webapp/persistence/test_profile_edit_changesets.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""Row-level persistence for Evidence Profile edit change-sets.

This table is operational recovery/audit state, never a second source of
profile-history truth -- new_markdown/previous_markdown are cleared as soon
as a row reaches a resolved terminal state (committed, rolled_back, expired).
They are deliberately NOT cleared on recovery_conflict, since that status
means the recovery decision is still ambiguous and the bytes are the
material needed to resolve it.

try_claim_commit_started is the confirm-time concurrency serialization
point: two sibling previews (same precondition) that both pass their hash
checks race on this single conditional UPDATE, and only one can win.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def insert_previewed_changeset(
    conn: sqlite3.Connection, *, changeset_id: str, editing_schema_version: str,
    precondition_source_hashes: dict[str, str], precondition_snapshot_content_id: str,
    prospective_source_hash: str, diff_summary: dict[str, Any],
    new_markdown: str, previous_markdown: str, created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO profile_edit_changesets "
        "(id, status, editing_schema_version, precondition_source_hashes, "
        "precondition_snapshot_content_id, prospective_source_hash, diff_summary, "
        "new_markdown, previous_markdown, created_at) "
        "VALUES (?, 'previewed', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            changeset_id, editing_schema_version,
            json.dumps(precondition_source_hashes, sort_keys=True),
            precondition_snapshot_content_id, prospective_source_hash,
            json.dumps(diff_summary, sort_keys=True),
            new_markdown, previous_markdown, created_at,
        ),
    )
    conn.commit()


def get_changeset(conn: sqlite3.Connection, changeset_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM profile_edit_changesets WHERE id = ?", (changeset_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def try_claim_commit_started(conn: sqlite3.Connection, changeset_id: str) -> bool:
    cursor = conn.execute(
        "UPDATE profile_edit_changesets SET status = 'commit_started' "
        "WHERE id = ? AND status = 'previewed'",
        (changeset_id,),
    )
    conn.commit()
    return cursor.rowcount == 1


def mark_file_written(conn: sqlite3.Connection, changeset_id: str) -> None:
    conn.execute(
        "UPDATE profile_edit_changesets SET status = 'file_written' WHERE id = ?",
        (changeset_id,),
    )
    conn.commit()


def mark_committed(conn: sqlite3.Connection, changeset_id: str, *, committed_at: str) -> None:
    conn.execute(
        "UPDATE profile_edit_changesets SET status = 'committed', committed_at = ?, "
        "new_markdown = NULL, previous_markdown = NULL WHERE id = ?",
        (committed_at, changeset_id),
    )
    conn.commit()


def mark_rolled_back(conn: sqlite3.Connection, changeset_id: str) -> None:
    conn.execute(
        "UPDATE profile_edit_changesets SET status = 'rolled_back', "
        "new_markdown = NULL, previous_markdown = NULL WHERE id = ?",
        (changeset_id,),
    )
    conn.commit()


def mark_recovery_conflict(conn: sqlite3.Connection, changeset_id: str) -> None:
    # Deliberately does NOT clear new_markdown/previous_markdown -- this status
    # means the recovery decision is still ambiguous and those bytes are the
    # material needed to resolve it. See module docstring.
    conn.execute(
        "UPDATE profile_edit_changesets SET status = 'recovery_conflict' WHERE id = ?",
        (changeset_id,),
    )
    conn.commit()


def mark_expired(conn: sqlite3.Connection, changeset_id: str) -> None:
    conn.execute(
        "UPDATE profile_edit_changesets SET status = 'expired', "
        "new_markdown = NULL, previous_markdown = NULL WHERE id = ?",
        (changeset_id,),
    )
    conn.commit()


def list_pre_terminal_changesets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM profile_edit_changesets "
        "WHERE status IN ('commit_started', 'file_written')"
    ).fetchall()
    return [dict(row) for row in rows]


def list_expirable_changesets(conn: sqlite3.Connection, *, cutoff_created_at: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM profile_edit_changesets "
        "WHERE status = 'previewed' AND created_at < ?",
        (cutoff_created_at,),
    ).fetchall()
    return [dict(row) for row in rows]


def is_recovery_pending(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM profile_edit_changesets "
        "WHERE status IN ('commit_started', 'file_written', 'recovery_conflict') LIMIT 1"
    ).fetchone()
    return row is not None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/webapp/persistence/test_profile_edit_changesets.py -v`
Expected: PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
git add webapp/persistence/profile_edit_changesets.py tests/webapp/persistence/test_profile_edit_changesets.py
git commit -m "Add profile_edit_changesets persistence layer: atomic claim, recovery, expiry"
```

---

## Task 4: Editing schema with canonical-order record addressing

**Files:**
- Create: `product/profile_editing_schema.py`
- Test: `tests/test_profile_editing_schema.py`

**Interfaces:**
- Consumes: nothing from other tasks — this is a standalone declarative module.
- Produces:
  - `PROFILE_EDITING_SCHEMA_VERSION: str` (e.g. `"profile-editing-schema.v0"`)
  - `EditableFieldSpec` (frozen dataclass): `section: tuple[str, ...]` (canonical Markdown heading path), `category: str`, `field: str` (the `(category, field)` this must parse back to), `repeatable: bool`.
  - `SINGLETON_FIELDS: dict[str, EditableFieldSpec]` — one entry per non-repeatable editable concept, keyed by a stable field id (e.g. `"identity.name"`).
  - `REPEATABLE_FIELD_GROUPS: dict[str, tuple[EditableFieldSpec, ...]]` — one entry per repeatable concept (e.g. `"employment"`), listing the `EditableFieldSpec`s that make up ONE record of that concept (e.g. `job_title`, `employer`, `date_range` for `"employment"`). **Record addressing is a function of position, not stored in the spec itself** — `field_id_for(group_name: str, index: int, sub_field: str) -> str` builds the addressable id string, e.g. `field_id_for("employment", 0, "job_title") == "employment[0].job_title"`, and `parse_repeatable_field_id(field_id: str) -> tuple[str, int, str] | None` does the reverse, returning `(group_name, index, sub_field)` or `None` if `field_id` isn't a valid repeatable-field address.

This is a materially different shape from a single flat `EDITABLE_FIELDS` dict (the round-1 plan's shape) — repeatable concepts are now explicitly modeled as record groups with positional addressing, per the spec's round-3 correction. Do not implement the flat-dict-with-`repeatable: bool` shape; it cannot represent "which specific record."

- [ ] **Step 1: Determine the exact category/field vocabulary before writing the schema**

Run: `python -c "
import re
text = open('product/profile_snapshot.py', encoding='utf-8').read()
for m in re.finditer(r'category=\"(\w+)\",\s*\n?\s*field=\"(\w+)\"', text):
    print(m.group(1), m.group(2))
"`

Cross-reference this output against the canonical template's actual sections (`.claude/skills/job-application-assistant/01-candidate-profile.md`: Identity, Languages, Education, Professional Experience, Independent Projects, Technical Skills [Programming & ML / Domain Expertise / Software & Tools], Publications, Awards). Do not invent a `(category, field)` pair that doesn't come from this real enumeration.

- [ ] **Step 2: Write the failing tests**

```python
from product.profile_editing_schema import (
    PROFILE_EDITING_SCHEMA_VERSION,
    REPEATABLE_FIELD_GROUPS,
    SINGLETON_FIELDS,
    field_id_for,
    parse_repeatable_field_id,
)


def test_schema_version_is_a_nonempty_string():
    assert isinstance(PROFILE_EDITING_SCHEMA_VERSION, str) and PROFILE_EDITING_SCHEMA_VERSION


def test_singleton_fields_declare_section_category_field():
    assert SINGLETON_FIELDS, "must declare at least one singleton field"
    for field_id, spec in SINGLETON_FIELDS.items():
        assert spec.section
        assert spec.category
        assert spec.field
        assert spec.repeatable is False


def test_identity_name_is_a_singleton_field():
    spec = SINGLETON_FIELDS["identity.name"]
    assert spec.category == "identity"
    assert spec.field == "name"


def test_employment_is_a_repeatable_group_with_expected_subfields():
    assert "employment" in REPEATABLE_FIELD_GROUPS
    sub_fields = {spec.field for spec in REPEATABLE_FIELD_GROUPS["employment"]}
    assert {"job_title", "employer", "date_range"} <= sub_fields
    for spec in REPEATABLE_FIELD_GROUPS["employment"]:
        assert spec.repeatable is True
        assert spec.category == "employment"


def test_field_id_for_builds_positional_address():
    assert field_id_for("employment", 0, "job_title") == "employment[0].job_title"
    assert field_id_for("employment", 1, "date_range") == "employment[1].date_range"


def test_parse_repeatable_field_id_round_trips():
    assert parse_repeatable_field_id("employment[0].job_title") == ("employment", 0, "job_title")
    assert parse_repeatable_field_id("employment[12].employer") == ("employment", 12, "employer")


def test_parse_repeatable_field_id_rejects_singleton_and_malformed_ids():
    assert parse_repeatable_field_id("identity.name") is None
    assert parse_repeatable_field_id("employment.job_title") is None  # missing index
    assert parse_repeatable_field_id("not a field id") is None
```

(Adjust the exact field ids/assertions to match Step 1's real enumeration — this test skeleton establishes the schema's contract shape, not necessarily every literal field id.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_profile_editing_schema.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Implement the schema module**

```python
"""Versioned declaration of which Evidence Profile fields the in-app editor
may mutate, and where they live in the canonical /setup-generated Markdown
shape.

This is a SEPARATE authority from product/profile_snapshot.py's parser --
the parser is the sole authority for what a rewritten section actually
parses to; this schema is the sole authority for what the UI is permitted
to mutate and how it renders back to Markdown. The two are reconciled by an
explicit runtime check in webapp/services/profile_editing.py's preview
step, never assumed consistent.

Repeatable concepts (employment, education) are addressed by canonical-order
POSITION -- employment[0].job_title, employment[1].job_title, ... -- never
by "some record has this value." This is sound specifically because the
whole-file precondition hash (see webapp/services/profile_editing.py)
guarantees record ordering hasn't shifted between start_edit and
preview_edit/confirm_edit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PROFILE_EDITING_SCHEMA_VERSION = "profile-editing-schema.v0"


@dataclass(frozen=True)
class EditableFieldSpec:
    section: tuple[str, ...]
    category: str
    field: str
    repeatable: bool


SINGLETON_FIELDS: dict[str, EditableFieldSpec] = {
    "identity.name": EditableFieldSpec(
        section=("Identity",), category="identity", field="name", repeatable=False,
    ),
    # ... remaining singleton fields from Step 1's real enumeration ...
}

REPEATABLE_FIELD_GROUPS: dict[str, tuple[EditableFieldSpec, ...]] = {
    "employment": (
        EditableFieldSpec(section=("Professional Experience",), category="employment", field="job_title", repeatable=True),
        EditableFieldSpec(section=("Professional Experience",), category="employment", field="employer", repeatable=True),
        EditableFieldSpec(section=("Professional Experience",), category="employment", field="date_range", repeatable=True),
        # ... remaining employment sub-fields (location, responsibility_or_achievement) ...
    ),
    "education": (
        EditableFieldSpec(section=("Education",), category="education", field="qualification", repeatable=True),
        EditableFieldSpec(section=("Education",), category="education", field="institution", repeatable=True),
        EditableFieldSpec(section=("Education",), category="education", field="date_range", repeatable=True),
        # ... remaining education sub-fields ...
    ),
    # ... any other repeatable groups from Step 1's enumeration ...
}

_REPEATABLE_FIELD_ID_RE = re.compile(r"^([a-z_]+)\[(\d+)\]\.([a-z_]+)$")


def field_id_for(group_name: str, index: int, sub_field: str) -> str:
    return f"{group_name}[{index}].{sub_field}"


def parse_repeatable_field_id(field_id: str) -> tuple[str, int, str] | None:
    match = _REPEATABLE_FIELD_ID_RE.match(field_id)
    if match is None:
        return None
    group_name, index_str, sub_field = match.groups()
    if group_name not in REPEATABLE_FIELD_GROUPS:
        return None
    valid_sub_fields = {spec.field for spec in REPEATABLE_FIELD_GROUPS[group_name]}
    if sub_field not in valid_sub_fields:
        return None
    return group_name, int(index_str), sub_field
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_profile_editing_schema.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add product/profile_editing_schema.py tests/test_profile_editing_schema.py
git commit -m "Add versioned Evidence Profile editing schema with positional record addressing"
```

---

## Task 5: Markdown rewrite — canonical shape, specific-record targeting, unmanaged-content preservation

**Files:**
- Create: `webapp/services/profile_markdown_rewrite.py`
- Test: `tests/webapp/services/test_profile_markdown_rewrite.py`

**Interfaces:**
- Consumes: `SINGLETON_FIELDS`, `REPEATABLE_FIELD_GROUPS`, `parse_repeatable_field_id`, `EditableFieldSpec` from Task 4.
- Produces: `rewrite_canonical_markdown(current_markdown: str, edits: dict[str, Any]) -> str`, raising a dedicated `ProfileMarkdownRewriteError` on any fail-closed case — matches how `webapp/services/profile_setup.py` raises `PipelineError` for its own validation failures, so a dedicated exception (not a result-type return) is this codebase's established convention for "this input was rejected."

This is the highest-risk task in this plan. It must now handle two additional real requirements beyond the round-1 plan: **targeting a specific record by position** (not "the first record that looks right"), and doing so correctly even when two records share a duplicate value (positional targeting must not accidentally key off value equality anywhere in its implementation). Do not skip the negative-path or duplicate-value tests.

- [ ] **Step 1: Write the failing tests — singleton field positive path**

```python
import pytest

from webapp.services.profile_markdown_rewrite import (
    ProfileMarkdownRewriteError,
    rewrite_canonical_markdown,
)

SAMPLE_SINGLETON = """---
framework_version: 1.1.1
---

# Candidate Profile

## Identity
- **Name:** Ada Lovelace
- **Location:** London, UK

<!-- a custom comment nobody should touch -->

## Education

| Degree | Period | Institution | Key Topics |
|--------|--------|-------------|------------|
| BSc Mathematics | 2010-2013 | Example University | Numerical methods |
"""


def test_editing_a_singleton_field_preserves_everything_else_byte_for_byte():
    result = rewrite_canonical_markdown(SAMPLE_SINGLETON, {"identity.name": "Ada King"})
    assert "**Name:** Ada King" in result
    assert "<!-- a custom comment nobody should touch -->" in result
    assert "| BSc Mathematics | 2010-2013 | Example University | Numerical methods |" in result


def test_unedited_sections_are_byte_identical():
    result = rewrite_canonical_markdown(SAMPLE_SINGLETON, {"identity.name": "Ada King"})
    education_onward = SAMPLE_SINGLETON[SAMPLE_SINGLETON.index("## Education"):]
    assert education_onward in result
```

- [ ] **Step 2: Write the failing tests — multi-record positional targeting, including a duplicate value**

```python
SAMPLE_MULTI_EMPLOYMENT = """---
framework_version: 1.1.1
---

# Candidate Profile

## Identity
- **Name:** Ada Lovelace

## Professional Experience

### Data Engineer - Example Corp (2020 - 2023)
London, UK
- Built pipelines.

### Data Engineer - Other Corp (2018 - 2020)
Manchester, UK
- Migrated systems.
"""
# Note: both roles deliberately share the job_title "Data Engineer" -- this
# fixture exists specifically to prove positional targeting never keys off
# value equality.


def test_editing_second_records_field_does_not_affect_first_record():
    result = rewrite_canonical_markdown(
        SAMPLE_MULTI_EMPLOYMENT, {"employment[1].job_title": "Senior Data Engineer"}
    )
    assert "### Data Engineer - Example Corp (2020 - 2023)" in result  # first role UNCHANGED
    assert "### Senior Data Engineer - Other Corp (2018 - 2020)" in result  # second role changed


def test_editing_first_records_field_does_not_affect_second_record():
    result = rewrite_canonical_markdown(
        SAMPLE_MULTI_EMPLOYMENT, {"employment[0].date_range": "2020 - 2024"}
    )
    assert "### Data Engineer - Example Corp (2020 - 2024)" in result
    assert "### Data Engineer - Other Corp (2018 - 2020)" in result  # second role's dates UNCHANGED


def test_editing_both_records_same_field_independently():
    result = rewrite_canonical_markdown(
        SAMPLE_MULTI_EMPLOYMENT,
        {"employment[0].employer": "Example Corp Ltd", "employment[1].employer": "Other Corp Inc"},
    )
    assert "Example Corp Ltd" in result
    assert "Other Corp Inc" in result
    assert "Example Corp" in result and "Example Corp Ltd" in result  # substring sanity, not a false match
```

- [ ] **Step 3: Write the failing tests — fail-closed negative paths**

```python
def test_field_not_in_canonical_shape_fails_closed():
    non_canonical = SAMPLE_SINGLETON.replace(
        "- **Name:** Ada Lovelace", "Name: Ada Lovelace (no bold, no list marker)"
    )
    with pytest.raises(ProfileMarkdownRewriteError):
        rewrite_canonical_markdown(non_canonical, {"identity.name": "Ada King"})


def test_missing_section_entirely_fails_closed():
    no_identity = SAMPLE_SINGLETON.replace(
        "## Identity\n- **Name:** Ada Lovelace\n- **Location:** London, UK\n\n", ""
    )
    with pytest.raises(ProfileMarkdownRewriteError):
        rewrite_canonical_markdown(no_identity, {"identity.name": "Ada King"})


def test_unknown_field_id_fails_closed():
    with pytest.raises(ProfileMarkdownRewriteError):
        rewrite_canonical_markdown(SAMPLE_SINGLETON, {"not.a.real.field": "x"})


def test_out_of_range_record_index_fails_closed():
    # SAMPLE_MULTI_EMPLOYMENT has only 2 employment records (indices 0, 1)
    with pytest.raises(ProfileMarkdownRewriteError):
        rewrite_canonical_markdown(SAMPLE_MULTI_EMPLOYMENT, {"employment[5].job_title": "X"})


def test_malformed_repeatable_field_id_fails_closed():
    with pytest.raises(ProfileMarkdownRewriteError):
        rewrite_canonical_markdown(SAMPLE_MULTI_EMPLOYMENT, {"employment.job_title": "X"})  # missing index
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `python -m pytest tests/webapp/services/test_profile_markdown_rewrite.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 5: Implement**

The exact regex/line-identification patterns must mirror `product/profile_snapshot.py`'s `_parse_candidate_markdown` patterns for the canonical shape ONLY. For `identity.name`, the target line matches `MARKDOWN_FIELD_RE` (defined in `product/profile_snapshot.py`) for label `Name` inside the `## Identity` section. For repeatable employment records, the target is the Nth `### Title - Company (Start - End)` sub-heading block within `## Professional Experience`, matched against the same `re.match(r"(.+?)\s+-\s+(.+?)\s+\((.+?)\)$", ...)`-style pattern `_parse_candidate_markdown` already uses for that block — reuse the exact pattern, do not write a second, subtly-different one.

```python
"""Rewrite the canonical Evidence Profile Markdown source for a set of
structured edits, preserving every byte outside the edited fields.

Mirrors product/profile_snapshot.py's canonical-shape parsing patterns
exactly so what this module writes is provably re-parseable by the
unmodified parser -- never a second, subtly different format understanding.

Repeatable-field edits target ONE SPECIFIC RECORD BY POSITION. Positional
targeting must never key off a field's VALUE anywhere in this module --
two records may legitimately share a value (e.g. the same job title at two
different employers), and editing one must never accidentally match or
affect the other.
"""
from __future__ import annotations

from typing import Any

from product.profile_editing_schema import (
    REPEATABLE_FIELD_GROUPS,
    SINGLETON_FIELDS,
    parse_repeatable_field_id,
)
from product.profile_snapshot import MARKDOWN_FIELD_RE  # exact same regex, reused not reimplemented


class ProfileMarkdownRewriteError(ValueError):
    pass


def rewrite_canonical_markdown(current_markdown: str, edits: dict[str, Any]) -> str:
    singleton_edits: dict[str, Any] = {}
    repeatable_edits: dict[str, dict[int, dict[str, Any]]] = {}  # group_name -> index -> {sub_field: value}

    for field_id, value in edits.items():
        if field_id in SINGLETON_FIELDS:
            singleton_edits[field_id] = value
            continue
        parsed = parse_repeatable_field_id(field_id)
        if parsed is None:
            raise ProfileMarkdownRewriteError(f"unknown editable field id {field_id!r}")
        group_name, index, sub_field = parsed
        repeatable_edits.setdefault(group_name, {}).setdefault(index, {})[sub_field] = value

    lines = current_markdown.splitlines(keepends=True)

    for field_id, value in singleton_edits.items():
        spec = SINGLETON_FIELDS[field_id]
        lines = _rewrite_singleton_field(lines, spec, value, field_id)

    for group_name, by_index in repeatable_edits.items():
        record_count = _count_records_in_canonical_shape(lines, group_name)
        for index, sub_field_edits in by_index.items():
            if index >= record_count:
                raise ProfileMarkdownRewriteError(
                    f"record index {index} out of range for {group_name!r} "
                    f"({record_count} record(s) found in canonical shape)"
                )
            lines = _rewrite_repeatable_record_fields(lines, group_name, index, sub_field_edits)

    return "".join(lines)
```

Implement `_rewrite_singleton_field` (locate the `## <section>` heading, then the `MARKDOWN_FIELD_RE`-matching line for the target label within it; raise `ProfileMarkdownRewriteError` if the section is missing or no matching line is found — fail closed, never guess placement), `_count_records_in_canonical_shape` (count how many `### Title - Company (Dates)` sub-heading blocks exist within `## Professional Experience` for `"employment"`, or table rows within `## Education` for `"education"` — purely structural counting, never inspecting field values), and `_rewrite_repeatable_record_fields` (locate the Nth record block by counting sub-heading/table-row boundaries in document order — **never by matching a value** — then rewrite only the specific sub-field lines within that Nth block's line range, per the same per-sub-field patterns `_parse_candidate_markdown` uses). Cover at minimum: identity singleton fields, and employment records (sub-heading block form) — enough to pass this task's own tests. If a field type declared in Task 4's schema genuinely has no implementation yet, that field must not be reachable from the API layer in Task 10 until its rewrite logic exists here.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/webapp/services/test_profile_markdown_rewrite.py -v`
Expected: PASS (10 tests)

- [ ] **Step 7: Self-review against the Global Constraints before moving on**

Re-read this task's implementation against two constraints: (a) "Unmanaged Markdown content is preserved byte-for-byte or the edit fails closed" — confirm every code path either preserves untouched content verbatim or raises; (b) "Repeatable fields use canonical-order positional addressing... never 'does some record have this value'" — grep your own implementation for any comparison against a field's *value* used to locate which record to edit; there must be none. Record location logic must be purely structural (heading/table-row counting), never value-matching.

- [ ] **Step 8: Commit**

```bash
git add webapp/services/profile_markdown_rewrite.py tests/webapp/services/test_profile_markdown_rewrite.py
git commit -m "Add canonical-shape Markdown rewrite with positional record addressing"
```

---

## Task 6: `start_edit` — canonical Markdown parsed directly, never aggregated snapshot claims

**Files:**
- Create: `webapp/services/profile_editing.py`
- Test: `tests/webapp/services/test_profile_editing.py`

**Interfaces:**
- Consumes: `SOURCE_PATHS` from `product/profile_snapshot.py`; `profile_snapshot_content_id` from `product/job_fit.py`; `get_current_profile_snapshot` from `webapp/services/pipeline.py`; `SINGLETON_FIELDS`, `REPEATABLE_FIELD_GROUPS`, `field_id_for`, `PROFILE_EDITING_SCHEMA_VERSION` from Task 4.
- Produces: `compute_source_hashes(root: Path) -> dict[str, str]` (SHA-256 hex digest per `SOURCE_PATHS` entry); `start_edit(conn, *, root: Path) -> dict[str, Any]` returning `{"editing_schema_version", "session_id", "editable_fields": {...current values, keyed by field id including positional repeatable ids...}, "conflicts": [...], "precondition_source_hashes", "precondition_snapshot_content_id"}`.

**Critical correctness requirement (spec round-3 correction):** editable field *values* in the returned `editable_fields` dict must come from parsing the canonical Markdown file directly — never from `get_current_profile_snapshot`'s aggregated `claims` list, which mixes in `CLAUDE.md` and `cv/main_example.tex` (both read-only) and can include a conflict's losing-side value. The snapshot is consulted only for `conflicts` and `precondition_snapshot_content_id`.

- [ ] **Step 1: Write the failing tests**

```python
import hashlib
from pathlib import Path

import pytest

from webapp.services.profile_editing import compute_source_hashes, start_edit


def test_compute_source_hashes_returns_one_hash_per_source_path(tmp_path):
    from product.profile_snapshot import SOURCE_PATHS
    for relative in SOURCE_PATHS:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"content of {relative}", encoding="utf-8")
    hashes = compute_source_hashes(tmp_path)
    assert set(hashes.keys()) == set(SOURCE_PATHS)
    expected = hashlib.sha256(f"content of {SOURCE_PATHS[0]}".encode()).hexdigest()
    assert hashes[SOURCE_PATHS[0]] == expected


def test_start_edit_returns_current_editable_field_values_and_preconditions(tmp_path, seeded_conn):
    # `seeded_conn` fixture: build via webapp.persistence.migrations.apply_migrations
    # on a fresh DB, then seed a real profile by calling
    # webapp.services.profile_setup.import_profile_markdown against a canonical-shape
    # Markdown fixture written to tmp_path -- read tests/webapp/test_profile_setup.py
    # (or equivalent) first to reuse its existing fixture-building helpers rather
    # than reinventing them.
    result = start_edit(seeded_conn, root=tmp_path)
    assert result["editing_schema_version"]
    assert result["session_id"]
    assert "precondition_source_hashes" in result
    assert "precondition_snapshot_content_id" in result
    assert "editable_fields" in result
    assert "conflicts" in result


def test_start_edit_never_surfaces_a_claude_md_value_as_editable(tmp_path, seeded_conn):
    """The critical test for the spec's round-3 correction: seed a profile
    where CLAUDE.md and the canonical Markdown DISAGREE on a value that would
    create a recorded conflict, and prove start_edit's editable_fields never
    returns CLAUDE.md's value -- only the canonical Markdown's own value,
    even if that value is on the "losing" side of the conflict."""
    from product.profile_snapshot import SOURCE_PATHS

    canonical_relative = next(p for p in SOURCE_PATHS if "01-candidate-profile" in p)
    claude_md_relative = "CLAUDE.md"

    # Seed CLAUDE.md with one name, canonical Markdown with a DIFFERENT name,
    # both under the "Identity" section shape _parse_claude_markdown/
    # _parse_candidate_markdown expect -- exact fixture text confirmed against
    # product/profile_snapshot.py's real Identity-section parsing at
    # implementation time, not guessed here.
    (tmp_path / claude_md_relative).write_text(
        "# Candidate Profile\n\n## Identity\n- **Name:** Wrong Name From CLAUDE\n",
        encoding="utf-8",
    )
    (tmp_path / canonical_relative).write_text(
        "# Candidate Profile\n\n## Identity\n- **Name:** Correct Canonical Name\n",
        encoding="utf-8",
    )
    # ... build/refresh the snapshot against tmp_path via the real pipeline
    # so a genuine conflict is recorded (or, if identity.name concept_ids
    # don't naturally collide across sources without additional setup,
    # adjust the fixture to whichever field DOES produce a real conflict per
    # product/profile_snapshot.py's actual concept-matching rules -- verify
    # this by inspection before writing the final fixture, do not assume) ...

    result = start_edit(seeded_conn, root=tmp_path)
    assert result["editable_fields"]["identity.name"] == "Correct Canonical Name"
    assert "Wrong Name From CLAUDE" not in str(result["editable_fields"])
```

(The second test's exact fixture construction needs to be finalized against `product/profile_snapshot.py`'s real conflict-detection rules — read `_group_claims`/`validate_snapshot` first, per the same investigation Task 10 of the Lane B plan already modeled, before writing the final fixture text. The assertion shape — canonical value present, `CLAUDE.md` value absent, from `editable_fields` specifically — is the fixed requirement; the exact Markdown fixture achieving a real conflict is an implementation-time detail to verify, not guess.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""Evidence Profile in-app editing: start_edit -> preview_edit -> confirm_edit.

Exact-preview binding: confirm_edit takes only a changeset_id, never an
edits payload -- it writes exactly the bytes stored at preview time.

start_edit parses the canonical Markdown DIRECTLY -- never from aggregated
snapshot claims, which mix in the two read-only sources and can surface a
conflict's losing-side value. The snapshot is consulted only for conflict
annotations and content-id.

See docs/superpowers/specs/2026-08-22-ticket12b-evidence-profile-manager-design.md.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from product.job_fit import profile_snapshot_content_id
from product.profile_editing_schema import (
    PROFILE_EDITING_SCHEMA_VERSION,
    REPEATABLE_FIELD_GROUPS,
    SINGLETON_FIELDS,
    field_id_for,
)
from product.profile_snapshot import SOURCE_PATHS
from webapp.services.pipeline import get_current_profile_snapshot


def compute_source_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for relative in SOURCE_PATHS:
        content = (root / relative).read_bytes()
        hashes[relative] = hashlib.sha256(content).hexdigest()
    return hashes


def _canonical_markdown_path(root: Path) -> Path:
    relative = next(p for p in SOURCE_PATHS if "01-candidate-profile" in p)
    return root / relative


def compute_session_id(*, editing_schema_version: str, precondition_source_hashes: dict[str, str], precondition_snapshot_content_id: str | None) -> str:
    payload = "|".join([
        editing_schema_version,
        precondition_snapshot_content_id or "",
        *(f"{k}={v}" for k, v in sorted(precondition_source_hashes.items())),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def start_edit(conn: sqlite3.Connection, *, root: Path) -> dict[str, Any]:
    canonical_markdown = _canonical_markdown_path(root).read_text(encoding="utf-8")
    editable_fields = _extract_current_field_values_from_canonical_markdown(canonical_markdown)

    profile_artifact = get_current_profile_snapshot(conn)
    payload = profile_artifact["payload"] if profile_artifact else {}

    source_hashes = compute_source_hashes(root)
    content_id = profile_snapshot_content_id(payload) if payload else None
    return {
        "editing_schema_version": PROFILE_EDITING_SCHEMA_VERSION,
        "session_id": compute_session_id(
            editing_schema_version=PROFILE_EDITING_SCHEMA_VERSION,
            precondition_source_hashes=source_hashes,
            precondition_snapshot_content_id=content_id,
        ),
        "editable_fields": editable_fields,
        "conflicts": payload.get("conflicts", []),
        "precondition_source_hashes": source_hashes,
        "precondition_snapshot_content_id": content_id,
    }


def _extract_current_field_values_from_canonical_markdown(canonical_markdown: str) -> dict[str, Any]:
    """Parse the canonical Markdown directly for current editable values --
    reuses the same section/pattern recognition webapp.services.profile_markdown_rewrite
    uses for writing, applied here for reading. Never touches snapshot claims."""
    result: dict[str, Any] = {}
    for field_id, spec in SINGLETON_FIELDS.items():
        result[field_id] = _read_singleton_field(canonical_markdown, spec)
    for group_name, sub_specs in REPEATABLE_FIELD_GROUPS.items():
        records = _read_repeatable_records(canonical_markdown, group_name, sub_specs)
        for index, record_values in enumerate(records):
            for sub_field, value in record_values.items():
                result[field_id_for(group_name, index, sub_field)] = value
    return result
```

Implement `_read_singleton_field` and `_read_repeatable_records` reusing the exact same structural-location logic Task 5's `rewrite_canonical_markdown` uses to find fields (ideally factor the shared "locate this section/pattern" logic into one place both Task 5 and Task 6 import, rather than duplicating it — check at implementation time whether `profile_markdown_rewrite.py` already has reusable locator functions before writing a second copy in this module).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/services/profile_editing.py tests/webapp/services/test_profile_editing.py
git commit -m "Add start_edit: canonical-Markdown-direct field values, stateless session_id"
```

---

## Task 7: `preview_edit` — render, rebuild, reconcile specific records, diff, persist

**Files:**
- Modify: `webapp/services/profile_editing.py`
- Test: `tests/webapp/services/test_profile_editing.py`

**Interfaces:**
- Consumes: `rewrite_canonical_markdown`, `ProfileMarkdownRewriteError` from Task 5; `build_snapshot` from `product/profile_snapshot.py`; `insert_previewed_changeset` from Task 3; `compute_source_hashes` from Task 6; `parse_repeatable_field_id`, `SINGLETON_FIELDS`, `REPEATABLE_FIELD_GROUPS` from Task 4.
- Produces: `preview_edit(conn, *, root: Path, precondition_source_hashes: dict, precondition_snapshot_content_id: str | None, edits: dict[str, Any]) -> dict[str, Any]` returning `{"changeset_id", "diff": {...}, "prospective_snapshot_summary": {...}, "warnings": [...]}`. Raises `ProfileEditPreconditionError` if the precondition no longer matches current state.

- [ ] **Step 1: Write the failing tests**

```python
def test_preview_edit_persists_a_previewed_changeset_with_exact_bytes(tmp_path, seeded_conn):
    session = start_edit(seeded_conn, root=tmp_path)
    result = preview_edit(
        seeded_conn, root=tmp_path,
        precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    assert result["changeset_id"]
    from webapp.persistence.profile_edit_changesets import get_changeset
    row = get_changeset(seeded_conn, result["changeset_id"])
    assert row["status"] == "previewed"
    assert "Ada King" in row["new_markdown"]


def test_preview_edit_rejects_stale_precondition(tmp_path, seeded_conn):
    session = start_edit(seeded_conn, root=tmp_path)
    stale_hashes = dict(session["precondition_source_hashes"])
    stale_hashes[list(stale_hashes)[0]] = "deliberately-wrong-hash"
    with pytest.raises(ProfileEditPreconditionError):
        preview_edit(
            seeded_conn, root=tmp_path, precondition_source_hashes=stale_hashes,
            precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
            edits={"identity.name": "Ada King"},
        )


def test_preview_edit_two_calls_produce_two_independent_changesets(tmp_path, seeded_conn):
    session = start_edit(seeded_conn, root=tmp_path)
    first = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    second = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada Lovelace"},
    )
    assert first["changeset_id"] != second["changeset_id"]


def test_preview_edit_reconciliation_checks_the_specific_targeted_record(tmp_path, seeded_conn):
    """Uses a profile with two employment records sharing a duplicate
    job_title (mirroring Task 5's duplicate-value fixture). Edits only
    employment[1].job_title and confirms the reconciliation check validates
    the SECOND record's rebuilt claim, not merely "some employment record
    has this job_title" -- which would pass even for a broken rewriter that
    edited the wrong record, since the value would still exist somewhere in
    the rebuilt snapshot."""
    ...  # seeded_conn fixture built against a multi-employment canonical
         # Markdown fixture, mirroring Task 5's SAMPLE_MULTI_EMPLOYMENT


def test_preview_edit_reconciliation_failure_does_not_persist_a_changeset(tmp_path, seeded_conn):
    session = start_edit(seeded_conn, root=tmp_path)
    with pytest.raises(ProfileMarkdownRewriteError):
        preview_edit(
            seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
            precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
            edits={"employment[5].job_title": "X"},  # out-of-range record index
        )
    # no row was persisted for this attempt -- covered by no changeset_id being
    # returned/usable; confirm via a count query on profile_edit_changesets if
    # the test needs to be more explicit about "zero new rows"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v -k preview`
Expected: FAIL — `preview_edit` doesn't exist yet.

- [ ] **Step 3: Implement**

```python
import hashlib
import json
import uuid

from product.profile_snapshot import build_snapshot
from webapp.persistence.profile_edit_changesets import insert_previewed_changeset


class ProfileEditPreconditionError(ValueError):
    pass


def preview_edit(
    conn: sqlite3.Connection, *, root: Path,
    precondition_source_hashes: dict[str, str],
    precondition_snapshot_content_id: str | None,
    edits: dict[str, Any],
) -> dict[str, Any]:
    current_hashes = compute_source_hashes(root)
    if current_hashes != precondition_source_hashes:
        raise ProfileEditPreconditionError("source changed since start_edit")
    profile_artifact = get_current_profile_snapshot(conn)
    current_payload = profile_artifact["payload"] if profile_artifact else {}
    current_content_id = (
        profile_snapshot_content_id(current_payload) if current_payload else None
    )
    if current_content_id != precondition_snapshot_content_id:
        raise ProfileEditPreconditionError("snapshot changed since start_edit")

    canonical_path = _canonical_markdown_path(root)
    current_markdown = canonical_path.read_text(encoding="utf-8")
    new_markdown = rewrite_canonical_markdown(current_markdown, edits)  # raises on fail-closed cases

    prospective_payload = _build_prospective_snapshot(root, canonical_path, new_markdown)
    _reconcile_editing_schema_with_parser(edits, prospective_payload)  # raises on disagreement, per specific record

    diff = _compute_diff(current_payload, prospective_payload)
    changeset_id = f"pec_{uuid.uuid4().hex}"
    prospective_hash = hashlib.sha256(new_markdown.encode("utf-8")).hexdigest()
    insert_previewed_changeset(
        conn, changeset_id=changeset_id, editing_schema_version=PROFILE_EDITING_SCHEMA_VERSION,
        precondition_source_hashes=current_hashes,
        precondition_snapshot_content_id=current_content_id,
        prospective_source_hash=prospective_hash,
        diff_summary=diff, new_markdown=new_markdown, previous_markdown=current_markdown,
        created_at=_now(),
    )
    return {
        "changeset_id": changeset_id,
        "diff": diff,
        "prospective_snapshot_summary": _summarize(prospective_payload),
        "warnings": [],
    }
```

Implement `_build_prospective_snapshot` (write `new_markdown` to a temp copy of `root` at the canonical path, other two sources copied as-is, call `build_snapshot` against the temp dir — directly mirroring `profile_setup.py`'s existing `_validate_prospective_snapshot` pattern). Implement `_reconcile_editing_schema_with_parser` so that for a **repeatable** field edit (`employment[1].job_title`), it locates the *specific Nth record* in the rebuilt snapshot's claims (by the same positional/document-order logic, not value matching — the rebuilt snapshot's claims retain source line-number ordering, which can be used to recover record order deterministically; confirm this against `product/profile_snapshot.py`'s actual claim ordering behavior before implementing, do not assume) and checks that record's `(category, field)` claim has the edited value — never "does any employment claim anywhere have this value." Raise the same `ProfileMarkdownRewriteError` from Task 5 on mismatch (same failure class, reused not re-invented). Implement `_compute_diff` (added/changed/removed claims between `current_payload` and `prospective_payload`, plus conflicts newly introduced / no longer present / unchanged-and-still-conflicted), `_summarize`, and `_now` (reuse the pattern already established in `webapp/persistence/migrations.py` if a shared helper doesn't already exist elsewhere — check before adding a duplicate).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/services/profile_editing.py tests/webapp/services/test_profile_editing.py
git commit -m "Add preview_edit: render, reconcile specific record, diff, persist"
```

---

## Task 8: `confirm_edit` — atomic `commit_started` claim, then the commit sequence

**Files:**
- Modify: `webapp/services/profile_editing.py`
- Test: `tests/webapp/services/test_profile_editing.py`

**Interfaces:**
- Consumes: `get_changeset`, `try_claim_commit_started`, `mark_file_written`, `mark_committed` from Task 3; `refresh_profile` from `webapp/services/pipeline.py`; the existing `_atomic_write_bytes` from `webapp/services/profile_setup.py` (import and reuse directly).
- Produces: `confirm_edit(conn, *, root: Path, changeset_id: str) -> dict[str, Any]` returning `{"profile": <new snapshot artifact>, "warnings": [...]}`. Raises `ProfileEditConflictError` on any precondition mismatch **or on losing the `commit_started` claim race** — the API layer (Task 10) maps all of these to `409`.

**This task implements the spec's round-3-corrected confirm sequence exactly** (steps 1-10, with step 5 as the atomic claim): re-check source hashes (1) → re-check snapshot content-id (2) → re-check stored-bytes hash (3) → check schema version (4) → **atomically claim `commit_started`, `409` if the claim is lost (5)** → atomic file write (6) → mark `file_written` (7) → `refresh_profile` (8) → mark `committed` (9) → transient columns already cleared by `mark_committed` (10).

- [ ] **Step 1: Write the failing tests**

```python
def test_confirm_edit_writes_exactly_the_previewed_bytes(tmp_path, seeded_conn):
    session = start_edit(seeded_conn, root=tmp_path)
    preview = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    from webapp.persistence.profile_edit_changesets import get_changeset
    expected_bytes = get_changeset(seeded_conn, preview["changeset_id"])["new_markdown"]

    result = confirm_edit(seeded_conn, root=tmp_path, changeset_id=preview["changeset_id"])

    canonical_path = _canonical_markdown_path(tmp_path)
    assert canonical_path.read_text(encoding="utf-8") == expected_bytes
    assert result["profile"]["payload"]["claims"]
    assert get_changeset(seeded_conn, preview["changeset_id"])["status"] == "committed"


def test_confirm_edit_rejects_stale_source(tmp_path, seeded_conn):
    session = start_edit(seeded_conn, root=tmp_path)
    preview = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    canonical_path = _canonical_markdown_path(tmp_path)
    canonical_path.write_text(
        canonical_path.read_text(encoding="utf-8") + "\n<!-- external edit -->\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfileEditConflictError):
        confirm_edit(seeded_conn, root=tmp_path, changeset_id=preview["changeset_id"])
    assert "Ada King" not in canonical_path.read_text(encoding="utf-8")


def test_confirming_one_changeset_invalidates_a_sibling_preview(tmp_path, seeded_conn):
    session = start_edit(seeded_conn, root=tmp_path)
    first = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    second = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada Lovelace"},
    )
    confirm_edit(seeded_conn, root=tmp_path, changeset_id=first["changeset_id"])
    with pytest.raises(ProfileEditConflictError):
        confirm_edit(seeded_conn, root=tmp_path, changeset_id=second["changeset_id"])


def test_second_confirm_on_same_changeset_loses_the_commit_started_race(tmp_path, seeded_conn):
    """Directly exercises the atomic claim, independent of a stale-precondition
    scenario: confirm the SAME changeset id twice in a row (simulating two
    concurrent requests that both passed steps 1-4's checks before either
    reached step 5). The second call must fail via the claim, not proceed to
    write the file a second time."""
    session = start_edit(seeded_conn, root=tmp_path)
    preview = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    confirm_edit(seeded_conn, root=tmp_path, changeset_id=preview["changeset_id"])
    with pytest.raises(ProfileEditConflictError):
        confirm_edit(seeded_conn, root=tmp_path, changeset_id=preview["changeset_id"])


def test_confirm_edit_blocked_while_recovery_pending(tmp_path, seeded_conn):
    """A row stuck at commit_started or file_written (simulating an
    in-progress/crashed confirm elsewhere) must block a DIFFERENT changeset's
    confirm attempt -- this is the API-layer recovery-pending guard's
    underlying condition; test it here at the service layer since
    confirm_edit itself should check is_recovery_pending, not only the API
    router, per the Global Constraint that this is enforced at every route/
    call path, not just one entry point."""
    from webapp.persistence.profile_edit_changesets import try_claim_commit_started
    session = start_edit(seeded_conn, root=tmp_path)
    stuck = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Someone Stuck"},
    )
    try_claim_commit_started(seeded_conn, stuck["changeset_id"])  # simulate a stuck in-progress confirm

    another = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Someone Else"},
    )
    with pytest.raises(ProfileEditConflictError):
        confirm_edit(seeded_conn, root=tmp_path, changeset_id=another["changeset_id"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v -k confirm`
Expected: FAIL — `confirm_edit` doesn't exist yet.

- [ ] **Step 3: Implement**

```python
from webapp.persistence.profile_edit_changesets import (
    get_changeset, is_recovery_pending, mark_committed, mark_file_written,
    try_claim_commit_started,
)
from webapp.services.profile_setup import _atomic_write_bytes  # reuse, do not duplicate


class ProfileEditConflictError(ValueError):
    def __init__(self, reason: str, *, source_changed: bool = False, snapshot_changed: bool = False):
        self.reason = reason
        self.source_changed = source_changed
        self.snapshot_changed = snapshot_changed
        super().__init__(reason)


def confirm_edit(conn: sqlite3.Connection, *, root: Path, changeset_id: str) -> dict[str, Any]:
    if is_recovery_pending(conn):
        raise ProfileEditConflictError("recovery_pending")

    row = get_changeset(conn, changeset_id)
    if row is None:
        raise ProfileEditConflictError(f"unknown changeset {changeset_id!r}")
    if row["status"] != "previewed":
        raise ProfileEditConflictError(f"changeset {changeset_id!r} is not in a confirmable state (status={row['status']!r})")

    # Steps 1-4: precondition re-checks
    current_hashes = compute_source_hashes(root)
    precondition_hashes = json.loads(row["precondition_source_hashes"])
    if current_hashes != precondition_hashes:
        raise ProfileEditConflictError("source changed since preview", source_changed=True)

    profile_artifact = get_current_profile_snapshot(conn)
    current_payload = profile_artifact["payload"] if profile_artifact else {}
    current_content_id = profile_snapshot_content_id(current_payload) if current_payload else None
    if current_content_id != row["precondition_snapshot_content_id"]:
        raise ProfileEditConflictError("snapshot changed since preview", snapshot_changed=True)

    recomputed_prospective_hash = hashlib.sha256(row["new_markdown"].encode("utf-8")).hexdigest()
    if recomputed_prospective_hash != row["prospective_source_hash"]:
        raise ProfileEditConflictError("stored changeset bytes do not match their own recorded hash (row corruption)")

    # (schema-version compatibility check -- implementation-plan detail, see spec step 4)

    # Step 5: THE ATOMIC CLAIM -- this is the confirm-race serialization point.
    if not try_claim_commit_started(conn, changeset_id):
        raise ProfileEditConflictError("a sibling changeset already committed or is committing")

    # Steps 6-9: only the caller that won step 5 ever reaches here for this changeset.
    canonical_path = _canonical_markdown_path(root)
    _atomic_write_bytes(canonical_path, row["new_markdown"].encode("utf-8"))
    mark_file_written(conn, changeset_id)

    artifact = refresh_profile(conn, root=str(root))

    mark_committed(conn, changeset_id, committed_at=_now())  # step 10's clearing happens inside mark_committed
    return {"profile": artifact, "warnings": []}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/services/profile_editing.py tests/webapp/services/test_profile_editing.py
git commit -m "Add confirm_edit: atomic commit_started claim closes the crash-window gap"
```

---

## Task 9: Recovery over both pre-terminal states, plus expiry sweep

**Files:**
- Modify: `webapp/services/profile_editing.py`
- Test: `tests/webapp/services/test_profile_editing.py`

**Interfaces:**
- Consumes: `list_pre_terminal_changesets`, `list_expirable_changesets`, `mark_committed`, `mark_rolled_back`, `mark_recovery_conflict`, `mark_expired` from Task 3.
- Produces: `recover_interrupted_changesets(conn, *, root: Path) -> dict[str, Any]` returning `{"resumed": [...], "rolled_back": [...], "recovery_conflicts": [...]}`; `expire_stale_changesets(conn, *, cutoff_created_at: str) -> list[str]` returning the ids expired.

**This task scans BOTH `commit_started` and `file_written`** (per Task 3's `list_pre_terminal_changesets`, replacing the round-1 plan's `file_written`-only scan) — this is the direct fix for the crash-window gap the spec's round-3 review found.

- [ ] **Step 1: Write the failing tests — recovery, all three file-content cases, for a row found at EITHER pre-terminal status**

```python
def test_recovery_resumes_a_row_stuck_at_commit_started_when_file_already_written(tmp_path, seeded_conn):
    """The specific scenario the spec's round-3 correction exists for: a crash
    right after the commit_started claim succeeds but the file write itself
    still completed (e.g. crash between os.replace() returning and the
    subsequent mark_file_written() call) -- the row is at commit_started, NOT
    file_written, but the file content already matches new_markdown."""
    session = start_edit(seeded_conn, root=tmp_path)
    preview = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    from webapp.persistence.profile_edit_changesets import get_changeset, try_claim_commit_started
    row = get_changeset(seeded_conn, preview["changeset_id"])
    try_claim_commit_started(seeded_conn, preview["changeset_id"])  # simulate: claimed...
    canonical_path = _canonical_markdown_path(tmp_path)
    canonical_path.write_text(row["new_markdown"], encoding="utf-8")  # ...write completed...
    # ...but crash before mark_file_written() ever ran -- row is STILL at commit_started

    summary = recover_interrupted_changesets(seeded_conn, root=tmp_path)

    assert preview["changeset_id"] in summary["resumed"]
    assert get_changeset(seeded_conn, preview["changeset_id"])["status"] == "committed"


def test_recovery_resumes_a_row_stuck_at_file_written(tmp_path, seeded_conn):
    session = start_edit(seeded_conn, root=tmp_path)
    preview = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    from webapp.persistence.profile_edit_changesets import get_changeset, mark_file_written, try_claim_commit_started
    row = get_changeset(seeded_conn, preview["changeset_id"])
    try_claim_commit_started(seeded_conn, preview["changeset_id"])
    canonical_path = _canonical_markdown_path(tmp_path)
    canonical_path.write_text(row["new_markdown"], encoding="utf-8")
    mark_file_written(seeded_conn, preview["changeset_id"])  # crash before refresh_profile

    summary = recover_interrupted_changesets(seeded_conn, root=tmp_path)

    assert preview["changeset_id"] in summary["resumed"]
    assert get_changeset(seeded_conn, preview["changeset_id"])["status"] == "committed"


def test_recovery_rolls_back_a_row_at_commit_started_whose_write_never_happened(tmp_path, seeded_conn):
    """A crash immediately after the commit_started claim, before the file
    write was even attempted -- the file still matches previous_markdown."""
    session = start_edit(seeded_conn, root=tmp_path)
    preview = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    from webapp.persistence.profile_edit_changesets import get_changeset, try_claim_commit_started
    try_claim_commit_started(seeded_conn, preview["changeset_id"])
    # canonical file untouched -- still equals previous_markdown

    summary = recover_interrupted_changesets(seeded_conn, root=tmp_path)

    assert preview["changeset_id"] in summary["rolled_back"]
    row = get_changeset(seeded_conn, preview["changeset_id"])
    assert row["status"] == "rolled_back"
    assert row["new_markdown"] is None and row["previous_markdown"] is None


def test_recovery_case_file_matches_neither_fails_closed_and_preserves_bytes(tmp_path, seeded_conn):
    session = start_edit(seeded_conn, root=tmp_path)
    preview = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    from webapp.persistence.profile_edit_changesets import get_changeset, mark_file_written, try_claim_commit_started
    try_claim_commit_started(seeded_conn, preview["changeset_id"])
    mark_file_written(seeded_conn, preview["changeset_id"])
    canonical_path = _canonical_markdown_path(tmp_path)
    external_content = "# something completely different, changed by an external process\n"
    canonical_path.write_text(external_content, encoding="utf-8")

    summary = recover_interrupted_changesets(seeded_conn, root=tmp_path)

    assert preview["changeset_id"] in summary["recovery_conflicts"]
    row = get_changeset(seeded_conn, preview["changeset_id"])
    assert row["status"] == "recovery_conflict"
    assert row["new_markdown"] is not None
    assert row["previous_markdown"] is not None
    assert canonical_path.read_text(encoding="utf-8") == external_content
```

- [ ] **Step 2: Write the failing tests — expiry sweep**

```python
def test_expire_stale_changesets_expires_old_abandoned_previewed_rows(tmp_path, seeded_conn):
    from webapp.persistence.profile_edit_changesets import get_changeset, insert_previewed_changeset
    insert_previewed_changeset(
        seeded_conn, changeset_id="pec_old", editing_schema_version="v0",
        precondition_source_hashes={"x": "y"}, precondition_snapshot_content_id="z",
        prospective_source_hash="h", diff_summary={}, new_markdown="# a", previous_markdown="# b",
        created_at="2020-01-01T00:00:00Z",
    )
    expired_ids = expire_stale_changesets(seeded_conn, cutoff_created_at="2026-01-01T00:00:00Z")
    assert "pec_old" in expired_ids
    row = get_changeset(seeded_conn, "pec_old")
    assert row["status"] == "expired"
    assert row["new_markdown"] is None and row["previous_markdown"] is None


def test_expire_stale_changesets_does_not_touch_recent_or_non_previewed_rows(tmp_path, seeded_conn):
    session = start_edit(seeded_conn, root=tmp_path)
    fresh = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    committed = preview_edit(
        seeded_conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada Lovelace"},
    )
    confirm_edit(seeded_conn, root=tmp_path, changeset_id=committed["changeset_id"])

    expired_ids = expire_stale_changesets(seeded_conn, cutoff_created_at="2020-01-01T00:00:00Z")

    assert fresh["changeset_id"] not in expired_ids  # too recent
    assert committed["changeset_id"] not in expired_ids  # not previewed anymore
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v -k "recover or expire"`
Expected: FAIL — neither function exists yet.

- [ ] **Step 4: Implement**

```python
def recover_interrupted_changesets(conn: sqlite3.Connection, *, root: Path) -> dict[str, list[str]]:
    summary: dict[str, list[str]] = {"resumed": [], "rolled_back": [], "recovery_conflicts": []}
    canonical_path = _canonical_markdown_path(root)
    current_content = canonical_path.read_text(encoding="utf-8") if canonical_path.exists() else None

    for row in list_pre_terminal_changesets(conn):  # scans BOTH commit_started AND file_written
        changeset_id = row["id"]
        if current_content == row["new_markdown"]:
            refresh_profile(conn, root=str(root))
            mark_committed(conn, changeset_id, committed_at=_now())
            summary["resumed"].append(changeset_id)
        elif current_content == row["previous_markdown"]:
            mark_rolled_back(conn, changeset_id)
            summary["rolled_back"].append(changeset_id)
        else:
            # File matches neither stored copy: something external changed it.
            # Do NOT overwrite anything, do not guess. Fail closed.
            mark_recovery_conflict(conn, changeset_id)
            summary["recovery_conflicts"].append(changeset_id)
    return summary


def expire_stale_changesets(conn: sqlite3.Connection, *, cutoff_created_at: str) -> list[str]:
    expired_ids = []
    for row in list_expirable_changesets(conn, cutoff_created_at=cutoff_created_at):
        mark_expired(conn, row["id"])
        expired_ids.append(row["id"])
    return expired_ids
```

Note `recover_interrupted_changesets`'s branching logic is UNCHANGED from the round-1 plan's version except for which rows it iterates over (`list_pre_terminal_changesets` instead of `list_file_written_changesets`) — the three-way file-content comparison itself is identical for a row found at either `commit_started` or `file_written`, since the fix is entirely about *which rows get scanned*, not the comparison logic.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v`
Expected: PASS, full file green.

- [ ] **Step 6: Commit**

```bash
git add webapp/services/profile_editing.py tests/webapp/services/test_profile_editing.py
git commit -m "Recover over commit_started and file_written; add expiry sweep"
```

---

## Task 10: API surface — locked `{session_id}`, `409` `recovery_pending` guard on every route, `GET /recovery-status`

**Files:**
- Create: `webapp/api/profile_editing.py`
- Modify: `webapp/app.py` (register router; call `recover_interrupted_changesets` and `expire_stale_changesets` at startup)
- Test: `tests/webapp/api/test_profile_editing_routes.py`

**Interfaces:**
- Consumes: `start_edit`, `preview_edit`, `confirm_edit`, `ProfileEditPreconditionError`, `ProfileEditConflictError` from Tasks 6-9; `is_recovery_pending` from Task 3; `get_conn` from `webapp/api/dependencies.py`.
- Produces: `POST /api/profile/edit-sessions`, `POST /api/profile/edit-sessions/{session_id}/preview`, `POST /api/profile/edit-changesets/{changeset_id}/confirm`, `GET /api/profile/edit-changesets/{changeset_id}`, `GET /api/profile/recovery-status`. **`{session_id}` is kept per the spec's lock** — the path segment exists, but the server never persists a session row; `session_id` is the same stateless hash `start_edit` (Task 6) already computed and returned, and every route re-validates the actual precondition from the request body/row regardless of what `session_id` was supplied in the URL (the path segment is client-side bookkeeping only, not an authority the server trusts).

- [ ] **Step 1: Read `webapp/app.py`'s router-registration and startup patterns before editing**

Read the file to find how `webapp/api/profile.py` and `webapp/api/search_workspaces.py` are currently registered, and where `apply_migrations` (and any other startup initialization) currently runs. `recover_interrupted_changesets` and `expire_stale_changesets` must run at that same startup point, in that order (recover before expire — an expirable row can only be `previewed`, which recovery never touches, so ordering between them is not actually load-bearing, but running recovery first keeps the startup sequence's intent legible: resolve anything mid-flight, then clean up anything abandoned).

- [ ] **Step 2: Write the failing test**

```python
from fastapi.testclient import TestClient


def test_start_edit_returns_200_with_session_id(client):  # `client` fixture: existing TestClient pattern from tests/webapp/api/*
    response = client.post("/api/profile/edit-sessions")
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"]
    assert "editing_schema_version" in body
    assert "precondition_source_hashes" in body


def test_preview_then_confirm_happy_path(client):
    session = client.post("/api/profile/edit-sessions").json()
    preview = client.post(
        f"/api/profile/edit-sessions/{session['session_id']}/preview",
        json={
            "precondition_source_hashes": session["precondition_source_hashes"],
            "precondition_snapshot_content_id": session["precondition_snapshot_content_id"],
            "edits": {"identity.name": "Ada King"},
        },
    )
    assert preview.status_code == 200
    changeset_id = preview.json()["changeset_id"]
    confirm = client.post(f"/api/profile/edit-changesets/{changeset_id}/confirm")
    assert confirm.status_code == 200


def test_confirm_with_stale_precondition_returns_409_without_source_content(client):
    session = client.post("/api/profile/edit-sessions").json()
    preview = client.post(
        f"/api/profile/edit-sessions/{session['session_id']}/preview",
        json={
            "precondition_source_hashes": session["precondition_source_hashes"],
            "precondition_snapshot_content_id": session["precondition_snapshot_content_id"],
            "edits": {"identity.name": "Ada King"},
        },
    ).json()
    client.post(f"/api/profile/edit-changesets/{preview['changeset_id']}/confirm")
    second_confirm = client.post(f"/api/profile/edit-changesets/{preview['changeset_id']}/confirm")
    assert second_confirm.status_code == 409
    body = second_confirm.json()
    assert len(str(body)) < 2000  # conflict responses are small, identity-only -- not full source dumps


def test_get_changeset_status_never_returns_source_text(client):
    session = client.post("/api/profile/edit-sessions").json()
    preview = client.post(
        f"/api/profile/edit-sessions/{session['session_id']}/preview",
        json={
            "precondition_source_hashes": session["precondition_source_hashes"],
            "precondition_snapshot_content_id": session["precondition_snapshot_content_id"],
            "edits": {"identity.name": "Ada King"},
        },
    ).json()
    status = client.get(f"/api/profile/edit-changesets/{preview['changeset_id']}")
    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "previewed"
    assert "new_markdown" not in body
    assert "previous_markdown" not in body


def test_recovery_status_reports_clean_state(client):
    response = client.get("/api/profile/recovery-status")
    assert response.status_code == 200
    assert response.json() == {"blocked": False, "reason": None}


def test_recovery_status_reports_blocked_when_pending(client, seeded_conn):
    from webapp.persistence.profile_edit_changesets import insert_previewed_changeset, try_claim_commit_started
    insert_previewed_changeset(
        seeded_conn, changeset_id="pec_stuck", editing_schema_version="v0",
        precondition_source_hashes={"x": "y"}, precondition_snapshot_content_id="z",
        prospective_source_hash="h", diff_summary={}, new_markdown="# a", previous_markdown="# b",
        created_at="2026-01-01T00:00:00Z",
    )
    try_claim_commit_started(seeded_conn, "pec_stuck")
    response = client.get("/api/profile/recovery-status")
    assert response.json() == {"blocked": True, "reason": "recovery_conflict"}
    # (exact reason string for commit_started/file_written vs. recovery_conflict
    # specifically is an implementation-time wording choice within the
    # {"blocked": bool, "reason": str|None} shape the spec locks -- confirm
    # against the spec's exact GET /api/profile/recovery-status description
    # before finalizing which reason strings this endpoint returns.)


def test_start_edit_blocked_while_recovery_pending(client, seeded_conn):
    from webapp.persistence.profile_edit_changesets import insert_previewed_changeset, try_claim_commit_started
    insert_previewed_changeset(
        seeded_conn, changeset_id="pec_stuck", editing_schema_version="v0",
        precondition_source_hashes={"x": "y"}, precondition_snapshot_content_id="z",
        prospective_source_hash="h", diff_summary={}, new_markdown="# a", previous_markdown="# b",
        created_at="2026-01-01T00:00:00Z",
    )
    try_claim_commit_started(seeded_conn, "pec_stuck")
    response = client.post("/api/profile/edit-sessions")
    assert response.status_code == 409
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/webapp/api/test_profile_editing_routes.py -v`
Expected: FAIL — router not registered / module doesn't exist.

- [ ] **Step 4: Implement the router**

```python
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from webapp.api.dependencies import get_conn
from webapp.persistence.profile_edit_changesets import get_changeset, is_recovery_pending
from webapp.services.profile_editing import (
    ProfileEditConflictError,
    ProfileEditPreconditionError,
    confirm_edit,
    preview_edit,
    start_edit,
)
from webapp.services.profile_markdown_rewrite import ProfileMarkdownRewriteError

router = APIRouter(prefix="/api/profile", tags=["profile-editing"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreviewEditBody(StrictBody):
    precondition_source_hashes: dict[str, str]
    precondition_snapshot_content_id: str | None
    edits: dict[str, object]


def _guard_recovery_pending(conn: sqlite3.Connection) -> None:
    if is_recovery_pending(conn):
        raise HTTPException(
            status_code=409,
            detail={"reason": "recovery_pending", "message": "An Evidence Profile update is unresolved; edits are blocked until it is resolved."},
        )


@router.post("/edit-sessions")
def post_edit_session(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _guard_recovery_pending(conn)
    return start_edit(conn, root=request.app.state.settings.profile_root)


@router.post("/edit-sessions/{session_id}/preview")
def post_preview_edit(
    session_id: str, body: PreviewEditBody, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    _guard_recovery_pending(conn)
    # session_id is not looked up server-side (no session table) -- the actual
    # precondition in `body` is what preview_edit validates; session_id is
    # client-side bookkeeping only, per the spec's stateless-session lock.
    try:
        return preview_edit(
            conn, root=request.app.state.settings.profile_root,
            precondition_source_hashes=body.precondition_source_hashes,
            precondition_snapshot_content_id=body.precondition_snapshot_content_id,
            edits=body.edits,
        )
    except ProfileEditPreconditionError as exc:
        raise HTTPException(status_code=409, detail={"reason": "source_changed_or_snapshot_changed", "message": str(exc)}) from exc
    except ProfileMarkdownRewriteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/edit-changesets/{changeset_id}/confirm")
def post_confirm_edit(
    changeset_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
):
    _guard_recovery_pending(conn)
    try:
        return confirm_edit(conn, root=request.app.state.settings.profile_root, changeset_id=changeset_id)
    except ProfileEditConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "source_changed" if exc.source_changed else "snapshot_changed" if exc.snapshot_changed else exc.reason,
                "source_changed": exc.source_changed,
                "snapshot_changed": exc.snapshot_changed,
                "message": str(exc),
            },
        ) from exc


@router.get("/edit-changesets/{changeset_id}")
def get_edit_changeset(changeset_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    row = get_changeset(conn, changeset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="changeset not found")
    row.pop("new_markdown", None)
    row.pop("previous_markdown", None)
    return row


@router.get("/recovery-status")
def get_recovery_status(conn: sqlite3.Connection = Depends(get_conn)):
    blocked = is_recovery_pending(conn)
    return {"blocked": blocked, "reason": "recovery_conflict" if blocked else None}
```

(The `reason` value `get_recovery_status` returns when blocked — always `"recovery_conflict"` even for a `commit_started`/`file_written` row that hasn't reached a genuine conflict — is deliberate: those two states resolve automatically at startup before the API ever accepts traffic, so in practice a client can only ever observe this endpoint returning `blocked: True` for a genuine `recovery_conflict`. If a future change makes `commit_started`/`file_written` externally observable, this reason string should be revisited — noted here so that possibility isn't silently lost.)

The `GET` handler explicitly strips `new_markdown`/`previous_markdown` from the response even while a row still holds them (`file_written`/`recovery_conflict` states), per the Global Constraint.

Register the router in `webapp/app.py` and add startup calls to `recover_interrupted_changesets` then `expire_stale_changesets`, immediately after wherever `apply_migrations` currently runs.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/webapp/api/test_profile_editing_routes.py -v`
Expected: PASS

- [ ] **Step 6: Run the full webapp API test suite for regressions**

Run: `python -m pytest tests/webapp/ -v`
Expected: PASS, no regressions.

- [ ] **Step 7: Commit**

```bash
git add webapp/api/profile_editing.py webapp/app.py tests/webapp/api/test_profile_editing_routes.py
git commit -m "Add Evidence Profile editing API routes with recovery-pending guard"
```

---

## Task 11: UI — Edit and Preview states, recovery banner, automated browser coverage

**Files:**
- Modify: `webapp/templates/profile.html`
- Modify: `webapp/static/app.js`
- Modify: `tests/webapp/test_browser_smoke.py` — **automated coverage, not manual-only**, per PM review: this ticket introduces a real review gate in the UI, and the repository already has Playwright infrastructure.

**Interfaces:**
- Consumes: the five API routes from Task 10.
- Produces: an "Edit Evidence Profile" action in View mode; an Edit form rendering current values grouped by `SINGLETON_FIELDS`/`REPEATABLE_FIELD_GROUPS` sections; a Preview screen; a recovery banner driven by `GET /api/profile/recovery-status`.

- [ ] **Step 1: Update the two lines of copy that currently claim editing doesn't happen here**

In `webapp/templates/profile.html`:
- The hero-section paragraph currently claiming "Update source profile files outside this interface, then refresh the snapshot" — update to reflect in-app editing is now possible (exact wording is a copy decision; keep it accurate and brief).
- The "Evidence claims" panel's "No editing or conflict resolution occurs here" line — reword to something like "Edit Evidence Profile to make changes; conflicts are resolved by editing source material, never by choosing a value here," preserving the underlying claim (no inline resolve-conflict UI) since it remains a Global Constraint.

- [ ] **Step 2: Add the "Edit Evidence Profile" entry point and Edit form**

Add a button near the existing `data-action="refresh-profile"` button that triggers Edit mode. Render the Edit form grouped by section: Identity, Employment, Education, Skills/tools, Certifications, Languages, Publications, Awards, driven by `SINGLETON_FIELDS`' `section` grouping plus `REPEATABLE_FIELD_GROUPS` (rendering one sub-form per existing record, using the positional field ids `start_edit` returned — e.g. `employment[0].job_title`, `employment[1].job_title` as separate form fields, one block per record, never a single shared field for "job title"). Each field shows its source relationship and, where the field's `(category, field)` matches a current conflict's, the existing conflict annotation — no "Accept winner" / "Resolve conflict" / "Mark verified" control anywhere.

Unmanaged content gets a small static notice, not an editable control.

No "Save" button — only "Preview changes", which POSTs to `/api/profile/edit-sessions/{session_id}/preview` (the locked route shape from Task 10) and transitions to the Preview screen.

- [ ] **Step 3: Add the Preview screen**

Dedicated state, showing: added/changed/removed claims, conflicts newly introduced / no longer present / unchanged-and-still-conflicted, warnings, affected source file(s), prospective snapshot summary. The confirm button's label is exactly: **"Apply exactly these reviewed changes to the Evidence Profile."** — POSTs to `/api/profile/edit-changesets/{changeset_id}/confirm`.

On a `409` response from confirm, show exactly: "Your Evidence Profile changed after this edit began. Nothing was written. Reload the latest profile and review your changes again." — and return to View mode.

- [ ] **Step 4: Add the recovery banner**

On page load (and after any failed edit action), call `GET /api/profile/recovery-status`. If `blocked: true`, show a blocking banner: "An Evidence Profile update was interrupted and the source changed unexpectedly. No automatic recovery was attempted." — and disable the "Edit Evidence Profile" action while this banner is showing. This is now a fully-specified endpoint (Task 10), not an open question — implement against it directly.

- [ ] **Step 5: Write automated browser coverage**

Read `tests/webapp/test_browser_smoke.py`'s existing conventions (fixture setup, how it drives the app, its assertion style) before writing new tests, and match them exactly — do not invent a parallel browser-test pattern.

```python
def test_evidence_profile_view_edit_preview_confirm_happy_path(page, live_server_url):
    # Navigate to the profile page (URL pattern per the file's existing convention)
    page.goto(f"{live_server_url}/profile")
    page.click('[data-action="edit-profile"]')  # or whatever data-action the Edit button uses per Step 2
    page.fill('[data-field-id="identity.name"]', "Ada King")
    page.click('button:has-text("Preview changes")')
    page.wait_for_selector('text=Apply exactly these reviewed changes to the Evidence Profile.')
    page.click('button:has-text("Apply exactly these reviewed changes to the Evidence Profile.")')
    page.wait_for_selector('text=Ada King')  # confirmed value now visible in View mode


def test_evidence_profile_confirm_conflict_shows_the_exact_message_and_writes_nothing(page, live_server_url):
    page.goto(f"{live_server_url}/profile")
    page.click('[data-action="edit-profile"]')
    page.fill('[data-field-id="identity.name"]', "Ada King")
    page.click('button:has-text("Preview changes")')
    # simulate an external change between preview and confirm -- via a second
    # browser context/tab completing its own edit first, or a direct file
    # write + snapshot refresh outside the page under test; exact mechanism
    # confirmed against whatever existing test_browser_smoke.py pattern
    # already handles cross-request state mutation, if any -- otherwise use
    # a second confirm on an already-committed changeset as the simplest
    # reliable trigger, mirroring the API-level test in Task 10.
    page.click('button:has-text("Apply exactly these reviewed changes to the Evidence Profile.")')
    page.wait_for_selector('text=Your Evidence Profile changed after this edit began. Nothing was written.')


def test_evidence_profile_recovery_banner_blocks_editing(page, live_server_url, seeded_conn):
    from webapp.persistence.profile_edit_changesets import insert_previewed_changeset, try_claim_commit_started
    insert_previewed_changeset(
        seeded_conn, changeset_id="pec_stuck", editing_schema_version="v0",
        precondition_source_hashes={"x": "y"}, precondition_snapshot_content_id="z",
        prospective_source_hash="h", diff_summary={}, new_markdown="# a", previous_markdown="# b",
        created_at="2026-01-01T00:00:00Z",
    )
    try_claim_commit_started(seeded_conn, "pec_stuck")
    # (Recovery only runs at app STARTUP, so seeding this after the live
    # server already started won't trigger automatic resolution -- this is
    # exactly the scenario the banner exists for: a genuinely stuck row.)

    page.goto(f"{live_server_url}/profile")
    page.wait_for_selector('text=An Evidence Profile update was interrupted and the source changed unexpectedly.')
    assert page.is_disabled('[data-action="edit-profile"]')
```

(Reconcile fixture names/mechanics — `page`, `live_server_url`, `seeded_conn` or their real equivalents — against `test_browser_smoke.py`'s actual existing fixtures at implementation time; the names here are illustrative of the required coverage, not necessarily the exact real fixture names in this codebase.)

- [ ] **Step 6: Run the new browser tests**

Run: `python -m pytest tests/webapp/test_browser_smoke.py -v -k evidence_profile`
Expected: PASS (allowing for the same pre-existing, unrelated Playwright missing-browser-binary environment gap documented in every prior plan in this repo if this environment lacks the browser binary — confirm this is the same known gap, not a new failure, before treating it as expected).

- [ ] **Step 7: Manual verification of anything the automated tests don't cover**

If any interaction (e.g. visual layout of the multi-record Employment sub-forms) isn't practically covered by the automated tests above, do a quick manual pass in a running dev server and note what was checked.

- [ ] **Step 8: Commit**

```bash
git add webapp/templates/profile.html webapp/static/app.js tests/webapp/test_browser_smoke.py
git commit -m "Add Evidence Profile edit/preview UI, recovery banner, browser coverage"
```

---

## Task 12: Full regression pass and spec acceptance-criteria check

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -q`
Expected: PASS (allowing for the same pre-existing, unrelated Playwright missing-browser-binary errors already documented in Lane B's and Ticket 12A's final verification, PLUS whatever this plan's own Task 11 browser tests contribute if the environment has real Chrome — confirm the failure set is exactly the known pre-existing one, not expanded by anything in this plan).

- [ ] **Step 2: Walk the spec's acceptance criteria explicitly, including the round-3 additions**

For each bullet in the spec's "Acceptance criteria" section — including the five criteria added in the round-3 amendment (commit_started crash window, canonical-source-only field values, positional record targeting with duplicate values, no unbounded changeset accumulation, concurrent-confirm race) — point to the specific test(s) from Tasks 1-11 that cover it. If any criterion has no corresponding test, add one now before calling this task done.

- [ ] **Step 3: Confirm no scope leak**

Run: `git diff master --stat` (from this branch) and confirm no file under a path owned by Ticket 7 (`product/semantic_job_fit.py`, `product/job_understanding*.py`), Lane B (`product/application_intelligence*.py`, `product/openai_application_intelligence_provider.py`), Gate 4, or Search Workspaces (`webapp/persistence/search_workspaces.py`, `webapp/api/search_workspaces.py`) appears in the diff.

- [ ] **Step 4: Confirm `product/profile_snapshot.py` is byte-identical to `master`**

Run: `git diff master -- product/profile_snapshot.py`
Expected: empty output.

- [ ] **Step 5: Confirm no unbounded row accumulation is possible even without the expiry sweep ever running**

Re-read Task 9's `expire_stale_changesets` and confirm it is actually invoked at startup (Task 10, Step 4) — an implemented-but-never-called sweep would silently reintroduce the exact shadow-history risk the spec's round-3 correction closed. This is worth an explicit re-check here since it spans two tasks (9 and 10) and is easy to lose track of.

---

## Self-Review Notes

**Spec coverage (updated for the round-3 amendment):**
- "Decision area 1" (unmanaged-content preservation, canonical-Markdown-direct `start_edit`, positional record addressing, editing-schema/parser reconciliation per specific record) → Tasks 4, 5, 6, 7.
- "Decision area 2" (file-content-hash concurrency) → Task 6 (`compute_source_hashes`), enforced at Tasks 7 and 8.
- "Decision area 3" (exact-preview binding, `commit_started` atomic claim, seven-state durable change-set, recovery over both pre-terminal states, transient storage, bounded expiry) → Tasks 2, 3, 7, 8, 9.
- "API surface" (locked `session_id`, recovery-pending guard on every route, `GET /recovery-status`) → Task 10.
- "UI structure" / "Preview state" / "Recovery UX" → Task 11, now with automated browser coverage.
- "Lifecycle rule" (one confirmable changeset per precondition) → verified as a structural consequence in Task 8's `test_confirming_one_changeset_invalidates_a_sibling_preview`, plus the additional `test_second_confirm_on_same_changeset_loses_the_commit_started_race` for the concurrent-race variant specifically.
- Migration-system gap discovered during planning (not in the spec, a real pre-existing constraint) → Task 1, resolved before Task 2 needs it.

**Placeholder scan:** Task 4's exact field-id enumeration (Step 1's real-vocabulary command) and Task 10's `reason` string for `commit_started`/`file_written` recovery states (noted as revisit-if-ever-externally-observable) are both explicitly flagged, low-risk, implementation-time decisions with a stated resolution path — not silent gaps. Task 6's second test (`test_start_edit_never_surfaces_a_claude_md_value_as_editable`) and Task 7's record-order-recovery note both explicitly say "confirm against real parser behavior before finalizing" rather than asserting an unverified claim as fact — this is intentional: those two spots depend on `product/profile_snapshot.py`'s exact conflict-detection and claim-ordering behavior, which must be verified against the real file at implementation time (as Task 10 of the Lane B plan modeled), not guessed here.

**Type consistency check:** `ProfileEditPreconditionError` (Task 7, preview-time) and `ProfileEditConflictError` (Task 8, confirm-time — now also covering the lost-claim-race case) remain distinct exception types mapped to different situations. `ProfileMarkdownRewriteError` (Task 5) is reused for Task 7's reconciliation failures — same failure class. The round-1 plan's flat `EDITABLE_FIELDS: dict[str, EditableFieldSpec]` shape is fully replaced by Task 4's `SINGLETON_FIELDS` + `REPEATABLE_FIELD_GROUPS` + `field_id_for`/`parse_repeatable_field_id` shape everywhere it's consumed (Tasks 5, 6, 7, 11) — verified no task still references the old flat shape or a bare `repeatable: bool` check without going through the positional-addressing functions. `list_file_written_changesets` (round-1 Task 3) is fully replaced by `list_pre_terminal_changesets` (this plan's Task 3) everywhere it's consumed (Task 9) — verified no task still calls the old, narrower-scoped function name.

**Round-3 amendment traceability:** every one of the six PM findings has a direct task-level fix: (1) `commit_started` crash window → Tasks 2/3/8/9's atomic-claim-and-dual-scan machinery; (2) canonical-Markdown-direct `start_edit` → Task 6, with a dedicated negative test proving `CLAUDE.md`/conflict values never leak into `editable_fields`; (3) positional record addressing → Task 4's schema shape plus Task 5/7's specific-record targeting and duplicate-value tests; (4) expiry mechanism → Task 3's `list_expirable_changesets`/`mark_expired` plus Task 9's sweep plus Task 10's startup wiring plus Task 12 Step 5's explicit "is it actually called" re-check; (5) locked `{session_id}` and explicit recovery-pending guard on every route → Task 10; (6) automated browser coverage → Task 11.
