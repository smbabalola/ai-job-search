# Bundle 6E-A: Human-Authorized SUBMIT — design spec

- **Base:** `master@efc6577` (6D-B merged).
- **Starts from:** a 6D-B run in `FILLED_AWAITING_SUBMISSION` with a live lease, in a tab still under the verified TOTAL quarantine.
- **Ends at:** exactly one of `CONFIRMED_SUCCESS`, `SUBMISSION_AMBIGUOUS` or proven `SUBMISSION_FAILED` for one human-authorized attempt (plus `EXPIRED_UNCLICKED` when nothing was dispatched).
- **Not in scope:** autonomous submission. That is 6E-B, and every autonomous entry point stays refused.

This spec is decision-final. Implementation makes no design decisions. Where a spike can fail, the fallback is decided here (§21), or the spike is a gate that stops the bundle and goes back to the user.

## 1. Purpose and core principle

The user reviews the filled application in the web app. They approve **that exact version**, bound to a single snapshot hash, with one "Submit application" action. The extension then proves that it is acting on the same live, quarantined, unchanged tab, and clicks the employer's certified submit control.

The network stays closed except for a narrow, verified, adapter-certified submit egress, and only for as long as the submission needs. Anything unexpected fails closed.

Principle, carried forward from 6D: **the executor acts only on a closed, approved authority. Anything discovered outside it is a stop condition.**

## 2. Scope

**In scope:**
- the Submit Review snapshot (`submission-review.v1`) and its page;
- the human SUBMIT authorization;
- the gate's human authority mode;
- the one-time SUBMIT grant, pre-click commit, intent claim and attempt (reused 6B mechanics);
- the adapter submit certification (`submit-certification.v1`) and the SUBMIT egress ruleset;
- the submit click;
- result determination and `submission-result.v1`;
- CAPTCHA/challenge handoff;
- cancellation before dispatch;
- ambiguity resolution by the user;
- statuses, popup and dossier;
- migration `021_human_submit`;
- structural tests, fixtures, a browser acceptance suite, and the live-certification runbook.

**Non-goals:**
- 6E-B autonomous submit. The public autonomous entry points keep raising `SubmissionNotAvailable`.
- Bulk "Submit selected (N)".
- Multi-page or wizard forms (6D-B already refuses them).
- Any adapter other than Greenhouse, including Lever, which is FILL-certified but not submit-certified.
- Live Greenhouse submission. It stays disabled until live certification evidence exists (§9.5).
- Any CAPTCHA solving, bypass or automation.
- Any "learning" mode that widens egress to discover traffic.
- Employer-side processing after submission.
- Emails and notifications.

## 3. Frozen decisions ledger

| # | Decision |
|---|---|
| E1 | Final authority lives in the web app: one "Submit application" action on the Submit Review page, carrying the displayed `review_hash`. There's no popup confirmation. |
| E2 | Authority source `HUMAN_SUBMIT`. The 6B gate gains an `authority` input. Under `HUMAN_SUBMIT` the standing policy, the account and workspace autonomy ceilings, and the autonomous counters and budgets aren't applied. Every other check is (§8.2). |
| E3 | 6B mechanics are reused: the SUBMIT grant (`SUBMIT_GRANT_TTL`), `_pre_click_commit_core`, the intent claim, attempts, `CLICK_DISPATCHED` durable before the physical click, and the result states. They're reached only through new human entry points that require a human authorization row. |
| E4 | TOTAL is never "lifted". It's replaced by a SUBMIT egress ruleset: the unchanged TOTAL block rules plus higher-priority, tab-scoped `allow` rules taken from the adapter's certified egress manifest, and nothing else. It's verified by exact read-back **after** `CLICK_DISPATCHED` is durable and **before** the physical click. Afterwards TOTAL is restored and verified. |
| E5 | Submission is permitted only for an adapter version whose `submit-certification.v1` is `LIVE_CERTIFIED`, or `FIXTURE_CERTIFIED` on a loopback origin with `JOBSEARCH_SUBMIT_FIXTURE_ORIGINS=1`. `greenhouse@2` is `FIXTURE_CERTIFIED`. |
| E6 | Deployment switch `JOBSEARCH_HUMAN_SUBMIT_ENABLED`, default off. When off, the gate caps a `HUMAN_SUBMIT` evaluation at FILL (`human_submit_disabled`). |
| E7 | One human authorization per fill run, ever. One attempt per grant. A retry needs a fresh tab and a new 6D-B fill run. |
| E8 | The server determines results, from executor-reported evidence and a pure rule (§11). The extension never declares success. |
| E9 | `SUBMISSION_FAILED` is recorded only with proof of non-submission (§11.2). Otherwise the result is `SUBMISSION_AMBIGUOUS`, which the user resolves. |
| E10 | CAPTCHA/challenge: detect and hand off to the human, never bypass. A challenge before the click refuses the attempt. A challenge after the click enters CHALLENGE_HANDOFF with continuous content watching and a mandatory re-observation when it clears. Any application-content change restores TOTAL immediately (§12). |
| E11 | Submit observations live in a new table (`submit_observations`). 6D-B tables and contracts are unchanged. |
| E12 | The extension learns of review re-observation requests and issued authorizations through the existing run heartbeat response, which gains additive fields. |
| E13 | The physical click is `SUBMIT_CLICK`: `element.click()` in the ISOLATED world, in a new `submit-executor.ts`. The 6D-B executor still never clicks. |
| E14 | Cancel exists only before `CLICK_DISPATCHED`. It records `EXPIRED_UNCLICKED` (source USER) and releases the intent. |
| E15 | Kill switch, sentinel and pause are re-checked at authorize, at pre-click, and at the dispatch acknowledgement. After `CLICK_DISPATCHED` nothing can recall the click. |
| E16 | Duplicate prevention: the live-intent unique index per job identity (shared with the Phase 3 human paths), one authorization per run, one attempt per grant, the single-use grant, and extension-persisted attempt phases that never click twice. |
| E17 | Migration `021_human_submit` is atomic and adds new append-only tables. It rebuilds `submission_intents` only to add the source `HUMAN_AUTHORIZED`, preserving every row, index and FK. No 6D-A or 6D-B table changes. |
| E18 | Every volatile value (ids, timestamps) is excluded from `review_hash`. A fresh observation of an unchanged page reproduces the hash exactly. |
| E19 | On `CONFIRMED_SUCCESS`, the existing workflow "applied" status change is recorded and linked to the attempt's intent. There is never a second live intent. |
| E20 | Manifest: add the `declarativeNetRequestFeedback` permission, subject to spike S-E2's decided fallback (§21). `host_permissions` are unchanged. **Resolved: fallback applied; the permission is not requested (see spike-results note).** |

