# Extension Session-Start + Attachment Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `MANUAL_TEST_SNAPSHOT_KEY`/`MANUAL_TEST_SESSION_ID_KEY`
DevTools bridge with a real, ordinary-user journey: confirmed Application
Pack → "Apply with extension" on the webapp → employer page opens →
user clicks the extension's toolbar icon (the required `activeTab` grant)
→ extension probes the page's fields → discovers/resumes/starts the exact
handoff session → receives only the candidate paths the detected fields
need → autofills → attaches the exact confirmed documents. Closes a
pre-existing gap against the *original* handoff design's own security
model: every handoff-session call other than start/discover switches from
the long-lived durable extension credential to a short-lived, per-session,
rotate-on-resume token.

**Architecture:** A new `handoff_session_tokens` table plus a
`last_activity_at` column on `handoff_sessions` back a new
`get_session_scope` FastAPI dependency (`webapp/api/handoff.py`, alongside
the existing `get_extension_scope`). `sendEvent`/`confirmSubmission`/the
new snapshot endpoint move to `get_session_scope`; only start/discover
keep the durable-credential `get_extension_scope`. A new
`POST /api/workspaces/{workspace_id}/handoff/sessions/{session_id}/snapshot`
endpoint projects a session's pinned pack's `candidate_snapshot` through a
closed, server-owned `normalized_field_type → candidate path` mapping —
never a bulk dump, never a client-supplied path. `workspace_detail.html`
gains an "Apply with extension" button inside the existing confirmed-pack
block, wired through a new loopback-only content script
(`extension/src/content-bridge/`) that writes a short-lived
`PendingHandoffContext` to `chrome.storage.session` (not `.local`) —
consumed only after a session is actually associated, never on mere read.
`background/index.ts`'s `runAutofillOnTab` (already `activeTab`-gated by
the existing `popup_run_autofill` toolbar-click message) is rewritten to
probe the page's adapter/fields *before* requesting any candidate data,
then discover-or-start a session, request the field-scoped projection,
inject it through the unchanged `INJECTED_SNAPSHOT_KEY` path, run
autofill, then attach the exact pinned documents. The two manual-test
storage keys are deleted; the third (`MANUAL_TEST_CLIENT_SEQUENCE_KEY`)
is kept but reworked from a single global slot into a genuinely
per-session map (keyed by `handoff_session_id`), since it backs real MV3
service-worker-restart recovery — a global slot would let two concurrent
application tabs corrupt each other's event sequencing, not just rename
test scaffolding.

**Tech Stack:** Python/FastAPI/SQLite (webapp), TypeScript/Vitest/esbuild
(extension) — matching every existing file in both trees exactly, no new
dependencies.

**Spec:** `docs/superpowers/specs/2026-09-12-extension-session-start-attachment-design.md`

## Global Constraints

- Employer-page access stays `activeTab`-only, granted exclusively by the
  user clicking the extension's own toolbar icon. **No task in this plan
  may add automatic probing/injection into an employer page the instant
  it loads** — every ATS-page interaction is downstream of the existing
  `popup_run_autofill` message, never triggered by webapp-side navigation
  alone (design spec Section 6).
- `POST /api/handoff/sessions` and `GET /api/handoff/sessions/discover`
  are the **only** two handoff routes that keep `get_extension_scope`
  (the durable credential). Every other handoff-session route
  (`sendEvent`, `confirmSubmission`, `get_replay_events`, the new
  snapshot endpoint) moves to the new `get_session_scope` (session
  token). Do not add the durable credential back to any of those once
  moved.
- Session tokens: raw value returned exactly once in the mint/rotate
  response body, never persisted — only `hash_pairing_secret`'s hash of
  it is stored (reusing the existing `sha256:<hex>` helper, not a new
  hashing scheme). `GET /sessions/discover` is metadata-only and never
  touches a token or `last_activity_at` for any session it lists —
  minting only ever happens in `POST /sessions/start` (brand-new
  session) or `POST /sessions/{id}/resume` (existing session, durable
  credential required, revokes every prior live token for that session
  in the same transaction as minting the new one). Do not mint or
  rotate a token for a session the caller has not explicitly chosen to
  start or resume.
- Session-scoped calls refresh `handoff_sessions.last_activity_at`; a
  session whose `last_activity_at` is more than
  `HANDOFF_SESSION_INACTIVITY_TIMEOUT` (2 hours, one constant, not
  user-configurable) in the past is treated as `expired` — excluded from
  discovery, and any presented token for it is rejected. No background
  sweep job; expiry is checked lazily at read time, matching the
  existing `pairing_secrets` pattern.
- The snapshot-projection endpoint's request body carries only
  `normalized_field_types` (strings from the existing adapter/classify
  vocabulary) — it is a **filter over a closed, server-owned mapping**
  (`webapp/services/handoff.py`), never an instruction for where to read
  inside `candidate_snapshot`. An unrecognized requested type is silently
  dropped, never an error. The response is always built as an explicit
  per-path projection, never the full `candidate_snapshot` object, even
  if every field happens to be requested.
- `chrome.storage.session` (not `.local`) holds `PendingHandoffContext` —
  a 5-minute TTL, consumed **only after** a session is successfully
  discovered/resumed/started using it, never merely on read. A failed
  discover/start attempt must leave the context in place for a retry.
- `MANUAL_TEST_SNAPSHOT_KEY` and `MANUAL_TEST_SESSION_ID_KEY` are deleted
  entirely (Task 12). `MANUAL_TEST_CLIENT_SEQUENCE_KEY`'s **mechanism**
  (resume `MessageRouter.clientSequence` correctly across an MV3
  service-worker restart) is preserved but reworked from the current
  single module-level `router`/single storage slot into a genuinely
  per-`handoff_session_id` map (Task 12) — the underlying restart-recovery
  algorithm is real production logic and must not regress, and it must
  not silently couple two concurrent application tabs' sequence
  namespaces together either.
- Do not change autofill classification, the safe-catalog boundary, the
  `never`/`ask`/`autofill`/`suggest` model, or any adapter rule — this
  plan touches session/context plumbing and attachment wiring only.
- No new host permissions beyond what Sub-project 1 already granted
  (`http://127.0.0.1:8420/*`). The new webapp-origin content script match
  does not widen what the extension can reach, only where a script
  auto-injects.
- A pack-artifact-pinned lookup (`get_artifact(conn, pack_artifact_id)`)
  is the **only** way any new code reads `candidate_snapshot` — never a
  fresh "current workspace pack" lookup. This is what prevents the
  silent-current-pack-drift Sub-project 1 was built to avoid.

## Two open items surfaced by implementation-level research, not present in the design spec

These are flagged per the design-review instruction to explain rather
than silently resolve — both are addressed by explicit tasks below, not
skipped.

1. **The render route the attachment fetch depends on is not actually
   session-token-gated today.** `GET
   /api/workspaces/{workspace_id}/application-pack/render/{kind}`
   (`webapp/api/review.py:152-180`) uses `Depends(get_account_scope)` —
   the ordinary single-account webapp-page dependency — and takes no
   `X-Handoff-Credential`/session-token header at all. The extension's
   existing `fetchExactPackDocument` already sends `X-Handoff-Credential`
   today, but the server silently ignores it; this only "works" because
   the system is single-account and loopback-only, so
   `get_account_scope`/`get_extension_scope` currently resolve to the
   same account regardless of which credential (if any) is presented.
   **Resolved: Task 11 does not modify this route at all.** It stays
   exactly as-is, serving ordinary browser downloads unchanged. Instead,
   a **new, dedicated, session-scoped route**,
   `GET /api/handoff/sessions/{session_id}/documents/{kind}`, is added —
   authenticated only by the session token, deriving `workspace_id` and
   the exact pinned `pack_artifact_id` from the resolved `SessionScope`
   rather than accepting either as a caller-supplied parameter. This
   keeps the ordinary webapp route's semantics untouched while making the
   exact-pack invariant structural for the extension's own path, and
   reuses the same `render_job_application_pack_document` function
   internally — no new rendering logic.
2. **A more direct URL-join path exists than the design spec assumed.**
   The design spec's Section 5.1 assumed
   `workspaces → discovery_candidates.promoted_workspace_id →
   discovery_candidates.canonical_occurrence_id →
   discovery_occurrences.source_url`. A table already exists that removes
   one hop: `application_workspace_origins`
   (`webapp/persistence/migrations.py:518-533`, columns
   `application_workspace_id, search_workspace_id, discovery_candidate_id,
   discovery_occurrence_id, discovery_run_id, promoted_at`, `UNIQUE
   (application_workspace_id, discovery_candidate_id)`). **Task 5 uses
   `workspaces.id → application_workspace_origins.application_workspace_id
   → discovery_occurrence_id → discovery_occurrences.source_url` instead**
   — same trustworthiness guarantee (still absent/`NULL` for a
   non-discovery-origin workspace), simpler query, no design-decision
   change.

---

## File Structure

```
webapp/
  persistence/
    migrations.py                      [MODIFY] 008_handoff_session_tokens, 009_handoff_session_activity
    handoff.py                         [MODIFY] new persistence functions for tokens/activity
  services/
    handoff.py                         [MODIFY] SessionScope, mint/rotate/validate, snapshot projection, expiry
  api/
    handoff.py                         [MODIFY] get_session_scope, discover (metadata-only), resume route,
                                                 route auth switches, new snapshot route, new session-scoped
                                                 document route
    views.py                           [MODIFY] workspace_detail context: apply_target_url
    review.py                          [UNCHANGED] existing render route stays exactly as-is (Task 11)
  templates/
    workspace_detail.html              [MODIFY] Apply with extension button

extension/
  manifest.json                        [MODIFY] content_scripts entry for content-bridge
  src/
    background/
      index.ts                         [MODIFY] pending-context listener, probe/discover/start orchestration,
                                                  attachment call sites, manual-key removal, sequence rekey
      server-client.ts                 [MODIFY] startSession returns sessionToken, snapshot-fetch method,
                                                  session-token headers for sendEvent/confirmSubmission
      attachment.ts                    [MODIFY] session-token header instead of durable credential
    content-bridge/
      index.ts                         [NEW] loopback-only, reads Apply-with-extension data-* attrs
    content/
      index.ts                         [MODIFY, maybe] probe-only first pass if not already separable
  scripts/
    build.mjs                          [MODIFY] bundle content-bridge entry point
  test/
    server-client.test.ts              [MODIFY] new sessionToken/snapshot-fetch tests
    attachment.test.ts                 [MODIFY] session-token header assertion
    content-bridge.test.ts             [NEW]
    pending-context.test.ts            [NEW]
    session-orchestration.test.ts      [NEW]

tests/
  webapp/
    services/test_handoff.py           [MODIFY] +tests for tokens, rotation, expiry, snapshot projection
    persistence/test_handoff.py        [MODIFY] +tests, extend rollback-simulation drop-list
    api/test_handoff_routes.py         [MODIFY] +tests for get_session_scope routes, new snapshot route
    api/test_views.py                  [MODIFY] +test for apply_target_url context
    test_extension_pairing_acceptance.py  [rename/extend, see Task 13]
```

---

## Task 1: Server — migrations for session tokens and activity expiry

