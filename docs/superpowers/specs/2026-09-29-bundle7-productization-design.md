# Bundle 7: Hosted Productization (7A–7H) — master design spec

- **Base:** `bundle6/6e-a-submit@b042e0e` (6E-A frozen, not yet merged). Branch `bundle7/productization`, local only.
- **Starts from:** a local, single-user prototype. It has one configured account (`account_local`), no login, SQLite, profile sources on local disk, an extension pinned to `127.0.0.1:8420`, and an operator-supplied `OPENAI_API_KEY`.
- **Ends at:** a hosted, multi-tenant web application. A brand-new customer can go through sign up → choose/pay for a plan → onboard → add/select CVs → configure preferences and rules → find or capture a job → prepare → review → fill → submit. They never need Python, environment variables, an API key or a local database.
- **Not in scope:** 6E-B autonomous submit; live Greenhouse submission; choosing a hosting provider and building the infrastructure (the release pass does that, §27.2); the recruiter/employer marketplace (room is left for it, §6.6).

This spec is decision-final. Implementation makes no product or architecture decisions. The only open items are the business decision points in §4. Every one of them sits behind a configurable value or a provider-neutral port, so none of them blocks implementation.

## 1. Purpose and principles

Bundle 7 turns a working single-user engine (prepare → review → fill → human submit) into a product that strangers can buy and use safely.

**Principles:**

1. **Hosted by construction.** Production runs on PostgreSQL, object storage, real authentication, operator-funded AI and central entitlements. SQLite and local disk stay for development and tests only. Nothing in production depends on them.
2. **Evolve, don't replace.** `AccountScope`, `OwnedResourceNotFound`, append-only evidence tables, the 6B gate, the 6D approval bindings and the 6E-A submit machinery are kept. Authentication feeds `AccountScope`; it doesn't route around it.
3. **Safety is never premium.** Review, approval, revocation, kill switches, cancellation, ambiguity resolution, data export and account deletion are available on every plan and in every billing state (§11.5).
4. **Plans differ by who takes initiative.**
   - Free: "I do the work; JobSearch organizes it."
   - Pro: "I choose the job; JobSearch helps do the work."
   - Power: "I define what I want; JobSearch continuously runs the search."
5. **Everything commercial is data or a port.** Plan contents, allowances, prices, retention periods and vendors are all configuration or adapters.
6. **Proposals, not silent truth.** Anything parsed or generated about the user is a proposal until the user confirms it.
7. **Exact history.** Every historical application stays bound to the exact CV version and reviewed artefacts it used.
8. **Fail closed.** A missing configuration, an unresolved business value, an unverifiable webhook, an unknown plan state or an exhausted allowance all refuse the action. None of them grants anything.

## 2. Scope

**In scope:**

| Area | Contents |
|---|---|
| 7-core, hosted foundations | Deployment modes; PostgreSQL support; object storage; a profile source store; the tenant table registry; removal of implicit default accounts |
| 7-core, identity | Users, authentication, sessions, CSRF, rate limits, security headers, audit log |
| 7-core, extension | Hosted extension security (§9) and the hosted threat model (§8) |
| 7A | CV Library and CV Strategy |
| 7B | Onboarding, profile, and CV import with confirmed proposals |
| 7C | Admin console, communications (outbox, email port, templates, consent model), announcements |
| 7D | Billing port, subscriptions, plan catalog, entitlements |
| 7E | Preferences, job families, application rules (standing-policy v2) |
| 7F | Notifications and inbox |
| 7G | Usage accounting and quotas, including an internal AI cost ceiling |
| 7H | Operational readiness: jobs and worker, retries, dead letters, deletion/export/purge, health and readiness, metrics, platform controls |
| Journey | UI for the whole release journey (§22), a local-data import tool, a release-readiness tool |

**Non-goals (Bundle 7):**
- 6E-B autonomous submit. Every autonomous SUBMIT entry point keeps raising `SubmissionNotAvailable`.
- Autonomous FILL. The Power automation ceiling is PREPARE (§11.3).
- Live submission on any adapter. Greenhouse stays `FIXTURE_CERTIFIED` with `live_evidence=None` (§8.6).
- Choosing a hosting vendor, provisioning infrastructure, backups, TLS termination, DNS or CDN. These belong to the release pass (§27.2).
- Recruiter/employer accounts and marketplace features.
- Sending marketing messages, and SMS or WhatsApp delivery. Only the consent model and channel column are built.
- SSO/OAuth sign-in (room left: `user_identities.provider`), MFA for customers (MFA is built for staff only), and team accounts.
- Admin access to customer content. Admins see metadata only (§19.4).
- Fixing the presentation of CV Quality v2. `cv_quality_v2_enabled` stays off, and tailoring uses the existing v1 generation path.
- Server-side fetching of arbitrary user-supplied URLs (the SSRF rule, §8.4 T12).
- Row-level security in PostgreSQL. Tenant isolation is enforced in the application and proven by tests (§10.6). RLS is recorded as a later hardening item.
- Converting generated CVs to PDF. Generated and tailored CVs are DOCX; uploaded CVs can be DOCX or PDF.

## 3. Frozen decisions ledger

### 3.1 Hosting and data

| # | Decision |
|---|---|
| H1 | Two deployment modes, set by `JOBSEARCH_DEPLOYMENT` ∈ {`local`, `hosted`} (default `local`). **local** is today's behavior: one configured account, no login, SQLite, filesystem stores. It refuses to start unless bound to a loopback host. **hosted** needs PostgreSQL, the object store, `JOBSEARCH_SECRET_KEY`, an `https` `JOBSEARCH_PUBLIC_ORIGIN`, pinned extension IDs and selected billing/email adapters. It refuses to start on SQLite, the filesystem profile store, or any missing or invalid setting. |
| H2 | PostgreSQL (≥ 15; tested on local 18.1) through psycopg 3, behind one connection adapter (`webapp/persistence/dbapi.py`) with a sqlite3-shaped API. The ~430 existing `conn.execute` call sites keep their SQL written with `?` placeholders. The adapter translates placeholders, escapes literal `%`, maps rows (index access and key access) and maps errors to a shared `dbapi.IntegrityError` / `dbapi.OperationalError`. |
| H3 | Transaction semantics are kept exactly. Every `BEGIN IMMEDIATE` block (31 sites) gets, on PostgreSQL, `BEGIN ISOLATION LEVEL REPEATABLE READ` plus a transaction-scoped global writer advisory lock. That reproduces SQLite's single-writer guarantee. Other transactions run at REPEATABLE READ. A serialization failure raises `dbapi.DatabaseBusy`: HTTP maps it to 503 with `Retry-After`, and workers retry it. A per-account lock is a recorded later scalability item. |
| H4 | Timestamps stay ISO-8601 UTC `TEXT` in both dialects. Autoincrement `seq` columns become `BIGINT GENERATED ALWAYS AS IDENTITY`. Append-only `RAISE(ABORT)` triggers become PL/pgSQL trigger functions with the same messages. Nothing in the domain SQL changes type. |
| H5 | Migrations: the SQLite chain `001`–`021` stays as it is, for dev databases. PostgreSQL starts from `pg/0001_baseline.sql`, which equals the post-`021` schema. Every Bundle 7 migration (`022`+) is written once with a `sqlite` body and a `postgres` body in one module. The schema-parity test (§10.3) fails on any structural difference between dialects. |
| H6 | Document bytes go through an `ObjectStore` port (`put`/`get`/`delete`/`exists`, integrity-verified), with `LocalFsObjectStore` for local mode and tests, and `S3CompatibleObjectStore` for hosted. Keys are tenant-prefixed: `accounts/{account_id}/documents/sha256/{aa}/{digest}.{ext}`. Content is never deduplicated across tenants. |
| H7 | The Markdown evidence profile sources go through a `ProfileSourceStore` port: filesystem in local mode (unchanged), database-backed (`profile_source_revisions`) in hosted mode. `build_snapshot` takes a source reader, not a root path. The Markdown format stays the canonical evidence format. |
| H8 | Every table is classified in a tenant table registry (`webapp/persistence/tenancy.py`) by owner path, purge class and export class. A test fails if any table in either dialect is unclassified (§10.5). |
| H9 | Persistence and service functions no longer default `account_id=DEFAULT_ACCOUNT_ID` (73 sites) or `search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID` (27 sites). These become required keyword arguments. A hosted database never contains `account_local`. |

### 3.2 Identity and access

| # | Decision |
|---|---|
| A1 | `users` are login identities, `accounts` are tenants and billing owners, and `account_memberships(user, account, role)` connects them. In Bundle 7 every customer account has `kind='candidate'` and exactly one `OWNER` membership. |
| A2 | Password authentication only: argon2id (`argon2-cffi`); length 12–128; rejected if on the bundled top-10,000 common-password list. `user_identities(provider='password')` leaves room for SSO. |
| A3 | Email verification is required before any AI use, plan checkout, extension pairing or document upload. Before verification a user can only see the dashboard shell, resend verification, and edit or delete their account. |
| A4 | Web sessions are server-side and opaque, in the `__Host-js_session` cookie (Secure, HttpOnly, SameSite=Lax, Path=/). The id is rotated at login and privilege change. Customer sessions: idle 7 days, absolute 30 days. Staff sessions: idle 30 minutes, absolute 8 hours. |
| A5 | CSRF: a synchronizer token per session is required on every unsafe method from a cookie-authenticated request (header `X-CSRF-Token` or form field), plus an `Origin`/`Referer` check against `JOBSEARCH_PUBLIC_ORIGIN`. Bearer-authenticated extension routes carry no cookies and are exempt. |
| A6 | Platform staff are dedicated `users` with append-only `platform_role_assignments` ∈ {`SUPPORT`, `OPERATIONS`, `BILLING`, `ADMIN`}. Staff users own no customer account. Staff sign-in requires TOTP (`pyotp`), and destructive admin actions require re-authentication within 5 minutes. The first admin is created by CLI. |
| A7 | Routes fall into five auth classes: PUBLIC, USER, EXTENSION, ADMIN, WEBHOOK (§21.1). Each router declares exactly one. An enumerating test fails on any undeclared route. |
| A8 | Rate limits live in the database (`rate_limit_buckets`, fixed window, correct across instances). The limits are technical, not business: signup 5/h/IP; login 10/15 min/IP+email; password reset 5/h/email; verification resend 5/h/user; pairing code 10/h/user; extension token refresh 60/h/device; AI-starting routes 30/min/account. |

### 3.3 Extension

| # | Decision |
|---|---|
| X1 | The extension's backend origin is a build-time constant. The dev build uses `http://127.0.0.1:8420`. The hosted build requires `--origin https://…`, and its manifest `host_permissions` become exactly `[origin/*]`. Permissions don't otherwise change from `b042e0e`: `storage`, `activeTab`, `scripting`, `declarativeNetRequest`; optional `tabs`, `webNavigation`. |
| X2 | The durable `X-Handoff-Credential` is replaced in both modes by a device credential: an access token (10 minutes, bearer) plus a rotating refresh token (30 days sliding, hashed at rest, one rotation family per device). Reusing a rotated refresh token revokes the whole family and notifies the user. |
| X3 | Pairing is started from a signed-in, verified web session. It mints a one-time code (10 minutes, single use, bound to user and account). The exchange creates an `extension_devices` row and returns the tokens. |
| X4 | Web → extension handoff carries a server-signed `handoff_ticket` (HMAC-SHA256 with the secret key; 5 minutes; single use; bound to user, account, workspace and purpose). The server refuses a ticket whose user differs from the bearer token's user (`ACCOUNT_MISMATCH`). |
| X5 | Extension persistence: `chrome.storage.local` holds only the device id, the refresh token, a masked account label and the executor instance id. `chrome.storage.session` holds the access token and active run records. CV bytes, profile content and page content are never persisted. Unpair, logout-everywhere or an account switch wipes all keys after the existing recovery path has restored TOTAL for any active run. |
| X6 | CORS: the API sends no permissive CORS headers. Extension requests are accepted only when the `Origin` is `chrome-extension://<id>` for an id in `JOBSEARCH_EXTENSION_IDS`, or absent (service-worker fetch). Every extension route authenticates by bearer token regardless. |

### 3.4 Commercial model

| # | Decision |
|---|---|
| B1 | A `BillingProvider` port (§12.2), with `FakeBillingProvider` (a local checkout page, used in dev and tests) and one real adapter chosen at DP-1. Prices live in the provider. The catalog maps plan × interval to provider price ids. |
| B2 | Subscription state is derived from a **provider snapshot**, fetched on every webhook event. Webhook events are only triggers. They are stored raw first, idempotent by provider event id, and processed by the worker. |
| B3 | Upgrades take effect immediately (the provider handles proration). Downgrades and cancellations take effect at period end. A payment failure gives `PAST_DUE` with a grace period (DP-4), after which the account's entitlements become Free. **No billing state ever locks the workspace or deletes data.** |
| E1 | Entitlements come from a versioned plan catalog (`product/plans/plan-catalog.v1.json` + schema) and are resolved by a pure function (§11.4). Entitlement grants can only add, and deployment ceilings and platform controls can only reduce. |
| E2 | Every allowance is finite. The catalog schema forbids unlimited values. An allowance whose business value is unresolved is `null`, meaning **refused (0)** in every mode. The release-readiness tool fails while any is `null` (§27.3). |
| U1 | Usage is a reservation ledger (`usage_reservations`): reserve before work, consume on success, release on system or provider failure, auto-release when expired. A window's usage is the sum over reservations in that window, so there are no resettable counters. |
| U2 | Windows: a paid plan uses the subscription's current period from the provider snapshot; Free uses the UTC calendar month. |
| U3 | A hidden per-account AI cost ceiling per window (`ai.cost_micro_usd`) protects operator spend underneath the user-facing allowances. |

