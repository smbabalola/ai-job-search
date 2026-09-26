# Bundle 6D-A — Review & Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The human review and approval contract. A Prepared Applications review surface lets the user inspect, replace and answer everything JobSearch would send, then approve an exact, hash-bound binding for later FILL. Approval can never become submission authority.

**Architecture:**
- **Pure contract** (`product/review_contract.py`) computes the approval binding, component hashes, claim-provenance digests, review state, `binding_matches` / `approval_effective`, invalidation reasons and `delta_only`.
- **Services:**
  - assemble the reviewable application from current state (planned fields, v2 exact documents, claims, warnings);
  - Save changes creates the immutable v2 pack;
  - the approval transaction writes only approval rows and events.
- **Persistence:** append-only tables (migration `019_review_approval`) record approvals, review events, deltas and field dispositions. All current state is derived by `seq`.
- **SUBMIT refusal:** `request_grant(SUBMIT)` and `pre_click_commit` refuse unconditionally.

**Tech Stack:** Python 3.13, SQLite (WAL), FastAPI + Jinja2 templates, pytest + Hypothesis 6.168.1, pytest-playwright.

**Spec:** `docs/superpowers/specs/2026-09-26-bundle6d-a-review-approval-design.md` (frozen at `96f39b07840a1c27f1058510c7aa9e1eef2b4e23`).

## Global Constraints

- **Base:** `master@fc316eec0050b6ae9deada2981089cbac1532adb`. The implementation branch is `bundle6/6d-a-review-approval`, created from this design branch so the spec and plan travel with it.
- **I-1:** no action beyond what the user reviewed inherits approval. It's enforced by binding-hash equality, never by timestamps or flags.
- **I-2:** approval is consent, not authority. Review, edit, Save and approve work while paused or halted; only FILL grants and execution are blocked.
- **I-7:** approval never creates or changes the pack it approves. Save changes creates the immutable v2 pack first.
- **I-8:** every known field is bound to `ANSWER` or `OMIT`. `OMIT` is allowed only for optional fields, and the system never defaults an optional field.
- **Approve request:** `POST .../review/approve` takes only `{displayed_binding_hash}`. Acknowledgement is its own action.
- **Write contract:** a normal approval writes 1 `application_approvals` row + 1 `APPROVED` event + the 6C queue wake. A delta re-approval also writes exactly 1 `DELTA_RESOLVED` per resolved delta. Approval never writes content or acknowledgements.
- **Effective approval:** `APPROVED_FOR_FILL` == `approval_effective` == `binding_matches` AND no blocking issue AND no unacknowledged ATTENTION AND no open delta AND not revoked AND not expired (TTL default 14 days, configurable shorter only).
- **Bulk eligibility:** requires `REVIEW_PRESENTED(current_binding_hash)`, recorded only by the human-facing page route `GET /workspaces/{id}/review`. The data API GET is read-only.
- **Exact files (D1):** approval requires the v2 exact-file pack. v1, or CV-v2 disabled (`JOBSEARCH_ENABLE_CV_QUALITY_V2` unset), is the BLOCKING issue `exact_files_required`.
- **G2:** `request_grant(stage=SUBMIT)` and `pre_click_commit` refuse unconditionally with reason `submission_not_available`. There is no placeholder authorization model, and the Phase 3 human handoff flow is regression-pinned first.
- **Canonical hashing** uses `product.autonomy_contract.canonical_hash` (floats normalized to `Decimal` first). History tables are append-only (UPDATE/DELETE triggers). Never `ORDER BY created_at`.
- **Out of bounds:** no browser automation, filling, extension or submission code, and no 6C scheduling behaviour change. D3 in-app editing is out of scope.
- **Known Windows flakes** (rerun once and report): `test_latest_valid_answer_governs`, `test_list_artifact_history_newest_first`, `test_record_status_change_tracks_previous_status`, and the extension service-worker timeouts.

## Review Focus

- **Two browser tabs:** the user runs Save changes in tab B, then clicks Approve in tab A, which still shows the old binding. The approval must be refused as stale and write nothing. Pinned in Task 9 (`test_approve_from_a_stale_tab_is_refused`).
- **An ACCOUNT-reach answer edited while reviewing another application:** every application whose approval bound the superseded answer must go to `NEEDS_REVIEW`. Pinned in Task 12 (`test_editing_an_account_answer_invalidates_every_approval_that_bound_it`).
- **A cited profile claim removed or changed after approval,** with identical document bytes: the claim label becomes `unknown` (BLOCKING) or the digest changes, and approval is not effective. Pinned in Task 6 (`test_removed_evidence_blocks_and_invalidates`).
- **The TTL boundary:** at exactly `created_at + ttl` the approval is expired (`>=`), and shortening the TTL setting expires older approvals. Pinned in Task 4 (`test_ttl_boundary_is_inclusive`).
- **Bulk where one binding changes between list render and click:** only that application is refused, and the others are approved with their own records. Pinned in Task 10 (`test_bulk_refuses_only_the_changed_item`).

## File Map

| File | Responsibility | Task |
|---|---|---|
| `webapp/persistence/migrations.py` | migration `019_review_approval` | 1 |
| `webapp/persistence/review_approval.py` | append-only approvals/events/deltas/dispositions, current-state reads | 2 |
| `product/review_contract.py` | pure binding, hashes, provenance, state, effectiveness, invalidation, delta-only | 3–4 |
| `webapp/config.py` | `review_approval_ttl_days` | 4 |
| `webapp/services/review_fields.py` | the planned field set | 5 |
| `webapp/services/review_application.py` | reviewable application assembly + state snapshot | 6 |
| `webapp/services/review_documents.py` | replace/select wrappers, Save changes, newer-draft warning | 7 |
| `webapp/services/review_approval.py` | approve/revoke/expiry/invalidation recorder/deltas/bulk/presented | 8–10 |
| `webapp/services/autonomy.py` | G2 SUBMIT refusal | 11 |
| `webapp/services/review_answers.py` | answers, proposals, dispositions, acknowledgements | 12 |
| `webapp/api/review_approval.py` (new), `webapp/api/applications.py` (new), `webapp/app.py` (router registration) | routes | 13 |
| `webapp/services/docx_preview.py`, templates, `webapp/static/app.js`, `webapp/services/autonomy_inbox.py`, `webapp/services/autonomy_dossier.py` | UI, preview, 6C link, dossier Approvals | 14 |
| `webapp/services/autonomy_scheduler.py` | one additive sweep call (`reconcile_approvals`) | 8 |
| tests (per task) + `tests/webapp/test_review_approval_acceptance.py`, `tests/webapp/test_review_approval_browser.py` | validation | 15 |

---

### Task 1: Migration `019_review_approval`

**Files:**
- Modify: `webapp/persistence/migrations.py` (constants near line 36; registry near line 83; new function after `_migrate_autonomy_prepare`)
- Modify: the tests that assert the full migration-id list (`tests/webapp/persistence/test_accounts_migration.py`, `test_search_workspace_migration.py`, `test_handoff.py`) by appending the new id
- Test: `tests/webapp/persistence/test_review_approval_migration.py`

**Interfaces:**
- Produces: `REVIEW_APPROVAL_MIGRATION_ID = "019_review_approval"`, `REVIEW_APPROVAL_APPEND_ONLY_TABLES`, `REVIEW_EVENTS` and `DELTA_KINDS`. Four tables: `application_approvals`, `application_review_events`, `review_deltas`, `application_field_dispositions`.

- [ ] **Step 1: Write the failing test**

```python
# tests/webapp/persistence/test_review_approval_migration.py
from __future__ import annotations

import sqlite3

import pytest

from webapp.persistence.db import connect, init_db
from webapp.persistence.migrations import REVIEW_APPROVAL_APPEND_ONLY_TABLES, REVIEW_APPROVAL_MIGRATION_ID
from webapp.persistence.workspaces import create_workspace


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "db.sqlite3")
    c = connect(tmp_path / "db.sqlite3")
    yield c
    c.close()


def test_migration_is_recorded_and_rerun_is_a_noop(tmp_path, conn):
    ids = [r[0] for r in conn.execute("SELECT id FROM schema_migrations ORDER BY rowid")]
    assert ids[-1] == REVIEW_APPROVAL_MIGRATION_ID
    init_db(tmp_path / "db.sqlite3")
    assert [r[0] for r in conn.execute("SELECT id FROM schema_migrations ORDER BY rowid")] == ids


def _approval(conn, ws, scope="FILL"):
    conn.execute("INSERT INTO application_approvals (id, account_id, application_workspace_id, scope, binding_json, "
                 "binding_hash, actor, created_at) VALUES ('apr_1', 'acct_default', ?, ?, '{}', 'sha256:x', 'u', 't')",
                 (ws, scope))


def test_scope_can_only_be_fill(conn):
    ws = create_workspace(conn, company="A", title="B")["id"]
    with pytest.raises(sqlite3.IntegrityError):
        _approval(conn, ws, scope="SUBMIT")
    _approval(conn, ws)


@pytest.mark.parametrize("table", REVIEW_APPROVAL_APPEND_ONLY_TABLES)
def test_history_tables_are_append_only(conn, table):
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                                        (table,))]
    for action in ("update", "delete"):
        assert f"{table}_append_only_{action}" in names


def test_disposition_and_event_vocabularies_are_closed(conn):
    ws = create_workspace(conn, company="A", title="B")["id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO application_field_dispositions (id, account_id, application_workspace_id, answer_key, "
                     "disposition, actor, created_at) VALUES ('d', 'acct_default', ?, 'k', 'MAYBE', 'u', 't')", (ws,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO application_review_events (id, account_id, application_workspace_id, event, actor, "
                     "created_at) VALUES ('e', 'acct_default', ?, 'REVIEW_OPENED', 'u', 't')", (ws,))
```

