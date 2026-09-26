# Bundle 6D-A — Review & Approval: Phased Implementation Plan (draft)

**Spec:** `docs/superpowers/specs/2026-09-26-bundle6d-a-review-approval-design.md` (draft; this plan follows its approval).
**Base:** `master@fc316eec0050b6ae9deada2981089cbac1532adb`.
**Status:** revised phase-level plan (it reflects the resolved D1–D8 and the five design corrections). After the spec is approved, this becomes a task-level plan (files, interfaces, tests, commits per task) in one pass.

## Global constraints

- I-1: no action beyond what the user reviewed inherits approval, enforced by binding-hash equality, never by timestamps or flags.
- I-2: approval is consent, not authority. Review, edit and approve work while paused or halted, and only FILL grants and execution are blocked.
- I-7: approval never creates or changes the pack it approves. Save changes creates the immutable v2 pack first.
- I-8: every known field is bound to `ANSWER` or `OMIT`. A filler never decides.
- D1: exact files are required and there is no v1 fallback. Enabling CV-v2 is a rollout requirement for 6D-A to be usable.
- There are no browser automation, filling, submission or extension changes in 6D-A.
- 6C scheduling behaviour is unchanged. The only 6B change is the SUBMIT refusal (G2), which can only reduce.
- All new history tables are append-only (triggers). Current state is derived by `seq`, and canonical hashing uses 6B `canonical_hash`.
- Every phase is TDD with focused suites. The full suite runs in six chunks after Phase 1 (migration) and at the end.

## Phase 0 — Decisions (resolved)

D1–D8 were resolved at review (spec §17). D3 in-app editing is deferred out of the core (see the follow-on at the end). D7 is an unconditional SUBMIT refusal with no placeholder authorization model.

## Phase 1 — Persistence and migration `019_review_approval`

- `application_approvals` (`scope` CHECK = 'FILL'), `application_review_events`, `review_deltas`, `application_field_dispositions` (CHECK `ANSWER`|`OMIT`), plus append-only triggers.
- Persistence functions: record/list approvals, events, deltas and dispositions; latest approval and current disposition by `seq`; delta status from resolution events.
- **Tests:** migration on a fresh DB and on a pre-6D DB (built from `master@fc316ee` via `git archive`), re-run is a no-op, the triggers reject UPDATE/DELETE, and the scope CHECK rejects non-FILL. Then the full suite.

## Phase 2 — Pure review contract (`product/review_contract.py`)

- `approval_binding(reviewable) -> dict`, `binding_hash(binding) -> str`, `component_hashes(binding) -> dict[str, str]`, `claim_provenance_hash(claims) -> str`, `binding_matches(approval, current) -> bool`, `approval_effective(snapshot) -> bool` (binding matches, no blocking issue, no unacknowledged ATTENTION, no open delta, not revoked, not expired), `derive_review_state(snapshot) -> ReviewState(state, reasons, blocking)`, `derive_warnings(...)`, `provenance_label(...)`, `invalidation_reasons(old_binding, new_binding) -> list[str]` (changed component names), `delta_only(previous, current, delta_keys) -> (bool, changed_components)`.
- There are no `webapp` imports (structural test).
- **Tests:**
  - table tests for every state and reason;
  - Hypothesis: the hash changes for every bound field and is unchanged for excluded ones (policy, capability, budget, pause, kill switch, fit);
  - Hypothesis: the claim-provenance digest changes when provenance, or a cited evidence item's content/basis hash, changes with identical document bytes and refs;
  - Hypothesis: a new BLOCKING/ATTENTION warning (e.g. from answer expiry) changes the binding, INFO doesn't, and acknowledging changes it again;
  - `approval_effective` implies `binding_matches`, never the reverse;
  - Hypothesis: `delta_only` is true exactly when all non-delta component hashes are equal;
  - adding a delta or warning never yields a more permissive state;
  - reason diffs are exact.

## Phase 3 — Reviewable application assembly (`webapp/services/review_application.py`)

- Build the reviewable application from current state:
  - job identity and posting content id;
  - apply target and provenance;
  - the current v2 selections and final pack;
  - document manifests;
  - the planned field set with dispositions (6B requirements resolved through approved-answer candidates, profile-evidence contact fields, resolved deltas; `OMIT` from dispositions);
  - claim provenance and its digest for AI documents;
  - warnings.
- v1 applications, and any application while CV-v2 is disabled, produce the "exact document files required" blocking issue. A selection that doesn't match the current pack produces "save your document changes" (no binding hash is offered).
- **Tests:** real-workflow fixtures (6C/v2 acceptance chains) covering every blocking issue and every warning class; provenance labels match recorded sources; an unknown source is BLOCKING.

## Phase 4 — Approval, revocation, expiry, invalidation and deltas (`webapp/services/review_approval.py`)