### 3.5 Product surfaces

| # | Decision |
|---|---|
| L1 | CV Library: named `cv_library_items` holding immutable, append-only `cv_library_versions`, each pointing at an `application_document_versions` row. Tailored variants are versions of their source item with `library_visible = 0`. They never become an item's "latest". |
| L2 | CV Strategy (`cv-strategy.v1`, versioned, account-level): job family → rule `FIXED_VERSION` \| `LATEST_VERSION` \| `TAILOR_FROM`, plus one mandatory default rule. The resolution is recorded per application (`application_cv_resolutions`). The user can always override at review. |
| L3 | A document version referenced by any approval, fill run or submission can't be deleted or archived away. Only account purge removes it (trigger-enforced). |
| P1 | Onboarding writes only through existing validated paths: evidence-profile entries via `profile_manager`, reusable answers via `approve_answer` + `answer_confirmations`, and search preferences via `user-profile`. |
| P2 | CV import produces `profile_proposals`. Nothing reaches the profile or the answers until the user accepts or edits each proposal. |
| R1 | **Preferences** are what I want (soft: ranking and discovery filters), stored in `user-profile.v2`. **Rules** are what I never or always accept (restrictive), stored in `standing-policy.v2`. A salary *floor* is a rule. A *desired* salary is a preference. |
| R2 | Rules apply in two ways. In automated (Power) screening and prepare they're enforced with their stated effect. In manual flows a `BLOCK` rule becomes an advisory the user must acknowledge (existing `rule_acknowledgements`), and `REDUCE_TO` / `REQUIRE_USER` don't apply because the human is already acting. |
| N1 | Notifications form one event log (`notifications`), written in the same transaction as the event that causes them. The inbox's "Action required" list is derived live from source state. Email fan-out goes through the transactional outbox, following the category rules (§17.3). |
| C1 | Communications: a transactional outbox (`outbound_messages`) → dispatcher → `EmailProvider` port (`ConsoleEmailProvider`, `SmtpEmailProvider` (generic), one vendor adapter at DP-2). Categories are `SERVICE` (always sent), `PRODUCT` (by preference) and `MARKETING` (vocabulary only, refused without a consent record, with no producers in Bundle 7). |
| O1 | Background work runs in a separate worker process (`python -m webapp.worker`) over a durable `jobs` table with leases, retries with backoff, and dead letters. The 6C autonomy driver runs inside the worker in hosted mode, never in the web process. |
| O2 | Account deletion has four stages: access terminated immediately → cooling-off (DP-4, may be 0) → purge → tombstone. Retained records are pseudonymized per retention class (§20.4). |

## 4. Business decision points (open; configurable; non-blocking)

Each of these is an explicit configuration value or adapter choice. Implementation builds the mechanism and ships dev/test values. The production values are resolved before the release gate. `python -m webapp.tools.release_readiness` fails until they are.

| DP | Decision | Mechanism in Bundle 7 | Safe default until resolved |
|---|---|---|---|
| DP-1 | Payment provider | `BillingProvider` port; `FakeBillingProvider` | Hosted mode refuses paid checkout (Free still works) |
| DP-2 | Transactional email provider | `EmailProvider` port; `SmtpEmailProvider` works with most vendors | Hosted mode refuses to start without a configured provider (verification mail is required) |
| DP-3 | Plan prices and numerical allowances, including the AI cost ceilings and Free's allowances (which may be 0) | `plan-catalog.v1.json`, validated | `null` → refused |
| DP-4 | Retention and timing: payment grace period, deletion cooling-off, how long billing/financial records, security audit records and consent records are retained, data-export link lifetime | `retention-policy.v1.json` | Grace 0 days; cooling-off 0 days; retained-record periods `null` → the purge keeps retained classes and flags them for review, never destroying them early |
| DP-5 | Legal copy (Terms, Privacy Notice) and its versions | `legal_documents` registry plus acceptance records | Hosted sign-up refuses while no published version exists |
| DP-6 | Trial policy (none, or N days of a paid plan) | Catalog `trial_days` per plan (`TRIALING` state supported) | 0 |
| DP-7 | Refunds on cancellation or deletion | Provider-side; the port exposes `cancel(immediately, refund=False)` | No refunds issued by the app |
| DP-8 | Grandfathering when the catalog changes | Subscriptions pin `catalog_version`; operator-run migration command | Pinned (existing subscribers keep their version) |
| DP-9 | Discovery sources enabled in hosted production (e.g. whether portal-CLI sources such as LinkedIn may run server-side) | Operator-level `discovery_source_settings` plus per-plan feature | All portal-CLI sources disabled in hosted mode; manual capture always available |
| DP-10 | Production domain and extension store listing | Build `--origin`; `JOBSEARCH_EXTENSION_IDS` | Dev origin only |

## 5. Target architecture

```
Customer browser ──HTTPS──► JobSearch web (N instances, FastAPI)
      │                          │  ├─ PostgreSQL (all state; append-only evidence)
      │                          │  ├─ ObjectStore (S3-compatible; tenant-prefixed)
      │                          │  ├─ AI providers (operator key; metered)
      │                          │  ├─ BillingProvider  (checkout, portal, snapshot)
      │                          │  └─ EmailProvider    (via outbox only)
      │                          ▲
      │                          │ webhooks (billing, email) — signature-verified
      │                 JobSearch worker (1..N): jobs, outbox, purge, webhook
      │                 processing, scheduled discovery, 6C autonomy driver
      ▼
Browser extension ──HTTPS bearer──► JobSearch web (/api/ext/*, /api/handoff/*, fill/submit ext routes)
      │
      └──► employer / ATS pages (6D-B quarantine, 6E-A certified egress — unchanged)
```

**Ports and adapters.** Each port has a fake or local adapter used by the tests and the journey suite.

| Port | Local / test | Hosted |
|---|---|---|
| `dbapi` dialect | SQLite | PostgreSQL |
| `ObjectStore` | `LocalFsObjectStore` | `S3CompatibleObjectStore` |
| `ProfileSourceStore` | `FilesystemProfileSourceStore` | `DatabaseProfileSourceStore` |
| `BillingProvider` | `FakeBillingProvider` | DP-1 adapter |
| `EmailProvider` | `ConsoleEmailProvider` (writes to the outbox log) | `SmtpEmailProvider` / DP-2 adapter |
| AI providers | existing fakes | existing OpenAI providers, wrapped in `MeteredProvider` |
| `ErrorReporter` | log-only | log-only (a vendor adapter is a release-pass choice) |
| `Clock` | injectable | system |

**Processes.** Web handles HTTP only; it runs no background threads in hosted mode, and `_start_autonomy_driver` is disabled when `deployment=hosted`. The worker runs a single loop over job kinds (§20.2). Both processes share `webapp.config.Settings`.

## 6. Tenancy, identity and authorization model

### 6.1 Tables (migration `022_identity`)

- **`users`** (id, email_normalized UNIQUE, email_display, display_name, status, email_verified_at, created_at, updated_at, last_login_at, password_changed_at)
  - `status` ∈ {`PENDING_VERIFICATION`, `ACTIVE`, `SUSPENDED`, `DELETION_REQUESTED`, `PURGED`}.
- **`user_identities`** (id, user_id, provider, secret_hash, created_at, revoked_at)
  - `provider` ∈ {`password`}.
- **`accounts`** gains: `kind` ∈ {`candidate`, `local`} (`local` only for `account_local`), `status` (∈ {`ACTIVE`, `SUSPENDED`, `DELETION_REQUESTED`, `PURGED`}), `updated_at`.
- **`account_memberships`** (account_id, user_id, role, created_at, revoked_at), with UNIQUE (account_id, user_id).
  - `role` ∈ {`OWNER`} (vocabulary room: `MEMBER`, `RECRUITER`).
- **`web_sessions`** (id_hash PK, user_id, kind, csrf_token_hash, created_at, last_seen_at, idle_expires_at, absolute_expires_at, revoked_at, revoke_reason, ip_hash, user_agent_summary)
  - `kind` ∈ {`CUSTOMER`, `STAFF`}.
- **`email_tokens`** (id, user_id, purpose, token_hash, created_at, expires_at, consumed_at)
  - `purpose` ∈ {`VERIFY_EMAIL`, `PASSWORD_RESET`, `EMAIL_CHANGE`}.
  - Lifetimes: VERIFY 48 h, RESET 1 h, CHANGE 24 h.
- **`platform_role_assignments`** (seq, id, user_id, role, action, actor_user_id, reason, created_at). Append-only; `action` ∈ {`GRANT`, `REVOKE`}.
- **`staff_totp`** (user_id PK, secret_encrypted, confirmed_at). The secret is encrypted with a key derived from `JOBSEARCH_SECRET_KEY`.
- **`legal_documents`** (id, kind, version, published_at, content_sha256).
- **`legal_acceptances`** (seq, id, user_id, legal_document_id, accepted_at, ip_hash). Append-only.
- **`rate_limit_buckets`** (key, window_start, count), with PK (key, window_start).

### 6.2 Resolution

`get_account_scope` becomes mode-aware:
- **local:** unchanged. It uses the configured account.
- **hosted:** session → user (status `ACTIVE`, email verified if the route requires it) → the OWNER membership → account (status `ACTIVE`) → `AccountScope(account_id, profile_store, object_store)`.

Any failure is `401` (no session) or `403 ACCOUNT_UNAVAILABLE`. It never falls back to another account.

`AccountScope` gains `user_id` and ports instead of `profile_root: Path`. `profile_root` survives only inside `FilesystemProfileSourceStore`.

### 6.3 Account creation

Sign-up creates, in one transaction:
- the user;
- the password identity;
- the account;
- the OWNER membership;
- the profile workspace (`ensure_profile_workspace`);
- the default search workspace;
- the initial profile source (`render_basic_profile({name})`);
- the default `standing-policy.v2`;
- the default `cv-strategy.v1` (default rule `LATEST_VERSION`, with no item yet → `NEEDS_USER_CHOICE`);
- the legal acceptances;
- a `VERIFY_EMAIL` token;
- an outbox message.

### 6.4 Account and user lifecycle

`PENDING_VERIFICATION` → `ACTIVE` ⇄ `SUSPENDED` (staff only) → `DELETION_REQUESTED` → `PURGED` (§20.4).

Suspension revokes every web session and extension device, pauses the account's queue items and scheduled searches, and makes `get_account_scope` return `403 ACCOUNT_SUSPENDED`. Data export and a deletion request stay reachable from a restricted page (§11.5).

### 6.5 Local mode

`account_local` keeps working with no users rows. A synthetic `user_id = "user_local"` exists only in memory, and USER routes skip the session. The pairing and bearer-token flow (X2–X3) is the same in both modes.

### 6.6 Marketplace room (nothing built)

- `accounts.kind` and `account_memberships.role` are open vocabularies at the schema level: CHECK lists extended by a later migration.
- Candidate data is keyed by account, never by user, so an employer tenant can be added as another account kind without moving candidate data.
- Cross-account sharing, meaning a candidate granting a recruiter access, would be a new explicit grant table. No Bundle 7 table assumes it.

## 7. Authentication and web sessions

- **Endpoints (PUBLIC, rate-limited):**
  - `POST /auth/signup` {email, password, display_name, accepted_legal_ids[]}
  - `POST /auth/login`
  - `POST /auth/logout`
  - `POST /auth/verify-email` {token}
  - `POST /auth/resend-verification`
  - `POST /auth/password-reset/request`
  - `POST /auth/password-reset/confirm` {token, new_password}
  - Pages: `/signup`, `/login`, `/verify-email`, `/reset-password`, `/pricing`.
- **Enumeration resistance:** signup, reset requests and resend return the same response and the same timing class whether or not the email exists. Signing up with an existing email sends an "account already exists" service email instead.
- **Login:**
  - An argon2id verify runs even for unknown emails (a dummy hash).
  - On success the session id is rotated, `last_login_at` updated and a `LOGIN_SUCCEEDED` audit row written.
  - Failures are audited as `LOGIN_FAILED` with the email hash only.
  - Progressive backoff comes from the rate limiter; there's no hard account lockout, which avoids a denial-of-service vector.
- **Password change** (`POST /settings/password`, requires the current password):
  - revokes all other sessions and all extension devices;
  - sends the `auth.password_changed` service email.
- **Password reset:** consumes the token, revokes all sessions and devices, and logs the user in fresh.
- **Email change:** a confirmation link goes to the new address, and a notice goes to the old one. The change applies only once the new address is confirmed.
- **Sign out everywhere** (`POST /settings/sessions/revoke-all`): revokes every session and device.
- **Staff login** (`/admin/login`): password + TOTP. TOTP enrolment is forced on first login. A staff session is kind `STAFF` and is never accepted on USER routes, and a customer session is never accepted on ADMIN routes.

## 8. Hosted threat model (supersedes 6B §16 "local, single-user" for production)