- [ ] **Step 2: Run it and see it fail**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence/test_review_approval_migration.py -q`
Expected: FAIL at import (`REVIEW_APPROVAL_MIGRATION_ID` doesn't exist).

- [ ] **Step 3: Implement**

```python
# webapp/persistence/migrations.py — constants
REVIEW_APPROVAL_MIGRATION_ID = "019_review_approval"
REVIEW_APPROVAL_APPEND_ONLY_TABLES = (
    "application_approvals", "application_review_events", "review_deltas", "application_field_dispositions",
)
REVIEW_EVENTS = (
    "REVIEW_PRESENTED", "PACK_CONFIRMED", "DOCUMENT_REPLACED", "SELECTION_CHANGED", "ANSWER_EDITED",
    "PROPOSAL_ACCEPTED", "FIELD_DISPOSITION_SET", "WARNING_ACKNOWLEDGED", "APPROVED", "APPROVAL_INVALIDATED",
    "REVOKED", "EXPIRED", "DELTA_OPENED", "DELTA_RESOLVED",
)
DELTA_KINDS = (
    "NEW_QUESTION", "CHANGED_QUESTION", "NEW_UPLOAD", "DECLARATION", "TARGET_CHANGE", "TRANSFORM_FAILURE",
    "OMIT_FIELD_REQUIRED", "DOCUMENT_CONVERSION",
)

# registry: append after the 018 entry
        (REVIEW_APPROVAL_MIGRATION_ID, _migrate_review_approval, False),


def _migrate_review_approval(conn: sqlite3.Connection) -> None:
    events = ", ".join(f"'{e}'" for e in REVIEW_EVENTS)
    kinds = ", ".join(f"'{k}'" for k in DELTA_KINDS)
    conn.executescript(f"""
        CREATE TABLE application_approvals (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            scope TEXT NOT NULL CHECK (scope = 'FILL'),
            binding_json TEXT NOT NULL,
            binding_hash TEXT NOT NULL,
            supersedes_id TEXT REFERENCES application_approvals(id),
            batch_id TEXT,
            resolved_delta_ids_json TEXT NOT NULL DEFAULT '[]',
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_application_approvals_ws ON application_approvals(application_workspace_id, seq);

        CREATE TABLE application_review_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            event TEXT NOT NULL CHECK (event IN ({events})),
            binding_hash TEXT,
            detail_json TEXT NOT NULL DEFAULT '{{}}',
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_review_events_ws ON application_review_events(application_workspace_id, event, seq);

        CREATE TABLE review_deltas (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            kind TEXT NOT NULL CHECK (kind IN ({kinds})),
            answer_key TEXT,
            subject TEXT,
            required INTEGER NOT NULL CHECK (required IN (0, 1)),
            question TEXT NOT NULL,
            observed_json TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_review_deltas_ws ON review_deltas(application_workspace_id, seq);

        CREATE TABLE application_field_dispositions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            application_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            answer_key TEXT NOT NULL,
            disposition TEXT NOT NULL CHECK (disposition IN ('ANSWER', 'OMIT')),
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_field_dispositions_ws
            ON application_field_dispositions(application_workspace_id, answer_key, seq);
    """)
    for table in REVIEW_APPROVAL_APPEND_ONLY_TABLES:
        for action in ("UPDATE", "DELETE"):
            conn.execute(f"CREATE TRIGGER {table}_append_only_{action.lower()} BEFORE {action} ON {table} "
                         f"BEGIN SELECT RAISE(ABORT, '{table} is append-only audit history'); END")
```

Append `REVIEW_APPROVAL_MIGRATION_ID` to the expected id lists in the three existing migration tests. The 6B pre-6B simulation test no-ops 018. If it fails on 019 (whose tables reference only pre-6B tables, so it shouldn't), apply the same no-op and ledger a ruling.

- [ ] **Step 4: Run the tests**

Run: `.venv/Scripts/python -m pytest tests/webapp/persistence -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Full suite (a migration change), then commit**

Run the six chunks, as in 6C Task 1:
- `tests --ignore=tests/webapp`
- `tests/webapp/persistence tests/webapp/api`
- `tests/webapp/services`
- the top-level `tests/webapp/test_*.py` files, excluding browser tests
- the browser tests in two halves

Expected: all pass (1 skipped live-OpenAI).

```bash
git add webapp/persistence/migrations.py tests/webapp/persistence
git commit -m "feat(review): add migration 019 for approvals, review events, deltas and field dispositions"
```

---

### Task 2: Review persistence (`webapp/persistence/review_approval.py`)

**Files:**
- Create: `webapp/persistence/review_approval.py`
- Test: `tests/webapp/persistence/test_review_approval_persistence.py`

**Interfaces:**
- Consumes: Task 1 tables.
- Produces (none of these commit; the caller owns the transaction):
  - `insert_approval(conn, *, account_id, application_workspace_id, binding, binding_hash, supersedes_id, batch_id, resolved_delta_ids, actor, now) -> dict` (the dict has parsed `binding` and `resolved_delta_ids`)
  - `latest_approval(conn, ws) -> dict | None` (by seq)
  - `approval_revoked(conn, approval) -> bool`
  - `record_event(conn, *, account_id, application_workspace_id, event, binding_hash, detail, actor, now) -> dict`
  - `events(conn, ws) -> list[dict]` (by seq, `detail` parsed)
  - `presented_at(conn, ws, binding_hash) -> bool`
  - `acknowledged_warning_keys(conn, ws) -> frozenset[str]`
  - `insert_delta(conn, *, account_id, application_workspace_id, kind, answer_key, subject, required, question, observed, source, now) -> dict`
  - `open_deltas(conn, ws) -> list[dict]`
  - `set_disposition(conn, *, account_id, application_workspace_id, answer_key, disposition, actor, now) -> dict`
  - `current_dispositions(conn, ws) -> dict[str, str]`
  - `invalidation_recorded(conn, ws, approval_id, current_hash) -> bool`

- [ ] **Step 1: Failing tests**

```python
# tests/webapp/persistence/test_review_approval_persistence.py
from __future__ import annotations

from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401
from webapp.persistence import review_approval as ra


def _approve(conn, ws, h, supersedes=None):
    return ra.insert_approval(conn, account_id=ACCOUNT, application_workspace_id=ws, binding={"h": h},
                              binding_hash=h, supersedes_id=supersedes, batch_id=None, resolved_delta_ids=[],
                              actor="u", now=NOW)


def test_latest_approval_is_by_seq_and_revocation_is_an_event(conn):
    ws = make_workspace(conn)
    a = _approve(conn, ws, "sha256:a")
    b = _approve(conn, ws, "sha256:b", a["id"])  # identical timestamp: seq decides
    latest = ra.latest_approval(conn, ws)
    assert latest["id"] == b["id"] and latest["binding"] == {"h": "sha256:b"}
    assert not ra.approval_revoked(conn, b)
    ra.record_event(conn, account_id=ACCOUNT, application_workspace_id=ws, event="REVOKED", binding_hash="sha256:b",
                    detail={"approval_id": b["id"]}, actor="u", now=NOW)
    assert ra.approval_revoked(conn, b) and not ra.approval_revoked(conn, a)


def test_presented_and_acknowledgements_are_exact(conn):
    ws = make_workspace(conn)
    ra.record_event(conn, account_id=ACCOUNT, application_workspace_id=ws, event="REVIEW_PRESENTED",
                    binding_hash="sha256:a", detail={}, actor="u", now=NOW)
    ra.record_event(conn, account_id=ACCOUNT, application_workspace_id=ws, event="WARNING_ACKNOWLEDGED",
                    binding_hash="sha256:a", detail={"warning_key": "w1"}, actor="u", now=NOW)
    assert ra.presented_at(conn, ws, "sha256:a") and not ra.presented_at(conn, ws, "sha256:b")
    assert ra.acknowledged_warning_keys(conn, ws) == frozenset({"w1"})


def test_deltas_open_until_resolved_and_dispositions_latest_wins(conn):
    ws = make_workspace(conn)
    d = ra.insert_delta(conn, account_id=ACCOUNT, application_workspace_id=ws, kind="NEW_QUESTION",
                        answer_key="subject:notice_period", subject="notice_period", required=True,
                        question="Notice period?", observed={"label": "Notice"}, source="FILL_SESSION:s1", now=NOW)
    assert [x["id"] for x in ra.open_deltas(conn, ws)] == [d["id"]]
    ra.record_event(conn, account_id=ACCOUNT, application_workspace_id=ws, event="DELTA_RESOLVED",
                    binding_hash="sha256:c", detail={"delta_id": d["id"]}, actor="u", now=NOW)
    assert ra.open_deltas(conn, ws) == []
    for disposition in ("OMIT", "ANSWER"):
        ra.set_disposition(conn, account_id=ACCOUNT, application_workspace_id=ws, answer_key="k",
                           disposition=disposition, actor="u", now=NOW)
    assert ra.current_dispositions(conn, ws) == {"k": "ANSWER"}


def test_invalidation_recorded_is_per_approval_and_hash(conn):
    ws = make_workspace(conn)
    a = _approve(conn, ws, "sha256:a")
    ra.record_event(conn, account_id=ACCOUNT, application_workspace_id=ws, event="APPROVAL_INVALIDATED",
                    binding_hash="sha256:n", detail={"approval_id": a["id"], "current_hash": "sha256:n"}, actor="system",
                    now=NOW)
    assert ra.invalidation_recorded(conn, ws, a["id"], "sha256:n")
    assert not ra.invalidation_recorded(conn, ws, a["id"], "sha256:m")
```

