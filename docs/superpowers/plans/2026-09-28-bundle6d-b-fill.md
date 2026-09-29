# Bundle 6D-B: FILL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:executing-plans. Per the user's standing preference, the work is sequential and in the main session, with foreground self-review and no subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill an approved application's single-page employer form, only from a user-confirmed closed plan, inside a verified network quarantine, ending at `FILLED_AWAITING_SUBMISSION` with evidence and no way to submit.

**Architecture:**
- **Server-authoritative:** pure `product/` modules define the observation, the plan, change classification, G4 coverage and the certification catalogue; `webapp/services/fill_*` run the run lifecycle and authorization.
- **Thin MV3 extension:** it observes (ISOLATED world, read-only), installs and verifies `declarativeNetRequest` session rulesets, and executes one server-authorized primitive at a time.
- **Legacy removed:** the Phase 3 autofill writer is removed, so exactly one employer-page writer remains.

**Tech Stack:**
- Python 3.12, FastAPI, SQLite (WAL), pytest, pytest-playwright (packed extension via `launch_persistent_context`);
- TypeScript MV3 extension (esbuild `scripts/build.mjs`, vitest, jsdom), with `chrome.declarativeNetRequest`, `chrome.scripting`, `chrome.storage.session`, and optional `tabs`/`webNavigation`.

**Spec:** `docs/superpowers/specs/2026-09-27-bundle6d-b-fill-design.md`, **frozen at `e71b830`**. Section references (§) point to it. 6D-A is `docs/superpowers/specs/2026-09-26-bundle6d-a-review-approval-design.md`.

**Branch:** `bundle6/6d-b-fill`, on top of the frozen spec commits. Commits stay local. Push, PR, merge and tag happen only on the user's explicit instruction.

## Global Constraints

- **6D-A is unchanged:** no change to its tables, CHECKs, binding schema (`application-approval-binding.v1`) or event vocabulary. 6D-B reads the effective binding through `review_state(...)`, and opens deltas only through the 6D-A intake.
- **Delta kinds are exactly** `NEW_QUESTION`, `CHANGED_QUESTION`, `DECLARATION`, `TRANSFORM_FAILURE`, `OMIT_FIELD_REQUIRED`, `NEW_UPLOAD`, `DOCUMENT_CONVERSION`, `TARGET_CHANGE`.
- **Action kinds are exactly** `WRITE`, `ATTACH_LOCAL`, `OMIT`, `IGNORE_NON_APPLICATION`.
- **Executor primitives are exactly** `SET_TEXT`, `SET_SELECT`, `SET_CHECKED`, `SET_FILES_LOCAL`, `DISPATCH_INPUT`, `DISPATCH_CHANGE`, in the ISOLATED world only.
- **Stop reasons** are the closed list of spec §14, and `UNSUPPORTED_FORM.cause` is the closed list of §14.
- **Timing (`fill-timing.v1`):**

  | Constant | Value |
  |---|---|
  | `HEARTBEAT_INTERVAL` | 10 s |
  | `RUN_LEASE_TTL` | 45 s |
  | `VALUE_ENVELOPE_TTL` | 30 s |
  | `SETTLE_QUIET_PERIOD` | 50 ms |
  | `SETTLE_CAP` | 1 s |

  These are named constants only (Python `product/fill_constants.py`, TypeScript `extension/src/fill/constants.ts`).
- **`QUARANTINE_RESOURCE_TYPES_V1`:** `main_frame, sub_frame, stylesheet, script, image, font, object, xmlhttprequest, ping, csp_report, media, websocket, webtransport, webbundle, other`.
- **Rulesets:**
  - PRELOAD v1 = Q1-PRE (block `post, put, patch, delete, connect, options` for all types, plus types `websocket, webtransport, ping` for any method) + Q2;
  - TOTAL v1 = Q1 (the tab: all types, every method) + Q2 (`TAB_ID_NONE` + employer initiator);
  - **no Q3**.
- **Manifest:** add `"declarativeNetRequest"` to `permissions`, and `"optional_permissions": ["tabs", "webNavigation"]`. `host_permissions` are unchanged.
- **No cleartext answer value** in any table, log, result or observation. Cleartext exists only in a value envelope in memory and on the fill-plan HTML page.
- **Every migration is atomic**, with no `executescript`. Append-only tables get UPDATE/DELETE-raising triggers.
- **HTTP:** ownership failure → 404; `ReviewRefused`/domain refusal → 409 with the exact reason; validation → 422; extra body keys are forbidden.
- **No submit capability:** no route, primitive, message or UI may submit, lift the quarantine, or accept a SUBMIT stage. The 6D-A G2 structural tests must keep passing.
- **Windows file writes** use the Edit/Write tools, or Python with `encoding="utf-8"`. Never platform-default encodings.

## Review Focus

These are the input classes the spec implies but no requirement names. Each has a pinned test in its owning task.

1. **Questions labelled only by `aria-labelledby`, `placeholder` or a wrapping element**, not `label[for]`. Expected: a stable `field_fingerprint` with the real question text, or `AMBIGUOUS_FIELD_IDENTITY`, never an empty-question row the user can't judge. Tested in **Task 11**.
2. **Unicode answers**: emoji, RTL text, combining characters, CRLF inside a textarea. Expected: TypeScript and Python hash identically after NFC and `\n` normalization, and the readback verifies. Tested in **Task 3** (vectors) and **Task 12**.
3. **Select options that differ only by whitespace or case**, where the approved rendered value matches more than one. Expected: `TRANSFORM_FAILURE`, never the first match. Tested in **Task 6**.
4. **A form that mounts late after the reset reload (SPA lazy load).** Expected: REVALIDATING waits for the adapter's application root, bounded by `SETTLE_CAP`-based polling up to a named `REVALIDATION_MOUNT_TIMEOUT = 10 s`. Only then does it compare, so the run isn't stopped with a spurious `STRUCTURE_CHANGED` or `OBSERVATION_MISMATCH`. Tested in **Task 13**.
5. **The user types into the page during FILLING.** Expected: a pending WRITE target that's no longer blank → `PREFILLED_VALUE_CONFLICT`; a completed field edited → `FIELD_VALUE_REVERTED`; never an overwrite. Tested in **Task 13**.

---

## File Map