## 4. Threat model

**Adversaries and faults:**
- Employer page scripts that try to submit early, exfiltrate data during the egress window, alter fields after review, or intercept or redirect the click.
- Other tabs and frames on the employer origin.
- A stale web-app view.
- Double clicks, retries, extension restarts and service-worker loss.
- A user acting in another tab.
- Server restarts between steps.
- Kill switch engagement mid-flow.

**Out of scope, as in 6D-B §4/§10.8:**
- WebRTC, other extensions and browser-internal traffic;
- a compromised browser;
- DNR enforcement defects;
- whatever the employer does after receiving the application.

**The egress window is a deliberate, bounded exception.** While allow rules exist, the page can send whatever it likes to the **allowed endpoints only**: the employer's own submit and confirmation paths on the bound origin, plus declared challenge-provider endpoints. That is exactly the recipient set the user authorized. Nothing else leaves: every other destination, type and method stays blocked by the TOTAL rules underneath.

## 5. Invariants

- **J1. Human authority only.** A SUBMIT grant is issued only by `request_human_submit_grant`, inside the transaction that inserts its `human_submit_authorizations` row. Every autonomous SUBMIT entry point still raises.
- **J2. Exact version.** The attempt is authorized only if the PRE_SUBMIT re-observation reproduces the authorized `review_hash` exactly.
- **J3. Durable before physical.** The submit click happens only after the server has acknowledged `CLICK_DISPATCHED` and the SUBMIT egress ruleset has been verified. The executor persists `DISPATCHING` locally before clicking and never clicks from a restored state.
- **J4. Fail closed.** Every failure before the physical click leaves TOTAL in place, or restores and verifies it. No path installs allow rules other than the certified manifest's, and no path ever removes the block rules.
- **J5. Bounded egress.** Allow rules exist only from their verified install until the result, the `SUBMIT_RESULT_WINDOW`, or the challenge window ends. Then TOTAL is restored and verified. A detected content change restores TOTAL immediately.
- **J6. Server-determined result.** Results come from the pure `determine_submit_result`, and no success state is recorded without the certified success signal.
- **J7. Proven failure only.** `SUBMISSION_FAILED` requires `proven_not_submitted` evidence meeting §11.2.
- **J8. No duplicates.** At most one live (CLAIMED/CONFIRMED) intent per account and job identity, across every source.
- **J9. No cleartext answers** in any table, log, event or result. Cleartext appears only on the Submit Review HTML page, as on the 6D-B fill-plan page.
- **J10. 6D-A/6D-B unchanged.** No change to their tables, CHECKs, bindings, event vocabularies or executor allowlist.
- **J11. Append-only evidence.** New evidence tables have UPDATE/DELETE-raising triggers.

## 6. Architecture and flow

```
Filled employer tab (6D-B FILLED_AWAITING_SUBMISSION, TOTAL, lease live)
  │  user opens /workspaces/{id}/submit
  ▼
Server: REVIEW re-observation requested ──heartbeat──▶ extension posts REVIEW observation
  ▼
Submit Review page renders submission-review.v1 (review_hash) + human-gate readiness
  │  "Submit application" (POST authorize {review_hash})
  ▼
Server txn: rebuild snapshot == review_hash? gate(HUMAN_SUBMIT) ALLOW? →
  insert human_submit_authorizations + issue SUBMIT grant (120 s)
  ▼  heartbeat tells the extension: authorization {grant_id, review_hash}
Extension: local proofs → PRE_SUBMIT observation → POST pre-click
  ▼
Server txn (human_pre_click_commit): snapshot(PRE_SUBMIT) == review_hash, gate ALLOW,
  grant consumable → consume, claim intent HUMAN_AUTHORIZED, attempt AUTHORIZED
  ▼  popup "Submission authorized — submitting…" [Cancel until dispatch]
Extension: persist DISPATCHING → POST dispatch (server: re-check halt/pause/TTL → CLICK_DISPATCHED)
  → install SUBMIT egress (TOTAL + allow rules) → verify exact
  → re-find the bound submit control → SUBMIT_CLICK
  ▼
Result watch (≤ SUBMIT_RESULT_WINDOW; challenge → CHALLENGE_HANDOFF ≤ CHALLENGE_HANDOFF_WINDOW)
  ▼
Restore TOTAL + verify → read matched egress rules → POST result evidence
  ▼
Server: determine_submit_result → attempt event + submission-result.v1
  (+ workflow "applied" on success; intent CONFIRMED / RELEASED)
```

**Units (new):**