**Objective:** Add the schema foundation for per-session authorization and
inactivity expiry, matching the existing `pairing_secrets`/
`handoff_sessions` migration patterns exactly, with the rollback-simulation
test extended to cover the new migration IDs.

**Files:**
- Modify: `webapp/persistence/migrations.py`
- Modify: `tests/webapp/persistence/test_handoff.py`

**Existing patterns reused:**
- `PAIRING_SECRETS_MIGRATION_ID = "007_pairing_secrets"` constant pattern
  (`migrations.py:25`) and its migration body
  (`_migrate_pairing_secrets`, lines 304-319) — the simplest template
  (one `_execute_statements` call, no backfill).
- `_migrate_handoff_sessions` (lines 217-270) — the closer template for a
  new table referencing `handoff_sessions` (creates 4 related tables in
  one block).
- `migrations` tuple registration (lines 46-54): `(migration_id,
  operation_fn, disable_foreign_keys: bool)`.
- `test_exact_004_application_documents_upgrade_to_005_handoff`
  (`tests/webapp/persistence/test_handoff.py:53-108`) — the existing
  rollback-simulation test that drops tables/`schema_migrations` rows and
  re-runs `apply_migrations`; already extended once for `007` per Task
  5/9's own history in Sub-project 1.

**New migration IDs:**
```python
HANDOFF_SESSION_TOKENS_MIGRATION_ID = "008_handoff_session_tokens"
HANDOFF_SESSION_ACTIVITY_MIGRATION_ID = "009_handoff_session_activity"
```

**New schema:**
```sql
-- 008_handoff_session_tokens
CREATE TABLE handoff_session_tokens (
    id TEXT PRIMARY KEY,
    handoff_session_id TEXT NOT NULL REFERENCES handoff_sessions(id),
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX idx_handoff_session_tokens_hash ON handoff_session_tokens(token_hash);
CREATE INDEX idx_handoff_session_tokens_session ON handoff_session_tokens(handoff_session_id);

-- 009_handoff_session_activity
ALTER TABLE handoff_sessions ADD COLUMN last_activity_at TEXT;
UPDATE handoff_sessions SET last_activity_at = started_at WHERE last_activity_at IS NULL;
```

- [ ] **Step 1: Write the failing migration test**

Add to `tests/webapp/persistence/test_handoff.py`:
```python
def test_008_handoff_session_tokens_creates_table(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO handoff_session_tokens (id, handoff_session_id, token_hash, created_at, revoked_at) "
        "VALUES ('hst_1', 'nonexistent', 'sha256:x', datetime('now'), NULL)"
    )
    # FK check happens at commit per apply_migrations' own pattern; direct
    # insert here just proves the table/columns exist with expected names.
    conn.rollback()
    conn.close()

def test_009_handoff_session_activity_backfills_from_started_at(tmp_path):
    conn = _conn(tmp_path)
    workspace_id, artifact_id = _setup_handoff_session_test(conn, "account_local", "ws_1")
    session = create_handoff_session(
        conn, account_id="account_local", workspace_id=workspace_id,
        pack_artifact_id=artifact_id, target_url="https://example.com/apply",
        target_domain="example.com", ats_adapter_id="generic", ats_adapter_version="generic@1",
    )
    row = conn.execute(
        "SELECT last_activity_at, started_at FROM handoff_sessions WHERE id = ?", (session["id"],)
    ).fetchone()
    assert row["last_activity_at"] == row["started_at"]
    conn.close()
```

Run: `python -m pytest tests/webapp/persistence/test_handoff.py -k "008 or 009" -v`
Expected: FAIL — table/column don't exist yet.

- [ ] **Step 2: Implement the migrations**

Add both migration functions and register them in the `migrations` tuple,
following `_migrate_pairing_secrets`'s exact structure (single
`_execute_statements` call per migration, `disable_foreign_keys=False`
for both since neither drops/recreates an existing table).

- [ ] **Step 3: Run to verify it passes**

Run: `python -m pytest tests/webapp/persistence/test_handoff.py -k "008 or 009" -v`
Expected: PASS (2 new tests).

- [ ] **Step 4: Extend the rollback-simulation test**

In `test_exact_004_application_documents_upgrade_to_005_handoff`, add
`HANDOFF_SESSION_TOKENS_MIGRATION_ID` and
`HANDOFF_SESSION_ACTIVITY_MIGRATION_ID` to the drop-list/`DELETE FROM
schema_migrations WHERE id IN (...)` tuple and the `DROP TABLE`
statement for `handoff_session_tokens` (the `last_activity_at` column
addition doesn't need a drop — a `ALTER TABLE ... ADD COLUMN` rollback
in this codebase's existing pattern is simulated by not re-adding it,
matching how other additive `ALTER TABLE` migrations in this file are
already handled — confirm the existing pattern for a comparable
column-add migration, e.g. `account_id` on `workspaces`, before writing
this).

- [ ] **Step 5: Run the full migration/rollback suite**

Run: `python -m pytest tests/webapp/persistence/test_handoff.py -v`
Expected: all pass, 15 existing + 2 new + rollback-test extension = 17
tests, zero regressions.

- [ ] **Step 6: Commit**

```bash
git add webapp/persistence/migrations.py tests/webapp/persistence/test_handoff.py
git commit -m "feat(webapp): add handoff_session_tokens table and last_activity_at column"
```

**STOP CONDITION:** Do not proceed to Task 2 until migrations apply
cleanly, the rollback-simulation test passes with both new IDs included,
and `PRAGMA foreign_key_check` is clean (asserted by the existing
`apply_migrations` machinery itself — no separate check needed).

---

## Task 2: Server — session-token mint/rotate/validate service layer

**Objective:** Persistence and service functions for minting a session
token, rotating it on resume, and validating/refreshing activity — no API
route changes yet (Task 3).

**Files:**
- Modify: `webapp/persistence/handoff.py`
- Modify: `webapp/services/handoff.py`
- Modify: `tests/webapp/services/test_handoff.py`

**Existing functions reused:**
- `hash_pairing_secret(secret) -> "sha256:<hex>"` (`persistence/handoff.py:15-16`)
  — reused as-is for token hashing.
- `create_handoff_session(conn, *, account_id, workspace_id,
  pack_artifact_id, target_url, target_domain, ats_adapter_id,
  ats_adapter_version, session_id=None, commit=True) -> dict`
  (`persistence/handoff.py:69-98`) — unchanged; token minting happens
  as a separate call immediately after, not folded into this function.
- `find_in_progress_handoff_sessions(conn, *, account_id, workspace_id,
  target_domain) -> list[dict]` (`persistence/handoff.py:110-123`) —
  gains the `last_activity_at` filter (Task 2 or Task 3, whichever is
  cleaner given the actual current WHERE-clause structure — decide at
  implementation time by reading the function fresh).
- `_TERMINAL_STATUSES = frozenset({"abandoned", "expired",
  "user_confirmed_submitted"})` (`persistence/handoff.py:66`) —
  `"expired"` is already a recognized value; this plan starts actually
  using it (a session whose activity has lapsed reads as expired via
  the query filter, not by writing this status value onto the row —
  simpler, and consistent with "no background sweep job").
- `AccountScope` (`webapp/services/ownership.py:21-53`, frozen dataclass:
  `account_id: str, profile_root: Path`, methods
  `require_search_workspace`, `require_job_workspace`,
  `profile_workspace_id`) — the shape to mirror for `SessionScope`.

**New persistence functions** (`webapp/persistence/handoff.py`):
```python
def create_session_token(
    conn: sqlite3.Connection, *, handoff_session_id: str, commit: bool = True,
) -> str:
    """Mints a raw token, persists only its hash, returns the raw value once."""

def revoke_session_tokens(
    conn: sqlite3.Connection, *, handoff_session_id: str, commit: bool = True,
) -> None:
    """Sets revoked_at on every currently-live token for this session."""

def get_session_token_row(
    conn: sqlite3.Connection, *, token_hash: str,
) -> sqlite3.Row | None:
    """Joins handoff_session_tokens -> handoff_sessions, filters
    revoked_at IS NULL, for get_session_scope to consume."""

def refresh_session_activity(
    conn: sqlite3.Connection, *, handoff_session_id: str, commit: bool = True,
) -> None:
    """Sets last_activity_at = now() for the given session."""
```

**New service functions** (`webapp/services/handoff.py`):
```python
HANDOFF_SESSION_INACTIVITY_TIMEOUT = timedelta(hours=2)

@dataclass(frozen=True)
class SessionScope:
    account_id: str
    handoff_session_id: str
    workspace_id: str
    pack_artifact_id: str

class HandoffSessionExpired(HandoffError):
    pass

class HandoffSessionTokenInvalid(HandoffError):
    pass

class HandoffSessionNotFound(HandoffError):
    pass

class HandoffSessionNotOwned(HandoffError):
    pass

def mint_session_token(conn, *, handoff_session_id: str) -> str: ...
def rotate_session_token(conn, *, handoff_session_id: str) -> str: ...
def resolve_session_scope(conn, *, raw_token: str) -> SessionScope: ...
```
(`resolve_session_scope` is what the API-layer `get_session_scope`
dependency in Task 3 calls — kept in the service layer so it's testable
without a FastAPI request context, matching how
`resolve_account_scope_from_extension_credential` is structured today.)

- [ ] **Step 1: Write the failing tests**

Add to `tests/webapp/services/test_handoff.py` (following the existing
`_conn`/`_setup_handoff_session_test` fixture pattern exactly):

```python
def test_mint_session_token_persists_hash_not_raw_value(tmp_path):
    conn = _conn(tmp_path)
    workspace_id, artifact_id = _setup_handoff_session_test(conn, "account_local", "ws_1")
    session = create_handoff_session(
        conn, account_id="account_local", workspace_id=workspace_id,
        pack_artifact_id=artifact_id, target_url="https://x/apply",
        target_domain="x", ats_adapter_id="generic", ats_adapter_version="generic@1",
    )
    token = mint_session_token(conn, handoff_session_id=session["id"])
    row = conn.execute(
        "SELECT token_hash FROM handoff_session_tokens WHERE handoff_session_id = ?",
        (session["id"],),
    ).fetchone()
    assert row["token_hash"] != token
    assert row["token_hash"] == hash_pairing_secret(token)
    conn.close()

def test_resolve_session_scope_succeeds_for_valid_token(tmp_path):
    # mint, then resolve_session_scope(conn, raw_token=token) returns a
    # SessionScope matching the session's account/workspace/pack_artifact_id
    ...

def test_resolve_session_scope_rejects_unrecognized_token(tmp_path):
    # raises HandoffSessionTokenInvalid
    ...

def test_resolve_session_scope_rejects_expired_session(tmp_path):
    # UPDATE handoff_sessions SET last_activity_at = <3 hours ago> WHERE id = ?
    # raises HandoffSessionExpired
    ...

def test_rotate_session_token_revokes_prior_tokens(tmp_path):
    token_1 = mint_session_token(conn, handoff_session_id=session["id"])
    token_2 = rotate_session_token(conn, handoff_session_id=session["id"])
    with pytest.raises(HandoffSessionTokenInvalid):
        resolve_session_scope(conn, raw_token=token_1)
    resolve_session_scope(conn, raw_token=token_2)  # succeeds
    ...

def test_refresh_session_activity_updates_last_activity_at(tmp_path):
    ...
```