| Path | Responsibility | Task |
|---|---|---|
| `extension/test/spikes/*`, `tests/webapp/spikes/test_fill_spikes_browser.py`, `docs/superpowers/notes/2026-09-28-6d-b-spike-results.md` | S1 and S3 spikes, with recorded results | 1 |
| `extension/src/fill/quarantine.ts`, `tests/webapp/test_fill_quarantine_browser.py`, `tests/webapp/fixtures/fill/` | rulesets, install and read-back verification; S2 enforcement suite | 2 |
| `extension/src/fill/siblings.ts` | sibling tab and frame containment (S4) | 2 |
| `product/fill_constants.py`, `extension/src/fill/constants.ts`, `product/fill_hash.py`, `extension/src/fill/hash.ts`, `tests/fixtures/fill/hash_vectors.json` | timing constants, the canonical value hash, cross-language vectors (S5) | 3 |
| `webapp/persistence/migrations.py` (`020_fill`), `webapp/persistence/fill.py` | tables and persistence | 4 |
| `product/fill_observation.py`, `product/fill_certification.py` | observation v1, fingerprints, proofs, certification catalogue | 5 |
| `product/fill_plan.py`, `product/fill_changes.py` | plan builder, plan hash, manifest derivation, G4 coverage, change classification | 6 |
| `webapp/services/fill_plans.py`, `webapp/services/fill_classification.py`, `webapp/services/review_approval.py` (additive transaction core) | proposal, mapping choices, confirmation; R7 classification | 7 |
| `webapp/services/fill_runs.py` | run start, lease, observation intake, quarantine events, grant | 8 |
| `webapp/services/fill_actions.py`, `webapp/services/fill_results.py` | intents, envelopes, outcomes, final validation, `fill-result.v1`, fill status | 9 |
| `webapp/api/fill_extension.py`, `webapp/api/fill_app.py`, `webapp/app.py` | routes | 10 |
| `extension/src/fill/observer.ts` | read-only observation v1 | 11 |
| `extension/src/fill/executor.ts`, `extension/src/fill/settle.ts`, `extension/test/fill-executor-allowlist.test.ts` | primitives, settle, readback, allowlist | 12 |
| `extension/src/fill/run-controller.ts`, `extension/src/background/index.ts`, `extension/manifest.json`, `extension/scripts/build.mjs`, `extension/src/popup/*` | state machine, persistence, heartbeat, detections, popup | 13 |
| legacy files (Task 14 list), `extension/test/single-writer-callgraph.test.ts` | legacy retirement and the single-writer proof | 14 |
| `webapp/templates/fill_plan.html`, `webapp/templates/review_application.html`, `prepared_applications.html`, `autonomy_dossier.html`, `webapp/api/review_pages.py` | fill-plan page, classification confirmation, statuses | 15 |
| `tests/webapp/test_fill_acceptance_browser.py`, `tests/webapp/services/test_fill_concurrency.py`, `tests/webapp/persistence/test_fill_upgrade.py` | final validation | 16 |

---

### Task 1: Spikes S1 (isolated-world file placement with byte readback) and S3 (controlled inputs). **Gate.**

**Objective.** Answer, with evidence and before anything depends on it:
- **(S1)** Can an ISOLATED-world content script place a `File` into an `<input type=file>` through `DataTransfer`, so that page scripts (React- and Vue-controlled fixtures) observe it? Can the script then read back **the exact bytes** of `input.files[0]` and SHA-256 them?
- **(S3)** Do ISOLATED-realm prototype setters followed by `input`/`change` events register in React- and Vue-controlled text, select, checkbox and radio fields, **without click**?

**Files:**
- Create:
  - `tests/webapp/fixtures/fill/spike_react.html` and `spike_vue.html` (self-contained, framework builds vendored under `tests/webapp/fixtures/fill/vendor/`);
  - `extension/src/fill/spike-entry.ts`, a throwaway ISOLATED bundle built only when `FILL_SPIKES=1`;
  - `tests/webapp/spikes/test_fill_spikes_browser.py`;
  - `docs/superpowers/notes/2026-09-28-6d-b-spike-results.md`.
- Modify: `extension/scripts/build.mjs`, adding a spike entry gated by `process.env.FILL_SPIKES === "1"`, so production builds never include it.

**Steps:**
- [ ] Write a Playwright test (the `launch_persistent_context` + `--load-extension` pattern of `tests/webapp/test_extension_pairing_acceptance.py::extension_context`). It serves the fixtures from a local HTTP server and injects `spike-entry.js` with `chrome.scripting.executeScript({world: "ISOLATED"})` through a test-only message.
  - It places a 37 KB DOCX `File` (the bytes of `tests/webapp/fixtures/fill/sample.docx`) and asserts:
    - (a) the page's React/Vue `onChange` fired and its state shows the filename;
    - (b) the ISOLATED script's `await input.files[0].arrayBuffer()` SHA-256 equals the fixture's SHA-256;
    - (c) name, size and type equal the fixture's.
  - For S3 it sets text, select, checkbox and radio, and asserts framework state after one settle. It records per kind whether state persisted after a forced re-render.
- [ ] Run it: `FILL_SPIKES=1 npm --prefix extension run build`, then `.venv/Scripts/python -m pytest tests/webapp/spikes -q -p no:cacheprovider`.
- [ ] Record the results in the notes file: pass/fail per assertion, Chrome version, and the S3 per-control-kind table. This table becomes the per-adapter `supported_control_kinds` input for Task 5.
- [ ] **GATE.**
  - If S1 (b) fails (exact byte readback is impossible in the ISOLATED world), **stop and report to the user**. Don't weaken D6/I5 or continue past Task 1 without a ruling.
  - If S1 passes, report the results in one short message and continue.
  - If S3 shows click-only kinds, those kinds are simply not certifiable (no user ruling needed).
- [ ] Commit `test(fill): S1/S3 spikes — isolated-world file placement with byte readback and controlled inputs` (the spike bundle stays behind the env flag).

### Task 2: Quarantine rulesets (S2) and sibling containment (S4)

**Objective.** Production modules for the PRELOAD/TOTAL rulesets and sibling containment, plus the S2/S4 browser proof suite.

**Files:**
- Create:
  - `extension/src/fill/quarantine.ts`;
  - `extension/src/fill/siblings.ts`;
  - `extension/test/fill-quarantine.test.ts`;
  - `extension/test/fill-siblings.test.ts`;
  - `tests/webapp/fixtures/fill/quarantine_probe.html` (buttons that fire each S2 probe), `sw.js` (a service worker doing `fetch`) and `ws_page.html` (opens a WebSocket before the run);
  - `tests/webapp/fixtures/fill/recording_server.py`, which records every request and socket event;
  - `tests/webapp/test_fill_quarantine_browser.py`.
- Modify: `extension/manifest.json` (`declarativeNetRequest`; `optional_permissions: ["tabs","webNavigation"]`).

**Interfaces (Produces):**
- `QUARANTINE_RESOURCE_TYPES_V1: readonly string[]`.
- `buildPreloadRules(tabId: number, employerHost: string): chrome.declarativeNetRequest.Rule[]` and `buildTotalRules(tabId, employerHost)`. Rule ids are fixed: Q1-PRE=9101, Q1=9111, Q2=9121.
- `canonicalRulesetHash(rules): Promise<string>` (SHA-256 of the canonical JSON of sorted rules).
- `installRuleset(kind: "PRELOAD"|"TOTAL", tabId, employerHost): Promise<{rulesetHash: string}>` (replaces the fill rule ids atomically via `updateSessionRules({removeRuleIds, addRules})`).
- `verifyRuleset(expectedHash): Promise<boolean>` (`getSessionRules()` → filter the fill ids → canonical hash equals the expected hash).
- `removeRuleset(): Promise<void>`, called only on tab close.
- `checkSiblingContainment(executionTabId: number, employerOrigin: string): Promise<{ok: true} | {ok: false, reason: "SIBLING_EMPLOYER_CONTEXT_OPEN" | "PERMISSIONS_MISSING"}>`. It uses `chrome.tabs.query({})` and `chrome.webNavigation.getAllFrames` per tab, and matches the frame URL origin.