| Unit | Responsibility |
|---|---|
| `product/submit_certification.py` | pure `submit-certification.v1` catalogue (egress manifest, signals, status) |
| `product/submit_review.py` | pure snapshot builder and `review_hash` |
| `product/submit_result.py` | pure `determine_submit_result` |
| `product/submit_constants.py` | `submit-timing.v1` constants (mirrored in `extension/src/submit/constants.ts`) |
| `product/autonomy_gate.py` | additive `HUMAN_SUBMIT` authority mode |
| `webapp/persistence/submit.py` | 021 tables' persistence |
| `webapp/services/submit_review.py` | re-observation requests, snapshot assembly, readiness |
| `webapp/services/human_submit.py` | authorize, grant, pre-click, dispatch, cancel, result, resolve |
| `webapp/api/submit_extension.py`, `webapp/api/submit_app.py` | routes |
| `webapp/templates/submit_review.html` | the Submit Review page |
| `extension/src/submit/egress.ts` | SUBMIT egress ruleset build, install and verify, and TOTAL restore |
| `extension/src/submit/signals.ts` | adapter success, failure and challenge detection |
| `extension/src/submit/submit-executor.ts` | `SUBMIT_CLICK` only |
| `extension/src/submit/submit-controller.ts` | the protocol state machine |
| popup additions | the submit views |

## 7. `submission-review.v1`

### 7.1 Content

A canonical JSON object, built by the pure `build_review_snapshot(inputs)`:

```
schema: "submission-review", schema_version: "v1"
application: { account_id, application_workspace_id, identity_key, employer_key }
target: { canonical_url, origin, adapter_id, adapter_version, tenant_key, ats_job_id }
certification: { certification_id, status }            # e.g. "greenhouse@2/submit@1", FIXTURE_CERTIFIED
fill: { fill_run_id, plan_hash, plan_confirmation_id, fill_result_hash,
        final_observation_fingerprint, ruleset_hash_total }
context: { executor_instance_id, browser_session_id, execution_tab_id }
observation: { observation_fingerprint, structure_fingerprint }   # the fresh REVIEW/PRE_SUBMIT one
submit_control: { control_fingerprint }
approval: { approval_id, binding_hash }                # 6D-A effective approval
answers: [ [approved_answer_id, confirmation_id, rendered_value_hash] ... ] (sorted)
documents: [ [document_kind, filename, sha256] ... ] (sorted)
policy: { engine_version, human_submit_contract: "human-submit.v1", subject_policy_hash,
          control_epoch, submit_timing_version }
```

`review_hash = canonical_hash("submission-review", "v1", snapshot)`. There are no ids of observation rows and no timestamps (E18).

### 7.2 Preconditions for a snapshot (else no snapshot; the page shows the reason)

- The run's state is `FILLED_AWAITING_SUBMISSION` with a live lease.
- No `POST_FILL_CHANGE_OBSERVED` detection was recorded since FILLED.
- There is no existing human authorization for the run.
- A REVIEW observation exists, newer than the latest re-observation request, and no older than `SUBMIT_REVIEW_OBSERVATION_MAX_AGE`.
- Its `observation_fingerprint` equals the run's FINAL_VALIDATING observation fingerprint. Equal means the page is unchanged since the fill verified it.
- It has exactly one entry in `submit_controls`, and the adapter's certified submit-control predicate (§9.2) matches that one control.
- The 6D-A approval is effective and equals the fill run's grant binding's approval. The plan hash and confirmation equal the run's.

## 8. Human authority

### 8.1 Authorization record

`human_submit_authorizations`: id, account_id, application_workspace_id, fill_run_id **UNIQUE**, review_hash, review_json, grant_id **UNIQUE**, actor, created_at. It is append-only.

### 8.2 Gate: `authority` input

`AuthorizationContext` gains `authority: AuthorityKind = AuthorityKind.STANDING_POLICY` as its final field.
- `canonical_hash` of the context omits `authority` when it equals the default, so every existing context fingerprint stays byte-identical. A pinned-vector test enforces this.
- Under `HUMAN_SUBMIT`:
  - `requested_stage` must be `SUBMIT`. Otherwise the result is `invalid_input` (`human_authority_stage`).
  - The initial cap is `ctx.deployment_ceiling` alone. The context builder passes `SUBMIT` when `human_submit_enabled`, else `FILL`, and notes `human_submit_disabled`. `account_max` and `workspace_ceiling` are noted, not applied.
  - `_apply_standing_policy` is skipped (noted `standing_policy_not_applied`).
  - Counters and budgets are skipped.
  - Identity, apply target, employer key, requirements (SUBMIT readiness), pack confirmability, stops (kill switch, sentinel, drift, auto-reject, duplicate intent) and questions/hard stops all apply unchanged.
- `adapter_submit_capable` for `HUMAN_SUBMIT` contexts comes from `submission_permitted(certification, origin, settings)` (§9.5), not from `JOBSEARCH_AUTONOMY_SUBMIT_ADAPTERS`.

### 8.3 Entry points (`webapp/services/human_submit.py`)

- **`authorize(conn, settings, account_id, ws, review_hash, actor, now)`** runs one BEGIN IMMEDIATE:
  1. Rebuild the snapshot from the latest REVIEW observation. `≠ review_hash` → `stale_review`.
  2. Evaluate the gate under `HUMAN_SUBMIT`. Anything other than ALLOW → the deny reason.
  3. Insert the authorization.
  4. `request_human_submit_grant` issues a SUBMIT grant (TTL `SUBMIT_GRANT_TTL`). Its binding is `build_binding(...)` plus `{"authority": "HUMAN_SUBMIT", "human_authorization_id", "review_hash"}`.
