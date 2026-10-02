# Bundle 6E-A: Human-Authorized SUBMIT — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One human-authorized submission of a 6D-B-filled application, bound to the exact reviewed snapshot. It runs through a verified, adapter-certified SUBMIT egress, with a server-determined result.

**Architecture:**
- **Pure product modules:** certification, review snapshot, result rule and constants.
- **Gate:** a `HUMAN_SUBMIT` authority mode added to the 6B gate.
- **Services:** reuse 6B's grant, pre-click, intent and attempt machinery through new human entry points, plus the 021 evidence tables.
- **Extension:** a SubmitController that owns the egress install, verify and restore, and the single `SUBMIT_CLICK`.
- **Web app:** a Submit Review page holds the one authorization act.

**Tech Stack:** Python 3.13, FastAPI, SQLite (WAL), Jinja2, TypeScript MV3 extension (esbuild, vitest), Playwright (Python) browser tests.

**Spec:** `docs/superpowers/specs/2026-09-29-bundle6e-a-human-submit-design.md` (commit `125953f`). Section references (§n, E-n, J-n) point into it.

## Global Constraints

- **Base:** `master@efc6577`. Branch `bundle6/6e-a-submit`, with no upstream.
- **Unchanged:**
  - 6D-A and 6D-B tables, CHECKs, bindings and event vocabularies (J10);
  - `extension/src/fill/executor.ts`, `quarantine.ts` and the 6D-B allowlist.
- **Autonomous SUBMIT stays closed.** `request_grant(stage=SUBMIT)` and `pre_click_commit` keep raising `SubmissionNotAvailable("submission_not_available")`.
- **Certification:** `greenhouse@2/submit@1` is `FIXTURE_CERTIFIED` with `live_evidence=None`, and Lever has no submit certification.
- **Config** (both default off):
  - `human_submit_enabled` ← `JOBSEARCH_HUMAN_SUBMIT_ENABLED == "1"`;
  - `submit_fixture_origins_enabled` ← `JOBSEARCH_SUBMIT_FIXTURE_ORIGINS == "1"`.
- **Rule ids:** the allow rules are 9201–9209 at priority 2000. The TOTAL rules 9111/9121 stay at priority 1000 and are never removed during a submit.
- **Timing (`submit-timing.v1`):**
  - `SUBMIT_REVIEW_OBSERVATION_MAX_AGE` = 60 s;
  - `REOBSERVATION_WAIT` = 20 s;
  - `SUBMIT_RESULT_WINDOW` = 30 s;
  - `SUBMIT_RESULT_POLL` = 250 ms;
  - `CHALLENGE_HANDOFF_WINDOW` = 300 s;
  - `DISPATCH_RESULT_TIMEOUT` = 360 s;
  - existing: `SUBMIT_GRANT_TTL` = 120 s, `CLICK_DISPATCH_TTL` = 60 s.
- **Closed vocabularies:**
  - `submit_events.event` — spec §17 list, verbatim;
  - `submit_observations.phase` ∈ {REVIEW, PRE_SUBMIT, CHALLENGE_CLEARED, POST_SUBMIT};
  - intent source adds `HUMAN_AUTHORIZED`.
- **No cleartext answers** in any table, log, event or result (J9).
- **HTTP:** 404 for ownership, 409 with the exact reason for a domain refusal, 422 for validation. Extra body keys are forbidden.
- **Migrations:** atomic, no `executescript`, append-only triggers on new tables.
- **Windows file writes:** use Edit/Write, or Python with `encoding="utf-8"`.
- **Test runs:**
  - browser tests one file at a time, in the foreground, with a memory check (free RAM < 600 MB or commit headroom < 2 GB → stop);
  - full suite in the six chunks of 6C Task 1.

## Review Focus

1. **Double submit from the UI:** a double-click, or two open Submit Review tabs, both POST authorize. Expected: exactly one authorization; the second gets 409 `already_authorized`. Owner: **Task 7**.
2. **The user edits a field on the employer tab after the Submit Review rendered.** Expected: authorize → 409 `stale_review`, never a submission of the edited content. Owner: **Tasks 6/7**.
3. **A slow employer confirmation**, arriving after `SUBMIT_RESULT_WINDOW`. Expected: `SUBMISSION_AMBIGUOUS` (the user resolves it), never `SUBMISSION_FAILED`. Owners: **Task 3** (rule) and **Task 13** (controller timeout).
4. **Already applied by hand:** the job identity was already marked "applied" through the tracker or Phase 3 handoff. Expected: the gate denies `duplicate`, and the Submit Review shows "already submitted". Owner: **Task 7**.
5. **A tenant or job key with regex metacharacters** (e.g. `acme.co+x`). Expected: the egress regex is escaped, so it matches only the literal path and never broadens. Owners: **Task 2** (Python) and **Task 11** (TypeScript).

---

## File Map