**Steps:**
- [ ] vitest (mocking `chrome.*`):
  - the rule shapes match the spec exactly (the TOTAL Q1 has every type in `QUARANTINE_RESOURCE_TYPES_V1` and no `requestMethods` filter; the PRELOAD Q1-PRE has the method list and the three types);
  - the hash is stable under rule reordering and changes with any field;
  - `verifyRuleset` fails on a missing rule, an extra fill rule, or an altered condition;
  - sibling detection: a second tab on the origin, a same-origin frame in another tab, and missing permissions each give their reason; a different-origin frame is ok.
- [ ] Playwright S2 (spec §22 table), under a **test-only background message** that installs a ruleset for the fixture tab (compiled only when `FILL_TEST_HOOKS=1`):
  - **PRELOAD:** assert every "blocked" probe never reaches the recording server and every "allowed" probe does (GET fetch, image beacon, script GET, GET navigation). Assert Q2 blocks the service worker's `fetch`.
  - **TOTAL:** assert zero recorded requests for every probe, including GET, the image beacon and navigation. Assert Q2 blocks the service worker's `fetch`, and that the extension's own call to the app server succeeds.
  - **Reset:** open the page-owned WebSocket, install PRELOAD, reload, and assert the recording server logs the close and receives no frames afterwards.
  - **`window.open`:** from a synthetic event the popup is blocked. If a tab does open, `checkSiblingContainment` reports it.
  - **Certification refusal fixture:** a page whose service worker holds a WebSocket is flagged by `tests/webapp/fixtures/fill/certify.py::uncontained_channels()`, and this marks the fixture non-certifiable. The same checker is used by Task 5's catalogue test.
- [ ] Playwright S4: a second tab and a same-origin frame embedded in another tab are both detected. Record the prerender and worker blind spots in the notes file.
- [ ] Commit `feat(fill): quarantine rulesets with exact read-back verification and sibling containment (S2/S4)`.

### Task 3: Timing constants and the cross-language canonical value hash (S5)

**Files:**
- Create:
  - `product/fill_constants.py`;
  - `extension/src/fill/constants.ts`;
  - `product/fill_hash.py`;
  - `extension/src/fill/hash.ts`;
  - `tests/fixtures/fill/hash_vectors.json`;
  - `tests/product/test_fill_hash_vectors.py`;
  - `extension/test/fill-hash-vectors.test.ts`;
  - `tests/product/test_fill_constants.py`.

**Interfaces:**
- Python `fill_value_hash(value: str) -> str`: NFC, then `\r\n`/`\r` → `\n`, then `canonical_hash("fill-rendered-value", "v1", normalized)`. This is **the** `rendered_value_hash` and `current_value_hash` function.
- TypeScript `fillValueHash(value: string): Promise<string>`, byte-identical to it (a canonical JSON envelope `{"schema":"fill-rendered-value","schema_version":"v1","payload":…}` with the key ordering and separators of `product.autonomy_contract.canonical_json`; `crypto.subtle` SHA-256; `"sha256:"` prefix).
- `FILL_TIMING_VERSION = "fill-timing.v1"`, plus the five constants of Global Constraints and `REVALIDATION_MOUNT_TIMEOUT = 10 s`.

**Steps:**
- [ ] Vectors (≥ 20): ASCII, whitespace, empty string, emoji, RTL, combining characters (decomposed and precomposed hash the same), CRLF/CR/LF (hash the same), a 10 000-character string, and JSON-hostile characters (quotes, backslash, U+2028). **(Review Focus 2.)**
- [ ] Both suites load the same JSON and assert every expected hash. A generator script, `tools/gen_fill_hash_vectors.py`, writes the expected values from Python. The TypeScript suite must pass unmodified.
- [ ] Constants tests: the Python and TypeScript files declare the same version string and values. The Python test parses `constants.ts` with a regex, and fails on drift.
- [ ] Commit `feat(fill): versioned timing constants and cross-language canonical value hash (S5)`.

### Task 4: Migration `020_fill` and persistence

**Files:**
- Modify: `webapp/persistence/migrations.py`, adding `FILL_MIGRATION_ID = "020_fill"` and `_migrate_fill`, registered with `disable_foreign_keys=False`.
- Create:
  - `webapp/persistence/fill.py`;
  - `tests/webapp/persistence/test_fill_migration.py`;
  - `tests/webapp/persistence/test_fill_persistence.py`.

**Tables** (spec §19; append-only ones get UPDATE/DELETE triggers raising `'<table> is append-only audit history'`):
- `fill_observations(seq, id, account_id, application_workspace_id, fill_run_id NULL, phase CHECK IN ('INITIAL','REVALIDATION','PRE_ACTION','POST_ACTION','FINAL','POST_FILL'), action_index NULL, structure_fingerprint, observation_fingerprint, observation_json, created_at)`;
- `fill_plans(seq, id, account_id, application_workspace_id, plan_hash UNIQUE, approval_id, approval_binding_hash, observation_id, plan_json, created_at)`;
- `fill_plan_mapping_choices(seq, id, account_id, application_workspace_id, observation_id, page_field_key, field_fingerprint, answer_key NULL, choice CHECK IN ('MAP','NEW_QUESTION'), actor, created_at)`;
- `fill_plan_confirmations(seq, id, account_id, application_workspace_id, plan_hash, approval_id, approval_binding_hash, actor, created_at)`;
- `delta_classification_proposals(seq, id, account_id, application_workspace_id, delta_id, subject, basis CHECK IN ('HEURISTIC','MODEL'), created_at)`;
- `delta_classification_confirmations(seq, id, account_id, application_workspace_id, delta_id, proposal_id, subject, successor_delta_id, actor, created_at)`;
- `fill_runs(seq, id, account_id, application_workspace_id, handoff_session_id, executor_instance_id, browser_session_id, execution_tab_id, timing_version, created_at)`;
- `fill_run_events(seq, id, fill_run_id, event CHECK IN (<run states>), reason NULL CHECK IN (<§14 list>), detail_json, created_at)`;
- `fill_run_grant_bindings(seq, id, fill_run_id UNIQUE, grant_id, approval_id, approval_binding_hash, plan_hash, structure_fingerprint, observation_fingerprint, ruleset_hash, created_at)`;
- `fill_action_events(seq, id, fill_run_id, action_index, event CHECK IN ('PRECHECK','WRITE_INTENT','ENVELOPE_ISSUED','OUTCOME'), outcome NULL CHECK IN (<§11.3 d list>), envelope_id NULL, readback_hash NULL, detail_json, created_at)`, with a partial unique index on `(fill_run_id, action_index) WHERE event='OUTCOME'` (at most one terminal outcome);
- `fill_quarantine_events(…, phase CHECK IN ('PRELOAD_INSTALLED','RELOADED','TOTAL_VERIFIED','VERIFIED_BEFORE_ACTION','LOST'), ruleset_hash, …)`;
- `fill_detection_events(…, kind CHECK IN ('SUBMIT_ATTEMPT_OBSERVED','NAVIGATION_ATTEMPT_OBSERVED','POST_FILL_CHANGE_OBSERVED','EXECUTION_CONTEXT_CLOSED'), …)`;
- `fill_results(seq, id, fill_run_id UNIQUE, result_hash, result_json, created_at)`;
- mutable: `fill_run_leases(fill_run_id PK, expires_at, updated_at)`;
- mutable: `active_fill_runs(application_workspace_id PK, fill_run_id UNIQUE, context_key UNIQUE)`, where `context_key = executor_instance_id|browser_session_id|execution_tab_id`.

