# Ticket 12B — Evidence Profile Manager: Design

Status: **Under revision — amendments pending PM re-review.** Not yet implemented.
This document reflects the design after three review rounds; see "Design history" at
the end.

## Baseline

- `master` at `6004d8d` (PR #21 merged — Ticket 12A Search Workspaces, on top of PR #20
  Lane B Application Generation Quality). No Ticket 12B branch, issue, or plan document
  existed prior to this spec.
- Source material: a recovered design brief (not an approved implementation plan) plus
  independent verification of its factual claims against the actual code at `6004d8d`
  (see "Baseline verification" below).

## Purpose

Ticket 12A gave the product isolated search workspaces. Ticket 12B gives the product
**safe, auditable in-app maintenance of the Evidence Profile those searches depend on**
— structured editing of the candidate's canonical evidence source, after initial setup,
without weakening evidence integrity.

Today, `webapp/services/profile_setup.py`'s `import_profile_markdown` only permits a
write while the canonical profile source is unconfigured
(`profile_source_is_unconfigured`) — a first-run-only path. Once configured, the
canonical Markdown source is effectively outside the normal product editing flow; any
correction requires manually hand-editing the file outside the app. Ticket 12B closes
that gap: structured editing, a preview/diff review gate, atomic + crash-recoverable
commit, and optimistic concurrency — all built on infrastructure this codebase already
has, not a parallel profile system.

## Baseline verification

Every factual claim below was checked directly against `master` at `6004d8d`, not
assumed from the recovered brief:

- **Three canonical sources, in this order** — confirmed at `product/profile_snapshot.py:30-34`
  (`SOURCE_PATHS = ("CLAUDE.md", ".claude/skills/job-application-assistant/01-candidate-profile.md", "cv/main_example.tex")`).
  Only the second is the editable canonical source (per `CANDIDATE_PROFILE_PATH` in
  `profile_setup.py:15-17`); `CLAUDE.md` and `cv/main_example.tex` are read-only
  corroborating/legacy sources, untouched by Ticket 12B.
- **In-app writes are setup-only today** — confirmed at `profile_setup.py:107-110`:
  `import_profile_markdown` raises `PipelineError` unless
  `profile_source_is_unconfigured(root_path)` is true. There is no existing path to
  edit a configured profile in-app.
- **Snapshots are already immutable artifacts with a current pointer** — confirmed at
  `webapp/services/pipeline.py:88-89`, `get_current_profile_snapshot` reads via
  `get_current_artifact(conn, PROFILE_WORKSPACE_ID, "profile_snapshot")`, the same
  content-addressed-artifact-plus-current-pointer pattern used throughout this codebase
  (job_fit_result, application_intelligence_result, etc.).
- **Real atomic-write and rollback precedent already exists** — `profile_setup.py`'s
  `_atomic_write`/`_atomic_write_bytes` (tempfile + `os.replace`, lines 176-193) and
  `import_profile_markdown`'s try/except rollback-to-previous-bytes-on-failure
  (lines 118-131) are the direct precedent this design's commit sequence reuses.
- **Prospective-snapshot validation precedent already exists** —
  `_validate_prospective_snapshot` (`profile_setup.py:146-173`) already builds a
  snapshot in a temporary directory before committing, exactly the "preview" pattern
  this design generalizes to the post-setup editing case.
- **Ticket 12A optimistic-concurrency precedent exists but does not directly apply** —
  `webapp/persistence/search_workspaces.py` has `SearchWorkspaceConflictError` and a
  DB-tracked `current_version_id`/`previous_version_id` revision pattern for
  DB-row-backed entities. Evidence Profile's canonical source is a *file*, not a DB
  row: a DB revision counter would be blind to any edit made to the file outside the
  app's own write path (e.g. a maintainer hand-editing the Markdown, or a `/setup` or
  `/expand` skill invocation). This design therefore uses direct file-content hashing
  instead (see Decision area 2) — a deliberate divergence from 12A's mechanism, not an
  oversight; the two are solving different problems (DB-row concurrency vs.
  file-content concurrency).

## Non-goals (explicit)

- **No changes to Ticket 7 (Semantic Job Fit), Lane B (Application Intelligence),
  Gate 4, Search Workspaces (Ticket 12A), or any historical artifact.** Ticket 12B only
  adds a new write path for the canonical profile Markdown source and the machinery to
  make that write path safe; it does not touch how any downstream consumer reads a
  Profile Snapshot.
- **No changes to `product/profile_snapshot.py`'s parsing/build logic.** The snapshot
  builder remains the sole authority for turning source files into claims. Ticket 12B
  consumes it exactly as `profile_setup.py` already does (`build_snapshot`,
  `refresh_profile`) — it does not reimplement or fork evidence extraction.
