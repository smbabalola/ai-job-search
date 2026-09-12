# Extension Session-Start + Attachment Integration — Design Specification

## 1. Goal

Sub-project 2 of the extension-pairing/application-handoff feature (Sub-project
1 — pairing — merged to `master` at `0c8f0a9703ca20c61ca35642b8ff3f63a1c5cba1`).

**Acceptance gate:** *An ordinary user, on a confirmed Application Pack, clicks
"Apply with extension," lands on the employer's application page, clicks the
JobSearch extension's toolbar icon on that page (the one additional,
Chrome-permission-required user action — not automatic), and the extension
then discovers/resumes/starts the correct handoff session, receives only the
candidate data the detected page fields require, and attaches the exact
confirmed CV/cover-letter documents — with no DevTools, no manual
`chrome.storage.local` editing, at any point.*

This document supersedes the boundary contract in
`docs/superpowers/specs/2026-09-11-extension-pairing-design.md` Section 9,
which explicitly said Sub-project 2 "gets its own detailed design, written
after this sub-project ships and is verified against the real implementation."
That verification (reading the actual merged code, not the boundary
contract's assumptions) surfaced three things Section 9 did not anticipate:

- A **Chrome-permission constraint the boundary contract's framing
  contradicted**: `activeTab` is granted only in direct response to the
  user invoking the extension itself (a toolbar-icon click), never as a
  side effect of a click on the webapp that merely causes a navigation to
  the employer's site. The extension cannot automatically probe or inject
  into the ATS page the instant it loads — probing requires the
  already-shipped toolbar-click gesture Sub-project 1's `popup_run_autofill`
  flow uses today. See Section 6.

- A **security-model gap between the accepted design and the shipped code**:
  the original handoff design spec's own Section 5.2 ("Per-handoff session
  authorization") specifies a short-lived session token distinct from the
  durable extension credential. The merged Sub-project 1 code never built
  this — every call (`startSession`, `sendEvent`, `confirmSubmission`) still
  presents the long-lived durable credential, and every corresponding server
  route uses the same `get_extension_scope` dependency
  (`webapp/api/handoff.py:61-72`), keyed purely on that credential. This is
  not a new requirement being introduced here; it is closing a gap against
  a decision the project already made and never implemented.
- A **structural circular dependency** in the naive "snapshot-first" framing:
  the privacy contract (original spec Section 17 — never release more than
  the current page's detected fields need) means the server cannot know
  which candidate paths to release until *after* the page has been scanned
  and its fields classified. But Section 9's framing implied fetching the
  snapshot as an early, independent step. This spec resolves that ordering
  explicitly (Section 6 below).

## 2. Verified current state

Investigated at `master` commit `0c8f0a9703ca20c61ca35642b8ff3f63a1c5cba1`
(PR #25, extension pairing, merged).

### 2.1 Session authorization today — durable credential only, everywhere

- `extension/src/background/server-client.ts:33-36`: `startSession` returns
  `Promise<{ id: string }>` — an id only.
- `sendEvent` (line 49) and `confirmSubmission` (line 66) both call
  `this.headers()` (lines 11-15), sending `X-Handoff-Credential: <durable
  credential>` — the same credential used for every other call.
- `webapp/api/handoff.py`: `post_start_session` (108-111),
  `get_discover_sessions` (119-123), `post_record_event` (134-138),
  `get_replay_events` (148-152), `post_confirm_submission` (160-164) all use
  `scope: AccountScope = Depends(get_extension_scope)` — identical
  dependency, no session-token-based dependency exists anywhere.
- `get_extension_scope` (`webapp/api/handoff.py:61-72`) takes
  `x_handoff_credential: str = Header(...)`, resolves it via
  `resolve_account_scope_from_extension_credential`
  (`webapp/services/handoff.py:84`) — keyed purely on the durable credential,
  no session-id parameter.
- `handoff_sessions` table (`webapp/persistence/migrations.py:232-244`,
  migration `005_handoff_sessions`): columns `id, account_id, workspace_id,
  pack_artifact_id, target_url, target_domain, ats_adapter_id,
  ats_adapter_version, started_at, status, user_confirmed_submitted_at`. No
  token/secret column, no `last_activity_at`, no expiry column.
- **The accepted design's own words**
  (`docs/superpowers/specs/2026-08-24-application-handoff-design.md`, Section
  5.2, "Per-handoff session authorization (short-lived, fine-grained)"):
  > "1. Before starting a handoff, the background worker presents its
  > durable extension credential to mint a **handoff session**... 2. The
  > server returns a `handoff_session_id`... **plus a short-lived session
  > token bound to it**. 3. All subsequent field-event/attachment-event/
  > confirmation calls for that application present the session token, not
  > the long-lived extension credential, limiting blast radius if one
  > session's token leaks."

### 2.2 Discovery — resumes by exact match, doesn't answer "which workspace"

- `GET /api/handoff/sessions/discover`
  (`webapp/api/handoff.py:119-131`), params `workspace_id`, `target_domain`
  (query), guarded by `get_extension_scope`.
- `discover_resumable_handoff_sessions`
  (`webapp/services/handoff.py:139-152`) calls
  `scope.require_job_workspace(conn, workspace_id)` then
  `find_in_progress_handoff_sessions`.
- `find_in_progress_handoff_sessions`
  (`webapp/persistence/handoff.py:110-123`): exact match on `account_id AND
  workspace_id AND target_domain AND status = 'in_progress' ORDER BY
  started_at DESC`.
- This endpoint requires the caller to already know `workspace_id` and
  `target_domain` — it resumes a known session, it does not solve "which
  workspace does this employer tab correspond to." That problem is what
  Section 5 (pending-context bridge) below solves.

### 2.3 Attachment handling — implemented, unused, no session-token dependency

- `extension/src/background/attachment.ts` (71 lines): two exported
  functions, neither called from anywhere (confirmed — no importer exists).
  - `fetchExactPackDocument(baseUrl, credential, workspaceId,
    packArtifactId, kind)` — calls the existing render route with the
    session's pinned `pack_artifact_id`, returns `RenderedDocument {kind,
    filename, mimeType, sha256, byteLength, bytes}`.
  - `buildAttachmentEventPayload(document, packArtifactId, rendererVersion,
    outcome)` — builds an `upload_selected`/`upload_confirmed`/
    `upload_failed` event payload. Outcome vocabulary is `selected |
    upload_confirmed_by_adapter | rejected | unknown` (per original spec
    Section 7) — never a boolean, since a file-input write doesn't prove an
    async ATS upload finished.

### 2.4 CandidateSnapshot — no server-side type, no per-workspace endpoint, and the original spec forbids one

- No Python-side `CandidateSnapshot` type exists (grepped `webapp/` for the
  literal string — zero hits). The concept exists only inside
  `application_pack_contract.py`'s pack schema as the `candidate_snapshot`
  key of a full Application Pack.
- TS type `extension/src/adapters/types.ts:25-36` is a hand-written, partial
  mirror (`identity`, `contact`, `employment[]`) — narrower than the full
  pack's `_CANDIDATE_KEYS`.
- **Original spec, Section 17, "Minimum data released to the extension"
  (structural non-goal, quoted):**
  > "a session-minting response contains only the specific `candidate_snapshot`
  > paths the matched adapter's field rules actually need for the current
  > page... There is no endpoint that returns 'the current profile' or 'the
  > current pack' in bulk to the extension; this is a **structural
  > non-goal**."
- This spec does **not** build a "CandidateSnapshot for a workspace"
  endpoint — that phrasing implies "whatever the workspace's current state
  is," which both re-opens the current-vs-confirmed-pack drift problem
  Sub-project 1 was built to avoid, and directly contradicts Section 17.
  Section 7 below designs a narrower, session-pinned, page-need-scoped
  replacement instead.

### 2.5 The three manual-test keys — one is real production logic

`extension/src/background/index.ts:15-20`:
```typescript
const MANUAL_TEST_SNAPSHOT_KEY = "handoff_manual_test_snapshot";
const MANUAL_TEST_SESSION_ID_KEY = "handoff_manual_test_session_id";
const MANUAL_TEST_CLIENT_SEQUENCE_KEY = "handoff_manual_test_client_sequence";
```
`ensureRouter()` (48-64) / `persistClientSequence()` (66-72): the third key
persists `{sessionId, sequence}` to survive an MV3 service-worker restart
(workers die after ~30s idle) and resume `MessageRouter`'s `clientSequence`
counter correctly. `MessageRouter.route()`
(`extension/src/background/message-router.ts:39-53`) stamps `clientSequence`
on every outgoing event; `DurableEventQueue`'s flush path
(`extension/src/background/event-queue.ts:32`) sorts pending events by that
number before sending. If the counter reset to 0 on a mid-session restart, a
post-restart event could sort ahead of or collide with pre-restart events,
corrupting flush order. **Only the storage-key name and manual-write
mechanism are test scaffolding; the underlying restart-recovery behavior is
real and must be preserved**, not deleted alongside the other two keys.

### 2.6 No canonical, trustworthy job/application URL exists in the data model today

- `handoff_sessions.target_url` (`webapp/persistence/migrations.py:237`) is
  a required parameter the **caller** supplies when starting a session — the
  server does not resolve it from any stored job-posting field. There is no
  `target_url`/`posting_url`/`application_url` column on `workspaces` at
  all.
- The closest thing that exists is `discovery_occurrences.source_url`
  (nullable, `webapp/persistence/migrations.py:587`), reachable from a
  workspace only via `discovery_candidates.promoted_workspace_id →
  discovery_candidates.canonical_occurrence_id → discovery_occurrences.source_url`
  (`webapp/services/discovery.py:448,473,480,484`). This chain exists **only
  for workspaces promoted from a discovery-pipeline candidate** — a workspace
  created any other way (manual entry, if that path exists elsewhere in the
  product) has no `source_url` anywhere, and even a promoted workspace's
  `source_url` is nullable (the column allows `NULL`).
- **This spec does not assume a trustworthy URL is always available.**
  Section 5.1 defines exactly what "trustworthy" means and what the UI does
  when it isn't.

### 2.7 `workspace_detail.html` — the Gate-4 confirmed-pack anchor

`webapp/templates/workspace_detail.html`, the `gate-four
document-finalization` panel: once `stages.review.artifact` exists, the
template already renders (excerpt):
```html
{% if stages.review.artifact %}<div class="pack-downloads">
  <p>Confirmed files remain immutable and downloadable:</p>
  <a class="button secondary" href="/api/workspaces/{{ workspace.id }}/application-pack/render/cv?pack_artifact_id={{ stages.review.artifact.id }}">Download CV</a>
  <a class="button secondary" href="/api/workspaces/{{ workspace.id }}/application-pack/render/cover_letter?pack_artifact_id={{ stages.review.artifact.id }}">Download Cover Letter</a>
</div>{% endif %}
```
This is the only place in the page a confirmed, immutable `pack_artifact_id`
is already exposed — the natural anchor for "Apply with extension," binding
to `stages.review.artifact.id` specifically (never a later/current pack).

## 3. Per-session authorization (closing the Section 5.2 gap)

### 3.1 New table: `handoff_session_tokens`

Migration `008_handoff_session_tokens`, following the `pairing_secrets`
pattern (`005_handoff_sessions`, `007_pairing_secrets`):

```sql
CREATE TABLE handoff_session_tokens (
    id TEXT PRIMARY KEY,
    handoff_session_id TEXT NOT NULL REFERENCES handoff_sessions(id),
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX idx_handoff_session_tokens_hash ON handoff_session_tokens(token_hash);
CREATE INDEX idx_handoff_session_tokens_session ON handoff_session_tokens(handoff_session_id);
```

- `token_hash` reuses `hash_pairing_secret`'s `sha256:<hex>` pattern — the
  plaintext token is never persisted, matching every other secret in this
  system.
- No separate expiry column on the token itself — a token is valid exactly
  as long as its session is `in_progress` and not expired (Section 4). A
  session transitioning to `expired`, `user_confirmed_submitted`, or any
  terminal state invalidates every token minted for it (checked by join at
  verification time, not by a duplicate expiry field to keep in sync).
- `revoked_at`: set if a session is explicitly abandoned; unused by this
  spec's own flows but reserved so a future "cancel this application"
  action has a place to write to without a schema change.

### 3.2 Minting and rotating a token

`POST /api/handoff/sessions` (session start) and `GET
/api/handoff/sessions/discover` (session resume) **both** require the
durable extension credential (`get_extension_scope`) and **both** mint a
fresh token row, returning the raw token in the response body as
`session_token` (alongside the existing `id`) exactly once — the raw
value is never persisted anywhere, only `hash_pairing_secret`'s hash of
it (Section 3.1), matching the pairing-secret pattern Sub-project 1
already established.

Resuming an existing session **rotates** its token: the discovery/resume
path revokes every prior token row for that `handoff_session_id`
(`UPDATE handoff_session_tokens SET revoked_at = ? WHERE
handoff_session_id = ? AND revoked_at IS NULL`) in the same transaction
that mints the new one, so a resumed session has exactly one live token
at a time — an old, possibly-leaked token from a prior browser session
for the same handoff stops working the moment the session is
re-authorized via the durable credential.

A token is invalid the instant any of the following is true: it was
explicitly revoked (rotation, above), its session is no longer
`in_progress` (Section 3.1's join-based check), or its session has
expired (Section 4).

`post_start_session` and `get_discover_sessions` remain on
`get_extension_scope` (the durable credential) — this is exactly the
"presenting the durable credential to mint a session" step Section 5.2
describes.

### 3.3 New dependency: `get_session_scope`

```python
def get_session_scope(
    x_handoff_session_token: str = Header(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> SessionScope:
    token_hash = hash_pairing_secret(x_handoff_session_token)
    row = conn.execute(
        "SELECT hst.*, hs.account_id, hs.workspace_id, hs.pack_artifact_id, "
        "hs.status, hs.last_activity_at "
        "FROM handoff_session_tokens hst "
        "JOIN handoff_sessions hs ON hs.id = hst.handoff_session_id "
        "WHERE hst.token_hash = ? AND hst.revoked_at IS NULL",
        (token_hash,),
    ).fetchone()
    if row is None:
        raise HTTPException(401, "session token not recognized")
    if row["status"] != "in_progress":
        raise HTTPException(401, "session is not active")
    if _is_expired(row["last_activity_at"]):
        raise HTTPException(401, "session has expired")
    return SessionScope(
        account_id=row["account_id"], handoff_session_id=row["handoff_session_id"],
        workspace_id=row["workspace_id"], pack_artifact_id=row["pack_artifact_id"],
    )
```

`sendEvent`, `confirmSubmission`, `get_replay_events`, and the new snapshot
endpoint (Section 7) all switch to `Depends(get_session_scope)`. Only
`post_start_session` and `get_discover_sessions` keep `get_extension_scope`.

### 3.4 Extension side

`ServerClient.startSession` return type becomes `Promise<{ id: string;
sessionToken: string }>`. `sendEvent`, `confirmSubmission`, and the new
snapshot-fetch method build headers as `{ "X-Handoff-Session-Token":
sessionToken, "Content-Type": "application/json" }` instead of calling
`this.headers()`. The durable credential (`X-Handoff-Credential`) is used
**only** for `startSession`/discovery — never again for the lifetime of
that session.

## 4. Session expiry: 2 hours of inactivity

### 4.1 Schema

Migration (can be combined with `008_handoff_session_tokens` or its own
`009_handoff_session_activity`):
```sql
ALTER TABLE handoff_sessions ADD COLUMN last_activity_at TEXT;
UPDATE handoff_sessions SET last_activity_at = started_at WHERE last_activity_at IS NULL;
```

- `HANDOFF_SESSION_INACTIVITY_TIMEOUT = timedelta(hours=2)` — one internal
  constant in `webapp/services/handoff.py`, not user-configurable, not a
  settings-table row. Two hours is long enough for a real application with
  interruptions, short enough that an abandoned handoff isn't resumable
  indefinitely.
- `last_activity_at` is refreshed on every successful `sendEvent`,
  `confirmSubmission`, and snapshot-fetch call (i.e., every
  `get_session_scope`-gated call that succeeds updates it) — not on
  `discover` alone, since a discovery query without a subsequent action
  shouldn't count as activity.
- `find_in_progress_handoff_sessions` (discovery) adds `AND
  last_activity_at > ?` (now - 2h) to its `WHERE` clause — an expired
  session is silently excluded from discovery results, not surfaced as an
  error. If nothing is found, discovery returns empty and the extension
  starts a fresh session (Section 6), exactly like the "no resumable
  session" case today.
- No background sweep job needed — expiry is enforced lazily at query time
  (both `get_session_scope` and discovery), matching this codebase's
  existing pattern (`pairing_secrets` expiry is checked the same way, not
  swept).

## 5. Gate-4 "Apply with extension" + pending-context bridge

### 5.1 CTA eligibility: confirmed pack + trustworthy target URL

**Decision:** the CTA is actionable only when **both** hold:

1. `stages.review.artifact` exists and is the workspace's current,
   confirmed Gate-4 pack (the same condition the existing "Confirmed files
   remain immutable and downloadable" block already checks — the CTA lives
   inside that same `{% if stages.review.artifact %}` block, never
   rendered outside it).
2. `workspace.source_url` (or equivalent join result, computed server-side
   and passed into the template context) is present and passes a basic
   sanity check (`http(s)://` scheme, non-empty host).

When (2) is not met (confirmed pack exists but no trustworthy URL):

- The CTA renders in a disabled state with the label "Apply with extension
  (no application link found)" and a `title` attribute explaining why.
- No new URL-entry UI is added in this sub-project — a user whose workspace
  lacks a discovered `source_url` continues using the manual download
  links already in the Gate-4 panel and applies without the extension, same
  as today. Building a "paste the job URL yourself" fallback is explicitly
  **out of scope** (see Section 9) — it's a real gap but solving it well
  (validation, re-use across future sessions, whether it should persist to
  the workspace row) deserves its own scoped decision, not a rushed
  add-on here.
- Server-side resolution: `webapp/services/workspaces.py` (or wherever
  `workspace_detail` assembles its template context) gains a lookup that
  joins `workspaces.id → discovery_candidates.promoted_workspace_id →
  discovery_candidates.canonical_occurrence_id →
  discovery_occurrences.source_url`, returning `None` if any link in that
  chain is absent — exposed to the template as `workspace.source_url`
  (or a dedicated `apply_target_url` context key, final naming decided at
  implementation time to match the existing context-building function's
  conventions).

### 5.2 Gate-4 markup addition

In the existing `<div class="pack-downloads">` block (Section 2.7), add:
```html
<button class="button apply-with-extension"
        data-workspace-id="{{ workspace.id }}"
        data-pack-artifact-id="{{ stages.review.artifact.id }}"
        data-target-url="{{ apply_target_url }}"
        {% if not apply_target_url %}disabled title="No application link found for this job"{% endif %}>
  Apply with extension
</button>
```
Binds to `stages.review.artifact.id` specifically — the exact confirmed
pack this Gate-4 panel is already displaying, never a later/current one
(same invariant the download links already honor).

### 5.3 Loopback content-script bridge

New `content_scripts` entry in `extension/manifest.json` (currently `[]`),
scoped **only** to the webapp's own origin:
```json
"content_scripts": [
  {
    "matches": ["http://127.0.0.1:8420/*"],
    "js": ["content-bridge/index.js"],
    "run_at": "document_idle"
  }
]
```
This adds no new host-permission surface beyond what Sub-project 1 already
granted (`host_permissions: ["http://127.0.0.1:8420/*"]`) — a
`content_scripts` match on an origin the extension can already `fetch()`
does not widen what the extension can reach, only where a script is
auto-injected.

New file `extension/src/content-bridge/index.ts` (a **separate** entry
point from the existing employer-page `extension/src/content/index.ts` —
different purpose, different injection trigger, not reused): listens for a
click on `.apply-with-extension`, reads its four `data-*` attributes, and
sends `chrome.runtime.sendMessage({ type: "set_pending_handoff_context",
workspaceId, packArtifactId, targetUrl, requestedAt: Date.now() })`. The
click handler does **not** call `preventDefault()` — the button is a plain
link-shaped element (or the page navigates via the button's own
click-to-navigate JS already present for other Gate-4 actions); the
ordinary navigation to `targetUrl` proceeds immediately after the message
is sent, matching "the ordinary link then opens the employer page" from
the approved decision.

### 5.4 Background worker: `chrome.storage.session`, not `chrome.storage.local`

`background/index.ts` gains a `set_pending_handoff_context` message
listener (checked before the existing `popup_run_autofill` branch, same
shape-guard style) that writes to `chrome.storage.session` (not `.local` —
this is deliberately not durable across a full browser restart, unlike
`CredentialStore`, since a pending "about to apply" context that survives a
restart days later is stale by construction):
```typescript
interface PendingHandoffContext {
  workspaceId: string;
  packArtifactId: string;
  targetUrl: string;
  requestedAt: number;
}
const PENDING_CONTEXT_KEY = "handoff_pending_context";
const PENDING_CONTEXT_TTL_MS = 5 * 60 * 1000; // 5 minutes
```
Consume-on-**success**, not consume-on-read: `runAutofillOnTab`'s
replacement (Section 6.2) reads this key without clearing it, and clears it
(`chrome.storage.session.remove(...)`) **only after** a session has been
successfully discovered/resumed or started (Section 6.2 step 6) using that
context. If discovery/start fails (network error, server error, an
unexpected 4xx), the context is left in place — the user can retry the
toolbar click within the TTL window without having to go back and click
"Apply with extension" again. If the stored `requestedAt` is older than
the 5-minute TTL when read, it is treated as absent (not used, and cleared
at that point, since a genuinely stale context is not worth keeping around
for a future retry) — long enough for "click Apply, switch tabs, the
employer page finishes loading, click the toolbar icon," short enough that
a context from an hour-old abandoned click never silently reappears.

`chrome.storage.session` requires no new manifest permission (it's part of
the existing `storage` permission already granted).

## 6. Employer-page flow: the toolbar click is the activeTab gesture, not navigation

### 6.1 Why this cannot be automatic

Chrome grants `activeTab` only in direct response to the user invoking the
extension itself (a toolbar-icon click, a context-menu action) — never as
a side effect of a click on an unrelated page, even one that causes a
navigation to a new origin. Clicking "Apply with extension" on the webapp
is a click on `127.0.0.1:8420`, not an invocation of the extension; it
grants nothing on whatever tab the resulting navigation lands on. **The
design must not have the extension automatically probe or inject into the
employer/ATS page the moment it loads.** The employer page only becomes
accessible to `chrome.scripting.executeScript` once the user separately
clicks the extension's own toolbar icon on that tab — exactly the
`activeTab` grant Sub-project 1's `popup_run_autofill` flow already
depends on for its existing (manual-test-bridge-driven) autofill.

Section 9 of the prior spec (superseded here) implied "receive session
context, then supply a snapshot, then run autofill" as steps that follow
automatically from navigation. The corrected flow instead makes the
already-shipped toolbar-click step do all of the newly-designed work, in
place of what it does today.

### 6.2 Corrected flow

1. **User clicks "Apply with extension"** on the webapp (Section 5) — the
   pending context is stored via the loopback bridge (Section 5.3-5.4);
   the ordinary link then navigates to the employer's application page in
   a new/existing tab. **No `activeTab` access exists yet at this point.**
2. **The employer page loads normally** — the extension does nothing to
   it. No content script is injected, no probe runs, nothing is read from
   or written to that tab.
3. **User clicks the JobSearch extension's toolbar icon**, on that
   employer tab. This is the same user gesture Sub-project 1's popup
   already uses, and it is what grants `activeTab` for this specific tab.
   The popup renders its existing paired state ("Paired ✓ / Run autofill
   on this tab" — no UI change needed here); clicking "Run autofill" sends
   the existing `popup_run_autofill` message, exactly as today.
4. **Active-tab probe**: only now, inside the `activeTab`-granted
   `runAutofillOnTab` replacement, inject the existing content-script
   bundle in a first pass that detects the matching adapter/version and
   scans+classifies the page's fields (`Adapter.scan`/`classify`, already
   implemented, currently only exercised by the adapter-bundle test
   harness) — **no candidate data is sent to the page at this step**.
5. **Background reads `PendingHandoffContext`** (Section 5.4), without
   clearing it yet (Section 5.4's consume-on-success rule) — if absent or
   expired, autofill cannot proceed for this click (surfaced as a
   popup/console message, exact UX TBD at implementation time — this is
   the real "user clicked the toolbar icon without having clicked Apply
   first" fallback case, not expected to be common but must not silently
   no-op confusingly).
6. **Discover-or-start**: background calls `GET
   /api/handoff/sessions/discover` (durable credential) with
   `PendingHandoffContext.workspaceId` + the tab's `target_domain`; if a
   resumable session comes back, mint/refresh its token (Section 3.2) and
   use it; otherwise call `POST /api/handoff/sessions` (durable credential)
   with the pending context's `workspaceId`/`packArtifactId`/`targetUrl` +
   the probe's detected `atsAdapterId`/`atsAdapterVersion`, receiving a
   fresh `{id, sessionToken}`. **Only on success here does the background
   clear `PendingHandoffContext`** (Section 5.4).
7. **Request the projected snapshot** (Section 7) using the session token,
   passing the exact set of `normalizedFieldType`s the probe's classify
   pass determined are `autofill`/`suggest` (never `ask`/`never` fields —
   those are never worth requesting candidate data for at all).
8. **Inject the projection** into the same isolated-world `globalThis` key
   the manual-test bridge used (`INJECTED_SNAPSHOT_KEY`,
   `snapshot-source.ts` — unchanged consumer, only the producer changes),
   then run the existing fill logic exactly as today.
9. **Attachment** (Section 8) happens after step 8 succeeds, using the
   same session token.

This is "Apply with extension → pending context stored → ATS page opens →
user clicks the toolbar icon → that click grants activeTab → probe →
discover/resume/start → snapshot projection → autofill" — the toolbar
click is not an incidental detail, it is the specific Chrome-permission
gesture that makes every step after it legal, and must appear explicitly
in the acceptance criteria (Section 12) as a real user action, not an
implementation footnote.

## 7. Session-scoped, exact-pack-pinned snapshot projection

### 7.1 Endpoint

```
POST /api/workspaces/{workspace_id}/handoff/sessions/{session_id}/snapshot
```
guarded by `Depends(get_session_scope)` (Section 3.3) — the path's
`workspace_id`/`session_id` are cross-checked against the resolved
`SessionScope.workspace_id`/`handoff_session_id` (a token for session A
must never be usable to fetch session B's projection, even for the same
account).

Request body:
```json
{ "normalized_field_types": ["name", "email", "employment[0].employer", ...] }
```
This field carries **only** the closed, existing normalized-field-type
vocabulary the adapters/classify layer already defines (the same strings
`extension/src/adapters/*` already produce) — never an arbitrary
candidate-JSON path, never free-form client input echoed into a lookup.
The server treats this list purely as a *filter* over its own
server-owned mapping (Section 7.2 step 3), never as an instruction for
where to look inside `candidate_snapshot` — an unrecognized string in the
list is silently ignored (not an error, since a slightly-mismatched
adapter version should degrade to "fewer fields," never fail the whole
request), and it can select from, but never expand, what the mapping
already permits.

### 7.2 Server behavior

1. Resolves `SessionScope.pack_artifact_id` — **the exact, immutable pack
   this session was started against**, not "the workspace's current pack."
   A session started before a newer pack was confirmed continues to see
   its own original pack for its entire lifetime; this is what "exact
   handoff session pinned to its `pack_artifact_id`" means and is what
   prevents the silent-current-pack-drift Sub-project 1's whole design was
   built to avoid.
2. Loads that pack artifact's `candidate_snapshot`
   (`application_pack_contract.py`'s existing shape — reused, not
   redefined).
3. Resolves each requested (and recognized) `normalized_field_type` through
   a **closed, server-defined mapping** —
   `NORMALIZED_FIELD_TYPE_TO_CANDIDATE_PATH: dict[str, str]` (or an
   equivalent explicit table), hand-maintained in
   `webapp/services/handoff.py` alongside the existing normalized-field-type
   vocabulary, listing every permitted `normalized_field_type → exact
   candidate_snapshot path` pair. **This mapping, not the request body, is
   what determines which paths can ever be returned** — the request body
   only selects a subset of what the mapping already allows; it cannot
   cause a path outside the mapping to be read or returned under any
   input. This is what makes "cannot become a bulk dump" a structural
   property of the code (a request can never name a path the mapping
   doesn't already know about) rather than a rule enforced only by callers
   behaving themselves.
4. Returns `{ "snapshot": { <only the requested paths> } }` — same response
   shape for a brand-new or a resumed session, since both resolve through
   the identical `SessionScope.pack_artifact_id` → pack-artifact lookup;
   "resumed" changes nothing about this endpoint's behavior, only how the
   session/token were obtained (Section 6.2 step 6).
5. Never returns a path that wasn't requested, and never returns the full
   `candidate_snapshot` object even if every field happened to be
   requested — the response is always built as an explicit per-path
   projection, so "request everything" and "a bulk profile dump" remain
   observably different code paths even if they'd produce the same JSON
   today (keeps Section 17's structural non-goal enforced by construction,
   not by convention).

## 8. Attachment integration

`fetchExactPackDocument`/`buildAttachmentEventPayload` (Section 2.3,
unchanged) get real call sites for the first time:

- **At session start/resume** (Section 6.2 steps 6-7, immediately after a
  session token exists): background calls `fetchExactPackDocument` for both
  `cv` and `cover_letter`, using `SessionScope.pack_artifact_id` (via the
  session, not a fresh workspace lookup) — same exact-pack pinning as the
  snapshot projection.
- **Immediately before the content script performs the file-input write**
  (per original spec Section 7's "fetched fresh... again right before
  upload" — no caching across that gap, since this is a short in-memory
  gap within one autofill run, not a persistence decision).
- The content script's actual file-input interaction and outcome
  classification (`selected | upload_confirmed_by_adapter | rejected |
  unknown`) is new work — no existing DOM-interaction code for this exists
  yet (confirmed: `attachment.ts` is pure data-fetching/payload-building
  only, no DOM code).
- Outcome sent via `buildAttachmentEventPayload` → the existing
  `sendEvent`/`DurableEventQueue` path (Section 3.4's session-token
  headers now apply here too).

## 9. Manual-test bridge removal

- `MANUAL_TEST_SNAPSHOT_KEY` and `MANUAL_TEST_SESSION_ID_KEY`: deleted
  entirely, along with `getManualTestValue` if nothing else in the file
  still needs its generic shape (check at implementation time — `snapshot`
  and `session_id` were its only two callers per the current file).
- `MANUAL_TEST_CLIENT_SEQUENCE_KEY`: **renamed and reworked**, not deleted.
  Becomes a production, session-scoped key (e.g.
  `handoff_client_sequence:<sessionId>`, or a `chrome.storage.session`
  entry keyed by the real `handoff_session_id` rather than a manual-test
  flag) written by the same `persistClientSequence`/`ensureRouter`
  restart-recovery logic (Section 2.5) — the *mechanism* is unchanged and
  must not regress; only its trigger (a real session from Section 6, not a
  manually-set storage key) changes.

## 10. Non-goals (this sub-project)

- General auth/session system (still out of scope — original spec Section
  18).
- A "paste the job URL yourself" fallback for workspaces with no
  discoverable `source_url` (Section 5.1) — real gap, deliberately
  deferred rather than rushed. **This release does not support
  "Apply with extension" for manually-created or otherwise
  non-discovery-origin workspaces that lack a trustworthy application
  URL** — such workspaces keep using the existing manual download links
  and apply without the extension, exactly as today; URL capture/paste is
  tracked as explicit follow-up scope, not silently promised here.
- Any change to autofill classification, the safe-catalog boundary, or
  adapter rule logic.
- Credential revocation UI.
- Multi-account-per-browser-profile UX.
- Extension packaging/Web Store distribution.
- A background sweep/cron for session expiry — enforced lazily at query
  time only (Section 4.1).
- Session-token revocation UI/flow beyond the reserved-but-unused
  `revoked_at` column.

## 11. Proposed task breakdown

1. **Server: session-token foundation** — `008_handoff_session_tokens`
   migration, `get_session_scope` dependency, `startSession`/discovery
   response changes to mint and return `session_token`.
2. **Server: session expiry** — `last_activity_at` column/migration, the
   2-hour constant, `get_session_scope`'s expiry check, discovery's
   activity filter, activity-refresh on every session-scoped call.
3. **Server: exact-pack snapshot projection endpoint** (Section 7) — new
   route, pack-artifact-pinned projection logic, tests for "never returns
   an unrequested path" and "resumed session sees its original pack, not a
   newer one."
4. **Server: Gate-4 "Apply with extension" CTA eligibility + trustworthy-URL
   resolution** (Section 5.1-5.2) — the confirmed-pack + `discovery_candidates →
   discovery_occurrences.source_url` join, template context wiring, the
   disabled-state UI for a confirmed pack with no trustworthy URL.
5. **Extension: loopback content-script bridge** (Section 5.3) — new
   `content_scripts` manifest entry, `content-bridge/index.ts`,
   `set_pending_handoff_context` message.
6. **Extension: `chrome.storage.session` pending-context store** (Section
   5.4) — background listener, TTL/consume-on-use logic.
7. **Extension: probe → discover/resume/start orchestration** (Section
   6.2) — replaces `runAutofillOnTab`'s current manual-key reads (invoked
   by the same `popup_run_autofill`/toolbar-click gesture as today, not a
   new trigger) with the real discover/start calls and session-token
   handling.
8. **Extension: projected-snapshot fetch + injection** — wires the probe's
   classify output into the Section 7 endpoint call and the existing
   `INJECTED_SNAPSHOT_KEY` injection path.
9. **Extension: attachment integration** (Section 8) — real call sites for
   `fetchExactPackDocument`/`buildAttachmentEventPayload`, the new
   file-input-interaction/outcome-classification content-script logic.
10. **Extension: remove `MANUAL_TEST_SNAPSHOT_KEY`/`MANUAL_TEST_SESSION_ID_KEY`,
    rework `MANUAL_TEST_CLIENT_SEQUENCE_KEY`** (Section 9).
11. **Build/manifest wiring** for the new `content-bridge` entry point
    (mirrors Sub-project 1's Task 8 popup-bundling pattern).
12. **Tests**: unit coverage per new module (session tokens, expiry,
    snapshot projection, pending-context TTL logic, token rotation on
    resume); a Playwright acceptance suite extending Sub-project 1's
    pattern — click "Apply with extension" on a fixture workspace, land on
    a real ATS fixture page, **then drive the extension's popup
    `popup_run_autofill` action on that tab exactly as a real toolbar
    click would** (the same trigger Sub-project 1's own acceptance suite
    already exercises), confirm the session then discovers/resumes/starts,
    the projected snapshot arrives, attachment fetch succeeds, and no
    manual storage step is used anywhere; a real
    resume-after-service-worker-restart test exercising the reworked
    sequence-recovery key.
13. **Manual Chrome lifecycle sign-off**, mirroring Sub-project 1's gate:
    real click of "Apply with extension" in a real browser, landing on the
    real employer/ATS page, **then a real click of the extension's toolbar
    icon on that tab** (this step must be performed, not skipped or
    assumed — it is the actual `activeTab` grant), confirming the whole
    toolbar-click→probe→session→snapshot→autofill→attachment chain, plus
    the disabled-CTA case for a workspace with no discoverable URL.

## 12. Expected acceptance criteria

**Automated:**
- New migration applies/rolls back cleanly (matching the existing
  migration-rollback test pattern from Sub-project 1).
- `get_session_scope` rejects: an unrecognized token, a token for a
  non-`in_progress` session, an expired session's token; accepts a valid
  token and refreshes `last_activity_at`.
- Snapshot endpoint: returns only requested+supported paths; a resumed
  session's projection is provably drawn from its original
  `pack_artifact_id` even after a newer pack exists for the same workspace
  (regression test for the drift this whole design avoids).
- Discovery excludes a session whose `last_activity_at` is older than 2
  hours.
- Pending-context TTL: a context older than 5 minutes is treated as absent
  when read.
- Playwright acceptance suite (Section 11 item 12) passes end-to-end
  against the real production build, extending
  `tests/webapp/test_extension_pairing_acceptance.py`'s established
  pattern.

**Manual:**
- Real "Apply with extension" click → real employer fixture page → **real
  toolbar-icon click on that tab** → visible autofill, with no DevTools
  step anywhere, in a real Chrome window.
- The disabled-CTA case is visually confirmed for a workspace lacking a
  discoverable URL.
- Retrying the toolbar click after a deliberately-forced discover/start
  failure (e.g. server briefly stopped) still succeeds once the server is
  back — confirming the pending context was not consumed by the failed
  attempt (Section 5.4).
- A real MV3 service-worker restart mid-session (idle the extension ~30s+
  during an in-progress application) does not corrupt event ordering —
  the reworked sequence-recovery key is exercised for real, not only
  unit-tested.

## 13. Explicitly resolved decisions (superseding the prior boundary contract)

| Prior open question | Resolution |
|---|---|
| **Automatic post-navigation probing vs. Chrome's `activeTab` model** | **Not automatic.** `activeTab` is granted only by a direct user gesture on the extension itself. The employer page is never touched until the user separately clicks the extension's toolbar icon on that tab — the same gesture Sub-project 1's `popup_run_autofill` already uses (Section 6). |
| Exact page/placement for "Apply with extension" | `workspace_detail.html`'s Gate-4 panel, inside the same `{% if stages.review.artifact %}` block as the existing download links, bound to `stages.review.artifact.id`; enabled only when that pack is confirmed/current *and* a trustworthy target URL exists (Section 5.1-5.2). |
| Webapp→extension context-handoff mechanism | Loopback content script + `chrome.storage.session` (not `.local`), 5-minute TTL, **consumed only after a session is successfully discovered/resumed/started** — not merely on read, so a failed attempt stays retryable (Section 5.3-5.4). |
| CandidateSnapshot-supply endpoint shape | Not a per-workspace endpoint — a session-token-gated, exact-pack-pinned projection over a **closed, server-owned normalized-field-type → candidate-path mapping**; the request selects a subset of that mapping and can never name a path outside it (Section 7). |
| Session-expiry duration | 2 hours of inactivity, one internal constant, `last_activity_at`-tracked (Section 4). |
| Per-session authorization | Restored per the original spec's own Section 5.2 — durable credential for start/discover only; a short-lived session token (raw returned once, hash-only persisted, bound to one `handoff_session_id`, invalidated on session expiry/termination, **rotated on every resume**) for everything else (Section 3). |
| Snapshot-before-page-scan ordering | Corrected to toolbar-click (activeTab) → probe → discover/resume/start → projected snapshot → inject → autofill (Section 6.2). |
| Manual-test key removal | Two keys deleted; the sequence-recovery key reworked into production form, not deleted (Section 9). |
| Trustworthy target URL source | `discovery_candidates → discovery_occurrences.source_url` join; CTA disabled with an explanatory title when absent; **workspaces without a trustworthy URL are not supported by this release** — a manual-URL-entry fallback is explicitly out-of-scope follow-up work (Section 5.1, Section 10). |
