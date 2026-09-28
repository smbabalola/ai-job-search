# Bundle 6D-B: FILL (quarantined, plan-bound form filling) — design spec

- **Status:** consolidated design for review. It is frozen only when the user approves it.
- **Date:** 2026-09-27.
- **Base:** `master@8b28c57432f4145aa3f035c43c72faa1b2209d0c`. Bundle 6D-A (Review & Approval) is merged there, as PR #34.
- **Depends on:**
  - `docs/superpowers/specs/2026-09-26-bundle6d-a-review-approval-design.md` ("6D-A"), frozen and unchanged by this bundle;
  - `docs/superpowers/specs/2026-09-24-bundle6b-autonomy-contract-design.md` ("6B").
- **Ends at:** `FILLED_AWAITING_SUBMISSION`. Nothing in this document designs submission (6E).

## 1. Purpose and core principle

6D-B fills the employer's application form for an application the user has approved (6D-A). It then stops, leaving a verified, locally filled page that **cannot send anything**.

> **The executor operates from a closed, approved action plan. It must not discover and decide while filling. Discovering anything outside that plan is a stop condition, not an invitation to improvise.**

Three decisions shape everything else:
- **Structural firewall.** Being unable to submit is a property of the environment: a total network quarantine of the execution tab. It is not a rule the executor is merely asked to follow.
- **Single-page forms only.** Multi-step wizards are refused before any write.
- **User-approved mapping.** Only deterministic, versioned adapter rules may link a live page field to an approved answer without review. Every other link is confirmed by the user in a separate `fill-plan.v1` confirmation.

## 2. Scope

**In scope:**
- observing the employer page (read-only);
- building `fill-plan.v1` and confirming it;
- classifying newly discovered questions (the R7 paths);
- the communications reset and the network quarantine;
- the FILL grant and per-action authorization;
- the executor primitives;
- change detection routed to 6D-A deltas;
- evidence and `fill-result.v1`;
- the fill-plan and classification UI;
- the extension popup states;
- retiring the legacy Phase 3 autofill.

**Non-goals.** Each of these is refused or left out; none is half-supported:
- multi-step forms;
- generic or uncertified adapters;
- runs started unattended or by the scheduler;
- clearing or overwriting a field;
- document conversion;
- application fields inside cross-origin frames;
- continuity across a browser shutdown;
- lifting the quarantine on a live page;
- any submit action, submission authorization, or other 6E concern.

## 3. Frozen decisions ledger

| # | Decision |
|---|---|
| D1 | Architecture: a **server-authoritative plan with a thin extension executor**. The extension observes and executes; the server maps, plans, authorizes and records. |
| D2 | **6D-A is unchanged.** 6D-B adds a separate immutable `fill-plan.v1` and an append-only fill-plan confirmation bound to `(plan_hash, approval_id, approval_binding_hash)`. |
| D3 | Every application field is `WRITE`, `ATTACH_LOCAL` or an explicit `OMIT`. Only a proven non-application control may be `IGNORE_NON_APPLICATION`. Any other application field is a delta and a stop. |
| D4 | The network firewall is a **total quarantine** built from `declarativeNetRequest` session rules. It is preceded by a **communications reset** (PRELOAD quarantine, then reload). |
| D5 | **Single-page forms only.** Wizard indicators mean `UNSUPPORTED_FORM` before any write. |
| D6 | Documents are `ATTACH_LOCAL`: the exact approved bytes are placed in the approved input and verified by a byte SHA-256 read back from `input.files`. No claim is made that the employer received them. |
| D7 | **No release in 6D-B.** The quarantine stays active at `FILLED_AWAITING_SUBMISSION`. Destroying the execution context (closing the tab) is the only exit. |
| D8 | The run is **user-initiated** (toolbar click on the employer tab) and reuses the Phase 3 pairing and handoff session. |
| D9 | **Sibling containment by refusal.** The optional permissions `tabs` and `webNavigation` are requested together when safe FILL is enabled. An employer-origin sibling tab or frame stops the run (§10.5). |
| D10 | The executor is an AST/API allowlist of exactly six mutation primitives (§12.1). |
| D11 | Fingerprints are split into `structure_fingerprint` (no mutable values) and `observation_fingerprint` (structure plus value-state hashes). |
| D12 | **Per-action authorization re-checks current authority.** Tickets are single-action and at-most-once, and any uncertainty is `WRITE_OUTCOME_UNKNOWN`, never retried. |
| D13 | `FINAL_VALIDATING` precedes `FILLED_AWAITING_SUBMISSION`. A post-fill MutationObserver records staleness and never authorizes anything. |
| D14 | Run identity is `(executor_instance_id, browser_session_id, execution_tab_id, fill_run_id)`, with a lease. A lost lease is `EXECUTOR_LOST`; nothing ever resumes. |
| D15 | Safe FILL requires an **adapter version explicitly certified** with `network_model = NO_UNCONTAINED_PERSISTENT_CHANNELS` and per-control-kind support. Generic and unknown adapters are never certified. |
| D16 | R7 classification has two paths: a versioned deterministic adapter rule, or a proposal plus **user-confirmed classification**. Classification confirmation is persisted separately from answer approval. |
| D17 | OMIT is a **pure non-write** that requires a blank field. A prefilled conflicting value stops the run; nothing is ever overwritten. |
| D18 | No cleartext answer in any plan, confirmation, ledger or result. Cleartext exists only in the single-action execution envelope and on the user-facing confirmation page. |
| D19 | The legacy Phase 3 discover-and-decide autofill and document attachment are **retired**. Once 6D-B ships, exactly one extension code path mutates an employer page. |
| D20 | Only the eight frozen 6D-A delta kinds are used: `NEW_QUESTION`, `CHANGED_QUESTION`, `DECLARATION`, `TRANSFORM_FAILURE`, `OMIT_FIELD_REQUIRED`, `NEW_UPLOAD`, `DOCUMENT_CONVERSION`, `TARGET_CHANGE`. Everything else is a stop reason. |

## 4. Threat model

**Adversaries and hazards in scope:**
- **Employer page JavaScript:**
  - auto-submit on `change`/`input`;
  - `form.submit()`/`requestSubmit()`;
  - `fetch`/XHR/`sendBeacon`/image-beacon exfiltration;
  - WebSocket/WebTransport;
  - navigation, including `pushState`;
  - hidden submit buttons, capture-phase interception and prototype tampering;
  - dynamic, conditional and re-rendered fields;
  - fields with duplicate labels.