- **`human_pre_click_commit(conn, settings, run_id, grant_id, observation, verification, now)`**:
  1. Store the PRE_SUBMIT observation.
  2. Then one BEGIN IMMEDIATE, which checks, in order:
     - the context key equals the run's;
     - `verification` equals the snapshot's context, `observation_fingerprint`, `submit_control_fingerprint` and TOTAL `ruleset_hash`;
     - the snapshot from PRE_SUBMIT equals the authorized `review_hash`;
     - `_pre_click_commit_core(authority=HUMAN_SUBMIT)`: gate re-evaluation and drift, grant consumable, grant consumed, intent claimed with source `HUMAN_AUTHORIZED`, attempt `AUTHORIZED`. **No limit reservations** under `HUMAN_SUBMIT`.
  3. Any refusal revokes the grant, with reason `pre_click:<reason>`.
- **`human_record_click_dispatched(attempt_id, now)`** re-checks the kill switch, sentinel and pause, then delegates to the existing `record_click_dispatched`. A refusal appends `EXPIRED_UNCLICKED` (source SERVER, evidence `{halted: reason}`) and releases the intent.
- **`cancel_before_dispatch(attempt_id | authorization_id, actor, now)`**: allowed only in `AUTHORIZED`, or before the grant is consumed. The grant is revoked (`user_cancelled`), and if an attempt exists, `EXPIRED_UNCLICKED` (source USER) is appended and the intent released.
- **`report_result(attempt_id, evidence, now)`** → §11.
- **`resolve_ambiguous`**: the existing 6B function (source USER), reached from the app.

The public `request_grant(SUBMIT)` and `pre_click_commit` keep raising `SubmissionNotAvailable`.

## 9. Submit certification and SUBMIT egress

### 9.1 `submit-certification.v1` (pure catalogue, mirrored in TypeScript)

Per adapter version:
- `certification_id`
- `status ∈ {FIXTURE_CERTIFIED, LIVE_CERTIFIED}`
- `live_evidence: str | None` (a path under `docs/superpowers/notes/`, required for LIVE_CERTIFIED)
- `submit_control` (§9.2)
- `egress` (§9.3)
- `success` (§11.1)
- `failure` (§11.1)
- `challenge` (§12.1)
- `failure_signal_proves_not_submitted: bool`

`greenhouse@2/submit@1`:
- status `FIXTURE_CERTIFIED`, `live_evidence = None`;
- `failure_signal_proves_not_submitted = True` (the fixture model: a server validation error re-renders the form with `#error_explanation` and stores nothing).

Lever has no entry.

### 9.2 Submit control

The certified predicate: within the application root, exactly one control with tag `button` or `input`, `type = submit`, and `form_owner` equal to the application form. For Greenhouse that's `#application_form [type=submit]`. The review binds its `control_fingerprint` (the 6D-B `fill-submit-control` v1 hash). At click time the executor finds **exactly one** element whose fingerprint equals the bound one. Otherwise it refuses (§10).

### 9.3 Egress manifest (templates resolved against the bound target)

Each entry has an id, `request_methods`, `resource_types`, and a `regex_filter` built from the bound `origin`, `tenant_key` and `ats_job_id`, anchored `^…$`, with those values escaped.

For `greenhouse@2/submit@1`:

| id | methods | types | URL |
|---|---|---|---|
| `E1_SUBMIT` | post | main_frame, xmlhttprequest | `{origin}/{tenant}/jobs/{job}` |
| `E2_CONFIRM` | get | main_frame, xmlhttprequest | `{origin}/{tenant}/jobs/{job}/confirmation(\?.*)?` |
| `E3_RENDER` | get | stylesheet, script, image, font | `{origin}/.*` |
| `C1_RECAPTCHA` | get, post | script, sub_frame, xmlhttprequest, image | `https://www\.(google\|gstatic\|recaptcha)\.(com\|net)/recaptcha/.*` |

- **E3** is the only same-origin wildcard. It's GET-only, and limited to rendering types on the employer origin, which is the authorized recipient.
- **C1** is declared for the real board, and exercised only by live certification. The fixtures simulate challenges in-page (§12).

### 9.4 Ruleset

`buildSubmitEgressRules(tabId, employerHost, manifest, target)`:
- the TOTAL rules unchanged (Q1 9111 and Q2 9121, priority 1000, block);
- plus one `allow` rule per manifest entry: ids 9201–9209, priority 2000, `tabIds: [tabId]`, with the entry's methods, types and `regexFilter`.
- Q2 stays block-only, so the service worker's `fetch` remains blocked.

`installSubmitEgress` swaps atomically, adding the allow rules with `updateSessionRules`. `verifySubmitEgress(expectedHash)` requires the fill-rule set to equal the expected canonical hash exactly. The 6D-B `verifyRuleset` still refuses any non-block rule, so it rejects this set by construction.

`restoreTotal` removes 9201–9209 and verifies the TOTAL hash recorded at FILL.

Every install, verify and restore is reported to the server as a `submit_events` row.

### 9.5 Permission to submit

`submission_permitted(cert, origin, settings)` is true exactly when:
- `cert.status == LIVE_CERTIFIED` and `cert.live_evidence` is set; or
- `cert.status == FIXTURE_CERTIFIED`, `settings.submit_fixture_origins_enabled`, and `origin` is `http://127.0.0.1:<port>` or `http://localhost:<port>`.

`JOBSEARCH_SUBMIT_FIXTURE_ORIGINS=1` is honoured only for those loopback origins. On any other origin the Submit Review shows "Live submission for Greenhouse isn't certified yet" and the server refuses (`adapter_not_live_certified`).

`docs/superpowers/notes/2026-09-29-greenhouse-live-submit-certification.md` is the runbook for what evidence flips the status. Flipping it is a reviewed code change, never a runtime toggle.

## 10. Submit execution protocol (extension `SubmitController`)

