# Bundle 6B: Autonomy Contract — Architectural Design

Status: approved design (brainstorming sections 1–5 and spec review, 2026-09-24), not yet implemented
Date: 2026-09-24
Base: `master` @ `91acb5a` (Phase 3 release, tag `phase3-release`)
Depends on: Phase 4A (`policy_decisions`, `product/application_decision_policy.py`), Phase 4B/4C (`application_blockers`, `blocker_resolutions`, `product/semantic_subject_registry.py`, resolved-answer consumption), Application Handoff (`handoff_sessions`, `handoff_events`, `submission_confirmations`, closed field mapping in `webapp/services/handoff.py`), apply-target resolution (`resolve_apply_target` / `ApplyTarget` in `webapp/services/workspace_view.py`), job identity (`application_workspace_job_identities`).

This spec is self-contained. It does not assume the reader has seen the brainstorming session that produced it.

---

## 1. Purpose, context and non-goals

### 1.1 Purpose

Bundle 6's destination is **unattended application submission**: Job Pipeline finds suitable jobs, evaluates them against the user's standing policies, prepares the application, resolves what it safely can from approved evidence, fills the employer form and submits it — without the user present. When something falls outside the user's standing policies, that one application pauses and asks the user, then resumes from the answer.

Bundle 6B defines the **autonomy contract** every later stage is checked against: who may grant autonomy, how an application's effective capability is computed, what may be represented to an employer in the user's name, how authority becomes a single executable action, and how everything is recorded.

The governing principle: **autonomous means pre-authorized, not unrestricted.** The user defines standing authority once; the system acts independently only inside that envelope. Autonomy grows because the user's approved-answer library grows, never because the system is given permission to guess.

### 1.2 Bundle 6 structure

| Sub-bundle | Content |
|---|---|
| 6A Foundations | Deterministic timestamp ordering; CV-v2 submission-lock race; CV-v2 employer-facing presentation; acceptance; un-gate CV-v2 **only if** acceptance passes |
| **6B Autonomy contract** | **This spec** |
| 6C Prepare | Scheduler/queue, eager answer propagation, exception inbox, pause/resume, notification channel |
| 6D Fill | Unattended navigation and filling; form manifests; extension/bridge health made observable |
| 6E Submit | Submission, submission proof, idempotency/recovery, shadow → dry-run → live rollout |

**CV-v2 independence:** nothing in 6B–6E may depend on CV Quality v2 being enabled. The autonomy architecture runs on whichever document path is current (today: the legacy path).

### 1.3 Superseded restrictions

This spec deliberately supersedes, **for applications covered by explicit standing authorization only**:

- Ticket 9 spec §18 ("autonomous job applications; employer submission") and invariant 10 ("There is no autonomous submission path anywhere");
- Application Handoff spec §21 non-goal ("autonomous/unattended job-application submission — the user always performs the final submit action").

Ticket 9 invariant 9 ("`applied` always means the user says the application was actually submitted") is amended: `applied` may also be recorded from an autonomous attempt in state `CONFIRMED_SUCCESS` (6E), and **never** from `AUTHORIZED`, `CLICK_DISPATCHED` or `SUBMISSION_AMBIGUOUS`.

Until 6E ships and passes its rollout gates, no code path submits unattended. The default for every account remains human submission.

### 1.4 Non-goals for 6B implementation

- No scheduler, queue worker or background execution (6C).
- No browser navigation, filling, manifests built from live pages, or extension changes (6D).
- No submission path, submission-proof classification or live rollout (6E).
- No generated free text submitted unseen, now or in Bundle 6 (§7.4). The architecture leaves room for a future explicit permission; it is not part of this contract.
- No standing answers for sensitive subjects (§6.3).
- No CAPTCHA solving, account creation, login, password handling or email verification, in any autonomous stage.
- No machine classification that grants authority (§4.1).
- No canonical Candidate Fact promotion; an `ACCOUNT`-reach approved answer (§7.5) is a reusable answer, not Evidence Profile content, and no UI may call it "saved to profile".

6B builds the **enforceable core**: the pure authorization gate, its schemas and policy data, the decision ledger, grants, limit/budget reservations, submission intents, the attempt record schema, the kill switch (including the file sentinel), shadow-mode decisions and the application dossier. §18 lists exactly what 6B ships.

---

## 2. Invariants

Each invariant must be enforced structurally where possible and covered by tests (§17).

1. **Only explicit user configuration grants authority.** The global maximum and search-workspace ceiling (§4) are the only sources of `PREPARE`/`FILL`/`SUBMIT`. Standing policies, machine classification, inference, deployment settings and freshness rules can only reduce authority, block, or require the user.
2. **Unknown never increases autonomy.** Missing or uncertain information may reduce capability or require clarification; it can never grant.
3. **Decision ≠ Grant ≠ Execution result.** A decision says the policy permits something; a grant says this exact action is authorized now; an execution result says what actually happened. They are separate records.
4. **PREPARE, FILL and SUBMIT are separate decisions.** A prior decision or grant for one stage is never permission for another.
5. **Every employer-facing representation is traceable** to approved evidence, an approved answer, or an automatically-confirmed pack that passed grounding (§7).
6. **The gate is pure and deterministic.** `evaluate_authorization` performs no I/O, reads no clock, mutates nothing, consumes no limits and creates no grants.
7. **Everything is recorded, including refusals.** Every evaluation — allow, reduce, block, deny, require-user, shadow — appends a ledger row with all reasons found.
8. **A SUBMIT grant is single-use, short-lived and atomically consumed**, together with the kill-switch check, limit reservation, intent claim and attempt creation, in one transaction (§10.3).
9. **After `CLICK_DISPATCHED`, no automatic retry** without positive proof that submission did not occur.
10. **Grants are never refreshed in place.** Any material drift from a grant's binding revokes it; a fresh decision and grant are required.
11. **Shadow and dry-run never create executable authority** and never consume real grants, intents, limit slots or reservations.
12. **Releasing a halt never resumes work.** Only an explicit resume makes applications eligible for fresh evaluation.
13. **"Current" is never defined by timestamp.** Every append-only table introduced here carries a monotonic integer `seq`, and every current-state projection — authorizations, kill switch, resume/pause controls, policy versions, approved answers, answer confirmations, rule acknowledgements, attempt events — is derived by `seq` (or an explicit current pointer), never by `created_at` and never by a random-id tie-break. 6B must not recreate the problem 6A fixes.
14. **Canonical hashing.** Every content hash and fingerprint (§15.1) is computed over a specified canonical serialization that embeds its schema name and version. Semantically identical inputs hash identically regardless of key order or formatting.

---

## 3. Vocabulary

**Capability levels** (totally ordered): `NONE < PREPARE < FILL < SUBMIT`.

- `PREPARE` — run the pipeline to an auto-confirmed Application Pack (Job Understanding → Job Fit → Application Intelligence → pack).
- `FILL` — navigate to the apply target and fill the employer form from a fill manifest; never submit.
- `SUBMIT` — perform the irreversible submit action.

**Stages** are the three actions an executor can request: `PREPARE`, `FILL`, `SUBMIT`.

**Decision results** (§9.5):

