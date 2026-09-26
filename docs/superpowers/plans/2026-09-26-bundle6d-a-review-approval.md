# Bundle 6D-A — Review & Approval: Phased Implementation Plan (draft)

**Spec:** `docs/superpowers/specs/2026-09-26-bundle6d-a-review-approval-design.md` (draft; this plan follows its approval).
**Base:** `master@fc316eec0050b6ae9deada2981089cbac1532adb`.
**Status:** phase-level plan for review. After the spec is approved, this becomes a task-level plan (files, interfaces, tests, commits per task) in one pass.

## Global constraints

- I-1: no action beyond what the user reviewed inherits approval, enforced by binding-hash equality, never by timestamps or flags.
- There are no browser automation, filling, submission or extension changes in 6D-A.
- 6C scheduling behaviour is unchanged. The only 6B change is the SUBMIT refusal (G2), which can only reduce.
- All new history tables are append-only (triggers). Current state is derived by `seq`, and canonical hashing uses 6B `canonical_hash`.
- Every phase is TDD with focused suites. The full suite runs in six chunks after Phase 1 (migration) and at the end.

## Phase 0 — Resolve the spec decisions

D1–D8 (spec §17) are confirmed or changed by the user. D1 (exact files required) and D3 (in-app editing in or out) change the scope of Phases 4 and 7.

## Phase 1 — Persistence and migration `019_review_approval`

- `application_approvals` (`scope` CHECK = 'FILL'), `application_review_events`, `review_deltas`, plus append-only triggers.
- If D3 is in scope, add `document_content_revisions` and the `user_edited` origin value. The latter needs a table rebuild of the `application_document_versions` CHECK, done as its own step with a data-preservation test.
- Persistence functions: record/list approvals, events and deltas; latest approval by `seq`; delta status from resolution events.
- **Tests:** migration on a fresh DB and on a pre-6D DB (built from `master@fc316ee` via `git archive`), re-run is a no-op, the triggers reject UPDATE/DELETE, and the scope CHECK rejects non-FILL. Then the full suite.

## Phase 2 — Pure review contract (`product/review_contract.py`)

- `approval_binding(reviewable) -> dict`, `binding_hash(binding) -> str`, `derive_review_state(snapshot) -> ReviewState(state, reasons, blocking)`, `derive_warnings(...)`, `provenance_label(...)`, `invalidation_reasons(old_binding, new_binding) -> list[str]`.
- There are no `webapp` imports (structural test).
- **Tests:**
  - table tests for every state and reason;
  - Hypothesis: the hash changes for every bound field and is unchanged for excluded ones (policy, capability, budget, fit);
  - adding a delta or warning never yields a more permissive state;
  - reason diffs are exact.

## Phase 3 — Reviewable application assembly (`webapp/services/review_application.py`)

- Build the reviewable application from current state:
  - job identity and posting content id;
  - apply target and provenance;
  - the current v2 selections and final pack;
  - document manifests;
  - the planned answer set (6B requirements resolved through approved-answer candidates, profile-evidence contact fields, resolved deltas);
  - claim provenance for AI documents;
  - warnings.
- v1 applications produce the "exact document files required" blocking issue.
- **Tests:** real-workflow fixtures (6C/v2 acceptance chains) covering every blocking issue and every warning class; provenance labels match recorded sources; an unknown source is BLOCKING.

## Phase 4 — Approval, revocation, expiry, invalidation and deltas (`webapp/services/review_approval.py`)

- The approval transaction (spec §9.2): stale-view refusal, blocking refusal, an in-transaction v2 Gate 4 of the exact selection revisions, the approval record and event, delta resolution, and a 6C wake.
- Revoke; TTL expiry (the `JOBSEARCH_REVIEW_APPROVAL_TTL_DAYS` setting, lower-only).
- The idempotent `APPROVAL_INVALIDATED` recorder (on read and in the 6C tick sweep as a reduce-only reconciliation).
- The delta intake service.
- **Bulk:** per-application independent transactions, a shared `batch_id`, and eligibility requiring `REVIEW_OPENED` at the current hash.
- **G2:** `request_grant(stage=SUBMIT)` refuses without a submission authorization (a 6B change with its own test).
- **Tests:**
  - every invalidation trigger in spec §18.4, both ways (material invalidates, non-material doesn't);
  - re-approval after deltas yields a complete superseding binding;
  - bulk partial outcomes;
  - concurrency (two approvals; approval vs document replacement), run 20×;
  - the SUBMIT refusal.

## Phase 5 — Editing paths and user-owned content

- **Answers:** edit or answer through the 6B approved-answer path with reach selection; accepting a proposal creates `USER_EDITED_PROPOSAL`; sensitive subjects are per application only.
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
  - a committed Playwright test covering review, preview, replace, answer edit, approve, stale-view refusal, bulk and no submit control;
  - the structural test extended to the 6D modules (no `pre_click_commit` / SUBMIT `request_grant`).

## Phase 7 — In-app content editing (only if D3 is in scope)

- Structured text edits of AI-generated CV/cover-letter content produce a user-owned `document_content_revisions` row, rendered by the existing renderer into a `user_edited` document version, which is selected explicitly.
- A regeneration never merges into user edits.
- **Tests:** round-trip edit → render → select → approve; regeneration leaves edits intact; provenance shows User supplied.

## Phase 8 — Final validation

- The acceptance journey on the real workflow: 6C prepares → review → edit → approve → an invalidation by replacement → re-approve → a delta intake → delta-only review → re-approve. There are zero FILL/SUBMIT grants, intents or attempts.
- Migrations (fresh and pre-6D), the full suite in six chunks, and a diff-boundary check against `master@fc316ee` (no fill, browser, submission or extension code).
- A single independent end-of-bundle review, then the PR.

## Out of scope (later bundles)

- **6D-B:** fill sessions, fill manifests from live pages, the submit firewall, FILL grants consuming approvals, and delta discovery.
- **6E-A:** `submission_authorizations`, the "Submit application" action, and the pre-click integration.
- **6E-B:** operating modes, autonomous submit activation, and warnings.