A submit starts when the heartbeat response carries `authorization {authorization_id, grant_id, review_hash, expires_at}` for this run. The controller then runs these steps in order:

1. **Local proofs.** Each failure → `PRE_CLICK_REFUSED` with its reason, no click, TOTAL untouched:
   - the controller's run id and tab id equal the run's;
   - the lease is live;
   - `verifyRuleset(totalHash)` passes;
   - sibling containment holds;
   - `pollDetections()` finds none;
   - the page's adapter and origin equal the snapshot's.
2. **Observe.** Take a PRE_SUBMIT observation. If a challenge signal (§12.1) is visible → `CHALLENGE_BEFORE_SUBMIT`: refuse, and the server revokes the grant.
3. **Pre-click.** Call `POST /runs/{id}/submit/pre-click`. A refusal stops the flow, and the reason reaches the popup and the app.
4. **Popup.** It shows "Submission authorized — submitting…" with **Cancel** until step 5 persists.
5. **Dispatch.** Persist `{attemptId, phase: "DISPATCHING"}` to `chrome.storage.session`, then call `POST …/dispatch`. If it doesn't return true → `EXPIRED_UNCLICKED` (the server already recorded it). Persist `DISPATCHED_ACK`.
6. **Egress.** Call `installSubmitEgress`, then `verifySubmitEgress`.
   - On failure: `restoreTotal` and verify it, then report the result `{click_performed: false, cause: EGRESS_NOT_VERIFIED}`.
7. **Click.** Re-find the bound submit control (exactly one), persist `CLICKED`, call `SUBMIT_CLICK(el)`, and record `CLICK_PERFORMED`.
   - Not found → restore TOTAL and report `{click_performed: false, cause: SUBMIT_CONTROL_MISSING}`.
8. **Watch.** Every `SUBMIT_RESULT_POLL` for up to `SUBMIT_RESULT_WINDOW`, evaluate the signals (§11.1, §12.1) on the current document.
   - After a main-frame navigation to an E2 URL (`chrome.tabs.onUpdated`, status complete), re-inject the page bundle first.
   - A content change on the application root (the 6D-B post-fill watcher) → `CONTENT_CHANGED_DURING_ATTEMPT`, and restore TOTAL immediately.
9. **Finish.** `restoreTotal` and verify it. Read the matched rules for the tab (§11.3), take a POST_SUBMIT observation if the form is still present, and call `POST …/result` with the evidence. Clear the persisted phase.

**Restart rule (J3).** After a restart, a persisted phase of `DISPATCHING`, `DISPATCHED_ACK` or `CLICKED`:
- never clicks;
- restores TOTAL;
- reports `{click_performed: "UNKNOWN" | false, cause: EXECUTOR_RESTARTED}`: `false` only for `DISPATCHING` or `DISPATCHED_ACK`, where no click was made. `determine_submit_result` then decides.

## 11. Result determination

### 11.1 Signals (certified per adapter)

For `greenhouse@2/submit@1`:
- **success:** a document on the bound origin whose URL matches E2 **and** which contains `#application_confirmation`, **or** the in-page swap: the application root is replaced by `#application_confirmation` on the same URL;
- **failure:** the application form is still present, **and** `#error_explanation` is visible, **and** there is no success signal;
- **challenge:** §12.1.

### 11.2 `determine_submit_result(evidence, certification) → (state, proven_not_submitted, reason)`

This is a pure function, and the first matching rule wins:

1. `click_performed == false` **and** `egress_verified_absent` (TOTAL verified with no allow rules ever installed, or restored before any click) → `SUBMISSION_FAILED`, proven. Reason: the cause.
2. `content_changed_during_attempt` → `SUBMISSION_AMBIGUOUS`, `CONTENT_CHANGED`.
3. Success signal observed, TOTAL restored and verified, no content change → `CONFIRMED_SUCCESS`.
4. Failure signal observed **and** `certification.failure_signal_proves_not_submitted` → `SUBMISSION_FAILED`, proven, `EMPLOYER_VALIDATION_ERROR`.
5. `matched_rules_available` **and** no match on `E1_SUBMIT` during the attempt → `SUBMISSION_FAILED`, proven, `NO_SUBMIT_REQUEST_LEFT`.
6. Anything else (timeout, unknown click, restart, challenge timeout, feedback unavailable) → `SUBMISSION_AMBIGUOUS`.

The server stores the attempt event via the existing `record_submission_result`, which already downgrades an unproven FAILED to AMBIGUOUS, then writes `submission-result.v1`.
- **`CONFIRMED_SUCCESS`:** the intent becomes CONFIRMED, and the workflow "applied" status change is recorded in the same transaction, linked to the intent (E19).
- **`SUBMISSION_FAILED`:** the intent is RELEASED.
- **Sweep:** an attempt still in `CLICK_DISPATCHED` after `CHALLENGE_HANDOFF_WINDOW + 60 s` is marked `SUBMISSION_AMBIGUOUS` by the existing sweep (`mark_stale_dispatches_ambiguous`, wired into the scheduler sweeps). An `AUTHORIZED` attempt past `CLICK_DISPATCH_TTL` becomes `EXPIRED_UNCLICKED` (the existing `expire_unclicked`, also wired in).

### 11.3 Matched-rule evidence

`chrome.declarativeNetRequest.getMatchedRules({tabId, minTimeStamp: dispatchTime})` returns the matched rule ids with timestamps. They're reported as `matched_rule_ids`. `matched_rules_available = true` only if the call succeeded and spike S-E2 established that allow-rule matches are reported (compiled constant `MATCHED_ALLOW_RULES_REPORTED`).

## 12. CAPTCHA / challenge handoff