- `ALLOW(effective_capability)`
- `REQUIRE_USER(items)`
- `BLOCK(reasons)` — a policy outcome (deny-list, gate `AUTO_REJECT`). Not a capability level.
- `DENY(reason)` — permanent for the current inputs: `invalid_input`, `kill_switch`, `duplicate`.
- `DENY_TEMPORARY(reason, retryable=true, retry_at)` — `limit` or `budget`.

`BLOCK` and `REQUIRE_USER` are policy outcomes, modeled separately from capability levels.

**Evaluation modes:** `LIVE`, `SHADOW`, `DRY_RUN` (§14).

**Apply-target provenance tiers** (§8.1): `discovery_verified`, `user_confirmed_apply_target` (new), `user_supplied`, `imported_source`, none.

---

## 4. Authority model

### 4.1 Hierarchy

```
Deployment ceiling (operator; can only lower)            §14.1
  ⊓ Global maximum (ACCOUNT_MAX; explicit user)          grants
  ⊓ Search-workspace ceiling (WORKSPACE_CEILING; explicit user)   grants
  → Restrictive standing policies (REDUCE_TO / BLOCK / REQUIRE_USER)
  → System reductions (identity, provenance, freshness, pack state, deployment)
  → Per-application effective capability
```

`ceiling = min(deployment_ceiling, ACCOUNT_MAX, WORKSPACE_CEILING)`. This is the only point where authority enters evaluation. Everything after it applies `min()` or a non-ALLOW outcome.

A future normalized role-family/region classification may be used in standing-policy predicates to **restrict** an already-authorized workspace, or the user may explicitly map a trusted category to a workspace-like authority record. A machine classification alone can never grant `SUBMIT` (or any level).

### 4.2 Defaults

- **No record means `NONE`.** Absent `ACCOUNT_MAX` → `NONE`; absent `DEFAULT_WORKSPACE_CEILING` → `NONE`. The system never supplies an implicit capability, not even `PREPARE`, so "only explicit user configuration grants capability" is literally true.
- The first enabling action (e.g. "enable autonomous preparation") explicitly writes the authorization records it implies — typically `ACCOUNT_MAX = PREPARE` and `DEFAULT_WORKSPACE_CEILING = PREPARE` — attributed to the user.
- `WORKSPACE_CEILING` absent → the account's explicitly recorded `DEFAULT_WORKSPACE_CEILING` (or `NONE` if there is none). A new search workspace never silently inherits `SUBMIT`; the UI must not offer a default workspace ceiling above `PREPARE` without a deliberate user choice.
- Raising any ceiling or limit is an explicit user action recorded with actor and time.

### 4.3 Example

| Configuration | Effect |
|---|---|
| Global maximum = `SUBMIT` | enables up to SUBMIT anywhere a workspace allows it |
| Workspace "Drilling Fluids — UK/ME" = `SUBMIT` | this pipeline may submit |
| Workspace "Planning Engineer" = `PREPARE` | this pipeline only prepares |
| Rule: fit score < 75 → `REDUCE_TO(PREPARE)`; unknown fit → `REDUCE_TO(PREPARE)` | |
| Rule: company on deny list → `BLOCK`; unknown company key → `REQUIRE_USER` | |
| Rule: employment type ≠ PERMANENT → `REQUIRE_USER`; unknown → `REQUIRE_USER` | |

---

## 5. Standing policy document

### 5.1 Form

One validated, content-hashed JSON document per account per version (`standing_policy_versions`, §15). Every decision cites a single `policy_version_hash`. The schema and a pure evaluator live in `product/standing_policy.py`, validated the way `product/evaluation_policy.py` validates its policy (collect all errors; reject invalid documents; unknown fields rejected).

The document contains:

- `rules[]` — restrictive rules (§5.2);
- `limits` — count limits and budgets (§12);
- `timezone` — the account's IANA timezone used for limit windows (e.g. `Europe/London`); required, no hard-coded default in the engine;
- `employer_lists` — named sets of employer keys (e.g. `deny`).

### 5.2 Rules

```json
{
  "id": "min-fit-75",
  "description": "Only prepare below fit 75",
  "when": {"attr": "fit.overall_score", "op": "lt", "value": 75},
  "effect": {"type": "REDUCE_TO", "level": "PREPARE"},
  "on_unknown": {"type": "REDUCE_TO", "level": "PREPARE"}
}
```

- **Effects** are exactly `REDUCE_TO(NONE|PREPARE|FILL)`, `BLOCK`, `REQUIRE_USER`. The schema has no granting effect; a rule cannot raise authority by construction.
- **Predicates** are over a closed attribute vocabulary (`product/standing_policy.py`): `fit.overall_score`, `fit.verdict`, `job.employment_type`, `job.location`, `job.title`, `company.key`, `workspace.id`, `identity.strength`, and additions by explicit vocabulary change only. Operators: `eq`, `ne`, `in`, `not_in`, `lt`, `lte`, `gt`, `gte`, `contains`, and `in_list` (membership in a named `employer_lists` entry, §5.1 — the only way a rule references an employer list); combinators `all`, `any`, `not`.
- **Three-valued evaluation.** Each predicate evaluates to `TRUE`, `FALSE` or `UNKNOWN` (Kleene logic for combinators).
  - `TRUE` → apply `effect`.
  - `FALSE` → no effect.
  - `UNKNOWN` → apply the rule's declared `on_unknown`.
- **`on_unknown` is mandatory** and must be one of `REQUIRE_USER`, `REDUCE_TO(level)`, `BLOCK`, or `NO_EFFECT`. `NO_EFFECT` is permitted only where absence genuinely has no safety consequence; for a rule whose effect is `BLOCK`, `on_unknown` must be `BLOCK` or `REQUIRE_USER` — the validator rejects `REDUCE_TO(...)` and `NO_EFFECT` there, because missing information must never weaken an explicit prohibition into permission (e.g. "unknown company key" on a deny-list rule must not be read as "not on the list"). A document violating this is invalid, so it fails closed (`DENY(invalid_input)`) if it ever reaches the evaluator. `NO_EFFECT` remains available on non-BLOCK rules where the user declares absence safe.
- **Combination is order-independent.** All applicable effects combine: any `BLOCK` → blocked; `REQUIRE_USER` items accumulate; `REDUCE_TO` takes the minimum. Permuting rules cannot change a result.

---

## 6. Subject policy

### 6.1 Registry extension

`product/semantic_subject_registry.py` remains the closed vocabulary of subjects. It gains policy data in `product/semantic_subject_policy.v1.json`, versioned and content-hashed (`registry_version` / `subject_policy_hash` cited by every decision). Per subject:

| Field | Meaning |
|---|---|
| `answer_kind` | `STRUCTURED` or `FREE_TEXT` |
| `max_reach` | `EMPLOYER` \| `SEARCH_WORKSPACE` \| `ACCOUNT` (§7.5) |
| `default_reach` | ≤ `max_reach`; `SEARCH_WORKSPACE` unless the subject clearly supports broader |
| `context_keys` | keys that must match for reuse, e.g. `["currency","region","employment_type"]` |
| `freshness_days` | integer or `null` (no expiry) |
| `submit_eligible` | whether an answer to this subject may ever be used in unattended SUBMIT |
| `sensitive` | sensitive class (§6.3); implies `submit_eligible=false` in v1 |

