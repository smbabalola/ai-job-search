# Ticket 12B — Evidence Profile Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user edit the canonical Evidence Profile Markdown source in-app after initial setup, through a preview-then-confirm pipeline with exact-preview binding, file-content-hash optimistic concurrency, and crash-safe atomic commit — without weakening evidence integrity, without a second source of profile-history truth, and without touching Ticket 7, Lane B, Gate 4, or Search Workspaces.

**Architecture:** A new `product/profile_editing_schema.py` declares, per editable concept, where it lives in the canonical `/setup`-generated Markdown shape and what `(category, field)` it must parse back to via the *existing*, unmodified `product/profile_snapshot.py`. A new `webapp/services/profile_editing.py` implements `start_edit` → `preview_edit` → `confirm_edit`, backed by a new `profile_edit_changesets` table (added via a generalized, list-driven `apply_migrations`) that records exact-preview-bound prospective bytes and drives three-way crash recovery on startup. A new `webapp/api/profile_editing.py` router exposes this over HTTP following the existing `webapp/api/search_workspaces.py` `409`-on-conflict convention. `webapp/templates/profile.html` gains Edit and Preview states alongside its existing View-only mode.

**Tech Stack:** Python 3, FastAPI + Pydantic (`StrictBody` pattern), SQLite (stdlib `sqlite3`), Jinja2 templates, existing vanilla-JS `webapp/static/app.js` patterns.

**Spec:** `docs/superpowers/specs/2026-08-22-ticket12b-evidence-profile-manager-design.md`

## Global Constraints

- **Exact-preview binding**: `confirm_edit` takes only a `changeset_id`. It never accepts an edits payload and never re-renders Markdown from one — it writes exactly the bytes stored at preview time, after re-verifying their own hash. (Spec: "Decision area 3", "Confirm sequence" step 3.)
- **No write path outside preview → confirm.** No PATCH/PUT endpoint for profile source files, ever. (Spec: "API surface".)
- **Concurrency is direct file-content SHA-256 hashing** of all three `product.profile_snapshot.SOURCE_PATHS` files plus the current snapshot's `content_id` (via `product.job_fit.profile_snapshot_content_id`) — never a DB revision counter. (Spec: "Decision area 2".)
- **Three-way crash recovery**, exactly: file content equals `new_markdown` → resume snapshot rebuild; equals `previous_markdown` → mark `rolled_back`; equals **neither** → mark `recovery_conflict` and fail closed, never guess, never write. (Spec: "Three-way crash recovery".)
- **`new_markdown`/`previous_markdown` are transient.** Cleared to `NULL` only on reaching `committed` or `rolled_back`. **Never cleared on `recovery_conflict`** — that row still needs its bytes for manual resolution. (Spec: "Storage rule".)
- **Editing schema and snapshot parser are independently versioned and reconciled at preview time**, never assumed consistent. A field whose rewritten Markdown doesn't parse back to its declared `(category, field)` is a hard preview failure. (Spec: "Decision area 1".)
- **Unmanaged Markdown content is preserved byte-for-byte or the edit fails closed** — never silently discarded, never guessed at. (Spec: "Decision area 1".)
- **Editing schema targets only the canonical `/setup`-generated Markdown shape** (Identity as field-list; Languages/Education as tables; Professional Experience as `### Title - Company (Start - End)` sub-headings with a location line + bullets; Independent Projects/Technical Skills/Publications/Awards/Certifications as flat bulleted lists) — confirmed as the actual current template shape in `.claude/skills/job-application-assistant/01-candidate-profile.md`. A section not in this shape fails closed with an explicit "not in an editable format" error; it remains fully readable (unchanged parser), just not editable via this feature until brought into canonical shape. **This does not support every format variant `product/profile_snapshot.py`'s parser tolerates for backward-compatible reading** — that's a deliberate scope decision, not an oversight.
- **No changes to `product/profile_snapshot.py`'s parsing/build logic**, Ticket 7, Lane B, Gate 4, or Search Workspaces. Every touchpoint with existing systems (`build_snapshot`, `refresh_profile`, `profile_snapshot_content_id`) is used exactly as `webapp/services/profile_setup.py` already uses them.
- **No `409` response body ever includes the changed source's actual content** — only identity/hash information, per the spec's conflict-response shape.
- **`recovery_conflict` blocks all further profile edits** until manually resolved to a terminal state; this is enforced at the API layer, not just the UI.

---

## File Structure

| File | Responsibility |
|---|---|
| `product/profile_editing_schema.py` | New. Declares editable fields, their canonical-Markdown location pattern, and their expected `(category, field)` in a rebuilt snapshot. `PROFILE_EDITING_SCHEMA_VERSION` constant. |
| `webapp/persistence/profile_edit_changesets.py` | New. Row-level CRUD for the `profile_edit_changesets` table: insert at preview, transition status, read for confirm/recovery, clear transient columns. |
| `webapp/persistence/migrations.py` | Modify. Generalize `apply_migrations` into a list-driven loop; add migration `002_profile_edit_changesets`. |
| `webapp/services/profile_editing.py` | New. `start_edit`, `preview_edit`, `confirm_edit`, `recover_interrupted_changesets` (startup recovery), Markdown section rewrite + unmanaged-content preservation, hash computation. |
| `webapp/api/profile_editing.py` | New. FastAPI router: `POST /api/profile/edit-sessions`, `POST /api/profile/edit-sessions/{id}/preview`, `POST /api/profile/edit-changesets/{id}/confirm`, `GET /api/profile/edit-changesets/{id}`. |
| `webapp/app.py` | Modify. Register the new router; call `recover_interrupted_changesets` at startup. |
| `webapp/templates/profile.html` | Modify. Add Edit and Preview states; update the two lines of copy that currently claim editing doesn't happen here. |
| `webapp/static/app.js` | Modify. Client-side flow for start/preview/confirm, 409 handling, recovery-banner display. |
| Tests | `tests/test_profile_editing_schema.py`, `tests/webapp/persistence/test_profile_edit_changesets.py`, `tests/webapp/persistence/test_migrations_profile_edit_changesets.py`, `tests/webapp/services/test_profile_editing.py`, `tests/webapp/api/test_profile_editing_routes.py`. |

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

Read `webapp/persistence/migrations.py` lines 1-61 (the `MIGRATION_ID` constant and `apply_migrations` function) exactly as shown in this plan's grounding — do not assume the shape; re-read the actual current file, since other work may have touched it between plan-writing and implementation.

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

## Task 2: `profile_edit_changesets` table (migration 002)

**Files:**
- Modify: `webapp/persistence/migrations.py`
- Test: `tests/webapp/persistence/test_migrations.py`