Run: `python -m pytest tests/webapp/services/test_handoff.py -k "session_token or session_scope or rotate_session or refresh_session" -v`
Expected: FAIL — none of these functions exist yet.

- [ ] **Step 2: Implement persistence functions**

Add the four persistence functions to `webapp/persistence/handoff.py`,
matching the file's existing style (raw SQL via `conn.execute`, explicit
`commit: bool = True` parameter on write functions, matching
`create_handoff_session`'s signature convention).

- [ ] **Step 3: Implement service functions**

Add `SessionScope`, `HandoffSessionExpired`, `HandoffSessionTokenInvalid`,
`mint_session_token`, `rotate_session_token`, `resolve_session_scope`,
and `HANDOFF_SESSION_INACTIVITY_TIMEOUT` to `webapp/services/handoff.py`.
`resolve_session_scope` hashes the raw token, calls
`get_session_token_row`, raises `HandoffSessionTokenInvalid` if not
found, checks `status` (must be `in_progress`) and
`last_activity_at`+`HANDOFF_SESSION_INACTIVITY_TIMEOUT` against
`datetime.now(timezone.utc)`, raising `HandoffSessionExpired` if lapsed,
then calls `refresh_session_activity` before returning the `SessionScope`.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/webapp/services/test_handoff.py -k "session_token or session_scope or rotate_session or refresh_session" -v`
Expected: PASS (6 new tests).

- [ ] **Step 5: Run the full services test file**

Run: `python -m pytest tests/webapp/services/test_handoff.py -v`
Expected: 22 existing + 6 new = 28 tests, zero regressions.

- [ ] **Step 6: Commit**

```bash
git add webapp/persistence/handoff.py webapp/services/handoff.py tests/webapp/services/test_handoff.py
git commit -m "feat(webapp): add session-token mint/rotate/validate service layer"
```

**STOP CONDITION:** Do not proceed to Task 3 until all 6 new tests pass
and the full `test_handoff.py` (services) suite shows zero regressions.
No API routes have been touched yet — this task is service-layer only.

---

## Task 3: Server — API contract: discovery (metadata-only) + explicit resume + `get_session_scope` + route auth switches

**Objective:** Wire the Task 2 service layer into FastAPI as a clean
three-way split: `GET /sessions/discover` returns resumable-session
metadata only (no token minting, no rotation, no activity refresh);
`POST /sessions/start` mints a token for a brand-new session
(durable-credential-gated); a new `POST /sessions/{id}/resume`
(also durable-credential-gated) is the only place a token is ever
rotated, and only for the one session the caller actually chose.
`sendEvent`/`confirmSubmission`/`get_replay_events` switch from
`get_extension_scope` to `get_session_scope`.

**Decision (no longer open):** discovery never mints or rotates a token
for any candidate it returns. Rotating tokens for every discovered
session — most of which the caller will never touch — creates side
effects (invalidating whatever token that session already had) for
sessions the user never selected. A resumable session keeps its existing
live token, if any, until the caller explicitly resumes it.

**Files:**
- Modify: `webapp/api/handoff.py`
- Modify: `tests/webapp/api/test_handoff_routes.py`

**Existing routes/dependency reused:**
- `get_extension_scope` (`webapp/api/handoff.py:61-72`) — unchanged,
  kept for `post_start_session`, `get_discover_sessions`, and the new
  `post_resume_session` only.
- `post_start_session` (108-111), `get_discover_sessions` (119-131),
  `post_record_event` (134-138), `get_replay_events` (148-152),
  `post_confirm_submission` (160-164) — exact current line ranges to
  modify (re-read the file fresh before editing; these line numbers are
  as of the researched snapshot and may drift slightly with Task 1/2's
  changes to imports).

**New dependency:**
```python
def get_session_scope(
    x_handoff_session_token: str = Header(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> SessionScope:
    try:
        return resolve_session_scope(conn, raw_token=x_handoff_session_token)
    except (HandoffSessionTokenInvalid, HandoffSessionExpired) as exc:
        raise HTTPException(401, str(exc)) from exc
```

**Response body changes:**
- `post_start_session`: after `start_handoff_session` returns, call
  `mint_session_token(conn, handoff_session_id=result["id"])`, return
  `{**result, "session_token": token}` — unchanged from before this
  revision, brand-new sessions still get their first token minted here.
- `get_discover_sessions`: **returns session metadata only** (`id`,
  `workspace_id`, `target_domain`, `status`, `started_at`,
  `last_activity_at` — no `session_token` field on any returned row, and
  no `mint_session_token`/`rotate_session_token` call anywhere in this
  route). This also means `find_in_progress_handoff_sessions`'s
  `last_activity_at` filter (Task 2) is the *only* place discovery's
  expiry exclusion happens — discovery itself must not additionally call
  `refresh_session_activity`, since merely listing candidates is not
  activity.
- **New route**, `post_resume_session`:
  ```python
  @router.post("/sessions/{session_id}/resume")
  def post_resume_session(
      session_id: str,
      scope: AccountScope = Depends(get_extension_scope),
      conn: sqlite3.Connection = Depends(get_conn),
  ):
      try:
          token = resume_handoff_session(conn, scope, handoff_session_id=session_id)
      except (HandoffSessionNotFound, HandoffSessionExpired, HandoffSessionNotOwned) as exc:
          raise _translate(exc) from exc
      return {"session_token": token}
  ```
  Backing service function (add to Task 2's scope retroactively if not
  already covered there, or add here — whichever this plan's actual Task
  2 commit ends up including; note in that task's commit message if this
  function moves there instead):
  ```python
  def resume_handoff_session(conn, scope: AccountScope, *, handoff_session_id: str) -> str:
      session = get_handoff_session(conn, handoff_session_id)  # new or existing lookup, confirm at implementation time
      if session is None:
          raise HandoffSessionNotFound()
      if session["account_id"] != scope.account_id:
          raise HandoffSessionNotOwned()
      if session["status"] != "in_progress":
          raise HandoffSessionNotFound()  # terminal/expired sessions are not resumable, same external signal as "not found"
      if _is_expired(session["last_activity_at"]):
          raise HandoffSessionExpired()
      revoke_session_tokens(conn, handoff_session_id=handoff_session_id, commit=False)
      token = create_session_token(conn, handoff_session_id=handoff_session_id, commit=False)
      refresh_session_activity(conn, handoff_session_id=handoff_session_id, commit=False)
      conn.commit()
      return token
  ```
  (Ownership check, in-progress-status check, and expiry check all
  happen here — resume is where "validates ownership/status/expiry"
  actually lives, not in discovery.)

**Route auth switches:**
- `post_record_event`, `get_replay_events`, `post_confirm_submission`:
  `scope: AccountScope = Depends(get_extension_scope)` →
  `scope: SessionScope = Depends(get_session_scope)`. Path/body
  `workspace_id`/`handoff_session_id` params (wherever the route path
  carries them) must be cross-checked against `scope.workspace_id`/
  `scope.handoff_session_id` — a token for session A must 401 (or 403)
  against session B's path, even same-account.

- [ ] **Step 1: Write the failing tests**

Extend `tests/webapp/api/test_handoff_routes.py` (currently 5 tests):

```python
def test_start_session_returns_session_token(client_with_credential): ...
def test_discover_sessions_returns_metadata_without_session_token(...):
    # asserts "session_token" key is absent from every row in the
    # discover response — the regression test for "discovery must not
    # mint/rotate"
    ...
def test_resume_session_rotates_token_and_invalidates_prior_one(...): ...
def test_resume_session_rejects_wrong_account(...): ...
def test_resume_session_rejects_expired_session(...): ...
def test_resume_session_rejects_terminal_status_session(...): ...
def test_send_event_requires_session_token_not_durable_credential(...): ...
def test_send_event_rejects_durable_credential_header(...):
    # regression test for the Global Constraint: a request using
    # X-Handoff-Credential instead of X-Handoff-Session-Token must be
    # rejected (401), not silently accepted
    ...
def test_session_token_for_one_session_rejected_against_another_sessions_path(...): ...
def test_send_event_refreshes_last_activity_at(...): ...
def test_discover_does_not_refresh_last_activity_at(...):
    # regression test: listing candidates is not activity
    ...
def test_expired_session_token_rejected(...): ...
```

Run: `python -m pytest tests/webapp/api/test_handoff_routes.py -v`
Expected: FAIL on all new tests — routes still use the old dependency/shape.

- [ ] **Step 2: Implement `get_session_scope`, `post_resume_session`, `resume_handoff_session`, and wire the auth switches**

Add the dependency, the new route, its backing service function, update
the 3 route signatures for the session-token switch, and confirm
`get_discover_sessions`'s response body carries no token field.

- [ ] **Step 3: Run to verify it passes**

Run: `python -m pytest tests/webapp/api/test_handoff_routes.py -v`
Expected: PASS (5 existing + ~11 new = ~16 tests).

- [ ] **Step 4: Check for ripple effects in other route tests**

`tests/webapp/api/test_views.py` and any other file that starts a
handoff session or calls these routes as test setup (search for
`"/api/handoff/sessions"` across `tests/`) may need their fixture setup
updated to use the new response shape (`session_token` present) or
switch their own test-only event/confirm calls to the new header. Run:
`python -m pytest tests/webapp -k handoff -v` to catch this broadly
before moving on.

- [ ] **Step 5: Full webapp test suite**

Run: `python -m pytest tests/webapp -q`
Expected: zero regressions outside the files explicitly touched in this
task and Task 1/2.

- [ ] **Step 6: Commit**

```bash
git add webapp/api/handoff.py tests/webapp/api/test_handoff_routes.py
# plus any ripple-effect test files touched in Step 4
git commit -m "feat(webapp): switch handoff session traffic to session-token authorization"
```

**STOP CONDITION:** Do not proceed to Task 4 until every existing caller
of the modified routes (production and test) has been found and updated
— a broad `grep -rn "X-Handoff-Credential\|get_extension_scope" webapp/ tests/`
after this task should show it remaining only on
`post_start_session`/`get_discover_sessions` and their tests.

---

## Task 4: Server — exact-pack snapshot projection endpoint

**Objective:** The new
`POST /api/workspaces/{workspace_id}/handoff/sessions/{session_id}/snapshot`
endpoint: session-token-gated, pinned to the session's exact
`pack_artifact_id`, projecting only recognized+requested normalized field
types through a closed server-owned mapping.

**Files:**
- Modify: `webapp/services/handoff.py`
- Modify: `webapp/api/handoff.py`
- Modify: `tests/webapp/services/test_handoff.py`
- Modify: `tests/webapp/api/test_handoff_routes.py`

**Existing code reused:**
- `get_artifact(conn, pack_artifact_id)` (from
  `webapp.persistence.artifacts`, already imported in
  `webapp/services/handoff.py:116` for `start_handoff_session`) — returns
  a dict with `["payload"]` already JSON-parsed. The new endpoint reads
  `get_artifact(conn, scope.pack_artifact_id)["payload"]["candidate_snapshot"]`
  — **exact same call `start_handoff_session` already makes, no new
  artifact-loading code**.
- `_CANDIDATE_KEYS` (`application_pack_contract.py` — re-read this file
  fresh to get its exact current name/location and the 11-key list:
  `profile_schema_version, identity, contact, employment, education,
  certifications, skills, languages, projects, publications, awards`)
  — the closed mapping's *values* must be real paths within this shape.
- `get_session_scope` (Task 3).

**New closed mapping** (`webapp/services/handoff.py`):
```python
NORMALIZED_FIELD_TYPE_TO_CANDIDATE_PATH: dict[str, str] = {
    "name": "identity.name",
    "email": "contact.email",
    # ... every normalized_field_type the extension's adapters/classify
    # layer actually produces (extension/src/adapters/*.ts — grep for
    # normalizedFieldType string literals to build the exhaustive list;
    # do not guess this list, enumerate it from the real adapter source).
    "employment[0].employer": "employment.0.employer",
    "employment[0].role": "employment.0.role",
    # ... etc for however many employment-index entries the generic/
    # greenhouse adapters actually reference today.
}
```

**Step 0 (research-before-write, not a checkbox — do this first):** grep
`extension/src/adapters/*.ts` for every string literal assigned to
`normalizedFieldType` to build the real, exhaustive mapping. Do not invent
plausible-looking entries; the mapping's completeness is a correctness
property (an omitted real normalized_field_type silently degrades
autofill for that field, per the Global Constraint's "silently drop
unrecognized" rule — which is fine for adapter-version skew, but not
acceptable for a type the current adapter set genuinely uses today).

**New service function:**
```python
def project_session_snapshot(
    conn: sqlite3.Connection, *, scope: SessionScope,
    normalized_field_types: list[str],
) -> dict[str, Any]:
    artifact = get_artifact(conn, scope.pack_artifact_id)
    candidate_snapshot = artifact["payload"]["candidate_snapshot"]
    projection: dict[str, Any] = {}
    for field_type in normalized_field_types:
        path = NORMALIZED_FIELD_TYPE_TO_CANDIDATE_PATH.get(field_type)
        if path is None:
            continue  # unrecognized type, silently dropped per contract
        value = _resolve_dotted_path(candidate_snapshot, path)
        if value is not None:
            projection[field_type] = value
    return projection
```
(`_resolve_dotted_path` is new helper code — walks a dotted/indexed path
string like `"employment.0.employer"` through nested dicts/lists,
returning `None` if any segment is missing rather than raising, since a
pack that genuinely lacks that data is a normal case, not an error.)

**New route** (`webapp/api/handoff.py`):
```python
@router.post("/workspaces/{workspace_id}/handoff/sessions/{session_id}/snapshot")
def post_session_snapshot(
    workspace_id: str, session_id: str, body: SnapshotProjectionBody,
    scope: SessionScope = Depends(get_session_scope),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if scope.workspace_id != workspace_id or scope.handoff_session_id != session_id:
        raise HTTPException(403, "session token does not match this session/workspace")
    return {"snapshot": project_session_snapshot(
        conn, scope=scope, normalized_field_types=body.normalized_field_types,
    )}
```

- [ ] **Step 1: Write the failing tests**

Service-layer (`tests/webapp/services/test_handoff.py`):
```python
def test_project_session_snapshot_returns_only_requested_recognized_paths(tmp_path):
    # a pack with a full candidate_snapshot; request ["name", "email"];
    # assert result == {"name": ..., "email": ...}, nothing else present
    ...

def test_project_session_snapshot_silently_drops_unrecognized_field_type(tmp_path):
    # request ["name", "not_a_real_field_type"]; result has only "name"
    ...

def test_project_session_snapshot_pinned_to_original_pack_not_current(tmp_path):
    # start a session against pack A; confirm a NEWER pack B for the same
    # workspace; project the session's snapshot again; assert it still
    # reflects pack A's data, not pack B's — the regression test for the
    # drift this whole design avoids
    ...

def test_project_session_snapshot_never_returns_full_candidate_snapshot_object(tmp_path):
    # request every field type the mapping has; assert the result keys
    # are exactly the normalized_field_types requested, never a bare
    # "candidate_snapshot" key or the full nested object under one key
    ...
```

API-layer (`tests/webapp/api/test_handoff_routes.py`):
```python
def test_post_snapshot_requires_session_token(...): ...
def test_post_snapshot_rejects_mismatched_session_path(...): ...
def test_post_snapshot_returns_projected_fields(...): ...
```

Run: `python -m pytest tests/webapp/services/test_handoff.py tests/webapp/api/test_handoff_routes.py -k snapshot -v`
Expected: FAIL — function/route don't exist yet.

- [ ] **Step 2: Enumerate the real normalized_field_type vocabulary**

Per Step 0 above — grep the extension adapters, build the exhaustive
mapping, do not guess.

- [ ] **Step 3: Implement `project_session_snapshot`, `_resolve_dotted_path`, the route**

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/webapp/services/test_handoff.py tests/webapp/api/test_handoff_routes.py -k snapshot -v`
Expected: PASS (4 service + 3 route = 7 new tests).

- [ ] **Step 5: Full webapp suite**

Run: `python -m pytest tests/webapp -q`
Expected: zero regressions.

- [ ] **Step 6: Commit**

```bash
git add webapp/services/handoff.py webapp/api/handoff.py tests/webapp/services/test_handoff.py tests/webapp/api/test_handoff_routes.py
git commit -m "feat(webapp): add session-scoped, exact-pack-pinned snapshot projection endpoint"
```

**STOP CONDITION:** Do not proceed to Task 5 until the pinned-vs-current
pack regression test (Step 1) passes and the mapping (Step 2) is
confirmed exhaustive against the real adapter source, not approximated.

---

## Task 5: Server — trustworthy target-URL resolution + Gate-4 CTA

**Objective:** Server-side resolution of a workspace's discovery-origin
application URL (using `application_workspace_origins`, per this plan's
"open items" note — simpler than the design spec's originally assumed
join), and the "Apply with extension" button in `workspace_detail.html`,
gated on both a confirmed pack and a trustworthy URL.

**Files:**
- Modify: `webapp/api/views.py`
- Modify: `webapp/templates/workspace_detail.html`
- Modify: `tests/webapp/api/test_views.py`

**Existing code reused:**
- `application_workspace_origins` table
  (`webapp/persistence/migrations.py:518-533`) — already links a
  workspace to its discovery occurrence directly; no new migration
  needed for this table (it already exists), only a new query against
  it.
- The `workspace_detail` view function (`webapp/api/views.py` — locate
  its exact name/line range fresh; it's whatever function currently
  assembles `stages`/`document_finalization`/etc. for the template) —
  gains one more context key.
- `stages.review.artifact` (already computed, drives the existing
  confirmed-pack block) — the CTA's first eligibility condition reuses
  this, not a new check.

**New query** (wherever `workspace_detail`'s context-building helper
lives — likely `webapp/services/workspaces.py` or inline in
`views.py`, confirm at implementation time):
```python
def resolve_apply_target_url(conn, *, workspace_id: str) -> str | None:
    row = conn.execute(
        "SELECT do.source_url FROM application_workspace_origins awo "
        "JOIN discovery_occurrences do ON do.id = awo.discovery_occurrence_id "
        "WHERE awo.application_workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    url = row["source_url"] if row else None
    if url and _is_trustworthy_url(url):
        return url
    return None

def _is_trustworthy_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
```

**Template change** (`workspace_detail.html`, inside the existing
`{% if stages.review.artifact %}<div class="pack-downloads">` block):
```html
<button class="button apply-with-extension"
        data-workspace-id="{{ workspace.id }}"
        data-pack-artifact-id="{{ stages.review.artifact.id }}"
        data-target-url="{{ apply_target_url }}"
        {% if not apply_target_url %}disabled title="No application link found for this job"{% endif %}>
  Apply with extension
</button>
```

- [ ] **Step 1: Write the failing tests**

`tests/webapp/services/test_handoff.py` or a new/existing workspaces
test file (confirm the right home at implementation time):
```python
def test_resolve_apply_target_url_returns_url_for_discovery_origin_workspace(tmp_path): ...
def test_resolve_apply_target_url_returns_none_for_non_discovery_workspace(tmp_path): ...
def test_resolve_apply_target_url_returns_none_for_null_source_url(tmp_path): ...
def test_resolve_apply_target_url_rejects_untrustworthy_scheme(tmp_path):
    # e.g. a javascript: or file: URL somehow present — must return None
    ...
```

`tests/webapp/api/test_views.py` (currently 18 tests):
```python
def test_workspace_detail_context_includes_apply_target_url_when_available(...): ...
def test_workspace_detail_apply_button_disabled_when_no_target_url(...): ...
def test_workspace_detail_apply_button_bound_to_confirmed_pack_artifact_id(...): ...
```

Run: `python -m pytest tests/webapp -k "apply_target_url or apply_with_extension" -v`
Expected: FAIL.

- [ ] **Step 2: Implement `resolve_apply_target_url`/`_is_trustworthy_url`, wire into `workspace_detail`'s context, add the template button**

- [ ] **Step 3: Run to verify it passes**

Run: `python -m pytest tests/webapp -k "apply_target_url or apply_with_extension" -v`
Expected: PASS (~7 new tests).

- [ ] **Step 4: Full webapp suite**

Run: `python -m pytest tests/webapp -q`
Expected: 18 existing (`test_views.py`) + ~3 new there, plus wherever
`resolve_apply_target_url`'s own tests land, zero regressions elsewhere.

- [ ] **Step 5: Commit**

```bash
git add webapp/api/views.py webapp/templates/workspace_detail.html tests/webapp/api/test_views.py
# plus wherever resolve_apply_target_url's own test file lands
git commit -m "feat(webapp): resolve trustworthy application URL and add Apply with extension CTA"
```

**STOP CONDITION:** Do not proceed to Task 6 until the disabled-vs-enabled
CTA states are both covered by a passing test, and the button is
confirmed (by test, not inspection alone) to sit inside the same
`{% if stages.review.artifact %}` block as the existing download links —
never rendered when there's no confirmed pack, regardless of URL
availability.

---

## Task 6: Extension — loopback webapp→extension pending-context bridge

**Objective:** A new, separate content-script entry point, scoped only
to `http://127.0.0.1:8420/*`, that reads the Apply-with-extension
button's `data-*` attributes on click and relays them to the background
worker — without calling `preventDefault()`, so the ordinary navigation
to the employer page proceeds.

**Files:**
- Create: `extension/src/content-bridge/index.ts`
- Create: `extension/test/content-bridge.test.ts`
- Modify: `extension/manifest.json`

**Existing conventions reused:**
- The isolated-`chrome.*`-calls-behind-a-function pattern established by
  `snapshot-source.ts`/`message-router.ts` (Sub-project 1's Global
  Constraints) — this new file follows the same shape: a pure function
  that takes a `document`/element and a `sendMessage`-shaped callback,
  wired to real `chrome.*` only at the bottom of the file.
- `extension/src/content/index.ts` exists as the *employer-page* content
  script (unrelated, different `matches`) — this is a **new, separate**
  entry point, not a modification of that file.

**New manifest entry:**
```json
"content_scripts": [
  {
    "matches": ["http://127.0.0.1:8420/*"],
    "js": ["content-bridge/index.js"],
    "run_at": "document_idle"
  }
]
```

**New file** (`extension/src/content-bridge/index.ts`):
```typescript
export interface PendingContextMessage {
  type: "set_pending_handoff_context";
  workspaceId: string;
  packArtifactId: string;
  targetUrl: string;
  requestedAt: number;
}

export function buildPendingContextMessage(
  button: { dataset: { workspaceId?: string; packArtifactId?: string; targetUrl?: string } },
): PendingContextMessage | null {
  const { workspaceId, packArtifactId, targetUrl } = button.dataset;
  if (!workspaceId || !packArtifactId || !targetUrl) return null;
  return { type: "set_pending_handoff_context", workspaceId, packArtifactId, targetUrl, requestedAt: Date.now() };
}

function wireUp(): void {
  document.querySelectorAll<HTMLButtonElement>(".apply-with-extension").forEach((button) => {
    button.addEventListener("click", () => {
      const message = buildPendingContextMessage(button);
      if (message) chrome.runtime.sendMessage(message);
      // deliberately no preventDefault() / no return false — the button's
      // own click-to-navigate behavior (or surrounding <a>, confirm exact
      // shape against the real Task 5 markup) proceeds unmodified.
    });
  });
}

wireUp();
```

- [ ] **Step 1: Write the failing test**

```typescript
// extension/test/content-bridge.test.ts
import { describe, expect, it } from "vitest";
import { buildPendingContextMessage } from "../src/content-bridge/index";

describe("buildPendingContextMessage", () => {
  it("builds a message from a button's data attributes", () => {
    const button = { dataset: { workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: "https://x/apply" } };
    const message = buildPendingContextMessage(button);
    expect(message).toMatchObject({
      type: "set_pending_handoff_context", workspaceId: "ws_1",
      packArtifactId: "art_1", targetUrl: "https://x/apply",
    });
    expect(typeof message?.requestedAt).toBe("number");
  });

  it("returns null when a required data attribute is missing", () => {
    expect(buildPendingContextMessage({ dataset: { workspaceId: "ws_1" } })).toBeNull();
  });
});
```

Run: `cd extension && npx vitest run test/content-bridge.test.ts`
Expected: FAIL — module doesn't exist.

- [ ] **Step 2: Implement**

- [ ] **Step 3: Run to verify it passes**

Run: `cd extension && npx vitest run test/content-bridge.test.ts`
Expected: PASS (2 tests).

- [ ] **Step 4: Manifest change + full suite**

Run: `cd extension && npx vitest run`
Expected: 90 existing + 2 new = 92, zero regressions. (The
`wireUp()`/DOM-wiring bottom of the file stays untested glue, per
established precedent — only `buildPendingContextMessage` is unit-tested.)

- [ ] **Step 5: Commit**

```bash
git add extension/src/content-bridge/index.ts extension/test/content-bridge.test.ts extension/manifest.json
git commit -m "feat(extension): add loopback content-bridge for Apply-with-extension context"
```

**STOP CONDITION:** Do not proceed to Task 7 until the manifest's
`content_scripts` entry is confirmed to add no `host_permissions` beyond
what Sub-project 1 already granted (diff `manifest.json`'s
`host_permissions` array — it must be unchanged).

---

## Task 7: Extension — `chrome.storage.session` pending-context store

**Objective:** Background-worker listener for the Task 6 message, storing
`PendingHandoffContext` in `chrome.storage.session` with the 5-minute TTL
and consume-on-success semantics — no orchestration logic yet (Task 9),
just the store itself.

**Files:**
- Modify: `extension/src/background/index.ts`
- Create: `extension/test/pending-context.test.ts`

**Existing conventions reused:**
- `CredentialStore`'s pattern (`credential-store.ts`) — a small class
  wrapping `chrome.storage.*` get/set/clear — but using
  `chrome.storage.session` instead of `.local`, and with TTL logic this
  class doesn't have, so this is a **new, separate class**
  (`PendingContextStore`), not a modification of `CredentialStore`.

**New code** (new file `extension/src/background/pending-context-store.ts`,
or inline in `index.ts` if that better matches the file's existing
untested-glue-vs-pure-logic split — decide by reading `index.ts` fresh;
likely a separate file given `CredentialStore` gets one):
```typescript
export interface PendingHandoffContext {
  workspaceId: string;
  packArtifactId: string;
  targetUrl: string;
  requestedAt: number;
}

const PENDING_CONTEXT_KEY = "handoff_pending_context";
const PENDING_CONTEXT_TTL_MS = 5 * 60 * 1000;

export class PendingContextStore {
  async set(context: PendingHandoffContext): Promise<void> {
    await chrome.storage.session.set({ [PENDING_CONTEXT_KEY]: context });
  }

  async peek(now: () => number = Date.now): Promise<PendingHandoffContext | null> {
    const result = await chrome.storage.session.get(PENDING_CONTEXT_KEY);
    const context = result[PENDING_CONTEXT_KEY] as PendingHandoffContext | undefined;
    if (!context) return null;
    if (now() - context.requestedAt > PENDING_CONTEXT_TTL_MS) {
      await this.clear();
      return null;
    }
    return context;
  }

  async clear(): Promise<void> {
    await chrome.storage.session.remove(PENDING_CONTEXT_KEY);
  }
}
```
(`peek`, not `get`/`consume` — deliberately does not clear on a
successful read, matching the Global Constraint's consume-on-**success**
rule; Task 9's orchestration code calls `.clear()` explicitly only after
a session is associated.)

`background/index.ts` gains a `set_pending_handoff_context` message
branch (checked before the existing `popup_run_autofill` branch — same
shape-guard style already used for that branch) that calls
`pendingContextStore.set(...)`.

- [ ] **Step 1: Write the failing tests**

```typescript
// extension/test/pending-context.test.ts
import { describe, expect, it, vi } from "vitest";
import { PendingContextStore } from "../src/background/pending-context-store";

describe("PendingContextStore", () => {
  it("stores and returns a context via peek", async () => {
    const store = new PendingContextStore();
    const setMock = vi.fn().mockResolvedValue(undefined);
    const getMock = vi.fn();
    vi.stubGlobal("chrome", { storage: { session: { set: setMock, get: getMock, remove: vi.fn() } } });
    const context = { workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: "https://x", requestedAt: Date.now() };
    getMock.mockResolvedValue({ handoff_pending_context: context });
    await store.set(context);
    expect(setMock).toHaveBeenCalledWith({ handoff_pending_context: context });
    const peeked = await store.peek();
    expect(peeked).toEqual(context);
    vi.unstubAllGlobals();
  });

  it("treats an expired context as absent and clears it", async () => {
    const removeMock = vi.fn().mockResolvedValue(undefined);
    const old = { workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: "https://x", requestedAt: Date.now() - 6 * 60 * 1000 };
    vi.stubGlobal("chrome", { storage: { session: {
      get: vi.fn().mockResolvedValue({ handoff_pending_context: old }), remove: removeMock, set: vi.fn(),
    } } });
    const store = new PendingContextStore();
    const peeked = await store.peek();
    expect(peeked).toBeNull();
    expect(removeMock).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("does not clear on peek when the context is still valid", async () => {
    // asserts remove() is NOT called for a fresh context — the
    // consume-on-success invariant this whole task exists to protect
    ...
  });
});
```

Run: `cd extension && npx vitest run test/pending-context.test.ts`
Expected: FAIL — class doesn't exist.

- [ ] **Step 2: Implement `PendingContextStore` and wire the message branch into `background/index.ts`**

- [ ] **Step 3: Run to verify it passes**

Run: `cd extension && npx vitest run test/pending-context.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 4: Full extension suite**

Run: `cd extension && npx vitest run`
Expected: 92 (Task 6) + 3 = 95, zero regressions.

- [ ] **Step 5: Commit**

```bash
git add extension/src/background/pending-context-store.ts extension/src/background/index.ts extension/test/pending-context.test.ts
git commit -m "feat(extension): add chrome.storage.session pending-context store"
```

**STOP CONDITION:** Do not proceed to Task 8 until the "does not clear on
peek" test explicitly passes — this is the test that would catch a
future regression back to consume-on-read.

---

## Task 8: Extension — ATS probe (adapter/field discovery, no candidate data)

**Objective:** Introduce a new, snapshot-free pure probe function that
performs `detect → scan → classify` and returns the selected adapter id/
version plus the set of `normalizedFieldType`s needed — with no DOM
writes and no handoff events emitted. Reuses existing adapter code
unchanged; does not rewrite `runContentScript`.

**Decision (no longer open):** confirmed by reading the current code —
`extension/src/content/index.ts` only ever calls `runContentScript` when
`readInjectedSnapshot(...)` already returned a non-null snapshot; there
is no branch that runs detect/scan/classify without one. Inside
`runContentScript` (`extension/src/content/content-script.ts:100-206`),
`adapters.find((a) => a.detect(document))` and `adapter.scan(document)`
and per-field `adapter.classify(field)` all run *before* any
`adapter.map(field, snapshot)` call — but the same pass also fires
`sendMessage` events (`field_detected`, `value_inserted`, etc.) and
performs DOM writes for `autofill`/`suggest` fields, as a side effect of
that identical loop. **This means the existing function cannot be
reused as-is for a snapshot-free probe** — a genuinely new function is
needed that stops at `classify()`, emits nothing, writes nothing, and
returns the classification results for the caller (Task 9) to turn into
a `normalized_field_types` request list.

**Files:**
- Create: `extension/src/content/probe.ts`
- Create: `extension/test/probe.test.ts`
- Modify: `extension/src/content/index.ts` — gains a second code path:
  if no snapshot is injected yet, run the new probe and report its
  result via `chrome.runtime.sendMessage` instead of the existing
  `console.warn`-and-skip branch.

**Existing code reused, unchanged:**
- `Adapter.detect(document): boolean`, `Adapter.scan(document):
  DetectedField[]`, `Adapter.classify(field): FieldDecision`
  (`extension/src/adapters/types.ts`) — already implemented per-adapter
  (generic/greenhouse/lever). This task adds **zero** new adapter logic,
  only a new caller of these three existing methods.
- The `adapters` array ordering already established in
  `extension/src/content/index.ts` (greenhouse/lever before the
  always-true generic fallback) — the probe uses the same array/order,
  not a new one.

**New file** (`extension/src/content/probe.ts`):
```typescript
import type { Adapter } from "../adapters/types";

export interface ProbeResult {
  atsAdapterId: string;
  atsAdapterVersion: string;
  normalizedFieldTypes: string[];
}

export function probePage(document: Document, adapters: Adapter[]): ProbeResult | null {
  const adapter = adapters.find((a) => a.detect(document));
  if (!adapter) return null;

  const fields = adapter.scan(document);
  const normalizedFieldTypes = new Set<string>();
  for (const field of fields) {
    const decision = adapter.classify(field);
    if (decision.behavior === "autofill" || decision.behavior === "suggest") {
      normalizedFieldTypes.add(decision.normalizedFieldType);
    }
    // ask/never fields are never worth requesting candidate data for —
    // matches design spec Section 6.2 step 7's exclusion exactly.
  }
  return {
    atsAdapterId: adapter.id,
    atsAdapterVersion: adapter.version,
    normalizedFieldTypes: [...normalizedFieldTypes],
  };
}
```
(No DOM writes, no `sendMessage` calls inside this function — it is
pure, taking `document`/`adapters` and returning a plain result, matching
this codebase's established pure-logic-behind-a-function convention.)

- [ ] **Step 1: Write the failing test**

```typescript
// extension/test/probe.test.ts — mirrors content-script.test.ts's
// existing JSDOM fixture-building pattern (confirm exact fixture-setup
// helper it uses and reuse it, rather than inventing a new one)
import { describe, expect, it } from "vitest";
import { probePage } from "../src/content/probe";
import { genericAdapter } from "../src/adapters";

describe("probePage", () => {
  it("detects the matching adapter and returns needed field types without writing to the DOM", () => {
    // build a fixture document matching generic_fixture.html's shape
    // (reuse tests/webapp/fixtures/handoff/generic_fixture.html's real
    // structure via JSDOM, matching content-script.test.ts's existing
    // fixture convention)
    const result = probePage(fixtureDocument, [genericAdapter]);
    expect(result?.atsAdapterId).toBe("generic");
    expect(result?.normalizedFieldTypes).toContain("name");
    expect(result?.normalizedFieldTypes).toContain("email");
    // assert no field value was written anywhere in fixtureDocument
  });

  it("returns null when no adapter detects the page", () => {
    const result = probePage(emptyDocument, [genericAdapter]);
    expect(result).toBeNull();
  });

  it("excludes ask/never fields from the returned normalizedFieldTypes", () => {
    // uses generic_fixture.html's certification checkbox (behavior
    // "never" per Sub-project 1's own existing test coverage) — assert
    // its normalizedFieldType is absent from the result
  });
});
```

Run: `cd extension && npx vitest run test/probe.test.ts`
Expected: FAIL — module doesn't exist.

- [ ] **Step 2: Implement `probePage`**

- [ ] **Step 3: Wire the no-snapshot-yet branch in `content/index.ts`**

Replace the existing `console.warn("[JobSearch Handoff] no candidate
snapshot available; skipping scan")` branch with a call to `probePage`
and a new `chrome.runtime.sendMessage` reporting its result (new message
type, e.g. `{type: "probe_result", ...ProbeResult}`) for the background
worker (Task 9) to consume.

- [ ] **Step 4: Run to verify**

Run: `cd extension && npx vitest run`
Expected: prior count + 3, zero regressions.

- [ ] **Step 5: Commit**

```bash
git add extension/src/content/probe.ts extension/src/content/index.ts extension/test/probe.test.ts
git commit -m "feat(extension): add snapshot-free ATS probe (detect/scan/classify only)"
```

**STOP CONDITION:** Do not proceed to Task 9 until `probePage` is
confirmed by test to perform zero DOM writes and emit zero
`sendMessage` calls itself (only `content/index.ts`'s new branch sends
the probe result) — the whole point of this task is that probing must
be side-effect-free until a session and its field-scoped snapshot exist.

---

## Task 9: Extension — discover/resume/start orchestration

**Objective:** Rewrite `runAutofillOnTab` (currently gated by
`popup_run_autofill`, already `activeTab`-scoped) to read the pending
context, run the Task 8 probe, discover-or-start a session using the
Task 3/4 API surface, and hold the resulting session token — stopping
short of snapshot injection (Task 10) and attachment (Task 11) so this
task is independently testable.

**Files:**
- Modify: `extension/src/background/index.ts`
- Modify: `extension/src/background/server-client.ts`
- Modify: `extension/test/server-client.test.ts`
- Create: `extension/test/session-orchestration.test.ts`

**Existing code reused:**
- `runAutofillOnTab(tabId)` (`background/index.ts` — current body per
  Sub-project 1: reads `MANUAL_TEST_SNAPSHOT_KEY`/
  `MANUAL_TEST_SESSION_ID_KEY`, calls `ensureRouter()`, injects the
  content bundle) — **this task replaces the manual-key reads with real
  orchestration; the actual `ensureRouter()`/injection calls at the end
  are reused, only what feeds them changes.** Manual-key deletion itself
  is Task 12, not this task — Task 9 can temporarily coexist with the old
  keys still present in the file if that's cleaner, or delete them now if
  the orchestration replacement makes that natural; decide at
  implementation time and note which was chosen in the commit message.
- `ServerClient.exchangePairing` (Sub-project 1) — the pattern to match
  for new `ServerClient` methods (no `this.headers()` call when a
  different auth scheme applies; explicit header construction instead).
- `PendingContextStore.peek()`/`.clear()` (Task 7).
- The Task 8 probe output.

**`ServerClient` additions:**
```typescript
async startSession(body: {
  workspaceId: string; packArtifactId: string; targetUrl: string;
  targetDomain: string; atsAdapterId: string; atsAdapterVersion: string;
}): Promise<{ id: string; sessionToken: string }> {
  // unchanged request shape, response now includes sessionToken
}

async discoverSessions(workspaceId: string, targetDomain: string): Promise<Array<{ id: string }>> {
  // GET /api/handoff/sessions/discover — metadata only, no session_token
  // field present on any returned row (Task 3's finalized shape).
}

async resumeSession(sessionId: string): Promise<{ sessionToken: string }> {
  // POST /api/handoff/sessions/{id}/resume, durable credential — the
  // only place a resumed session's token is minted/rotated (Task 3).
}
```

**New orchestration function** (`background/index.ts` or a new
`extension/src/background/session-orchestration.ts`, matching the
pure-logic-behind-a-function convention):
```typescript
export async function associateHandoffSession(
  pendingContext: PendingHandoffContext, probe: { atsAdapterId: string; atsAdapterVersion: string },
  targetDomain: string, serverClient: ServerClient,
): Promise<{ sessionId: string; sessionToken: string }> {
  const existing = await serverClient.discoverSessions(pendingContext.workspaceId, targetDomain);
  if (existing.length > 0) {
    const { sessionToken } = await serverClient.resumeSession(existing[0].id);
    return { sessionId: existing[0].id, sessionToken };
  }
  const { id, sessionToken } = await serverClient.startSession({
    workspaceId: pendingContext.workspaceId, packArtifactId: pendingContext.packArtifactId,
    targetUrl: pendingContext.targetUrl, targetDomain,
    atsAdapterId: probe.atsAdapterId, atsAdapterVersion: probe.atsAdapterVersion,
  });
  return { sessionId: id, sessionToken };
}
```

- [ ] **Step 1: Write the failing tests**

`server-client.test.ts` additions (mocked-fetch pattern, matching
existing tests exactly):
```typescript
it("startSession returns a sessionToken alongside the id", async () => { ... });
it("discoverSessions returns resumable session ids", async () => { ... });
it("resumeSession returns a rotated sessionToken", async () => { ... });
```

`session-orchestration.test.ts` (new, constructor-injected fake
`ServerClient`, matching `pairing-form.test.ts`'s mocking style):
```typescript
it("starts a fresh session when discovery finds nothing", async () => { ... });
it("resumes and rotates the token when discovery finds an existing session", async () => { ... });
```

Run: `cd extension && npx vitest run test/server-client.test.ts test/session-orchestration.test.ts`
Expected: FAIL.

- [ ] **Step 2: Implement `ServerClient` additions and `associateHandoffSession`**

- [ ] **Step 3: Wire into `runAutofillOnTab`**

Replace the manual-key reads with: `pendingContextStore.peek()` → if
null, surface a "click Apply with extension first" message and return
→ run the Task 8 probe → call `associateHandoffSession` → on success,
`pendingContextStore.clear()` → hold `{sessionId, sessionToken}` for
Task 10/11 to consume (module-level variable or return value threaded
through, matching however this file already threads `router` state).

- [ ] **Step 4: Run to verify it passes**

Run: `cd extension && npx vitest run`
Expected: prior count + ~5 new, zero regressions.

- [ ] **Step 5: Commit**

```bash
git add extension/src/background/server-client.ts extension/src/background/index.ts \
        extension/test/server-client.test.ts extension/test/session-orchestration.test.ts
git commit -m "feat(extension): replace manual session bridge with real discover/resume/start orchestration"
```

**STOP CONDITION:** Do not proceed to Task 10 until the failed-discovery
(fresh start) and successful-discovery (resume+rotate) paths both have
passing tests, and `pendingContextStore.clear()` is confirmed (by test)
to be called only on the success path, never on a discover/start
failure.

---

## Task 10: Extension — snapshot projection fetch + injection

**Objective:** Call the Task 4 snapshot endpoint with the Task 8 probe's
detected field types, inject the projection through the existing
`INJECTED_SNAPSHOT_KEY` path, run autofill.

**Files:**
- Modify: `extension/src/background/server-client.ts`
- Modify: `extension/src/background/index.ts`
- Modify: `extension/test/server-client.test.ts`

**Existing code reused:**
- `INJECTED_SNAPSHOT_KEY` injection call
  (`background/index.ts`'s existing `chrome.scripting.executeScript`
  call that writes to `globalThis[INJECTED_SNAPSHOT_KEY]`) — unchanged
  consumer-side; only what value is written changes (the Task 4
  projection, not the old manual-test snapshot).
- `snapshot-source.ts` — unchanged, still reads the same global key.

**`ServerClient` addition:**
```typescript
async fetchSessionSnapshot(
  workspaceId: string, sessionId: string, sessionToken: string,
  normalizedFieldTypes: string[],
): Promise<Record<string, unknown>> {
  const response = await fetch(
    `${BASE_URL}/api/workspaces/${workspaceId}/handoff/sessions/${sessionId}/snapshot`,
    {
      method: "POST",
      headers: { "X-Handoff-Session-Token": sessionToken, "Content-Type": "application/json" },
      body: JSON.stringify({ normalized_field_types: normalizedFieldTypes }),
    },
  );
  if (!response.ok) throw new Error(`snapshot fetch failed: ${response.status}`);
  const result = await response.json();
  return result.snapshot;
}
```

- [ ] **Step 1: Write the failing test**

```typescript
it("fetchSessionSnapshot posts normalized field types and returns the projection", async () => {
  // mocked fetch, asserts URL/method/headers/body match, asserts
  // X-Handoff-Session-Token is sent (not X-Handoff-Credential)
});
```

Run: `cd extension && npx vitest run test/server-client.test.ts`
Expected: FAIL.

- [ ] **Step 2: Implement `fetchSessionSnapshot`, wire the call + injection into `runAutofillOnTab`** (after Task 9's `associateHandoffSession` succeeds: call `fetchSessionSnapshot` with the probe's field types, then the existing injection `executeScript` call, then the existing content-bundle injection, unchanged).

- [ ] **Step 3: Run to verify it passes**

Run: `cd extension && npx vitest run`
Expected: prior count + 1, zero regressions.

- [ ] **Step 4: Commit**

```bash
git add extension/src/background/server-client.ts extension/src/background/index.ts extension/test/server-client.test.ts
git commit -m "feat(extension): fetch and inject the session-scoped snapshot projection"
```

**STOP CONDITION:** Do not proceed to Task 11 until a full source-read
confirms `INJECTED_SNAPSHOT_KEY`'s consumer (`snapshot-source.ts`) needs
no change — the projection's shape (a flat `{normalizedFieldType: value}`
map) vs. the old manual-test snapshot's shape (whatever
`MANUAL_TEST_SNAPSHOT_KEY` held) must be compatible, or
`snapshot-source.ts` needs a corresponding update, which would be new,
unplanned scope worth flagging rather than silently absorbing here.

---

## Task 11: Extension + Server — attachment integration via a dedicated session-scoped document endpoint

**Objective:** Real call sites for the already-implemented
`fetchExactPackDocument`/`buildAttachmentEventPayload`, a **new, dedicated
session-scoped document endpoint** (not a dual-auth change to the
existing ordinary-webapp render route), and the new file-input-
interaction/outcome-classification content-script code.

**Decision (no longer open):** the existing `GET
/application-pack/render/{kind}` route (`webapp/api/review.py:152-180`)
stays completely unchanged — it keeps `Depends(get_account_scope)` only,
serving ordinary browser downloads (the Gate-4 "Download CV"/"Download
Cover Letter" links) exactly as today, with zero new auth branches added
to it. A **new route**,
`GET /api/handoff/sessions/{session_id}/documents/{kind}`, is added
instead: session-token-gated, deriving `workspace_id` and the exact
pinned `pack_artifact_id` from the resolved `SessionScope` — **never**
accepting either as a caller-supplied parameter. This is what makes the
exact-pack invariant structural rather than conventional: the request
has no field the caller could set to ask for a different workspace or
pack, because the route signature has no such parameter at all. The new
route internally calls the exact same
`render_job_application_pack_document(conn, workspace_id, kind=kind,
pack_artifact_id=pack_artifact_id, account_id=scope.account_id,
documents_root=documents_root)` function
(`webapp/api/review.py`'s existing import) the ordinary render route
already uses — confirmed by reading the current route body — so no new
rendering logic or artifact type is introduced, only a new,
narrower-surfaced caller of it.

**Files:**
- Modify: `webapp/api/handoff.py` (new route lives alongside the other
  session-scoped routes, not in `review.py`)
- Modify: `extension/src/background/attachment.ts`
- Modify: `extension/src/background/index.ts`
- Modify: `extension/test/attachment.test.ts`
- New: attachment DOM-interaction code (content-script side — exact
  file TBD at implementation time; likely
  `extension/src/content/attachment-dom.ts` or inline in the existing
  content script, matching whatever Task 8 concluded about file
  structure there)

**Existing code reused, unchanged:**
- `render_job_application_pack_document(conn, workspace_id, *, kind,
  pack_artifact_id, account_id, documents_root)` (imported and used by
  `webapp/api/review.py`'s existing render route) — the new route calls
  this identically, sourcing `workspace_id`/`pack_artifact_id` from
  `scope.workspace_id`/`scope.pack_artifact_id` (the resolved
  `SessionScope`, Task 2) rather than from request parameters, and
  `account_id` from `scope.account_id`.
- `_RENDER_KINDS = {"cv", "cover_letter"}` (`review.py`) — reused for the
  new route's own `kind` validation (import it, or duplicate the literal
  set if importing across these two modules is awkward — confirm the
  cleaner option at implementation time; duplicating a 2-item frozenset
  is a reasonable alternative to an awkward cross-module import).
- `fetchExactPackDocument(baseUrl, credential, workspaceId,
  packArtifactId, kind)` — **signature changes**: drops `workspaceId`/
  `packArtifactId` as caller-supplied parameters entirely (they're no
  longer needed — the new endpoint doesn't take them), takes
  `sessionId`/`sessionToken` instead, and calls the new URL shape. Both
  existing call sites (this function and its 2 existing tests in
  `attachment.test.ts`) must be updated together.
- `buildAttachmentEventPayload` — unchanged.

**New route** (`webapp/api/handoff.py`):
```python
@router.get("/sessions/{session_id}/documents/{kind}")
def get_session_document(
    session_id: str, kind: str,
    scope: SessionScope = Depends(get_session_scope),
    conn: sqlite3.Connection = Depends(get_conn),
    documents_root: Path = Depends(get_documents_root),
):
    if scope.handoff_session_id != session_id:
        raise HTTPException(403, "session token does not match this session")
    if kind not in _RENDER_KINDS:
        raise HTTPException(404, f"unknown rendered document kind {kind!r}")
    try:
        rendered_file = render_job_application_pack_document(
            conn, scope.workspace_id, kind=kind, pack_artifact_id=scope.pack_artifact_id,
            account_id=scope.account_id, documents_root=documents_root,
        )
    except (PipelineError, JobWorkspaceNotFound) as exc:
        raise _translate(exc) from exc
    return Response(
        content=rendered_file.content, media_type=rendered_file.mime_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(rendered_file.filename)}; filename=\"{kind}.docx\""
            ),
            "X-Content-Hash": rendered_file.content_hash,
        },
    )
```
(Mirrors the existing render route's response shape exactly — same
headers, same `Response` construction — only the auth dependency and
the source of `workspace_id`/`pack_artifact_id` differ.)

**`fetchExactPackDocument`'s new signature:**
```typescript
export async function fetchExactPackDocument(
  baseUrl: string, sessionId: string, sessionToken: string, kind: "cv" | "cover_letter",
): Promise<RenderedDocument> {
  const response = await fetch(
    `${baseUrl}/api/handoff/sessions/${sessionId}/documents/${kind}`,
    { headers: { "X-Handoff-Session-Token": sessionToken } },
  );
  if (!response.ok) throw new Error(`failed to fetch ${kind}: ${response.status}`);
  // ... unchanged parsing of the response into RenderedDocument
}
```

- [ ] **Step 1: Write the failing tests**

Server: new tests in `tests/webapp/api/test_handoff_routes.py`:
```python
def test_get_session_document_returns_rendered_cv(...): ...
def test_get_session_document_rejects_wrong_session_path(...): ...
def test_get_session_document_uses_sessions_pinned_pack_not_a_newer_one(...):
    # the same drift-regression shape as Task 4's snapshot test —
    # confirm a session's document fetch reflects its original pack
    # even after a newer one is confirmed for the same workspace
    ...
def test_ordinary_render_route_still_works_with_no_session_header(...):
    # confirms webapp/api/review.py's existing route is untouched —
    # this is the regression test for "strictly additive, not a
    # modification to the existing route"
    ...
```

Extension: update the 2 existing `attachment.test.ts` assertions (they
currently assert `X-Handoff-Credential` and the old
`workspaceId`/`packArtifactId` URL shape) to the new
`sessionId`/`sessionToken`/`X-Handoff-Session-Token` shape — a required
test update, not new coverage, matching the function's new signature.

New attachment DOM-interaction tests (exact shape TBD — depends on
Task 8's file-structure decision).

Run relevant subsets, expect FAIL on the changed assertions and new tests.

- [ ] **Step 2: Implement the new route, `fetchExactPackDocument`'s new signature, and the new DOM-interaction code + outcome classification**

- [ ] **Step 3: Wire attachment fetch/send into `runAutofillOnTab`** — after Task 10's injection/autofill succeeds: fetch CV+cover-letter via `fetchExactPackDocument(baseUrl, sessionId, sessionToken, kind)`, perform the file-input write, classify the outcome, send via `buildAttachmentEventPayload` → `sendEvent` (session-token-headed, per Task 3).

- [ ] **Step 4: Run to verify**

Run: `cd extension && npx vitest run` and `python -m pytest tests/webapp -q`
Expected: `attachment.test.ts`'s 4 existing tests now pass with the
updated signature/header assertions (not a net-new count, modified
assertions); the new server-side route tests pass; whatever new
DOM-interaction tests were added pass; zero regressions elsewhere,
including in `webapp/api/review.py`'s own existing render-route tests
(untouched file, must still fully pass unmodified).

- [ ] **Step 5: Commit**

```bash
git add webapp/api/handoff.py extension/src/background/attachment.ts extension/src/background/index.ts \
        extension/test/attachment.test.ts tests/webapp/api/test_handoff_routes.py <new attachment DOM files>
git commit -m "feat: add session-scoped document endpoint, wire attachment fetch/upload into the live handoff flow"
```

**STOP CONDITION:** Do not proceed to Task 12 until (a) `webapp/api/review.py`
is confirmed completely unmodified by `git diff` for this task, and its
existing render-route tests all still pass untouched, and (b) the new
session-scoped endpoint is confirmed to have no request parameter that
could select a different workspace or pack than the one the presented
session token is bound to.

---

## Task 12: Extension — remove manual-test bridge, productionize client-sequence recovery (per-session)

**Objective:** Delete `MANUAL_TEST_SNAPSHOT_KEY`/`MANUAL_TEST_SESSION_ID_KEY`
and their reads entirely. Rework the client-sequence recovery mechanism
(currently `MANUAL_TEST_CLIENT_SEQUENCE_KEY` plus a single module-level
`router: MessageRouter | null` variable) into a genuinely **per-session**
model — a map/store keyed by `handoff_session_id`, not a single global
slot — so two concurrent application tabs (two different handoff
sessions) never share or corrupt one sequence namespace.

**Decision (no longer open):** confirmed by reading the current code
(`extension/src/background/index.ts:32,48-64`) — `router` is a single
module-level `MessageRouter | null`, and `ensureRouter()` silently
discards the old router and builds a fresh one the moment the observed
session id changes (`if (router && router.handoffSessionId ===
handoffSessionId) return router;` — any other session id falls through
to constructing a new one, abandoning the old). This is exactly the
"one global counter, reset on session change" behavior that would
silently corrupt or lose in-flight sequence state if two tabs' sessions
ever interleaved. **This must become genuinely per-session**, not a
rename: both the module-level `router` variable and the persisted
sequence storage move from a single slot to a map keyed by
`handoff_session_id`.

**Files:**
- Modify: `extension/src/background/index.ts`
- New (or extend an existing test file, per Step 1's finding): a test
  file covering the reworked `ensureRouter`-equivalent logic, extracted
  into a testable pure-enough function if it isn't already (this file's
  `router`/`ensureRouter`/`persistClientSequence` have never been
  unit-tested directly — established as untested glue in Sub-project
  1's own research — so extraction may be needed here for the first
  time to make the per-session behavior actually verifiable).

**Existing code reused:**
- `MessageRouter`'s constructor shape (`new MessageRouter(eventQueue,
  handoffSessionId, startingClientSequence)`) and its
  `handoffSessionId`/`clientSequence` properties — unchanged.
- `MessageRouter`, `DurableEventQueue`'s sort-by-`clientSequence` flush
  logic (`event-queue.ts:32`) — unchanged consumers; a per-session map
  of routers changes nothing about how any single router's events are
  queued/flushed/sorted.

**New shape:**
```typescript
// Module-level state becomes a map, not a single nullable slot.
const routers = new Map<string, MessageRouter>();

interface PersistedClientSequence {
  sequence: number;
}
const SESSION_CLIENT_SEQUENCE_KEY_PREFIX = "handoff_client_sequence:";

async function ensureRouter(handoffSessionId: string): Promise<MessageRouter> {
  const existing = routers.get(handoffSessionId);
  if (existing) return existing;

  const persisted = await getStorageValue<PersistedClientSequence>(
    `${SESSION_CLIENT_SEQUENCE_KEY_PREFIX}${handoffSessionId}`,
  );
  const startingClientSequence = persisted?.sequence ?? 0;

  const router = new MessageRouter(eventQueue, handoffSessionId, startingClientSequence);
  routers.set(handoffSessionId, router);
  return router;
}

async function persistClientSequence(router: MessageRouter): Promise<void> {
  await chrome.storage.session.set({
    [`${SESSION_CLIENT_SEQUENCE_KEY_PREFIX}${router.handoffSessionId}`]:
      { sequence: router.clientSequence } satisfies PersistedClientSequence,
  });
}
```
Callers of `ensureRouter()` (the `onMessage` listener's event-relay
branch) now pass the actual `handoffSessionId` for the message's tab
(threaded through from Task 9's `associateHandoffSession` result, held
per-tab — confirm exactly how tab-to-session association is tracked at
implementation time, since this is the piece that makes "two tabs, two
sessions" concretely work end-to-end, not just at the storage layer).
Switches from `chrome.storage.local` to `chrome.storage.session` for the
sequence value itself (matching `PendingHandoffContext`'s storage area —
this is genuinely fresh per-session state, not something that should
survive a full browser restart either) — **note this is a change from
the design spec's Section 9, which didn't specify a storage area for
this key; `chrome.storage.session` is the more correct choice given the
data's actual lifetime, decide differently only if implementation
reveals a reason it must survive a browser restart, which the original
manual-test version never actually required either.**

- [ ] **Step 1: Determine whether `ensureRouter`/`persistClientSequence`
  need extraction to be unit-testable**, and extract if so (matching the
  pure-logic-behind-a-function convention already established elsewhere
  in this codebase — e.g. taking a storage-get/set function as a
  parameter rather than calling `chrome.storage.session` directly,
  mirroring `CredentialStore`'s own testable shape).

- [ ] **Step 2: Write the failing tests**

```typescript
it("creates independent routers for two different session ids", async () => {
  const routerA = await ensureRouter("session_a");
  const routerB = await ensureRouter("session_b");
  expect(routerA).not.toBe(routerB);
  expect(routerA.handoffSessionId).toBe("session_a");
  expect(routerB.handoffSessionId).toBe("session_b");
});

it("returns the same router for a repeated call with the same session id", async () => {
  const first = await ensureRouter("session_a");
  const second = await ensureRouter("session_a");
  expect(first).toBe(second);
});

it("resumes clientSequence numbering for a session after a simulated service-worker restart", async () => {
  // persist {sequence: 5} for session_a, then simulate a restart by
  // clearing the in-memory `routers` map (not the storage) and calling
  // ensureRouter("session_a") again — assert the new router starts at
  // clientSequence 5, not 0
});

it("advancing session_a's sequence does not affect session_b's persisted sequence", async () => {
  // the regression test that concurrent tabs cannot corrupt each
  // other's sequence state — the actual point of this whole task
});
```

Run: `cd extension && npx vitest run test/<new or extended file>`
Expected: FAIL against the old single-slot implementation.

- [ ] **Step 3: Delete the two manual-test keys and their read sites**

Remove `MANUAL_TEST_SNAPSHOT_KEY`, `MANUAL_TEST_SESSION_ID_KEY`,
`getManualTestValue` (if nothing else needs its generic shape after
this — confirm at implementation time; the reworked
`ensureRouter`/`persistClientSequence` above use a differently-named
storage helper, not `getManualTestValue`).

- [ ] **Step 4: Implement the per-session `routers` map, storage-keying scheme, and wire in real session ids from Task 9**

- [ ] **Step 5: Run to verify**

Run: `cd extension && npx vitest run` and `npx tsc --noEmit`
Expected: zero regressions; all Step 2 tests pass, including the
two-concurrent-sessions-don't-corrupt-each-other test.

- [ ] **Step 6: Commit**

```bash
git add extension/src/background/index.ts <new/extended test file>
git commit -m "feat(extension): remove manual-test session bridge, make client-sequence recovery per-session"
```

**STOP CONDITION:** Do not proceed to Task 13 until `grep -rn
"MANUAL_TEST_SNAPSHOT_KEY\|MANUAL_TEST_SESSION_ID_KEY"
extension/src/` returns zero matches, and the two-concurrent-sessions
test explicitly passes — a single-slot implementation that merely
renamed the old key would pass every other test in this task except
that one, so it is the test that actually proves this task's real
objective was met.

---

## Task 13: Build wiring + full verification + Playwright acceptance + manual Chrome lifecycle gate

**Objective:** Bundle the new `content-bridge` entry point, run every
automated check, extend the Sub-project 1 Playwright acceptance suite to
cover the full new journey, then perform the manual Chrome sign-off —
mirroring Sub-project 1's Task 8 gate exactly.

**Files:**
- Modify: `extension/scripts/build.mjs`
- Modify: `tests/webapp/test_extension_pairing_acceptance.py` (extend, or
  rename to reflect broader scope — decide at implementation time; likely
  extend in place rather than rename, to keep git history continuous,
  unless the file becomes unwieldy)

**Existing pattern reused:**
- Sub-project 1's Task 8 `build.mjs` change (mkdir/copy/build steps
  added identically for `popup`) — apply the same shape for
  `content-bridge`.
- `tests/webapp/test_extension_pairing_acceptance.py`'s `live_server`
  fixture (bound to the fixed port 8420 the built extension's
  `ServerClient` hardcodes), `extension_context` fixture, and
  `_open_popup`/`_service_worker` helpers — reused directly, not
  reinvented.

- [ ] **Step 1: Build script — add `content-bridge` entry point**

Mirror the exact `mkdir`/`build({...})` pattern already used for
`background`/`content`/`popup` in `scripts/build.mjs`.

Run: `cd extension && npm run build && find dist/extension -type f`
Expected: `dist/extension/content-bridge/index.js` now present alongside
everything else.

- [ ] **Step 2: `npx tsc --noEmit`**

Expected: 0 errors.

- [ ] **Step 3: Full extension Vitest suite**

Run: `cd extension && npx vitest run`
Expected: the cumulative count from Tasks 6/7/9/10 (90 + 2 + 3 + ~5 + 1 =
~101, exact number depends on how many tests Tasks 8/11/12 actually
added — reconcile the real total against the plan's running estimate
at this step, note any drift and why).

- [ ] **Step 4: Full webapp pytest suite**

Run: `python -m pytest tests/webapp -q`
Expected: cumulative count from Tasks 1-5/11, zero regressions.

- [ ] **Step 5: Extend the Playwright acceptance suite**

New tests in `tests/webapp/test_extension_pairing_acceptance.py` (or its
renamed successor):
```python
def test_apply_with_extension_button_disabled_without_trustworthy_url(live_server, extension_context): ...
def test_apply_with_extension_stores_pending_context_via_loopback_bridge(...): ...
def test_toolbar_click_after_apply_associates_session_and_autofills(...):
    # the full journey: navigate to workspace_detail with a confirmed
    # pack + discovery-origin URL, click "Apply with extension", follow
    # the navigation to a real ATS fixture page, THEN drive the
    # extension's popup_run_autofill exactly as a real toolbar click
    # would (matching how Sub-project 1's own suite already does this),
    # assert the session associates, the projected snapshot arrives, and
    # autofill runs on the fixture page with zero runtime errors
    ...
def test_retry_after_failed_association_still_succeeds(...):
    # forces a discover/start failure (e.g. stop live_server briefly, or
    # inject a bad workspace_id), confirms pending context is NOT
    # cleared, then confirms a subsequent retry succeeds
    ...
def test_attachment_reaches_render_route_with_session_token(...): ...
```

Run: `python -m pytest tests/webapp/test_extension_pairing_acceptance.py -v`
Expected: 6 existing (Sub-project 1) + ~5 new = ~11, all passing.

- [ ] **Step 6: Full CI-equivalent local run**

Run the exact CI sequence locally: `npm ci --prefix extension && npm
--prefix extension run typecheck && npm --prefix extension test && npm
--prefix extension run build && python -m pytest -q --browser-channel
chrome`.
Expected: matches CI exactly, no surprises before pushing.

- [ ] **Step 7: Commit build wiring + acceptance tests**

```bash
git add extension/scripts/build.mjs tests/webapp/test_extension_pairing_acceptance.py
git commit -m "feat(extension): bundle content-bridge entry point; extend Playwright acceptance for session-start/attachment"
```

- [ ] **Step 8: Manual Chrome lifecycle sign-off** (not automatable,
  documented for the user, matching Sub-project 1's Task 8 Step 7 exactly):

1. Run the webapp locally.
2. Navigate to a workspace with a confirmed pack and a discovery-origin
   trustworthy URL — confirm "Apply with extension" renders enabled.
3. Navigate to a workspace with a confirmed pack but no trustworthy URL
   — confirm the button renders disabled with the explanatory title.
4. Click "Apply with extension" on the eligible workspace — confirm the
   employer/ATS fixture page opens in a new tab, and confirm (via
   DevTools on the *background service worker's* console, not the page)
   that `chrome.storage.session` now holds a `PendingHandoffContext`.
5. **Click the extension's toolbar icon on that new tab** — this is the
   `activeTab` grant; do not skip or assume this step. Confirm the popup
   shows "Paired ✓ / Run autofill on this tab" as before.
6. Click "Run autofill on this tab" — confirm the session associates
   (check the background console for no errors), the fixture page's
   fields populate, and the pending context is cleared afterward
   (`chrome.storage.session` no longer holds it).
7. Force a discover/start failure (temporarily stop the local webapp
   after clicking "Apply with extension" but before clicking the toolbar
   icon), click the toolbar icon anyway, confirm a clear failure message
   and that the pending context is still present; restart the webapp,
   click the toolbar icon again, confirm it now succeeds.
8. Idle the extension ~30-60 seconds mid-session (to force an MV3
   service-worker restart) between two autofill-relevant actions if the
   fixture page supports multiple fields filled in separate passes;
   confirm event ordering is not corrupted (check the server's received
   events, if inspectable, or at minimum confirm no visible errors).
9. Confirm the attached CV/cover-letter documents reach the fixture
   page's file input (if the fixture page has one) with the correct
   filenames/content.

Report back: did each of the 9 numbered checks pass, and what (if
anything) step 7/8 showed.

**STOP CONDITION (final):** Sub-project 2 is implementation-complete only
once every automated check in Steps 1-6 passes AND every manual check in
Step 8 is confirmed. Do not merge, tag, release, or begin any further
sub-project until this full gate is closed.

---

## Sub-project 2 Acceptance Gate (end-to-end ordinary-user journey)

*A user with a confirmed Application Pack for a discovery-origin job
clicks "Apply with extension" on `workspace_detail.html`. The employer's
application page opens in a new tab — the extension does nothing to it
yet. The user clicks the JobSearch extension's toolbar icon on that tab
(the required, real `activeTab`-granting gesture). The extension's popup
shows "Paired ✓ / Run autofill on this tab"; the user clicks it. The
extension probes the page to detect the ATS adapter and classify its
fields — no candidate data exists yet at this point. Using the pending
context stored when "Apply with extension" was clicked, the extension
discovers an existing in-progress session for this workspace/domain or
starts a new one, receiving a short-lived session token bound to that
one session. It requests only the candidate-snapshot fields the detected
page's fields actually need, scoped to the exact confirmed pack the
session is pinned to — never a bulk profile dump, never a newer pack
that may have been confirmed since. The returned projection is injected
and the existing safe-catalog autofill logic runs unchanged. The
extension fetches the exact confirmed CV and cover-letter documents (by
the same pinned pack id) and attaches them to the page's file input,
recording the outcome. At no point does the user open DevTools or
manually edit `chrome.storage.local` — every step is a real click on a
real page.*

This is the acceptance gate for Sub-project 2 as a whole, distinct from
any individual task's own stop condition above.