- **Other contexts on the employer origin:** sibling tabs, same-partition frames in other tabs, SharedWorkers, service workers, and `localStorage`/`BroadcastChannel` coordination.
- **Pre-existing channels:** sockets or state opened before the executor started.
- **Stale and concurrent state:**
  - stale tabs or views of the app, and concurrent runs across devices;
  - approval invalidation, pause, kill switch or policy change mid-run;
  - service-worker restarts, browser crashes and shutdown.
- **Regression risk:** a future code change that reintroduces an unprotected writer.

**Out of scope, stated honestly:**
- a compromised browser, extension, OS or server;
- a malicious local user;
- Chrome defects in `declarativeNetRequest` enforcement;
- employer-side processing after 6E.

**Residual risks documented, not hidden:**
- `webNavigation` and `tabs` can't see SharedWorkers, service workers or not-yet-activated prerendered pages. These are handled only by the quarantine rules and adapter certification (§10.5, D15).
- A packed extension can't observe individual blocked requests. Prevention is structural; in-page detection is best-effort (§10.6).

## 5. Invariants

- **I1. No unapproved write.** Every employer-page mutation corresponds to exactly one action of a confirmed `fill-plan.v1`, whose confirmation is bound to the currently effective 6D-A approval.
- **I2. No write outside quarantine.** No mutation happens unless the exact TOTAL quarantine ruleset has been read back and verified immediately before that action, and the communications reset preceded it.
- **I3. Nothing leaves the tab.** While a run's context exists, every network request from the execution tab is blocked, as are background requests initiated by the employer origin (Q2).
- **I4. Closed plan.** The executor never maps, classifies or chooses. Anything the plan doesn't cover stops the run before the next write.
- **I5. Exact values.** Each written value's readback hash equals the plan's `rendered_value_hash`, and each attached file's byte SHA-256 equals the approved document SHA.
- **I6. At most once.** An action is applied at most once. The server accepts at most one terminal outcome per action, and uncertainty is terminal (`WRITE_OUTCOME_UNKNOWN`).
- **I7. Current authority.** Every action intent re-checks:
  - the effective approval;
  - the plan confirmation;
  - current FILL authority (pause, kill switch/sentinel, capability ceiling, current policy);
  - grant validity.
- **I8. One writer.** The production extension has exactly one code path that can mutate an employer page, and it is reachable only through quarantine verification and per-action authorization.
- **I9. No cleartext at rest.** No answer value is persisted by 6D-B.
- **I10. Weak result.** `fill-result.v1` never claims employer receipt, persistence, acceptance, submission, submission authority or fitness for 6E.

## 6. Architecture

**Server** (new modules; existing modules change only additively):
- `product/fill_observation.py`: the pure observation schema, validation, canonicalization and both fingerprints.
- `product/fill_plan.py`: the pure plan builder and plan hash (§8), plus the manifest-v1 derivation.
- `product/fill_certification.py`: the closed catalogue of certified adapter versions, per-control-kind support, deterministic mapping rules, non-application rules and classification rules (§7.4).
- `webapp/services/fill_runs.py`: the run lifecycle, grant, intents, outcomes, leases and results.
- `webapp/services/fill_plans.py`: plan proposal, mapping choices and confirmation.
- `webapp/services/fill_classification.py`: R7 proposals and confirmation.
- `webapp/persistence/fill.py`, with migration `020_fill` (§19).
- Routes (§20).

**Extension:**
- `observer`: read-only, ISOLATED world, derived from the existing probe.
- `quarantine`: DNR ruleset install, read-back and verification; sibling-context checks.
- `executor`: ISOLATED world, the six primitives only.
- `run-controller`: state machine, `chrome.storage.session` persistence, heartbeat, server calls.
- `popup` states.

**Retired:** legacy `runContentScript` autofill, `approveSuggestion`, `attachment-runner`/`attachDocuments`, and the extension's use of the candidate snapshot projection (§21).

## 7. Observation v1

### 7.1 Content

The observer builds a canonical document from the page. It is read-only, never contains a cleartext value, and never records a value.

**Context:**
- canonical URL and origin;
- `adapter_id` and `adapter_version`;
- `tenant_key` and ATS job id, when known;
- the frame tree (origin per frame);
- the application root, which the adapter identifies deterministically; if it can't, the result is `UNSUPPORTED_FORM`;
- multi-step indicators.

**Elements:** every form-associated element, plus every element inside the application root that has an interactive role. Each carries:
- `page_field_key`: deterministic and unique within the observation;
- `control_kind`;
- `field_fingerprint`: the canonical hash of:
  - tag, type, name, id and form owner;
  - normalized label and question text;
  - ARIA role and attributes;
  - required, disabled, readonly and visible;
  - the full ordered option list (value + text);
  - `accept`/`multiple`;
  - `maxlength`/`pattern`/`min`/`max`;
  - frame identity;
- `classification`: `APPLICATION`, or `NON_APPLICATION` with a proof (§7.3);
- `value_state`: one of `BLANK`, or `NONBLANK(current_value_hash)`, where the hash is computed locally with the pinned normalization of §7.5.

**Submit-class controls:** count and fingerprints. They are never actionable.

### 7.2 Fingerprints

- **`structure_fingerprint`**: the canonical hash of:
  - the context;
  - every element's `page_field_key`, `field_fingerprint`, `control_kind` and `classification`;
  - the submit-class inventory;
  - the multi-step indicators;

  with **no value states**.
- **`observation_fingerprint`**: the canonical hash of `structure_fingerprint` plus every APPLICATION element's `value_state`.

**Identity uniqueness.** If two APPLICATION elements have indistinguishable identity (same fingerprint, or an ambiguous `page_field_key`), the observation is marked `AMBIGUOUS_FIELD_IDENTITY`:
- `PLAN_NEEDS_REVIEW` when the user can safely disambiguate by choosing among fingerprinted candidates;
- otherwise `UNSUPPORTED_FORM`.

It is never resolved by DOM order.

### 7.3 Non-application proofs (closed)

- `OUTSIDE_APPLICATION_ROOT`: the adapter identified the root deterministically, and the element is outside it and not linked back to it through `form=`.
- `ADAPTER_NON_APPLICATION_RULE(rule_id@version)`: a certified adapter rule, with regression fixtures.
- `SUBMIT_CLASS_CONTROL`: inventoried separately, never executable.