### 8.1 Assets

- Candidate content: CVs, the evidence profile, answers, applications, reviewed snapshots, submission evidence.
- Credentials: passwords, sessions, device tokens, pairing codes, handoff tickets, staff TOTP.
- Authority: approvals, fill and submit grants, human submit authorizations.
- Money: subscriptions, and operator AI spend.
- Integrity of the audit and evidence history.

### 8.2 Adversaries

| # | Adversary |
|---|---|
| ADV1 | Another tenant |
| ADV2 | An anonymous internet attacker |
| ADV3 | A malicious or compromised employer or job page (already modelled in 6D-B and 6E-A) |
| ADV4 | Someone holding a stolen device token or session |
| ADV5 | A malicious extension-storage reader on the user's machine |
| ADV6 | A forged webhook sender |
| ADV7 | A malicious or careless staff member |
| ADV8 | The user themselves, forging executor telemetry |
| ADV9 | A credential-stuffing bot |

### 8.3 Trust boundaries

browser ↔ web; extension ↔ web; extension ↔ employer page; web ↔ providers (AI, billing, email); worker ↔ database; staff ↔ admin console.

### 8.4 Threats and mitigations

| T | Threat | Mitigation | Residual |
|---|---|---|---|
| T1 | Cross-tenant read or write through a guessed id (ADV1) | Every user and extension route resolves `AccountScope` from credentials. Persistence queries are scoped by account, and a missing resource and a cross-owner resource return the same result. The cross-tenant harness (§10.6) probes every route. The tenant registry covers every table. | Application-level only; RLS deferred (§2) |
| T2 | Session theft or fixation (ADV4) | `__Host-` cookie; Secure, HttpOnly, SameSite=Lax; rotation at login; idle and absolute expiry; "sign out everywhere"; revocation on password change | A token lives until it expires or is revoked |
| T3 | CSRF (ADV2) | Synchronizer token plus Origin check (A5); SameSite=Lax | — |
| T4 | XSS in the web app (ADV2/3, via job text) | Jinja autoescape everywhere; strict CSP with per-request nonces (`script-src 'nonce-…'`; no `unsafe-inline`); inline handlers removed; job and posting text only ever rendered escaped | — |
| T5 | Device token theft from extension storage (ADV5) | 10-minute access tokens only in session storage; refresh-token rotation with reuse detection (X2); a device list with revoke; a new-device service email; server-side revocation on password change, suspension and deletion | A refresh token read at rest is usable until its first rotation by the real device, or until revoked |
| T6 | Account confusion between web and extension | Handoff ticket bound to user (X4); the popup shows the paired account; the web page shows a mismatch banner | — |
| T7 | Malicious employer page (ADV3) | Unchanged 6D-B quarantine, TOTAL egress rules and siblings containment, and the 6E-A certified egress. Nothing about the backend origin reaches employer pages: host permissions cover the backend origin only, and handoff documents are fetched by the service worker. | As in 6D-B/6E-A |
| T8 | Forged billing or email webhooks (ADV6) | Signature verification per adapter with a timestamp tolerance of 5 minutes; raw storage idempotent by event id; state always re-derived from a provider snapshot fetch (B2), so a forged event can't set state | — |
| T9 | Staff abuse (ADV7) | Least-privilege roles; TOTP; re-authentication for destructive actions; no content access (§19.4); every staff action audited and visible to ADMIN; staff can't read tokens or secrets | An ADMIN can suspend or delete (audited) |
| T10 | A user forging telemetry (ADV8) | The consequences stay inside that user's own account. Metering is server-side at the AI boundary, so forged executor telemetry can't bypass allowances. Submission-outcome evidence only affects the user's own records. The 6E-A result rules are unchanged. | This supersedes the 6B assumption: forging is no longer assumed away, it is contained |
| T11 | Credential stuffing, enumeration (ADV9/2) | Rate limits (A8); dummy-hash timing; uniform responses; the common-password list | No breached-password API (external dependency deferred) |
| T12 | SSRF | The server never fetches user-supplied URLs. Job capture is paste (text plus an optional URL stored as data) or discovery sources. Portal CLIs run only for operator-enabled sources (DP-9), with fixed targets, timeouts and output-size limits. | — |
| T13 | Resource and cost abuse | Plan allowances plus the hidden AI cost ceiling (U3); per-account rate limits on AI-starting routes; the platform AI halt control | — |
| T14 | Replay of pairing codes, tickets or grants | Single-use and short TTL for codes and tickets; 6B grant nonces and one-shot submit authority unchanged | — |
| T15 | Data remanence after deletion | Staged purge including object-store keys, and pseudonymized retained classes (§20.4) | Backups: release pass (§27.2) |
| T16 | Secrets exposure | Secrets only in environment/secret manager (release pass). Tokens and codes are stored hashed (SHA-256 of 256-bit random values). The TOTP secret is encrypted. Logs redact the `Authorization`, cookie and token fields. | — |

### 8.5 Preserved Bundle 6 semantics (unchanged by hosting)

Human review and authorization; exact reviewed-snapshot binding (`review_hash`, document `document_version_id` + `sha256`); pre-click revalidation; one-shot authorization and grant; `CLICK_DISPATCHED` before the physical click; confirmed / ambiguous / proven-failure result rules; CAPTCHA and challenge handoff to the human; fail-closed on every unverifiable step; the 6D-B TOTAL quarantine and allowlist; the 6E-A `submission-result.v1` contract as frozen at `b042e0e`.

### 8.6 Live submission gate

In hosted mode, `human_submit_ceiling()` returns `FILL` unless **all** of these hold:
1. `JOBSEARCH_HUMAN_SUBMIT_ENABLED=1`;
2. platform control `SUBMIT_ENABLED` is on;
3. the target adapter's submit certification is `LIVE_CERTIFIED` with non-null `live_evidence`;
4. the operator has recorded the hosted threat-model sign-off (`platform_controls` `HOSTED_THREAT_MODEL_SIGNED_OFF`).

Greenhouse stays `FIXTURE_CERTIFIED`, so hosted production submits nothing in Bundle 7. The journey suite proves submit against fixture origins in local mode, where `submit_fixture_origins_enabled` already requires loopback.

## 9. Extension ↔ backend security design

### 9.1 Tables (migration `023_extension_devices`)

- **`extension_devices`** (id, account_id, user_id, label, created_at, last_seen_at, revoked_at, revoke_reason).
- **`extension_refresh_tokens`** (id, device_id, family_id, token_hash, issued_at, expires_at, rotated_at, revoked_at). Unique on token_hash.
- **`extension_access_tokens`** (token_hash PK, device_id, expires_at). Kept so tokens can be revoked; the worker sweeps expired rows.
- **`pairing_codes`** replaces the use of `pairing_secrets`: (id, account_id, user_id, code_hash, created_at, expires_at, consumed_at).
- **`handoff_tickets`** (id, account_id, user_id, workspace_id, purpose, nonce_hash, expires_at, consumed_at).

Legacy `extension_credentials` and `pairing_secrets` rows are revoked by the migration. Every existing paired extension has to pair again once; the release notes say so.

### 9.2 Flows

**Pair:**
- The web page `/settings/extension` (USER, verified) calls `POST /api/ext/pairing-codes`. The response carries the code and its expiry.
- The user types the code into the extension popup's existing pairing form.
- The extension calls `POST /api/ext/pair` {code, device_label}.
- The response carries {device_id, access_token, access_expires_at, refresh_token, account_label}.
- The server writes the audit row `EXTENSION_DEVICE_PAIRED` and sends the service email `security.new_device_paired`.

**Refresh:** `POST /api/ext/token` {device_id, refresh_token}.
- On success the old refresh token is marked rotated, and a new access/refresh pair is returned.
- If the presented token was already rotated, the whole family and the device are revoked (audit `EXTENSION_TOKEN_REUSE`, security notification), and the call returns 401 `DEVICE_REVOKED`.

**Call:**
- Every extension route requires `Authorization: Bearer <access>`.
- The route resolves device → user and account, and the account must be `ACTIVE` and the user's email verified.
- The result is the same `AccountScope` a web route would get, plus `device_id`.
- The 6D-B/6E-A run, session and executor checks are unchanged on top of this.

**Handoff:**
- The web page embeds a `handoff_ticket` in the pending context it passes through the content bridge.
- The extension presents the ticket on `POST /api/handoff/sessions`.
- The server checks the ticket's HMAC, expiry, single use, and `ticket.user_id == token.user_id` and `ticket.account_id == token.account_id`. Any failure refuses with `ACCOUNT_MISMATCH` / `TICKET_INVALID`.

**Unpair / logout / switch:**
- Popup "Sign out" calls `POST /api/ext/devices/self/revoke`, then wipes local state.
- A revoked device gets 401 `DEVICE_REVOKED` on its next call. The extension then runs `recoverSubmitAfterRestart`-style cleanup: restore TOTAL, abort active runs, report `EXECUTOR_RESTARTED` where possible. After that it wipes storage and returns to the pairing form.
- Pairing a different account follows the same path first.

**Content bridge:**
- The bridge accepts messages only from `window.location.origin == BACKEND_ORIGIN`, the build constant.
- It forwards only the closed `pending-context` message type.
- It validates the ticket's shape before forwarding.

### 9.3 Tests

Vitest: token client rotation, reuse handling, storage hygiene (an exact key allowlist per storage area), and bridge origin refusal. Python: pairing, refresh, reuse detection, ticket binding, and revocation cascade. Browser: one pairing plus handoff run in local mode, which uses the same code path.

## 10. Data architecture

### 10.1 Connection adapter (`webapp/persistence/dbapi.py`)

- `connect(settings)` returns a `Connection` with `execute(sql, params=())`, `executemany`, `commit`, `rollback`, `close`, `row_factory`-equivalent rows (`row["col"]`, `row[0]`, `dict(row)`, `keys()`), and `dialect` ∈ {`sqlite`, `postgres`}.
- SQL is written in the portable subset (§10.2) with `?` placeholders. The PostgreSQL path translates `?` → `%s` outside string literals and escapes literal `%` → `%%`.
- Statements `BEGIN IMMEDIATE`, `BEGIN`, `COMMIT` and `ROLLBACK` are intercepted. `BEGIN IMMEDIATE` becomes (H3) `BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT pg_advisory_xact_lock(<WRITER_LOCK_KEY>)`.
- `PRAGMA …` statements are refused on PostgreSQL, except the known no-op set (`foreign_keys`, `journal_mode`, `busy_timeout`), which is ignored there.
- `lock_account(conn, account_id)`: PostgreSQL takes `pg_advisory_xact_lock(hash)`; SQLite is a no-op, since the caller is already inside `BEGIN IMMEDIATE`.
- Errors: `dbapi.IntegrityError`, `dbapi.OperationalError`, `dbapi.DatabaseBusy` (PostgreSQL serialization failure or deadlock; SQLite `database is locked` after the busy timeout).
- `sqlite3.Connection` type hints and `sqlite3.IntegrityError` / `sqlite3.Row` references across `webapp/` are codemodded to `dbapi.*`. The SQLite path still returns real `sqlite3` objects wrapped thinly, so behavior is identical.

### 10.2 Portable SQL subset (enforced by a lint test over `webapp/**/*.py` string literals)

**Refused:**
- `INSERT OR IGNORE`, `INSERT OR REPLACE`: use `INSERT … ON CONFLICT (…) DO NOTHING` / `DO UPDATE SET …`, supported by SQLite ≥ 3.24.
- `json_extract`, `json_each`: do JSON work in Python.
- `strftime`, `julianday`, `datetime('now')`: timestamps come from Python.
- `lastrowid`: use `RETURNING`, supported by SQLite ≥ 3.35.
- `executescript` outside `migrations.py` / `db.py`.
- `AUTOINCREMENT` outside the SQLite migration bodies.
- `GLOB`, and any `LIKE` whose correctness depends on case folding. SQLite's `LIKE` is ASCII case-insensitive and PostgreSQL's is case-sensitive, so write `lower(col) LIKE lower(?)` explicitly.

**Allowed:** `ON CONFLICT`, `RETURNING`, `COALESCE`, `CAST(x AS INTEGER)`, `LIMIT/OFFSET`, standard joins, `EXISTS`, and partial indexes (both dialects support `CREATE INDEX … WHERE`).

The 13 existing non-migration occurrences are rewritten in Task 2.

### 10.3 Migrations and schema parity

- `init_db(settings)` dispatches:
  - **SQLite:** `schema.sql` plus the chain, as today.
  - **PostgreSQL:** `pg/0001_baseline.sql` if the schema is empty, then the Bundle 7 chain.
- The parity test runs when a PostgreSQL DSN is available: `JOBSEARCH_TEST_PG_DSN`, default `postgresql://postgres@localhost:5432/postgres`, skipped with a visible reason if it's unreachable. It builds both schemas and compares, per table:
  - column names, order and nullability;
  - primary keys;
  - unique constraints and indexes, including partial predicates normalized;
  - foreign keys;
  - CHECK vocabularies, extracted as value sets;
  - the set of append-only triggers, by table and event.
- Type affinities are compared through a mapping table (`TEXT`↔`text`, `INTEGER`↔`bigint|integer`, `REAL`↔`double precision`, `BLOB`↔`bytea`).

### 10.4 Object store and profile source store