**Steps:**
- [ ] Tests:
  - a fresh DB has 20 migrations and a re-run is a no-op;
  - every append-only table rejects UPDATE/DELETE;
  - each CHECK rejects an out-of-vocabulary value;
  - the partial unique index rejects a second OUTCOME for the same action;
  - `active_fill_runs` rejects a second run for the same application and for the same `context_key`;
  - **6D-A tables and triggers are byte-identical before and after** (compare the `sqlite_master` rows for `application_*`, `review_deltas`, `approved_answers`).
- [ ] Persistence functions (no commit; callers own transactions):
  - `insert_observation`, `insert_plan`/`get_plan_by_hash`;
  - `insert_mapping_choice`/`current_mapping_choices(observation_id)`;
  - `insert_plan_confirmation`/`plan_confirmed(plan_hash, approval_id, binding_hash) -> bool`;
  - `insert_classification_proposal`/`latest_proposal(delta_id)`, `insert_classification_confirmation`;
  - `insert_run`/`get_run`, `append_run_event`/`run_state(run_id)` (latest event);
  - `insert_grant_binding`/`get_grant_binding`;
  - `append_action_event`/`action_outcome(run_id, i)`/`issued_envelope(run_id, i)`;
  - `append_quarantine_event`, `append_detection_event`;
  - `insert_result`;
  - `claim_active_run`/`release_active_run`, `touch_lease`/`expired_leases(now)`.

  Assert that none of them writes a cleartext value (a helper scans inserted JSON for a sentinel value used in the test).
- [ ] Commit `feat(fill): migration 020_fill with append-only fill evidence and run concurrency keys`.

### Task 5: Observation v1 and the certification catalogue (pure)

**Files:**
- Create:
  - `product/fill_observation.py`;
  - `product/fill_certification.py`;
  - `tests/product/test_fill_observation.py`;
  - `tests/product/test_fill_certification.py`.