| Path | Responsibility | Task |
|---|---|---|
| `extension/src/submit/spike-entry.ts`, `tests/webapp/spikes/test_submit_spikes_browser.py`, `tests/webapp/fixtures/fill/submit/*`, `docs/superpowers/notes/2026-09-29-6e-a-spike-results.md` | S-E1..S-E4 | 1 |
| `product/submit_constants.py`, `extension/src/submit/constants.ts`, `product/submit_certification.py`, `extension/src/submit/certification.ts` | constants and certification | 2 |
| `product/submit_review.py`, `product/submit_result.py` | snapshot, `review_hash`, result rule | 3 |
| `product/autonomy_contract.py`, `product/autonomy_gate.py`, `webapp/services/autonomy_context.py`, `webapp/config.py` | `HUMAN_SUBMIT` authority | 4 |
| `webapp/persistence/migrations.py` (021), `webapp/persistence/submit.py`, `webapp/persistence/workflow.py` | data | 5 |
| `webapp/services/submit_review.py` | re-observation, REVIEW intake, snapshot, readiness | 6 |
| `webapp/services/human_submit.py` (authorize/grant/cancel), `webapp/services/autonomy.py` | authorization | 7 |
| `webapp/services/human_submit.py` (pre-click/dispatch) | pre-click | 8 |
| `webapp/services/human_submit.py` (result/resolve/status), `webapp/services/autonomy_scheduler.py` | results | 9 |
| `webapp/api/submit_extension.py`, `webapp/api/submit_app.py`, `webapp/api/fill_extension.py`, `webapp/app.py` | routes | 10 |
| `extension/src/submit/egress.ts`, `extension/src/submit/signals.ts`, `extension/manifest.json` | egress and signals | 11 |
| `extension/src/submit/submit-executor.ts`, `extension/src/fill/page-api.ts`, `extension/src/fill/page-bundle.ts` | click and page API | 12 |
| `extension/src/submit/submit-controller.ts`, `extension/src/submit/server.ts`, `extension/src/background/fill-wiring.ts`, `extension/src/popup/*` | protocol and popup | 13 |
| `webapp/templates/submit_review.html`, `prepared_applications.html`, `autonomy_dossier.html`, `webapp/api/review_pages.py` | UI | 14 |
| revised structural tests, `tests/webapp/services/test_submit_concurrency.py`, `docs/superpowers/notes/2026-09-29-greenhouse-live-submit-certification.md` | structure, concurrency, runbook | 15 |
| `tests/webapp/test_submit_acceptance_browser.py`, `tests/webapp/persistence/test_submit_upgrade.py` | final validation | 16 |

---

### Task 1: Spikes S-E1..S-E4 (GATE)

**Files:**
- Create:
  - `tests/webapp/fixtures/fill/submit/form_post.html` (native form POST to `/{tenant}/jobs/{job}` → 303 to `/confirmation`);
  - `tests/webapp/fixtures/fill/submit/react_xhr.html` (a vendored React form whose `onSubmit` XHR-POSTs, then swaps in `#application_confirmation`);
  - `extension/src/submit/spike-entry.ts`, built only with `FILL_SPIKES=1`;
  - `tests/webapp/spikes/test_submit_spikes_browser.py`;
  - `docs/superpowers/notes/2026-09-29-6e-a-spike-results.md`.
- Modify:
  - `extension/scripts/build.mjs` (a spike entry behind `FILL_SPIKES`);
  - `tests/webapp/fixtures/fill/recording_server.py` (serve `POST /{tenant}/jobs/{job}` → 303 to `/confirmation`, and `GET …/confirmation` → a page with `#application_confirmation`; record every request with its method and path);
  - `extension/manifest.json` (add `"declarativeNetRequestFeedback"`; Task 11 drops it if S-E2 fails).