Nothing else qualifies. In particular, `type=hidden`, `role=search` and cookie-banner heuristics are **not** proofs. An unknown element that belongs to the application fails closed.

### 7.4 Adapter certification (`fill-certification`)

A certified adapter version declares all of the following, with regression fixtures for each rule:
- `network_model = NO_UNCONTAINED_PERSISTENT_CHANNELS`;
- the supported control kinds (e.g. text, email, textarea, single select; radio or checkbox only when its forms accept state changes without clicks);
- deterministic mapping rules (`rule_id@version` → an answer key or document kind);
- non-application rules;
- deterministic classification rules (`rule_id@version` → a registered semantic subject).

Generic and unknown adapters are never certified. The initial candidates are Greenhouse and Lever, each certified individually, and only after spikes S2–S4 pass on their fixtures.

### 7.5 Canonical hashing across languages

The observer (TypeScript) computes `current_value_hash`; the server (Python) computes `rendered_value_hash`. One normalization and hash definition is pinned for both:
- UTF-8, NFC;
- line endings normalized to `\n`;
- no trimming beyond what the control itself applies;
- a domain-separated SHA-256.

It is shared through **cross-language test vectors** that both suites must pass.

## 8. `fill-plan.v1`

### 8.1 Schema

```text
fill-plan.v1
  schema_version            "fill-plan.v1"
  account_id
  application_workspace_id
  approval_id               6D-A approval
  approval_binding_hash     6D-A binding hash
  canonical_target_url
  target_origin
  adapter_id, adapter_version
  tenant_key, ats_job_id    when available
  structure_fingerprint
  observation_fingerprint
  actions[]                 one per observed element, ordered by the certified adapter's action order
    page_field_key
    field_fingerprint
    action_kind             WRITE | ATTACH_LOCAL | OMIT | IGNORE_NON_APPLICATION
    answer_key              WRITE / OMIT
    document_kind           ATTACH_LOCAL
    source_ref              approved answer id / evidence claim id / document_version_id
    value_hash              the approved value hash (6D-A binding)
    transform_id            one of the field's permitted transforms (6D-A binding)
    rendered_value_hash     the exact string or option that will be written (WRITE)
    document                {document_version_id, sha256, byte_length, filename, media_type} (ATTACH_LOCAL)
    mapping_basis           ADAPTER_RULE(rule_id@version) | USER_CONFIRMED(choice_id) | NON_APPLICATION_PROOF(kind)
    preconditions           the expected value state before the action (BLANK, or BLANK|EQUAL for WRITE)
  plan_hash                 canonical hash of everything above
```

The plan carries **hashes and references, never values**.

### 8.2 Builder rules (pure)

The builder takes the observation, the effective 6D-A approval, the certification catalogue and the user's mapping choices.

**WRITE** requires all of the following, otherwise it's a stop or a delta (§8.3):
- a 6D-A binding field with disposition `ANSWER` for the same `answer_key`;
- a `transform_id` among its permitted transforms;
- a mapping basis;
- a supported control kind for this adapter version;
- for select and radio, the rendered value must equal exactly one observed option, otherwise `TRANSFORM_FAILURE`;
- `maxlength`/`pattern`/`min`/`max` must be satisfiable, otherwise `TRANSFORM_FAILURE`.

**ATTACH_LOCAL** requires:
- the approved document of that kind (6D-A pack), bound by version and SHA;
- a certified file-input mapping;
- an `accept` list that clearly admits the document's media type:
  - a clear mismatch (e.g. only `.pdf` accepted and the document is DOCX) is a `DOCUMENT_CONVERSION` delta;
  - an ambiguous or unparseable `accept` stops the run.

  No conversion ever happens in 6D-B.

**OMIT** requires a 6D-A binding field with disposition `OMIT`. Its precondition is `BLANK`, and nothing is written.

**IGNORE_NON_APPLICATION** requires a §7.3 proof.

**Custom widgets:**
- approved OMIT + an unsupported widget is allowed if the field is blank;
- approved ANSWER + an unsupported widget is `UNSUPPORTED_FORM`;
- an undecided or new widget goes to delta or review.

The builder never downgrades an ANSWER to OMIT.

### 8.3 Routing when an application element can't be bound

| Cause | Route |
|---|---|
| A question not in the approval | 6D-A `NEW_QUESTION` (classified per §9) |
| A declaration or attestation | 6D-A `DECLARATION` |
| An extra upload requirement | 6D-A `NEW_UPLOAD` |
| An OMIT field that the page requires | 6D-A `OMIT_FIELD_REQUIRED` |
| Changed wording or options on a known field | 6D-A `CHANGED_QUESTION` |
| No option or format fits the approved value | 6D-A `TRANSFORM_FAILURE` |
| A clear document-format mismatch | 6D-A `DOCUMENT_CONVERSION` |
| A different target, tenant or job | 6D-A `TARGET_CHANGE` |
| Content approved, but no deterministic mapping | `PLAN_NEEDS_REVIEW` (a user mapping choice) |
| Ambiguous identity that the user can resolve | `PLAN_NEEDS_REVIEW` |
| A wizard, uncertified adapter, cross-origin application frame, ambiguity the user can't resolve, or a required answered custom widget | `UNSUPPORTED_FORM` |

All deltas are opened through the existing 6D-A intake (§11 of 6D-A), with `source = "FILL_RUN:<fill_run_id>"` and `observed = {field_key: page_field_key, field_fingerprint, question, required, options_hash}`.

6D-A deltas are resolved first: no plan is built until the approval is effective again.

### 8.4 User mapping choices

A row that needs mapping offers **only deterministically compatible** approved answers: the same subject, a compatible control kind, and passing the 6D-A reach/context applicability rules. It never offers all approved answers.

- If the field's *meaning* is uncertain, it goes through classification (§9) first. Mapping and classification are distinct acts.
- "This is a new question" opens a 6D-A `NEW_QUESTION` instead of mapping the field.
- Each choice is an append-only mapping-choice record, and the rebuilt plan carries `USER_CONFIRMED(choice_id)`.

### 8.5 Confirmation

The `/workspaces/{id}/fill-plan` page builds its presentation from **one database snapshot**:
- the approval, the latest observation and the mapping choices;
- the resulting `plan_hash`, which becomes `displayed_plan_hash`.

The page may show the cleartext rendered value for WRITE rows, derived and shown for the user's confirmation, and it must correspond to that row's `rendered_value_hash`.