- [ ] **Step 2: Run it and see it fail** (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# webapp/persistence/review_approval.py
"""Append-only review/approval history (6D-A spec §13-14). No function
commits: callers own the transaction. Current state is derived by seq."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from product.autonomy_contract import canonical_json, to_utc_iso


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _insert(conn, table: str, values: dict[str, Any]) -> dict[str, Any]:
    cols = ", ".join(values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in values)})",
                       tuple(values.values()))
    return dict(conn.execute(f"SELECT * FROM {table} WHERE seq = ?", (cur.lastrowid,)).fetchone())


def _approval(row) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["binding"] = json.loads(out["binding_json"])
    out["resolved_delta_ids"] = json.loads(out["resolved_delta_ids_json"])
    return out


def insert_approval(conn, *, account_id: str, application_workspace_id: str, binding: dict[str, Any],
                    binding_hash: str, supersedes_id: str | None, batch_id: str | None,
                    resolved_delta_ids: list[str], actor: str, now: datetime) -> dict[str, Any]:
    row = _insert(conn, "application_approvals", {
        "id": _id("apr"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "scope": "FILL", "binding_json": canonical_json(binding), "binding_hash": binding_hash,
        "supersedes_id": supersedes_id, "batch_id": batch_id,
        "resolved_delta_ids_json": json.dumps(sorted(resolved_delta_ids)), "actor": actor,
        "created_at": to_utc_iso(now)})
    return _approval(row)


def latest_approval(conn, ws: str) -> dict[str, Any] | None:
    return _approval(conn.execute("SELECT * FROM application_approvals WHERE application_workspace_id = ? "
                                  "ORDER BY seq DESC LIMIT 1", (ws,)).fetchone())


def record_event(conn, *, account_id: str, application_workspace_id: str, event: str, binding_hash: str | None,
                 detail: dict[str, Any], actor: str, now: datetime) -> dict[str, Any]:
    return _insert(conn, "application_review_events", {
        "id": _id("revt"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "event": event, "binding_hash": binding_hash, "detail_json": canonical_json(detail), "actor": actor,
        "created_at": to_utc_iso(now)})


def events(conn, ws: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM application_review_events WHERE application_workspace_id = ? ORDER BY seq",
                        (ws,)).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in rows]


def _events_of(conn, ws: str, event: str) -> list[dict[str, Any]]:
    return [e for e in events(conn, ws) if e["event"] == event]


def approval_revoked(conn, approval: dict[str, Any]) -> bool:
    return any(e["detail"].get("approval_id") == approval["id"]
               for e in _events_of(conn, approval["application_workspace_id"], "REVOKED"))


def presented_at(conn, ws: str, binding_hash: str) -> bool:
    return any(e["binding_hash"] == binding_hash for e in _events_of(conn, ws, "REVIEW_PRESENTED"))


def acknowledged_warning_keys(conn, ws: str) -> frozenset[str]:
    return frozenset(e["detail"]["warning_key"] for e in _events_of(conn, ws, "WARNING_ACKNOWLEDGED"))


def insert_delta(conn, *, account_id: str, application_workspace_id: str, kind: str, answer_key: str | None,
                 subject: str | None, required: bool, question: str, observed: dict[str, Any], source: str,
                 now: datetime) -> dict[str, Any]:
    return _insert(conn, "review_deltas", {
        "id": _id("dlt"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "kind": kind, "answer_key": answer_key, "subject": subject, "required": 1 if required else 0,
        "question": question, "observed_json": canonical_json(observed), "source": source,
        "created_at": to_utc_iso(now)})


def open_deltas(conn, ws: str) -> list[dict[str, Any]]:
    resolved = {e["detail"].get("delta_id") for e in _events_of(conn, ws, "DELTA_RESOLVED")}
    rows = conn.execute("SELECT * FROM review_deltas WHERE application_workspace_id = ? ORDER BY seq",
                        (ws,)).fetchall()
    return [{**dict(r), "observed": json.loads(r["observed_json"])} for r in rows if r["id"] not in resolved]


def set_disposition(conn, *, account_id: str, application_workspace_id: str, answer_key: str, disposition: str,
                    actor: str, now: datetime) -> dict[str, Any]:
    return _insert(conn, "application_field_dispositions", {
        "id": _id("disp"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "answer_key": answer_key, "disposition": disposition, "actor": actor, "created_at": to_utc_iso(now)})


def current_dispositions(conn, ws: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in conn.execute("SELECT answer_key, disposition FROM application_field_dispositions "
                          "WHERE application_workspace_id = ? ORDER BY seq", (ws,)).fetchall():
        out[r["answer_key"]] = r["disposition"]
    return out


def invalidation_recorded(conn, ws: str, approval_id: str, current_hash: str) -> bool:
    return any(e["detail"].get("approval_id") == approval_id and e["detail"].get("current_hash") == current_hash
               for e in _events_of(conn, ws, "APPROVAL_INVALIDATED"))
```

- [ ] **Step 4:** `.venv/Scripts/python -m pytest tests/webapp/persistence/test_review_approval_persistence.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(review): add append-only review and approval persistence`

---

### Task 3: Pure contract part 1: binding, hashes, claim provenance (`product/review_contract.py`)

**Files:**
- Create: `product/review_contract.py`
- Test: `tests/product/test_review_contract_binding.py`

**Interfaces:**
- Produces: the enums `WarningLevel`, `ProvenanceLabel`; the dataclasses `ReviewWarning`, `EvidenceRef`, `Claim`, `ReviewDocument`, `PlannedField`, `Reviewable`; and these functions:
  - `evidence_basis_hash(record) -> str`
  - `claim_content_hash(unit) -> str`
  - `claim_provenance_hash(claims) -> str | None`
  - `approval_binding(r) -> dict`
  - `binding_hash(binding) -> str`
  - `component_hashes(binding) -> dict[str, str]`
  - `invalidation_reasons(old, new) -> list[str]`
  - `delta_only(previous, current, delta_keys) -> tuple[bool, tuple[str, ...]]`
- Constants: `BINDING_SCHEMA`, `REVIEW_CONTRACT_VERSION`.

- [ ] **Step 1: Failing tests**

```python
# tests/product/test_review_contract_binding.py
from __future__ import annotations

import dataclasses

from hypothesis import given, strategies as st

from product.review_contract import (
    Claim, EvidenceRef, PlannedField, ProvenanceLabel, Reviewable, ReviewDocument, ReviewWarning, WarningLevel,
    approval_binding, binding_hash, claim_provenance_hash, component_hashes, delta_only, evidence_basis_hash,
    invalidation_reasons,
)


def claim(evidence_value="Python"):
    ev = EvidenceRef("clm_1", evidence_basis_hash({"id": "clm_1", "value": evidence_value}))
    return Claim("cv-1", "sha256:unit", ProvenanceLabel.PROFILE_EVIDENCE, (ev,))


def reviewable(**kw):
    docs = (ReviewDocument("cv", "doc_cv", "a" * 64, 100, "ai_generated", (claim(),)),
            ReviewDocument("cover_letter", "doc_cl", "b" * 64, 90, "user_uploaded", ()))
    fields = (PlannedField("subject:notice_period", "notice_period", True, "Notice period?", "ANSWER",
                           "APPROVED_ANSWER", "ans_1", "sha256:v", ("whitespace_normalize",), "1 month",
                           ProvenanceLabel.USER_SUPPLIED),
              PlannedField("subject:relocate", "relocate", False, "Relocate?", "OMIT", None, None, None, (), None,
                           None))
    warnings = (ReviewWarning("user_managed:cover_letter", WarningLevel.ATTENTION, "not verified", True),
                ReviewWarning("fit_score", WarningLevel.INFO, "fit 82", False))
    values = dict(account_id="acct", application_workspace_id="ws", job_identity_key="source:x:1",
                  identity_strength="SOURCE_RECORD", job_posting_content_id="job_1",
                  target_url="https://example.test/apply", target_provenance="discovery_verified",
                  pack_artifact_id="art_1", pack_content_hash="sha256:p", pack_schema_version="application-pack.v2",
                  documents=docs, fields=fields, warnings=warnings)
    values.update(kw)
    return Reviewable(**values)


def test_info_warnings_are_outside_the_binding_but_attention_and_blocking_are_in():
    base = approval_binding(reviewable())
    assert [w["warning_key"] for w in base["review_warnings"]] == ["user_managed:cover_letter"]
    more_info = reviewable(warnings=reviewable().warnings + (ReviewWarning("x", WarningLevel.INFO, "i", False),))
    assert binding_hash(approval_binding(more_info)) == binding_hash(base)
    blocking = ReviewWarning("answer_expired:k", WarningLevel.BLOCKING, "expired", False)
    assert binding_hash(approval_binding(reviewable(warnings=reviewable().warnings + (blocking,)))) != binding_hash(base)


def test_acknowledging_changes_the_binding():
    unacked = reviewable(warnings=(ReviewWarning("user_managed:cover_letter", WarningLevel.ATTENTION, "n", False),))
    assert binding_hash(approval_binding(unacked)) != binding_hash(approval_binding(reviewable()))


def test_evidence_content_change_with_same_ref_and_bytes_changes_the_digest():
    changed = ReviewDocument("cv", "doc_cv", "a" * 64, 100, "ai_generated", (claim("Rust"),))
    r2 = reviewable(documents=(changed,) + reviewable().documents[1:])
    assert claim_provenance_hash((claim("Rust"),)) != claim_provenance_hash((claim(),))
    assert invalidation_reasons(approval_binding(reviewable()), approval_binding(r2)) == ["document:cv"]


def test_user_managed_documents_carry_no_claim_digest():
    doc = next(d for d in approval_binding(reviewable())["documents"] if d["kind"] == "cover_letter")
    assert doc["claim_provenance_hash"] is None


def test_delta_only_exactly_when_non_delta_components_are_equal():
    base = approval_binding(reviewable())
    extra = PlannedField("subject:salary", "salary", True, "Salary?", "ANSWER", "APPROVED_ANSWER", "ans_2",
                         "sha256:s", (), "50k", ProvenanceLabel.USER_SUPPLIED)
    with_delta = approval_binding(reviewable(fields=reviewable().fields + (extra,)))
    assert delta_only(base, with_delta, {"subject:salary"}) == (True, ())
    also_target = approval_binding(reviewable(fields=reviewable().fields + (extra,), target_url="https://other.test"))
    assert delta_only(base, also_target, {"subject:salary"}) == (False, ("apply_target",))


@given(st.sampled_from(["job_identity_key", "job_posting_content_id", "target_url", "target_provenance",
                        "pack_artifact_id", "pack_content_hash"]), st.text(min_size=1, max_size=8))
def test_every_bound_scalar_changes_the_hash(field, suffix):
    base = reviewable()
    changed = dataclasses.replace(base, **{field: getattr(base, field) + suffix})
    assert binding_hash(approval_binding(changed)) != binding_hash(approval_binding(base))


def test_component_names():
    assert set(component_hashes(approval_binding(reviewable()))) == {
        "job", "apply_target", "pack", "document:cv", "document:cover_letter", "field:subject:notice_period",
        "field:subject:relocate", "review_warnings"}
```

- [ ] **Step 2: Run it and see it fail** (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# product/review_contract.py
"""Bundle 6D-A pure review contract (spec §5, §6.1, §8.3, §9.1, §11.1).
No IO, no clock; webapp is never imported."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping

from product.autonomy_contract import canonical_hash

BINDING_SCHEMA = "application-approval-binding.v1"
REVIEW_CONTRACT_VERSION = "review-contract.v1"


class WarningLevel(str, Enum):
    BLOCKING = "BLOCKING"
    ATTENTION = "ATTENTION"
    INFO = "INFO"


class ProvenanceLabel(str, Enum):
    PROFILE_EVIDENCE = "profile_evidence"
    AI_DERIVED = "ai_derived"
    USER_SUPPLIED = "user_supplied"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReviewWarning:
    key: str
    level: WarningLevel
    message: str
    acknowledged: bool = False


@dataclass(frozen=True)
class EvidenceRef:
    ref: str
    basis_hash: str


@dataclass(frozen=True)
class Claim:
    claim_id: str
    content_hash: str
    label: ProvenanceLabel
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class ReviewDocument:
    kind: str
    document_version_id: str
    sha256: str
    byte_length: int
    origin: str
    claims: tuple[Claim, ...]


@dataclass(frozen=True)
class PlannedField:
    answer_key: str
    subject: str | None
    required: bool
    question: str
    disposition: str | None  # "ANSWER" | "OMIT" | None (undecided)
    source_kind: str | None  # "EVIDENCE" | "APPROVED_ANSWER"
    source_ref: str | None
    value_hash: str | None
    permitted_transforms: tuple[str, ...]
    display_value: str | None
    provenance_label: ProvenanceLabel | None


@dataclass(frozen=True)
class Reviewable:
    account_id: str
    application_workspace_id: str
    job_identity_key: str | None
    identity_strength: str | None
    job_posting_content_id: str | None
    target_url: str | None
    target_provenance: str | None
    pack_artifact_id: str | None
    pack_content_hash: str | None
    pack_schema_version: str | None
    documents: tuple[ReviewDocument, ...]
    fields: tuple[PlannedField, ...]
    warnings: tuple[ReviewWarning, ...]


def _safe(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {k: _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


def evidence_basis_hash(record: Mapping[str, Any]) -> str:
    """The immutable content of one cited evidence item as presented."""
    return canonical_hash("profile-evidence-basis", "v1", _safe(dict(record)))


def claim_content_hash(unit: Mapping[str, Any]) -> str:
    return canonical_hash("review-claim-content", "v1", _safe(dict(unit)))


def claim_provenance_hash(claims: Iterable[Claim]) -> str | None:
    items = sorted(({"claim_id": c.claim_id, "content_hash": c.content_hash, "label": c.label.value,
                     "evidence": sorted(({"ref": e.ref, "basis_hash": e.basis_hash} for e in c.evidence),
                                        key=lambda e: e["ref"])}
                    for c in claims), key=lambda c: c["claim_id"])
    return canonical_hash("claim-provenance", "v1", items) if items else None


def approval_binding(r: Reviewable) -> dict[str, Any]:
    return {
        "schema_version": BINDING_SCHEMA,
        "account_id": r.account_id, "application_workspace_id": r.application_workspace_id,
        "job": {"identity_key": r.job_identity_key, "identity_strength": r.identity_strength,
                "job_posting_content_id": r.job_posting_content_id},
        "apply_target": {"canonical_url": r.target_url, "provenance": r.target_provenance},
        "pack": {"artifact_id": r.pack_artifact_id, "content_hash": r.pack_content_hash,
                 "schema_version": r.pack_schema_version},
        "documents": sorted(({"kind": d.kind, "document_version_id": d.document_version_id, "sha256": d.sha256,
                              "byte_length": d.byte_length, "origin": d.origin,
                              "claim_provenance_hash": claim_provenance_hash(d.claims)
                              if d.origin == "ai_generated" else None}
                             for d in r.documents), key=lambda d: d["kind"]),
        "fields": sorted(({"answer_key": f.answer_key, "subject": f.subject, "required": f.required,
                           "disposition": f.disposition, "source_kind": f.source_kind, "source_ref": f.source_ref,
                           "value_hash": f.value_hash, "permitted_transforms": sorted(f.permitted_transforms)}
                          for f in r.fields), key=lambda f: f["answer_key"]),
        "review_warnings": sorted(({"warning_key": w.key, "level": w.level.value, "acknowledged": w.acknowledged}
                                   for w in r.warnings if w.level is not WarningLevel.INFO),
                                  key=lambda w: w["warning_key"]),
        "review_contract_version": REVIEW_CONTRACT_VERSION,
    }


def binding_hash(binding: Mapping[str, Any]) -> str:
    return canonical_hash("application-approval-binding", "v1", dict(binding))


def component_hashes(binding: Mapping[str, Any]) -> dict[str, str]:
    out = {name: canonical_hash("approval-component", "v1", binding[name])
           for name in ("job", "apply_target", "pack", "review_warnings")}
    for d in binding["documents"]:
        out[f"document:{d['kind']}"] = canonical_hash("approval-component", "v1", d)
    for f in binding["fields"]:
        out[f"field:{f['answer_key']}"] = canonical_hash("approval-component", "v1", f)
    return out


def invalidation_reasons(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    a, b = component_hashes(old), component_hashes(new)
    return sorted(name for name in set(a) | set(b) if a.get(name) != b.get(name))


def delta_only(previous: Mapping[str, Any], current: Mapping[str, Any],
               delta_keys: Iterable[str]) -> tuple[bool, tuple[str, ...]]:
    allowed = {f"field:{k}" for k in delta_keys}
    other = tuple(c for c in invalidation_reasons(previous, current) if c not in allowed)
    return not other, other
```

- [ ] **Step 4:** `.venv/Scripts/python -m pytest tests/product/test_review_contract_binding.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(review): add the pure approval binding, component hashes and claim provenance digest`

---

### Task 4: Pure contract part 2: state, effectiveness, TTL (`product/review_contract.py`, `webapp/config.py`)

**Files:**
- Modify: `product/review_contract.py` (append)
- Modify: `webapp/config.py`: add `review_approval_ttl_days: int` from `JOBSEARCH_REVIEW_APPROVAL_TTL_DAYS`, default 14, clamped to `1..14` (shorter only; invalid input gives 14)
- Test: `tests/product/test_review_contract_state.py`, `tests/webapp/test_config_review.py`

**Interfaces:**
- Produces: `ReviewSnapshot`, `ReviewState` (`state`, `reasons`, `blocking`, `binding_matches`, `approval_effective`, `binding`, `binding_hash`), `blocking_issues(r)`, `unacknowledged_attention(r)`, `derive_review_state(s)`. State constants: `NOT_READY`, `READY_FOR_REVIEW`, `NEEDS_REVIEW`, `APPROVED_FOR_FILL`, `CLOSED`.

- [ ] **Step 1: Failing tests**

```python
# tests/product/test_review_contract_state.py
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from product.review_contract import (
    ReviewSnapshot, ReviewWarning, WarningLevel, approval_binding, binding_hash, derive_review_state,
)
from tests.product.test_review_contract_binding import reviewable

NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
TTL = timedelta(days=14)


def snap(r="default", approval="match", **kw):
    r = reviewable() if r == "default" else r
    latest = None
    if approval == "match":
        latest = {"binding_hash": binding_hash(approval_binding(r)), "created_at": NOW, "revoked": False}
    elif approval == "other":
        latest = {"binding_hash": "sha256:old", "created_at": NOW, "revoked": False}
    values = dict(reviewable=r, workflow_status="drafted", latest_approval=latest, open_delta_keys=(), now=NOW,
                  ttl=TTL)
    values.update(kw)
    return ReviewSnapshot(**values)


def test_effective_approval_is_approved_for_fill():
    s = derive_review_state(snap())
    assert (s.state, s.binding_matches, s.approval_effective) == ("APPROVED_FOR_FILL", True, True)


@pytest.mark.parametrize("case,reason", [
    ("blocking", "blocking_issues"), ("unacked", "unacknowledged_attention"), ("delta", "open_deltas"),
    ("revoked", "revoked"), ("expired", "expired")])
def test_binding_can_match_while_approval_is_not_effective(case, reason):
    r = reviewable()
    if case == "blocking":
        r = reviewable(warnings=r.warnings + (ReviewWarning("b", WarningLevel.BLOCKING, "x", False),))
    if case == "unacked":
        r = reviewable(warnings=(ReviewWarning("user_managed:cover_letter", WarningLevel.ATTENTION, "n", False),))
    s0 = snap(r, open_delta_keys=("subject:salary",) if case == "delta" else ())
    if case == "revoked":
        s0 = replace(s0, latest_approval={**s0.latest_approval, "revoked": True})
    if case == "expired":
        s0 = replace(s0, now=NOW + TTL)
    s = derive_review_state(s0)
    assert s.binding_matches and not s.approval_effective and s.state == "NEEDS_REVIEW" and reason in s.reasons


def test_ttl_boundary_is_inclusive():
    assert derive_review_state(snap(now=NOW + TTL - timedelta(microseconds=1))).approval_effective
    assert not derive_review_state(snap(now=NOW + TTL)).approval_effective


def test_other_states():
    assert derive_review_state(snap(approval=None)).state == "READY_FOR_REVIEW"
    assert derive_review_state(snap(approval="other")).reasons[0] == "binding_changed"
    assert derive_review_state(snap(workflow_status="applied")).state == "CLOSED"
    assert derive_review_state(snap(r=None, approval=None)).state == "NOT_READY"


def test_undecided_optional_and_unanswered_required_fields_block():
    base = reviewable()
    undecided = replace(base, fields=(replace(base.fields[1], disposition=None), base.fields[0]))
    required_blank = replace(base, fields=(replace(base.fields[0], disposition="OMIT"), base.fields[1]))
    assert "field_undecided:subject:relocate" in derive_review_state(snap(undecided, approval=None)).blocking
    assert "field_unanswered:subject:notice_period" in derive_review_state(snap(required_blank, approval=None)).blocking
```

```python
# tests/webapp/test_config_review.py
import pytest

from webapp.config import Settings


@pytest.mark.parametrize("raw,expected", [(None, 14), ("7", 7), ("30", 14), ("0", 1), ("x", 14)])
def test_review_ttl_is_shorter_only(monkeypatch, tmp_path, raw, expected):
    if raw is None:
        monkeypatch.delenv("JOBSEARCH_REVIEW_APPROVAL_TTL_DAYS", raising=False)
    else:
        monkeypatch.setenv("JOBSEARCH_REVIEW_APPROVAL_TTL_DAYS", raw)
    assert Settings(db_path=tmp_path / "d.sqlite3").review_approval_ttl_days == expected
```

- [ ] **Step 2: Run them and see them fail.**

- [ ] **Step 3: Implement**

```python
# product/review_contract.py (append)
NOT_READY, READY_FOR_REVIEW, NEEDS_REVIEW, APPROVED_FOR_FILL, CLOSED = (
    "NOT_READY", "READY_FOR_REVIEW", "NEEDS_REVIEW", "APPROVED_FOR_FILL", "CLOSED")
_OPEN_WORKFLOW = (None, "drafted")


@dataclass(frozen=True)
class ReviewSnapshot:
    reviewable: Reviewable | None
    workflow_status: str | None
    latest_approval: Mapping[str, Any] | None  # {"binding_hash", "created_at": datetime, "revoked": bool}
    open_delta_keys: tuple[str, ...]
    now: Any
    ttl: Any


@dataclass(frozen=True)
class ReviewState:
    state: str
    reasons: tuple[str, ...]
    blocking: tuple[str, ...]
    binding_matches: bool
    approval_effective: bool
    binding: dict[str, Any] | None
    binding_hash: str | None


def blocking_issues(r: Reviewable) -> tuple[str, ...]:
    issues = [w.key for w in r.warnings if w.level is WarningLevel.BLOCKING]
    for f in r.fields:
        if f.required and f.disposition != "ANSWER":
            issues.append(f"field_unanswered:{f.answer_key}")
        elif not f.required and f.disposition is None:
            issues.append(f"field_undecided:{f.answer_key}")
    return tuple(sorted(issues))


def unacknowledged_attention(r: Reviewable) -> tuple[str, ...]:
    return tuple(sorted(w.key for w in r.warnings if w.level is WarningLevel.ATTENTION and not w.acknowledged))


def derive_review_state(s: ReviewSnapshot) -> ReviewState:
    if s.workflow_status not in _OPEN_WORKFLOW:
        return ReviewState(CLOSED, ("workflow_closed",), (), False, False, None, None)
    if s.reviewable is None:
        return ReviewState(NOT_READY, ("not_ready",), (), False, False, None, None)
    binding = approval_binding(s.reviewable)
    current = binding_hash(binding)
    blocking = blocking_issues(s.reviewable)
    unacked = unacknowledged_attention(s.reviewable)
    latest = s.latest_approval
    matches = latest is not None and latest["binding_hash"] == current
    reasons: list[str] = []
    if latest is not None:
        if not matches:
            reasons.append("binding_changed")
        if latest["revoked"]:
            reasons.append("revoked")
        if s.now >= latest["created_at"] + s.ttl:
            reasons.append("expired")
    if s.open_delta_keys:
        reasons.append("open_deltas")
    if blocking:
        reasons.append("blocking_issues")
    if unacked:
        reasons.append("unacknowledged_attention")
    effective = matches and not reasons
    if effective:
        state = APPROVED_FOR_FILL
    elif latest is None and not s.open_delta_keys:
        state = READY_FOR_REVIEW
    else:
        state = NEEDS_REVIEW
    return ReviewState(state, tuple(reasons), blocking, matches, effective, binding, current)
```

```python
# webapp/config.py — field on Settings, next to the 6C autonomy settings
def _parse_review_ttl() -> int:
    try:
        value = int(os.environ.get("JOBSEARCH_REVIEW_APPROVAL_TTL_DAYS", "14"))
    except ValueError:
        return 14
    return max(1, min(14, value))

    review_approval_ttl_days: int = field(default_factory=_parse_review_ttl)
```

- [ ] **Step 4:** Run both test files. Expected: PASS.
- [ ] **Step 5: Commit** `feat(review): derive review state, binding match versus effective approval, and the approval TTL`

---

### Task 5: The planned field set (`webapp/services/review_fields.py`)

**Objective:** Derive every known field (spec §8.1, §8.4) with its disposition, source, value hash and provenance. It is pure read (no writes).

**Files:** Create `webapp/services/review_fields.py`. Create `tests/webapp/services/review_fixtures.py` (shared worlds; later tasks append). Test `tests/webapp/services/test_review_fields.py`.

**Interfaces:**
- Consumes: `product.review_contract` types; `autonomy_answers.current_approved_answers`; `review_approval.current_dispositions`, `open_deltas`; `product.fill_manifest.value_hash`; `product.representation_transforms.TRANSFORM_IDS`; `product.semantic_subject_policy.load_subject_policy`, `subject_entry`, `SENSITIVE_CLASSES`.
- Produces: `planned_fields(conn, *, account_id, application_workspace_id, profile_payload, employer_key, search_workspace_id, now) -> tuple[tuple[PlannedField, ...], tuple[ReviewWarning, ...]]`, and `CONTACT_FIELDS = ("full_name", "email", "phone", "location")`.

**Rules:**
- **Sources:**
  - Governing blockers: `application_blockers` rows for the workspace with a non-null `semantic_subject_key` and `status IN ('open', 'resolved')`. `answer_key = "subject:<semantic_subject_key>"`, `required=True`, `question` = the blocker question. The first blocker by rowid wins per subject.
  - Contact evidence: profile claims whose `field` ∈ `CONTACT_FIELDS`, excluding `placeholder` and conflicted concept ids. `answer_key = "contact:<field>"`, `required=True`, `source_kind="EVIDENCE"`, `source_ref` = claim id, `value_hash = value_hash(claim["value"])`, `disposition="ANSWER"`, label `PROFILE_EVIDENCE`.
  - Delta fields: every `review_deltas` row (open or resolved) whose `answer_key` isn't already planned. Unknown-subject deltas use `answer_key = "delta:<delta_id>"`, with the delta's `required` and `question`.
- **Candidate for a subject:**
  - The latest approved answer by precedence EMPLOYER (scope = `employer_key`) > SEARCH_WORKSPACE (scope = `search_workspace_id`) > ACCOUNT; within a reach, the latest by `seq`.
  - The label is `USER_SUPPLIED` for `USER` / `USER_EDITED_PROPOSAL`, or `PROFILE_EVIDENCE` if `basis.kind == "EVIDENCE"`.
  - `value_hash = value_hash(value)` and `source_ref` = the approved answer id.
- **Warnings:**
  - `confirmed_at + freshness_days <= now`: BLOCKING `answer_expired:<key>`;
  - expiring in under 7 days: ATTENTION `answer_expiring:<key>`;
  - a `proposed_answers` row for the subject's blocker with no approved answer newer than it: BLOCKING `proposal_unaccepted:<key>`;
  - a subject in a sensitive class whose chosen answer isn't EMPLOYER-scoped to this employer: BLOCKING `sensitive_needs_this_application:<key>`.
- **Disposition:**
  - required with a value: `"ANSWER"`;
  - required without a value: `None`;
  - optional: the stored disposition from `current_dispositions`, else `None`. A stored `ANSWER` with no value gives `None`.
- `permitted_transforms = tuple(sorted(TRANSFORM_IDS))`, and `display_value = str(value)` for answers.

**Tests (write first, see them fail, then implement):**
- `test_blocker_subjects_and_contact_fields_are_planned` (6C `prepared_chain`).
- `test_employer_answer_beats_account_answer`.
- `test_expired_answer_raises_a_blocking_warning_with_unchanged_value_hash`.
- `test_optional_field_without_disposition_is_undecided_and_omit_is_bound`.
- `test_pending_proposal_is_blocking`.
- `test_placeholder_or_conflicted_contact_claims_are_not_planned`.
- `test_unknown_subject_delta_is_planned_under_delta_key`.

Commit: `feat(review): derive the planned field set with dispositions and answer warnings`.

---

### Task 6: Reviewable application assembly (`webapp/services/review_application.py`)

**Objective:** Build the `Reviewable` and `ReviewState` from current state (spec §6, §6.1, §7.1–7.2, §8.3). Read only.

**Files:** Create `webapp/services/review_application.py`. Append a `v2_chain` fixture to `tests/webapp/services/review_fixtures.py` (the 6C `ready_chain` plus `cv_quality_v2_enabled=True`, `generate_application_documents`, both selections and the user v2 confirmation). Test `tests/webapp/services/test_review_application.py`.

**Interfaces:**
- Consumes: Tasks 2–5; `get_selection`, `get_document_version`; `get_current_artifact`; `validate_application_pack_v2`; `webapp.services.workspace_view.resolve_apply_target` (URL + provenance tier); the identity key and strength from `application_workspace_job_identities` (via `autonomy_candidates.candidate_identity` on the stored source record); `application_job_identity_conflicts`; the profile snapshot via `autonomy_prepare._profile`.
- Produces:
  - `build_reviewable(conn, *, settings, account_id, application_workspace_id, now) -> Reviewable | None`
  - `review_state(conn, *, settings, account_id, application_workspace_id, now) -> ReviewState` (builds the `ReviewSnapshot`: latest approval with `revoked` via `approval_revoked` and `created_at` parsed with `parse_utc`; open delta keys; `ttl = timedelta(days=settings.review_approval_ttl_days)`; workflow status)

**Rules:**
- `None` when there is neither a current `application_pack` nor a v2 selection.
- **Documents:**
  - When the current pack is v2 and `final_documents[kind].document_version_id` equals `get_selection(kind)` for both kinds, documents come from the pack manifests.
  - Otherwise, show the selections and add BLOCKING `save_document_changes`.
  - `settings.cv_quality_v2_enabled` false, or a v1 pack, adds BLOCKING `exact_files_required`.
- **Claims (`ai_generated` only):**
  - Units come from `generation_basis.reviewed_application_pack` (`cv_content` for `cv`, `cover_letter_content` for `cover_letter`).
  - `claim_id = unit_id`, `content_hash = claim_content_hash(unit)`, and the refs are the sorted union of the unit's and its atoms' `profile_evidence_ids`.
  - `basis_hash = evidence_basis_hash(claim record)`.
  - Label: `UNKNOWN` if any ref is absent from the current profile claims, or is a placeholder or conflicted. `PROFILE_EVIDENCE` if every atom's `atom_kind == "candidate_fact"`. Otherwise `AI_DERIVED`.
  - `UNKNOWN` gives BLOCKING `unknown_source:<unit_id>`. Any `AI_DERIVED` claim gives ATTENTION `ai_derived_claims:<kind>`.
- **Other warnings:**
  - `user_uploaded`: ATTENTION `user_managed:<kind>`;
  - no apply target: BLOCKING `no_apply_target`;
  - `user_supplied` provenance: ATTENTION `target_user_supplied`;
  - an identity conflict: BLOCKING `identity_conflict`;
  - the fit score: INFO `fit_score`;
  - field warnings from Task 5;
  - the newer-draft warning from Task 7.
- `acknowledged = key in acknowledged_warning_keys(ws)`.
- Pack fields: `artifact_id`, `content_hash = canonical_hash("application-pack-content", "v1", payload)` (float-safe), and `schema_version`.

**Tests:**
- `test_v2_chain_builds_the_full_reviewable`.
- `test_selection_change_without_save_blocks_with_save_document_changes`.
- `test_v1_or_v2_disabled_blocks_with_exact_files_required`.
- `test_removed_evidence_blocks_and_invalidates` (Review Focus).
- `test_provenance_change_with_identical_bytes_invalidates`.
- `test_new_attention_warning_invalidates_immediately`.
- `test_answer_expiry_invalidates_immediately`.
- `test_policy_capability_budget_pause_and_kill_switch_do_not_change_the_hash`.

Commit: `feat(review): assemble the reviewable application with exact documents, claim provenance and warnings`.

---

### Task 7: Documents: replace, select, Save changes (`webapp/services/review_documents.py`)

**Objective:** Spec §7.2–7.4. Replacing or selecting records history. Save changes creates the immutable v2 pack. A newer AI draft only warns.

**Interfaces:**
- `replace_document(conn, *, settings, account_id, application_workspace_id, kind, filename, content, actor, now) -> dict`: `upload_application_document` + `select_application_document`, plus `DOCUMENT_REPLACED`.
- `select_document(conn, *, settings, account_id, application_workspace_id, kind, document_version_id, actor, now) -> dict`: `select_application_document` + `SELECTION_CHANGED`.
- `save_changes(conn, *, settings, account_id, application_workspace_id, actor, now) -> dict`: `confirm_application_pack(conn, ws, effective_date=now.date().isoformat(), documents_root=settings.documents_root, account_id=account_id, document_selection_revisions={kind: get_selection(...)["revision"] for kind in ("cv", "cover_letter")})`. That is the user's own v2 Gate 4. Then `PACK_CONFIRMED` with `{pack_artifact_id}` and the new binding hash from `review_state`.
- `newer_draft_warnings(conn, *, account_id, application_workspace_id) -> tuple[ReviewWarning, ...]`: ATTENTION `newer_ai_draft:<kind>` when an `ai_generated` version for the workspace has a greater `application_document_versions` rowid than the selected version. It never changes the selection. Task 6 includes these warnings.

**Rules:**
- P1/P2: nothing in 6C or the pipeline calls these functions (checked by a grep-style structural assertion in Task 13).
- Save works whether paused or halted.

**Tests:**
- `test_replace_records_history_and_blocks_until_saved`.
- `test_save_changes_creates_exactly_one_pack_for_the_current_revisions` (DB diff).
- `test_save_changes_works_while_paused_and_halted`.
- `test_pipeline_rerun_never_moves_a_selection` (the newer-draft warning appears).
- `test_use_the_new_draft_is_an_explicit_selection`.

Commit: `feat(review): add document replace/select history and Save changes that builds the exact pack`.

---

### Task 8: The approval transaction, revoke, expiry, invalidation audit (`webapp/services/review_approval.py`)

**Objective:** Spec §9.2–9.4, §13.

**Interfaces:**
- `class ReviewRefused(Exception)` with `.reason`.
- `approve(conn, *, settings, account_id, application_workspace_id, displayed_binding_hash, actor, now, batch_id=None) -> dict`, via `run_immediate`:
  1. Check ownership (`LookupError`).
  2. `review_state(...)`:
     - `CLOSED` or `NOT_READY` gives `ReviewRefused("not_approvable")`;
     - `displayed_binding_hash != state.binding_hash` gives `"stale"`;
     - `save_document_changes` or `exact_files_required` in `state.blocking` gives `"no_pack"`;
     - any other blocking issue gives `"blocking"`;
     - unacknowledged ATTENTION gives `"unacknowledged_attention"`;
     - the latest approval already has this `binding_hash` and is effective gives `"already_approved"`. This makes a same-hash double approval (including a race) write exactly one record (spec §18 criterion 16).
  3. `insert_approval(binding=state.binding, binding_hash=state.binding_hash, supersedes_id=latest id, resolved_delta_ids=[open deltas whose answer_key is a decided field])`.
  4. `record_event(APPROVED, detail={"approval_id", "batch_id"})`.
  5. One `record_event(DELTA_RESOLVED, detail={"delta_id", "approval_id"})` per resolved delta.
  6. `ap.wake(conn, queue="APPLICATION", item_id=ws, now=now)` when a 6C queue row exists.
  
  There is no pause or kill-switch check. It writes nothing else.
- `revoke(conn, *, settings, account_id, application_workspace_id, actor, now)`: `REVOKED` with `approval_id`. With no approval, `ReviewRefused("no_approval")`.
- `record_invalidation_if_needed(conn, *, settings, account_id, application_workspace_id, now) -> bool`:
  - latest approval exists, `not binding_matches`, and `not invalidation_recorded`: `APPROVAL_INVALIDATED` with `{approval_id, previous_hash, current_hash, reasons}`;
  - TTL passed and no `EXPIRED` for this approval yet: `EXPIRED`.
- `reconcile_approvals(conn, *, settings, now) -> int`: for every workspace with an approval, run `record_invalidation_if_needed` in its own short transaction. Reduce-only (events only). One additive line in `autonomy_scheduler._sweeps`: `out["review_invalidations"] = reconcile_approvals(conn, settings=settings, now=now)`.

**Tests:**
- `test_normal_approval_write_contract`: a DB diff (row counts per table before and after) shows exactly `application_approvals +1`, `application_review_events +1`, and the queue row's `next_eligible_at` changed; nothing else.
- `test_delta_reapproval_writes_one_delta_resolved_per_delta`.
- `test_approval_never_acknowledges`.
- `test_approve_while_paused_and_halted_succeeds`.
- `test_stale_blocking_and_no_pack_refusals_write_nothing`.
- `test_invalidation_recorded_once_with_component_reasons`.
- `test_revoke_and_expiry`.
- `test_second_approval_at_the_same_hash_is_already_approved_and_writes_nothing`.
- `test_reconcile_runs_in_the_6c_sweep_and_is_reduce_only`.

Commit: `feat(review): add the approval transaction with its exact write contract, revocation, expiry and invalidation audit`.

---

### Task 9: Review deltas: intake and delta-only view (`webapp/services/review_approval.py`)

**Objective:** Spec §11, §11.1.

**Interfaces:**
- `open_review_delta(conn, *, account_id, application_workspace_id, kind, answer_key, subject, required, question, observed, source, now) -> dict`: `insert_delta` + `DELTA_OPENED` in one `run_immediate`. For `kind="OMIT_FIELD_REQUIRED"`, the `answer_key` must be a field currently bound `OMIT` (else `ReviewRefused("not_an_omitted_field")`), and the delta is `required=True`.
- `review_view_mode(conn, *, settings, account_id, application_workspace_id, now) -> dict` returns `{"mode": "first_review" | "delta_only" | "full", "changed_sections": [...], "delta_keys": [...], "previous_hash": str | None}`. With no approval it's `first_review`. Otherwise `delta_only(latest.binding, state.binding, delta_keys)` decides.
- Task 5's rule makes an `OMIT_FIELD_REQUIRED` delta's field `required=True` (the delta's `required` overrides the blocker's), so `OMIT` becomes impossible for that field.

**Tests:**
- `test_delta_makes_needs_review_immediately`.
- `test_delta_only_when_only_delta_fields_differ`.
- `test_any_other_change_shows_full_changed_sections`.
- `test_omit_field_reported_mandatory_is_a_delta_and_cannot_be_omitted_again`.
- `test_approve_from_a_stale_tab_is_refused` (Review Focus): compute hash A, `save_changes` after a replacement, `approve(displayed=A)` gives `stale`, and the DB diff is empty.

Commit: `feat(review): add review delta intake and hash-proven delta-only review`.

---

### Task 10: Bulk approval and `REVIEW_PRESENTED`

**Interfaces:**
- `record_presented(conn, *, settings, account_id, application_workspace_id, actor, now) -> str`: computes the current binding hash, writes `REVIEW_PRESENTED` with it, and returns it. It is called **only** by the HTML page route (Task 14). A `NOT_READY` or `CLOSED` application records nothing and returns `None`.
- `approve_selected(conn, *, settings, account_id, items, actor, now) -> list[dict]` (`items: list[{"workspace_id", "displayed_binding_hash"}]`):
  - one `batch_id = "batch_" + uuid`;
  - each item independently: if `presented_at(ws, displayed_hash)` is false, `{"outcome": "not_presented"}`; otherwise `approve(..., batch_id=batch_id)`, catching `ReviewRefused` (`{"outcome": reason}`) and `LookupError` (`{"outcome": "not_found"}`).

**Tests:**
- `test_bulk_creates_one_record_per_application_with_one_batch_id`.
- `test_bulk_refuses_only_the_changed_item` (Review Focus).
- `test_presented_is_bound_to_the_hash`.
- `test_data_api_get_never_makes_an_item_eligible` (lands with Task 13; written here and marked to run after Task 13 by importing the router lazily).

Commit: `feat(review): add bulk approval with per-application records and presented-at-hash eligibility`.

---

### Task 11: G2: SUBMIT is unconditionally impossible (`webapp/services/autonomy.py`)

**Objective:** Spec §12 G2, D7.

**Step order (mandatory):**
1. Add `tests/webapp/test_handoff_human_flow_regression.py`, pinning the Phase 3 human flow: pairing generate/exchange → `POST /api/handoff/sessions` → events → user confirmation → a `submission_confirmations` row and `user_confirmed_submitted` status. Follow the existing `tests/webapp/test_handoff_browser_smoke.py::test_handoff_session_lifecycle_against_fixture_workspace`. Run it green on **unmodified** code and commit it on its own (`test(handoff): pin the human handoff flow before the SUBMIT guard`).
2. Write failing tests in `tests/webapp/services/test_submit_refusal.py`:
   - `request_grant(stage=SUBMIT)` with SUBMIT ceilings and policy raises `SubmissionNotAvailable` (`str(e) == "submission_not_available"`), and the row counts of `autonomy_decisions`, `autonomy_grants`, `limit_reservations`, `submission_intents` and `submission_attempts` are unchanged;
   - `pre_click_commit(grant_id=<any>)` raises the same, with the same counts unchanged;
   - a FILL `request_grant` is unaffected.
3. Implement in `webapp/services/autonomy.py`:

```python
class SubmissionNotAvailable(PermissionError):
    """Bundle 6D-A (spec §12 G2): no SUBMIT authority exists before 6E-A.
    6E-A replaces this unconditional refusal with validation of its
    one-time submission_authorizations."""


# first statement of request_grant (before the manifest checks)
    if stage == Capability.SUBMIT:
        raise SubmissionNotAvailable("submission_not_available")

# first statement of pre_click_commit
    raise SubmissionNotAvailable("submission_not_available")
```

4. Update the existing 6B tests that exercised SUBMIT grants, pre-click success, or the flows built on them (search: `stage=Capability.SUBMIT`, `pre_click_commit(`). Each becomes an assertion of the refusal, or is marked as covering behaviour 6E-A will reinstate (`pytest.mark.skip(reason="6E-A: submission authorization replaces the 6D-A hard refusal")`). Every changed test is listed in the commit body and ledgered as a ruling. Re-run the handoff regression and the 6B/6C suites.

Commit: `feat(autonomy): make SUBMIT grants and pre-click unconditionally unavailable until 6E-A`.

---

### Task 12: Answers, proposals, dispositions, acknowledgements (`webapp/services/review_answers.py`)

**Interfaces:**
- `answer_field(conn, *, settings, account_id, application_workspace_id, answer_key, value, reach, actor, now) -> dict`:
  - resolves the field's subject;
  - `approve_answer(..., provenance="USER", reach, scope_id, context, basis={"kind": "USER_ASSERTION"}, supersedes_id=<the current candidate at the same reach/scope, if any>)`;
  - sensitive subjects are forced to `EMPLOYER` reach with `scope_id = employer_key` (or `ReviewRefused("sensitive_reach")` if the subject policy forbids it);
  - records `ANSWER_EDITED` with `{answer_key, approved_answer_id}`.
- `accept_proposal(conn, *, ..., proposal_id, edited_value=None, reach, actor, now)`: gives `USER_EDITED_PROPOSAL` and `PROPOSAL_ACCEPTED`.
- `set_field_disposition(conn, *, ..., answer_key, disposition, actor, now)`: `OMIT` on a required field gives `ReviewRefused("required_field")`. Records the disposition and `FIELD_DISPOSITION_SET`.
- `acknowledge_warning(conn, *, settings, account_id, application_workspace_id, warning_key, actor, now)`: only a current ATTENTION warning (else `ReviewRefused("not_attention")`). Records `WARNING_ACKNOWLEDGED` with `{warning_key}`.

**Tests:**
- `test_answer_supersedes_and_invalidates`.
- `test_editing_an_account_answer_invalidates_every_approval_that_bound_it` (Review Focus).
- `test_omit_refused_for_required`.
- `test_acknowledge_only_attention_and_it_changes_the_binding`.
- `test_system_proposal_never_supersedes_a_user_answer`.
- `test_sensitive_subject_is_answered_for_this_application_only`.

Commit: `feat(review): add answer, proposal, disposition and acknowledgement actions`.

---

### Task 13: API routes and structural checks

**Files:** Create `webapp/api/review_approval.py` and `webapp/api/applications.py`, and register both in `webapp/app.py` like the existing routers. Test `tests/webapp/api/test_review_approval_routes.py`, plus `tests/webapp/test_review_structure.py`.

**Routes** (spec §16; `AccountScope` ownership; `ReviewRefused` gives 409 `{detail: reason}`; `LookupError` gives 404; validation gives 422; bodies are `_Body` with `extra="forbid"`):
- `GET /api/applications/prepared`: `[{workspace_id, company, title, state, reasons, blocking_count, attention_count, presented_at_current_hash}]`.
- `GET /api/workspaces/{id}/review`: `{reviewable, state, binding_hash, view_mode}`. **Read-only.**
- `GET /api/workspaces/{id}/review/documents/{kind}/preview`: `{paragraphs: [...]}` from `docx_preview`.
- `POST .../review/documents/{kind}` (multipart replace) and `POST .../review/documents/{kind}/select {document_version_id}`.
- `POST .../review/save`.
- `POST .../review/answers {answer_key, value, reach}`, `POST .../review/proposals/{proposal_id}/accept {edited_value?, reach}`, `POST .../review/fields/{answer_key}/disposition {disposition}`.
- `POST .../review/warnings/ack {warning_key}`.
- `POST .../review/approve {displayed_binding_hash}` (any extra key gives 422).
- `POST .../review/revoke`.
- `POST /api/applications/approve-selected {items: [{workspace_id, displayed_binding_hash}]}`.
- `POST /api/workspaces/{id}/review/deltas {kind, answer_key?, subject?, required, question, observed, source}`.

**Tests:**
- Ownership 404 for another account's workspace, on every route.
- `test_review_data_get_writes_nothing` (the DB diff is empty).
- `test_approve_rejects_extra_fields`.
- The 409 reason mapping.
- The bulk outcome list.
- The structural tests in `tests/webapp/test_review_structure.py`:
  - the AST of `review_*.py` modules and `webapp/api/review_approval.py` / `applications.py` has no `pre_click_commit` and no `request_grant`;
  - `product/review_contract.py` imports no `webapp`;
  - no `ORDER BY ... created_at` in the new modules;
  - no 6C or pipeline module references `select_application_document`, `save_changes` or `set_selection` (P1).

Commit: `feat(review): expose review, approval, bulk and delta routes with structural boundary checks`.

---

### Task 14: UI, preview, 6C link, dossier Approvals

**Files:**
- Create: `webapp/services/docx_preview.py`: `docx_paragraphs(data: bytes) -> list[{"style", "text"}]` via `zipfile` + `xml.etree.ElementTree` over `word/document.xml` (`w:p`, `w:pStyle`, `w:t`). Read-only, no new dependency, `ValueError` for a non-DOCX.
- Create: `webapp/templates/prepared_applications.html` and `webapp/templates/review_application.html`.
- Create: page routes in `webapp/api/review_approval.py`: `GET /applications/prepared` (HTML list with checkboxes and **Approve selected (N)**) and `GET /workspaces/{id}/review` (HTML). The latter calls `record_presented(...)` and embeds the returned hash as `data-displayed-binding-hash` on the Approve form. It is the only caller of `record_presented`.
- Modify: `webapp/templates/base.html` (nav **Prepared**) and `webapp/static/app.js` (the review page actions: fetch-POST, then reload; bulk sends each item's presented hash).
- Modify: `webapp/services/autonomy_inbox.py` + `webapp/templates/autonomy_inbox.html`. PREPARED entries read **Ready for review** and link to `/workspaces/{id}/review`. Labels and links only; no notification or scheduling logic changes.
- Modify: `webapp/services/autonomy_dossier.py` + `webapp/templates/autonomy_dossier.html`. Add an **Approvals** section with approvals (hash, batch, actor), `invalidation_reasons` between successive approvals, deltas, and review events in `seq` order.

**Review page sections:**
- job, and the target with its provenance in words;
- documents (preview, filename, size, SHA-256, origin, **Download exact file**, **Replace**, **Use the new draft** when warned);
- claims with labels and evidence links;
- fields (value / **Leave blank** / edit);
- warnings (**Acknowledge** for ATTENTION);
- the delta-only banner ("Everything else is unchanged from your previous approval (hash …)") or changed-section markers;
- **Save changes**, **Approve for filling** (copy: "Filling does not submit. Submission will ask you separately."), **Revoke approval**.

No element text or value contains "Submit".

**Tests:**
- `tests/webapp/services/test_docx_preview.py`: generated DOCX round trip; non-DOCX bytes give `ValueError`.
- `tests/webapp/api/test_review_pages.py`: the page renders all sections; `test_page_route_records_presented_and_api_does_not`; the delta-only banner only in `delta_only` mode; `"Submit" not in html`; the inbox PREPARED entry links to the review page; the dossier Approvals section lists approvals and diffs.

Commit: `feat(review): add Prepared Applications and review pages, DOCX preview, 6C Ready-for-review link and dossier Approvals`.

---

### Task 15: Final validation

**Steps:**
1. **Concurrency** (`tests/webapp/services/test_review_approval_concurrency.py`; threads with separate connections, a barrier, WAL; the file is run 20× in a row):
   - two approvals at the same hash give exactly one approval record; the other is refused `already_approved` (the Task 8 guard; the serialized transactions guarantee the second sees the first);
   - approve vs Save changes gives a stale refusal, or an approval of the earlier pack that is then not effective (never a partial state);
   - approve vs answer supersede, likewise;
   - bulk vs a single approve of the same application give exactly one record.
2. **Acceptance** (`tests/webapp/test_review_approval_acceptance.py`, real workflow, CV-v2 enabled, fake providers):
   1. 6C prepares.
   2. The page is presented.
   3. Download, then replace the CV with an edited DOCX, then Save changes.
   4. Answer the missing fields, and Leave blank an optional one.
   5. Acknowledge the ATTENTION warnings.
   6. Pause automation, then approve (it succeeds and is effective).
   7. Replace the cover letter: `NEEDS_REVIEW`, `full` mode with `document:cover_letter`.
   8. Save and re-approve.
   9. Delta intake (`NEW_QUESTION`): `delta_only` mode; answer it; re-approve (exactly one `DELTA_RESOLVED` event).
   10. Revoke, then expire (clock).
   
   Throughout: zero FILL/SUBMIT grants, intents or attempts, and SUBMIT refused.
3. **Browser** (`tests/webapp/test_review_approval_browser.py`, the pytest-playwright pattern of `tests/webapp/test_autonomy_6c_browser.py`): review page, preview, replace + Save, answer, Leave blank, acknowledge, approve, stale-tab refusal (two pages), delta-only banner, bulk from the Prepared list, and no Submit control.
4. **Migrations:** a fresh DB gets 19 migrations and a re-run is a no-op. A pre-6D DB built by `master@fc316ee` code (`git archive fc316ee webapp product` into the scratchpad) with 6C and v2 document data upgrades through 019 with rows preserved; FK and integrity checks are clean; a re-run is a no-op.
5. **Full suite** in the six chunks. Record totals.
6. **Diff boundary** against `fc316ee`: only the File Map paths, plus the test updates listed in Task 11. No fill, browser-automation, extension or submission code.
7. Commit `test(review): add 6D-A concurrency, acceptance and browser validation`. Then the single independent end-of-bundle review, and stop before the PR.

---

## Self-review

- **Spec coverage:**
  - §1 invariants: Global Constraints + Tasks 4, 8, 10, 11.
  - §5 states: Task 4.
  - §6 page: Tasks 6, 14.
  - §6.1 warnings: Tasks 3, 5, 6, 7.
  - §7 documents: Tasks 6, 7, 14.
  - §8 fields and provenance: Tasks 5, 6, 12.
  - §9 binding, approval, stale view, revoke and TTL: Tasks 3, 4, 8.
  - §10 bulk: Task 10.
  - §11 deltas: Task 9.
  - §12 G1 (Task 1 CHECK), G2 (Task 11), G3 (Task 13), G4 (the binding fields in Task 3; consumed by 6D-B), G5 (Task 14).
  - §13 audit: Tasks 2, 8, 14.
  - §14 data: Task 1.
  - §15 6B/6C: Tasks 8 (sweep line), 11, 14.
  - §16 API: Task 13.
  - §17 decisions: Global Constraints.
  - §18 criteria: 1 → T3/T6; 2 → T8; 3 → T8; 4 → T7; 5 → T6/T8/T12; 6 → T8; 7 → T9; 8 → T5/T12; 9 → T10; 10 → T7; 11 → T11/T13/T14; 12 → T4; 13 → T3/T6; 14 → T6; 15 → T8/T14; 16 → T15; 17 → T15.
  - §19 testing strategy: every task, plus Task 15.
- **Carried decisions (ledgered as rulings by the executor):**
  - contact fields are `required=True`, because spec §8.1(b) is silent on optionality and this avoids a system default for optional fields;
  - unknown-subject deltas use `answer_key = "delta:<delta_id>"`;
  - the newer-draft comparison is by document-version rowid, never by timestamp;
  - the `already_approved` refusal (Task 8) implements spec criterion 16's "the other is a no-op or 409" as a 409.
- **Names** used consistently: `approval_binding`, `binding_hash`, `component_hashes`, `claim_provenance_hash`, `evidence_basis_hash`, `claim_content_hash`, `derive_review_state`, `binding_matches`, `approval_effective`, `planned_fields`, `build_reviewable`, `review_state`, `save_changes`, `approve`, `approve_selected`, `record_presented`, `open_review_delta`, `review_view_mode`, `reconcile_approvals`, `ReviewRefused`, `SubmissionNotAvailable`.