**Steps:**
- [ ] Write the spike test, using the 6D-B `launch_persistent_context` + test-hook pattern of `tests/webapp/test_fill_quarantine_browser.py`. Under TOTAL, with one priority-2000 tab-scoped allow rule (`post`, `main_frame`/`xmlhttprequest`, regex on the fixture's submit path), assert:
  - **S-E1:**
    - the matching POST reaches the server;
    - a same-origin POST to another path does not;
    - a GET to the third-party port 8431 does not;
    - a service-worker `fetch` does not.
  - **S-E2:** `chrome.declarativeNetRequest.getMatchedRules({tabId})` includes the allow rule's id after the POST.
  - **S-E3:** `element.click()` from the ISOLATED world on the submit button triggers:
    - (a) the native form POST navigation on `form_post.html`;
    - (b) the React `onSubmit` XHR on `react_xhr.html`.
  - **S-E4:** after the allowed POST navigation plus an allow rule for the `GET` confirmation, `tabs.onUpdated` reports `complete`, and a re-injected script reads `#application_confirmation`.
- [ ] Run `FILL_SPIKES=1 FILL_TEST_HOOKS=1 npm --prefix extension run build`, then `.venv/Scripts/python -m pytest tests/webapp/spikes/test_submit_spikes_browser.py -q -p no:cacheprovider`.
- [ ] Record pass/fail per assertion, and the Chrome version, in the notes file.
- [ ] **GATE:**
  - If S-E1 or S-E3 fails, **stop and report to the user.**
  - An S-E2 or S-E4 failure applies the spec §21 fallback. Record it in the notes, and set the constants in Task 2 (`MATCHED_ALLOW_RULES_REPORTED`, `NAVIGATION_SUCCESS_OBSERVABLE`).
- [ ] Commit `test(submit): S-E1..S-E4 spikes — narrow allow over TOTAL, matched-rule feedback, isolated click, post-navigation observation`.

### Task 2: Submit constants and certification (pure, cross-language)

**Files:**
- Create:
  - `product/submit_constants.py`;
  - `extension/src/submit/constants.ts`;
  - `product/submit_certification.py`;
  - `extension/src/submit/certification.ts`;
  - `tests/product/test_submit_constants.py`;
  - `tests/product/test_submit_certification.py`;
  - `extension/test/submit-certification.test.ts`.

**Interfaces (Produces):**
- **Python:**
  - `SUBMIT_TIMING_VERSION = "submit-timing.v1"` and the §20 constants as `timedelta`s;
  - `MATCHED_ALLOW_RULES_REPORTED: bool` and `NAVIGATION_SUCCESS_OBSERVABLE: bool` (from Task 1);
  - `HUMAN_SUBMIT_CONTRACT = "human-submit.v1"`.
- **Python:**
  - `@dataclass(frozen=True) EgressEntry(id, methods: tuple[str,...], types: tuple[str,...], template: str)`;
  - `SubmitCertification(certification_id, adapter_id, adapter_version, status, live_evidence, egress: tuple[EgressEntry,...], success_selectors, confirmation_template, failure_selectors, challenge_selectors, failure_signal_proves_not_submitted)`;
  - `SUBMIT_CATALOGUE: dict[tuple[str,str], SubmitCertification]` (only `("greenhouse","greenhouse@2")`);
  - `submit_certified(adapter_id, adapter_version) -> SubmitCertification | None`;
  - `resolve_egress(cert, *, origin: str, tenant_key: str, ats_job_id: str) -> list[dict]` (each `{"id","methods","types","regex"}`, with `re.escape` on every bound value);
  - `is_loopback_origin(origin) -> bool`;
  - `submission_permitted(cert, origin, *, fixture_origins_enabled: bool) -> tuple[bool, str | None]` (`(False, "adapter_not_submit_certified" | "adapter_not_live_certified")`).
- **TypeScript:** mirror types plus `SUBMIT_CERTIFICATIONS`, `resolveEgress(...)` (escaping with `/[.*+?^${}()|[\]\\\/]/g`), `submitCertificationFor(adapterId, version)`.

**Steps:**
- [ ] **Tests:**
  - the constants in Python and TypeScript are equal (the Python test regex-parses `constants.ts`, as in the 6D-B `test_fill_constants.py`);
  - the catalogue has exactly one entry;
  - `live_evidence is None`;
  - Lever is absent;
  - `resolve_egress` gives the exact regexes of spec §9.3 for `http://127.0.0.1:8430`, `acme`, `123`;
  - **Review Focus 5:** tenant `acme.co+x` produces `acme\.co\+x`, and the regex doesn't match `acmeXco+x`;
  - `submission_permitted`:
    - `LIVE_CERTIFIED` without evidence → false;
    - `FIXTURE_CERTIFIED` on `https://boards.greenhouse.io` → `adapter_not_live_certified`;
    - loopback with the flag off → false; with the flag on → true;
    - `http://localhost.evil.com` → false.
  - The TypeScript suite asserts that the same vectors produce identical regex strings (a shared `tests/fixtures/submit/egress_vectors.json`, written by the Python test's generator helper).
- [ ] Implement it, then run `.venv/Scripts/python -m pytest tests/product/test_submit_constants.py tests/product/test_submit_certification.py -q -p no:cacheprovider` and `npm --prefix extension test -- submit-certification`.
- [ ] Commit `feat(submit): submit-timing.v1 constants and submit-certification.v1 catalogue with escaped egress templates`.

### Task 3: Review snapshot and result rule (pure)

**Files:**
- Create:
  - `product/submit_review.py`;
  - `product/submit_result.py`;
  - `tests/product/test_submit_review.py`;
  - `tests/product/test_submit_result.py`.

**Interfaces (Produces):**
- `@dataclass(frozen=True) ReviewInputs` with every §7.1 field as a plain value;
- `build_review_snapshot(inputs: ReviewInputs) -> dict` and `review_hash(snapshot: dict) -> str`, which is `canonical_hash("submission-review","v1",snapshot)` from `product.autonomy_contract`;
- `@dataclass(frozen=True) ResultEvidence(click_performed: bool | str, egress_ever_installed: bool, total_restored_verified: bool, success_observed: bool, failure_observed: bool, content_changed: bool, matched_rule_ids: tuple[int,...], matched_rules_available: bool, cause: str | None)`;
- `determine_submit_result(evidence: ResultEvidence, cert: SubmitCertification, e1_rule_id: int) -> tuple[str, bool, str | None]`, which returns `(state, proven_not_submitted, reason)`;
- `build_submission_result(...) -> dict` and `submission_result_hash(result) -> str` (spec §15).

**Steps:**
- [ ] **Snapshot tests:**
  - equal inputs → an equal hash;
  - changing any single §7.1 field changes the hash;
  - lists are sorted, so input order doesn't matter;
  - no key contains `_id` of an observation row or any timestamp (a recursive key scan asserts that `created_at` and `observation_id` are absent).
- [ ] **Result tests**, one per §11.2 rule plus precedence:
  - a content change beats success (rule 2 over rule 3);
  - `click_performed=False` with the egress never installed → FAILED proven;
  - `click_performed=False` with egress installed but TOTAL not restored → AMBIGUOUS;
  - success without `total_restored_verified` → AMBIGUOUS;
  - failure with a cert flag of False → AMBIGUOUS;
  - no E1 match with `matched_rules_available=False` → AMBIGUOUS;
  - **Review Focus 3:** timeout (nothing observed) → AMBIGUOUS;
  - `click_performed="UNKNOWN"` → AMBIGUOUS;
  - `build_submission_result` carries the §15 `non_claims` exactly, and no cleartext (reuse `webapp.persistence.fill._check_no_cleartext`'s rule: no key named `value`, `answer` or `text`).
- [ ] Implement it and run the tests.
- [ ] Commit `feat(submit): submission-review.v1 snapshot hash and the pure server-side result rule`.

### Task 4: `HUMAN_SUBMIT` authority in the 6B gate

**Files:**
- Modify:
  - `product/autonomy_contract.py`: `class AuthorityKind(str, Enum): STANDING_POLICY = "STANDING_POLICY"; HUMAN_SUBMIT = "HUMAN_SUBMIT"`, plus the field `authority: AuthorityKind = AuthorityKind.STANDING_POLICY` as the last field of `AuthorizationContext`. `canonical_hash`'s dataclass encoding omits a field named `authority` whose value is `STANDING_POLICY`.
  - `product/autonomy_gate.py`: `_context_errors` validates `authority`, and requires `requested_stage == SUBMIT` when it's HUMAN (error `human_authority_stage`). `_evaluate` applies the §8.2 branch.
  - `webapp/config.py`: the two flags, and `human_submit_ceiling() -> Capability` (SUBMIT if enabled, else FILL).
  - `webapp/services/autonomy_context.py`: `build_context(..., authority=AuthorityKind.STANDING_POLICY)`. For HUMAN it sets `deployment_ceiling=settings.human_submit_ceiling()` and `adapter_submit_capable=submission_permitted(...)[0]` (the certification from the observation's adapter), and passes `authority`.
- Test: `tests/product/test_autonomy_gate_human_submit.py`; extend `tests/product/test_autonomy_gate.py` with a pinned-fingerprint test.

**Steps:**
- [ ] **Tests:**
  - (a) Before changing any code, compute the `canonical_hash(CONTEXT_SCHEMA, CONTEXT_SCHEMA_VERSION, ctx)` of the existing gate test's base context. Pin that literal in the new test, then assert it's unchanged after the change. **(Acceptance 18.)**
  - (b) HUMAN with `requested_stage=FILL` → `invalid_input`.
  - (c) HUMAN with the standing policy `None` → ALLOW at SUBMIT, given the other checks pass. The same context under STANDING_POLICY → `standing_policy_missing`.
  - (d) HUMAN with `account_max=FILL` → still SUBMIT, noted.
  - (e) HUMAN with the deployment ceiling FILL → capped, with the note `human_submit_disabled`.
  - (f) HUMAN still denies:
    - `kill_switch`;
    - `duplicate` (CLAIMED, and CONFIRMED not overridden);
    - `stale_binding`;
    - apply-target `adapter_not_submit_capable`;
    - an identity-weak cap;
    - requirement completion blockers.
  - (g) HUMAN ignores counters at their limit and budgets over their cap.
- [ ] Implement. Then run `.venv/Scripts/python -m pytest tests/product -q -p no:cacheprovider` and `tests/webapp/services -k autonomy`.
- [ ] Commit `feat(autonomy): HUMAN_SUBMIT authority mode — human authority replaces standing policy and autonomous caps; fingerprints unchanged`.

### Task 5: Migration `021_human_submit` and persistence

**Files:**
- Modify:
  - `webapp/persistence/migrations.py`: `HUMAN_SUBMIT_MIGRATION_ID = "021_human_submit"`, `SUBMIT_APPEND_ONLY_TABLES`, and `_migrate_human_submit`, registered with `disable_foreign_keys=True` for the `submission_intents` rebuild (the 019 pattern: create `submission_intents_new` with the added source value, copy the rows, drop, rename, recreate `idx_submission_intents_live`, `PRAGMA foreign_key_check` must be empty).
  - `webapp/persistence/workflow.py`: in `record_human_intent`, return without inserting when a live intent (CLAIMED/CONFIRMED, not overridden) exists for the account and job identity.
- Create:
  - `webapp/persistence/submit.py`;
  - `tests/webapp/persistence/test_submit_migration.py`;
  - `tests/webapp/persistence/test_submit_persistence.py`.

**Interfaces (Produces, none commit):**
- `insert_authorization(conn, *, account_id, application_workspace_id, fill_run_id, review_hash, review, grant_id, actor, now) -> dict`
- `authorization_for_run(conn, fill_run_id) -> dict | None`
- `get_authorization(conn, id)`
- `insert_reobservation_request(conn, *, account_id, fill_run_id, now) -> dict`
- `latest_reobservation_request(conn, fill_run_id)`
- `insert_submit_observation(conn, *, account_id, application_workspace_id, fill_run_id, attempt_id, phase, structure_fingerprint, observation_fingerprint, observation, now) -> dict`
- `latest_submit_observation(conn, fill_run_id, phase) -> dict | None`
- `append_submit_event(conn, *, authorization_id, attempt_id, event, detail, now) -> dict`
- `submit_events(conn, authorization_id) -> list[dict]`
- `insert_submission_result(conn, *, attempt_id, result, result_hash, now)`
- `get_submission_result(conn, attempt_id)`
- `attempts_for_application(conn, application_workspace_id) -> list[dict]` (each with its latest state)

Every writer calls `fill._check_no_cleartext` on its JSON.

**Steps:**
- [ ] **Migration tests:**
  - a fresh DB has 21 migrations, ending in `021_human_submit`;
  - every new table rejects UPDATE and DELETE;
  - the event, phase and source CHECKs reject unknown values and accept `HUMAN_AUTHORIZED`;
  - a live partial unique index still rejects a second live intent;
  - FK and integrity checks are clean;
  - a re-run is a no-op.
- [ ] **Persistence tests:**
  - round-trips;
  - `authorization_for_run` uniqueness (a second insert raises `IntegrityError`);
  - cleartext rejection;
  - `record_human_intent` with a live HUMAN_AUTHORIZED intent inserts nothing.
- [ ] Run `.venv/Scripts/python -m pytest tests/webapp/persistence -q -p no:cacheprovider`. Existing migration-count assertions (e.g. `test_fill_upgrade.py`'s "twenty") are updated to 21 **only** where they assert the latest count. The 020 upgrade assertion stays.
- [ ] Commit `feat(submit): migration 021_human_submit — authorizations, submit observations/events/results; HUMAN_AUTHORIZED intents`.

### Task 6: Submit Review service (re-observation, REVIEW intake, snapshot, readiness)

**Files:**
- Create:
  - `webapp/services/submit_review.py`;
  - `tests/webapp/services/submit_fixtures.py` (a `filled_world` fixture built on `grant_world`: `filling(w)`, `run_all(w, run)`, `final_validate(..., page_after(w, 5))`, imported from `tests.webapp.services.test_fill_actions`; plus `review_observation(w)`, which returns `page_after(w, 5)` with the fixture target);
  - `tests/webapp/services/test_submit_review.py`.

**Interfaces (Produces):**
- `class SubmitRefused(Exception)` with `.reason`
- `request_reobservation(conn, *, account_id, application_workspace_id, now) -> dict | None` (None when there's no FILLED run with a live lease)
- `pending_reobservation(conn, fill_run_id) -> dict | None`
- `record_submit_observation(conn, *, settings, run_id, phase, attempt_id, observation, now) -> dict` (validates with `validate_observation(observation, CATALOGUE)`; phase-state rules: REVIEW needs FILLED with no authorization; the others need an authorization)
- `review_inputs(conn, *, settings, account_id, application_workspace_id, observation_row, now) -> ReviewInputs` (raises `SubmitRefused` with a §7.2 reason: `no_filled_run`, `lease_expired`, `post_fill_change`, `already_authorized`, `observation_missing`, `observation_stale`, `page_changed_since_fill`, `submit_control_not_unique`, `approval_not_effective`, `plan_mismatch`, `adapter_not_submit_certified`)
- `current_review(conn, *, settings, account_id, application_workspace_id, now) -> dict` (`{"state": "PENDING_OBSERVATION"|"UNAVAILABLE"|"READY"|"BLOCKED", "reasons": [...], "snapshot": dict|None, "review_hash": str|None, "decision": {...}|None}`; READY only when the dry `HUMAN_SUBMIT` gate evaluation is ALLOW)

**Steps:**
- [ ] **Tests:**
  - a request, then a REVIEW observation equal to FINAL → READY with a hash;
  - a second identical observation → the same hash (E18);
  - **Review Focus 2:** an observation with one field's `current_value_hash` changed → `page_changed_since_fill`;
  - an observation older than 60 s → `observation_stale`;
  - two submit controls → `submit_control_not_unique`;
  - a POST_FILL_CHANGE detection → `post_fill_change`;
  - an expired lease → `lease_expired`;
  - the deployment flag off → BLOCKED with `human_submit_disabled`;
  - a non-loopback target → `adapter_not_live_certified`;
  - REVIEW for a run with an authorization → refused;
  - no cleartext in the stored observation.
- [ ] Implement and run `.venv/Scripts/python -m pytest tests/webapp/services/test_submit_review.py -q -p no:cacheprovider`.
- [ ] Commit `feat(submit): Submit Review re-observation, snapshot assembly and human-gate readiness`.

### Task 7: Authorization and the human SUBMIT grant

**Files:**
- Create:
  - `webapp/services/human_submit.py` (the authorize/cancel part);
  - `tests/webapp/services/test_human_submit_authorize.py`.
- Modify: `webapp/services/autonomy.py`, adding `request_human_submit_grant(conn, *, settings, account_id, application_workspace_id, authorization_id, review_hash, observation, run_id, now) -> GrantOutcome`. It must be called inside the caller's transaction, and it calls `_request_grant_core(..., stage=SUBMIT, in_transaction=True, authority=HUMAN_SUBMIT, extra_binding={...})`. `_request_grant_core` gains `authority` and `extra_binding` parameters, with defaults preserving 6B.

**Interfaces (Produces):**
- `authorize(conn, *, settings, account_id, application_workspace_id, review_hash, actor, now) -> dict` (`{"authorization_id","grant_id","expires_at"}`, or raises `SubmitRefused` with `stale_review`, `already_authorized`, a §7.2 reason, or a gate deny reason, one of `kill_switch|duplicate|stale_binding|human_submit_disabled|adapter_not_live_certified|...`)
- `cancel_authorization(conn, *, account_id, authorization_id, actor, now) -> dict`

**Steps:**
- [ ] **Tests:**
  - authorize on READY → one authorization, one ISSUED SUBMIT grant (TTL 120 s) whose binding has `authority == "HUMAN_SUBMIT"` and the `review_hash`;
  - **Review Focus 1:** a second authorize on the same run → `already_authorized`; two threads with separate connections → exactly one succeeds;
  - a wrong hash → `stale_review`;
  - the kill switch → `kill_switch`;
  - **Review Focus 4:** another workspace with a CONFIRMED `HUMAN_APPLIED` intent for the same identity → `duplicate`;
  - flag off → `human_submit_disabled`;
  - cancel before pre-click → the grant is REVOKED (`user_cancelled`) and an authorization-level `CANCELLED_BEFORE_DISPATCH` event is written;
  - `request_grant(stage=SUBMIT)` still raises `SubmissionNotAvailable`.
- [ ] Implement, then run the tests plus `tests/webapp/services -k "autonomy or submit"`.
- [ ] Commit `feat(submit): human SUBMIT authorization bound to the review hash; one-time SUBMIT grant through the human entry point`.

### Task 8: Pre-click commit, dispatch acknowledgement and attempt cancel

**Files:**
- Modify:
  - `webapp/services/autonomy.py`: `_pre_click_commit_core(..., authority=AuthorityKind.STANDING_POLICY, intent_source="AUTONOMOUS")`. Under HUMAN it skips the limit reservations and builds the context with `authority`. The public `pre_click_commit` is unchanged and still raises.
  - `webapp/services/human_submit.py`.
- Test: `tests/webapp/services/test_human_submit_preclick.py`.

**Interfaces (Produces):**
- `human_pre_click_commit(conn, *, settings, run_id, grant_id, observation, verification: dict, now) -> dict` (`{"attempt_id"}`, or raises `SubmitRefused` with `context_mismatch`, `verification_mismatch`, `review_changed`, `grant_not_consumable`, a gate reason, or `challenge_before_submit` when `verification["challenge_visible"]` is true). `verification` keys: `executor_instance_id, browser_session_id, execution_tab_id, canonical_url, observation_fingerprint, submit_control_fingerprint, ruleset_hash, challenge_visible`.
- `human_record_click_dispatched(conn, *, settings, attempt_id, now) -> bool`
- `cancel_attempt(conn, *, attempt_id, actor, now) -> dict` (only in AUTHORIZED)

**Steps:**
- [ ] **Tests:**
  - happy pre-click → the attempt is AUTHORIZED, the grant CONSUMED, and the intent CLAIMED with source `HUMAN_AUTHORIZED`, with no `limit_reservations` rows;
  - each verification key mismatch → its reason, with the grant REVOKED (`pre_click:<reason>`);
  - PRE_SUBMIT differing from the review → `review_changed`;
  - a second pre-click with the same grant → `grant_not_consumable`;
  - `challenge_visible` → `challenge_before_submit`, plus a `CHALLENGE_BEFORE_SUBMIT` event;
  - dispatch → True and `CLICK_DISPATCHED`;
  - dispatch after the kill switch → False, `EXPIRED_UNCLICKED` (SERVER, `{"halted": "kill_switch"}`), intent RELEASED;
  - dispatch after 60 s → False (the existing TTL);
  - `cancel_attempt` in AUTHORIZED → `EXPIRED_UNCLICKED` (USER) and intent RELEASED; after CLICK_DISPATCHED → `SubmitRefused("already_dispatched")`;
  - the autonomous `pre_click_commit` still raises.
- [ ] Implement and run the tests.
- [ ] Commit `feat(submit): human pre-click commit (exact review, context and control proofs), halt-checked dispatch acknowledgement, cancel before dispatch`.

### Task 9: Results, resolution, status and sweeps

**Files:**
- Modify:
  - `webapp/services/human_submit.py`;
  - `webapp/services/autonomy_scheduler.py` (in `_sweeps`: `expire_unclicked(conn, now=now)` and `mark_stale_dispatches_ambiguous(conn, now=now, result_timeout=DISPATCH_RESULT_TIMEOUT)`, both reduce-only).
- Test: `tests/webapp/services/test_human_submit_results.py`.

**Interfaces (Produces):**
- `record_submit_event(conn, *, attempt_id, event, detail, now) -> dict` (closed list)
- `report_result(conn, *, settings, attempt_id, evidence: dict, now) -> dict` (`{"state","proven_not_submitted","reason","result_hash"}`). It:
  - builds `ResultEvidence` from the evidence plus the stored events;
  - calls `determine_submit_result`;
  - calls `record_submission_result` (existing);
  - on success, confirms the intent, calls `record_status_change(new_status="applied", commit=False, note="Submitted via extension (human-authorized)")` and links `workflow_event_id`;
  - on FAILED, the intent is released;
  - writes `submission-result.v1`.

  All of this is one transaction. A second report → `SubmitRefused("result_already_recorded")`.
- `resolve(conn, *, account_id, attempt_id, submitted: bool, actor, now) -> str` (wraps the existing `resolve_ambiguous`; on `submitted=True` also records "applied" as above)
- `submission_status(conn, *, settings, account_id, application_workspace_id, now) -> dict` (`{"status": NOT_READY|SUBMIT_READY|SUBMITTING|CHALLENGE_WAITING|SUBMITTED|SUBMISSION_UNCLEAR|SUBMISSION_FAILED, "attempt_id", "reason"}`)

**Steps:**
- [ ] **Tests:**
  - success evidence → CONFIRMED_SUCCESS, intent CONFIRMED, one workflow "applied" event, and still exactly one live intent (the `record_human_intent` no-op);
  - employer validation error → FAILED proven, intent RELEASED;
  - no E1 match with feedback available → FAILED proven (skipped if `MATCHED_ALLOW_RULES_REPORTED` is False);
  - nothing observed → AMBIGUOUS; `resolve(True)` → CONFIRMED_SUCCESS (USER) plus applied; `resolve(False)` → FAILED;
  - content changed → AMBIGUOUS;
  - a second report refused;
  - the sweep: CLICK_DISPATCHED older than 360 s → AMBIGUOUS, and AUTHORIZED older than 60 s → EXPIRED_UNCLICKED;
  - `submission_status` covers each state;
  - `result_json` has no cleartext.
- [ ] Implement and run the tests plus `tests/webapp/services -k scheduler`.
- [ ] Commit `feat(submit): server-determined results, submission-result.v1, ambiguity resolution, submission status and reduce-only sweeps`.

### Task 10: Routes

**Files:**
- Create:
  - `webapp/api/submit_extension.py` (spec §18 extension routes, with the same session auth dependency as `webapp/api/fill_extension.py`);
  - `webapp/api/submit_app.py` (the JSON app routes: state, authorize, cancel, resolve);
  - `tests/webapp/api/test_submit_routes.py`.
- Modify:
  - `webapp/api/fill_extension.py` (the heartbeat response adds `reobserve` and `authorization` via `submit_review.pending_reobservation` and `human_submit.pending_authorization(conn, run_id, now)`, which returns an ISSUED, unconsumed, unexpired human grant for the run);
  - `webapp/app.py` (include the routers);
  - `webapp/services/human_submit.py` (`pending_authorization`).

**Steps:**
- [ ] **Tests**, one per route:
  - ownership → 404;
  - `SubmitRefused` → 409 with the reason;
  - an extra key → 422;
  - the heartbeat returns `authorization` only while the grant is ISSUED and unexpired, and `reobserve` only while a request is unanswered;
  - pre-click/dispatch/events/result/cancel happy paths.
- [ ] **Structural test** (`tests/webapp/test_submit_structure.py`), by AST over `webapp/`:
  - `_pre_click_commit_core` is called only from `autonomy.pre_click_commit` (which raises before the call) and from `human_submit.human_pre_click_commit`;
  - `_request_grant_core(... stage=SUBMIT ...)` is reachable only through `request_human_submit_grant`;
  - `request_human_submit_grant` is called only from `human_submit.authorize`, after `insert_authorization`.
- [ ] Run `.venv/Scripts/python -m pytest tests/webapp/api tests/webapp/test_submit_structure.py -q -p no:cacheprovider`.
- [ ] Commit `feat(submit): extension and app submit routes, heartbeat directives, and structural proof that SUBMIT authority is human-only`.

### Task 11: Extension egress and signals

**Files:**
- Create:
  - `extension/src/submit/egress.ts`;
  - `extension/src/submit/signals.ts`;
  - `extension/test/submit-egress.test.ts`;
  - `extension/test/submit-signals.test.ts`.
- Modify: `extension/manifest.json` (keep `declarativeNetRequestFeedback` only if S-E2 passed); `tests/test_extension_production_build.py` (the manifest permission list).

**Interfaces (Produces):**
- `SUBMIT_ALLOW_RULE_BASE = 9201`
- `SUBMIT_ALLOW_PRIORITY = 2000`
- `buildSubmitEgressRules(tabId, employerHost, egress: ResolvedEgress[]): Rule[]` (TOTAL rules via `buildTotalRules` + allow rules)
- `installSubmitEgress(tabId, employerHost, egress, api?) → Promise<{rulesetHash}>`
- `verifySubmitEgress(expectedHash, api?) → Promise<boolean>` (the exact fill-plus-submit id set; the canonical hash equals the expected one; no other allow rule of this extension)
- `restoreTotal(tabId, employerHost, totalHash, api?) → Promise<boolean>` (removes 9201–9209, then `verifyRuleset(totalHash)`)
- `matchedRuleIds(tabId, since) → Promise<{available: boolean, ids: number[]}>`
- signals: `detectSignals(document, cert): {success: boolean, failure: boolean, challenge: boolean}` (visibility per spec §12.1)

**Steps:**
- [ ] **vitest:**
  - rule shapes: TOTAL unchanged plus allows with the exact ids, priority, `tabIds`, methods, types and regex;
  - the hash is stable under reordering;
  - verify fails on a missing allow, an extra allow, an altered regex, or a removed TOTAL rule;
  - `restoreTotal` leaves exactly the TOTAL set;
  - **Review Focus 5 (TypeScript):** an escaped tenant regex doesn't match a lookalike;
  - signals: the success in-page swap, success on the confirmation URL, failure with the form plus `#error_explanation`, a hidden challenge iframe that isn't a challenge, and a visible `[data-submit-challenge]` that is one.
- [ ] Run `npm --prefix extension run typecheck && npm --prefix extension test`.
- [ ] Commit `feat(extension): SUBMIT egress ruleset (TOTAL plus certified allows) with exact read-back, TOTAL restore, matched-rule evidence and adapter signals`.

### Task 12: `SUBMIT_CLICK` executor and page API

**Files:**
- Create:
  - `extension/src/submit/submit-executor.ts` (`export function submitClick(el: Element): void { (el as HTMLElement).click(); }`, the only click in `extension/src`);
  - `extension/test/submit-callgraph.test.ts`.
- Modify:
  - `extension/src/fill/page-api.ts` and `page-bundle.ts`: `observeForSubmit(adapterId)` (the 6D-B `observe`), `findSubmitControl(adapterId, fingerprint) → Promise<boolean>` (exactly one match), `clickSubmit(adapterId, fingerprint) → Promise<"CLICKED"|"SUBMIT_CONTROL_MISSING">` (re-find, then `submitClick`), `signals(certificationId)`, `challengeVisible(certificationId)`;
  - `extension/test/single-writer-callgraph.test.ts` (allow `.click()` only in `submit-executor.ts`; `executor.ts` still has none).

**Steps:**
- [ ] **Call-graph test** (the TypeScript compiler API, as in 6D-B Task 14):
  - `.click(`, `.requestSubmit(`, `.submit(` and `dispatchEvent(new MouseEvent` occur only in `submit-executor.ts`;
  - `submitClick` is referenced only from `page-bundle.ts`'s `clickSubmit`;
  - `clickSubmit` is referenced only from `submit-controller.ts` (asserted in Task 13 once that file exists; here, assert it's referenced nowhere else).
- [ ] Run the extension tests.
- [ ] Commit `feat(extension): single SUBMIT_CLICK primitive in its own module; page API for submit observation, control proof and signals`.

### Task 13: SubmitController, wiring and popup

**Files:**
- Create:
  - `extension/src/submit/submit-controller.ts`;
  - `extension/src/submit/server.ts` (`HttpSubmitServer`: submitObservation, preClick, dispatch, event, result, cancel);
  - `extension/test/submit-controller.test.ts`.
- Modify:
  - `extension/src/background/fill-wiring.ts` (the heartbeat callback routes `reobserve` to `observeForReview` and `authorization` to `SubmitController.run` for that tab's controller; a popup message `submit_cancel` calls `SubmitController.cancel`);
  - `extension/src/fill/run-controller.ts` (the heartbeat exposes the response to a subscriber, additive);
  - `extension/src/popup/fill-view.ts` (the spec §16.3 texts);
  - `extension/src/popup/index.ts` (the Cancel button).

**Interfaces (Produces):**
- `class SubmitController { constructor(ports: SubmitPorts, ctx: SubmitContext); observeForReview(requestId): Promise<void>; run(auth: AuthorizationDirective): Promise<SubmitView>; cancel(): Promise<boolean>; static recover(store, ports, tabId): Promise<void> }`
- `SubmitView.phase ∈ {"REVIEW_OBSERVING","AUTHORIZED","DISPATCHING","SUBMITTING","CHALLENGE","SUBMITTED","UNCLEAR","NOT_SUBMITTED"}`
- persisted key `submit:<tabId>` → `{attemptId, phase: "DISPATCHING"|"DISPATCHED_ACK"|"CLICKED", dispatchedAt}`

**Steps:**
- [ ] **vitest** (fake timers, mocked ports), with one test per spec §10 step failure:
  - local proof failure → no pre-click call;
  - a visible challenge → `challenge_visible: true` sent, and no dispatch;
  - pre-click refusal;
  - dispatch false → no egress install;
  - egress verify failure → restore then result `{click_performed:false, cause:"EGRESS_NOT_VERIFIED"}`;
  - control missing → the same with `SUBMIT_CONTROL_MISSING`;
  - success → restore then result;
  - **Review Focus 3:** a timeout at 30 s → restore then result with nothing observed;
  - challenge then clear with an equal observation → continue; with a different observation → immediate restore and `content_changed:true`;
  - a content change detection mid-watch → immediate restore;
  - the challenge window of 300 s expires → restore;
  - cancel before dispatch → server cancel, no dispatch; cancel after `DISPATCHING` is persisted → false;
  - **recover:** a persisted `CLICKED` → restore then result `{click_performed:"UNKNOWN", cause:"EXECUTOR_RESTARTED"}`, and `clickSubmit` is never called; `DISPATCHING` → `click_performed:false`;
  - ordering (J3): the `DISPATCHING` persist happens before the `dispatch` call, and `verifySubmitEgress` before `clickSubmit`;
  - the call-graph assertion: `clickSubmit` is referenced only from `submit-controller.ts`, after `dispatch` and `verifySubmitEgress` in the same method.
- [ ] Run `npm --prefix extension run typecheck && npm --prefix extension test && npm --prefix extension run build`.
- [ ] Commit `feat(extension): SubmitController — proofs, durable dispatch, verified egress, single click, bounded watch, challenge handoff, restart safety; popup submit views`.

### Task 14: UI — Submit Review page and statuses

**Files:**
- Create:
  - `webapp/templates/submit_review.html`;
  - `tests/webapp/api/test_submit_pages.py`.
- Modify:
  - `webapp/api/review_pages.py` (`GET /workspaces/{id}/submit`: records a re-observation request and renders `current_review`, reusing the 6D-B fill-plan page's cleartext answer rendering for the exact rendered values);
  - `webapp/templates/prepared_applications.html` and `autonomy_dossier.html` (`submission_status` label and attempts list);
  - `webapp/api/applications.py` (`submission_status` in the Prepared rows).

**Steps:**
- [ ] **Page tests:**
  - PENDING_OBSERVATION shows "Checking the employer page…" and polls `…/submit/state`;
  - READY shows every §16.1 item, and the form posts `review_hash` in a hidden input;
  - BLOCKED shows each reason in plain language, and the button is `disabled`;
  - after authorize, the page shows the progress states from `/submit/state`;
  - AMBIGUOUS shows the two resolve buttons;
  - the Prepared list and dossier show the status labels;
  - no cleartext answers outside the Submit Review page (grep the rendered Prepared and dossier HTML for the fixture answer values).
- [ ] Implement it and run `.venv/Scripts/python -m pytest tests/webapp/api/test_submit_pages.py tests/webapp/api/test_review_pages.py -q -p no:cacheprovider`.
- [ ] Commit `feat(submit): Submit Review page with the single authorization act, progress and ambiguity resolution; submission statuses`.

### Task 15: Structural revisions, concurrency and the live-certification runbook

**Files:**
- Modify: the existing no-submit structural tests found by `grep -rln "submission_not_available\|no_submit\|SUBMIT" tests/webapp/test_*structure*.py tests/webapp/services/test_submit_refusal.py tests/webapp/api/test_fill_routes.py`. Each "no submit exists" assertion is revised to "submit is reachable only through the human path" (for example, the route inventory now lists the §18 routes, and each is asserted to go through `human_submit`). Every other assertion is kept verbatim.
- Create:
  - `tests/webapp/services/test_submit_concurrency.py`;
  - `docs/superpowers/notes/2026-09-29-greenhouse-live-submit-certification.md`.

**Steps:**
- [ ] **Concurrency** (separate connections, WAL, the file run 20×):
  - two authorizes → one;
  - authorize vs grant expiry → consistent;
  - pre-click vs cancel → exactly one wins;
  - a result report vs the ambiguous sweep → one terminal result;
  - two result reports → one.
- [ ] **Runbook:**
  - the live evidence needed to flip `greenhouse@2/submit@1` to `LIVE_CERTIFIED`: a controlled posting; the recorded request list proving the egress manifest is sufficient and minimal; success and failure signal screenshots; challenge behaviour; the Chrome version; the reviewer sign-off;
  - that the flip is a code change setting `status` and `live_evidence` in both catalogues, plus a test.
- [ ] Run the changed structural tests and the concurrency file 20×.
- [ ] Commit `test(submit): human-only SUBMIT structural invariants, submit concurrency, and the Greenhouse live-certification runbook`.

### Task 16: Final validation

**Files:**
- Create:
  - `tests/webapp/test_submit_acceptance_browser.py`;
  - `tests/webapp/fixtures/fill/submit/*` scenario scripts (extending Task 1's fixtures: validation error, client validation block, no-signal, in-page challenge `[data-submit-challenge]` with a "complete" button the test clicks as the human, the exfiltration attempts during egress);
  - `tests/webapp/persistence/test_submit_upgrade.py` (`git archive efc6577`, skipped only when that commit is absent, as in the 6D-B follow-up).

**Steps:**
- [ ] **Browser acceptance**, one test per spec §22 item 1–16, driving the real app, extension (test-hook build) and fixtures. Set `JOBSEARCH_HUMAN_SUBMIT_ENABLED=1` and `JOBSEARCH_SUBMIT_FIXTURE_ORIGINS=1` for the live server, except in the live-gate tests.
- [ ] **Upgrade test:** a DB built by `efc6577` code with intents, attempts and a FILLED 6D-B run → `021` applied, rows preserved, FK and integrity clean, a re-run a no-op.
- [ ] **Full suite:** the six chunks, plus `npm --prefix extension run typecheck && npm --prefix extension test && npm --prefix extension run build`. The production `dist/extension` must have no test hooks, no spike code and no loopback host permission. Record the totals.
- [ ] **Diff boundary** vs `efc6577`: only File Map paths and tests. `git diff efc6577 -- webapp/persistence/migrations.py` is additive except the declared `submission_intents` rebuild inside `_migrate_human_submit`. No 6D-A or 6D-B table, CHECK or binding changes.
- [ ] Commit `test(submit): 6E-A browser acceptance, upgrade and boundary validation`. Then stop for the user's single end-of-bundle review. No push, PR or merge without instruction.

---

## Self-review

- **Spec coverage:**

  | Spec | Tasks |
  |---|---|
  | §3 decisions E1–E20 | Global Constraints and all tasks |
  | §5 invariants | J1 → 7/10; J2 → 6/8; J3 → 13; J4/J5 → 11/13; J6/J7 → 3/9; J8 → 5/7/15; J9 → 3/5/14; J10 → 16; J11 → 5 |
  | §7 | 3, 6 |
  | §8 | 4, 7, 8 |
  | §9 | 2, 11 |
  | §10 | 13 |
  | §11 | 3, 9, 11 |
  | §12 | 11, 13 |
  | §13 | 8, 9, 13 |
  | §14 | 5, 7, 15 |
  | §15 | 3, 9 |
  | §16 | 13, 14 |
  | §17 | 5 |
  | §18 | 10 |
  | §19 | 11–13 |
  | §20 | 2 |
  | §21 | 1 |
  | §22 | 16 |
  | §24 | 4, 5, 7–10, 13 |

- **Placeholders:** none. Every interface above has its name and signature, and each test list names concrete inputs and expected outputs.
- **Names used consistently:** `submission_permitted`, `resolve_egress`, `build_review_snapshot`, `review_hash`, `determine_submit_result`, `request_human_submit_grant`, `human_pre_click_commit`, `human_record_click_dispatched`, `report_result`, `pending_authorization`, `installSubmitEgress`, `verifySubmitEgress`, `restoreTotal`, `clickSubmit` and `submitClick`.
- **Review Focus** items are pinned in Tasks 2, 3, 6, 7, 11 and 13.