Freshness and reach are tuned by editing this data, not the engine.

### 6.2 v1 baseline (tunable)

| Subject | Kind | Max reach | Context keys | Freshness | SUBMIT |
|---|---|---|---|---|---|
| `work_authorization.right_to_work` | STRUCTURED | ACCOUNT | `country` | none | yes |
| `work_authorization.sponsorship_required` | STRUCTURED | ACCOUNT | `country` | none | yes |
| `licence.driving` | STRUCTURED | ACCOUNT | — | none | yes |
| `employment.notice_period` | STRUCTURED | ACCOUNT | — | 60 days | yes |
| `employment.availability_start` | STRUCTURED | ACCOUNT | — | 30 days | yes |
| `compensation.salary_expectation` | STRUCTURED | SEARCH_WORKSPACE | `currency`, `region`, `employment_type` | 60 days | yes |
| `mobility.relocation` | STRUCTURED | SEARCH_WORKSPACE | `region` | 180 days | yes |
| `mobility.travel_or_rotation` | STRUCTURED | SEARCH_WORKSPACE | `region` | 180 days | yes |
| `motivation.role_type` | FREE_TEXT | ACCOUNT | — | 180 days | yes |
| `motivation.industry` | FREE_TEXT | ACCOUNT | — | 180 days | yes |
| `motivation.job_search_reason` | FREE_TEXT | ACCOUNT | — | 180 days | yes |
| `motivation.employer_specific` | FREE_TEXT | EMPLOYER | — | 365 days | yes |
| `legal.attestation` | — | — | — | — | **no** (sensitive) |
| `demographic.eeo` | — | — | — | — | **no** (sensitive) |
| `background.criminal_record` | — | — | — | — | **no** (sensitive) |
| `health.disability` | — | — | — | — | **no** (sensitive) |

The first four keys already exist in the registry (`work_authorization.*`, `employment.notice_period`, `licence.driving`); the rest are new registry entries, which the registry's own contract permits as additions.

A generic answer never satisfies an employer-specific question: `motivation.employer_specific` ("Why Wood?") is a distinct subject from `motivation.role_type`/`industry`/`job_search_reason`, and only an approved answer for the same employer key satisfies it.

### 6.3 Sensitive subjects

- A **required** sensitive field on the form → `REQUIRE_USER`; unattended SUBMIT stops.
- An **optional** sensitive field → left unanswered when the form permits omission; this is not a representation and does not pause the application.
- No standing answers for sensitive subjects in v1. A later spec may introduce explicitly approved standing answers for selected sensitive subjects.

---

## 7. Representation contract

Unattended SUBMIT is permitted only when **every** employer-facing representation on three surfaces satisfies this section. At FILL, unsatisfied items are simply not filled (and recorded); at SUBMIT, any unsatisfied required item makes the decision non-grantable.

### 7.1 Application Pack (CV, cover letter, pack)

- Automated generation is allowed.
- Every factual claim must be grounded in approved evidence; the existing unsupported-claim validation must report **zero** unsupported claims.
- Zero unresolved `REQUIRE_USER` policy decisions for the pack.
- Only then may the pack **auto-confirm**. Auto-confirmation writes a review record whose reviewer provenance is the autonomy gate (distinguishable from a human review), referenced by the decision. A pack that cannot auto-confirm caps capability at `PREPARE` and raises the relevant `REQUIRE_USER` items.
- The pack path used is whatever is current; CV-v2 is not required (§1.2).

### 7.2 Structured form answers

- Must trace to an approved evidence field (candidate snapshot path, generalizing the closed mapping in `webapp/services/handoff.py`) or to an approved answer (§7.5) for the same canonical subject, within reach and context, and fresh for SUBMIT.
- **Format-only transforms** are allowed from a closed, deterministic set in `product/representation_transforms.py`: country name ↔ ISO code, date format (ISO ↔ dd/mm/yyyy, mm/dd/yyyy), phone → E.164, whitespace normalization, identity. Case normalization is not part of the 6B set; it is added deliberately in 6D only when real form behaviour requires it. Each transform declares a round-trip check, implemented independently of the transform itself, proving the underlying value is unchanged; malformed input fails closed. No other transformation is permitted.

### 7.3 Free-text screening questions

- For unattended SUBMIT, the answer must be a previously approved reusable answer for that canonical subject, within reach, context and freshness.
- No approved answer → `REQUIRE_USER`.

### 7.4 No unseen generated free text

The system must not generate a new answer to employer-facing free-text questions — motivation ("Why do you want to work here?"), salary explanations, relocation reasoning, notice-period explanations, experience narratives, or any other — and submit it unseen.

While an application is paused, the system **may** generate a **proposed answer**, stored with provenance `SYSTEM_PROPOSED`. A proposed answer is never usable by any stage until the user approves (optionally editing) it. Approval may include "reuse this answer for this subject", with a reach chosen up to the subject's `max_reach`.

The data model keeps `provenance` explicit on every answer so a future, separately-specified permission for generated free text could be added; Bundle 6 does not add it.

### 7.5 Approved-answer library

- `approved_answers` is separate from `blocker_resolutions`. A resolution records "this blocker was answered"; an approved answer records "this may be reused". Resolving a blocker may create an approved answer.
- Reach: `EMPLOYER` (scope id = employer key), `SEARCH_WORKSPACE` (scope id = search workspace id), `ACCOUNT`. The user picks a reach ≤ the subject's `max_reach`; the default is the subject's `default_reach`.
- Rows are immutable. Editing creates a new row that supersedes the old one (`supersedes_id`); the current answer for (subject, reach, scope, context) is the latest non-superseded row by `seq`.
- **Freshness** is anchored at the latest `answer_confirmations` row (append-only; approval itself writes the first). An expired answer is never deleted: it remains usable for `PREPARE` and, where the subject allows, `FILL`; it loses unattended `SUBMIT` eligibility until reconfirmed. Expiry only ever reduces autonomy.
- **Validity is more than freshness.** Each approved answer records its **basis**: the candidate evidence/profile fields it restates or depends on (evidence ids and the canonical hash of their values at approval, from the current `profile_snapshot`), or an explicit `USER_ASSERTION` basis when no profile field corresponds. At evaluation the gate receives the current values of those fields:
  - basis field superseded or changed → the answer is **stale-by-basis**: ineligible for unattended SUBMIT (usable for PREPARE/FILL only where the subject allows) until the user reconfirms or replaces it;
  - current evidence **contradicts** the answer (a conflicting value for the same subject in the current profile or a newer resolved answer) → ineligible for SUBMIT **and** FILL, and raised as `REQUIRE_USER`.
  "No expiry" for a stable fact therefore means no *time-based* expiry; it never lets an old answer survive a changed candidate profile.
- Context keys must match exactly for reuse; an answer lacking a required context value is not reusable for a job whose context is known to differ, and a job whose context value is unknown cannot use it for SUBMIT (it may for PREPARE/FILL).

### 7.6 Employer key

Employer-bound answers and per-employer limits need a durable **employer key**: the ATS tenant/company key from the adapter where available, else a normalized company key from job identity, with a recorded strength (`ATS_TENANT` | `NORMALIZED_NAME` | `UNKNOWN`). `UNKNOWN` → employer-bound answers are unusable and the per-employer limit cannot be evaluated, which caps capability at `FILL` with reason `employer_key_unknown`.