### 12.1 Detection (certified)

A visible element matching the adapter's challenge selectors:
- Greenhouse: `iframe[src*="recaptcha/api2/bframe"]` visible, `iframe[title*="challenge" i]` visible, or `[data-submit-challenge]` (the fixture).

"Visible" means a non-zero client rect and a non-hidden computed style.

### 12.2 Behaviour

- **Before the click** (PRE_SUBMIT): refuse with `CHALLENGE_BEFORE_SUBMIT`, and revoke the grant. The page is under TOTAL, so the challenge can't work. The user is told to close the tab and start the fill again in a fresh tab.
- **After the click:**
  - Enter CHALLENGE_HANDOFF: record `CHALLENGE_DETECTED`, and extend the watch window to `CHALLENGE_HANDOFF_WINDOW`.
  - The popup and the app show "Verification needed: complete the check on the employer tab". The egress stays as installed. The content watcher stays armed.
  - When the challenge is no longer visible → `CHALLENGE_CLEARED`, then a **mandatory re-observation**. If the form is still present, it must reproduce the authorized snapshot's `observation_fingerprint`. A mismatch → `CONTENT_CHANGED_DURING_ATTEMPT`, restore TOTAL immediately → §11.2 rule 2.
  - Any content change during the handoff restores TOTAL immediately.
  - When the window ends → restore TOTAL → §11.2.
- Nothing ever interacts with the challenge. The executor's only primitive is the one `SUBMIT_CLICK`.

## 13. Cancellation, timeouts, recovery

- **Cancel:** the popup, and the app while the state is authorized-not-dispatched (§8.3).
- **Grant expiry (120 s) with no pre-click:** the grant EXPIRES (existing), and the review can be re-authorized **only on a new run** (E7). The page says so.
- **Tab closed:**
  - before dispatch → the attempt expires unclicked;
  - after dispatch → the sweep marks it ambiguous.
- **Lease lost:** the 6D-B reaper marks FILLED_CONTEXT_UNVERIFIED, and no authorization or pre-click is possible afterwards (snapshot precondition, context key check).

## 14. Duplicate prevention

Together, these give J8:
- the live-intent unique index per job identity, where the gate denies `duplicate` for CLAIMED, and for CONFIRMED unless overridden;
- a UNIQUE authorization per fill run;
- a UNIQUE attempt per grant;
- the single-use grant;
- the J3 restart rule.

The Submit Review refuses (`already_submitted`/`submission_in_progress`) while the application has an attempt in AUTHORIZED, CLICK_DISPATCHED, SUBMISSION_AMBIGUOUS or CONFIRMED_SUCCESS.

`record_status_change("applied")` must not create a second live intent when a HUMAN_AUTHORIZED intent is live. Its `record_human_intent` becomes a no-op for an existing live intent, and the success path links `workflow_event_id` to the attempt's intent.

## 15. Evidence: `submission-result.v1`

The stored result:

```
schema: "submission-result", schema_version: "v1"
attempt_id, authorization_id, grant_id, review_hash, fill_run_id
state, proven_not_submitted, reason
certification_id
events: [ {event, at} ... ]        # from submit_events (install/verify/click/signals/restore)
observations: { pre_submit_fingerprint, challenge_cleared_fingerprint?, post_submit_fingerprint? }
matched_rule_ids, matched_rules_available
non_claims: { employer_accepted: false, employer_stored_application: "UNKNOWN",
              content_equals_review_after_click: <bool from evidence>, delivered_to_recruiter: false }
```

`result_hash = canonical_hash("submission-result","v1", result)`. There is no cleartext anywhere in it.

## 16. Product surface

### 16.1 Submit Review page (`/workspaces/{id}/submit`)

It shows the following, rendered from one snapshot and carrying `review_hash`:
- the job, employer and target URL;
- the adapter and certification status;
- the page-check time and fingerprint;
- the fill plan hash and confirmation, and the fill result hash;
- the CV and cover letter (filename + SHA-256);
- each question with the exact rendered value it contains;
- the approval binding hash, policy/control version, and the review hash.

While the re-observation is pending, it shows "Checking the employer page…" and polls `/workspaces/{id}/submit/state`. After `REOBSERVATION_WAIT` it shows "Couldn't reach the employer tab. Keep it open and try again."

The "Submit application" button is enabled only when the human gate returns ALLOW. Otherwise the reasons are listed in plain language.

After authorizing, the page polls the state. It shows "Submitting…", "Verification needed on the employer tab", or the final result. For AMBIGUOUS, it offers "It was submitted" and "It was not submitted" (the resolve route).

### 16.2 Statuses (Prepared list, dossier)

`submission_status` sits next to the fill status:
- `NOT_READY`
- `SUBMIT_READY`
- `SUBMITTING`
- `CHALLENGE_WAITING`
- `SUBMITTED`
- `SUBMISSION_UNCLEAR`
- `SUBMISSION_FAILED`

The dossier lists the attempts with their `submission-result.v1` summaries.

### 16.3 Popup

- "Checking this page for your submission review…"
- "Submission authorized — submitting…" [Cancel]
- "Verification needed: complete the check on this page"
- "Submitted: the employer's confirmation was seen"
- "Outcome unclear: check your email or the employer site, then record it in the app"
- "Not submitted: \<reason\>"

After any result, the page is quarantined again (TOTAL).

## 17. Data: migration `021_human_submit` (atomic, no `executescript`)