**Confirm fill plan (`displayed_plan_hash`)** runs in `BEGIN IMMEDIATE`:
1. Recompute the current approval, the plan and the observation identity.
2. On an exact hash match, append a `fill_plan_confirmations` row `(plan_hash, approval_id, approval_binding_hash, confirmed_by, at)`.
3. Otherwise refuse with `stale_plan` and write nothing.

A confirmation is **reusable** only when that exact tuple is rebuilt. A new 6D-A approval makes every earlier confirmation unusable, without mutating history.

## 9. Classifying newly discovered questions (R7)

```text
New live question
  ├── certified deterministic classification rule (rule_id@version → registered subject)
  │       → 6D-A delta opened classified (subject set)
  └── no deterministic rule
          → 6D-A delta opened unclassified
            + a stored proposal of a registered subject (server heuristic or model; never authoritative)
            → user confirms classification ("this question means <subject>")
            → atomic R7 successor transition (6D-A §11 R7):
                the predecessor is resolved with {reason: "classified", successor_delta_id}
                the classified successor stays open until answered and re-approved
            → retries return the same successor and write nothing
```

- A proposal never classifies anything by itself. Until the user confirms, the delta stays unclassified and, per 6D-A R7, can't be answered.
- Confirming the classification is recorded in a 6D-B append-only table (`delta_classification_confirmations`: delta id, proposal id, subject, actor, time). It is written **in the same transaction** as the successor insert. It is a different decision from answer approval, which remains the 6D-A answer flow and approval.
- Proposals are stored in `delta_classification_proposals`, as observational suggestions.

## 10. Communications reset and network quarantine

### 10.1 Permissions

- `declarativeNetRequest` becomes a required manifest permission. It can't be optional, and blocking rules need no host permissions.
- `tabs` and `webNavigation` are **optional permissions**, requested together the first time the user enables safe FILL.
- `host_permissions` are unchanged.

### 10.2 Resource types (closed constant)

```text
QUARANTINE_RESOURCE_TYPES_V1 = main_frame, sub_frame, stylesheet, script, image, font, object,
  xmlhttprequest, ping, csp_report, media, websocket, webtransport, webbundle, other
```

### 10.3 Rulesets (versioned; verified exactly by read-back)

- **PRELOAD v1:**
  - Q1-PRE (`tabIds=[execution_tab_id]`) blocks the request methods `post`, `put`, `patch`, `delete`, `connect`, `options` for all types, and blocks the types `websocket`, `webtransport`, `ping` for any method;
  - Q2 (below).
- **TOTAL v1:**
  - **Q1 TARGET_TAB_TOTAL**: `tabIds=[execution_tab_id]`, all of `QUARANTINE_RESOURCE_TYPES_V1`, every method, action `block`;
  - **Q2 EMPLOYER_BACKGROUND**: `tabIds=[TAB_ID_NONE]` with `initiatorDomains=[employer origin host]`, all types, every method, action `block`;
  - an employer-wide rule across all tabs (Q3) is **not** part of v1. Sibling containment is proven by refusal (§10.5), and a domain-wide rule would also freeze unrelated tabs that share an ATS host.

Session rules are the only kind that support `tabIds`. The canonical ruleset (ids, priorities, conditions, actions) has a `ruleset_hash`. The executor verifies the exact read-back set against it, not merely that some rules exist.

### 10.4 Communications reset (REVALIDATING)

1. Verify the permissions and sibling containment (§10.5).
2. Install PRELOAD and verify it by read-back.
3. **Reload the same target URL.** This destroys sockets, workers bound to the page, and script state created before the firewall.
4. Take a full re-observation. Its `observation_fingerprint` must equal the confirmed plan's, with no writes on any difference:
   - a different `structure_fingerprint` goes through the §13 diff routing;
   - the same structure with different value states is `OBSERVATION_MISMATCH`.
5. Upgrade to TOTAL, verify it by read-back, and record `QUARANTINE_ACTIVE` (with the `ruleset_hash`) with the server.

If the page can't rebuild the same approved surface under PRELOAD, the result is `UNSUPPORTED_FORM` before any write.

### 10.5 Sibling-context containment

**Invariant:** *exactly one known document context exists in the employer's relevant storage partition: the execution document.* No other top-level tab or frame on the employer origin may exist.

- Before PRELOAD, the extension enumerates tabs (`tabs`) and every tab's frames (`webNavigation.getAllFrames`), and requires exactly the execution document. Otherwise the run ends as `FILL_STOPPED(SIBLING_EMPLOYER_CONTEXT_OPEN)`.
- During REVALIDATING, FILLING and FINAL_VALIDATING, the extension watches tab and navigation events, and re-checks before every action.
- Background and network-capable contexts (service workers, SharedWorkers) are contained by Q2 and by adapter certification (D15). The APIs can't see them, and 6D-B claims no more than that.

### 10.6 Defense in depth (not the firewall)

- Executor discipline (§12.1).
- A capture-phase `submit` listener cancels and records any submit event as `SUBMIT_ATTEMPT_OBSERVED`.
- `pagehide`, an opener-created tab, or a URL/history change records `NAVIGATION_ATTEMPT_OBSERVED`.

Each of these is a stop reason while a run is active and evidence afterwards.

### 10.7 Lifetime

- There is no release control in 6D-B, and the quarantine persists at `FILLED_AWAITING_SUBMISSION` and after any stop.
- Closing the tab removes the rules and records `EXECUTION_CONTEXT_CLOSED` when observed.
- Browser shutdown clears the session rules. **6D-B makes no continuity guarantee across a shutdown.** The run is terminated, the old page is never resumed as an authorized context, and a new run needs the full reset.

## 11. Grant, run lifecycle and per-action authorization

### 11.1 Run states

```text
OBSERVING ──unsupported──► UNSUPPORTED_FORM
    │
    ▼
PLAN_PROPOSED ──mapping or delta needed──► PLAN_NEEDS_REVIEW   (the run ends; the user reviews; the next click is a new run)
    │ confirmed plan matches
    ▼
REVALIDATING  (sibling check → PRELOAD → reload → exact observation → TOTAL)
    ▼
QUARANTINE_ACTIVE
    ▼ FILL grant (§11.2)
FILLING ──any stop──► FILL_STOPPED(reason, detail)
    ▼ last action verified
FINAL_VALIDATING ──fail──► FILL_STOPPED(...)
    ▼
FILLED_AWAITING_SUBMISSION   (quarantine remains; post-fill observer active)
```