- **`ObjectStore`:**
  - `put(account_id, content: bytes, media_type)` → {storage_key, byte_length, sha256}; content-addressed per tenant; no clobber.
  - `get(record)` verifies length and sha256.
  - `delete(storage_key)` is used only by purge.
- **`DocumentBlobStore`** becomes a thin wrapper, with its existing no-clobber and integrity semantics kept.
- **Media types:** `application/vnd.openxmlformats-officedocument.wordprocessingml.document` (`.docx`, validated by `validate_docx_package`) and `application/pdf` (`.pdf`, validated by `%PDF-` magic plus a `pypdf` parse).
- **Size:** ≤ 10 MiB per upload.
- **Existing keys** (`sha256/aa/digest.docx`) stay readable in local mode. The import tool (§23.3) rewrites them into the tenant-prefixed layout.
- **`ProfileSourceStore`:**
  - `read(account_id, source_path) → text | None`;
  - `write(account_id, source_path, text, expected_revision)` → revision;
  - `list_included(account_id)`.
- **Database backend:** `profile_source_revisions` (seq, id, account_id, source_path, revision, content, sha256, created_at). Append-only; the current revision is the max `revision`.
- `profile_manager`, `profile_setup` and `pipeline.refresh_profile` call the store. `build_snapshot(reader, included_sources)` gets its file text from the reader.

### 10.5 Tenant table registry

`TENANT_TABLES: dict[str, TableSpec]`. Each `TableSpec` holds:
- `owner`: `account_id` column | a parent path like `("fill_runs", "fill_run_id")` | `GLOBAL`;
- `purge`: `DELETE` | `PSEUDONYMIZE` (with column list) | `RETAIN` (class) | `GLOBAL`;
- `export`: include/exclude.

It covers all 97 current tables and every new table. The registry drives purge (§20.4), export (§20.5), the import tool (§23.3) and the isolation harness.

`GLOBAL` tables: `schema_migrations`, `discovery_source_settings`, `legal_documents`, `platform_controls`, `plan_catalog_versions`, `rate_limit_buckets`, `jobs` (rows carry an optional account_id, which purge honours).

`user_profile_versions` gains `account_id` (migration `022`; backfilled through `search_workspace_user_profile_history`). The legacy singleton `current_user_profile` is classified `RETAIN`-local-only and is never read in hosted mode.

### 10.6 Cross-tenant isolation harness

A pytest module:
1. builds two accounts, A and B, each with one full object graph through the fixture factories: search workspace, job workspace, artifacts, documents, CV items, approvals, a fill run, a submit attempt, notifications and devices;
2. enumerates every USER and EXTENSION route from `app.routes`;
3. substitutes A's ids into the path and body while authenticated as B.

It asserts 404/403 with no A data in the response body and no writes to A's rows (a row-hash snapshot is taken before and after). Routes without id parameters are covered by B's own data only. New routes are covered automatically. Per-route opt-outs need a reason string, and the test fails if more than 5 exist.

## 11. Plans, capability matrix and entitlements (7D core)

### 11.1 Catalog (`product/plans/plan-catalog.v1.json`, schema `plan-catalog.v1`)

```json
{
  "schema_version": "plan-catalog.v1",
  "catalog_version": "2026-10.1",
  "plans": {
    "free":  {"display_name": "Free",  "rank": 0, "trial_days": 0, "features": {...}, "allowances": {...}, "provider_prices": {}},
    "pro":   {"display_name": "Pro",   "rank": 1, "trial_days": null, "features": {...}, "allowances": {...}, "provider_prices": {"month": null, "year": null}},
    "power": {"display_name": "Power", "rank": 2, "trial_days": null, "features": {...}, "allowances": {...}, "provider_prices": {"month": null, "year": null}}
  }
}
```

- The catalog is loaded, validated and hashed at startup. Its hash is recorded in `plan_catalog_versions` (append-only).
- `plan-catalog.dev.json` holds the values used in tests. Hosted mode refuses to load the dev catalog.
- The validator requires:
  - every feature key in `FEATURES` present as a boolean;
  - every allowance key in `ALLOWANCES` present as `{limit: int ≥ 0 | null, window: "period"}`;
  - no `-1`, no `"unlimited"`, no missing keys;
  - rank strictly increasing;
  - `free` present with `provider_prices = {}`.

### 11.2 Features (closed vocabulary) and default matrix

| Feature | Meaning | Free | Pro | Power |
|---|---|---|---|---|
| `workspace.tracking` | Job workspaces, statuses, notes, manual capture | ✓ | ✓ | ✓ |
| `library.cv` | CV Library, versions, strategy mapping as defaults | ✓ | ✓ | ✓ |
| `profile.onboarding` | Onboarding, profile, reusable answers | ✓ | ✓ | ✓ |
| `profile.cv_import` | AI CV extraction into proposals | ✓ (allowance-bound) | ✓ | ✓ |
| `ai.prepare` | Job understanding + fit + application intelligence + pack | ✓ (allowance-bound) | ✓ | ✓ |
| `ai.cv_tailor` | Tailored CV generation | ✗ | ✓ | ✓ |
| `apply.assisted_fill` | 6D-B fill of an approved application | ✓ | ✓ | ✓ |
| `apply.human_submit` | 6E-A human-authorized submit (subject to §8.6) | ✓ | ✓ | ✓ |
| `discovery.on_demand` | User-initiated discovery runs | ✓ | ✓ | ✓ |
| `discovery.scheduled` | Saved searches that run on a schedule | ✗ | ✗ | ✓ |
| `automation.screening` | 6C candidate screening against fit and rules | ✗ | ✗ | ✓ |
| `automation.prepare` | 6C autonomous PREPARE into the review queue | ✗ | ✗ | ✓ |
| `rules.enforced_automation` | Standing rules act on their own (R2) | ✗ | ✗ | ✓ |
| `notifications.digest` | New-match digests | ✗ | ✗ | ✓ |

The matrix above is the shipped default. The ✓/✗ values sit in the catalog, but the *meaning* of each feature is fixed here. Pro differs from Free by AI assistance volume and tailoring. Power differs from Pro by initiative: scheduled search, screening, autonomous prepare and enforced rules. It is not simply larger numbers.

### 11.3 Allowances (closed vocabulary; all numbers are DP-3)

| Allowance | Unit | Consumed when |
|---|---|---|
| `applications.prepare` | job workspaces | Every AI prepare stage (understanding, fit, intelligence, whether manual or 6C) reserves with idempotency key `prepare:{workspace_id}:{window_key}`. The first reservation is consumed when its stage succeeds, and released if that stage fails. Later stages and reruns in the same window reuse the consumed reservation at no extra charge, but still count toward the cost ceiling. |
| `cv.tailor` | tailored CV versions | Each successful tailored generation |
| `profile.cv_import` | import runs | Each successful extraction run |
| `library.cv_items` | active items (gauge) | Checked when an item is created or unarchived. Over-limit after a downgrade: existing items stay usable and adding is refused. |
| `storage.bytes` | bytes (gauge) | Checked at upload |
| `discovery.on_demand_runs` | runs | Each on-demand run |
| `discovery.scheduled_searches` | active schedules (gauge) | Checked when a schedule is enabled |
| `automation.prepare` | autonomous prepares | Each 6C autonomous prepare (in addition to `applications.prepare`) |
| `ai.cost_micro_usd` | hidden internal ceiling | Every metered provider call (U3). Never shown as a number to the user; shown as "fair-use limit reached". |

Fill and submit are **not metered**. They're user actions under safety review, not cost centres.