---

## 8. Apply target and executor trust

### 8.1 Provenance tiers and caps

A new tier, `user_confirmed_apply_target`, records that the user explicitly selected/confirmed this exact target as the submit destination for this job. It is distinct from `user_supplied` ("I pasted a URL"). It is written only by an explicit user confirmation action, with actor and time, and is bound to the job identity.

| Target | Cap |
|---|---|
| `discovery_verified` **and** all SUBMIT conditions below | `SUBMIT` |
| `user_confirmed_apply_target` **and** all SUBMIT conditions below | `SUBMIT` |
| plain `user_supplied` (any domain, including supported ATS) | `FILL` |
| `imported_source` | `FILL` |
| unsupported or generic site | `FILL` |
| no target | `PREPARE` |

SUBMIT conditions (all required):

1. the adapter and adapter version are declared `submit_capable` (§8.2);
2. the final page after redirects stays within the adapter's declared domain/redirect set;
3. the ATS tenant/company identity read from the page matches the expected employer key;
4. the ATS job posting identifier matches the durable job identity where one is available;
5. no unexplained redirect or identity mismatch occurred.

A domain match alone is never sufficient. Any mismatch → `REQUIRE_USER` (never "probably the same job").

### 8.2 Adapter tiers

- Only adapters declared `submit_capable` at a pinned version may receive SUBMIT grants. Candidates: Greenhouse and Lever, and only after 6E rollout gates. The generic adapter is capped at `FILL`.
- A submit-capable adapter must produce a **complete form manifest** (every field on every page; classification to a normalized field type or semantic subject; required/optional; conditional triggers).
- Unclassified **required** field → `REQUIRE_USER`. Unknown **optional** field → left blank only when the form permits omission.
- New required or conditional fields appearing after manifest creation → fresh classification, usually `REQUIRE_USER`; never a silent rebuild.

### 8.3 Fill manifest

The server builds an immutable, content-hashed **fill manifest** and binds its hash into FILL and SUBMIT grants. Each entry:

`page_key`, `page_field_key`, `normalized_field_type` or `subject`, `source` object `{kind, ref, confirmation_id}` — `kind` ∈ `EVIDENCE` (ref = evidence path), `APPROVED_ANSWER` (ref = `approved_answer_id`, `confirmation_id` = `answer_confirmation_id`, required), `PACK_DOCUMENT` (ref = pack document hash), `transform_id` (§7.2), `value_hash`, `required`.

The manifest answers "exactly what did Job Pipeline authorize the browser to put into this employer form". The executor never invents a value; it may only place what the manifest releases. The 6B deliverable is the manifest **schema, hashing and validation** (`product/fill_manifest.py`); 6D builds manifests from live pages.

### 8.4 Pre-click verification

Before SUBMIT, the executor reads back every field's DOM value and every attachment hash and sends a **verification snapshot**. The pre-click transaction (§10.3) compares it with the grant's manifest. Any mismatch, or any field present that is not in the manifest, revokes the grant and returns the application to `FILLED` for fresh classification and a fresh decision.

### 8.5 Hard stops

CAPTCHA, login wall, account creation, password entry, email/SMS verification, or any step outside the manifest → `REQUIRE_USER`. No workaround in any autonomous stage.

### 8.6 Threat model and trust boundary

- **The server is the only authority.** Decisions and grants are server-created and server-validated.
- **The executor is a non-authoritative execution client.** It holds grants, executes manifests and reports DOM snapshots, `CLICK_DISPATCHED` and result observations. It cannot create values, decisions or grants. Its reports are **client-supplied evidence**, bound to a paired session, grant nonce, adapter version and manifest hash — not independent proof of truth.
- **Threat model for Bundle 6: local, single-user.** The paired extension on the user's own machine is assumed not to be adversarial; the design does not defend against a compromised executor forging telemetry. A multi-user or hosted deployment requires a separate threat-model revision before live SUBMIT.
- **Employer page content is untrusted input.** In the FILL and SUBMIT path, no LLM reads page content to decide actions; field classification is deterministic adapter code. A future LLM classifier may only reduce capability (raise `REQUIRE_USER`), never grant or choose values.
- **Executor-agnostic.** Any executor must follow the protocol: grant → manifest → verification snapshot → durable `CLICK_DISPATCHED` → result.

---

## 9. The authorization gate

### 9.1 Location and signature

`product/autonomy_gate.py`:

```python
def evaluate_authorization(ctx: AuthorizationContext) -> AuthorizationDecision: ...
```

Types in `product/autonomy_contract.py` (frozen dataclasses, `ENGINE_VERSION`). Pure: no I/O, no clock (`ctx.now` is an input), no mutation, no randomness.

### 9.2 Context (`AuthorizationContext`)

- `mode` (`LIVE` | `SHADOW` | `DRY_RUN`), `requested_stage`, `now`;
- authority: `deployment_ceiling`, `account_max`, `workspace_ceiling` (with record ids);
- `kill_switch_engaged`, `sentinel_present`;
- `standing_policy` (document + hash), `subject_policy` (document + hash);
- job facts: attributes for the closed predicate vocabulary, each either a value or `UNKNOWN`;
- governing `policy_decisions` outcomes for the workspace;
- pack state: grounding result, unresolved `REQUIRE_USER` count, auto-confirmable flag, pack artifact id;
- representation manifest requirements: required fields/questions with their resolved source candidates (approved answers with latest confirmation, recorded basis and the **current** values of their basis fields, evidence paths);
- current rule acknowledgements for the application (rule id, rule hash, observed fingerprint, disposition);
- `run_id` when the evaluation belongs to an autonomous run;
- apply target: URL, provenance tier, adapter id/version, submit-capable flag, employer key + strength, expected ATS job id;
- identity: `job_identity_key`, strength (`SOURCE_RECORD` | `CANONICAL_URL` | `WEAK`), conflict flag, existing intents for the key;
- limits snapshot: counters per window, open reservations; budget snapshot per category;
- for SUBMIT at pre-click: the grant binding and the verification-snapshot comparison result.

Assembled by `webapp/services/autonomy.py` from database reads; for SUBMIT at pre-click, inside the same transaction (§10.3).

### 9.3 Algorithm: collect, then resolve

Every check runs and emits reason codes; the result is derived afterwards by fixed precedence. The ledger always holds the complete explanation.