- **No CV/document extraction.** Explicitly deferred per the recovered brief: any
  future feature that produces *proposed* evidence from an uploaded CV/document
  requiring explicit accept/reject is out of scope for v1. Ticket 12B only edits the
  already-canonical Markdown source directly.
- **No automatic conflict resolution.** The editor never presents an "accept this
  value" / "resolve conflict" / "mark verified" action. See Decision area 4.
- **No generic PATCH/PUT endpoint for profile source files.** Every write goes through
  preview → confirm; there is no direct-write API surface at all, including for
  power users or automation.
- **No second source of profile-history truth.** The change-set table (Decision area
  3) is operational recovery state, not a historical record — see its transient-storage
  rule.

## Decision area 1: Editable field coverage and unmanaged-content preservation

The structured editor covers exactly the categories the recovered brief lists:
identity, employment, education, skills/tools, certifications, languages,
publications, awards — matching `product/profile_snapshot.py`'s existing parsed
categories.

**Editor-schema authority vs. snapshot-parser authority are kept deliberately
separate**, addressing a self-review finding from the first design round (see Design
history): coupling the editable-field whitelist directly to whatever
`profile_snapshot.py` happens to parse today would make the editor a de facto second,
undocumented parser contract. Instead:

- A new, explicitly versioned schema (`PROFILE_EDITING_SCHEMA_VERSION`, defined in a
  new module — file name TBD at implementation-plan time, e.g.
  `product/profile_editing_schema.py`) declares the set of editable fields: one entry
  per editable concept (e.g. `identity.name`, `employment[].job_title`,
  `employment[].date_range`, `employment[].responsibility_or_achievement`, ...), each
  declaring (a) which canonical-Markdown section/pattern it renders to, and (b) which
  `(category, field)` pair in a rebuilt snapshot it is expected to produce.
- `product/profile_snapshot.py`'s parser is the sole authority for what a rewritten
  Markdown section actually parses to. The editing schema is the sole authority for
  what the UI is permitted to mutate and how it renders back to Markdown.
- **These two authorities are reconciled, never assumed consistent, at preview time**:
  after rendering a prospective Markdown section from an edit, the editor rebuilds a
  prospective snapshot (via the existing `build_snapshot`) and checks that the claim(s)
  the editing schema declared it intended to produce are actually present with the
  expected `(category, field)` in that rebuilt snapshot. **Any disagreement is a hard
  preview-time failure** ("editing schema and snapshot parser disagree for field X"),
  never a silent divergence. This closes the ambiguity the PM review explicitly asked
  the self-review to check for.
- The two schemas are independently versioned. The editing schema can gain new
  editable fields without requiring a `profile_snapshot.py` parser change, and vice
  versa — a parser change that doesn't correspond to an editing-schema field simply
  isn't editable yet, which is safe (fails closed, not silently).

**`start_edit` reads current field values from the canonical Markdown directly, never
from aggregated snapshot claims** (added during second PM review round). The current
snapshot's `claims` list is an aggregate over all three `SOURCE_PATHS` — it includes
`CLAUDE.md` (legacy, read-only) and `cv/main_example.tex` (imported/corroborating,
read-only) alongside the one editable canonical source. Sourcing editable field values
from that aggregate would let a read-only source's value — including one on the losing
side of a recorded conflict — surface as if it were an editable canonical value,
directly contradicting "the other two sources are read-only." `start_edit` therefore
parses `.claude/skills/job-application-assistant/01-candidate-profile.md` directly,
using the same canonical-shape recognition Decision area 1 already defines for
rewriting, to produce the current editable values. The current snapshot is still
consulted, but only for two purposes: (a) `conflicts` annotations shown alongside a
field, and (b) `precondition_snapshot_content_id`, neither of which requires reading
any claim's *value* out of the aggregate.

**Record addressing for repeatable fields** (added during second PM review round): an
`EditableFieldSpec` for a repeatable concept (employment, education) must identify a
*specific record*, not merely a `(category, field)` pair with no record identity — a
profile with two employment records has two distinct `job_title` values, and an edit
must target one of them, not "some claim with field=job_title." **v1 uses
canonical-order record addressing**: within a section, records are addressed by their
zero-based position in canonical-Markdown order (e.g. `employment[0].job_title`,
`employment[0].date_range`, `employment[1].job_title`, ...). This is sound specifically
*because* of the whole-file precondition hash: `start_edit`'s precondition guarantees
the canonical file's byte content — and therefore record ordering — has not changed
between `start_edit` and `preview_edit`/`confirm_edit`, so positional addressing cannot
silently drift onto the wrong record between those calls. The rewriter and the
schema/parser reconciliation check (below) must both operate on the *specific
positional record* an edit targets, never on "does some record somewhere in the
rebuilt snapshot have the expected value" — the latter would pass even if an edit
landed on the wrong record, or if two records happened to share a duplicate value.