A refused grant leaves the page quarantined, and the run ends as `FILL_STOPPED`.

### 11.2 FILL grant (6D-A G4 plus the 6B gate)

A service `request_fill_grant(fill_run_id)` runs in one `BEGIN IMMEDIATE` transaction. It verifies:
- the 6D-A approval is effective;
- a valid confirmation exists for the run's `(plan_hash, approval_id, approval_binding_hash)`;
- the run's revalidated observation equals the plan's;
- `QUARANTINE_ACTIVE` is recorded with the TOTAL `ruleset_hash`.

It then calls the **existing public** `request_grant(stage=FILL, fill_manifest=…)`. The manifest v1 is derived 1:1 from the plan's WRITE and ATTACH_LOCAL actions:
- `source.kind` is EVIDENCE, APPROVED_ANSWER or PACK_DOCUMENT;
- `value_hash` follows the pinned per-kind rule;
- `transform_id` is the plan's.

The 6B binding and TTL are unchanged. The same transaction records the run's grant binding `(grant_id, approval_id, approval_binding_hash, plan_hash, structure_fingerprint, observation_fingerprint, ruleset_hash)`. The 6B gate is applied fully (pause, kill switch/sentinel, capability ceiling, policy, budgets, `fill_per_day`).

### 11.3 Per-action protocol (strict plan order)

**a. Local pre-check (extension):**
- the TOTAL ruleset read-back equals `ruleset_hash`;
- sibling containment still holds;
- a fresh **structural** re-observation equals the plan's `structure_fingerprint`;
- value states match the expected evolution: completed actions equal their rendered hashes, pending WRITEs are `BLANK` or `EQUAL`, OMIT fields are `BLANK`;
- the target's `field_fingerprint` matches the plan.

**b. Intent (server, `BEGIN IMMEDIATE`), keyed `(fill_run_id, action_index)`.** It checks:
- the grant is ISSUED and unexpired, and the run's grant binding is unchanged;
- current FILL authority: it recomputes the 6B authorization reducers (pause, kill switch/sentinel, capability ceiling, current policy version) without consuming any budget, and a reduction refuses the intent and revokes the grant;
- the 6D-A approval is still effective (review state recomputed);
- the plan confirmation is still valid;
- the action index is the next expected one.

It then:
- records `WRITE_INTENT`, which is authoritative;
- for WRITE, returns a **single-action execution envelope** with the cleartext rendered value and its hash;
- for ATTACH_LOCAL, unlocks only the exact document bytes (version and SHA), fetched through the existing exact-document endpoint;
- sets a 30-second envelope lifetime.

A retried intent returns the same envelope while it is still unused.

**c. Execute (extension):**
1. Before touching the DOM, persist `{run, action_index, envelope_id, phase: MAY_HAVE_WRITTEN}` in `chrome.storage.session`.
2. Re-check the target fingerprint in the same task.
3. Apply exactly one primitive group (§12.2).
4. Settle (§12.3).
5. Read back.

**d. Outcome (server).** It accepts **at most one terminal outcome** per action:
- `WRITTEN_VERIFIED`;
- `NOOP_ALREADY_EQUAL`;
- `OMIT_VERIFIED`;
- `IGNORE_RECORDED`;
- `ATTACH_LOCAL_VERIFIED`;
- or a failure: `READBACK_MISMATCH`, `TARGET_CHANGED`, `FIELD_VALIDITY_FAILED`, `FIELD_VALUE_REVERTED`.

It also records the post-action structural observation. A diff opens deltas or stops the run (§13) **before the next action**.

OMIT and IGNORE go through the intent and outcome for the audit, but get no value envelope and no mutation.

**Safety property:** one authorization identity per action; the executor applies it at most once; the server accepts at most one terminal outcome. If execution is lost after `MAY_HAVE_WRITTEN` and before an outcome, the result is **`FILL_STOPPED(WRITE_OUTCOME_UNKNOWN)`**, never an automatic retry. A fresh run observes what actually exists.

### 11.4 Identity, lease, concurrency

- **Run identity:** `executor_instance_id` (from the pairing), `browser_session_id` (random per browser start, kept in `chrome.storage.session`), `execution_tab_id` (ephemeral) and `fill_run_id`.
- **Lease:** a heartbeat every 10 seconds, with a 45-second lease. An expired lease is `EXECUTOR_LOST`, and it never authorizes resumption.
- **Concurrency:** at most one non-terminal run per application, and at most one run per `(executor_instance_id, browser_session_id, execution_tab_id)`. Both are enforced server-side, which also covers two devices.
- **Interruptions:**
  - pause, kill switch, a policy reduction, approval invalidation, confirmation staleness or grant expiry → the next intent is refused and the run stops;
  - the existing `revoke_issued_grants` covers the kill switch.

### 11.5 Timing constants (named, versioned, boundary-tested)

Every timing value is a named constant in one versioned module per side. The Python and TypeScript definitions carry the same version identifier, and the values are never scattered as literals:

| Constant | v1 value |
|---|---|
| `FILL_TIMING_VERSION` | `fill-timing.v1` |
| `HEARTBEAT_INTERVAL` | 10 s |
| `RUN_LEASE_TTL` | 45 s |
| `VALUE_ENVELOPE_TTL` | 30 s |
| `SETTLE_QUIET_PERIOD` | 50 ms |
| `SETTLE_CAP` | 1 s |

Tests exercise each constant at its boundary:
- a lease just before and just after expiry;
- an envelope used just before and just after its TTL;
- a mutation at the quiet-period edge and one at the cap.

The run records `FILL_TIMING_VERSION` in its identity, so its evidence says which timing rules applied.

## 12. Executor

### 12.1 Allowlist

The executor bundle runs in the **ISOLATED** world, using its own realm's prototype setters. Its only mutation primitives are:

`SET_TEXT`, `SET_SELECT`, `SET_CHECKED`, `SET_FILES_LOCAL`, `DISPATCH_INPUT`, `DISPATCH_CHANGE`.

A **structural AST/import/API allowlist test** fails the build on any other DOM mutation or dispatch in the executor or run-controller code, including:
- `click`, `submit`/`requestSubmit`;
- keyboard, pointer and focus events;
- `location`, `window.open`, `form.action`;
- `innerHTML`/attribute writes;
- a generic "call a DOM method" helper.

### 12.2 Primitive semantics