The automation ceiling for Power is `PREPARE`. The effective autonomy capability is min(the user's autonomy authorization (6B), the entitlement (PREPARE if `automation.prepare`, else NONE), the deployment ceiling `JOBSEARCH_AUTONOMY_MAX_CAPABILITY`, platform control `AUTOMATION_ENABLED`).

### 11.4 Resolution (pure: `product/entitlements.py`)

`effective_entitlements(catalog, subscription_snapshot | None, grants[], ceilings, now) → Entitlements{plan_id, catalog_version, features{}, allowances{}, window{key, start, end}, source}`:

1. **Plan:** the subscription's plan if its state is `ACTIVE`, `TRIALING`, `CANCEL_SCHEDULED`, or `PAST_DUE` within grace (DP-4). Otherwise `free`.
2. **Catalog version:** the subscription's pinned `catalog_version` (DP-8), falling back to the current one for Free.
3. **Grants** (`entitlement_grants`, append-only; staff BILLING or ADMIN): `PLAN_OVERRIDE` (a higher-ranked plan, with expiry) or `ALLOWANCE_BONUS` (+n for one allowance in the current window). Grants never lower anything.
4. **Ceilings:** platform controls (`AI_ENABLED`, `AUTOMATION_ENABLED`, `DISCOVERY_ENABLED`, `SIGNUPS_ENABLED`) turn off the matching features. Deployment ceilings apply as in 6B.
5. **Window:** the paid period from the snapshot, else the UTC calendar month.

`EntitlementGate` (service): `require_feature(scope, feature)` raises `FeatureNotInPlan(feature, plan_id, upgrade_to)`; `reserve(scope, allowance, amount, subject, idempotency_key)` → reservation (§13).

Gate call sites (exhaustive list; a test asserts each entry point calls the gate):
- `pipeline.run_job_understanding` / `run_job_fit` / `run_application_intelligence` (`ai.prepare`), through the prepare orchestration entry;
- tailored CV generation;
- CV import;
- document upload and CV item creation;
- on-demand discovery;
- enabling a scheduled search;
- 6C screening and autonomous prepare (inside the worker);
- `fill_runs` start (feature only);
- the human submit authorization (feature only).

### 11.5 Never gated (tested)

Regardless of plan, billing state or allowance, these always work, and for a suspended account they work through the restricted page:
- viewing one's own data;
- review, approve, revoke;
- field dispositions;
- ambiguity resolution;
- cancelling a fill or submit before dispatch;
- the kill switch and autonomy controls (off / pause);
- notification read/archive;
- communication preferences and consent withdrawal;
- data export;
- account deletion and cancelling a deletion;
- signing out;
- device revocation;
- managing billing (portal) and cancelling.

## 12. Billing and subscriptions (7D)

### 12.1 Tables (migration `024_billing_entitlements`)

- **`billing_customers`** (account_id PK, provider, provider_customer_id UNIQUE, created_at).
- **`subscriptions`** (id, account_id, provider, provider_subscription_id UNIQUE, plan_id, interval, catalog_version, state, current_period_start, current_period_end, cancel_at_period_end, past_due_since, snapshot_json, snapshot_hash, updated_at). One non-terminal row per account (a partial unique index on account_id where state is not in (`ENDED`, `INCOMPLETE_EXPIRED`)).
- **`subscription_events`** (seq, id, subscription_id, account_id, from_state, to_state, cause, provider_event_id, created_at). Append-only.
- **`billing_webhook_events`** (id, provider, provider_event_id UNIQUE, received_at, signature_verified, payload_json, processed_at, process_error, attempts).
- **`checkout_sessions`** (id, account_id, provider, provider_session_id, plan_id, interval, status, created_at, completed_at). `status` ∈ {`OPEN`, `COMPLETED`, `EXPIRED`, `CANCELED`}.
- **`entitlement_grants`** (seq, id, account_id, kind, plan_id, allowance, amount, reason, actor_user_id, starts_at, expires_at, revoked_at, created_at). Append-only except `revoked_at`, which is set once by trigger check.
- **`plan_catalog_versions`** (catalog_version PK, catalog_hash, catalog_json, loaded_at).

### 12.2 `BillingProvider` port

```python
class BillingProvider(Protocol):
    name: str
    def ensure_customer(self, *, account_id: str, email: str) -> str: ...
    def create_checkout(self, *, customer_id: str, price_id: str, success_url: str, cancel_url: str,
                        trial_days: int, idempotency_key: str) -> CheckoutRef: ...
    def create_portal(self, *, customer_id: str, return_url: str) -> str: ...
    def change_plan(self, *, subscription_id: str, price_id: str, when: Literal["now", "period_end"]) -> None: ...
    def cancel(self, *, subscription_id: str, immediately: bool) -> None: ...
    def fetch_subscription(self, subscription_id: str) -> SubscriptionSnapshot: ...
    def verify_webhook(self, *, headers: Mapping[str, str], body: bytes, now: datetime) -> ProviderEvent: ...
```

- `SubscriptionSnapshot` is normalized: status mapped to the internal state, `plan_id` from a reverse price map, the period, and `cancel_at_period_end`.
- A provider status the adapter can't map becomes `UNKNOWN`. The state machine treats it as Free and writes an admin alert: fail closed to Free, never to paid.
- **`FakeBillingProvider`** serves `/dev/billing/checkout/{id}` (registered only when `deployment=local` or in tests). It has buttons for "Pay succeeds", "Payment fails", "Cancel". It emits signed fake webhooks to the app's own webhook route and keeps snapshots in memory or in a JSON file.

### 12.3 Internal state machine

States: `INCOMPLETE`, `TRIALING`, `ACTIVE`, `PAST_DUE`, `CANCEL_SCHEDULED`, `ENDED`, `INCOMPLETE_EXPIRED`, `UNKNOWN`.

Every processed event re-fetches the snapshot and runs `transition(current, snapshot) → new_state`. The transition function is pure and table-tested, including out-of-order and duplicate events.

`PAST_DUE` records `past_due_since`. Entitlement falls to Free once `now − past_due_since > grace` (DP-4).

**Flows:**
- **Choose plan** (`/plans`): Free needs no provider call. For Pro or Power: `ensure_customer` → `create_checkout` → redirect → the success page polls `GET /api/billing/status` until the subscription is non-`INCOMPLETE` (30 s max, then it shows "We're confirming your payment" with a notification when it lands).
- **Manage** (`/settings/billing`): the portal link; "Change plan" (upgrade now, downgrade at period end); "Cancel" (at period end).
- **Notifications and email:** `billing.payment_failed` (SERVICE), `billing.subscription_changed` (SERVICE), `billing.subscription_canceled` (SERVICE).

## 13. Usage accounting and quotas (7G)

### 13.1 Tables (in `024`)

- **`usage_reservations`** (id, account_id, allowance, amount, subject_type, subject_id, idempotency_key UNIQUE, window_key, status, reserved_at, settled_at, expires_at, settlement_ref). `status` ∈ {`RESERVED`, `CONSUMED`, `RELEASED`}. The only permitted transitions are `RESERVED→CONSUMED` and `RESERVED→RELEASED` (trigger).
- **`ai_cost_events`** (seq, id, account_id, subject_type, subject_id, provider, model, input_tokens, output_tokens, cost_micro_usd, request_ref, created_at). Append-only. Written by `MeteredProvider` from the provider response's usage and the `ai-pricing.v1.json` operator table.

### 13.2 Semantics

- **`reserve`:**
  - Inside the caller's transaction: `lock_account` → compute used = Σ amount where account, allowance, window_key and status ∈ {RESERVED, CONSUMED} → refuse with `AllowanceExhausted(allowance, used, limit, window_end)` if used + amount > limit (null ⇒ limit 0) → insert RESERVED with `expires_at = now + 30 min`.
  - The same `idempotency_key` returns the existing reservation.
- **`consume(reservation_id, settlement_ref)`** runs when the work succeeds. **`release`** runs on system or provider failure, or when the user cancels before work starts.
- **Sweeper:** the worker's `usage.sweep` job releases RESERVED rows past `expires_at`.
- **AI cost ceiling:** `MeteredProvider` checks Σ `ai_cost_events.cost_micro_usd` in the window before each call. At or over the ceiling it refuses with `FairUseLimitReached`. The call that crosses the ceiling is allowed to finish.
- **Gauges** (`library.cv_items`, `storage.bytes`, `discovery.scheduled_searches`) are computed live from the source tables at check time. They aren't reserved.

### 13.3 User-visible limits

- A usage page (`/settings/usage`) shows each non-hidden allowance: used / limit / window end, with the plan comparison.
- The header shows a compact meter for `applications.prepare`.
- Before any consuming action, the UI states the cost ("Uses 1 of your 12 remaining prepares this period").
- Notifications `usage.limit_near` at ≥ 80% and `usage.limit_reached` at 100%, once per allowance per window (dedupe key).
- A refusal shows an explanation and an upgrade path. It never shows a stack trace or a generic error.

## 14. CV Library and CV Strategy (7A)

### 14.1 Tables (migration `025_cv_library`)

- **`cv_library_items`** (id, account_id, title, description, status, created_at, updated_at). `status` ∈ {`ACTIVE`, `ARCHIVED`}.
- **`cv_library_versions`** (seq, id, account_id, item_id, version_no, document_version_id, origin, parent_version_id, template_id, library_visible, note, created_by, created_at). Append-only.
  - UNIQUE (item_id, version_no); FK (document_version_id, account_id, 'cv') → `application_document_versions`.
  - `origin` ∈ {`USER_UPLOAD`, `AI_GENERATED`, `AI_TAILORED`, `IMPORTED_LEGACY`}.
- **`account_policy_documents`** (seq, id, account_id, doc_type, schema_version, doc_json, doc_hash, created_by, created_at). Append-only. `doc_type` ∈ {`job-families`, `cv-strategy`}. The current document is the max seq per (account, doc_type).
- **`application_cv_resolutions`** (seq, id, account_id, application_workspace_id, job_families_hash, cv_strategy_hash, family_id, family_match_json, rule_json, outcome, item_id, version_id, overridden_by_user, created_at). Append-only. `outcome` ∈ {`RESOLVED_VERSION`, `TAILOR_REQUESTED`, `NEEDS_USER_CHOICE`}.
- **`application_document_versions`:** `media_type` CHECK widened to the two allowed types. A new trigger refuses `DELETE` while any `application_approvals.binding_json`, `fill_runs` or `submission_results` reference exists. The reference is detected through a maintained `document_version_references` (document_version_id, referrer_type, referrer_id) table, populated in the same transaction as approvals, fill runs and submits (append-only).
- **Migration:** each `reusable_application_documents` row of kind `cv` becomes an item (title = label) with v1 `IMPORTED_LEGACY`. The legacy table stays readable and is no longer written.

### 14.2 Documents

- **`job-families.v1`:** `{families: [{id, name, match: {title_any: [str], title_none: [str], seniority_in: [SENIORITY_LEVELS]}, priority: int}]}`, at most 50 families.
  - Matching is deterministic: casefolded, NFKC-normalized whole-word matching over job-understanding `title` (falling back to the source record title) and `seniority`.
  - Several matches at the highest priority, or none, gives `UNKNOWN`.
- **`cv-strategy.v1`:** `{default: Rule, by_family: {family_id: Rule}}`.
  - `Rule = {mode: FIXED_VERSION, version_id} | {mode: LATEST_VERSION, item_id} | {mode: TAILOR_FROM, item_id, template_id}`.
  - Validation: every referenced item or version belongs to the account and is ACTIVE; `template_id` is in the template registry.
  - The default rule is required. The initial default is `{mode: LATEST_VERSION, item_id: null}`, which resolves to `NEEDS_USER_CHOICE` until the user picks one.

### 14.3 Resolution (`webapp/services/cv_strategy.py`, pure core in `product/cv_strategy.py`)

At prepare, after job understanding:
1. classify the family, then look up the rule;
2. `FIXED_VERSION` → that version;
3. `LATEST_VERSION` → the max `version_no` with `library_visible = 1` of the item;
4. `TAILOR_FROM` → requires feature `ai.cv_tailor` and a `cv.tailor` reservation. On success it creates a version with origin `AI_TAILORED`, `library_visible = 0` and parent = the item's latest. Without the feature, it falls back to `LATEST_VERSION` of the same item and records `outcome = RESOLVED_VERSION` with `rule_json.fallback = "TAILOR_NOT_IN_PLAN"`, which is shown to the user.
5. `NEEDS_USER_CHOICE` → the review shows a required CV choice. It's added to the existing review items, so approval is impossible without it.

The chosen version becomes the workspace's `application_document_selections` cv row through the existing selection path. The user's override uses the existing `DOCUMENT_REPLACED` / `SELECTION_CHANGED` review events and appends a resolution row with `overridden_by_user = 1`.

### 14.4 Exact historical binding

The 6D-A approval binding already carries `document_version_id` + `sha256`, and `fill_runs` and `submission_results` bind to the approval. Bundle 7 adds:
- the `application_cv_resolutions` record of *why* that CV was used;
- the undeletable-reference rule (L3);
- the application detail page shows "CV used: <item title> v<n> (<origin>)", with a download of the exact bytes.

Archiving an item never affects historical views.

### 14.5 Templates

`product/cv_templates.py` is a registry: `{"standard@1": {"renderer": "cv_document_renderer", "formats": ["docx"]}}`. Adding templates is a data change. Template choice applies only to `AI_GENERATED` / `AI_TAILORED`. Uploaded CVs are always used byte-exact.

### 14.6 UI

- `/cvs`: the library list with the default and family mapping summary.
- `/cvs/{item}`: versions, upload a new version, a note, archive, and "used by N applications".
- `/cvs/strategy`: families and rules editor (shared with 7E).
- Upload: DOCX/PDF ≤ 10 MiB. It shows the storage and item gauges.

## 15. Onboarding and profile (7B)

### 15.1 Tables (migration `026_onboarding_profile`)

- **`account_onboarding`** (account_id PK, state_json, updated_at). This is mutable UI progress, not evidence. `state_json` = `{steps: {step_id: "TODO"|"DONE"|"SKIPPED"}, version: "onboarding.v1"}`.
- **`profile_import_runs`** (id, account_id, document_version_id, status, provider_audit_id, reservation_id, error_code, created_at, completed_at). `status` ∈ {`QUEUED`, `RUNNING`, `PROPOSED`, `FAILED`}.
- **`profile_proposals`** (seq, id, account_id, import_run_id, target, kind, fields_json, source_excerpt, confidence, created_at). Append-only.
  - `target` ∈ {`PROFILE_ENTRY`, `ANSWER`}.
  - `kind` ∈ `ENTRY_DEFINITIONS` keys, or answer subjects in the registry.
- **`profile_proposal_resolutions`** (seq, id, proposal_id, resolution, final_fields_json, resulting_ref, actor, created_at). Append-only. `resolution` ∈ {`ACCEPTED`, `EDITED_ACCEPTED`, `REJECTED`}.

### 15.2 Steps (`onboarding.v1`)

Order is recommended, and every step except 1 can be skipped and resumed from the dashboard checklist.

| # | Step | Writes through |
|---|---|---|
| 1 | **About you:** name, contact email (defaults to the login email), phone, city/country | `profile_manager` identity entries; answers `contact.email`, `contact.phone`, `location.current` (reach ACCOUNT; these subjects are added to the semantic subject registry if they're missing, along with the step-4 subjects) |
| 2 | **Your CV:** upload, which creates the item "My CV" v1 (`USER_UPLOAD`) and sets it as the default `LATEST_VERSION` rule | Library (§14) |
| 3 | **Import from CV** (optional): an extraction run → proposals → accept, edit or reject each for employment, education, certification, technical_skill, language, achievement, and answer proposals | `profile_manager.create_profile_entry` with provenance `cv_import:{document_version_id}`; `approve_answer` + `answer_confirmations` |
| 4 | **Work eligibility and logistics:** work authorization per country, sponsorship need, notice period, relocation, rotation availability, earliest start | Answers (reach ACCOUNT) with confirmations |
| 5 | **What you're looking for:** target roles, seniority, locations, remote preference, employment types, desired compensation | `user-profile.v2` on the default search workspace |
| 6 | **Job families and CV mapping:** created from target roles (a suggested family per role, editable) and mapped to CVs | `job-families.v1`, `cv-strategy.v1` |
| 7 | **Rules** (optional): salary floor, excluded locations, acceptable rotations, employer block list | `standing-policy.v2` |
| 8 | **Browser extension:** install link and pairing | §9 |

- **Ready to prepare** (checked by `ai.prepare` in addition to entitlement): email verified; identity name present; ≥ 1 active CV item; the default CV rule resolves; ≥ 1 target role. When it fails, the response is `ONBOARDING_INCOMPLETE` with the missing items, and the UI links to the steps.
- **Ready to fill:** a paired, non-revoked extension device.

### 15.3 CV extraction

- Text is extracted server-side: DOCX with `python-docx`, PDF with `pypdf`. There's no OCR; a scanned PDF gives `FAILED: NO_TEXT` and a suggestion to enter details manually.
- The text goes to a new `CvExtractionProvider` (OpenAI adapter plus a fake) with a strict JSON schema. It's wrapped by `MeteredProvider`, and the reservation is `profile.cv_import`.
- Proposals are validated against `ENTRY_DEFINITIONS` and the answer-subject registry. An invalid proposal is dropped and counted in the run detail.
- Nothing is written to the profile or the answers until a resolution. A resolution writes the entry and records `resulting_ref`. Accepting several proposals goes through one `profile_manager` batch path, so there's a single source revision per accept batch.

### 15.4 Existing onboarding walkthroughs

`onboarding_progress` and the walkthrough registry stay as they are: contextual tours. The dashboard shows the `onboarding.v1` checklist until every non-optional step is DONE or SKIPPED.

## 16. Preferences, job families and application rules (7E)

### 16.1 `user-profile.v2` (preferences, soft)

v1 fields plus `rotation_preference` ∈ {`no_preference`, `rotation_only`, `rotation_acceptable`, `no_rotation`}, `acceptable_rotations: ["14/14","21/21","28/28","other"]`, `relocation` ∈ {`no`, `within_country`, `international`}, `job_family_ids: [id]`.

v1 documents are read as v2 with defaults. v1 writes are refused once the migration has run. The normalizer is extended, and v2 is the only write format.

Preferences drive discovery search terms, filters and ranking. Nothing refuses a job for a preference alone.

### 16.2 `standing-policy.v2` (rules, restrictive)

- It keeps the v1 schema and semantics: effects `REDUCE_TO` / `BLOCK` / `REQUIRE_USER`, three-valued predicates, `on_unknown`, `limits`, `employer_lists`.
- **New attributes:**
  - `job.family` (str: family id or `UNKNOWN`);
  - `job.compensation_max_annual` (int, in the rule's currency; UNKNOWN when the job gives no compensation or a different currency: no FX conversion);
  - `job.country` (str, ISO-3166 alpha-2 from the job-understanding location; UNKNOWN when absent);
  - `job.remote_mode` (str, from job understanding; UNKNOWN when absent);
  - `job.rotation` (str `"N/M"`). Derived deterministically: the first match of `(\d{1,2})\s*/\s*(\d{1,2})` within 40 characters of the word "rotation" (casefolded) in the posting text; UNKNOWN when there's no match.
- **Well-known rule ids**, generated by the Rules UI and editable as rules:
  - `pref.salary_floor`: `job.compensation_max_annual lt X` → effect `BLOCK` or `REQUIRE_USER`, as the user chooses; `on_unknown: NO_EFFECT` or `REQUIRE_USER`.
  - `pref.excluded_locations`: `job.country in [..]` → `BLOCK`.
  - `pref.rotation`: `job.rotation not_in [..]` → the user's chosen effect.
  - `pref.excluded_employers`: `company.key in_list blocked` → `BLOCK`.
- v1 documents upgrade in place on read, since the added attributes are optional. The migration re-saves each account's current v1 as v2 with an identical rule list.

### 16.3 Where rules act (R2)

- **Automated:**
  - Power screening (6C `autonomy_candidate_screenings`) and autonomous prepare evaluate `standing-policy.v2` exactly as 6B/6C evaluate v1.
  - `BLOCK` → not prepared, with a reason.
  - `REQUIRE_USER` → the item goes to the inbox as needs-user.
  - `REDUCE_TO` → a capability cap.
- **Manual (every plan):**
  - Prepare, the review page and the fill start evaluate the same rules.
  - `BLOCK` results appear as "This job conflicts with your rule: …" and require a `rule_acknowledgements` row (disposition `OVERRIDDEN_FOR_THIS_APPLICATION`) before approval.
  - Other effects are displayed only.
- Rules never grant.

### 16.4 UI

`/preferences` holds preferences, the families and CV mapping (shared with `/cvs/strategy`), and the rules, as three tabs on one page. Every save creates a new document version. The page shows "applies to new preparations; existing approvals are unaffected". Approvals bind their snapshot, and a changed rule doesn't invalidate an existing approval.

## 17. Notifications and inbox (7F)

### 17.1 Tables (migration `027_notifications_comms`)

- **`notifications`** (id, account_id, kind, category, severity, subject_type, subject_id, dedupe_key, detail_json, created_at, read_at, archived_at). UNIQUE (account_id, dedupe_key).
  - `category` ∈ {`ACTION_REQUIRED`, `OUTCOME`, `ACCOUNT`, `SECURITY`, `BILLING`, `USAGE`, `ANNOUNCEMENT`, `DISCOVERY`}.
  - `severity` ∈ {`INFO`, `WARNING`, `CRITICAL`}.
- **`notification_preferences`** (account_id, category, email_mode, updated_at), with PK (account_id, category).
  - `email_mode` ∈ {`IMMEDIATE`, `DAILY_DIGEST`, `OFF`}.
  - Only `ACTION_REQUIRED`, `OUTCOME`, `USAGE` and `DISCOVERY` rows are allowed. `SECURITY`, `BILLING` and `ACCOUNT` are always IMMEDIATE and aren't configurable.
  - Defaults: `ACTION_REQUIRED` IMMEDIATE, `OUTCOME` IMMEDIATE, `USAGE` IMMEDIATE, `DISCOVERY` DAILY_DIGEST.

### 17.2 Kinds (closed vocabulary; producer → category)

| Kind | Producer | Category |
|---|---|---|
| `application.prepared` | pipeline / 6C prepare success | OUTCOME |
| `application.review_required` | pack ready for review; new review delta | ACTION_REQUIRED |
| `application.approval_expiring` | worker, 48 h before approval TTL | ACTION_REQUIRED |
| `application.blocker_needs_answer` | new open blocker | ACTION_REQUIRED |
| `application.automation_blocked` | 6C prepare blocked or failed operationally | OUTCOME |
| `fill.completed_awaiting_submit` | `FILLED_AWAITING_SUBMISSION` | ACTION_REQUIRED |
| `fill.failed` | fill result failure | OUTCOME |
| `submit.confirmed` | `CONFIRMED_SUCCESS` | OUTCOME |
| `submit.ambiguous` | `SUBMISSION_AMBIGUOUS` | ACTION_REQUIRED |
| `submit.failed` | proven `SUBMISSION_FAILED` | OUTCOME |
| `submit.challenge_handoff` | challenge detected | ACTION_REQUIRED (in-app only; time-critical; no email) |
| `discovery.new_matches` | scheduled run with qualifying candidates | DISCOVERY |
| `usage.limit_near`, `usage.limit_reached` | usage | USAGE |
| `billing.payment_failed`, `billing.subscription_changed`, `billing.subscription_canceled` | billing | BILLING |
| `security.new_device_paired`, `security.token_reuse_detected`, `security.password_changed`, `security.email_changed` | auth / devices | SECURITY |
| `account.suspended`, `account.deletion_requested`, `account.data_export_ready` | ops | ACCOUNT |
| `announcement.published` | admin | ANNOUNCEMENT |

`notify(conn, *, account_id, kind, subject, dedupe_key, detail)` does no commit. It writes the notification and, where the category rules require it, enqueues the outbox message in the same transaction. The 6C `ap.create_notification` also calls `notify`, mapping 6C kinds as NEEDS_USER and CANDIDATE_QUESTION→`application.blocker_needs_answer`, PREPARED→`application.prepared`, and BLOCKED/OPERATIONAL_ERROR→`application.automation_blocked`. The 6C table is kept.

### 17.3 Email fan-out

- An IMMEDIATE category enqueues `notify.immediate` (template per kind group).
- `DAILY_DIGEST` is collected by the worker's `notify.digest` job at 07:00 in the account's timezone (from `standing-policy` `timezone`, default UTC).
- `OFF` sends no email.
- A `submit.challenge_handoff` never emails.

### 17.4 Inbox

`/inbox` has three tabs.
- **Action required** is derived live:
  - open blockers;
  - packs awaiting review;
  - open review deltas;
  - fills awaiting submit;
  - ambiguous submissions;
  - challenge handoffs in progress;
  - unacknowledged BLOCK rule conflicts;
  - onboarding-incomplete prerequisites.
  
  Each links to the page that resolves it, and disappears when the source state resolves.
- **Updates** is the notifications list, with read/archive.
- **Announcements**.

The header badge = the Action-required count + unread CRITICAL notifications. The existing 6C inbox summary is folded into Action required.

## 18. Communications and consent (7C)

### 18.1 Tables (in `027`)

- **`outbound_messages`** (id, account_id, user_id, channel, category, template_id, template_version, locale, to_address, payload_json, idempotency_key UNIQUE, status, attempts, next_attempt_at, provider, provider_message_id, last_error, created_at, sent_at).
  - `channel` ∈ {`EMAIL`} (vocabulary room: `SMS`, `WHATSAPP`).
  - `category` ∈ {`SERVICE`, `PRODUCT`, `MARKETING`}.
  - `status` ∈ {`QUEUED`, `SENDING`, `SENT`, `FAILED`, `SUPPRESSED`, `CANCELED`}.
- **`email_suppressions`** (address_hash PK, reason, created_at). `reason` ∈ {`HARD_BOUNCE`, `COMPLAINT`}.
- **`email_provider_events`** (id, provider, provider_event_id UNIQUE, kind, address_hash, payload_json, received_at, processed_at).
- **`communication_consents`** (seq, id, account_id, user_id, channel, purpose, state, wording_version, source, created_at). Append-only.
  - `purpose` ∈ {`MARKETING`}.
  - `state` ∈ {`GRANTED`, `WITHDRAWN`}.
  - The current state is the latest row per (user, channel, purpose). The default with no row is "not granted".
- **`announcements`** (id, title, body_markdown, audience, severity, published_at, expires_at, created_by, created_at, withdrawn_at).
  - `audience` ∈ {`ALL`, `PLAN:free`, `PLAN:pro`, `PLAN:power`}.
  - The body is rendered with a restricted Markdown subset (paragraphs, bold/italic, links to `https`) and sanitized.

### 18.2 Dispatch

The worker's `outbox.dispatch` job claims QUEUED rows past `next_attempt_at` (lease), then:
1. **Suppression check:** SERVICE auth mail (verify, reset) is still sent to a suppressed address only if the reason is `HARD_BOUNCE` older than 30 days; otherwise → `SUPPRESSED`.
2. **Category check:** `MARKETING` is refused (`CANCELED: NO_CONSENT_OR_NOT_ENABLED`). The dispatcher has no marketing path in Bundle 7.
3. The message is rendered from the versioned template.
4. `EmailProvider.send(message, idempotency_key)`.
5. Transient failure: backoff 1 m, 5 m, 30 m, 2 h, 6 h, then `FAILED` (dead letter, visible in admin with a retry action). Permanent failure → `FAILED` immediately.

Messages are enqueued in the producing transaction. Nothing ever sends synchronously from a request.

### 18.3 Templates

`webapp/templates/email/{template_id}/v1/{subject.txt, body.txt, body.html}`. A template is rendered with the Jinja sandboxed environment and autoescape for HTML.

Closed template ids:
- `auth.verify_email`, `auth.password_reset`, `auth.password_changed`, `auth.email_change_confirm`, `auth.email_change_notice`, `auth.account_exists`;
- `security.new_device_paired`, `security.token_reuse_detected`;
- `billing.payment_failed`, `billing.subscription_changed`, `billing.subscription_canceled`;
- `account.deletion_requested`, `account.deletion_completed`, `account.data_export_ready`, `account.suspended`;
- `notify.immediate`, `notify.digest`;
- `usage.limit_near`, `usage.limit_reached`;
- `announcement.service_notice`.

Every mail carries the account-settings link. PRODUCT mail carries a one-click link to the preferences page. SERVICE mail states that it's a service message.

### 18.4 Preference centre

`/settings/communications` has:
- the notification email mode per configurable category;
- a marketing email opt-in checkbox (default off; writes consent rows with `wording_version`; no marketing is sent in Bundle 7, and the page says "We'll only use this if we start sending product news");
- a list of service messages, marked "always sent".

SMS and WhatsApp aren't shown. The data model accepts them later without a migration of the consents table.

## 19. Admin console (7C)

### 19.1 Roles → permissions (closed)

| Permission | SUPPORT | OPERATIONS | BILLING | ADMIN |
|---|---|---|---|---|
| View users/accounts metadata, usage, onboarding, devices count, notifications count, audit for an account | ✓ | ✓ | ✓ | ✓ |
| Resend verification; send password-reset link | ✓ | ✓ | | ✓ |
| Suspend / unsuspend account; revoke sessions/devices | | ✓ | | ✓ |
| Engage/release per-account autonomy kill switch | | ✓ | | ✓ |
| View billing state, subscription events, webhook events | | | ✓ | ✓ |
| Create/revoke entitlement grants | | | ✓ | ✓ |
| Retry dead-letter jobs/outbox | | ✓ | | ✓ |
| Announcements CRUD | | ✓ | | ✓ |
| Platform controls | | | | ✓ |
| Staff role management | | | | ✓ |
| Initiate account deletion on verified user request | | | | ✓ |
| Platform audit log (all) | | | | ✓ |

### 19.2 Pages

`/admin` holds:
- **Dashboard:** accounts by status and plan; signups over 7 and 30 days; verified %; onboarding completion %; prepares, fills and submits over 7 days; the outbox (queued, failed); jobs (queued, dead); webhooks (unprocessed, failed); AI cost over 7 and 30 days; unresolved DP count from the readiness tool.
- `/admin/accounts` (search by email or id; filter by status and plan) and `/admin/accounts/{id}` (detail, actions).
- `/admin/billing/events`, `/admin/jobs`, `/admin/outbox`, `/admin/announcements`, `/admin/controls`, `/admin/staff`, `/admin/audit`.

### 19.3 Platform controls (`platform_controls`, append-only; the current value is the latest per key)

Keys: `SIGNUPS_ENABLED`, `AI_ENABLED`, `AUTOMATION_ENABLED`, `DISCOVERY_ENABLED`, `SUBMIT_ENABLED`, `HOSTED_THREAT_MODEL_SIGNED_OFF`.

A missing key reads as the fail-closed value: false for every key, except that `SIGNUPS_ENABLED`, `AI_ENABLED` and `DISCOVERY_ENABLED` default true in local mode only.

The 6B file sentinel `AUTONOMY_HALT` keeps working in local mode. In hosted mode `AUTOMATION_ENABLED=false` is the halt.

### 19.4 Privacy boundary

- Admin queries never select candidate content columns: profile sources, document bytes and names, artifacts payloads, answers values, observations, job text.
- The admin service layer only calls admin read-models (`webapp/services/admin_read_models.py`) that return counts and statuses.
- A test asserts that no admin route response contains a seeded canary string planted in every content table.

## 20. Operational readiness (7H)

### 20.1 Audit log (migration `028_ops`)

- **`audit_log`** (seq, id, occurred_at, actor_type, actor_id, account_id, action, target_type, target_id, request_id, ip_hash, detail_json). Append-only.
  - `actor_type` ∈ {`USER`, `STAFF`, `EXTENSION_DEVICE`, `SYSTEM`, `PROVIDER`}.
  - `action` is a closed vocabulary (`product/audit_actions.py`), including:
    - LOGIN_SUCCEEDED/FAILED, LOGOUT, SESSIONS_REVOKED;
    - PASSWORD_CHANGED, PASSWORD_RESET, EMAIL_VERIFIED, EMAIL_CHANGED;
    - EXTENSION_DEVICE_PAIRED/REVOKED, EXTENSION_TOKEN_REUSE;
    - PLAN_CHECKOUT_STARTED, SUBSCRIPTION_STATE_CHANGED;
    - ENTITLEMENT_GRANT_CREATED/REVOKED;
    - ACCOUNT_SUSPENDED/UNSUSPENDED, ACCOUNT_DELETION_REQUESTED/CANCELED, ACCOUNT_PURGED;
    - DATA_EXPORT_REQUESTED/DOWNLOADED;
    - PLATFORM_CONTROL_SET, STAFF_ROLE_CHANGED, ANNOUNCEMENT_PUBLISHED;
    - ADMIN_ACCOUNT_VIEWED, DEAD_LETTER_RETRIED;
    - CONSENT_CHANGED.
- `ip_hash` = HMAC(secret, ip) truncated. Raw IPs aren't stored.
- Users see their own account's security-relevant entries on `/settings/security`.

### 20.2 Jobs and worker

- **`jobs`** (id, kind, account_id, payload_json, dedupe_key UNIQUE (NULLs allowed), run_at, attempts, max_attempts, lease_holder, lease_expires_at, status, last_error, created_at, updated_at, finished_at). `status` ∈ {`QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `DEAD`}.
- Kinds (closed):
  - `outbox.dispatch`, `billing.webhook.process`, `email.webhook.process`;
  - `usage.sweep`, `tokens.sweep`;
  - `notify.digest`, `notify.approval_expiry_scan`;
  - `discovery.scheduled_run`;
  - `account.purge`, `account.export`;
  - `autonomy.tick` (wraps the 6C driver tick).
- **Worker loop:** claim by lease (60 s, renewed), run the handler with a per-kind timeout, backoff `min(2^attempts × 30 s, 6 h)`, then `DEAD` after `max_attempts` (default 8). Every DEAD job raises an ops alert (a `notifications` row for staff via the admin dashboard, plus an `ErrorReporter` event).
- **Handlers are idempotent,** keyed by `dedupe_key` or by the domain's own idempotency.
- **Periodic scheduling:** a `schedule` table in code (`webapp/worker/schedule.py`) enqueues periodic kinds with a dedupe key per time slot.

### 20.3 Scheduled discovery (Power)

- **`search_schedules`** (search_workspace_id PK, account_id, cadence, enabled, next_run_at, last_run_id, updated_at). `cadence` ∈ {`DAILY`, `WEEKLY`}.
- Enabling requires `discovery.scheduled` plus the `discovery.scheduled_searches` gauge.
- The worker runs `discovery.scheduled_run` → `run_discovery_search` with operator-enabled sources only (DP-9) → the 6C screening queue (if `automation.screening`) → the digest notification.
- Downgrade or suspension disables schedules. Schedules aren't deleted, and they re-enable on the user's action.

### 20.4 Account deletion and purge

- **Request** (`/settings/account/delete`): re-enter the password and type the account email.
  - account and user → `DELETION_REQUESTED`;
  - all sessions and devices revoked (the current session is replaced by a deletion-restricted session);
  - queue items paused, schedules disabled, kill switch engaged;
  - the subscription cancelled at the provider (`cancel(immediately=True)`, no refund, DP-7);
  - service email `account.deletion_requested`;
  - `account.purge` job queued at `now + cooling_off` (DP-4).
- **Cancel** (during cooling-off): sign in → restricted page → "Keep my account" → back to ACTIVE, with the kill switch left engaged (the user releases it).
- **Purge job** runs in one transaction per phase, idempotent:
  1. Revalidate `DELETION_REQUESTED` and the cooling-off period.
  2. Delete object-store keys for the account prefix. This is safe to repeat.
  3. Database purge: insert a `purge_in_progress(account_id)` row, then walk the registry in FK-safe order.
     - `DELETE`-class rows are deleted.
     - `PSEUDONYMIZE`-class columns are overwritten: email → `deleted-{id}@invalid`, names → null, ip_hash → null.
     - `RETAIN`-class rows are left as they are and tagged with a retention class.
     - Finally the purge row is removed.
     - Append-only DELETE triggers are recreated (migration `028`) with `WHEN NOT EXISTS (SELECT 1 FROM purge_in_progress)`. UPDATE triggers stay strict, except that pseudonymization runs through a dedicated `PSEUDONYMIZE` allowance with the same WHEN guard, limited to the registry's listed columns.
  4. Tombstone: `accounts.status = PURGED`, `users.status = PURGED`, `email_normalized` replaced by `purged:{sha256(email)}` so the address can sign up again; audit `ACCOUNT_PURGED`; service email `account.deletion_completed` to the address captured before the purge.
- **Retention classes (DP-4 periods):**

  | Class | Records |
  |---|---|
  | `BILLING_FINANCIAL` | `subscriptions`, `subscription_events`, `billing_customers`, `checkout_sessions` |
  | `SECURITY_AUDIT` | `audit_log` |
  | `CONSENT_PROOF` | `communication_consents`, `legal_acceptances` |
  | `SUPPRESSION` | `email_suppressions` (hash only) |

  - A `retention.expire` periodic job deletes retained rows past their period.
  - A `null` period means the rows are retained indefinitely and listed by the readiness tool.

### 20.5 Data export

- `POST /settings/account/export` → `account.export` job → a ZIP in the object store (`accounts/{id}/exports/{export_id}.zip`) holding:
  - JSON per export-class table: the account's rows, secrets excluded;
  - the documents' original bytes;
  - the profile sources.
- Link expiry per DP-4, default 7 days. Download requires a USER session.
- Notification `account.data_export_ready`; audit on request and on download.
- One export at a time per account, and at most 3 per day (technical limits).

### 20.6 Health, readiness, metrics, logging, errors

- **`/health`:** liveness, unchanged.
- **`/ready`:** the database connects, the migrations are current, the object store `exists` probe passes, the catalog loads, and in hosted mode an email provider and a secret key are configured. Returns 503 with failing check names (no secrets).
- **`/metrics`:** Prometheus text. It requires the `JOBSEARCH_METRICS_TOKEN` bearer and is disabled when unset. Metrics:
  - request count and latency by route template and status;
  - jobs by kind and status;
  - outbox by status;
  - webhook lag;
  - AI calls and cost by provider;
  - `dbapi.DatabaseBusy` count.
- **Logging:** structured JSON lines (`webapp/observability.py`) with `request_id` (the `X-Request-ID` inbound if it's a valid UUID, else generated; echoed in the response), route, account_id (not email), duration, and outcome. Redaction covers the `authorization`, `cookie`, `set-cookie`, `password`, `token`, `code`, `refresh_token` and `access_token` keys, recursively.
- **`ErrorReporter` port:** unhandled exceptions → log plus the reporter; the user gets a generic 500 page with the request id.

### 20.7 Security headers (middleware)

- `Strict-Transport-Security: max-age=31536000; includeSubDomains` (hosted only);
- `Content-Security-Policy: default-src 'self'; script-src 'self' 'nonce-{n}'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`;
- `X-Content-Type-Options: nosniff`;
- `Referrer-Policy: strict-origin-when-cross-origin`;
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`;
- `Cross-Origin-Opener-Policy: same-origin`.

Templates get a `csp_nonce`. Existing inline scripts and handlers move into static files or nonce'd blocks (Task 7). File downloads get `Content-Disposition: attachment` with a sanitized filename, and the media type from the record.

## 21. API boundaries

### 21.1 Auth classes

| Class | Dependency | Accepts | CSRF | Examples |
|---|---|---|---|---|
| PUBLIC | `public_route` | anyone (rate-limited) | form endpoints: a pre-session CSRF cookie token | `/`, `/pricing`, `/signup`, `/login`, `/auth/*`, `/health`, `/ready` |
| USER | `get_account_scope` (+ `require_verified`, `require_active`) | customer session, or local mode | yes (unsafe methods) | every existing app and API route, `/settings/*`, `/cvs/*`, `/preferences`, `/inbox`, `/plans`, `/api/billing/*` |
| EXTENSION | `get_extension_scope` | bearer access token | no | `/api/ext/*`, `/api/handoff/*` (session endpoints), fill-extension and submit-extension routes |
| ADMIN | `get_admin_scope(permission)` | staff session + TOTP-verified | yes | `/admin/*` |
| WEBHOOK | `webhook_route(provider)` | signature-verified body | no | `/webhooks/billing/{provider}`, `/webhooks/email/{provider}` |

`/metrics` is its own class, bearer-token only. `/dev/billing/*` exists only when `deployment=local` or in tests.

### 21.2 New route groups (JSON under `/api`, pages without)

- **Auth:** `/auth/*` (§7).
- **Account settings:** `/settings/{profile-basics, password, email, sessions, security, extension, communications, billing, usage, account}`, plus `/api/settings/*` for the JSON actions.
- **Billing:**
  - `GET /api/billing/status`
  - `POST /api/billing/checkout` {plan_id, interval}
  - `POST /api/billing/portal`
  - `POST /api/billing/change-plan`
  - `POST /api/billing/cancel`
  - `POST /api/billing/resume`
- **Usage:** `GET /api/usage`.
- **Plans:** `GET /api/plans` (public catalog view: display names, features and allowances; prices come from the provider, or "—" when unresolved).
- **CV Library:**
  - `GET/POST /api/cvs`
  - `GET/PATCH /api/cvs/{item}`
  - `POST /api/cvs/{item}/versions` (multipart)
  - `GET /api/cvs/{item}/versions/{v}/download`
  - `POST /api/cvs/{item}/archive|unarchive`
  - `GET/PUT /api/cv-strategy`
  - `GET/PUT /api/job-families`
- **Onboarding:**
  - `GET /api/onboarding`
  - `POST /api/onboarding/steps/{id}` {action: DONE|SKIPPED|REOPEN}
  - `POST /api/profile-imports` (from a document_version_id)
  - `GET /api/profile-imports/{id}`
  - `POST /api/profile-proposals/{id}/resolve` {resolution, fields?}
  - `POST /api/profile-proposals/resolve-batch`
- **Preferences:**
  - `GET/PUT /api/search-workspaces/{id}/preferences` (v2, existing route upgraded)
  - `GET/PUT /api/rules` (standing-policy v2)
  - `POST /api/search-workspaces/{id}/schedule`
- **Notifications:**
  - `GET /api/notifications`
  - `POST /api/notifications/{id}/read|archive`
  - `POST /api/notifications/read-all`
  - `GET /api/inbox/action-required`
  - `GET/PUT /api/notification-preferences`
- **Account:**
  - `POST /api/account/export`
  - `GET /api/account/exports/{id}/download`
  - `POST /api/account/delete`
  - `POST /api/account/delete/cancel`
- **Extension:**
  - `POST /api/ext/pairing-codes` (USER)
  - `POST /api/ext/pair` (PUBLIC, code-authenticated, rate-limited)
  - `POST /api/ext/token` (PUBLIC, refresh-authenticated)
  - `POST /api/ext/devices/self/revoke` (EXTENSION)
  - `GET /api/ext/whoami` (EXTENSION)
  - `GET /api/settings/devices` (USER)
  - `POST /api/settings/devices/{id}/revoke` (USER)
- **Admin:** `/admin/*` pages plus `/admin/api/*` actions (§19).

Existing routes keep their paths and payloads. The only change is the auth dependency class, and on EXTENSION routes the header, from `X-Handoff-Credential` to `Authorization: Bearer`.

### 21.3 Error contract

Domain refusals return JSON `{"error": CODE, "message": str, "detail": {...}}` with stable codes:
- `FEATURE_NOT_IN_PLAN` (402)
- `ALLOWANCE_EXHAUSTED` (402)
- `FAIR_USE_LIMIT_REACHED` (429)
- `ONBOARDING_INCOMPLETE` (409)
- `EMAIL_NOT_VERIFIED` (403)
- `ACCOUNT_SUSPENDED` (403)
- `ACCOUNT_UNAVAILABLE` (403)
- `DEVICE_REVOKED` (401)
- `ACCOUNT_MISMATCH` (403)
- `TICKET_INVALID` (403)
- `RATE_LIMITED` (429)
- `DATABASE_BUSY` (503)
- `CSRF_FAILED` (403)

Pages render the same codes as human messages with an action (upgrade, verify, onboarding link).

## 22. User journeys and states

### 22.1 Release journey (the acceptance path; §25.4 automates it)

| # | User does | System does | State after |
|---|---|---|---|
| 1 | Visits `/pricing`, clicks **Get started** | — | — |
| 2 | Signs up (email, password, name, accepts Terms/Privacy) | Creates user, account, workspaces, default docs; sends verification mail | user `PENDING_VERIFICATION` |
| 3 | Clicks the verification link | Verifies the email; creates a session | user `ACTIVE` |
| 4 | Chooses **Pro** (or Free) at `/plans` | Checkout via BillingProvider → webhook → snapshot → `ACTIVE` subscription; entitlements Pro | subscription `ACTIVE` |
| 5 | Onboarding steps 1–2: basics, uploads a CV | Profile entries and answers; CV item "My CV" v1; default strategy rule | onboarding 1–2 DONE |
| 6 | Step 3: imports from the CV, accepts or edits proposals | Extraction (1 `profile.cv_import`); resolutions write the entries | profile populated |
| 7 | Steps 4–6: eligibility, preferences, a job family "Drilling Engineer" → a CV | Answers; `user-profile.v2`; `job-families.v1`; `cv-strategy.v1` | ready to prepare |
| 8 | Step 7: sets a salary-floor rule | `standing-policy.v2` | — |
| 9 | Step 8: installs the extension, enters the pairing code | Device paired; security email | ready to fill |
| 10 | Captures a job (pastes the posting, or picks from on-demand discovery) | Job workspace created | workspace `new` |
| 11 | Clicks **Prepare** (UI shows "uses 1 of N") | Reservation → understanding → fit → CV resolution (family → CV) → intelligence → pack; consumes; notification `application.prepared` + `application.review_required` | pack awaiting review |
| 12 | Reviews and approves (6D-A), acknowledges any rule conflict | Approval bound to exact documents | approved |
| 13 | Starts the fill from the review page, then the extension fills (6D-B) | Fill run; `fill.completed_awaiting_submit` | `FILLED_AWAITING_SUBMISSION` |
| 14 | Opens Submit Review, clicks **Submit application** (6E-A) | Authorization → pre-click → dispatch → click → result; `submit.confirmed` | `SUBMITTED` / result recorded |
| 15 | Sees the application in the tracker with "CV used: My CV v1" | — | — |

Steps 13–14 run against fixture ATS origins in local mode in the automated suite (§8.6). The hosted live equivalent is a release-gate item (§27.2).

### 22.2 Other journeys (each has an API-level test)

- **Free user:** signs up, skips payment, onboards, prepares up to the Free allowance, then gets `ALLOWANCE_EXHAUSTED` with an upgrade path. Tracking, library, review, fill and submit of already-prepared applications keep working.
- **Payment failure:** `PAST_DUE` → banner plus email → after the grace period, entitlements are Free → pays → back to `ACTIVE`, with no data lost.
- **Downgrade Power → Pro:** at period end, schedules and automation stop, and the Power features show "paused — requires Power".
- **Deletion:** request → cooling-off → purge → tombstone → the same email can sign up fresh.
- **Suspension:** staff suspends → the user sees the restricted page → export and delete still work.
- **Device theft:** the user revokes the device → the extension's next call gets 401 and it wipes itself.

### 22.3 Application states

Unchanged from Bundles 6D–6E. Bundle 7 adds no application state. It only adds preconditions: entitlement, readiness, rule acknowledgement.

## 23. Migrations and compatibility with Bundle 6 data

### 23.1 SQLite chain (dev databases)

| Migration | Contents |
|---|---|
| `022_identity` | §6.1 tables; `user_profile_versions.account_id` backfill |
| `023_extension_devices` | §9.1; revokes legacy extension credentials |
| `024_billing_entitlements` | §12.1, §13.1 |
| `025_cv_library` | §14.1, including `document_version_references` (backfilled from existing approvals, fill runs and results) and its delete-refusal trigger; legacy reusable CVs → items |
| `026_onboarding_profile` | §15.1; `profile_source_revisions`. In local mode, `account_onboarding` is seeded as all steps DONE for `account_local` when the profile is already configured |
| `027_notifications_comms` | §17.1, §18.1; 6C notifications back-projected into `notifications` (dedupe by key) |
| `028_ops` | `audit_log`, `jobs`, `search_schedules`, `purge_in_progress`, `platform_controls`; append-only DELETE triggers recreated with the purge guard (`rate_limit_buckets` is created in `022`) |

Each migration also has a PostgreSQL body. The parity test covers both.

### 23.2 Existing data semantics

- No existing row's meaning changes.
- Approvals, fill runs and submission results keep their bindings and hashes.
- `standing-policy` v1 rows stay, and the current policy is re-saved as v2 with identical rules.
- `user-profile` v1 versions stay, and they're read as v2.

### 23.3 Local → hosted import (`python -m webapp.tools.import_local_account`)

- **Arguments:** `--sqlite PATH --profile-root PATH --documents-root PATH --target-dsn DSN --object-store CONFIG --email EMAIL --display-name NAME`.
- **Steps:**
  1. Create the hosted user (status ACTIVE, email verified by operator assertion `--assume-verified`) and the account.
  2. Copy every registry table's `account_local` rows in FK order, re-keying `account_id` and preserving ids, `seq` order and hashes.
  3. Upload blobs to the tenant prefix and rewrite `storage_key`.
  4. Load profile sources into `profile_source_revisions`.
  5. Write an import report.
- It's idempotent by refusing when the target account email already exists. `--dry-run` validates without writing.
- This is how the founder's current data moves into production.

## 24. Security and privacy summary

- Passwords argon2id; tokens and codes hashed; TOTP secret encrypted; no secrets in logs.
- Tenant isolation proven by the harness. Content never deduplicated across tenants.
- Admins have no content access (canary test).
- Minimum data sent to AI providers: extraction gets CV text only, and the existing minimization rules stay. Operator keys only.
- Consents recorded with wording version. Service vs marketing kept distinct. No marketing sending.
- Deletion is real (object store plus database) with explicit retained classes. Export is available.
- CSP, HSTS, CSRF, SameSite, rate limits.
- Webhooks verified and replay-safe. State derived from provider snapshots.
- Extension: least privilege (X1), short tokens, rotation with reuse detection, bound handoff tickets, storage allowlist.
- Live submit gated (§8.6).

## 25. Test strategy

### 25.1 Per area (focused runs during Bundle 7)

- **Pure product modules:**
  - `entitlements`, `cv_strategy`, `job_families` matcher, `standing_policy` v2, the subscription transition table, the plan catalog validator, the retention policy validator, the audit vocabulary;
  - property tests (hypothesis) for transitions and matcher determinism.
- **dbapi:** the placeholder translator (quoted `?`, `%`, comments), row mapping, error mapping, `BEGIN IMMEDIATE` translation, `DatabaseBusy`. It's run against both dialects.
- **Persistence:** new tables tested on SQLite by default and on PostgreSQL with `--db postgres` (conftest option; a fresh schema per test module, built with `CREATE DATABASE … TEMPLATE` from a migrated template for speed).
- **Schema parity** (§10.3), the **portable SQL lint** (§10.2), **tenant registry completeness** (§10.5), the **route auth-class declaration** test (A7).
- **Services and API** (TestClient with auth fixtures: `signed_in_client(user)`, `staff_client(role)`, `extension_client(device)`): auth, billing (fake provider, signed webhooks, out-of-order and duplicate events), usage (concurrency: 20 threads reserving the last unit → exactly one succeeds, on both dialects), library, strategy resolution, onboarding and proposals, rules in manual and automated modes, notifications and outbox, admin permissions matrix, deletion and purge (every registry table verified empty or pseudonymized or retained), export contents.
- **Cross-tenant harness** (§10.6), **never-gated** test (§11.5), **gate call-site** test (§11.4), **admin canary** test (§19.4).
- **Extension (vitest):** token client, storage allowlist, bridge origin, wipe on revoke. Production-build test: hosted build `host_permissions == [origin/*]` and permissions unchanged.

### 25.2 Integration during Bundle 7

After each phase, run that phase's tests plus a fixed smoke set:
- `tests/webapp/api/test_review_approval*.py`;
- `tests/webapp/services/test_human_submit_results.py`;
- `tests/webapp/api/test_submit_routes.py`;
- the fill service tests;
- `tests/test_extension_production_build.py`.

This catches cross-module breakage without the full historical suite.

### 25.3 Browser (Playwright)

- **Pairing + handoff** with the new tokens (local mode).
- **Release journey** (§25.4).
- The existing fill/submit acceptance files run once, at the end of Phase 2 (extension auth change), then again only in the final pass.

### 25.4 Release-journey suite (`tests/webapp/test_release_journey_browser.py`)

- **Setup:** local mode on PostgreSQL (`--db postgres`), `LocalFsObjectStore`, `FakeBillingProvider`, `ConsoleEmailProvider`, fake AI providers (deterministic fixtures), a fixture ATS origin, and the extension test-hook build.
- The test reads the verification link and the pairing code from the console-email outbox and the page. There are no backdoors.
- It walks §22.1 steps 1–15 and asserts:
  - the state after each step;
  - exactly one `applications.prepare` consumed;
  - the notifications created;
  - the approval binding's `document_version_id` equals the library version resolved by the family rule;
  - `SUBMITTED` with a recorded result.
- A second variant runs the Free-user journey to `ALLOWANCE_EXHAUSTED`.

### 25.5 Deferred to the final release pass (not run during Bundle 7)

- The full historical suite on SQLite.
- The full suite on PostgreSQL (`--db postgres`).
- All browser suites.
- The full extension suite.
- The dependency audit.

This is the single comprehensive regression covering 6E-A + Bundle 7 + the existing application flow. The asyncio shutdown warning seen at the 6E-A freeze is recorded for triage there.

## 26. Implementation sequencing

The phases run in order; within a phase, tasks run in order. One logical commit per task (more if a task says so). The plan (`docs/superpowers/plans/2026-09-29-bundle7-productization.md`) carries the task detail.

| Phase | Tasks | Theme |
|---|---|---|
| 0 | 1–6 | Foundations: deps and modes, dbapi and portable SQL, PostgreSQL baseline and parity, object store and profile source store, tenant registry and default-account removal, security middleware and observability |
| 1 | 7–10 | Identity: users, auth and sessions, CSRF and rate limits, audit log, mode-aware `AccountScope`, route classes, isolation harness |
| 2 | 11–12 | Extension hosted security: devices, tokens, pairing, tickets, extension client and storage hygiene, hosted build |
| 3 | 13–17 | Commercial core: catalog and entitlements, billing port and fake, webhooks and state machine, usage ledger and gate, metered AI and cost ceiling |
| 4 | 18–20 | Comms and notifications: jobs and worker, outbox and email and templates, notifications and inbox and preferences and consent |
| 5 | 21–26 | Product surfaces: CV library, job families and strategy, preferences and rules v2, onboarding and proposals and CV extraction, plans and billing and usage UI, scheduled discovery and Power automation wiring |
| 6 | 27–29 | Admin and ops: admin console and roles and controls, deletion and purge and retention and export, readiness and metrics and dead-letter tooling |
| 7 | 30–32 | Journey: local→hosted import tool, release-journey browser suite, docs, runbooks and release-readiness tool |

## 27. Acceptance criteria

### 27.1 Bundle 7 exit (checked at the end of Bundle 7, before the release pass)

1. The §22.1 journey passes automatically (§25.4) on PostgreSQL, with fake providers and fixture ATS.
2. The Free-allowance journey passes.
3. These tests pass: schema parity; portable SQL lint; tenant registry completeness; route auth classes; cross-tenant harness; never-gated; gate call-sites; admin canary.
4. Hosted mode starts with a complete configuration and refuses to start with each required setting missing (one test per setting).
5. No `DEFAULT_ACCOUNT_ID` default parameters remain (lint).
6. Extension hosted build has `host_permissions == [origin/*]`; permissions exactly as X1.
7. The 6E-A frozen semantics are intact: the §25.2 smoke set passes, and autonomous SUBMIT entry points still raise `SubmissionNotAvailable`.
8. `release_readiness` runs and lists exactly the open DPs.

### 27.2 Commercial release gate (the release pass; not Bundle 7 implementation)

1. The full comprehensive regression (§25.5) is green on both dialects.
2. Hosting provider chosen. Infrastructure provisioned: PostgreSQL (backups, PITR), object store (encryption at rest), web and worker processes, TLS, a secret manager, log shipping, alerting on `/ready`, DEAD jobs, webhook lag and the error reporter.
3. DP-1…DP-10 resolved, and `release_readiness` passes.
4. A real payment adapter and a real email adapter contract-tested against the vendors' test modes.
5. Hosted threat-model sign-off recorded, and a penetration-style review of §8.4 done.
6. At least one adapter `LIVE_CERTIFIED` for submit, with live evidence, to make hosted submit available. Otherwise the commercial journey launches with submit disabled, and that is an explicit launch decision.
7. The extension published with a production origin and store id.
8. A brand-new external person completes sign up → pay → onboard → CVs → rules → capture → prepare → review → fill → submit on the hosted service with no developer help, no Python, no environment variables and no API key.

### 27.3 `release_readiness` tool

`python -m webapp.tools.release_readiness --config <env-file>` reports, and exits non-zero on any failure:
- DP status: catalog `null`s, missing provider prices, retention `null`s, legal documents unpublished, no email or billing adapter, no production origin or extension id;
- `/ready` checks;
- the platform-control state;
- certification status per adapter.

## 28. Invariants carried forward (must remain true; each has an existing or new test)

1. Autonomous SUBMIT is closed (6E-B not started).
2. Human submit semantics are as frozen at `b042e0e` (§8.5).
3. Greenhouse is `FIXTURE_CERTIFIED`, and `submit_fixture_origins_enabled` requires loopback.
4. Standing policy can never grant. Entitlements and grants can never exceed the user's own authorization or the deployment ceilings.
5. Proposals (semantic, answers, CV import) never become truth without a recorded human resolution, or the existing system-confirmation rules where they already apply.
6. Evidence tables stay append-only. The only exception is the purge guard, and only inside a `purge_in_progress` transaction.
7. A missing or cross-owner resource gives the same result.
8. Review approval bindings are never rewritten.