**Unmanaged content preservation**: edits rewrite only the Markdown regions the editing
schema recognizes (a known section heading + a known field's list-item pattern within
it). Content outside those regions — custom sections, comments, formatting the schema
doesn't model, and any recognized-section content that doesn't match a known field
pattern — is preserved byte-for-byte via the same section-identification pass, never
regenerated. **If the section-boundary identification cannot confidently locate where a
recognized section starts/ends to safely rewrite it in place, the edit fails closed**
with an explicit error at preview time, rather than guessing at placement or silently
dropping content. This is the literal implementation of the recovered brief's Decision
#1 ("preserve unsupported/unmanaged content or fail closed — never silently discard
it").

## Decision area 2: Optimistic concurrency — direct file-content hashing

Per PM decision, concurrency uses **direct SHA-256 hashing of the three source files**,
not a DB-tracked revision counter:

- `start_edit` computes and returns SHA-256 of each of the three files in
  `SOURCE_PATHS`, plus the current profile snapshot's `content_id` (from
  `profile_snapshot_content_id`, already used throughout the codebase for staleness
  detection — same content-identity pattern, reused not reinvented). This tuple —
  `precondition_source_hashes` + `precondition_snapshot_content_id` — is the edit's
  precondition.
- Hashing the actual file bytes (rather than a DB revision counter) detects **any**
  change to the canonical source, including one made outside the app's own write path
  (a maintainer hand-editing the Markdown, a `/setup` or `/expand` skill run, direct
  filesystem access) — a DB-only counter would be blind to exactly this class of
  change, since it only increments on writes the app itself performs.
- The precondition is re-verified, byte-for-byte, at `confirm_edit` time (Decision area
  3) — never trusted from `start_edit` or `preview_edit` alone.

## Decision area 3: Preview/change-set/recovery model

### The core invariant: exact-preview binding

**What was shown is what gets written.** `confirm_edit` never accepts an edits payload
and never re-derives Markdown from one. It takes only a `changeset_id` — the identity
of an already-fully-rendered, already-hashed, already-previewed set of bytes — and, if
every precondition still holds, writes exactly those bytes. This closes the TOCTOU gap
identified during design review: without this binding, a client could preview edit A,
then submit a confirm request carrying a different edit B against the same
still-unchanged precondition, and the server would have no way to know the user never
actually reviewed B.

### Table: `profile_edit_changesets`

**Corrected during a second PM review round (see Design history):** the status
enum below includes `commit_started`, a durable pre-write state, closing a real gap
in the first-round design — a crash between the file write (step 6, below) and the
status update that used to follow it (`file_written`, old step 7) left the row at
`previewed` with the file already changed, invisible to recovery (which only ever
scanned `file_written`). `commit_started` is claimed atomically, *before* the file
write, closing that gap.

```sql
CREATE TABLE profile_edit_changesets (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
        -- 'previewed' | 'commit_started' | 'file_written' | 'committed'
        -- | 'rolled_back' | 'recovery_conflict' | 'expired'
    editing_schema_version TEXT NOT NULL,
    precondition_source_hashes TEXT NOT NULL,       -- JSON: {source_path: sha256}
    precondition_snapshot_content_id TEXT NOT NULL,
    prospective_source_hash TEXT NOT NULL,          -- SHA-256 over new_markdown; the preview identity
    diff_summary TEXT NOT NULL,                     -- claim/source diff shown at preview; retained permanently for audit
    new_markdown TEXT,                              -- transient; see storage rule below
    previous_markdown TEXT,                         -- transient; see storage rule below
    created_at TEXT NOT NULL,
    committed_at TEXT
);
```

**Storage rule (corrected during PM review):** `new_markdown`/`previous_markdown` are
operational recovery data, not historical evidence truth — the immutable Evidence
Snapshot system remains the sole authority for historical profile content. These two
columns are cleared to `NULL` **only when a row reaches a resolved terminal state**:
`committed`, `rolled_back`, or `expired`. **They are explicitly NOT cleared on
transition into `recovery_conflict`** — that state means the recovery decision is
still ambiguous and requires operator attention, and clearing the bytes would destroy
the exact material needed to understand and resolve the ambiguity. A
`recovery_conflict` row keeps its source bytes until it is manually walked to a
resolved terminal state (see Recovery UX).

**Expiry (added during second PM review round):** the spec always intended
`new_markdown`/`previous_markdown` to be transient, but the first-round design left
"may be expired automatically" underspecified — no mechanism, no trigger. This is
corrected to two explicit, bounded cleanup rules:

- **Abandoned `previewed` rows** (never reached `commit_started`) are eligible for
  expiry after a bounded age (implementation-plan detail: a fixed duration, e.g. 24
  hours, is sufficient — no file write occurred, so there is no recovery material tied
  to them, only an unused prospective render). Expiry transitions `status='expired'`
  and clears both transient columns, exactly like `committed`/`rolled_back`.
- **Sibling previews invalidated by a successful commit** (per the Lifecycle rule
  below: once any one changeset commits, every other `previewed` row sharing the same
  `precondition_source_hashes`/`precondition_snapshot_content_id` is now structurally
  unconfirmable) are likewise eligible for the same bounded expiry — they do not need
  to wait out the full abandonment window once a sibling has already committed, but
  immediate proactive scrubbing at commit time is an implementation-plan optimization,
  not a design-level requirement; the bounded-age expiry sweep alone is sufficient to
  guarantee these never accumulate as permanent shadow history.

Everything else in the row — hashes, ids, `diff_summary`, `editing_schema_version`,
timestamps, final status — is retained permanently as an audit trail.

### Confirm sequence

**Corrected during second PM review round:** the sequence now claims a durable
`commit_started` state *before* the file write, closing the crash-window gap
described above, and step 6 is explicitly framed as the one place two concurrent
confirms against sibling previews must be serialized (see the note after step 6).

1. Re-read the three source files; recompute hashes; compare against
   `precondition_source_hashes`. Mismatch → `409` (`source_changed`).
2. Recompute `content_id` of the *current* profile snapshot; compare against
   `precondition_snapshot_content_id`. Mismatch → `409` (`snapshot_changed`).
3. Recompute SHA-256 of the row's own stored `new_markdown`; compare against
   `prospective_source_hash`. Mismatch → hard failure (row corruption / tampering
   defense — should never happen in normal operation).
4. Check `editing_schema_version` compatibility (exact match, or an explicitly
   documented compatible range — compatibility policy is an implementation-plan
   detail, not a design-level decision needed now).
5. **Atomically claim the row**: `UPDATE profile_edit_changesets SET status =
   'commit_started' WHERE id = ? AND status = 'previewed'`, checking the update
   affected exactly one row. This single conditional UPDATE is the serialization
   point: if two sibling previews (same precondition, different edits) both pass
   steps 1-4 and race to confirm, only one's UPDATE can match `status = 'previewed'`
   — the loser's UPDATE affects zero rows and is treated as a `409` (`conflict`,
   "a sibling changeset already committed"), never proceeding to step 6. This closes
   the race the first-round design left open: without this atomic claim, both could
   pass their hash checks and both attempt the file write.
6. Atomic file write (`_atomic_write_bytes`, existing precedent). Only the caller
   that won step 5's claim ever reaches this step for a given precondition.
7. Mark `status='file_written'`.
8. `refresh_profile` — rebuild + persist the new snapshot, advance the current
   pointer. (Existing, already-atomic-at-the-DB-layer operation, reused unmodified.)
9. Mark `status='committed'`, set `committed_at`.
10. Clear `new_markdown`/`previous_markdown` (now safely in a resolved terminal
    state).

Return: new snapshot identity, resulting conflict/warning summary.

### Three-way crash recovery (corrected during second PM review round)

On restart, for **every row at `status='commit_started'` OR `status='file_written'`**
— both are pre-terminal states a crash can leave behind mid-commit. (A crash before
step 5's atomic claim succeeds leaves the row at `previewed`, genuinely safe/inert
since no file write occurred and the row never left the state a fresh confirm attempt
can safely retry from scratch. A crash after step 9 is already terminal.)

**This is the fix for the gap the first-round design had**: previously only
`file_written` was scanned, so a crash between step 6 (file write) and step 7 (mark
`file_written`) left a row at `previewed` — indistinguishable from "confirm never
started" — while the canonical file had already changed. Scanning `commit_started`
too closes this: that status is claimed *before* step 6's write, so a crash anywhere
from immediately after the claim through the write itself is now visible to recovery.

For each such row:

1. Read the canonical file's actual current content.
2. **Equal to `new_markdown`** → the write (step 6) completed (whether the crash
   happened at `commit_started` right after a fast write, or at `file_written`
   afterward, the file content is what tells recovery the write finished) but the
   snapshot rebuild (steps 8-9) did not. Resume from step 8: `refresh_profile` is
   idempotent (it rebuilds from whatever the file currently contains), so retrying it
   is safe. On success, proceed through steps 9-10 normally.
3. **Equal to `previous_markdown`** → the write (step 6) never took effect (crash
   before `os.replace` completed, or before it was attempted — this is the only
   possibility for a row found at `commit_started` whose file still matches the old
   content). Mark `status='rolled_back'`, clear the transient bytes. Nothing to undo —
   the file was never changed.