- **Save changes**: the user's v2 confirmation of the exact current selection revisions (the existing user Gate 4), creating the immutable pack and recording `PACK_CONFIRMED`.
- The approval transaction (spec §9.2) takes only `displayed_binding_hash`. It writes the approval record, the `APPROVED` event, and exactly one `DELTA_RESOLVED` per resolved delta. It never acknowledges warnings (acknowledgement is its own route). It refuses a stale view, blocking issues, or no pack for the current revisions. It never creates or changes a pack. There is no pause or kill-switch check, delta resolution is recorded, and the 6C queue is woken.
- Revoke; TTL expiry (the `JOBSEARCH_REVIEW_APPROVAL_TTL_DAYS` setting, lower-only).
- The idempotent `APPROVAL_INVALIDATED` recorder (on read and in the 6C tick sweep as a reduce-only reconciliation).
- The delta intake service.
- **Bulk:** per-application independent transactions, a shared `batch_id`, and eligibility requiring `REVIEW_PRESENTED` at the current hash, recorded only by the human-facing page route. A data API GET never counts.
- **G2:** first, a regression test pins the existing Phase 3 human extension handoff flow. Then `request_grant(stage=SUBMIT)` and the SUBMIT pre-click path refuse unconditionally (`submission_not_available`), with no placeholder authorization model.
- **Tests:**
  - every invalidation trigger in spec §18.5, both ways (material invalidates, non-material doesn't), including claim provenance at identical bytes;
  - approve succeeds while paused or halted;
  - the approve write contract by DB diff: normal vs delta re-approval, and never content or acknowledgements;
  - each `binding_matches`-but-not-effective case gives `NEEDS_REVIEW`;
  - delta-only vs full-section re-review (§11.1), and re-approval yields a complete superseding binding;
  - bulk partial outcomes;
  - concurrency (two approvals; approval vs document replacement), run 20×;
  - the SUBMIT refusal.

## Phase 5 — Editing paths and user-owned content

- **Answers:** edit or answer through the 6B approved-answer path with reach selection; accepting a proposal creates `USER_EDITED_PROPOSAL`; sensitive subjects are per application only.
- **Dispositions:** `ANSWER` / `OMIT` per field; `OMIT` refused for required fields; an optional field without a disposition blocks approval; the delta intake treats an `OMIT` field reported as mandatory as a delta.
- **Documents:** replace (the v2 upload) and explicit selection; the "newer AI draft available" ATTENTION warning, with Compare and **Use the new draft**.
- **Rules P1–P4 enforced:** no automated path moves a selection or supersedes a user answer.
- **Tests:** a 6C/pipeline rerun after user edits leaves selections and answers untouched; each edit invalidates an existing approval with the right reason and event.

## Phase 6 — API, UI and 6C integration

- The routes in spec §16.
- The Prepared Applications list, and the review page with preview, answers, provenance, warnings, **Approve for filling**, **Revoke** and bulk. There is no submit control.
- A server-side DOCX preview renderer (read-only structure/text from the stored bytes).
- 6C: PREPARED is labelled "Ready for review" and links to the review page. The dossier gets an Approvals section.
- **Tests:**
  - route ownership/404/409/422;
  - a committed Playwright test covering review, preview, replace + Save changes, answer edit, Leave blank, approve, stale-view refusal, the delta-only view, bulk and no submit control;
  - the structural test extended to the 6D modules (no `pre_click_commit` / SUBMIT `request_grant`).

## Phase 7 — Final validation

- The acceptance journey on the real workflow with CV-v2 enabled:
  1. 6C prepares.
  2. The user reviews, downloads, edits externally, replaces, runs Save changes, and sets Leave blank on an optional field.
  3. The user approves while automation is paused.
  4. A replacement invalidates the approval (full-section re-review), and the user re-approves.
  5. A delta intake arrives, the user sees the delta-only review, and re-approves.

  There are zero FILL/SUBMIT grants, intents or attempts.
- Migrations (fresh and pre-6D), the full suite in six chunks, and a diff-boundary check against `master@fc316ee` (no fill, browser, submission or extension code).
- A single independent end-of-bundle review, then the PR.

## Follow-on (deferred D3) — In-app content editing

A separable final phase or a small follow-on PR. It doesn't block the approval boundary.
- Structured text edits of AI-generated CV/cover-letter content produce a user-owned `document_content_revisions` row, rendered by the existing renderer into a `user_edited` document version. The migration rebuilds the origin CHECK with a data-preservation test.
- The version is then selected, saved (pack) and approved under the same rules. A regeneration never merges into user edits.

## Out of scope (later bundles)

- **6D-B:** fill sessions, fill manifests from live pages, the submit firewall, FILL grants consuming approvals, and delta discovery.
- **6E-A:** `submission_authorizations`, the "Submit application" action, and the pre-click integration.
- **6E-B:** operating modes, autonomous submit activation, and warnings.