1. **Validate** the context (versions known, required inputs present, documents valid, every closed-schema field and container of the expected type). Failure → `DENY(invalid_input)` deterministically, never an exception. The decision records the validation errors plus any fully independent safe facts (a validly-typed engaged kill switch or present sentinel); checks that depend on malformed input are not evaluated.
2. **Ceiling** = `min(deployment_ceiling, account_max, workspace_ceiling)`.
3. **Reductions** (each `min()`):
   - standing-policy `REDUCE_TO` effects and `on_unknown` reductions;
   - identity strength `WEAK` or identity conflict → `FILL`;
   - apply-target tier cap (§8.1);
   - employer key `UNKNOWN` → `FILL`;
   - an answer SUBMIT would need is expired, stale-by-basis (§7.5), or its context is unknown → `FILL`. A genuinely optional field (omission permitted) is not needed by SUBMIT: its expired/stale/context-unknown answer is omitted (`optional_omitted`) and does not reduce;
   - an answer contradicted by current evidence is not a permitted source at all (it produces a `REQUIRE_USER` item in step 5);
   - a **required** field that SUBMIT needs and that has no permitted source — answer missing, contradicted, unclassified, or sensitive — caps capability at `FILL` exactly as an expired required answer does, while still producing its `REQUIRE_USER` item in step 5. `effective_capability` therefore always states the highest level that can actually proceed now; a worse answer state can never record a higher capability. Genuinely optional (omittable) fields remain non-blocking;
   - pack not auto-confirmable → `PREPARE`;
   - `mode` ≠ `LIVE` does **not** reduce (shadow evaluates the live outcome) but makes the decision non-grantable (§14).
4. **Stops:** kill switch or sentinel → `kill_switch`; standing-policy `BLOCK` or governing `AUTO_REJECT` → `BLOCK`; a `CLAIMED` (in-flight) intent for the identity → `duplicate` always; a `CONFIRMED` intent → `duplicate` unless the user recorded an explicit override (overrides apply to confirmed submissions only, never to an in-flight claim); exhausted count or budget → `limit`/`budget` with `retry_at` when computable.
5. **Questions:** standing-policy `REQUIRE_USER` (effect or `on_unknown`) not covered by a current rule acknowledgement; unresolved governing `REQUIRE_USER` decisions; required fields/questions without a permitted source; sensitive required fields; hard stops reported by the executor.
   - *Stage scoping:* standing-policy and governing-decision items apply to every stage. Field/question items (required fields, free-text questions, sensitive fields), apply-target/page identity mismatches and executor hard stops arise only for FILL and SUBMIT.
   - *Hard stops* (CAPTCHA, login/account wall, email verification, out-of-manifest step) always surface at FILL/SUBMIT; they are operational blockers and are not subject to relevance.
   - *Relevance:* surface **all actionable blockers that must eventually be cleared to reach the requested stage**, even when clearing one alone would not suffice, so the inbox shows everything at once. Relevance is judged against the *structural* cap — the ceilings and non-actionable reductions (standing-policy `REDUCE_TO`, identity, apply-target tier/adapter/verification, employer key) — not against actionable reductions (pack not auto-confirmable, answer freshness). A field/question item that could not help reach the requested stage even with every actionable blocker cleared is recorded as a silent reason and does not pause the application.
   - *Rule acknowledgements:* the user clears a standing-policy `REQUIRE_USER` item for one application by an explicit `rule_acknowledgements` record (proceed / do not proceed), bound to the application, the rule id, the **rule content hash** (canonical hash of that one rule, §15.1) and the fingerprint of the attribute values the rule observed. The overall `policy_version_hash` is recorded for audit only. Validity depends on the rule content hash and the observed-attribute fingerprint: editing an unrelated rule does not invalidate the acknowledgement; changing the acknowledged rule, a change in the attribute values it observes, or a change to the contents of any employer list it references (`in_list`), does — the referenced lists' contents are part of the observed fingerprint. "Do not proceed" becomes `BLOCK` for that application. An acknowledgement never raises capability above the ceiling; it only lifts a restriction the user themselves wrote.

### 9.4 Precedence

```
DENY(invalid_input | kill_switch)
  > BLOCK
  > DENY(duplicate)
  > DENY_TEMPORARY(limit | budget)
  > REQUIRE_USER
  > ALLOW(effective_capability)
```

All lower-precedence reasons are retained in the decision.

### 9.5 Output (`AuthorizationDecision`)

- `result` (§3), `requested_stage`, `effective_capability`, `grantable = (result is ALLOW) and effective_capability ≥ requested_stage and mode == LIVE`;
- `reasons[]` — stable, machine-readable codes with parameters, sorted deterministically;
- `require_user_items[]` — subject / field / rule references;
- `retryable`, `retry_at` for `DENY_TEMPORARY`;
- `input_fingerprint` — hash of the canonicalized context;
- `engine_version`, `policy_version_hash`, `subject_policy_hash`.

A SUBMIT request returning `ALLOW(FILL)` is not an engine failure: no SUBMIT grant is issued and the application settles at `FILLED_AWAITING_HUMAN_SUBMIT` (§11.1).

### 9.6 Timing

Evaluation runs: at enqueue (PREPARE); immediately before FILL; and, for SUBMIT, once to issue the SUBMIT grant and again inside the pre-click transaction against current authoritative state. SUBMIT always uses the current policy state; a grant snapshot is never authority by itself.

---

## 10. Grants, reservations, intents and attempts

### 10.1 Grants

`autonomy_grants`: one per grantable LIVE decision. Fields: `decision_id` (unique), `stage`, `nonce`, `binding_json` and `binding_fingerprint`, `issued_at`, `expires_at`, `status` (`ISSUED` | `CONSUMED` | `EXPIRED` | `REVOKED`), `consumed_at`, `revoked_reason`.

The binding includes, where applicable: pack artifact id and document hashes; fill-manifest hash; approved-answer ids and confirmation ids; apply-target canonical URL, provenance tier, adapter id/version, tenant/employer key, ATS job id, permitted redirect set; job identity key; `policy_version_hash`, `subject_policy_hash`, `engine_version`; account/workspace ids.