New append-only tables, each with triggers that raise on UPDATE/DELETE:
- `human_submit_authorizations` (§8.1);
- `submit_reobservation_requests` (id, account_id, fill_run_id, requested_at);
- `submit_observations` (id, account_id, application_workspace_id, fill_run_id, attempt_id NULL, phase CHECK IN ('REVIEW','PRE_SUBMIT','CHALLENGE_CLEARED','POST_SUBMIT'), structure_fingerprint, observation_fingerprint, observation_json, created_at);
- `submit_events` (id, authorization_id, attempt_id NULL, event CHECK IN the closed list: `PRE_CLICK_REFUSED, CHALLENGE_BEFORE_SUBMIT, CANCELLED_BEFORE_DISPATCH, EGRESS_INSTALLED, EGRESS_VERIFY_FAILED, CLICK_PERFORMED, SUBMIT_CONTROL_MISSING, CHALLENGE_DETECTED, CHALLENGE_CLEARED, CONTENT_CHANGED_DURING_ATTEMPT, SIGNAL_OBSERVED, TOTAL_RESTORED, TOTAL_RESTORE_FAILED, EXECUTOR_RESTARTED, RESULT_REPORTED`, detail_json, created_at);
- `submission_results` (id, attempt_id UNIQUE, result_json, result_hash, created_at).

`submission_intents` is rebuilt (the 019 foreign-keys-off pattern) to add `'HUMAN_AUTHORIZED'` to the source CHECK:
- rows, the partial unique index `idx_submission_intents_live` and FK integrity are preserved;
- `PRAGMA foreign_key_check` must be clean;
- a re-run is a no-op.

## 18. API

Extension routes (session-token authenticated, as in 6D-B):
- `POST /api/extension/fill/runs/{run_id}/submit/observations` {phase, attempt_id?, observation}
- `POST …/submit/pre-click` {grant_id, observation, verification}
- `POST …/submit/{attempt_id}/dispatch`
- `POST …/submit/{attempt_id}/events` {event, detail}
- `POST …/submit/{attempt_id}/result` {evidence}
- `POST …/submit/{attempt_id}/cancel`
- the heartbeat response gains `reobserve: {request_id} | null` and `authorization: {...} | null`

App routes:
- `GET /workspaces/{id}/submit` (HTML; records a re-observation request)
- `GET /workspaces/{id}/submit/state` (JSON)
- `POST /workspaces/{id}/submit/authorize` {review_hash}
- `POST /workspaces/{id}/submit/cancel` {authorization_id}
- `POST /workspaces/{id}/submit/attempts/{attempt_id}/resolve` {submitted: bool}

HTTP semantics are as in 6D-B:
- 404 for an ownership failure;
- 409 with the exact reason for a domain refusal;
- 422 for validation errors;
- extra body keys are forbidden.

## 19. Extension changes

- **Manifest:** add `"declarativeNetRequestFeedback"` to `permissions` (E20).
- **New:** `src/submit/{constants,egress,signals,submit-executor,submit-controller}.ts`, and the popup submit view.
- **`fill-wiring.ts`:** the heartbeat handler routes `reobserve` and `authorization` to the SubmitController. The page API gains `observeForSubmit`, `findSubmitControl(fingerprint)`, `submitClick(fingerprint)` and `signals(certificationId)`.
- **Allowlist/call-graph test:**
  - `.click()` and `requestSubmit`/`submit()` occur only in `submit-executor.ts`;
  - `submitClick` is referenced only from `submit-controller.ts`, after the dispatch acknowledgement and `verifySubmitEgress`;
  - `executor.ts` (6D-B) is unchanged and still clicks nothing;
  - allow rules are built only in `egress.ts`.

## 20. Timing constants (`submit-timing.v1`)

| Constant | Value |
|---|---|
| `SUBMIT_GRANT_TTL` | 120 s (existing 6B constant) |
| `CLICK_DISPATCH_TTL` | 60 s (existing 6B constant) |
| `SUBMIT_REVIEW_OBSERVATION_MAX_AGE` | 60 s |
| `REOBSERVATION_WAIT` | 20 s |
| `SUBMIT_RESULT_WINDOW` | 30 s |
| `SUBMIT_RESULT_POLL` | 250 ms |
| `CHALLENGE_HANDOFF_WINDOW` | 300 s |
| `DISPATCH_RESULT_TIMEOUT` | `CHALLENGE_HANDOFF_WINDOW` + 60 s |

They're mirrored in Python and TypeScript, and a drift test compares the two.

## 21. Spikes (first plan task)

On fixtures, before anything depends on them:
- **S-E1 (GATE).** A priority-2000 tab-scoped `allow` rule overrides the TOTAL block for exactly the matching method, type and URL. A same-origin non-matching POST, a GET to a third-party port, and a service-worker `fetch` all stay blocked. **If this fails, stop and report to the user.**
- **S-E2 (decided fallback).** `getMatchedRules` (with `declarativeNetRequestFeedback`) reports allow-rule matches for the tab. If not: `MATCHED_ALLOW_RULES_REPORTED = false`, rule §11.2-5 never fires, and the permission is dropped from the manifest.
- **S-E3 (GATE).** `element.click()` from the ISOLATED world on the fixture's submit button triggers:
  - (a) a native form POST navigation;
  - (b) a React `onSubmit` handler doing an XHR POST.

  **If either fails, stop and report.** The alternative (trusted input via `chrome.debugger`) is a user decision.
- **S-E4 (decided fallback).** After an allowed main-frame POST navigation to the E2 confirmation URL, `tabs.onUpdated` fires, and re-injecting the page bundle reads the success marker. If not, success is detectable only in-page, and navigation outcomes are `SUBMISSION_AMBIGUOUS`. Fixtures and tests follow the fallback.

The results are recorded in `docs/superpowers/notes/2026-09-29-6e-a-spike-results.md`.

## 22. Acceptance criteria