| Primitive group | Eligible controls | Verification |
|---|---|---|
| `SET_TEXT` → `DISPATCH_INPUT` → `DISPATCH_CHANGE` | `input` of type text/email/tel/url/number/date (date as ISO), and `textarea`; connected, enabled, not readonly | value hash equals `rendered_value_hash`; `validity` shows no type/pattern/length/range failure |
| `SET_SELECT` → events | single `select`; the planned option exists with its fingerprinted value and text | `value` and `selectedOptions[0]` match the plan |
| `SET_CHECKED` → events | a checkbox, or the planned radio of a group (`name` + form owner), only where the adapter certifies click-free state | `checked` state of the whole group equals the plan |
| `SET_FILES_LOCAL` → `DISPATCH_CHANGE` | a certified file input | byte SHA-256 of `input.files[0]` equals the approved SHA; name, size and type match |

Events bubble and are dispatched on the written element only. There is never a click, key, focus or blur.

### 12.3 Settle

The executor waits one macrotask, then for a **50 ms quiet period with no relevant application-surface mutation**, capped at **1 s**. Relevant means:
- application fields or root;
- required state or options;
- submit-class controls;
- multi-step indicators;
- file inputs;
- frames or the target state.

Unrelated animation, clocks or analytics DOM is ignored. Relevant mutation that continues through the cap is `STRUCTURE_UNSTABLE`, and the executor never proceeds on timer expiry.

Settling only decides when an action can be verified. It doesn't prove permanence; the next pre-check and the post-fill observer still apply.

### 12.4 FINAL_VALIDATING and post-fill observation

After the last action verifies, the page settles and then gets a full read-only observation. It must show:
- the exact structure;
- every WRITE value exact;
- every OMIT field blank;
- every attachment byte-exact locally.

The run then reaches `FILLED_AWAITING_SUBMISSION`, with the `fill-result.v1` written.

A MutationObserver then stays active for as long as the quarantined page exists. A relevant change records `POST_FILL_CHANGE_OBSERVED`, with the observation, and marks the filled surface **stale for 6E**. It never authorizes a write.

## 13. Change classification (re-observation diff against the plan)

| Observed change | Result |
|---|---|
| A new APPLICATION field whose text matches the certified declaration patterns | `DELTA_OPENED` / `DECLARATION` |
| A new APPLICATION file input | `DELTA_OPENED` / `NEW_UPLOAD` |
| Any other new APPLICATION field | `DELTA_OPENED` / `NEW_QUESTION` (classified per §9) |
| An OMIT field became required | `DELTA_OPENED` / `OMIT_FIELD_REQUIRED` |
| Label, question or options changed on a planned field | `DELTA_OPENED` / `CHANGED_QUESTION` |
| The page rejects the rendered value (validity) | `DELTA_OPENED` / `TRANSFORM_FAILURE` (`value_hash`) |
| URL, origin or tenant changed | `DELTA_OPENED` / `TARGET_CHANGE` |
| A field removed, submit-class controls changed, a wizard indicator appeared | `STRUCTURE_CHANGED` |
| A completed field's value changed | `FIELD_VALUE_REVERTED` |
| Relevant mutation past the settle cap | `STRUCTURE_UNSTABLE` |

Every delta from one diff is opened together, and the run stops before the next action.

## 14. Closed stop reasons (`FILL_STOPPED.reason`)

`DELTA_OPENED` (with `detail.delta_kind`), `UNSUPPORTED_FORM` (with `detail.cause`), `STRUCTURE_CHANGED`, `STRUCTURE_UNSTABLE`, `OBSERVATION_MISMATCH`, `OMIT_FIELD_NOT_BLANK`, `PREFILLED_VALUE_CONFLICT`, `TARGET_CHANGED`, `READBACK_MISMATCH`, `FIELD_VALIDITY_FAILED`, `FIELD_VALUE_REVERTED`, `ATTACH_LOCAL_MISMATCH`, `SIBLING_EMPLOYER_CONTEXT_OPEN`, `PERMISSIONS_MISSING`, `QUARANTINE_RULESET_CHANGED`, `QUARANTINE_LOST`, `SUBMIT_ATTEMPT_OBSERVED`, `NAVIGATION_ATTEMPT_OBSERVED`, `GRANT_REFUSED`, `GRANT_EXPIRED`, `AUTHORITY_REDUCED` (with `detail`: `PAUSED` | `KILL_SWITCH` | `CEILING` | `POLICY`), `APPROVAL_NOT_EFFECTIVE`, `PLAN_CONFIRMATION_STALE`, `ENVELOPE_EXPIRED`, `WRITE_OUTCOME_UNKNOWN`, `EXECUTOR_LOST`, `EXECUTION_CONTEXT_CLOSED`.

`detail.cause` for `UNSUPPORTED_FORM` is a closed set: `MULTI_STEP`, `UNCERTIFIED_ADAPTER`, `NO_APPLICATION_ROOT`, `CROSS_ORIGIN_APPLICATION_FRAME`, `AMBIGUOUS_FIELD_IDENTITY`, `UNSUPPORTED_REQUIRED_WIDGET`, `RESET_SURFACE_MISMATCH`.

## 15. Evidence and `fill-result.v1`

**Authoritative** (created by the server only):
- plans and confirmations;
- classification confirmations and mapping choices;
- grants and grant bindings;
- `WRITE_INTENT` and envelope issue;
- accepted terminal outcomes;
- run transitions and stop reasons;
- delta creation;
- results.

**Observational** (supplied by the executor, labelled as such, trust root = the pairing credential):
- observations;
- readbacks;
- ruleset read-backs;
- MutationObserver reports;
- submit/navigation detections;
- sibling checks.

Observational evidence never grants anything and never makes an approval effective.

**No cleartext at rest.** Only references, hashes, indices, fingerprints and outcomes are stored.

**`fill-result.v1`** is an immutable, hashed artifact written at every terminal state. It contains:
- the run identity;
- `approval_id` and `approval_binding_hash`, `plan_hash`, `grant_id`;
- the terminal state, the stop reason and its detail;
- per-action outcomes, with readback hashes and local document SHA verifications;
- the final structure and observation fingerprints;
- the `ruleset_hash` and the quarantine status;
- the delta ids opened;
- timestamps;
- and explicit **non-claims**:

```text
employer_received_answers: false          employer_received_documents: false
employer_persisted_application: false     employer_accepted_application: false
submitted: false                          submission_authorized: false
fit_for_submission_without_6E_revalidation: false
```