4. **Equal to neither** → **do not overwrite anything, do not guess, do not attempt
   automatic resolution.** Mark `status='recovery_conflict'`. This means the canonical
   file was changed by something external between this changeset's file write and the
   completion of its snapshot rebuild — a state that should never occur in normal
   operation, and treating either stored copy as authoritative would silently discard
   whatever that external change was. Recovery must never interpret an external edit
   as permission to restore either stored copy.

Recovery runs automatically at process startup (before the profile page or edit API
becomes available) for cases 2 and 3, which are both safely resolvable without human
judgment. Case 4 always halts and requires the operator-facing recovery UX below —
never resolved automatically, regardless of how "obviously right" one of the two
stored copies might look.

**Unresolved pre-terminal/ambiguous states block new edits.** Any row currently at
`commit_started`, `file_written`, or `recovery_conflict` — i.e. anything recovery
has not yet resolved to `committed`/`rolled_back`, or has explicitly halted on — means
new `start_edit`/`preview_edit`/`confirm_edit` calls must be rejected until resolved.
In normal operation this window is momentary (recovery runs before the API becomes
available at all); it only persists visibly when a `recovery_conflict` is genuinely
unresolved, in which case this is the same "profile edits are disabled" rule the
Recovery UX section already specifies for that case, now stated as a hard API-layer
invariant, not only a UI-layer one.

### Lifecycle rule (falls out of the model, stated explicitly)

**One current edit session (from a single `start_edit`) may produce many previews;
only one exact preview can ever be confirmed.** This is not a separately-implemented
rule — it is a direct consequence of confirm-time precondition re-verification: once
any one changeset commits, the canonical file and current snapshot both change, so
every other changeset's `precondition_source_hashes`/`precondition_snapshot_content_id`
immediately stops matching current state, and its confirm attempt fails at step 1 or 2
above. No additional bookkeeping (e.g. explicitly invalidating sibling changesets) is
needed or implemented — staleness is detected structurally, at the moment it's checked,
not tracked proactively.

## API surface

Deliberately small and action-oriented. No generic PATCH/PUT for profile source files
— every write goes through preview → confirm, with no bypass, including for
power-user/automation use.

**Recovery-blocked state is enforced at this layer, not only the UI** (added during
second PM review round). Every one of the four routes below first checks whether any
`profile_edit_changesets` row is currently at `commit_started`, `file_written`, or
`recovery_conflict` — i.e. anything crash recovery has not yet resolved. If so, the
route rejects with `409` (reason `recovery_pending`) rather than proceeding, regardless
of whether the caller is the UI or a direct API client. In normal operation this check
never blocks anything, since recovery runs at startup before the API accepts traffic;
it only actually blocks when a `recovery_conflict` is genuinely unresolved, which is
also when the UI's blocking banner (Recovery UX, below) is showing. A dedicated
recovery-status signal, `GET /api/profile/recovery-status` (added alongside the four
core routes for this reason), returns `{"blocked": bool, "reason": "recovery_conflict"
| null}` so the UI has one explicit endpoint to poll/check rather than inferring
recovery state from a `409` on some other call.

### `POST /api/profile/edit-sessions`

Starts an edit against current profile state. No write. Returns:
- `editing_schema_version`
- structured editable fields (current values, grouped by category, parsed from the
  canonical Markdown directly per the corrected Decision area 1 — never from
  aggregated snapshot claims)