**Interfaces:**
- Consumes: `MIGRATIONS` list, `_execute_statements`, `_now` from Task 1.
- Produces: `profile_edit_changesets` table (see spec's exact DDL, reproduced below), migration id `002_profile_edit_changesets` appended to `MIGRATIONS`.

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


def test_profile_edit_changesets_status_check_constraint():
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
                'previewed', 'file_written', 'committed', 'rolled_back', 'recovery_conflict'
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

Append to `MIGRATIONS`:

```python
MIGRATIONS: list[tuple[str, "Callable[[sqlite3.Connection], None]"]] = [
    ("001_search_workspaces", lambda conn: _migrate_search_workspaces(conn)),
    ("002_profile_edit_changesets", lambda conn: _migrate_profile_edit_changesets(conn)),
]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/webapp/persistence/test_migrations.py -v`
Expected: PASS (5 tests total)

- [ ] **Step 5: Run full persistence suite for regressions**

Run: `python -m pytest tests/webapp/persistence/ -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add webapp/persistence/migrations.py tests/webapp/persistence/test_migrations.py
git commit -m "Add profile_edit_changesets table (migration 002)"
```

---

## Task 3: `profile_edit_changesets` persistence layer (row CRUD)

**Files:**
- Create: `webapp/persistence/profile_edit_changesets.py`
- Test: `tests/webapp/persistence/test_profile_edit_changesets.py`

**Interfaces:**
- Consumes: `sqlite3.Connection` (via the existing `get_conn` dependency pattern elsewhere — this module itself takes a raw connection, matching `webapp/persistence/artifacts.py`'s style).
- Produces:
  - `insert_previewed_changeset(conn, *, changeset_id: str, editing_schema_version: str, precondition_source_hashes: dict[str, str], precondition_snapshot_content_id: str, prospective_source_hash: str, diff_summary: dict, new_markdown: str, previous_markdown: str, created_at: str) -> None`
  - `get_changeset(conn, changeset_id: str) -> dict | None`
  - `mark_file_written(conn, changeset_id: str) -> None`
  - `mark_committed(conn, changeset_id: str, *, committed_at: str) -> None` (also clears `new_markdown`/`previous_markdown`)
  - `mark_rolled_back(conn, changeset_id: str) -> None` (also clears `new_markdown`/`previous_markdown`)
  - `mark_recovery_conflict(conn, changeset_id: str) -> None` (does **not** clear the transient columns)
  - `list_file_written_changesets(conn) -> list[dict]` (for startup recovery)

- [ ] **Step 1: Write the failing test**

```python
import json
import sqlite3

import pytest

from webapp.persistence.migrations import apply_migrations
from webapp.persistence.profile_edit_changesets import (
    get_changeset,
    insert_previewed_changeset,
    list_file_written_changesets,
    mark_committed,
    mark_file_written,
    mark_recovery_conflict,
    mark_rolled_back,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_migrations(c)
    yield c
    c.close()


def _insert(conn, changeset_id="pec_1"):
    insert_previewed_changeset(
        conn, changeset_id=changeset_id, editing_schema_version="v0",
        precondition_source_hashes={"CLAUDE.md": "aaa"},
        precondition_snapshot_content_id="cps_1",
        prospective_source_hash="bbb",
        diff_summary={"added": []},
        new_markdown="# new", previous_markdown="# old",
        created_at="2026-01-01T00:00:00Z",
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


def test_mark_file_written_transitions_status(conn):
    _insert(conn)
    mark_file_written(conn, "pec_1")
    assert get_changeset(conn, "pec_1")["status"] == "file_written"


def test_mark_committed_clears_transient_columns(conn):
    _insert(conn)
    mark_file_written(conn, "pec_1")
    mark_committed(conn, "pec_1", committed_at="2026-01-01T00:01:00Z")
    row = get_changeset(conn, "pec_1")
    assert row["status"] == "committed"
    assert row["new_markdown"] is None
    assert row["previous_markdown"] is None
    assert row["committed_at"] == "2026-01-01T00:01:00Z"


def test_mark_rolled_back_clears_transient_columns(conn):
    _insert(conn)
    mark_file_written(conn, "pec_1")
    mark_rolled_back(conn, "pec_1")
    row = get_changeset(conn, "pec_1")
    assert row["status"] == "rolled_back"
    assert row["new_markdown"] is None
    assert row["previous_markdown"] is None


def test_mark_recovery_conflict_does_not_clear_transient_columns(conn):
    _insert(conn)
    mark_file_written(conn, "pec_1")
    mark_recovery_conflict(conn, "pec_1")
    row = get_changeset(conn, "pec_1")
    assert row["status"] == "recovery_conflict"
    assert row["new_markdown"] == "# new"
    assert row["previous_markdown"] == "# old"


def test_list_file_written_changesets_only_returns_that_status(conn):
    _insert(conn, "pec_1")
    _insert(conn, "pec_2")
    mark_file_written(conn, "pec_1")
    ids = {row["id"] for row in list_file_written_changesets(conn)}
    assert ids == {"pec_1"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/webapp/persistence/test_profile_edit_changesets.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""Row-level persistence for Evidence Profile edit change-sets.

This table is operational recovery/audit state, never a second source of
profile-history truth -- new_markdown/previous_markdown are cleared as soon
as a row reaches a resolved terminal state (committed, rolled_back). They
are deliberately NOT cleared on recovery_conflict, since that state means
the recovery decision is still ambiguous and the bytes are the material
needed to resolve it.
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


def list_file_written_changesets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM profile_edit_changesets WHERE status = 'file_written'"
    ).fetchall()
    return [dict(row) for row in rows]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/webapp/persistence/test_profile_edit_changesets.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add webapp/persistence/profile_edit_changesets.py tests/webapp/persistence/test_profile_edit_changesets.py
git commit -m "Add profile_edit_changesets persistence layer"
```

---

## Task 4: Editing schema for the canonical Markdown shape

**Files:**
- Create: `product/profile_editing_schema.py`
- Test: `tests/test_profile_editing_schema.py`

**Interfaces:**
- Consumes: nothing from other tasks — this is a standalone declarative module.
- Produces:
  - `PROFILE_EDITING_SCHEMA_VERSION: str` (e.g. `"profile-editing-schema.v0"`)
  - `EDITABLE_FIELDS: dict[str, EditableFieldSpec]` — one entry per editable concept, keyed by a stable field id (e.g. `"identity.name"`, `"employment.job_title"`).
  - `EditableFieldSpec` (a small dataclass or TypedDict): `section: str` (canonical Markdown heading path, e.g. `("Identity",)` or `("Professional Experience",)`), `category: str`, `field: str` (the `(category, field)` this must parse back to per `product/profile_snapshot.py`'s existing `_ASSERTION_TYPE_SHAPES`-equivalent vocabulary — check the exact category/field vocabulary against `product/profile_snapshot.py` directly at implementation time, since this plan must not guess it), `repeatable: bool` (true for employment/education/etc. records, false for singleton identity fields).

- [ ] **Step 1: Determine the exact category/field vocabulary before writing the schema**

Run: `python -c "
import re
text = open('product/profile_snapshot.py', encoding='utf-8').read()
# Find every add_claim(category=..., field=...) call to enumerate the real vocabulary
for m in re.finditer(r'category=\"(\w+)\",\s*\n?\s*field=\"(\w+)\"', text):
    print(m.group(1), m.group(2))
"`

Cross-reference this output against the canonical template's actual sections (`.claude/skills/job-application-assistant/01-candidate-profile.md`, already read during design: Identity, Languages, Education, Professional Experience, Independent Projects, Technical Skills [Programming & ML / Domain Expertise / Software & Tools], Publications, Awards). Do not invent a `(category, field)` pair that doesn't come from this real enumeration.

- [ ] **Step 2: Write the failing test**

```python
from product.profile_editing_schema import (
    EDITABLE_FIELDS,
    PROFILE_EDITING_SCHEMA_VERSION,
)


def test_schema_version_is_a_nonempty_string():
    assert isinstance(PROFILE_EDITING_SCHEMA_VERSION, str) and PROFILE_EDITING_SCHEMA_VERSION


def test_every_editable_field_declares_section_category_and_field():
    assert EDITABLE_FIELDS, "editing schema must declare at least one field"
    for field_id, spec in EDITABLE_FIELDS.items():
        assert spec.section, f"{field_id} missing section"
        assert spec.category, f"{field_id} missing category"
        assert spec.field, f"{field_id} missing field"
        assert isinstance(spec.repeatable, bool)


def test_identity_name_is_declared_and_not_repeatable():
    spec = EDITABLE_FIELDS["identity.name"]
    assert spec.category == "identity"
    assert spec.field == "name"
    assert spec.repeatable is False


def test_employment_fields_are_repeatable():
    for field_id in ("employment.job_title", "employment.date_range", "employment.employer"):
        assert EDITABLE_FIELDS[field_id].repeatable is True
```

(Adjust the exact field ids/assertions in Steps 2-3 to match whatever the real `(category, field)` enumeration from Step 1 actually produces — this test skeleton establishes the shape of the schema's contract, not necessarily every literal field id, which must come from the real vocabulary, not be guessed here.)

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/test_profile_editing_schema.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Implement the schema module**

Structure (fill in the real field list from Step 1's enumeration):

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
"""
from __future__ import annotations

from dataclasses import dataclass

PROFILE_EDITING_SCHEMA_VERSION = "profile-editing-schema.v0"


@dataclass(frozen=True)
class EditableFieldSpec:
    section: tuple[str, ...]
    category: str
    field: str
    repeatable: bool


EDITABLE_FIELDS: dict[str, EditableFieldSpec] = {
    "identity.name": EditableFieldSpec(
        section=("Identity",), category="identity", field="name", repeatable=False,
    ),
    # ... remaining fields from Step 1's real enumeration ...
}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_profile_editing_schema.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add product/profile_editing_schema.py tests/test_profile_editing_schema.py
git commit -m "Add versioned Evidence Profile editing schema"
```

---

## Task 5: Markdown section rewrite with unmanaged-content preservation

**Files:**
- Create: `webapp/services/profile_markdown_rewrite.py`
- Test: `tests/webapp/services/test_profile_markdown_rewrite.py`

**Interfaces:**
- Consumes: `EDITABLE_FIELDS`, `EditableFieldSpec` from Task 4.
- Produces: `rewrite_canonical_markdown(current_markdown: str, edits: dict[str, Any]) -> str`, raising a dedicated `ProfileMarkdownRewriteError` on any fail-closed case — matches how `webapp/services/profile_setup.py` raises `PipelineError` for its own validation failures, so a dedicated exception (not a result-type return) is this codebase's established convention for "this input was rejected."

This is the highest-risk task in this plan — it is the concrete implementation of "preserve unmanaged content or fail closed, never guess." Do not skip the negative-path tests.

- [ ] **Step 1: Write the failing tests — positive path first**

```python
import pytest

from webapp.services.profile_markdown_rewrite import (
    ProfileMarkdownRewriteError,
    rewrite_canonical_markdown,
)

SAMPLE = """---
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
    result = rewrite_canonical_markdown(SAMPLE, {"identity.name": "Ada King"})
    assert "**Name:** Ada King" in result
    assert "<!-- a custom comment nobody should touch -->" in result
    assert "| BSc Mathematics | 2010-2013 | Example University | Numerical methods |" in result


def test_unedited_sections_are_byte_identical():
    result = rewrite_canonical_markdown(SAMPLE, {"identity.name": "Ada King"})
    # Everything from "## Education" onward is untouched by this edit
    education_onward = SAMPLE[SAMPLE.index("## Education"):]
    assert education_onward in result
```

- [ ] **Step 2: Write the failing tests — fail-closed negative paths**

```python
def test_field_not_in_canonical_shape_fails_closed():
    non_canonical = SAMPLE.replace("- **Name:** Ada Lovelace", "Name: Ada Lovelace (no bold, no list marker)")
    with pytest.raises(ProfileMarkdownRewriteError):
        rewrite_canonical_markdown(non_canonical, {"identity.name": "Ada King"})


def test_missing_section_entirely_fails_closed():
    no_identity = SAMPLE.replace("## Identity\n- **Name:** Ada Lovelace\n- **Location:** London, UK\n\n", "")
    with pytest.raises(ProfileMarkdownRewriteError):
        rewrite_canonical_markdown(no_identity, {"identity.name": "Ada King"})


def test_unknown_field_id_fails_closed():
    with pytest.raises(ProfileMarkdownRewriteError):
        rewrite_canonical_markdown(SAMPLE, {"not.a.real.field": "x"})
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/webapp/services/test_profile_markdown_rewrite.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Implement**

The exact regex/line-identification patterns must mirror `product/profile_snapshot.py`'s `_parse_candidate_markdown` patterns for the canonical shape ONLY (per the Global Constraints scope decision) — e.g. for `identity.name`, the target line matches `MARKDOWN_FIELD_RE` for label `Name` inside the `## Identity` section (read `product/profile_snapshot.py`'s actual `MARKDOWN_FIELD_RE` definition and the `Identity` section branch of `_parse_candidate_markdown`, lines ~545-550 as grounded during design, and reuse the exact same regex — do not write a second, subtly-different pattern).

```python
"""Rewrite the canonical Evidence Profile Markdown source for a set of
structured edits, preserving every byte outside the edited fields.

Mirrors product/profile_snapshot.py's canonical-shape parsing patterns
exactly (MARKDOWN_FIELD_RE etc.) so what this module writes is provably
re-parseable by the unmodified parser -- never a second, subtly different
format understanding.
"""
from __future__ import annotations

from typing import Any

from product.profile_editing_schema import EDITABLE_FIELDS
from product.profile_snapshot import MARKDOWN_FIELD_RE  # exact same regex, reused not reimplemented


class ProfileMarkdownRewriteError(ValueError):
    pass


def rewrite_canonical_markdown(current_markdown: str, edits: dict[str, Any]) -> str:
    for field_id in edits:
        if field_id not in EDITABLE_FIELDS:
            raise ProfileMarkdownRewriteError(f"unknown editable field id {field_id!r}")

    lines = current_markdown.splitlines(keepends=True)
    # ... locate each edited field's line within its declared section,
    # matching the canonical-shape pattern for that field's category/field
    # combination; raise ProfileMarkdownRewriteError with a specific reason
    # if the section is missing or the line doesn't match the expected
    # canonical pattern (fail closed, never guess at placement).
    # Full per-field-type rewrite logic (singleton field-list line,
    # repeatable table row, repeatable sub-heading block) implemented here,
    # covering exactly the field types enumerated in Task 4's EDITABLE_FIELDS.
    ...
    return "".join(lines)
```

Implement the full per-field-type rewrite logic referenced in the comment above, covering at minimum: singleton field-list lines (Identity), and enough of the repeatable patterns (Education table rows, Employment sub-heading blocks) to pass this task's own tests plus the acceptance-criteria scenarios from the spec. If a field type declared in `EDITABLE_FIELDS` genuinely has no implementation yet, that field must not be reachable from the API layer in Task 7 until its rewrite logic exists here — do not expose an editable field the rewriter can't actually handle.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/webapp/services/test_profile_markdown_rewrite.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Self-review against the Global Constraint before moving on**

Re-read this task's implementation against the constraint: "Unmanaged Markdown content is preserved byte-for-byte or the edit fails closed — never silently discarded, never guessed at." Confirm every code path either (a) preserves untouched content verbatim, or (b) raises `ProfileMarkdownRewriteError` — there must be no path that silently drops or alters content outside an explicitly edited field.

- [ ] **Step 7: Commit**

```bash
git add webapp/services/profile_markdown_rewrite.py tests/webapp/services/test_profile_markdown_rewrite.py
git commit -m "Add canonical-shape Markdown rewrite with unmanaged-content preservation"
```

---

## Task 6: `start_edit` and hash/precondition computation

**Files:**
- Create: `webapp/services/profile_editing.py`
- Test: `tests/webapp/services/test_profile_editing.py`

**Interfaces:**
- Consumes: `SOURCE_PATHS`, `build_snapshot` from `product/profile_snapshot.py`; `profile_snapshot_content_id` from `product/job_fit.py`; `get_current_profile_snapshot` from `webapp/services/pipeline.py`; `EDITABLE_FIELDS`, `PROFILE_EDITING_SCHEMA_VERSION` from Task 4.
- Produces: `compute_source_hashes(root: Path) -> dict[str, str]` (SHA-256 hex digest per `SOURCE_PATHS` entry); `start_edit(conn, *, root: Path) -> dict[str, Any]` returning `{"editing_schema_version", "editable_fields": {...current values...}, "conflicts": [...], "precondition_source_hashes", "precondition_snapshot_content_id"}`.

- [ ] **Step 1: Write the failing test**

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


def test_start_edit_returns_current_editable_field_values_and_preconditions(tmp_path, conn):
    # conn fixture: build via webapp.persistence.migrations.apply_migrations on
    # an in-memory/tempfile DB, matching the pattern used in Task 3's tests;
    # seed a real profile snapshot first via the existing profile_setup path
    # (import_profile_markdown against a canonical-shape Markdown fixture) so
    # start_edit has a real current snapshot to read.
    result = start_edit(conn, root=tmp_path)
    assert result["editing_schema_version"]
    assert "precondition_source_hashes" in result
    assert "precondition_snapshot_content_id" in result
    assert "editable_fields" in result
    assert "conflicts" in result
```

(The `conn`/seeded-profile fixture setup mirrors whatever pattern `tests/webapp/test_profile_setup.py` or equivalent already uses for a configured profile — read that existing test file first to reuse its fixture-building helpers rather than reinventing them.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""Evidence Profile in-app editing: start_edit -> preview_edit -> confirm_edit.

Exact-preview binding: confirm_edit takes only a changeset_id, never an
edits payload -- it writes exactly the bytes stored at preview time. See
docs/superpowers/specs/2026-08-22-ticket12b-evidence-profile-manager-design.md.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from product.job_fit import profile_snapshot_content_id
from product.profile_editing_schema import EDITABLE_FIELDS, PROFILE_EDITING_SCHEMA_VERSION
from product.profile_snapshot import SOURCE_PATHS
from webapp.services.pipeline import get_current_profile_snapshot


def compute_source_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for relative in SOURCE_PATHS:
        content = (root / relative).read_bytes()
        hashes[relative] = hashlib.sha256(content).hexdigest()
    return hashes


def start_edit(conn: sqlite3.Connection, *, root: Path) -> dict[str, Any]:
    profile_artifact = get_current_profile_snapshot(conn)
    payload = profile_artifact["payload"] if profile_artifact else {}
    current_values = _extract_current_field_values(payload)
    return {
        "editing_schema_version": PROFILE_EDITING_SCHEMA_VERSION,
        "editable_fields": current_values,
        "conflicts": payload.get("conflicts", []),
        "precondition_source_hashes": compute_source_hashes(root),
        "precondition_snapshot_content_id": profile_snapshot_content_id(payload) if payload else None,
    }


def _extract_current_field_values(payload: dict[str, Any]) -> dict[str, Any]:
    claims_by_category_field: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for claim in payload.get("claims", []):
        key = (claim.get("category"), claim.get("field"))
        claims_by_category_field.setdefault(key, []).append(claim)
    result: dict[str, Any] = {}
    for field_id, spec in EDITABLE_FIELDS.items():
        matches = claims_by_category_field.get((spec.category, spec.field), [])
        result[field_id] = (
            [c["value"] for c in matches] if spec.repeatable
            else (matches[0]["value"] if matches else None)
        )
    return result
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/services/profile_editing.py tests/webapp/services/test_profile_editing.py
git commit -m "Add start_edit: current field values and edit preconditions"
```

---

## Task 7: `preview_edit` — render, rebuild, reconcile, diff, persist

**Files:**
- Modify: `webapp/services/profile_editing.py`
- Test: `tests/webapp/services/test_profile_editing.py`

**Interfaces:**
- Consumes: `rewrite_canonical_markdown`, `ProfileMarkdownRewriteError` from Task 5; `build_snapshot` from `product/profile_snapshot.py`; `insert_previewed_changeset` from Task 3; `compute_source_hashes`, `_extract_current_field_values` from Task 6.
- Produces: `preview_edit(conn, *, root: Path, precondition_source_hashes: dict, precondition_snapshot_content_id: str | None, edits: dict[str, Any]) -> dict[str, Any]` returning `{"changeset_id", "diff": {...}, "prospective_snapshot_summary": {...}, "warnings": [...]}`. Raises a dedicated `ProfileEditPreconditionError` (new, in this module) if the precondition no longer matches current state — this is the "session precondition still matches" check from the spec's `preview_edit` step 1; distinct from the confirm-time `409`, since at preview time nothing has been written and the client should simply be told to restart via a fresh `start_edit`.

- [ ] **Step 1: Write the failing tests**

```python
def test_preview_edit_persists_a_previewed_changeset_with_exact_bytes(tmp_path, conn):
    session = start_edit(conn, root=tmp_path)
    result = preview_edit(
        conn, root=tmp_path,
        precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    assert result["changeset_id"]
    from webapp.persistence.profile_edit_changesets import get_changeset
    row = get_changeset(conn, result["changeset_id"])
    assert row["status"] == "previewed"
    assert "Ada King" in row["new_markdown"]


def test_preview_edit_rejects_stale_precondition(tmp_path, conn):
    session = start_edit(conn, root=tmp_path)
    stale_hashes = dict(session["precondition_source_hashes"])
    stale_hashes[list(stale_hashes)[0]] = "deliberately-wrong-hash"
    with pytest.raises(ProfileEditPreconditionError):
        preview_edit(
            conn, root=tmp_path, precondition_source_hashes=stale_hashes,
            precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
            edits={"identity.name": "Ada King"},
        )


def test_preview_edit_two_calls_produce_two_independent_changesets(tmp_path, conn):
    session = start_edit(conn, root=tmp_path)
    first = preview_edit(
        conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    second = preview_edit(
        conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada Lovelace"},
    )
    assert first["changeset_id"] != second["changeset_id"]


def test_preview_edit_reconciliation_failure_does_not_persist_a_changeset(tmp_path, conn):
    # A field whose rewrite doesn't parse back to its declared (category, field)
    # must fail the preview WITHOUT persisting a changeset row.
    ...  # construct via a deliberately-broken canonical file per Task 5's
         # negative-path fixtures, or a monkeypatched EDITABLE_FIELDS entry
         # pointing at a mismatched category/field
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

    canonical_path = root / [p for p in SOURCE_PATHS if "01-candidate-profile" in p][0]
    current_markdown = canonical_path.read_text(encoding="utf-8")
    new_markdown = rewrite_canonical_markdown(current_markdown, edits)  # raises on fail-closed cases

    prospective_payload = _build_prospective_snapshot(root, canonical_path, new_markdown)
    _reconcile_editing_schema_with_parser(edits, prospective_payload)  # raises on disagreement

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

Implement `_build_prospective_snapshot` (write `new_markdown` to a temp copy of `root` at the canonical path, other two sources copied as-is, call `build_snapshot` against the temp dir — directly mirroring `profile_setup.py`'s existing `_validate_prospective_snapshot` pattern, reusing that approach rather than inventing a new one), `_reconcile_editing_schema_with_parser` (for each edited field id, confirm the prospective payload's claims contain the expected `(category, field)` with the edited value; raise the same `ProfileMarkdownRewriteError` from Task 5 on mismatch — this is conceptually the same failure class, "this rewrite cannot be trusted," so it reuses that exception rather than introducing a fourth type), `_compute_diff` (added/changed/removed claims between `current_payload` and `prospective_payload`, plus conflicts newly introduced / no longer present / unchanged-and-still-conflicted, per the spec's Preview state field list), `_summarize`, and `_now` (reuse the `_now()` pattern already established in `webapp/persistence/migrations.py` if a shared helper doesn't already exist elsewhere — check for one before adding a duplicate).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/services/profile_editing.py tests/webapp/services/test_profile_editing.py
git commit -m "Add preview_edit: render, reconcile, diff, persist exact-preview bytes"
```

---

## Task 8: `confirm_edit` — exact-preview-bound commit sequence

**Files:**
- Modify: `webapp/services/profile_editing.py`
- Test: `tests/webapp/services/test_profile_editing.py`

**Interfaces:**
- Consumes: `get_changeset`, `mark_file_written`, `mark_committed` from Task 3; `refresh_profile` from `webapp/services/pipeline.py`; the existing `_atomic_write_bytes` pattern from `webapp/services/profile_setup.py` (import and reuse it directly rather than duplicating — check whether it needs to be exported/renamed to be importable, or duplicate the ~15-line function only if genuinely necessary; prefer import).
- Produces: `confirm_edit(conn, *, root: Path, changeset_id: str) -> dict[str, Any]` returning `{"profile": <new snapshot artifact>, "warnings": [...]}`. Raises `ProfileEditConflictError` (new) on any of the confirm-sequence's three precondition mismatches — this is the exception the API layer (Task 9) maps to `409`.

- [ ] **Step 1: Write the failing tests**

```python
def test_confirm_edit_writes_exactly_the_previewed_bytes(tmp_path, conn):
    session = start_edit(conn, root=tmp_path)
    preview = preview_edit(
        conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    from webapp.persistence.profile_edit_changesets import get_changeset
    expected_bytes = get_changeset(conn, preview["changeset_id"])["new_markdown"]

    result = confirm_edit(conn, root=tmp_path, changeset_id=preview["changeset_id"])

    canonical_path = root / [p for p in SOURCE_PATHS if "01-candidate-profile" in p][0]
    assert canonical_path.read_text(encoding="utf-8") == expected_bytes
    assert result["profile"]["payload"]["claims"]  # a real rebuilt snapshot came back


def test_confirm_edit_rejects_stale_source(tmp_path, conn):
    session = start_edit(conn, root=tmp_path)
    preview = preview_edit(
        conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    canonical_path = root / [p for p in SOURCE_PATHS if "01-candidate-profile" in p][0]
    canonical_path.write_text(
        canonical_path.read_text(encoding="utf-8") + "\n<!-- external edit -->\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfileEditConflictError):
        confirm_edit(conn, root=tmp_path, changeset_id=preview["changeset_id"])
    # nothing written beyond the external edit
    assert "Ada King" not in canonical_path.read_text(encoding="utf-8")


def test_confirming_one_changeset_invalidates_a_sibling_preview(tmp_path, conn):
    session = start_edit(conn, root=tmp_path)
    first = preview_edit(
        conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    second = preview_edit(
        conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada Lovelace"},
    )
    confirm_edit(conn, root=tmp_path, changeset_id=first["changeset_id"])
    with pytest.raises(ProfileEditConflictError):
        confirm_edit(conn, root=tmp_path, changeset_id=second["changeset_id"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v -k confirm`
Expected: FAIL — `confirm_edit` doesn't exist yet.

- [ ] **Step 3: Implement**

```python
from webapp.persistence.profile_edit_changesets import (
    get_changeset, mark_committed, mark_file_written,
)
from webapp.services.profile_setup import _atomic_write_bytes  # reuse, do not duplicate


class ProfileEditConflictError(ValueError):
    def __init__(self, reason: str, *, source_changed: bool = False, snapshot_changed: bool = False):
        self.reason = reason
        self.source_changed = source_changed
        self.snapshot_changed = snapshot_changed
        super().__init__(reason)


def confirm_edit(conn: sqlite3.Connection, *, root: Path, changeset_id: str) -> dict[str, Any]:
    row = get_changeset(conn, changeset_id)
    if row is None:
        raise ProfileEditConflictError(f"unknown changeset {changeset_id!r}")
    if row["status"] != "previewed":
        raise ProfileEditConflictError(f"changeset {changeset_id!r} is not in a confirmable state (status={row['status']!r})")

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

    canonical_path = root / [p for p in SOURCE_PATHS if "01-candidate-profile" in p][0]
    _atomic_write_bytes(canonical_path, row["new_markdown"].encode("utf-8"))
    mark_file_written(conn, changeset_id)

    artifact = refresh_profile(conn, root=str(root))

    mark_committed(conn, changeset_id, committed_at=_now())
    return {"profile": artifact, "warnings": []}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/services/profile_editing.py tests/webapp/services/test_profile_editing.py
git commit -m "Add confirm_edit: exact-preview-bound atomic commit"
```

---

## Task 9: Three-way crash recovery

**Files:**
- Modify: `webapp/services/profile_editing.py`
- Test: `tests/webapp/services/test_profile_editing.py`

**Interfaces:**
- Consumes: `list_file_written_changesets`, `mark_committed`, `mark_rolled_back`, `mark_recovery_conflict` from Task 3.
- Produces: `recover_interrupted_changesets(conn, *, root: Path) -> dict[str, Any]` returning a summary (`{"resumed": [...], "rolled_back": [...], "recovery_conflicts": [...]}`) for startup logging.

- [ ] **Step 1: Write the failing tests — the three cases explicitly**

```python
def test_recovery_case_file_matches_new_markdown_resumes_and_commits(tmp_path, conn):
    session = start_edit(conn, root=tmp_path)
    preview = preview_edit(
        conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    from webapp.persistence.profile_edit_changesets import get_changeset, mark_file_written
    row = get_changeset(conn, preview["changeset_id"])
    canonical_path = root / [p for p in SOURCE_PATHS if "01-candidate-profile" in p][0]
    canonical_path.write_text(row["new_markdown"], encoding="utf-8")  # simulate: write completed
    mark_file_written(conn, preview["changeset_id"])  # ...but crash before refresh_profile

    summary = recover_interrupted_changesets(conn, root=tmp_path)

    assert preview["changeset_id"] in summary["resumed"]
    assert get_changeset(conn, preview["changeset_id"])["status"] == "committed"


def test_recovery_case_file_matches_previous_markdown_rolls_back(tmp_path, conn):
    session = start_edit(conn, root=tmp_path)
    preview = preview_edit(
        conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    from webapp.persistence.profile_edit_changesets import get_changeset, mark_file_written
    mark_file_written(conn, preview["changeset_id"])  # simulate: marked but the write never actually happened

    summary = recover_interrupted_changesets(conn, root=tmp_path)

    assert preview["changeset_id"] in summary["rolled_back"]
    row = get_changeset(conn, preview["changeset_id"])
    assert row["status"] == "rolled_back"
    assert row["new_markdown"] is None and row["previous_markdown"] is None


def test_recovery_case_file_matches_neither_fails_closed_and_preserves_bytes(tmp_path, conn):
    session = start_edit(conn, root=tmp_path)
    preview = preview_edit(
        conn, root=tmp_path, precondition_source_hashes=session["precondition_source_hashes"],
        precondition_snapshot_content_id=session["precondition_snapshot_content_id"],
        edits={"identity.name": "Ada King"},
    )
    from webapp.persistence.profile_edit_changesets import get_changeset, mark_file_written
    mark_file_written(conn, preview["changeset_id"])
    canonical_path = root / [p for p in SOURCE_PATHS if "01-candidate-profile" in p][0]
    external_content = "# something completely different, changed by an external process\n"
    canonical_path.write_text(external_content, encoding="utf-8")

    summary = recover_interrupted_changesets(conn, root=tmp_path)

    assert preview["changeset_id"] in summary["recovery_conflicts"]
    row = get_changeset(conn, preview["changeset_id"])
    assert row["status"] == "recovery_conflict"
    assert row["new_markdown"] is not None  # bytes preserved, not cleared
    assert row["previous_markdown"] is not None
    assert canonical_path.read_text(encoding="utf-8") == external_content  # untouched by recovery
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v -k recover`
Expected: FAIL — `recover_interrupted_changesets` doesn't exist yet.

- [ ] **Step 3: Implement**

```python
def recover_interrupted_changesets(conn: sqlite3.Connection, *, root: Path) -> dict[str, list[str]]:
    summary: dict[str, list[str]] = {"resumed": [], "rolled_back": [], "recovery_conflicts": []}
    canonical_path = root / [p for p in SOURCE_PATHS if "01-candidate-profile" in p][0]
    current_content = canonical_path.read_text(encoding="utf-8") if canonical_path.exists() else None

    for row in list_file_written_changesets(conn):
        changeset_id = row["id"]
        if current_content == row["new_markdown"]:
            artifact = refresh_profile(conn, root=str(root))
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/webapp/services/test_profile_editing.py -v`
Expected: PASS, full file green.

- [ ] **Step 5: Commit**

```bash
git add webapp/services/profile_editing.py tests/webapp/services/test_profile_editing.py
git commit -m "Add three-way crash recovery for interrupted profile edits"
```

---

## Task 10: API surface

**Files:**
- Create: `webapp/api/profile_editing.py`
- Modify: `webapp/app.py` (register router; call `recover_interrupted_changesets` at startup)
- Test: `tests/webapp/api/test_profile_editing_routes.py`

**Interfaces:**
- Consumes: `start_edit`, `preview_edit`, `confirm_edit`, `ProfileEditPreconditionError`, `ProfileEditConflictError` from Tasks 6-9; `get_conn` from `webapp/api/dependencies.py`; `EDITABLE_FIELDS` from Task 4 (for request validation).
- Produces: the four routes from the spec's API surface, registered under `/api/profile`.

- [ ] **Step 1: Read `webapp/app.py`'s router-registration pattern before editing**

Read the file to find how `webapp/api/profile.py` and `webapp/api/search_workspaces.py` are currently registered (likely `app.include_router(...)`), and match that exact pattern for the new router. Also find where (if anywhere) startup-time initialization already runs (e.g. `ensure_profile_workspace`, `apply_migrations`) — `recover_interrupted_changesets` must run at the same point, after migrations, before the app serves traffic.

- [ ] **Step 2: Write the failing test**

```python
from fastapi.testclient import TestClient


def test_start_edit_returns_200_with_expected_shape(client):  # `client` fixture: existing TestClient pattern from tests/webapp/api/*
    response = client.post("/api/profile/edit-sessions")
    assert response.status_code == 200
    body = response.json()
    assert "editing_schema_version" in body
    assert "precondition_source_hashes" in body


def test_preview_then_confirm_happy_path(client):
    session = client.post("/api/profile/edit-sessions").json()
    preview = client.post(
        f"/api/profile/edit-sessions/preview",
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
        "/api/profile/edit-sessions/preview",
        json={
            "precondition_source_hashes": session["precondition_source_hashes"],
            "precondition_snapshot_content_id": session["precondition_snapshot_content_id"],
            "edits": {"identity.name": "Ada King"},
        },
    ).json()
    # simulate an external change between preview and confirm via a second edit+confirm
    client.post("/api/profile/edit-changesets/" + preview["changeset_id"] + "/confirm")
    # now confirming any OTHER previously-issued preview (if one existed) would 409;
    # simplest direct test: re-confirm the same (already-committed) changeset
    second_confirm = client.post(f"/api/profile/edit-changesets/{preview['changeset_id']}/confirm")
    assert second_confirm.status_code == 409
    body = second_confirm.json()
    assert "source" not in str(body).lower() or "changed" in body["detail"].lower()  # no raw content leaked
    # more precisely: assert the response body contains no full markdown/file content
    assert len(str(body)) < 2000  # conflict responses are small, identity-only -- not full source dumps


def test_get_changeset_status(client):
    session = client.post("/api/profile/edit-sessions").json()
    preview = client.post(
        "/api/profile/edit-sessions/preview",
        json={
            "precondition_source_hashes": session["precondition_source_hashes"],
            "precondition_snapshot_content_id": session["precondition_snapshot_content_id"],
            "edits": {"identity.name": "Ada King"},
        },
    ).json()
    status = client.get(f"/api/profile/edit-changesets/{preview['changeset_id']}")
    assert status.status_code == 200
    assert status.json()["status"] == "previewed"
```

(Reconcile the exact route paths/request shapes against the spec's API surface section precisely — `POST /api/profile/edit-sessions/{session_id}/preview` per the spec includes a `session_id` path parameter; if `start_edit` in this implementation doesn't persist a server-side session row [it doesn't — Task 6 returns everything the client needs to carry itself], decide at implementation time whether to keep the `{session_id}` path segment as a client-supplied opaque token or drop it in favor of passing the precondition directly in the preview request body as sketched above. This is a small API-shape decision left open by the spec's prose sketch; pick whichever avoids an unnecessary server-side session table for state that's already fully captured in the precondition tuple — the tests above assume no `{session_id}` path segment. If a reviewer disagrees, this is cheap to change since nothing else depends on it yet.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/webapp/api/test_profile_editing_routes.py -v`
Expected: FAIL — router not registered / module doesn't exist.

- [ ] **Step 4: Implement the router**

Follow `webapp/api/search_workspaces.py`'s `StrictBody` + `_mutate`-style error-mapping pattern exactly:

```python
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from webapp.api.dependencies import get_conn
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


@router.post("/edit-sessions")
def post_edit_session(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return start_edit(conn, root=request.app.state.settings.profile_root)


@router.post("/edit-sessions/preview")
def post_preview_edit(
    body: PreviewEditBody, request: Request, conn: sqlite3.Connection = Depends(get_conn),
):
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
    try:
        return confirm_edit(conn, root=request.app.state.settings.profile_root, changeset_id=changeset_id)
    except ProfileEditConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "source_changed" if exc.source_changed else "snapshot_changed" if exc.snapshot_changed else "conflict",
                "source_changed": exc.source_changed,
                "snapshot_changed": exc.snapshot_changed,
                "message": str(exc),
            },
        ) from exc


@router.get("/edit-changesets/{changeset_id}")
def get_edit_changeset(changeset_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    from webapp.persistence.profile_edit_changesets import get_changeset
    row = get_changeset(conn, changeset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="changeset not found")
    row.pop("new_markdown", None)
    row.pop("previous_markdown", None)
    return row
```

Note the `GET` handler explicitly strips `new_markdown`/`previous_markdown` from the response even while a row still holds them (`file_written`/`recovery_conflict` states) — per the Global Constraint that the API never returns raw mid-recovery source bytes to a client, regardless of the row's internal storage state.

Register the router in `webapp/app.py` (match the existing `app.include_router(...)` call site for `webapp.api.profile`'s router) and add a startup call to `recover_interrupted_changesets` immediately after wherever `apply_migrations` currently runs at startup.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/webapp/api/test_profile_editing_routes.py -v`
Expected: PASS

- [ ] **Step 6: Run the full webapp API test suite for regressions**

Run: `python -m pytest tests/webapp/ -v`
Expected: PASS, no regressions.

- [ ] **Step 7: Commit**

```bash
git add webapp/api/profile_editing.py webapp/app.py tests/webapp/api/test_profile_editing_routes.py
git commit -m "Add Evidence Profile editing API routes"
```

---

## Task 11: UI — Edit and Preview states, recovery banner

**Files:**
- Modify: `webapp/templates/profile.html`
- Modify: `webapp/static/app.js`
- Manual verification: no automated UI test in this plan (no existing browser-test precedent for `profile.html` beyond the pre-existing `test_browser_smoke.py` suite — extend that file only if a reviewer requests it; this task's Step 4 is a manual browser check per this repo's own CLAUDE.md convention for UI changes)

**Interfaces:**
- Consumes: the four API routes from Task 10.
- Produces: an "Edit Evidence Profile" action in View mode; an Edit form rendering `EDITABLE_FIELDS`-shaped current values; a Preview screen; a recovery banner.

- [ ] **Step 1: Update the two lines of copy that currently claim editing doesn't happen here**

In `webapp/templates/profile.html`:
- Line 5 currently reads: `<p>This page shows what the product can cite. Update source profile files outside this interface, then refresh the snapshot.</p>` — update to reflect that editing is now possible in-app (exact wording is a copy decision, not an architectural one; keep it accurate and brief).
- Line 31 currently reads: `<p>No editing or conflict resolution occurs here.</p>` inside the "Evidence claims" panel — this is specifically about the View-mode claims list, which genuinely still has no inline editing (editing happens via the separate Edit mode/form) — reword to something like "Edit Evidence Profile to make changes; conflicts are resolved by editing source material, never by choosing a value here" rather than deleting the sentence's intent, since the underlying claim (no inline resolve-conflict UI) remains true and is a Global Constraint.

- [ ] **Step 2: Add the "Edit Evidence Profile" entry point and Edit form**

Add a button in the existing `<section class="hero compact">` block (near the existing `data-action="refresh-profile"` button) that triggers Edit mode. Render the Edit form grouped exactly per the spec's UI structure section: Identity, Employment, Education, Skills/tools, Certifications, Languages, Publications, Awards — driven by `EDITABLE_FIELDS`' `section` grouping (Task 4), populated with `start_edit`'s current values (Task 6/10). Each field shows its source relationship (which of the three `SOURCE_PATHS` it comes from — always the canonical Markdown for editable fields, but display it explicitly for clarity) and, when the field's `(category, field)` matches a current conflict's, the existing conflict annotation text — no "Accept winner" / "Resolve conflict" / "Mark verified" control anywhere in this form, per the Global Constraint.

Unmanaged content gets a small static notice, not an editable control: something like "Content outside these fields is preserved unchanged."

No "Save" button — only "Preview changes", which POSTs to `/api/profile/edit-sessions/preview` (per Task 10's actual route shape) and transitions to the Preview screen.

- [ ] **Step 3: Add the Preview screen**

Dedicated state (not a modal), showing exactly the fields from the spec's "Preview state" section: added/changed/removed claims, conflicts newly introduced / no longer present / unchanged-and-still-conflicted, warnings, affected source file(s), prospective snapshot summary. The confirm button's label is exactly: **"Apply exactly these reviewed changes to the Evidence Profile."** — POSTs to `/api/profile/edit-changesets/{changeset_id}/confirm`.

On a `409` response from confirm, show exactly the spec's specified message: "Your Evidence Profile changed after this edit began. Nothing was written. Reload the latest profile and review your changes again." — and return to View mode (forcing a fresh `start_edit` on next attempt, since precondition mismatch always means restart per the spec).

- [ ] **Step 4: Add the recovery banner**

If `GET /api/profile` (or a startup-recovery-status signal — check whether Task 10's startup call should also expose a lightweight `GET` for "is there an unresolved recovery_conflict" state, and add one if not already covered) indicates an unresolved `recovery_conflict`, show a blocking banner with the spec's exact wording: "An Evidence Profile update was interrupted and the source changed unexpectedly. No automatic recovery was attempted." — and disable the "Edit Evidence Profile" action while this banner is showing.

- [ ] **Step 5: Manual browser verification**

Start the dev server (check `SETUP.md`/`CONTRIBUTING.md` for the exact run command used elsewhere in this repo's UI-change workflow) and manually exercise: View → Edit → Preview → Confirm happy path; a 409 by editing the canonical file externally between preview and confirm; the recovery banner (seed a `recovery_conflict` row directly via the persistence layer in a throwaway script, load the page, confirm the banner shows and editing is blocked, then clean up the seeded row).

- [ ] **Step 6: Commit**

```bash
git add webapp/templates/profile.html webapp/static/app.js
git commit -m "Add Evidence Profile edit/preview UI and recovery banner"
```

---

## Task 12: Full regression pass and spec acceptance-criteria check

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -q`
Expected: PASS (allowing for the same pre-existing, unrelated Playwright missing-browser-binary errors already documented in Lane B's and Ticket 12A's final verification — confirm these are still the only non-passing items, not new ones).

- [ ] **Step 2: Walk the spec's acceptance criteria explicitly**

For each bullet in the spec's "Acceptance criteria" section, point to the specific test(s) from Tasks 1-11 that cover it. If any criterion has no corresponding test, add one now before calling this task done — do not defer a spec-mandated acceptance criterion silently.

- [ ] **Step 3: Confirm no scope leak**

Run: `git diff master --stat` (from this branch) and confirm no file under a path owned by Ticket 7 (`product/semantic_job_fit.py`, `product/job_understanding*.py`), Lane B (`product/application_intelligence*.py`, `product/openai_application_intelligence_provider.py`), Gate 4, or Search Workspaces (`webapp/persistence/search_workspaces.py`, `webapp/api/search_workspaces.py`) appears in the diff.

- [ ] **Step 4: Confirm `product/profile_snapshot.py` is byte-identical to `master`**

Run: `git diff master -- product/profile_snapshot.py`
Expected: empty output.

---

## Self-Review Notes

**Spec coverage:**
- "Decision area 1" (unmanaged-content preservation, editing-schema/parser reconciliation) → Tasks 4, 5, 7.
- "Decision area 2" (file-content-hash concurrency) → Task 6 (`compute_source_hashes`), enforced at Tasks 7 and 8.
- "Decision area 3" (exact-preview binding, durable change-set, three-way recovery, transient storage) → Tasks 3, 7, 8, 9.
- "API surface" → Task 10.
- "UI structure" / "Preview state" / "Recovery UX" → Task 11.
- "Lifecycle rule" (one confirmable changeset per precondition) → verified as a structural consequence in Task 8's `test_confirming_one_changeset_invalidates_a_sibling_preview`, not separately implemented, matching the spec's own framing.
- Migration-system gap discovered during planning (not in the spec, a real pre-existing constraint) → Task 1, resolved before Task 2 needs it.

**Placeholder scan:** the two open items (Task 4's exact field-id list, Task 10's `{session_id}` path-segment question) are both explicitly flagged as small, low-risk, implementation-time decisions with a stated resolution path — not silent gaps. Every other task has concrete code.

**Type consistency check:** `ProfileEditPreconditionError` (Task 7, preview-time) and `ProfileEditConflictError` (Task 8, confirm-time) are intentionally distinct exception types mapped to different situations (restart-required vs. 409-conflict) — verified this distinction is preserved consistently from Task 7/8's implementation through Task 10's exception handling, matching the spec's own preview-vs-confirm precondition-check distinction. `ProfileMarkdownRewriteError` (Task 5) is deliberately reused, not re-invented, for Task 7's schema/parser reconciliation failure — both are "this rewrite cannot be trusted" cases; caught this during self-review (the first draft's Task 5 interface line and Task 7's reconciliation description each independently proposed a different exception-handling approach) and fixed both to be consistent. `EDITABLE_FIELDS`/`EditableFieldSpec` (Task 4) is consumed with the same field names (`section`, `category`, `field`, `repeatable`) in Tasks 5, 6, and 11 — no drift introduced.
