# Bundle 6C — Prepare: Design

**Status:** approved for implementation planning (2026-09-26).
**Branch:** `bundle6/6c-prepare` from `master@20979b9` (Bundle 6B merged).
**Builds on:** `docs/superpowers/specs/2026-09-24-bundle6b-autonomy-contract-design.md` (the "6B spec"). Every 6B invariant and user ruling A–P continues to apply; this spec only adds.

---

## 1. Purpose and scope

6C makes the system operate the **PREPARE** stage of the pipeline unattended, inside the standing authority the user has granted: discovered jobs are evaluated, eligible ones are promoted into applications, and applications are prepared through to an Application Pack — either genuinely system-confirmed, or stopped with an accurately derived reason the user can act on.

**The 6C boundary:**

```
user-triggered discovery run ─► discovered candidate ─► autonomous evaluation ─► screening
      ─► eligible candidate auto-promoted ─► PREPARE pipeline ─► PREPARED (system-confirmed)
                                                               └► NEEDS_USER | BLOCKED | OPERATIONAL_ERROR
existing application ─► explicit user enrolment ─► (same PREPARE pipeline)
```

**In scope:** candidate evaluation, screening and promotion; the scheduler (one engine, two drivers); the PREPARE step engine; mechanical review and system Gate 4; the exception inbox; eager answer propagation (wake-only); in-app notifications; recovery, retries and sweeps; budget admission and cost settlement; enrolment; the 6B carry-forwards (dossier pack/document hashes, expiry sweeps, authoritative pause checks); SQLite WAL.

**Out of scope for 6C:** scheduled or continuous discovery, portal polling or autonomous search reruns; automatic omission of judgment items; a restrictive "prepare-for-review only" preference; external notification channels (email, desktop); downward cost corrections; any FILL or SUBMIT work (6D/6E); a standing-policy promotion limit (operator setting instead, §11).

**Document path:** 6C uses whichever application-document path is currently enabled. CV Quality v2 is not a dependency (6B §1.2); at the 6C base commit (`master@20979b9`) the legacy path is current.

---

## 2. Principles and invariants

1. **Pre-authorized, not unrestricted** (6B). Nothing here grants capability. The PREPARE capability the user granted is the only standing authorization; operator settings and enrolment can only enable scheduling within it or lower it.
2. **Decisions at authorization boundaries, not per function call.** Autonomy decisions are recorded at the PREPARE boundaries defined in §6.5; internal pipeline calls are recorded as step attempts (§4.2), not decisions.
3. **State is derived, never asserted** (6B §11.1). What an application or candidate *is* comes from artifacts, decisions, blockers, screenings, promotions, enrolments and control events. The step-attempt log is observational only and is never consulted to decide progress.
4. **`PREPARED` is earned.** It is reached only when the existing Gate 4 conditions are genuinely satisfied. The scheduler never fabricates a Gate 4 confirmation, and system confirmations are never presented as the user's.
5. **Judgment stays with the user.** Only mechanically verifiable review items are system-confirmed; every judgment item stays undecided and becomes a `NEEDS_USER` question.
6. **Uncertainty becomes a question, structural impossibility does not.** A question is surfaced only when answering it could unlock progress.
7. **Fail closed on spend.** No unattended paid work without an explicit budget and a hard per-step cost envelope.
8. **Halt → explicit resume → fresh authorization** (6B §11.4). Clearing a halt condition never resumes anything.
9. **No FILL/SUBMIT.** 6C code never calls `request_grant` or `pre_click_commit`; unattended FILL and SUBMIT remain structurally impossible.
10. **"Current" is never defined by timestamp** (6B invariant 13): all current-state projections here are by `seq` or an explicit pointer.
11. **Append-only history, mutable coordination.** History/event tables are append-only (UPDATE/DELETE blocked by triggers). Queue/lease tables are mutable operational coordination state and never carry authority or lifecycle truth.

---

## 3. Components

| Module | Responsibility |
|---|---|
| `product/candidate_promotion.py` | Pure `evaluate_candidate_promotion(ctx) -> ScreeningResult`. Reuses 6B semantics (ceilings, standing-policy evaluation, controls, identity, limits) over candidate attributes. No IO. |
| `product/prepare_steps.py` | Pure `next_prepare_step(snapshot) -> Step`; pure `mechanical_review(item, profile) -> SystemReviewVerdict \| UNDECIDED`; pure `pack_revision(...)`, backoff and error-class helpers. |
| `webapp/persistence/autonomy_prepare.py` | Persistence for the 6C tables (§4). |
| `webapp/services/autonomy_scheduler.py` | `run_tick(conn, *, settings, now, rng, worker_id) -> TickReport`: sweeps, lease, derive, authorize, one step, record. |
| `webapp/services/autonomy_candidates.py` | Candidate enqueue, evaluation, screening, revalidating promotion, candidate exceptions. |
| `webapp/services/autonomy_prepare.py` | PREPARE step execution (reusing `understand_job`, `fit_job`, `generate_application_intelligence`), system review, system Gate 4, latch. |
| `webapp/services/autonomy_inbox.py` | Derived inbox, `propagate_answer`, `Notifier`, badge summary. |
| `webapp/autonomy_worker.py` | CLI driver: `python -m webapp.autonomy_worker --once \| --loop`. |
| `webapp/app.py` (lifespan) | In-app driver thread. |
| `webapp/api/autonomy.py` + templates | Inbox page and the 6C endpoints (§13). |