1. The happy path, with navigation to a confirmation page, ends in `CONFIRMED_SUCCESS`, and every other endpoint recorded zero requests:
   authorize → PRE_SUBMIT → pre-click → dispatch → egress verified → click → success signal → TOTAL restored.
2. The in-page (XHR) success variant ends in `CONFIRMED_SUCCESS`.
3. An employer validation error ends in `SUBMISSION_FAILED`, proven (`EMPLOYER_VALIDATION_ERROR`). The intent is released.
4. A client-side validation block (no E1 match) ends in `SUBMISSION_FAILED`, proven (`NO_SUBMIT_REQUEST_LEFT`) if S-E2 passed; otherwise in `SUBMISSION_AMBIGUOUS`.
5. No signal within the window → `SUBMISSION_AMBIGUOUS`. The user resolves it both ways.
6. **Challenge after the click:** handoff → the challenge is completed by the test acting as the human → re-observation matches → success.
7. **Challenge plus a field edit:** TOTAL is restored immediately → the result follows rule 2 or 5 of §11.2. The recording server shows no E1 request after the edit.
8. **Challenge before the click:** refused. No click, no dispatch, and the grant is revoked.
9. **Stale review:** a field changed after the review rendered → authorize returns 409 `stale_review`.
10. **Page changed between authorize and pre-click:** pre-click refused, grant revoked.
11. **Cancel before dispatch:** `EXPIRED_UNCLICKED` (USER), and zero E1 requests.
12. **Kill switch engaged after authorize:** pre-click or dispatch refused.
13. **Exfiltration during the egress window** (fetch, beacon, image, WebSocket to a third-party port; a non-E1 same-origin POST): zero recorded.
14. **Duplicates:**
    - a second authorize on the same run → 409;
    - another workspace with the same job identity → gate `duplicate`;
    - a double-click on the button → one authorization.
15. **Extension restart after `CLICK_DISPATCHED`:** no second click, and the result is AMBIGUOUS via the report or the sweep.
16. **Live gate:**
    - a greenhouse@2 page on a non-loopback origin → the button is disabled and authorize returns 409 `adapter_not_live_certified`;
    - fixture origins without the env flag → refused;
    - the deployment switch off → `human_submit_disabled`.
17. **Autonomous path:** `request_grant(SUBMIT)` and `pre_click_commit` still raise `SubmissionNotAvailable`. A structural test proves `_pre_click_commit_core` and SUBMIT grant issuance are reachable only from the human functions.
18. **Gate compatibility:** existing context fingerprints are byte-identical (a pinned vector).
19. **Migration 021:** a fresh DB gets 21 migrations. An upgrade from a `efc6577`-built DB with intents, attempts and 6D-B runs preserves them; FK and integrity checks are clean; a re-run is a no-op. It skips only when `efc6577` is absent.
20. **Structural:**
    - the 6D-B executor allowlist is unchanged;
    - click and submit calls occur only in `submit-executor.ts`;
    - allow rules are built only in `egress.ts`;
    - the production build contains no test hooks.

## 23. Testing strategy

- **Pure:** the snapshot and hash vectors (volatile-field exclusion), `determine_submit_result` (every rule and precedence), the certification catalogue and regex escaping, `submission_permitted`, and the gate `HUMAN_SUBMIT` mode (every skipped and applied check, stage validation, fingerprint compatibility).
- **Services:**
  - authorize, pre-click, dispatch, cancel, result and resolve, each with every refusal reason;
  - concurrency (separate connections, WAL, run 20×): double authorize, authorize vs expiry, pre-click racing cancel, result racing the sweep.
- **Persistence:** 021 fresh and upgrade.
- **Extension (vitest, mocked chrome):**
  - the egress rule shapes, their hash, and verify rejecting any extra, missing or altered rule;
  - the controller state machine: each step's failure, the restart rule, cancel, challenge transitions, the content-change restore;
  - the signals;
  - the call graph.
- **Browser (Playwright, packed test-hook build, the recording fixture server):** the §22 scenarios, run one file at a time in the foreground.
- **Existing suites** stay green. The 6D-A/6D-B structural no-submit tests are revised in place from "no submit exists" to "submit is reachable only through the human path". Every other assertion in them is kept.

## 24. Changes to existing contracts (exhaustive)

- **`product/autonomy_contract.py`:** `AuthorityKind` and the defaulted `authority` field; the hash omits the default.
- **`product/autonomy_gate.py`:** the `HUMAN_SUBMIT` branch (§8.2).
- **`webapp/services/autonomy.py`:**
  - `_pre_click_commit_core` gains `authority` and `intent_source` parameters, with defaults preserving 6B;
  - new `request_human_submit_grant`;
  - the public refusals unchanged.
- **`webapp/services/autonomy_context.py`:** `build_context(authority=…)` computes the HUMAN deployment ceiling and `adapter_submit_capable` from §9.5.
- **`webapp/persistence/workflow.py`:** `record_human_intent` is a no-op when a live intent exists (§14).
- **`webapp/services/autonomy_scheduler.py`:** the sweeps call `expire_unclicked` and `mark_stale_dispatches_ambiguous(result_timeout=DISPATCH_RESULT_TIMEOUT)`.
- **`webapp/api/fill_extension.py`:** the heartbeat response composes the 6E-A fields.
- **6D-B extension:** `fill-wiring.ts` hands off to the SubmitController, the page bundle gains the submit page API, and the popup gains submit views. `executor.ts`, `quarantine.ts` and the 6D-B allowlist are unchanged.
- **Config:** `human_submit_enabled` (`JOBSEARCH_HUMAN_SUBMIT_ENABLED`) and `submit_fixture_origins_enabled` (`JOBSEARCH_SUBMIT_FIXTURE_ORIGINS`), both default off.