`FILLED_AWAITING_SUBMISSION` means only that the approved local form state was produced and verified under quarantine.

## 16. Product surface

### 16.1 The user's acts

Each act is rendered from one snapshot, carries its displayed hash and refuses a stale view.

1. **Confirm classification**, on the 6D-A review page's delta section (§9).
2. **Approve the application**: 6D-A, unchanged.
3. **Confirm the fill plan**, on `/workspaces/{id}/fill-plan` (§8.5). Rows show the question as the page shows it, the control kind, the action and the mapping basis:
   - WRITE rows show the approved answer and the exact rendered value;
   - ATTACH_LOCAL rows show the document name and SHA;
   - OMIT rows show "left blank (approved)";
   - IGNORE rows show the proof.

### 16.2 Application statuses

- **APPROVED_FOR_FILL** (6D-A) means the approval is effective. The app shows "Approved: open the employer page to prepare filling".
- **READY_TO_FILL** means all of:
  - an effective approval;
  - a current supported observation;
  - a confirmed matching fill plan;
  - the required permissions and environment available.

**Fill status** is derived from the latest run and sits next to the unchanged 6D-A review state:
- `NOT_STARTED`;
- `PLAN_NEEDS_REVIEW`;
- `UNSUPPORTED_FORM`;
- `FILLING`;
- `FILL_STOPPED`;
- `FILLED_AWAITING_SUBMISSION` (while the lease is live);
- `FILLED_CONTEXT_UNVERIFIED`: previously verified, but continuity can no longer be proven, so it's unusable by 6E.

An observed tab close is recorded as `EXECUTION_CONTEXT_CLOSED`. That is the stop reason for a non-terminal run, and only an event after `FILLED_AWAITING_SUBMISSION`. The status is **stale** when the current approval or plan differs from the run's. It appears on the Prepared list and in the dossier, which also carries the run history.

### 16.3 Extension popup