- current conflict annotations (from the current snapshot's `conflicts`)
- opaque edit session token (`session_id` — see below)
- current snapshot identity (`content_id`)
- source precondition identity (the three file hashes)

**`session_id` is locked as a stateless, deterministic token** (added during second PM
review round, resolving what the design originally left open): computed as a hash over
`editing_schema_version` + the precondition tuple
(`precondition_source_hashes`, `precondition_snapshot_content_id`) — no server-side
session table, no session-specific state beyond what the precondition tuple already
captures. Any client holding the same precondition tuple (e.g. two browser tabs that
both called `start_edit` against the same unchanged state) computes the same
`session_id`; this is harmless, since `preview_edit` always re-validates the
precondition against *current* state regardless of what `session_id` the caller
supplies, and always creates a new, independently-identified changeset row. The token
exists to make the URL path self-describing and to let a client detect, client-side,
that it's still operating against the state it started from — not to carry any
authority the precondition-tuple recheck doesn't already provide.

### `POST /api/profile/edit-sessions/{session_id}/preview`

Accepts only structured edits expressed in terms the versioned editing schema allows
(never raw Markdown, never arbitrary field names). Server:
1. Verifies the session's precondition still matches current source/snapshot state
   (if not, the client must restart via a new `start_edit` — no partial recovery at
   this stage, since nothing has been written yet).
2. Renders prospective canonical Markdown (recognized-section rewrite, unmanaged
   content preserved byte-for-byte, per Decision area 1).
3. Rebuilds a prospective snapshot (`build_snapshot`, unmodified).
4. Verifies editing-schema ↔ snapshot-parser agreement for every edited field; hard
   failure on disagreement.
5. Computes the exact diff (added/changed/removed claims, by source; conflicts newly
   introduced / no longer present / unchanged-and-still-conflicted).
6. Persists a `previewed` change-set row containing the exact bytes.
7. Returns `changeset_id`, the diff, a prospective-snapshot summary, and any
   validation warnings/conflicts.

Each call creates a **new** change-set with a new identity — an older preview from the
same session is never mutated by a later one.

### `POST /api/profile/edit-changesets/{changeset_id}/confirm`

No edits payload. Runs the confirm sequence above. Returns the new snapshot identity
and resulting conflict/warning summary on success; `409` with a structured conflict
body on precondition failure (see below).

### `GET /api/profile/edit-changesets/{changeset_id}`

Status + audit metadata (hashes, diff_summary, timestamps, status) for UI reload after
interruption. Does **not** return `new_markdown`/`previous_markdown` once the row has
reached a resolved terminal state (they've been cleared per the storage rule); while a
row is still `file_written` or `recovery_conflict`, this endpoint still must not expose
the raw source text in the response — that's operational recovery data for the server's
own use, not something the API surface returns even to an authorized client, to avoid
ever needing to reason about who's allowed to see mid-recovery source bytes.

### Conflict response shape (`409`)

Specifically for optimistic-concurrency failure (confirm-time precondition mismatch).
Body includes:
- a machine-readable reason: `source_changed` and/or `snapshot_changed`
- which specific source path(s) changed identity
- expected vs. current snapshot ids
- whether the client must restart (`start_edit` again) or may safely re-preview from
  a fresh `start_edit` — in this design, precondition mismatch always means restart,
  since the precondition is captured once at `start_edit` and never refreshed
  in-place

**Never includes the changed source's actual content** — only identity/hash
information. The UI message is generic and non-technical: "Your Evidence Profile
changed after this edit began. Nothing was written. Reload the latest profile and
review your changes again." No automatic merge, ever.

## UI structure

Extends the existing Profile page; no parallel "profile administration" surface.

### View mode (existing, unchanged)

The trust-inspection surface already in place: current snapshot identity, sources,
verified claims, conflicts, warnings, provenance. An explicit **Edit Evidence
Profile** action enters Edit mode.

### Edit mode

Renders the versioned editable concepts in the same logical groups the recovered
brief specifies: Identity, Employment, Education, Skills/tools, Certifications,
Languages, Publications, Awards. Each field shows its source relationship and, where
relevant, an existing conflict annotation (e.g. "Current snapshot reports conflicting
evidence for this concept," with references to the conflicting sources) — but **no**
"Accept winner" / "Resolve conflict" / "Mark verified" action anywhere. The user edits
source material; the prospective rebuild (at preview time) decides whether the
conflict still exists.

Unmanaged Markdown content is never presented as if it were editable structured
data — a small notice states that unsupported/custom content will be preserved
unchanged. The editor is explicitly **not** a claims-database UI; it is a controlled
editor over canonical source material.

There is no "Save" directly from the edit form — only **Preview changes**, which calls
`preview_edit` and transitions to the Preview state.

### Preview state (the real review gate)

Dedicated screen, not a modal-over-the-form. Shows:
- added claims
- changed claims
- removed claims
- conflicts newly introduced
- conflicts no longer present
- unchanged, still-conflicted concepts
- warnings
- which source file(s) are affected
- prospective snapshot summary

Confirmation action is explicitly worded to reinforce exact-preview binding: **"Apply
exactly these reviewed changes to the Evidence Profile."** — calls `confirm_edit` with
only the `changeset_id`.

### Recovery UX

Normally invisible: startup recovery for cases 2 and 3 above (resume, or roll back) is
automatic and produces no user-facing signal beyond the profile simply being current
and correct.

If a `recovery_conflict` row exists, the Profile page shows a blocking operational
banner: "An Evidence Profile update was interrupted and the source changed
unexpectedly. No automatic recovery was attempted." **Profile edits are disabled while
any `recovery_conflict` row is unresolved** — no stacking a new edit attempt on top of
an unresolved recovery ambiguity. The UI determines whether to show this banner via
`GET /api/profile/recovery-status` (added during second PM review round, see "API
surface"), rather than inferring it from a failed edit attempt — this same status also
covers the momentary, normally-invisible window where a `commit_started`/`file_written`
row is still being resolved by startup recovery. Resolving a `recovery_conflict` row (walking it to
`committed` or `rolled_back`) is an operator action outside this design's UI scope —
the mechanism for that resolution (a CLI tool, a manual DB fix, an admin UI) is an
implementation-plan detail, not decided here; what's locked at the design level is only
that it must be an explicit, human-directed action, never automatic.

## Acceptance criteria (design-level; concrete tests are implementation-plan work)

- A configured profile can be edited via `start_edit` → `preview_edit` → `confirm_edit`
  and the resulting canonical Markdown file, on disk, contains exactly the previewed
  bytes.
- Confirming a changeset whose precondition no longer matches (source file changed, or
  snapshot advanced by something else) fails with `409` and writes nothing.
- Two previews from the same edit session are independent changesets; confirming one
  makes the other's precondition stale (provable via the confirm sequence, not a
  separate invalidation step).
- Unmanaged Markdown content survives a preview → confirm cycle byte-for-byte.
- An editing-schema field whose rendered Markdown fails to parse back to the expected
  claim fails the preview, with no write attempted.
- Simulated crash after file write but before snapshot rebuild: restart resumes and
  reaches `committed` without data loss or duplicate writes.
- Simulated crash before file write: restart marks the row `rolled_back`; canonical
  file is provably unchanged.
- Simulated external file mutation between write and rebuild (case 4): restart marks
  `recovery_conflict`, canonical file is provably untouched by recovery, and both
  stored copies remain retrievable (not cleared) until explicit resolution.
- No claim/gap/verdict/match/conflict data owned by Ticket 7, Lane B, Gate 4, or Search
  Workspaces changes as a result of any Ticket 12B operation.
- **(Added, second PM review round)** Simulated crash immediately after the
  `commit_started` claim succeeds but before the file write completes: restart finds
  the row at `commit_started` with the file still matching `previous_markdown`, and
  marks it `rolled_back` — the gap the first-round design left open (a crash in this
  exact window used to leave the row invisibly at `previewed`).
- **(Added)** `start_edit` never surfaces a value from `CLAUDE.md` or
  `cv/main_example.tex` as an editable field value, including when that value is on
  the losing/non-canonical side of a recorded conflict with the canonical Markdown's
  value.
- **(Added)** A profile with two employment records and a shared/duplicate value
  between them (e.g. both list the same `date_range`) can have one record's field
  edited without affecting the other — proving record addressing targets the specific
  positional record, not merely "any record with a matching value."
- **(Added)** No `profile_edit_changesets` row accumulates `new_markdown`/
  `previous_markdown` indefinitely: an abandoned `previewed` row past its bounded
  expiry window transitions to `expired` with both columns cleared.
- **(Added)** Two confirms issued concurrently against sibling previews (same
  precondition) never both succeed in writing the file — exactly one wins the
  `commit_started` atomic claim; the other receives `409`.

## Self-review

Run explicitly against six named risks, per PM instruction:

**1. Any path that could write without preview/confirm?** No. The only write path is
`confirm_edit`'s step 6, gated by the full precondition/binding check. No PATCH/PUT
endpoint exists. `start_edit` and `preview_edit` are both read-only with respect to the
canonical file (preview writes only to the `profile_edit_changesets` row, never to the
file).

**2. Any way previewed bytes could diverge from committed bytes?** No — `confirm_edit`
takes no edits payload and never re-renders; it writes the stored `new_markdown`
verbatim, after confirming its own hash still matches `prospective_source_hash` (guards
against row tampering/corruption, not just precondition drift). This is the
exact-preview-binding fix from design review; verified as fully closed in this
revision.

**3. Any recovery path that can overwrite external edits?** No — case 4 (file content
matches neither stored copy) explicitly never writes; it halts. Cases 2 and 3 only ever
either leave the file as-is (case 3) or continue an in-progress `refresh_profile` from
whatever the file *already* contains (case 2) — recovery never writes profile source
content itself, only advances the DB-side snapshot/status bookkeeping.

**4. Any accidental second source of profile-history truth?** Closed by the storage
rule (`new_markdown`/`previous_markdown` cleared on every resolved terminal state —
`committed`, `rolled_back`, `expired` — and `recovery_conflict` explicitly
operational-recovery-pending, not a historical record) **plus, as of the third PM
review round, an actual expiry mechanism** — the round-2 design correctly stated the
storage rule but had no way to make an *abandoned* `previewed` row (one that never
proceeded to `commit_started`, and therefore never reached any of the clearing
triggers above) ever reach a terminal state at all, which would have let such rows
accumulate full source-text pairs indefinitely — exactly the shadow-history the design
set out to avoid, just via a different unhandled path than the terminal-state one this
answer originally addressed. The bounded-age expiry sweep (see "Storage rule") closes
this.

**5. Any ambiguity between editor-schema authority and snapshot-parser authority?**
Closed by design (not merely stated): the two schemas are independently versioned and
reconciled by an explicit runtime check at preview time (step 4 of `preview_edit`) —
any disagreement is a hard preview failure, never assumed-consistent, never silently
accepted. Neither authority can silently drift from the other without the reconciliation
check catching it on the very next preview. As of the third PM review round, this
reconciliation is explicitly required to check the *specific positional record* a
repeatable-field edit targets, not merely "does some record in the rebuilt snapshot
have the expected value" — closing a variant of this same ambiguity that the
round-2 design's `EditableFieldSpec` (lacking any record-addressing concept) would
not have caught.

**6. Any scope leak into Ticket 7, Lane B, Gate 4, Search Workspaces, or historical
artifacts?** None found. Every touchpoint with those systems is read-only and
pre-existing: `refresh_profile`/`build_snapshot` (used exactly as `profile_setup.py`
already uses them, not modified), `profile_snapshot_content_id` (used exactly as the
rest of the codebase already uses it for staleness identity, not modified). No table,
function, or route introduced by this design is written to or read by Ticket 7's job
fit analysis, Lane B's application intelligence, Gate 4's completion gating, or Search
Workspaces' isolation model. The Non-goals section states this explicitly and the API
surface contains nothing that could reach those systems.

## Design history

- Round 1 (initial proposal): established the four-section shape (pipeline,
  unmanaged-content preservation, durable change-set, conflict UX) with a first-draft
  crash-recovery model (two-way: file matches new vs. previous) and non-transient
  change-set storage.
- Round 2 (PM review, this document) corrected: (1) added exact-preview binding —
  `confirm_edit` takes only a `changeset_id`, never an edits payload, closing a
  preview/commit divergence gap; (2) added the third crash-recovery case
  (`recovery_conflict` — file matches neither stored copy — fail closed, never guess);
  (3) made `new_markdown`/`previous_markdown` transient, cleared on resolved terminal
  states only, explicitly NOT cleared on `recovery_conflict` (a further correction
  within round 2 itself, since first clearing then un-clearing would have destroyed
  needed recovery material); (4) decoupled the editable-field whitelist from
  `profile_snapshot.py`'s parser into an independently versioned editing schema,
  reconciled by an explicit runtime check rather than assumed consistency; (5) locked
  the full API surface, conflict-response shape, UI states (View/Edit/Preview/Recovery),
  and the "one session, many previews, one confirmable changeset" lifecycle rule as a
  structural consequence of the precondition-recheck design, not a separately
  implemented invalidation mechanism.
- Round 3 (PM review against the executed implementation plan) corrected six further
  gaps: (1) **the crash-window gap** — the round-2 confirm sequence wrote the file at
  step 6 but only marked `file_written` at step 7, so a crash in that exact window left
  the row at `previewed` (indistinguishable from "never started") with the file already
  changed, invisible to recovery, which only ever scanned `file_written`; fixed by
  adding a durable `commit_started` state claimed atomically *before* the file write,
  with recovery now scanning both `commit_started` and `file_written`; (2) the same
  atomic claim (`UPDATE ... WHERE status = 'previewed'`) doubles as the serialization
  point preventing two sibling previews from both passing precondition checks and
  racing to write the file concurrently; (3) `start_edit` was specified to source
  editable field values from aggregated snapshot claims, which would let a read-only
  source's value (`CLAUDE.md`, `cv/main_example.tex`) — including a conflict's
  losing-side value — surface as if editable; corrected to parse the canonical
  Markdown directly, using the snapshot only for conflict annotations and content-id;
  (4) repeatable fields (employment, education) had no record-addressing model,
  meaning "does some claim have the expected value" would pass even for the wrong
  record or a duplicate value on a different record; corrected to canonical-order
  positional addressing (`employment[0].job_title`, ...), sound specifically because
  the whole-file precondition hash guarantees ordering hasn't shifted between calls;
  (5) `new_markdown`/`previous_markdown` were declared transient but had no actual
  expiry mechanism for abandoned `previewed` rows or commit-invalidated siblings,
  which would have silently accumulated as the exact shadow-history the design set out
  to avoid; corrected with a bounded-age expiry sweep and a new `expired` terminal
  state; (6) the API surface left `{session_id}` as an open implementation choice and
  didn't state the recovery-blocking rule as an API-layer invariant; both locked:
  `session_id` is a stateless hash of schema version + precondition tuple (no session
  table), and every route now explicitly rejects with `409` while any
  `commit_started`/`file_written`/`recovery_conflict` row is unresolved, backed by a
  new `GET /api/profile/recovery-status` signal for the UI.