- FILL grant: bound to one fill session (the shape of today's `handoff_sessions`), repeatable within that session and its TTL.
- SUBMIT grant: single-use.
- **TTLs are stage-specific, separately configurable constants** in `product/autonomy_contract.py`: `FILL_SESSION_TTL` (long enough for multi-page applications; proposed 30 min), `SUBMIT_GRANT_TTL` (proposed 120 s), `CLICK_DISPATCH_TTL` (proposed 60 s, §10.4). No TTL is shared between stages.
- Any change to a bound input invalidates the grant; it is marked `REVOKED` (reason `stale_binding`), never updated.

### 10.2 Submission intents (duplicate prevention)

`submission_intents`: `account_id`, `job_identity_key`, `state` (`CLAIMED` | `CONFIRMED` | `RELEASED`), `source` (`AUTONOMOUS` | `HUMAN_HANDOFF` | `HUMAN_APPLIED`), links to the attempt or handoff confirmation. A partial unique index on `(account_id, job_identity_key) WHERE state IN ('CLAIMED','CONFIRMED')` makes a second live intent impossible.

- The key is taken from `application_workspace_job_identities` in precedence `source_record_key` > `canonical_url_key`. A `weak_fallback_key`-only identity can reach FILL, never unattended SUBMIT.
- Human submissions (handoff confirmations, user-recorded `applied`) create `CONFIRMED` intents, so duplicate prevention spans both paths.
- A `CONFIRMED` intent permanently suppresses autonomous submission for that identity unless the user records an explicit `intent_overrides` row.
- `CLAIMED` is released only by `EXPIRED_UNCLICKED`, proven pre-submission failure (§11.2), or a user attestation that submission did not occur.

### 10.3 The pre-click transaction

Immediately before the executor may click, one `BEGIN IMMEDIATE` transaction:

1. reads the kill-switch state and the file sentinel (synchronously);
2. assembles the SUBMIT context from current state and runs `evaluate_authorization` (writing its decision row);
3. compares the decision and the verification snapshot with the grant binding; any drift → revoke grant, commit the denial, no click;
4. reserves the count limits (§12) — conditional insert that fails if the window is exhausted;
5. consumes the SUBMIT grant — `UPDATE … SET status='CONSUMED' WHERE id=? AND status='ISSUED' AND expires_at > :now`, requiring exactly one row changed;
6. claims the intent — insert `CLAIMED` (the partial unique index enforces exclusivity);
7. creates the `submission_attempts` row in state `AUTHORIZED`.

Commit is the **authorization point of no return** — not proof that submission happened. Kill switch engaged before commit → no click. Engaged after commit → the already-authorized browser action may complete; that fact is recorded on the attempt.

### 10.4 Attempt lifecycle

`submission_attempts` (schema in 6B; populated by 6E):

```
AUTHORIZED ──► CLICK_DISPATCHED ──► CONFIRMED_SUCCESS
     │                 ├──────────► SUBMISSION_AMBIGUOUS
     │                 └──────────► SUBMISSION_FAILED
     └──► EXPIRED_UNCLICKED
DUPLICATE_SUPPRESSED   (terminal; no submission attempted)
```

- The executor must receive server acknowledgement that `CLICK_DISPATCHED` is durably recorded **before** dispatching the click. No acknowledgement, no click.
- `AUTHORIZED` with no dispatch record after `CLICK_DISPATCH_TTL` → `EXPIRED_UNCLICKED`: the grant is dead, the intent is released, and a completely fresh SUBMIT decision is required.
- `CLICK_DISPATCHED` with no classified result → `SUBMISSION_AMBIGUOUS`. **Never retried automatically.** The user resolves it to `CONFIRMED_SUCCESS` or attests non-submission.
- `SUBMISSION_FAILED` permits retry only with positive proof that nothing was submitted (e.g. adapter-declared validation errors still on the form); otherwise it is treated as ambiguous.
- Post-dispatch browser evidence classifies the result; it never authorizes a retry.
- Transitions are append-only (`submission_attempt_events`); the current state is derived.

---

## 11. Lifecycle, queue and exceptions (contract for 6C–6E)

### 11.1 States

State is **derived** from the ledger, grants, attempts, intents, open exceptions and control events, never asserted. The only mutable scheduling record is `autonomy_queue_items` (`application_workspace_id`, `next_stage`, `next_eligible_at`, `lease_holder`, `lease_expires_at`, `paused`); it carries no authority. Its `paused` flag is an operational cache of `autonomy_control_events` (§11.4), which is the authoritative, append-only history of every pause and resume.

```
QUEUED ─► PREPARING ─► PREPARED ─┬─► PREPARED_AWAITING_HUMAN                 (eff = PREPARE)
                                 └─► FILLING ─► FILLED ─┬─► FILLED_AWAITING_HUMAN_SUBMIT  (eff = FILL)
                                                        └─► SUBMIT_AUTHORIZED ─► SUBMITTING ─► SUBMITTED
                                                                   │                 ├─► SUBMISSION_AMBIGUOUS
                                                                   │                 └─► SUBMISSION_FAILED
                                                                   └─► EXPIRED_UNCLICKED ─► FILLED (fresh decision)
```

Overlays, by deterministic precedence: `HALTED > BLOCKED > NEEDS_USER > PAUSED > WAITING(retry_at) > lifecycle state`.

Each forward arrow requires a fresh `ALLOW` for that stage (and a grant for FILL/SUBMIT). `*_AWAITING_HUMAN` states hand off to the existing human handoff flow.

### 11.2 Idempotency

- PREPARE is safely idempotent (content-addressed artifacts).
- FILL is conditionally repeatable within the same bound session and context.
- SUBMIT is deliberately **not** generically idempotent; its safety comes from the durable intent, the single-use grant and ambiguous-state handling.

### 11.3 Exceptions (the inbox)

- A `REQUIRE_USER` decision opens items that reuse Phase 4B `application_blockers` keyed by semantic subject — no new blocker model.
- Each inbox entry lists **all** current reasons for that application (collect-then-resolve).
- A proposed answer (§7.4) may accompany an item.
- Resolving writes a `blocker_resolution` and optionally an `approved_answer`; the application's queue item becomes eligible (`next_eligible_at = now`) and re-evaluates from the stage it paused at.
- **Eager propagation wakes** sibling applications waiting on the same subject within the answer's reach; it never clears them. Each sibling independently re-checks scope, context keys, freshness and policy.

### 11.4 Stop mechanisms

| Mechanism | Scope | Effect |
|---|---|---|
| Pause | application or search workspace | no new decisions or grants; in-flight atomic step completes; ceilings unchanged |
| Ceiling/policy change | workspace or account | effective at the next evaluation (incl. pre-FILL and pre-click) |
| Kill switch (UI / local endpoint / file sentinel) | global | all stages → `DENY(kill_switch)`; every `ISSUED` grant revoked in the engaging transaction; consumed grants and existing attempts continue their lifecycle and are not rewritten |

Every pause, resume and resume-all is an `autonomy_control_events` row with actor, reason and time; kill-switch changes are `autonomy_kill_switch` rows. No safety-relevant control exists only as a mutable flag.

**Resume:** clearing the kill switch or removing the sentinel only removes a halt signal. Applications stay halted until an explicit **resume all**, which wakes queue items for fresh evaluation from current policy; it never revives old decisions or grants.

### 11.5 Recovery

Lost leases are retaken after expiry. `DENY_TEMPORARY` sets `next_eligible_at = retry_at`. `BLOCK` and `DENY(duplicate)` are not retried by the scheduler; only an authority, policy or override change reopens them.

---

## 12. Limits and budgets

### 12.1 Count limits (policy data; product defaults)

| Limit | Default |
|---|---|
| SUBMIT per calendar day | 3 |
| SUBMIT per autonomous run | 3 |
| FILL per calendar day | 10 |
| SUBMIT per employer key per rolling 30 days | 2 |

Defaults are deliberately conservative and are not permanent. Raising any limit is an explicit user action. Calendar windows use the policy document's IANA `timezone`. An "autonomous run" is a durable `autonomy_runs` record (§15); its identity is part of this contract so the per-run limit is evaluable and auditable, and every SUBMIT decision, grant and attempt carries its `run_id`. 6C starts and ends runs.

Exact duplicate prevention is §10.2; the per-employer limit is an anti-flood control.

### 12.2 Budgets

Budget categories: `LLM`, `BROWSER`, `EXTERNAL_API`, `OTHER`, each with per-day and per-application caps. The engine consumes a generic **cost-budget input**; it is not coupled to any one table. Initially `provider_audits` supplies `LLM` actuals.

**Reservation guarantee.** Each cost-generating stage reserves its declared maximum cost when its grant is issued and reconciles to actuals afterwards. The worst-case overrun is bounded by one in-flight reservation **only if** every cost-generating operation has an enforceable maximum envelope (e.g. `max_tokens` × a price table held as data, a request cap). An operation that cannot be hard-bounded must reserve a genuinely safe maximum or it may not run under autonomy (fail closed).

### 12.3 Reservation semantics

`limit_reservations`: `counter_key` (account, window, kind), `grant_id` or `attempt_id`, `amount`, `status` (`RESERVED` | `CONSUMED` | `RELEASED`). Reservations are made atomically with the grant (FILL, budgets) or in the pre-click transaction (SUBMIT counts), so two workers cannot both take the last slot. Shadow and dry-run never reserve.

---

## 13. Application dossier (first-class feature)

A read-only, derived, exportable (JSON) record per application answering: **what did it apply for, why, what exactly did it send, where, and what happened?**

It joins: the full decision chain (PREPARE → FILL → SUBMIT, including denials, reductions, shadow and dry-run decisions) with all reasons; authority and policy versions cited; grant bindings; the fill manifest (every value, source and transform); verification snapshots; attempt lifecycle with evidence; the intent; pack artifact and document hashes; exceptions raised and how they were answered.

It is never edited. Ledger rows are retained indefinitely (local data). 6B ships the dossier service and a read-only UI page; 6C–6E extend it as their records appear.

---

## 14. Rollout, shadow and dry-run

### 14.1 Deployment ceiling

Operator settings in `webapp/config.py` `Settings` (off by default, like `cv_quality_v2_enabled`): a global `autonomy_max_capability`, a per-adapter `submit_capable` allow-list, and a `live_submit_daily_cap`. The deployment ceiling may lower `SUBMIT` to `FILL`/`PREPARE`/`NONE`; it can never raise anything beyond the user's explicit authorization.

### 14.2 Stages

| Stage | Behavior |
|---|---|
| 0 (default) | Autonomy off; today's behavior. |
| 1 Shadow | The gate evaluates and writes decisions with `mode=SHADOW`; no grants; the user works normally; the dossier compares "would have" with what the user did. |
| 2 Live PREPARE + FILL | Option B in practice; exercises manifests and verification. |
| 3 Dry-run SUBMIT | Full pipeline through the pre-click checks with `mode=DRY_RUN`; ends in a `dry_run_submission_cases` record (`DRY_RUN_WOULD_SUBMIT`); the user clicks via the human flow and records agreement. |
| 4 Live SUBMIT canary | Per adapter, hard **1/day** regardless of the user's configured limits. |

### 14.3 Dry-run isolation

A dry-run must not consume a real SUBMIT grant, submission intent, daily submission slot or submission-related cost reservation. It produces `DRY_RUN` decisions and `dry_run_submission_cases` evidence only; the verification snapshot is compared against the FILL grant's manifest. Shadow decisions are likewise non-executable.

### 14.4 Promotion criteria (per submit-capable adapter)

- ≥ 10 clean dry-run cases;
- zero identity, manifest and verification mismatches;
- explicit user agreement on each case that it would have been acceptable to submit;
- cases span more than one employer, job and form shape where practical.

Then live SUBMIT starts as the 1/day canary. The deployment ceiling is raised only after clean live-canary evidence.

---

## 15. Data model

New migration(s) `016_autonomy_contract` onward. All append-only tables carry `seq INTEGER PRIMARY KEY AUTOINCREMENT` (or a unique monotonic `seq` column), and every current-state projection uses it (§2 invariant 13); `created_at` is informational only. Ids are opaque text ids alongside `seq` where referenced externally.

| Table | Kind | Key columns |
|---|---|---|
| `autonomy_authorizations` | append-only | `account_id`, `scope_type` (`ACCOUNT_MAX` \| `WORKSPACE_CEILING` \| `DEFAULT_WORKSPACE_CEILING`), `scope_id`, `capability`, `set_by`, `created_at` |
| `autonomy_kill_switch` | append-only | `account_id`, `engaged`, `reason`, `actor`, `created_at` |
| `standing_policy_versions` | append-only | `account_id`, `policy_json`, `policy_hash`, `created_by`, `created_at` |
| `approved_answers` | append-only | `id`, `account_id`, `subject`, `answer_kind`, `value_json`, `reach`, `scope_id`, `context_json`, `provenance` (`USER` \| `USER_EDITED_PROPOSAL`), `basis_json` (evidence ids + value hash, or `USER_ASSERTION`), `basis_profile_version_id`, `supersedes_id`, `source_blocker_resolution_id`, `approved_by`, `created_at` |
| `answer_confirmations` | append-only | `approved_answer_id`, `confirmed_by`, `created_at` |
| `proposed_answers` | append-only | `id`, `blocker_id`, `subject`, `value_json`, `provenance='SYSTEM_PROPOSED'`, `created_at` |
| `rule_acknowledgements` | append-only | `account_id`, `application_workspace_id`, `rule_id`, `rule_hash`, `observed_fingerprint`, `policy_version_hash` (audit only), `disposition` (`PROCEED` \| `DO_NOT_PROCEED`), `actor`, `created_at` |
| `autonomy_runs` | append-only start/end events | `run_id`, `account_id`, `started_by` (`SCHEDULER` \| `USER`), `started_at`, `ended_at`, `end_reason` (identity defined in 6B; operated from 6C) |
| `autonomy_control_events` | append-only | `account_id`, `scope_type` (`APPLICATION` \| `SEARCH_WORKSPACE` \| `ACCOUNT`), `scope_id`, `action` (`PAUSE` \| `RESUME` \| `RESUME_ALL`), `actor`, `reason`, `created_at` |
| `apply_target_confirmations` | append-only | `application_workspace_id`, `job_identity_key`, `canonical_url`, `confirmed_by`, `created_at` |
| `autonomy_decisions` | append-only | `id`, `account_id`, `application_workspace_id`, `mode`, `requested_stage`, `result`, `effective_capability`, `grantable`, `reasons_json`, `require_user_json`, `retry_at`, `inputs_json`, `input_fingerprint`, `engine_version`, `policy_version_hash`, `subject_policy_hash`, `grant_id` (pre-click evaluations), `created_at` |
| `autonomy_grants` | status column; transitions append-logged | per §10.1 |
| `limit_reservations` | status column | per §12.3 |
| `submission_intents` | state column + partial unique index | per §10.2 |
| `intent_overrides` | append-only | `intent_id`, `actor`, `reason`, `created_at` |
| `submission_attempts` | row per consumed SUBMIT grant | `id`, `grant_id` (unique), `intent_id`, `run_id`, `created_at` |
| `submission_attempt_events` | append-only | `attempt_id`, `state`, `evidence_json`, `source` (`SERVER` \| `EXECUTOR` \| `USER`), `created_at` |
| `dry_run_submission_cases` | append-only | `decision_id`, `adapter_id`, `adapter_version`, `manifest_hash`, `verification_result`, `user_agreement`, `created_at` |
| `autonomy_queue_items` | mutable (scheduling only) | per §11.1 (table created in 6B; used from 6C) |

`blocker_resolutions`, `application_blockers`, `policy_decisions` and `handoff_*` are unchanged; new tables reference them.

### 15.1 Canonical hashing

One function, `canonical_hash(schema: str, schema_version: str, payload) -> str`, in `product/autonomy_contract.py`, used for every hash and fingerprint in this spec: standing-policy document (`policy_version_hash`), individual rule (`rule_hash`), subject policy (`subject_policy_hash`), fill manifest, representation/answer set, answer basis values, observed-attribute fingerprints, grant binding, and the authorization `input_fingerprint`.

- Serialization: UTF-8 JSON with keys sorted lexicographically at every level, no insignificant whitespace (`separators=(",", ":")`), `ensure_ascii=False`, strings NFC-normalized; the envelope is `{"schema": ..., "schema_version": ..., "payload": ...}`.
- Numbers: integers as integers; non-integers rejected in hashed payloads unless the schema declares a decimal-as-string field (no float formatting ambiguity). `NaN`/`Infinity` rejected.
- Collections whose order is semantically irrelevant (e.g. sets of reasons, answer ids) are sorted by the schema's declared key before hashing; ordered collections (e.g. manifest pages) keep order.
- Algorithm: SHA-256, hex-encoded, prefixed `sha256:`.
- Semantically identical inputs must hash identically regardless of key order or formatting; tests enforce this (§17).

### 15.2 File sentinel

A fixed path under the application-data directory (`<data_dir>/AUTONOMY_HALT`), resolved from `Settings`. Presence means halt. It is checked synchronously wherever the kill-switch state is read, including inside the pre-click transaction; a filesystem watcher, if any, is only an optimization.

---

## 16. Module layout

| Module | Responsibility |
|---|---|
| `product/autonomy_contract.py` | Enums, `AuthorizationContext`, `AuthorizationDecision`, reason codes, `ENGINE_VERSION`, canonicalization + fingerprinting |
| `product/autonomy_gate.py` | `evaluate_authorization` (pure) |
| `product/standing_policy.py` | Policy schema/validation, predicate vocabulary, three-valued evaluator |
| `product/semantic_subject_registry.py` + `semantic_subject_policy.v1.json` | Vocabulary (extended) + subject policy loader/validator |
| `product/representation_transforms.py` | Closed transform set with round-trip checks |
| `product/fill_manifest.py` | Manifest schema, validation, hashing |
| `webapp/persistence/autonomy.py` | Table access; atomic operations (grant issue, pre-click transaction, reservations, intents, kill switch) |
| `webapp/services/autonomy.py` | Context assembly, decide-and-record, grant issuance, pre-click commit, kill switch + sentinel, resume, dossier |
| `webapp/api/autonomy.py` + templates | Authority settings, standing-policy editor (validated JSON form in 6B), kill switch, dossier page |

`product/` must not import `webapp/` (existing layering rule).

---

## 17. Testing requirements

**Gate properties** (property-based, e.g. Hypothesis, over generated contexts):

- *Monotonicity:* tightening any input (lower a ceiling, add a restrictive rule, mark an attribute `UNKNOWN`, expire an answer, weaken identity or provenance, engage the kill switch) never raises `effective_capability` and never turns a non-ALLOW into ALLOW.
- *Unknown safety:* replacing any attribute with `UNKNOWN` never raises capability.
- *Order independence:* permuting rules never changes the decision.
- *Determinism:* identical contexts give identical decisions and fingerprints.
- *Authority source:* only `deployment_ceiling`/`account_max`/`workspace_ceiling` can raise the ceiling; no policy document can produce a capability above it; the schema rejects granting effects.
- *Precedence:* every combination of terminal conditions resolves per §9.4; all reasons retained.
- *Relevance:* `REQUIRE_USER` is raised only when resolution could reach the requested stage.

**Policy validation:** rejects unknown attributes/operators, missing `on_unknown`, `NO_EFFECT` on BLOCK rules, granting effects, missing timezone.

**Concurrency** (real SQLite, multiple threads/connections): two workers consuming one SUBMIT grant → exactly one succeeds; two workers taking the last limit slot → exactly one; two intents for one identity → exactly one; kill switch engaged concurrently with a pre-click transaction → either no click (engaged first) or recorded post-commit engagement, never both outcomes lost.

**Lifecycle fault injection** (against the 6B state derivation and pre-click API, with a fake executor): crash at each state; `AUTHORIZED` without dispatch → `EXPIRED_UNCLICKED` and intent release; `CLICK_DISPATCHED` without result → `SUBMISSION_AMBIGUOUS` and no retry; kill switch at each state; sentinel added/removed; resume semantics; grant binding drift at each bound input → revocation.

**Isolation:** shadow and dry-run never write grants, intents, reservations or attempts.

**Ordering:** equal-`created_at` fixtures prove every current-state projection uses `seq`.

**Canonical hashing:** key-order, whitespace and Unicode-normalization permutations of the same payload hash identically; different schema versions of the same payload hash differently; floats/NaN are rejected (property tests).

**Explicit authority:** with no authorization records the effective capability is `NONE` at every stage; the enabling action writes attributed records.

**Answer validity:** a changed basis field makes an answer SUBMIT-ineligible; contradicting current evidence makes it FILL- and SUBMIT-ineligible and raises `REQUIRE_USER`; a stable "no expiry" answer is still invalidated by a basis change.

**Rule acknowledgements:** an edit to an unrelated rule keeps an acknowledgement valid; an edit to the acknowledged rule, or a change in its observed attributes, voids it.

Property-based tests use **Hypothesis**, added to `requirements-dev.txt` in its own dependency commit.

**Dossier:** reconstructs a complete scripted application history exactly.

---

## 18. 6B implementation scope

**In 6B:**

- Pure gate, contract types, standing-policy schema/evaluator, subject policy data and registry extension, representation transforms, fill-manifest schema.
- Migrations and persistence for every §15 table.
- Services: context assembly, decide-and-record (all modes), grant issuance/revocation, pre-click transaction (exercised by tests and a fake executor only), intents including human-path intent creation from existing handoff confirmations and `applied` records, kill switch (UI, endpoint, sentinel), resume all.
- Shadow-mode evaluation hooked to existing workflow points (read-only with respect to the workflow).
- Authority settings, standing-policy editing and kill-switch UI; dossier page.
- All §17 tests.

**Deferred:** scheduler, queue workers, notification delivery, eager propagation execution (6C); page manifests, navigation, filling, bridge health (6D); real submission, proof classification, dry-run execution against live forms, rollout operations (6E).

---

## 19. Open points for the implementation plan

These do not change the contract; the plan must settle them.

1. Exact reason-code catalogue and its stability/versioning rules.
2. How the existing unsupported-claim validation result is surfaced as the pack's grounding input on the legacy document path.
3. Migration of existing `blocker_resolutions` with scope `SEARCH_WORKSPACE`/`CANDIDATE_FACT` into `approved_answers`: proposed **no automatic migration** — existing answers remain valid for Phase 4C consumption, and become reusable for autonomy only after explicit user approval.
4. The standing-policy editor's first form (validated JSON editor vs. structured form); the contract requires only that invalid documents are rejected.
5. Final values of `FILL_SESSION_TTL` / `SUBMIT_GRANT_TTL` / `CLICK_DISPATCH_TTL` (proposed 30 min / 120 s / 60 s).
6. How answer-basis fields are identified for each subject (mapping from subject to candidate snapshot paths), and which subjects default to `USER_ASSERTION`.
7. The v1 freshness values in §6.2 other than "no expiry" for stable facts are proposed baselines (salary and availability deliberately short-lived) and may be tuned in the plan without changing the contract.