Small refactors in existing code (behaviour-preserving for existing callers):

- `promote_discovery_candidate` gains an inner `_promote_candidate_in_transaction(conn, ...)` that runs inside a caller-held transaction; the public function keeps its own transaction.
- `confirm_application_pack` gains an inner system variant taking `provenance="SYSTEM_AUTO_CONFIRMED"` and a system note; the user path is unchanged.
- `save_review_decision` / `record_review_decision` accept `decision_provenance` and `system_basis` (default `USER`, none).

---

## 4. Data model — migration `018_autonomy_prepare`

### 4.1 New append-only history/event tables

All have `seq INTEGER PRIMARY KEY AUTOINCREMENT`, a unique `id`, `created_at`, and UPDATE/DELETE-blocking triggers.

| Table | Columns (besides seq/id/created_at) | Purpose |
|---|---|---|
| `autonomy_candidate_screenings` | `account_id`, `search_workspace_id`, `candidate_id`, `discovery_run_id`, `discovery_fit_id`, `outcome` (`PROMOTE`\|`REQUIRE_USER`\|`BLOCK`\|`NOT_ELIGIBLE`\|`DENY`\|`DENY_TEMPORARY`), `reason_code`, `reasons_json`, `require_user_json`, `could_unlock` (0/1), `retry_at`, `input_fingerprint`, `authority_json` (deployment/account/workspace ceilings), `policy_version_hash`, `subject_policy_hash`, `engine_version` | Immutable statement of what the evaluator decided at that moment. Never updated. |
| `autonomy_candidate_promotions` | `screening_id`, `candidate_id`, `search_workspace_id`, `application_workspace_id`, `actor_type` (`SCHEDULER`\|`USER`), `actor` | Links a screening (or a user's Promote) to the application it created. |
| `autonomy_candidate_exceptions` | `account_id`, `search_workspace_id`, `candidate_id`, `screening_id`, `items_json` | A candidate-level question (no application exists, so never `application_blockers`). |
| `autonomy_candidate_exception_resolutions` | `exception_id`, `resolution` (`PROMOTE`\|`DISMISS`), `actor`, `reason` | Current resolution = latest by `seq`. |
| `autonomy_prepare_steps` | `attempt_id`, `subject_type` (`APPLICATION`\|`CANDIDATE`), `subject_id`, `step_kind`, `attempt_no`, `event` (`STARTED`\|`SUCCEEDED`\|`REUSED`\|`FAILED`\|`ABANDONED`), `input_fingerprint` (for candidate attempts it binds the admission inputs, §7.1), `authorization_decision_id` (required for `APPLICATION` attempts; **NULL for `CANDIDATE` attempts**, which have no application decision), `retry_request_id` (NULL outside an explicit retry cycle), `lease_generation`, `worker_id`, `run_id`, `artifact_refs_json` (ids + content hashes), `reservation_ids_json`, `cost_json` (reserved max, settled actual, source), `error_class`, `error_code`, `error_detail` | Observational attempt log: one `STARTED` row plus one terminal row per attempt. |
| `autonomy_review_latches` | `application_workspace_id`, `pack_revision`, `reason` (`EXPLICIT_REVIEW`\|`REOPENED_CONFIRMED`), `actor` | Human-review-required latch for one pack revision. |
| `autonomy_enrolments` | `account_id`, `application_workspace_id`, `action` (`ENROL`\|`UNENROL`), `actor_type` (`USER`\|`SCHEDULER`), `actor`, `reason` | Scheduling eligibility (distinct from authority). Current = latest by `seq`. |
| `autonomy_retry_requests` | `account_id`, `subject_type`, `subject_id`, `step_kind`, `input_fingerprint`, `actor` | The user's explicit "Retry". **One-shot:** it opens exactly one new retry cycle for that `step_kind + input_fingerprint` (§10.2); attempts in that cycle reference it. |
| `autonomy_notification_events` | `account_id`, `notification_key`, `kind` (`NEEDS_USER`\|`CANDIDATE_QUESTION`\|`PREPARED`\|`OPERATIONAL_ERROR`\|`BLOCKED`), `subject_type`, `subject_id`, `event` (`CREATED`\|`SEEN`\|`RESOLVED`), `detail_json` | Notification history; badge and unread state are derived. |

### 4.2 New mutable coordination table

`autonomy_candidate_queue` — `candidate_id` (PK), `account_id`, `search_workspace_id`, `next_eligible_at` (NULL = dormant), `lease_holder`, `lease_generation INTEGER NOT NULL DEFAULT 0`, `lease_expires_at`, `updated_at`. Fenced exactly like the application queue (§6.4). **No append-only trigger** — like 6B's `autonomy_queue_items`, it is operational coordination state only.

### 4.3 Additive changes to existing tables

- **`autonomy_queue_items`** (6B): `+ lease_generation INTEGER NOT NULL DEFAULT 0`. Lease fields remain mutable coordination state, never authority or lifecycle truth.
- **`review_decisions`**: `+ decision_provenance TEXT NOT NULL DEFAULT 'USER' CHECK (decision_provenance IN ('USER','SYSTEM_AUTO_CONFIRMED'))`, `+ system_basis_json TEXT` (required iff `SYSTEM_AUTO_CONFIRMED`: canonical reason code, the item's content hash, the pack revision). Existing rows are `USER`. Dispositions are unchanged — a system decision uses `acknowledged_and_proceed`.
- **`limit_reservations`** (6B): `+ subject_type TEXT` / `+ subject_id TEXT` (both NULL or both set; `subject_type IN ('APPLICATION','CANDIDATE')`) — attribution metadata only; the existing `(account, counter_name, window_key)` remains the single source of truth for every cap. `+ settled_amount TEXT`, `+ settlement_ref TEXT` with a partial unique index `WHERE settlement_ref IS NOT NULL` — exactly-once, immutable settlement (§11.3).

### 4.4 Connection settings (separate commit)

`webapp/persistence/db.connect` sets `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=30000`, so the UI, the in-app driver and a CLI worker can write concurrently. Committed on its own with a full-suite run.

---

## 5. Authority, enrolment and controls

- **Authority** is 6B's, unchanged: `min(deployment ceiling, ACCOUNT_MAX, WORKSPACE_CEILING)`, standing policy, kill switch/sentinel. PREPARE work requires `JOBSEARCH_AUTONOMY_MAX_CAPABILITY >= PREPARE` (the deployment ceiling defaults to `NONE`).
- **Operator execution gate:** `JOBSEARCH_AUTONOMY_SCHEDULER=1` (setting `autonomy_scheduler_enabled`, default off). Both drivers obey it; turning it on grants nothing, turning it off stops scheduler work. The CLI `--once` works whenever the gate is on, whether or not the web app runs.
- **Enrolment** is scheduling eligibility, not authority. Auto-promoted applications are enrolled `SCHEDULER/ENROL` in the promotion transaction. Existing applications are enrolled only by the user ("Prepare autonomously", `USER/ENROL`); a PREPARE ceiling never silently starts reworking older applications. "Stop preparing" writes `USER/UNENROL`.
- **Pause** (6B): application or search workspace, authoritative `is_paused`.
- **Halt** (6B): kill switch or `AUTONOMY_HALT`. Removing the halt condition or re-enabling the scheduler gate never resumes; only explicit resume-all, followed by fresh PREPARE authorization, does. Resume-all cannot override an active kill switch.
- **Race checks.** Pause, enrolment and halt are each checked at lease selection **and** immediately before a step starts. If any fails in between: release the lease, write no decision and no step attempt. An operation already in flight finishes and records its truthful outcome; no subsequent step is scheduled.

---

## 6. Queue and tick flow

### 6.1 Work sources

- **Candidates:** every `new`/`saved` candidate whose discovery run finished with status `completed` or `partial` (not `failed`), in an active search workspace whose effective ceiling is ≥ PREPARE, gets one `autonomy_candidate_queue` row.
- **Applications:** enrolled applications (§5) have an `autonomy_queue_items` row (`next_stage = PREPARE`).

### 6.2 Wake and dormancy

When there is nothing to do for an item, it goes **dormant** (`next_eligible_at = NULL`) — never immediately re-eligible. Only a wake event sets `next_eligible_at = now`, always in the same transaction as its cause: an answer or resolution (its own item and propagated siblings, §9.2), enrolment, a user Retry, resume/resume-all, a standing-policy save, or a capability change. Wakes cover both queues where relevant, and change scheduling state only.

**Material input changes also wake**, so a dormant item is re-derived whenever `next_prepare_step()` or screening could now produce work:

- a new current Evidence Profile snapshot wakes every enrolled application of the account (and candidate rows whose discovery fit depends on the profile);
- a material job or source change that can invalidate understanding, fit or intelligence wakes that enrolled application;
- a candidate source change, or a `completed`/`partial` discovery upsert that makes a candidate's discovery fit stale, wakes that candidate's queue row (creating it if eligible).

These wakes are written in the same transaction as the change that causes them.

### 6.3 `run_tick(conn, *, settings, now, rng, worker_id)`

A driver never overlaps its own `run_tick()` calls: it runs one tick to completion, then waits about `autonomy_tick_interval` before the next. Concurrency comes only from independent drivers/workers, coordinated by leases.

1. **Sweeps** (§10.4) — always, even while halted; each in its own short transaction; reduce-only.
2. **Gates.** If the scheduler gate is off or the account is halted, return after sweeps.
3. **Select work fairly.** Alternate 1:1 between due application items and due candidate items (applications first), up to `autonomy_max_items_per_tick`. Selection never scans the whole candidate backlog against application timestamps.
4. For each selected item:
   1. **Lease** (`BEGIN IMMEDIATE`): item is due, lease free or expired, not paused (`is_paused`), enrolled (applications). Set `lease_holder = worker_id`, increment `lease_generation`, `lease_expires_at = now + step hard timeout + margin`. Commit.
   2. **Derive** (read-only snapshot) the next step: `next_prepare_step` (§8.1) or the candidate step (§7). None → release lease and go dormant.
   3. **Authorize.** Application work: the PREPARE authorization (§6.5). Candidate work: the fresh candidate evaluation admission check before a paid `EVALUATE` (§7.1); screening and promotion are governed by §7.2–7.3. Not permitted → set the derived overlay (or record the screening), release, done.
   4. **Re-check** pause, enrolment, halt, scheduler gate (§5). Failed → release; no decision, no attempt.
   5. **Reserve** the step's hard-maximum cost if the step is cost-bearing (§11). Failed → `next_eligible_at = retry_at`, release.
   6. **Record `STARTED`** (short transaction), then **run the step with no write transaction held**, bounded by the step's hard timeout.
   7. **Finalize** in one short transaction guarded by `WHERE lease_holder = ? AND lease_generation = ? AND lease_expires_at > :now`: terminal attempt row, reservation settlement, outcome effects, `next_eligible_at` (now after success; backoff after failure; NULL when dormant), lease release. If the guard matches no row the worker lost its lease — retaken **or merely expired**: it writes nothing further (a worker whose lease expired cannot commit just because nobody retook it). The attempt's outcome is then settled by recovery (§10.1).

### 6.4 Leases

A lease lasts the step's hard timeout plus a margin (`autonomy_lease_margin`), so a live worker never loses its lease mid-step. Acquisition atomically sets the holder, increments `lease_generation` and sets the expiry; finalization requires the expected holder, the expected generation **and** an unexpired lease. Both queues are fenced identically. Expired leases are simply retaken; a leftover `STARTED` row never blocks recovery (§10.1).

### 6.5 PREPARE authorization at the boundary

Every application step is preceded by a pure 6B gate evaluation of the current context (`requested_stage = PREPARE`, `mode = LIVE`). A **still-valid recorded PREPARE authorization** is reused — no new decision row — only if all hold:

- the latest recorded PREPARE/LIVE decision for the application has the same requested stage and mode;
- its input fingerprint equals the current one;
- its policy, subject-policy and authority hashes equal the current ones;
- `now` is within its **validity horizon** — the earliest expiry of any time-sensitive input (budget window end, answer freshness expiry, grant or TTL boundaries). If a horizon cannot be computed reliably, there is no reuse.

Otherwise `decide_and_record` records a fresh decision. `now` is never added to the fingerprint. A step runs only if the (reused or fresh) decision is `grantable` for PREPARE with `effective_capability >= PREPARE` — `ALLOW(NONE)` is never permission. The attempt row records the `authorization_decision_id` used. Kill switch/halt and pause are always checked independently at execution time (§5).

---

## 7. Candidates: evaluation, screening, promotion

### 7.1 Evaluate (cost-bearing; only when needed)

If the candidate's discovery fit is missing or stale (`discovery_fit_is_stale`), a paid evaluation may run only after a fresh **candidate evaluation admission check** from authoritative state, immediately before the reservation:

- scheduler gate enabled;
- kill switch and sentinel clear;
- search workspace active and not paused (`is_paused`);
- current `min(deployment ceiling, ACCOUNT_MAX, search-workspace ceiling) >= PREPARE`;
- candidate still `new`/`saved`, from a `completed`/`partial` discovery run;
- strong durable identity;
- no existing application workspace or live/confirmed intent for that identity;
- an explicit LLM budget and an `EVALUATE` cost envelope, with budget available.

This is not the promotion screening and records no application decision (none can exist yet). The attempt's `input_fingerprint` binds these admission inputs, so the paid action stays auditable; its `authorization_decision_id` is NULL. On success, reserve the evaluation's hard maximum (subject `CANDIDATE`) and call the existing `evaluate_discovery_candidate` with a deterministic `request_id` (candidate + input fingerprint), so a retry reuses a stored result instead of paying again. If the check fails, nothing is reserved or spent; the candidate is screened (§7.2) or waits, as its state implies.

### 7.2 Screen (pure, free)

`evaluate_candidate_promotion(ctx)` (pure) returns a `ScreeningResult`; the service persists it as one immutable screening row. Inputs: deployment/account/search-workspace ceilings; kill switch and sentinel; the standing policy (evaluated with 6B's `evaluate_rules`, three-valued, declared `on_unknown`); attributes from the discovery fit and source record (`fit.overall_score`, `fit.verdict`, `job.employment_type`, `job.location`, `job.title`, `company.key`, `workspace.id`, `identity.strength`); candidate lifecycle state; whether its run finished (`completed`/`partial`); identity strength and key (from `product.job_identity`); whether **any application workspace on the account** already carries that identity (`application_workspace_job_identities` source-record or canonical-URL key) or a live/confirmed intent exists; fit freshness; budget and promotions-per-day state.

**Outcome precedence** (mirrors 6B; all reasons retained):

1. `DENY(invalid_input | kill_switch)`
2. `BLOCK` — a standing-policy BLOCK
3. `NOT_ELIGIBLE(reason)` — structural: weak identity, existing application, existing intent, candidate state, unfinished/failed run, stale or missing fit, ceiling below PREPARE. Not retried until an input changes.
4. `DENY_TEMPORARY(budget | promotions_cap)` with `retry_at`
5. `REQUIRE_USER` — standing-policy REQUIRE_USER (e.g. unknown fit score with `on_unknown: REQUIRE_USER`)
6. `PROMOTE`

No fit threshold is hard-coded; thresholds live in the standing policy.

**Anti-noise:** `could_unlock = 1` only when `REQUIRE_USER` is the sole obstacle to `PROMOTE`. Only then is an `autonomy_candidate_exceptions` row opened (and a `CANDIDATE_QUESTION` notification). Otherwise the screening is recorded and the candidate stays unpromoted, visible in discovery as "not auto-promoted" with its reason.

### 7.3 Promote (revalidating, one transaction)

After a `PROMOTE` screening, in one `BEGIN IMMEDIATE` transaction:

1. **Revalidate**: recompute the screening inputs from current state — fingerprint, authority and policy hashes, kill switch/sentinel, pause, identity and dedupe, candidate state, budget and promotions cap. Any difference → no promotion (a fresh screening happens on a later tick).
2. `_promote_candidate_in_transaction(...)` (existing logic, in the caller's transaction).
3. `autonomy_candidate_promotions` (`actor_type = SCHEDULER`), `autonomy_enrolments` (`SCHEDULER/ENROL`), queue item with `next_eligible_at = now`, a promotions-cap reservation.

The new application's first recorded 6B PREPARE decision happens on its next tick; from then on the application machinery applies.

**Deduplication is never a merge:** where today's manual promotion reuses an existing workspace for the same identity (`created: false`), autonomy screens it as `NOT_ELIGIBLE(existing_application)`.

### 7.4 Human path

Resolving a candidate exception with **Promote** is a *user* promotion: the same revalidating transaction with `actor_type = USER` (authority, halt and pause are still checked; the REQUIRE_USER itself is what the user answered). It creates the application, writes `USER/ENROL` and wakes the application so PREPARE continues. It does **not** consume `autonomy_max_promotions_per_day`, which caps only auto-created applications. **Dismiss** sets the candidate's existing `dismissed` lifecycle state. Both write an `autonomy_candidate_exception_resolutions` row.

---

## 8. Preparation and Gate 4

### 8.1 Step derivation — `next_prepare_step(snapshot)` (pure, first match wins)

1. Workspace status past `drafted` (applied, interview, …) → **DONE** (dormant). Autonomy never reworks a submitted application.
2. No usable current Evidence Profile snapshot → operational **NEEDS_USER** ("refresh your Evidence Profile").
3. Job understanding missing or stale → **UNDERSTAND** (cost-bearing).
4. Fit missing or stale → **FIT** (cost-bearing).
5. Application intelligence missing or stale → **INTELLIGENCE** (cost-bearing).
6. Review items without a decision that the mechanical classifier can accept → **SYSTEM_REVIEW** (local).
7. Undecided judgment items remain → **NEEDS_USER** (pack review).
8. No outstanding items and no current confirmed pack → **GATE4** (local).
9. A current, non-stale pack → **PREPARED** (dormant; `PREPARED` notification).

Staleness uses the existing `check_staleness`. Paid steps call `understand_job`, `fit_job` and `generate_application_intelligence` unchanged, with deterministic request ids; an existing artifact matching the step's input fingerprint is reused (`REUSED`), never recomputed.

### 8.2 Mechanical review (pure)

A review item is system-confirmable only when it needs no judgment:

| Item | Result |
|---|---|
| `content_unit` with status `READY`, non-empty text, every cited evidence id present in the **current bound profile snapshot**, not a conflict or placeholder, not contradicted, and the exact claim set matching the item's content hash | `acknowledged_and_proceed`, `decision_provenance = SYSTEM_AUTO_CONFIRMED`, basis `{reason: "grounded_ready_unit", item_content_hash, pack_revision}` |
| `content_unit` `NEEDS_REVIEW`; functional-equivalent or transferable match; `gate_flag` (`FLAG`/`UNVERIFIED`); `human_judgment_question`; `profile_conflict`; `profile_placeholder` | **UNDECIDED** — no review decision is written |

- A `USER` decision is never overridden or duplicated; the system only fills gaps.
- Judgment items stay genuinely undecided; the inbox (§9.1) derives the question from that. No synthetic "NEEDS_USER" review decision exists.
- `pack_revision` = canonical hash of (profile snapshot content id, `job_fit_result` content id, `application_intelligence_result` content id). Any change makes prior system decisions stale; they are not reused.
- Automatic omission (`omit_from_positioning`) of judgment items is **not** done in 6C.

### 8.3 System Gate 4

One `BEGIN IMMEDIATE` transaction using the system variant of `confirm_application_pack`, which re-checks inside the transaction:

1. still-valid PREPARE authority (`ALLOW`, `grantable`, `effective_capability >= PREPARE`);
2. not paused, not halted, still enrolled, scheduler gate on;
3. no human-review latch for the current `pack_revision`;
4. exact current profile, artifact and revision hashes;
5. every review item resolved;
6. every `SYSTEM_AUTO_CONFIRMED` decision's basis still matches (re-running the grounding invariant independently of SYSTEM_REVIEW);
7. zero unsupported or contradicted claims in the pack;
8. `completion_status == READY`.

It writes the pack and the `drafted` workflow event with the note *"Application pack system-confirmed under standing PREPARE authority (6B representation rule)."* — never "by user". A system-confirmed pack is not permission to FILL or SUBMIT; 6D/6E perform their own authorization and provenance checks.

### 8.4 Human-review latch

A latch for a `pack_revision` is written only when the user:

- explicitly chooses **"Review this pack"**, or
- reopens or changes a revision that has **already been system-confirmed**.

A user decision that resolves an outstanding judgment item on a **not-yet-confirmed** revision does **not** latch; the scheduler then reconsiders remaining items mechanically and may complete Gate 4. While a latch exists for the current revision, the classifier returns UNDECIDED for every item and system Gate 4 is barred. A new revision (e.g. after a rerun) is not covered by an old latch.

---

## 9. Inbox, propagation, notifications

### 9.1 Inbox (derived, read-only)

`/autonomy/inbox`; each entry lists **all** current reasons for its subject (collect-then-resolve):

- **Needs your answer (actionable):** open candidate exceptions (Promote / Dismiss); application REQUIRE_USER items from the application's still-valid PREPARE decision — standing-policy rules (existing rule-acknowledgement form) and governing `application_blockers` (existing answer form, optional "save as reusable answer" with reach via 6B `approve_answer`, showing any `SYSTEM_PROPOSED` draft); pack review (undecided judgment items of the current revision, linking to the existing review screen); operational `NEEDS_USER` (missing profile, missing budget or cost envelope, human-fixable errors — with the error).
- **Blocked / failed (informational):** standing-policy BLOCK, governing AUTO_REJECT, and `OPERATIONAL_ERROR` items. A **Retry** action is offered only for a retry-eligible failure — an exhausted `TRANSIENT` cycle. Direct `INTERNAL`/`INTEGRITY` errors (invariant, schema or contract violations, cost overage) are shown without Retry. `HUMAN_FIXABLE` failures are not here; they are `NEEDS_USER` items above.
- **Ready:** newly `PREPARED` applications.

Every resolution uses its existing path (or the new candidate-exception endpoint) and wakes its own item in the same transaction.

### 9.2 Answer propagation (wake-only)

When an approved answer is saved (directly, or with a blocker resolution), `propagate_answer(conn, subject, reach, scope_id)` runs **in the same transaction** and wakes every enrolled application within reach waiting on that semantic subject — an open blocker with that `semantic_subject_key`, or a REQUIRE_USER item for it in its current decision:

- `ACCOUNT` → all enrolled applications of the account;
- `SEARCH_WORKSPACE` → enrolled applications of that search workspace;
- `EMPLOYER` → enrolled applications whose employer key matches.

It never writes sibling blocker resolutions, review decisions, approved answers or confirmations. Each sibling independently re-checks registry scope, context keys, freshness, current evidence and policy on its next tick (6B §11.3).

### 9.3 Notifications

`Notifier.notify(kind, subject_type, subject_id, notification_key, detail)` writes `CREATED` unless an unresolved event with the same key exists. The key binds the **occurrence**: subject + condition/reason + the relevant revision or decision fingerprint — repeated ticks do not re-notify, but the same problem recurring after resolution does.

- Viewing the inbox writes `SEEN` for the entries shown. `SEEN` never implies `RESOLVED`.
- `RESOLVED` is written only when the underlying derived condition has disappeared (§10.4 reconciliation).
- **Badge = unresolved actionable items + unseen informational items.** Actionable — `NEEDS_USER` (including human-fixable failures) and `CANDIDATE_QUESTION` — count until resolved; informational — `PREPARED`, `BLOCKED` and `OPERATIONAL_ERROR` — count only until seen.
- Delivered via `GET /api/autonomy/inbox/summary` and a small `app.js` update of the "Autonomy" nav link. No external channel in 6C; the `Notifier` interface allows adding one later.

---

## 10. Recovery, retries, sweeps

### 10.1 Lease loss and orphaned attempts

- Finalization is fenced by holder, `lease_generation` and lease expiry (§6.3); a worker that lost or outlived its lease commits nothing further.
- A worker that takes over an item and finds a `STARTED` attempt with no terminal row appends `ABANDONED` for it. Its reservation settles at the provider-audited actual cost if proven, else at the reserved hard maximum (§11.3).
- An existing artifact matching the step's input fingerprint is reused (`REUSED`).

### 10.2 Error classes

An explicit mapping from exception types; unmapped → `INTERNAL`.

| Class | Examples | Handling |
|---|---|---|
| `TRANSIENT` | timeout, rate limit, provider 5xx | up to **3 automatic retries** with delays 60 s → 300 s → 900 s, ±20 % jitter from the injected `rng`; honour `Retry-After` (never shorter than the scheduled delay) |
| `HUMAN_FIXABLE` | missing provider credentials, invalid profile, missing budget/envelope | `NEEDS_USER` with the error |
| `INTERNAL` / `INTEGRITY` | invariant violation, schema/contract error, cost overage | `OPERATIONAL_ERROR`, non-retrying, no Retry offered |

**Escalation:** a retry cycle is the initial attempt plus up to 3 automatic retries — **4 attempts in total** (fail → 60 s → fail → 300 s → fail → 900 s → fail → escalate), for the same step with the same input fingerprint. The 4th consecutive `TRANSIENT` failure escalates to `NEEDS_USER` if a human could plausibly help, else `OPERATIONAL_ERROR`. A changed input fingerprint starts a new cycle. An escalated state clears only on materially new inputs or a new explicit user Retry.

**Retry requests are one-shot.** One `autonomy_retry_requests` row opens exactly one new cycle (again up to 4 attempts) for its `step_kind + input_fingerprint`; every attempt in it carries that `retry_request_id`. When that cycle escalates, the same request cannot be reused — a newer Retry or new inputs are required. Earlier failure history is never modified.

### 10.3 Idempotency

PREPARE steps are content-addressed and idempotent (6B §11.2); retries and recovery reuse matching artifacts. Candidate evaluation uses deterministic request ids.

### 10.4 Sweeps (every tick, including while halted; reduce-only; idempotent)

- `expire_grants` (6B)
- `expire_unclicked` (6B)
- `mark_stale_dispatches_ambiguous` with `autonomy_dispatch_result_timeout` (≈ 10 min)
- notification reconciliation: `RESOLVED` for conditions that no longer hold
- orphaned-attempt settlement for expired leases (§10.1)

---

## 11. Budget and cost accounting

1. **Explicit budget required.** Autonomous cost-bearing work requires an LLM budget in the standing policy (`limits.budgets.LLM` with `per_day`, and `per_application` for application steps). If it is absent, paid autonomous steps do not run; the item becomes operational `NEEDS_USER` ("Set an autonomy spending budget"). Manual use is unaffected.
2. **Hard per-step envelope.** Each cost-bearing step kind (`EVALUATE`, `UNDERSTAND`, `FIT`, `INTELLIGENCE`) needs a hard maximum in `autonomy_step_cost_max` (derived from provider token caps and price). No envelope → fail closed (`NEEDS_USER`, "configure step cost limits").
3. **Admission and settlement.** Before a paid step, reserve its hard maximum in `limit_reservations` (`budget:LLM:day` for the account's local day, plus `budget:LLM:application` for application steps), with `subject_type`/`subject_id`. While unsettled, usage counts the full reserved amount, so concurrent workers cannot overspend. After the step, settle **exactly once**: `UPDATE … SET settled_amount = ?, settlement_ref = ?, status = 'CONSUMED' WHERE id = ? AND settled_amount IS NULL`. The `settled_amount IS NULL` guard is the primary exactly-once control; `settlement_ref` is a **stable per-reservation** reference — `canonical_hash("autonomy-settlement", "v1", {attempt_id, reservation_id})` — the same value on every retry of that settlement and distinct for each reservation of the same attempt (an application attempt reserves both `budget:LLM:day` and `budget:LLM:application`); unique when non-null as additional protection — completion, `ABANDONED` and recovery can never double-settle. A settled row is **immutable**: a provider audit that appears later never rewrites `settled_amount`; any correction is a future auditable adjustment event (§11.6). Usage then counts `COALESCE(settled_amount, amount)` for settled rows. Actual cost comes from provider-audit usage metadata × configured price; if unavailable, settle at the reserved maximum.
4. **Overage.** If actual cost exceeds the reserved maximum: record and charge the true amount, raise `OPERATIONAL_ERROR`, and refuse further autonomous calls of that step kind until its envelope is corrected. Accounting is never clamped.
5. **Promotions cap.** `autonomy_max_promotions_per_day` (operator setting, default 5, lower-only) is a reserved counter `promotions:day` checked in screening and re-checked in the promotion transaction.
6. Downward correction of past settlements, if ever needed, will be an auditable adjustment mechanism (deferred).

---

## 12. Dossier (6B carry-forward)

The application dossier adds: each pack's content id, its source artifact content ids and document hashes; which review items were `SYSTEM_AUTO_CONFIRMED`, with reason and basis; latches; enrolment history; the originating candidate screening and promotion; and the step-attempt timeline with costs. It shows **"Current state (derived)"** and **"Attempt history (observational)"** as separately labelled sections, so history is never mistaken for authority.

---

## 13. API and UI

| Surface | Purpose |
|---|---|
| `GET /autonomy/inbox`, `GET /api/autonomy/inbox` | Derived inbox (§9.1); viewing writes `SEEN`. |
| `GET /api/autonomy/inbox/summary` | Badge counts. |
| `POST /api/workspaces/{id}/autonomy/enrol` \| `/unenrol` | "Prepare autonomously" / "Stop preparing" (owned job workspace only). |
| `POST /api/workspaces/{id}/autonomy/review-pack` | Explicit latch for the current revision. |
| `POST /api/autonomy/retry` `{subject_type, subject_id, step_kind}` | Explicit Retry; refused (409) unless the current failure is retry-eligible — an exhausted `TRANSIENT` cycle with no open retry cycle for that step + input fingerprint. |
| `POST /api/autonomy/candidate-exceptions/{id}/resolve` `{resolution, reason}` | Promote / Dismiss. |
| `GET /api/autonomy` (extended) | Adds scheduler gate state, in-app driver status and last tick time. |

All state changes are attributed to the account (actor) and scoped to the caller's own workspaces.

---

## 14. Settings (operator; defaults)

| Setting | Env | Default |
|---|---|---|
| `autonomy_scheduler_enabled` | `JOBSEARCH_AUTONOMY_SCHEDULER=1` | off |
| `autonomy_tick_interval` | — | ≈ 30 s wait between completed ticks (a driver never overlaps itself) |
| `autonomy_max_items_per_tick` | — | 4 (alternating) |
| `autonomy_step_timeout` | — | 600 s per step kind |
| `autonomy_lease_margin` | — | 120 s |
| `autonomy_step_cost_max` | `JOBSEARCH_AUTONOMY_STEP_COST_MAX` (JSON) | none → fail closed |
| `autonomy_max_promotions_per_day` | — | 5 (lower-only) |
| `autonomy_dispatch_result_timeout` | — | 10 min |
| `autonomy_retry_delays` | — | (60, 300, 900) s → 3 automatic retries, 4 attempts per cycle |

---

## 15. Testing

1. **Pure (unit + Hypothesis, injected clock/rng):** `evaluate_candidate_promotion` precedence; monotonicity (restricting any input never yields a more permissive outcome); never `PROMOTE` with weak identity, existing application/intent, unfinished/failed run, stale fit or halt; ceiling = min. `next_prepare_step` table (DONE before missing profile). `mechanical_review`: only grounded `READY` units accepted; every judgment type stays UNDECIDED (property); any content-hash/profile change invalidates. Authorization reuse predicate (stage/mode, fingerprint, hashes, validity horizon; no-horizon → fresh). Backoff and jitter determinism. Notification key occurrence semantics.
2. **Persistence:** migration 018 on a fresh database and on a **representative pre-6C database built by `master@20979b9` code** and upgraded; re-run is a no-op; FK and integrity checks. Append-only triggers on every specified history/event table (not on `autonomy_candidate_queue`). Current-by-`seq` projections (enrolments, exception resolutions, notification events). Idempotent settlement (no double charge across completion/`ABANDONED`/recovery). Stale `lease_generation` cannot finalize.
3. **Services (fake providers from the acceptance fixtures, injected clock):** every step kind, `REUSED`, `ABANDONED`, each error class, escalation on the 4th attempt of a cycle, one-shot Retry (a used retry request cannot reopen another cycle), expired-lease finalization refused even when not retaken. Authorization reuse vs fresh; `ALLOW(NONE)` never runs a step. Race injection for pause, unenrol and halt between lease and step (no start) and during a step (truthful finish, nothing further). Missing budget and missing envelope fail closed; overage charged truthfully then blocked; window rollover. Candidate: each revalidation input changed between screening and promotion → no promotion; dedupe → `NOT_ELIGIBLE`; anti-noise; human Promote/Dismiss. System Gate 4: fault-inject each of the eight rechecked conditions → refusal. Latch only on explicit review or reopening a confirmed revision; answering one item does not latch. Propagation wakes in-reach siblings with **zero** sibling writes. Candidate evaluation admission: each failed admission input → no reservation and no spend; candidate attempts carry a NULL `authorization_decision_id` and bind the admission inputs. Settlement of both reservations of one application attempt succeeds with distinct stable `settlement_ref`s and never double-settles. `/api/autonomy/retry` refuses non-retry-eligible failures (direct `INTERNAL`/`INTEGRITY`, open cycle). Material input changes (new profile snapshot, job/source change, stale discovery fit) wake dormant items. Human Promote writes `USER/ENROL`, wakes the application and does not consume the auto-promotion cap.
4. **Concurrency (threads, separate connections, WAL; each 20×):** two workers on one application or candidate lease; the last budget unit; the promotions cap; two promotions of the same candidate; kill switch racing a tick.
5. **Acceptance (real workflow, fake portal runner and fake LLMs):** a user-triggered discovery run, then ticks until quiescent: a grounded eligible candidate ends promoted and system-confirmed `PREPARED` (attributed to the system, never the user); a judgment candidate → `NEEDS_USER` → answer → wake → `PREPARED`; weak-identity and already-applied duplicate candidates not promoted; an older application untouched until enrolled; **zero FILL/SUBMIT grants, intents or attempts**; the dossier shows derived state and labelled history; badge counts are correct.
   - **Halt/recovery sequence:** halt active → no new work starts → an in-flight step finishes truthfully → remove halt → still no work → explicit resume-all → fresh PREPARE evaluation → work may resume.
   - **Negative controls:** with the scheduler gate off, deployment ceiling `NONE`, kill switch engaged, or no budget configured — no new paid or autonomous preparation work, promotion, FILL or SUBMIT occurs, while reduce-only expiry/reconciliation sweeps remain permitted.
6. **Browser (headless Chromium):** inbox, badge update, enrol/unenrol, candidate Promote/Dismiss, "Review this pack".
7. **Gates:** the WAL commit with its own full-suite run; final chunked full suite; migration checks; greps (no `created_at` ordering in new modules; no `webapp` import in `product/`; no `request_grant`/`pre_click_commit` in 6C modules, also enforced by a structural test); diff boundary against `master@20979b9`.

---

## 16. Acceptance and closure

**Bundle 6C is accepted only when autonomous PREPARE can take an eligible discovered candidate through promotion and preparation to either genuinely system-confirmed `PREPARED` or an accurately derived human/operational stop; all concurrency, recovery, budget and migration tests pass; and unattended FILL and SUBMIT remain structurally impossible.**

---

## 17. Deferred

Scheduled/continuous discovery; automatic omission of judgment items; a restrictive "prepare-for-review only" preference; external notification channels; auditable downward cost adjustments; a standing-policy promotion limit (schema v2); FILL (6D) and SUBMIT (6E), including ATS job id and permitted-redirect-set binding before any live SUBMIT (6B ruling).