**Interfaces:**
- `validate_observation(doc) -> None` (strict keys, per spec §7.1; `value_state` is `{"state":"BLANK"}` or `{"state":"NONBLANK","current_value_hash": "sha256:…"}`; raises `ObservationError(errors)`).
- `structure_fingerprint(doc) -> str`.
- `observation_fingerprint(doc) -> str`.
- `unsupported_causes(doc, catalogue) -> list[str]`, returning the closed §14 causes: `MULTI_STEP`, `UNCERTIFIED_ADAPTER`, `NO_APPLICATION_ROOT`, `CROSS_ORIGIN_APPLICATION_FRAME`, `AMBIGUOUS_FIELD_IDENTITY` (only when it isn't user-resolvable).
- `ambiguous_identities(doc) -> list[list[str]]`.
- Certification: `AdapterCertification(adapter_id, adapter_version, network_model, supported_control_kinds: frozenset[str], mapping_rules: tuple[MappingRule,...], non_application_rules, classification_rules, declaration_patterns)`, `certified(adapter_id, adapter_version) -> AdapterCertification | None`. The initial `CATALOGUE` has Greenhouse and Lever entries, where `supported_control_kinds` come from the Task 1 S3 table.
- `MappingRule(rule_id, version, match: callable(element) -> bool, target: ("answer_key", str) | ("document_kind", str))`, a deterministic label/structure match.

**Steps:**
- [ ] Tests:
  - fingerprint sensitivity: structure changes with label, options order, required, accept or frame; it is unchanged by value state. The observation fingerprint changes with value state;
  - non-application proofs are accepted only as the three kinds (`type=hidden` without an adapter rule stays APPLICATION);
  - each unsupported cause;
  - identity ambiguity is never resolved by order.
- [ ] Certification tests:
  - generic and unknown adapters return `None`;
  - every mapping and classification rule has at least one positive and one negative fixture (label strings), and maps only to registered semantic subjects (`product.semantic_subject_registry.SEMANTIC_SUBJECTS`) or contact keys;
  - `network_model == "NO_UNCONTAINED_PERSISTENT_CHANNELS"` for every entry;
  - the Task 2 `uncontained_channels()` checker returns empty for each certified adapter's fixture pages.
- [ ] Commit `feat(fill): observation v1 fingerprints and certified-adapter catalogue`.

### Task 6: Plan builder, G4 coverage and change classification (pure)

**Files:**
- Create:
  - `product/fill_plan.py`;
  - `product/fill_changes.py`;
  - `tests/product/test_fill_plan.py`;
  - `tests/product/test_fill_g4.py`;
  - `tests/product/test_fill_changes.py`.

**Interfaces:**
- `build_plan(*, observation, binding, approval_id, binding_hash, catalogue_entry, mapping_choices, values: Mapping[str, str], documents: Mapping[str, dict]) -> PlanResult`.
  - `values` maps `answer_key` → the approved cleartext, used **only** to compute `rendered_value_hash` through `apply_transform` + `fill_value_hash`. It is never stored.
  - `PlanResult(plan: dict | None, deltas: list[DeltaSpec], needs_review: list[ReviewRow], unsupported: list[str])`.
  - `DeltaSpec(kind, answer_key|None, subject|None, required, question, observed)`.
- `plan_hash(plan) -> str`.
- `derive_manifest(plan, *, confirmation_ids: Mapping[str, str]) -> dict`: fill-manifest v1 per spec §11.2.1, validated by `product.fill_manifest.validate_fill_manifest`.
- `g4_violations(manifest, plan, binding) -> list[str]`: the six rules. An empty list means covered.
- `classify_changes(plan_structure, observation, *, completed: set[int], catalogue_entry) -> ChangeResult(deltas, stop_reason|None)`: the §13 table.

**Steps:**
- [ ] Plan tests, one per §8.2/§8.3 row:
  - WRITE requires an ANSWER field, a permitted transform and a supported control kind;
  - a select/radio with no exact option → `TRANSFORM_FAILURE`;
  - **whitespace/case-duplicate options matching twice → `TRANSFORM_FAILURE` (Review Focus 3)**;
  - `maxlength` exceeded → `TRANSFORM_FAILURE`;
  - ATTACH_LOCAL with a clear accept mismatch → `DOCUMENT_CONVERSION`; an ambiguous accept → unsupported;
  - OMIT precondition BLANK;
  - a prefilled WRITE with a different hash is a precondition conflict; an equal hash gives precondition `BLANK|EQUAL`;
  - custom-widget rules (an approved OMIT + blank is ok; an approved ANSWER → `UNSUPPORTED_REQUIRED_WIDGET`; never a downgrade);
  - a new question → a `NEW_QUESTION` DeltaSpec;
  - approved content with no deterministic mapping → a `needs_review` row whose candidates include **only compatible** answer keys;
  - IGNORE only with a proof;
  - **no action or plan JSON contains a cleartext value** (a sentinel-scan test).
- [ ] Plan-hash tests: stable under dict order; changes with any action field, the approval id or the binding hash.
- [ ] G4 tests (spec acceptance 19): each of the six rules has a failing fixture producing exactly its violation; OMIT and IGNORE never yield entries; the manifest `value_hash` equals the approved hash (not rendered); `PACK_DOCUMENT.ref` equals the sha256.
- [ ] Change tests: every §13 row, including the combination "two new fields in one diff" → both deltas and one stop.
- [ ] Commit `feat(fill): plan builder, G4 manifest coverage and change classification`.

### Task 7: Fill-plan services (proposal, mapping, confirmation) and R7 classification

**Files:**
- Create:
  - `webapp/services/fill_plans.py`;
  - `webapp/services/fill_classification.py`;
  - `tests/webapp/services/test_fill_plans.py`;
  - `tests/webapp/services/test_fill_classification.py`;
  - `tests/webapp/services/fill_fixtures.py`, which extends the 6D-A `v2_chain` world with an effective approval and a canned certified observation.
- Modify: `webapp/services/review_approval.py`, adding `open_review_delta_in_transaction(conn, …)`. The existing `open_review_delta` becomes `run_immediate(conn, lambda: open_review_delta_in_transaction(...))`, with **behaviour unchanged**. Its existing 6D-A tests must pass untouched.

**Interfaces:**
- `propose_plan(conn, *, settings, account_id, application_workspace_id, observation_id, now) -> ProposeOutcome(plan_hash|None, state: "PLAN_PROPOSED"|"PLAN_NEEDS_REVIEW"|"UNSUPPORTED_FORM"|"DELTAS_OPENED", details)`. It opens DeltaSpecs through `open_review_delta_in_transaction` with `source=f"FILL_RUN:{run_id}"` or `"FILL_OBSERVATION:{observation_id}"`, and stores proposals for unclassified deltas.
- `record_mapping_choice(conn, …, page_field_key, answer_key|None, choice)`, which refuses (409 `incompatible_mapping`) an answer key not among the row's compatible candidates.
- `fill_plan_presentation(conn, …) -> dict`, one read transaction (the `review_payload` pattern: `BEGIN` → build → rollback). It returns rows with their **cleartext rendered value for display** plus `displayed_plan_hash`.
- `confirm_plan(conn, …, displayed_plan_hash, actor, now)` in `BEGIN IMMEDIATE`: recompute, and on an exact match append the confirmation, otherwise `ReviewRefused("stale_plan")`.
- `confirm_classification(conn, …, delta_id, displayed_proposal_id, subject, actor, now)`: one transaction that inserts the classified successor through `open_review_delta_in_transaction` (subject + the same `observed.field_key`) and the `delta_classification_confirmations` row. A retry returns the same successor and writes nothing.

**Steps:**
- [ ] Tests:
  - a proposal with every deterministic mapping → `PLAN_PROPOSED`;
  - a non-deterministic row → `PLAN_NEEDS_REVIEW`;
  - a new question → a delta opened through the 6D-A intake (6D-A state becomes `NEEDS_REVIEW`);
  - an incompatible mapping choice refused;
  - the presentation matches the plan's `rendered_value_hash` values;
  - confirm with a stale hash → `stale_plan`, zero rows;
  - confirm → one row, and reuse only for the identical tuple;
  - a new 6D-A approval invalidates the old confirmation (`plan_confirmed` → False);
  - classification: a deterministic rule opens a classified delta directly; a proposal alone leaves the delta unclassified and **not answerable** (6D-A `answer_field` → `unclassified_subject`);
  - confirm → the predecessor resolved `{reason:"classified"}` + the successor + the confirmation row atomically; a retry writes nothing;
  - the classification-confirmation row and a later `ANSWER_EDITED` event are separately queryable;
  - the 6D-A `test_review_deltas.py` suite is green unchanged.
- [ ] Commit `feat(fill): fill-plan proposal, mapping choices, snapshot-bound confirmation and user-confirmed R7 classification`.

### Task 8: Run lifecycle, observations, quarantine events and the FILL grant

**Files:**
- Create:
  - `webapp/services/fill_runs.py`;
  - `tests/webapp/services/test_fill_runs.py`;
  - `tests/webapp/services/test_fill_grant.py`.
- Modify: `webapp/persistence/autonomy_answers.py`, adding a read-only `latest_answer_confirmation_id(conn, approved_answer_id) -> str | None` (the latest `answer_confirmations` row by `seq`).

**Interfaces:**
- `start_run(conn, *, settings, account_id, handoff_session_id, application_workspace_id, executor_instance_id, browser_session_id, execution_tab_id, now) -> dict`: claims `active_fill_runs`, inserts the run and the lease, records the `OBSERVING` event; refuses `run_active`/`context_in_use`.
- `record_observation(conn, *, run_id, phase, action_index, observation, now) -> dict`: validates and stores it.
- `revalidation_matches(conn, *, run_id) -> MatchResult`: the REVALIDATION observation vs the confirmed plan; `OBSERVATION_MISMATCH` or a §13 diff.
- `record_quarantine(conn, *, run_id, phase, ruleset_hash, now)`: `TOTAL_VERIFIED` → the `QUARANTINE_ACTIVE` event.
- `request_fill_grant(conn, *, settings, run_id, now) -> GrantResult`, in one `BEGIN IMMEDIATE`:
  - the approval is effective;
  - the plan is confirmed;
  - revalidation matches;
  - `QUARANTINE_ACTIVE` exists;
  - G4 holds: `derive_manifest` + `g4_violations == []`;
  - then the public `webapp.services.autonomy.request_grant(stage=Capability.FILL, fill_manifest=manifest, observation=…)`. On a grant it inserts `fill_run_grant_bindings`; on a refusal it stops with `GRANT_REFUSED`.
- `heartbeat(conn, *, run_id, now)`, and `reap_expired_leases(conn, *, now) -> int`: `EXECUTOR_LOST` or, if the run was FILLED, `FILLED_CONTEXT_UNVERIFIED`. Added to `autonomy_scheduler._sweeps` as a reduce-only line.
- `stop_run(conn, *, run_id, reason, detail, now)`: appends the event, writes the result (Task 9's `write_result`), releases `active_fill_runs`.

**Steps:**
- [ ] Tests:
  - concurrency keys;
  - revalidation mismatch routing (value-only → `OBSERVATION_MISMATCH`; structure → the diff);
  - the grant happy path binds every hash;
  - each grant precondition failing (no approval, stale confirmation, no `QUARANTINE_ACTIVE`, a G4 violation, pause, kill switch, `fill_per_day` exhausted) → no grant, a stop reason, **and the page's quarantine is untouched** (no server path removes it);
  - lease expiry at `RUN_LEASE_TTL` − 1 ms / + 1 ms (boundary);
  - the reaper is reduce-only.
- [ ] Commit `feat(fill): run lifecycle, observation intake, quarantine events, leases and the G4-gated FILL grant`.

### Task 9: Per-action authorization, outcomes, final validation, `fill-result.v1` and fill status

**Files:**
- Create:
  - `webapp/services/fill_actions.py`;
  - `webapp/services/fill_results.py`;
  - `tests/webapp/services/test_fill_actions.py`;
  - `tests/webapp/services/test_fill_results.py`.

**Interfaces:**
- `request_intent(conn, *, settings, run_id, action_index, precheck: dict, now) -> Envelope`, in one `BEGIN IMMEDIATE`. It verifies:
  - the grant is ISSUED and unexpired, and the binding row is unchanged;
  - `decide_and_record`-style **authority recompute** without budget reservation (pause, sentinel/kill switch, ceiling, the current policy version equal to the grant's); on a reduction it calls `revoke_grant` and stops `AUTHORITY_REDUCED`;
  - the approval is effective (`APPROVAL_NOT_EFFECTIVE`) and G4 still holds;
  - the confirmation is valid (`PLAN_CONFIRMATION_STALE`);
  - `action_index` is the next expected one (`409 out_of_order`).

  It records `WRITE_INTENT` + `ENVELOPE_ISSUED`. `Envelope(envelope_id, action_kind, rendered_value: str | None, rendered_value_hash, document: {document_version_id, sha256, …} | None, expires_at = now + VALUE_ENVELOPE_TTL)`. A retry with the same key and an unused envelope returns the same `envelope_id`, re-rendering the value from the approved source (never stored).
- `record_outcome(conn, *, run_id, action_index, envelope_id, outcome, readback_hash, post_observation, now) -> NextStep`. It accepts at most one outcome (the unique index); `ENVELOPE_EXPIRED` if it's reported after TTL; verifies `readback_hash` against the plan; stores the POST_ACTION observation and runs `classify_changes`. Deltas → opened together through `open_review_delta_in_transaction`, then stop `DELTA_OPENED`.
- `final_validate(conn, *, run_id, observation, now)`: every WRITE exact, OMIT blank, ATTACH exact, same structure → `FILLED_AWAITING_SUBMISSION`, else the stop.
- `record_unknown_outcome(conn, *, run_id, action_index, now)` → `WRITE_OUTCOME_UNKNOWN`.
- `record_detection(conn, *, run_id, kind, detail, now)`: active run → stop; after FILLED → a `POST_FILL_CHANGE_OBSERVED`/`EXECUTION_CONTEXT_CLOSED` event only.
- `write_result(conn, run_id) -> dict`: `fill-result.v1` with the non-claims exactly as spec §15.
- `fill_status(conn, *, settings, account_id, application_workspace_id, now) -> {"status", "stale": bool, "ready_to_fill": bool}`.

**Steps:**
- [ ] Tests:
  - the intent refusal matrix (every reason above), each with **zero envelope rows**;
  - envelope idempotency;
  - envelope TTL boundary (`VALUE_ENVELOPE_TTL` ± 1 ms);
  - at most one outcome under concurrent reports (two connections, a barrier);
  - OMIT and IGNORE get no value envelope;
  - ATTACH_LOCAL unlocks only the approved document;
  - `READBACK_MISMATCH`, `TARGET_CHANGED`, `FIELD_VALIDITY_FAILED`, `FIELD_VALUE_REVERTED` each stop the run;
  - post-action new fields → all deltas opened together + one stop;
  - final validation catches a question revealed by the last write (spec acceptance 13);
  - the result has the exact non-claim keys, all `false`;
  - **a sentinel cleartext value never appears in any table after a full run**;
  - fill status for every state, including `stale` after a new approval and `ready_to_fill` requiring all four conditions.
- [ ] Commit `feat(fill): per-action authority-rechecked envelopes, at-most-once outcomes, final validation and fill-result.v1`.

### Task 10: Routes

**Files:**
- Create:
  - `webapp/api/fill_extension.py` (prefix `/api/handoff/sessions/{session_id}/fill`, auth = handoff session token through the existing `resolve_session_scope`);
  - `webapp/api/fill_app.py` (the user session);
  - `tests/webapp/api/test_fill_routes.py`;
  - `tests/webapp/test_fill_structure.py`.
- Modify: `webapp/app.py` (register both routers).

**Routes** (bodies are Pydantic with `extra="forbid"`; refusals → 409 with the reason; not-owned → 404; validation → 422):
- Extension:
  - `POST runs`;
  - `POST runs/{rid}/observations`;
  - `GET runs/{rid}/plan-status`;
  - `POST runs/{rid}/quarantine`;
  - `POST runs/{rid}/grant`;
  - `POST runs/{rid}/actions/{i}/intent`;
  - `POST runs/{rid}/actions/{i}/outcome`;
  - `POST runs/{rid}/actions/{i}/unknown`;
  - `POST runs/{rid}/heartbeat`;
  - `POST runs/{rid}/detections`;
  - `POST runs/{rid}/final`.
- App:
  - `GET /api/workspaces/{id}/fill-plan/state`;
  - `POST /api/workspaces/{id}/fill-plan/mappings`;
  - `POST /api/workspaces/{id}/fill-plan/confirm`;
  - `POST /api/workspaces/{id}/review/deltas/{delta_id}/classification/confirm`;
  - `GET /api/workspaces/{id}/fill-runs`, `GET …/fill-runs/{rid}`.

**Steps:**
- [ ] Tests:
  - a real foreign-account workspace and a foreign session token → 404 with a zero-row DB diff on every route (the 6D-A `foreign` fixture pattern);
  - the state GET is read-only;
  - the confirm route refuses a stale hash;
  - the intent response is the only response body containing a cleartext value (scan every other route's response for the sentinel).
- [ ] Structural test (`test_fill_structure.py`):
  - `fill_*` modules never import `pre_click_commit`, `_pre_click_commit_core`, `_request_grant_core`, or pass `Capability.SUBMIT`;
  - no route path contains `submit` except the pre-existing handoff `confirm-submission`;
  - no route removes quarantine rules.
- [ ] Commit `feat(fill): extension and app routes with ownership, stale-view and no-submit structural checks`.

### Task 11: Extension observer (read-only observation v1)

**Files:**
- Create:
  - `extension/src/fill/observer.ts`;
  - `extension/src/fill/observation-types.ts`;
  - `extension/test/fill-observer.test.ts`;
  - `extension/test/fixtures/fill-*.html` (greenhouse, lever, conditional, duplicate-label, aria-only-label, wizard, cross-origin-frame).

**Interfaces:**
- `observe(document: Document, adapter: CertifiedAdapter, context: {canonicalUrl, origin}): Promise<ObservationV1>`. It returns the exact JSON shape validated by `product/fill_observation.py`. `current_value_hash` comes from `fillValueHash`.
- `CertifiedAdapter` = the existing adapter's `detect`/`scan` + `applicationRoot(document): Element | null` + `adapterVersion`.

**Steps:**
- [ ] vitest (jsdom):
  - the shape matches the Python schema. A shared JSON fixture is emitted by the TypeScript test into `tests/fixtures/fill/observation_from_ts.json` and validated by a Python test in the same commit;
  - value state BLANK/NONBLANK, and the hash equals the vector;
  - **question text from `aria-labelledby`, `placeholder` and a wrapping label, and `AMBIGUOUS_FIELD_IDENTITY` when there is none (Review Focus 1)**;
  - submit-class inventory;
  - wizard → a multi-step indicator;
  - cross-origin frame reported;
  - **no DOM mutation**: a MutationObserver records zero mutations during `observe`, and element `value`s are unchanged.
- [ ] Commit `feat(fill): read-only observation v1 in the isolated world`.

### Task 12: Executor primitives, settle and the allowlist

**Files:**
- Create:
  - `extension/src/fill/executor.ts`;
  - `extension/src/fill/settle.ts`;
  - `extension/test/fill-executor.test.ts`;
  - `extension/test/fill-settle.test.ts`;
  - `extension/test/fill-executor-allowlist.test.ts`.

**Interfaces:**
- `executeAction(document, action: PlanAction, envelope: Envelope | null): Promise<ActionOutcome>`. It dispatches only to `setText`, `setSelect`, `setChecked`, `setFilesLocal`, then `dispatchInput`/`dispatchChange` (`new Event(type, {bubbles: true})` on that element only), then `settle`, then readback.
- `ActionOutcome = {outcome: "WRITTEN_VERIFIED"|"NOOP_ALREADY_EQUAL"|"OMIT_VERIFIED"|"IGNORE_RECORDED"|"ATTACH_LOCAL_VERIFIED"|"READBACK_MISMATCH"|"TARGET_CHANGED"|"FIELD_VALIDITY_FAILED"|"FIELD_VALUE_REVERTED"|"ATTACH_LOCAL_MISMATCH", readbackHash?: string}`.
- `settle(root: Element, isRelevant: (m: MutationRecord) => boolean): Promise<"SETTLED"|"STRUCTURE_UNSTABLE">`, using `SETTLE_QUIET_PERIOD` and `SETTLE_CAP` from constants.

**Steps:**
- [ ] Tests:
  - each primitive on eligible and ineligible controls (readonly, disabled, detached → `TARGET_CHANGED`);
  - validity failure → `FIELD_VALIDITY_FAILED`;
  - a radio sets the whole group;
  - `setFilesLocal` verifies the byte SHA of `input.files[0]` (jsdom `File` + `arrayBuffer`) and fails on a wrong document;
  - OMIT never mutates (a MutationObserver shows zero mutations);
  - **a Unicode/CRLF textarea readback verifies (Review Focus 2)**;
  - settle: irrelevant mutations ignored; a relevant mutation at 49 ms resets the quiet period; continuous relevant mutation → `STRUCTURE_UNSTABLE` at the cap, never "settled".
- [ ] Allowlist test: parse `extension/src/fill/**/*.ts` with the TypeScript compiler API and fail on any of:
  - `.click(`, `.submit(`, `.requestSubmit(`, `.focus(`, `.blur(`;
  - `KeyboardEvent`/`MouseEvent`/`PointerEvent`/`FocusEvent`/`InputEvent` construction;
  - `dispatchEvent` outside `dispatchInput`/`dispatchChange`;
  - `innerHTML`/`outerHTML`/`insertAdjacentHTML`;
  - `setAttribute` or `removeAttribute`;
  - `location` assignment/`assign`/`replace`, `window.open`, `.action =`;
  - `Reflect.apply` or a computed member call on DOM objects.
- [ ] Commit `feat(fill): six-primitive isolated executor with readback, bounded settle and AST allowlist`.

### Task 13: Run controller, manifest, popup and detections

**Files:**
- Create:
  - `extension/src/fill/run-controller.ts`;
  - `extension/src/fill/server.ts` (typed client for the Task 10 extension routes);
  - `extension/src/fill/detections.ts` (the capture-phase `submit` listener, `pagehide`, URL/history change, opener-created tabs, the post-fill MutationObserver);
  - `extension/test/fill-run-controller.test.ts`.
- Modify:
  - `extension/src/background/index.ts` (the toolbar click → `startFillRun(tabId)`; the permission request flow);
  - `extension/scripts/build.mjs` (new `fill/observer`, `fill/executor` bundles);
  - `extension/src/popup/index.ts` + `popup.html` (the §16.3 states, badge, "Abort: close this tab", **no release control**);
  - `extension/manifest.json` (verify the Task 2 changes).

**Behaviour:** the spec §11.1 state machine:
1. **Permissions:** request `tabs` + `webNavigation` if missing (`PERMISSIONS_MISSING` if denied).
2. **Siblings:** `checkSiblingContainment`.
3. **INITIAL observe:** POST the observation, then GET `plan-status`, which stops for review, unsupported or deltas.
4. **Reset:** PRELOAD install+verify → reload → wait for the application root (`REVALIDATION_MOUNT_TIMEOUT`) → REVALIDATION observe → the server match → TOTAL install+verify.
5. **Grant.**
6. **Per action:**
   - local pre-check (ruleset verify, siblings, structure, value state, target fingerprint);
   - intent;
   - persist `{run, i, envelope_id, phase: "MAY_HAVE_WRITTEN"}` in `chrome.storage.session` **before** the DOM;
   - `executeAction`;
   - outcome.
7. **FINAL observe → final.**
8. **Post-fill observer.**

Heartbeat every `HEARTBEAT_INTERVAL`. On service-worker start, any stored `MAY_HAVE_WRITTEN` phase → `POST …/unknown`, and the run is never resumed. On tab removal → `EXECUTION_CONTEXT_CLOSED` + `removeRuleset()`.

**Steps:**
- [ ] vitest with a mocked `chrome.*` and a mocked server:
  - every transition;
  - a refused intent stops with no DOM call;
  - service-worker restart with `MAY_HAVE_WRITTEN` → `unknown`, no retry;
  - **Review Focus 4:** the root mounting at 3 s after reload → the comparison happens after mount; the root never mounting → `UNSUPPORTED_FORM(RESET_SURFACE_MISMATCH)`;
  - **Review Focus 5:** the user types into a pending target → `PREFILLED_VALUE_CONFLICT`; the user edits a completed field → `FIELD_VALUE_REVERTED`;
  - a submit event during FILLING → `SUBMIT_ATTEMPT_OBSERVED` stop; after FILLED → an event only;
  - the popup never renders a release control (DOM assertion).
- [ ] Commit `feat(fill): run controller, persisted action phases, heartbeat, detections and quarantine-aware popup`.

### Task 14: Retire the legacy writer and prove a single writer

**Files:**
- Delete:
  - `extension/src/content/content-script.ts` autofill (`runContentScript`, `approveSuggestion`);
  - `extension/src/content/attachment-runner.ts`, `attachment-dom.ts`, `attachment-source.ts`, `snapshot-source.ts`;
  - `extension/src/background/snapshot-projection.ts`;
  - their tests (`content-script.test.ts`, `attachment*.test.ts`, `snapshot-*.test.ts`).
- Modify:
  - `extension/src/content/index.ts` (probe-only; no snapshot branch);
  - `extension/src/background/index.ts` (remove `runAutofillOnTab`, `attachDocuments`, `attachInTab`);
  - `extension/scripts/build.mjs` (drop the `attachment-runner` entry);
  - `webapp/api/handoff.py` + `webapp/services/handoff.py` (remove `POST /sessions/{id}/snapshot` and `project_session_snapshot` and their tests).
- Keep: pairing, sessions, `GET /sessions/{id}/documents/{kind}` (reused by envelopes), events, and `confirm-submission`.
- Create: `extension/test/single-writer-callgraph.test.ts`.

**Steps:**
- [ ] Call-graph test: build a TypeScript program over `extension/src/**`, and collect every call to a DOM mutation API (the Task 12 forbidden list plus `.value =`, `.checked =`, `.files =`, `.selectedIndex =`). Assert every occurrence is inside `extension/src/fill/executor.ts`, and that `executeAction` is referenced only from `run-controller.ts`, after `verifyRuleset` and `request_intent` on every path (checked by a control-flow assertion in the test: the call site sits in the post-intent block).
- [ ] Update `tests/webapp/test_handoff_human_flow_regression.py` so it keeps pairing, session, documents and `confirm-submission` green, and asserts the snapshot route is gone (404).
- [ ] Run the extension tests, then `pytest tests/webapp/api tests/webapp/test_handoff_human_flow_regression.py`.
- [ ] Commit `refactor(extension): retire the legacy Phase 3 autofill writer; single employer-page writer proven by call graph`.

### Task 15: UI (fill-plan page, classification confirmation, statuses)

**Files:**
- Create:
  - `webapp/templates/fill_plan.html`;
  - `tests/webapp/api/test_fill_pages.py`.
- Modify:
  - `webapp/api/review_pages.py` (`GET /workspaces/{id}/fill-plan`, which renders `fill_plan_presentation`);
  - `webapp/templates/review_application.html` (a classification-proposal row with **Confirm meaning**, which carries `displayed_proposal_id`);
  - `webapp/templates/prepared_applications.html`;
  - `webapp/templates/autonomy_dossier.html` (fill status and run history);
  - `webapp/api/applications.py` (`prepared_applications` adds `fill_status`).

**Behaviour:**
- Page actions use `data-fill-action` / the existing `data-review-action`, never the global `data-action`.
- Confirm carries `displayed_plan_hash`; a stale confirm shows the refusal.
- Rows show the question, control, action and mapping basis, and a WRITE row shows the cleartext rendered value.
- **"Approved: open the employer page to prepare filling"** vs **"Ready to fill"** per `fill_status`.
- No Submit or release control anywhere.

**Steps:**
- [ ] Tests:
  - page render from one snapshot (inject a change inside `fill_plan_presentation` → the displayed hash stays consistent, and a confirm with it is refused as stale);
  - the confirm button is absent until every row is resolved;
  - the classification Confirm writes only the classification (no answer);
  - no `data-action=` in the new or modified templates (the existing 6D-A template regression test, extended);
  - the no-Submit structural scan is extended to the new templates;
  - the status wording per state.
- [ ] Commit `feat(fill): fill-plan confirmation page, classification confirmation and fill statuses`.

### Task 16: Final validation

**Steps:**
- [ ] **Adversarial browser acceptance** (`tests/webapp/test_fill_acceptance_browser.py`, packed extension, the recording fixture server, a certified Greenhouse fixture plus adversarial pages under `tests/webapp/fixtures/fill/adversarial/`). One test per spec §24 page:
  - Enter-submits;
  - auto-submit on select/change;
  - a hidden submit button;
  - JS submit interception;
  - fetch, beacon, image-beacon, WebSocket and WebTransport exfiltration attempts after TOTAL (the recording server gets zero requests);
  - a service-worker proxy;
  - a pre-existing socket (closed by the reset);
  - `pushState`/navigation;
  - `window.open`;
  - dynamic required and conditional fields;
  - duplicate labels;
  - SPA re-render;
  - a prefilled conflict;
  - an OMIT prefill;
  - a custom widget;
  - click-only checkboxes;
  - a wizard;
  - a cross-origin frame;
  - a sibling tab and a sibling frame;
  - a stale app tab;
  - approval invalidation mid-fill;
  - a kill switch mid-fill;
  - executor loss;
  - two concurrent runs.

  Plus the happy path: observe → plan review → confirm → reset → quarantine → fill → FINAL → `FILLED_AWAITING_SUBMISSION`, with the popup showing the quarantined state and the `fill-result.v1` non-claims.
- [ ] **Concurrency** (`tests/webapp/services/test_fill_concurrency.py`, separate connections, WAL, the file run 20×):
  - two starts for one application → one run;
  - duplicate intents → one envelope;
  - concurrent outcomes for one action → one accepted;
  - a lease reap racing a heartbeat → consistent.
- [ ] **Migrations** (`tests/webapp/persistence/test_fill_upgrade.py`): fresh = 20; an upgrade from a DB built by `git archive 8b28c57 webapp product` code with 6D-A approvals, deltas and answers → `020` applied, 6D-A rows and triggers byte-identical, FK/integrity clean, a re-run a no-op (skips if `8b28c57` is absent).
- [ ] **Full suite** in the six chunks + `npm --prefix extension run typecheck && npm --prefix extension test && npm --prefix extension run build` (production build **without** `FILL_SPIKES`/`FILL_TEST_HOOKS`; assert the spike and test-hook bundles are absent from `dist/`). Record the totals.
- [ ] **Diff boundary** vs `8b28c57`: only File Map paths, the listed deletions, and the tests. No 6D-A table, CHECK or binding change (`git diff 8b28c57 -- webapp/persistence/migrations.py` shows additions only after `_migrate_review_approval`).
- [ ] Commit `test(fill): adversarial acceptance, concurrency, migration and boundary validation`. Then stop for the user's single end-of-bundle review. No push, PR or merge without instruction.

---

## Self-review

- **Spec coverage:**

  | Spec | Tasks |
  |---|---|
  | §1–§3 decisions | Global Constraints, all tasks |
  | §4/§10.8 residual risks | 2, 5, 16 |
  | §5 invariants | I1 → 6/7/9; I2 → 2/13; I3 → 2/16; I4 → 6/13; I5 → 9/12; I6 → 4/9/13; I7 → 9; I8 → 14; I9 → 4/6/9/10; I10 → 9 |
  | §6 modules | File Map |
  | §7 | 5, 11 (§7.5 → 3) |
  | §8 | 6, 7, 15 |
  | §9 | 7, 15 |
  | §10 | 2, 13 |
  | §11 | 8, 9, 13 (§11.2.1 → 6/8/9; §11.5 → 3) |
  | §12 | 12, 13 |
  | §13 | 6, 9 |
  | §14 | 4 (CHECKs), 9 |
  | §15 | 4, 9 |
  | §16 | 13, 15 |
  | §17 | 9, 10 (read-only routes, results) |
  | §18 | 2, 13 |
  | §19 | 4 |
  | §20 | 10 |
  | §21 | 14 |
  | §22 | S1/S3 → 1, S2/S4 → 2, S5 → 3 |

- **Acceptance criteria:** 1 → 6/9/16; 2 → 2/16; 3 → 2/16; 4 → 13/15/16; 5 → 9/12; 6 → 6/9/13; 7 → 8/9/16; 8 → 9/13; 9 → 6/9/16; 10 → 7; 11 → 7/15; 12 → 2/13/16; 13 → 9; 14 → 12/14; 15 → 4/9/16; 16 → 10/14/16; 17 → 16; 18 → 4/16; 19 → 6/8/9.
- **Gate:** Task 1 stops for the user if S1 byte readback is impossible.
- **Names used consistently:** `fill_value_hash`/`fillValueHash`, `structure_fingerprint`, `observation_fingerprint`, `build_plan`, `plan_hash`, `derive_manifest`, `g4_violations`, `classify_changes`, `open_review_delta_in_transaction`, `propose_plan`, `confirm_plan`, `confirm_classification`, `start_run`, `request_fill_grant`, `request_intent`, `record_outcome`, `final_validate`, `write_result`, `fill_status`, `installRuleset`, `verifyRuleset`, `checkSiblingContainment`, `observe`, `executeAction`, `settle`.