- Enable safe FILL (the permission request);
- Observing…;
- Needs your review (link);
- Unsupported (cause);
- Quarantined · preparing;
- Quarantined · filling *i*/*N*;
- **"Filled. This page is quarantined and cannot send anything. Submission is a later, separate step."**;
- Stopped (reason, next step, evidence link).

A persistent badge marks the quarantined tab. The only exit is **"Abort: close this tab"**; there's no release control.

### 16.4 Recovery

Every stop leaves the page quarantined. The user deals with the cause, opens the job page **fresh**, and clicks the toolbar icon, which starts a new run with a full reset. After `WRITE_OUTCOME_UNKNOWN`, `EXECUTOR_LOST` or `QUARANTINE_LOST`, the UI tells the user to close the tab and start fresh. Nothing resumes.

## 17. Interface exposed to 6E (not designed here)

Durable:
- `fill_run_id` and the executor/browser-session identity;
- `execution_tab_id` (ephemeral);
- `fill-result.v1` and its hash;
- the plan, approval and grant identities;
- the `ruleset_hash`;
- lease and heartbeat evidence;
- post-fill observations and mutations.

Available live: a **read-only re-observation** of the still-quarantined context, recorded as a POST_FILL observation.

`fill-result.v1` alone never proves the context is live. 6E must require the extension to demonstrate that **the same live quarantined context still exists**, and must obtain a fresh observation. 6D-B guarantees nothing about lifting the quarantine, submission authorization, or fitness for submission.

## 18. Extension manifest changes

- Add `"declarativeNetRequest"` to `permissions`.
- Add `"optional_permissions": ["tabs", "webNavigation"]`.
- `host_permissions` are unchanged.
- There are no new static content scripts. The observer and executor are injected on demand, in the ISOLATED world, through `scripting` on the user-activated tab.

## 19. Data (migration `020_fill`, one atomic migration, no `executescript`)

Append-only, with update/delete triggers:
- `fill_observations`;
- `fill_plans`;
- `fill_plan_mapping_choices`;
- `fill_plan_confirmations`;
- `delta_classification_proposals`;
- `delta_classification_confirmations`;
- `fill_runs` (identity only);
- `fill_run_events` (transitions, stop reasons);
- `fill_run_grant_bindings`;
- `fill_action_events` (PRECHECK, WRITE_INTENT, ENVELOPE_ISSUED, OUTCOME);
- `fill_quarantine_events`;
- `fill_detection_events`;
- `fill_results`.

Mutable operational tables, which are not audit records:
- `fill_run_leases` (heartbeat);
- `active_fill_runs` (primary key `application_workspace_id`, plus a unique execution-context key; inserted at start and deleted at the terminal state in the same transaction as the terminal event).

CHECK constraints enumerate every closed vocabulary (states, stop reasons, outcomes, action kinds, mapping bases, non-application proofs). No 6D-A table or CHECK is altered.

## 20. API surface

The app (user session) exposes:
- `GET /workspaces/{id}/fill-plan` (HTML, one snapshot);
- `GET /api/workspaces/{id}/fill-plan/state` (read-only);
- `POST …/fill-plan/mappings` (append a mapping choice);
- `POST …/fill-plan/confirm` `{displayed_plan_hash}`;
- `POST /api/workspaces/{id}/review/deltas/{delta_id}/classification/confirm` `{displayed_proposal_id, subject}`;
- `GET /api/workspaces/{id}/fill-runs` and `…/fill-runs/{run_id}` (read-only evidence, result).

The extension (handoff session token) exposes, under `/api/handoff/sessions/{sid}/fill/`:
- `runs` (start: identity tuple);
- `runs/{rid}/observations` (phase + observation);
- `runs/{rid}/quarantine` (phase + `ruleset_hash`);
- `runs/{rid}/grant`;
- `runs/{rid}/actions/{i}/intent`;
- `runs/{rid}/actions/{i}/outcome`;
- `runs/{rid}/heartbeat`;
- `runs/{rid}/detections`;
- `runs/{rid}/final`.

Ownership failures are 404, domain refusals are 409 with the exact reason, and validation failures are 422. Bodies forbid extra keys.

No route submits, lifts the quarantine, or accepts any SUBMIT stage.

## 21. Legacy retirement (D19)

**Removed:**
- the legacy `runContentScript` autofill and `approveSuggestion` writers;
- `attachment-runner`/`attachDocuments` (MAIN-world employer-page attachment);
- the extension's use of the candidate snapshot projection, whose endpoint is removed along with its tests.

**Kept:**
- pairing and the handoff session;
- the read-only probe (reused by the observer);
- exact document retrieval (reused by ATTACH_LOCAL envelopes);
- the user's **"I submitted it myself"** confirmation;
- downloads, the dossier, and ordinary manual application outside the executor.

A **structural call-graph test** proves that no employer-page mutation primitive is reachable in the production extension except through the 6D-B executor, after quarantine verification and per-action authorization.

## 22. Technical spikes (the first plan tasks; outcomes gate the design)

- **S1. `SET_FILES_LOCAL` in the ISOLATED world, with byte readback.** Place a `File` through `DataTransfer` from the isolated world, confirm the page sees it (including React forms on the fixtures), and hash the bytes read back from `input.files[0]`. **If exact byte verification is impossible, stop and return to the user before weakening D6 or I5.**
- **S2. Quarantine enforcement.** In Playwright with the packed extension, prove that PRELOAD and TOTAL block every listed type and method from the tab, and that Q2 blocks the employer's service-worker fetches:
  - form POST;
  - `fetch`/XHR;
  - `sendBeacon`;
  - an image beacon;
  - WebSocket and WebTransport handshakes;
  - `pushState` plus navigation;
  - `window.open`.

  Also prove that the reload destroys a socket opened beforehand, and that the extension's own server calls are unaffected.
- **S3. Controlled inputs.** Prove that isolated-world setters plus `input`/`change` register in React- and Vue-controlled fields on the certified fixtures, and establish which control kinds need a click (not certifiable).
- **S4. Sibling enumeration.** `tabs` + `webNavigation.getAllFrames` detect sibling tabs and same-origin frames in other tabs. Document the prerender and worker blind spots.
- **S5. Cross-language hash vectors** (§7.5).

## 23. Acceptance criteria

1. **Closed plan.** Every mutation matches a confirmed plan action. A field outside the plan never gets written, and it produces the §8.3/§13 routing.
2. **Firewall.** Under the adversarial pages (§24), after PRELOAD (REVALIDATING step 2) no state-changing request, beacon or socket leaves the execution tab. After TOTAL, no request of any kind leaves it, and none is initiated by the employer background context (Q2). The S2 assertions hold in CI.
3. **Reset.** A socket or worker created before the run can't transmit after the reset, and a page that can't rebuild its surface under PRELOAD ends `UNSUPPORTED_FORM` with zero writes.
4. **No release.** No UI, route or message lifts the quarantine. `FILLED_AWAITING_SUBMISSION` pages stay quarantined until the tab closes.
5. **Exact values.** Every WRITE's readback hash equals `rendered_value_hash`, and every ATTACH_LOCAL byte SHA equals the approved SHA. A mismatch stops the run.
6. **OMIT and prefill.** An OMIT field is never mutated, and a non-blank OMIT field stops the run. A conflicting prefilled WRITE target stops the run, and an equal one is a verified no-op.
7. **Authority.** Pause, kill switch, a policy reduction, approval invalidation, confirmation staleness and grant expiry each refuse the next intent and stop the run, with zero further writes.
8. **At most once.** Losing the executor after `MAY_HAVE_WRITTEN` gives `WRITE_OUTCOME_UNKNOWN`, and no retry occurs in that run.
9. **Change detection.** The delta and stop mapping of §13 holds for every row. All deltas from one diff are opened together through 6D-A intake, and 6D-A review then shows them in delta-only or full mode, as 6D-A defines.
10. **Classification.** Deterministic rules open classified deltas. A proposal never classifies anything, and user confirmation performs the atomic R7 transition exactly once (retries write nothing). Classification confirmation and answer approval are separately provable.
11. **Confirmation binding.** A stale `displayed_plan_hash` is refused with no writes, and a new 6D-A approval invalidates earlier confirmations.
12. **Siblings.** An employer-origin sibling tab or same-origin frame in another tab stops the run before any write, or before the next write if it appears mid-run.
13. **Final validation.** A question revealed by the last write stops the run as `DELTA_OPENED` instead of reaching FILLED. A post-fill change records `POST_FILL_CHANGE_OBSERVED` and marks the result stale.
14. **Single writer.** The structural call-graph test and the executor allowlist test pass, and the legacy writers are gone.
15. **Evidence.** Every run has a complete authoritative and observational trail and a `fill-result.v1` with the non-claims. No cleartext answer appears in any table or result.
16. **Boundary.** No route, primitive or UI element submits. The 6D-A G2 refusal and structural tests still pass. The Phase 3 "I submitted it myself" confirmation still works.
17. **Concurrency.** Two concurrent run starts for one application produce one run. Duplicate intents return one envelope. Concurrent outcomes for one action accept exactly one. Lease expiry gives `EXECUTOR_LOST`. Each race runs 20× clean.
18. **Migration.** `020` is fresh and atomic, and an upgrade from a `master@8b28c57` database is clean, with 6D-A tables byte-identical.

## 24. Testing strategy

- **Pure** (Python): observation canonicalization and both fingerprints, the plan builder (every §8.2/§8.3 rule), manifest derivation, the certification catalogue, change classification, and closed vocabularies. Property tests cover fingerprint sensitivity: structure vs value state, and option order.
- **Extension** (vitest): the observer, executor primitives and readbacks, settle, the run-controller state machine, `chrome.storage.session` phases, ruleset canonicalization, the allowlist AST test, the call-graph test, and the cross-language vectors.
- **Services:** grant (G4 + 6B), intents, envelopes, outcomes, leases, confirmation, classification, mapping choices, result writing, and write-contract database diffs.
- **Concurrency:** separate connections, WAL, 20× runs (§23.17).
- **Browser acceptance** (Playwright with the packed extension, against local adversarial fixture pages):
  - Enter-submits and auto-submit on select/change;
  - hidden submit buttons and JS submit interception;
  - `fetch`/`sendBeacon`/image-beacon/WebSocket/WebTransport exfiltration;
  - a service-worker proxy and a pre-existing socket;
  - `pushState`/navigation and `window.open`;
  - dynamic required and conditional fields, duplicate labels, SPA re-renders;
  - prefilled conflicts and OMIT prefills;
  - custom widgets and click-only checkboxes;
  - a wizard indicator and a cross-origin frame;
  - sibling tab and sibling frame;
  - stale app tabs, approval invalidation mid-fill, kill switch mid-fill;
  - executor loss, and two concurrent runs.
- **Migrations:** fresh and upgrade (§23.18).
- **Full suite:** in the six local chunks, then PR CI.
