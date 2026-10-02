# Bundle 7: Hosted Productization (7A–7H) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the local single-user prototype into a hosted, multi-tenant product. A brand-new customer must be able to go through sign up → choose/pay → onboard → CVs → rules → capture → prepare → review → fill → submit with no developer help.

**Architecture:**
- **Foundations first:** PostgreSQL behind a sqlite3-shaped `dbapi` adapter; object and profile-source store ports; a tenant registry.
- **Identity next:** users, sessions, CSRF, audit, and a mode-aware `AccountScope`. Then hosted extension auth.
- **Commercial core:** a plan catalog → a pure entitlement resolver → a billing port with a fake adapter → a usage reservation ledger → metered AI.
- **Product surfaces** go on top: CV library and strategy, onboarding and proposals, preferences and rules, notifications, outbox.
- **Ops and admin:** worker and jobs, purge and export.
- **Finally** the release-journey suite and the import and readiness tools.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, SQLite (dev/tests) and PostgreSQL 18.1 (local; ≥ 15 required) via psycopg 3, argon2-cffi, pyotp, pypdf, boto3 (S3-compatible adapter only), TypeScript MV3 extension (esbuild, vitest), Playwright (Python).

**Spec:** `docs/superpowers/specs/2026-09-29-bundle7-productization-design.md` (commits `263fd74`, `6fb3377`). References §n / H/A/X/B/E/U/L/P/R/N/C/O-n point into it.

## Global Constraints

- **Branch and base:**
  - Base is `bundle6/6e-a-submit@b042e0e`; the branch is `bundle7/productization`, local only.
  - **Nothing is pushed.** No PR, no tag, no merge until the final release pass.
- **Do not modify 6E-A behavior:**
  - `extension/src/submit/*` semantics, `product/submit_*.py`, the 6D-B quarantine and allowlist, and the 6D-A binding format are frozen. Only the auth header and origin plumbing around them may change (Task 11/12).
  - Autonomous SUBMIT keeps raising `SubmissionNotAvailable`.
  - Greenhouse stays `FIXTURE_CERTIFIED` with `live_evidence=None`.
- **Modes:** `JOBSEARCH_DEPLOYMENT` ∈ {`local` (default), `hosted`}. Local mode must keep every existing test passing unchanged except for the explicitly listed edits: auth header, required `account_id` kwargs, and the `dbapi` imports.
- **PostgreSQL in tests:**
  - DSN `JOBSEARCH_TEST_PG_DSN` (default `postgresql://postgres@localhost:5432/postgres`).
  - PostgreSQL-only tests are skipped with the reason `"postgres unavailable: <error>"` when it can't be reached. They're never silently passed.
- **Commands:**
  - Python: `.venv/Scripts/python -m pytest <paths> -q -p no:cacheprovider`.
  - Extension: `npm --prefix extension test -- <pattern>` and `npm --prefix extension run build`.
- **Test scope (user instruction):** run each task's focused tests plus, at the end of each phase, the smoke set in spec §25.2. **Never run the full historical suite during Bundle 7.** That's the final release pass.
- **Memory:** run browser tests one file at a time. If commit headroom drops below 3 GB, pause and report.
- **Transitional writer lock (spec §10.7):** only the allowlisted `BEGIN IMMEDIATE` sites take it. The timeout is `JOBSEARCH_WRITER_LOCK_TIMEOUT_MS` (default 10000, range 1000–30000). New Bundle 7 code never adds a `BEGIN IMMEDIATE`; it uses `lock_account`, row locks or constraints.
- **Commercial gate (spec §27.2-6):** it is hard. Deployment-ready without a live-certified adapter is **not** the commercial release.
- **Technical constants (verbatim from the spec):**
  - **Sessions:** customer idle 7 d, absolute 30 d; staff idle 30 min, absolute 8 h.
  - **Email tokens:** VERIFY 48 h, RESET 1 h, EMAIL_CHANGE 24 h.
  - **Extension:** access token 10 min; refresh token 30 d sliding; pairing code 10 min; handoff ticket 5 min.
  - **Staff re-auth window:** 5 min.
  - **Password:** 12–128 characters, plus the top-10k list.
  - **Uploads:** ≤ 10 MiB, DOCX or PDF only.
  - **Usage reservation expiry:** 30 min.
  - **Rate limits (A8):**
    - signup: 5/h/IP;
    - login: 10/15 min/IP+email;
    - password reset: 5/h/email;
    - verification resend: 5/h/user;
    - pairing code: 10/h/user;
    - token refresh: 60/h/device;
    - AI-starting routes: 30/min/account.
  - **Outbox backoff:** 1 m, 5 m, 30 m, 2 h, 6 h, then FAILED.
  - **Jobs:** lease 60 s; backoff `min(2^attempts × 30 s, 6 h)`; `max_attempts` 8.
  - **Webhook timestamp tolerance:** 5 min.
  - **Data export:** 3/day, one at a time.
- **Business values (DP-1…DP-10) are never hard-coded.** Dev and test values live only in `product/plans/plan-catalog.dev.json` and `product/policies/retention-policy.dev.json`. The production files ship with `null`s.
- **Commit messages** end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Closed vocabularies** are copied verbatim from the spec: features §11.2, allowances §11.3, notification kinds §17.2, template ids §18.3, audit actions §20.1, job kinds §20.2, platform controls §19.3, error codes §21.3.

## Review Focus

1. **Duplicate or early webhooks.** A billing webhook can arrive for a customer or subscription the app hasn't recorded yet, and can arrive twice. Expectation: it's stored once, processing retries until the checkout row exists, it never crashes, and the state ends up correct. Test in Task 15.
2. **Double-click or concurrent Prepare** on the same workspace, and two tabs at once. Expectation: exactly one `applications.prepare` reservation and at most one consumed unit, even with 20 concurrent threads, on both dialects. Test in Task 16.
3. **Email variants.** `" Foo@Example.COM "` versus `foo@example.com`, including NFKC-equivalent Unicode. Expectation: same user; signup is refused as a duplicate through the uniform response; login works with either form. Test in Task 8.
4. **Mislabelled or oversize uploads.** A PDF named `.docx`, a zero-byte file, 10 MiB + 1 byte, a password-protected PDF. Expectation: a clear refusal code; no blob written; no library version created. Test in Task 21.
5. **Downgrade or suspension during Power automation.** A downgrade or suspension can land while a 6C autonomous prepare holds a reservation. Expectation: the in-flight step either completes and consumes or releases; nothing new starts; schedules become disabled, not deleted. Test in Task 26.

## File Map

| Path | Responsibility | Task |
|---|---|---|
| `requirements*.txt`, `webapp/config.py`, `webapp/deployment.py` | deps, modes, hosted config validation | 1 |
| `webapp/persistence/dbapi.py`, codemod across `webapp/`, `tests/test_portable_sql_lint.py` | connection adapter, portable SQL | 2 |
| `webapp/persistence/pg/0001_baseline.sql`, `webapp/persistence/pg/triggers.sql`, `webapp/persistence/db.py`, `tests/webapp/persistence/test_schema_parity.py`, `tests/conftest_pg.py` | PostgreSQL baseline, parity, test fixtures | 3 |
| `webapp/storage/object_store.py`, `webapp/storage/profile_sources.py`, `webapp/services/document_blob_store.py`, `product/profile_snapshot.py`, profile services | storage ports | 4 |
| `webapp/persistence/tenancy.py`, account-default removal across `webapp/` | tenant registry, required scoping | 5 |
| `webapp/security_middleware.py`, `webapp/observability.py`, templates and static (CSP) | headers, CSP nonces, request ids, logging redaction | 6 |
| `webapp/persistence/identity.py`, migration `023_identity`, `webapp/services/auth.py`, `webapp/api/auth.py`, auth templates | users, passwords, sessions, email tokens, legal | 7–8 |
| `webapp/services/rate_limit.py`, `webapp/services/csrf.py`, `webapp/persistence/audit.py`, `product/audit_actions.py` | CSRF, rate limits, audit | 9 |
| `webapp/api/dependencies.py`, `webapp/api/route_classes.py`, `tests/webapp/test_route_auth_classes.py`, `tests/webapp/test_cross_tenant_isolation.py` | mode-aware scope, route classes, isolation harness | 10 |
| migration `025_extension_devices`, `webapp/persistence/extension_devices.py`, `webapp/services/extension_auth.py`, `webapp/api/extension_auth.py`, `webapp/api/handoff.py` | devices, tokens, pairing, tickets | 11 |
| `extension/src/background/{credential-store,token-client,server-client}.ts`, `extension/src/fill/server.ts`, `extension/src/submit/server.ts`, `extension/src/content-bridge/*`, `extension/scripts/build.mjs`, `extension/src/popup/*` | extension auth client, storage hygiene, hosted build | 12 |
| `product/plans/*`, `product/entitlements.py`, `webapp/services/entitlements.py` | catalog, resolver, gate | 13 |
| migration `027_billing`, `webapp/billing/{port,fake,registry}.py`, `webapp/persistence/billing.py`, `webapp/api/billing.py`, `webapp/api/dev_billing.py` | billing port, fake provider, checkout | 14 |
| `product/subscription_state.py`, `webapp/services/billing_webhooks.py`, `webapp/api/webhooks.py` | state machine, webhook ingestion | 15 |
| `webapp/persistence/usage.py`, `webapp/services/usage.py`, gate call sites | reservation ledger | 16 |
| `webapp/services/metered_provider.py`, `product/policies/ai-pricing.v1.json`, provider wiring | metered AI, cost ceiling | 17 |
| migration `029_jobs`, `webapp/worker/*` | jobs, worker, schedule | 18 |
| migration `030_comms`, `webapp/comms/*`, `webapp/templates/email/**` | outbox, email port, templates, suppression, consent | 19 |
| migration `031_notifications`, `webapp/services/notifications.py`, `webapp/api/notifications.py`, `webapp/templates/inbox.html` | notifications, inbox, preferences | 20 |
| migration `032_cv_library`, `webapp/persistence/cv_library.py`, `webapp/services/cv_library.py`, `webapp/api/cv_library.py`, templates | CV library | 21 |
| `product/job_families.py`, `product/cv_strategy.py`, `product/cv_templates.py`, `webapp/services/cv_strategy.py`, pipeline hook | families, strategy, resolution | 22 |
| `product/user_profile.py` (v2), `product/standing_policy.py` (v2), `webapp/api/preferences.py`, `webapp/templates/preferences.html`, review integration | preferences and rules v2 | 23 |
| migration `035_onboarding`, `webapp/services/onboarding_v1.py`, `webapp/services/cv_import.py`, `product/cv_extraction*.py`, `webapp/api/onboarding_v1.py`, templates | onboarding, CV import, proposals | 24 |
| `webapp/templates/{plans,pricing,billing,usage}.html`, `webapp/api/views.py`, header meter | plan, billing and usage UI | 25 |
| `webapp/services/search_schedules.py`, 6C wiring in the worker | scheduled discovery, Power automation | 26 |
| `webapp/api/admin*.py`, `webapp/services/admin_read_models.py`, `webapp/templates/admin/**`, `webapp/tools/create_admin.py` | admin console | 27 |
| migration `037_purge`, `webapp/services/account_lifecycle.py`, `webapp/services/purge.py`, `webapp/services/export.py`, `product/policies/retention-policy*.json` | deletion, purge, retention, export | 28 |
| `webapp/api/ops.py` (`/ready`, `/metrics`), `webapp/observability.py` metrics | readiness, metrics | 29 |
| `webapp/tools/import_local_account.py` | local → hosted import | 30 |
| `tests/webapp/test_release_journey_browser.py`, `tests/webapp/test_free_journey.py` | journey suites | 31 |
| `webapp/tools/release_readiness.py`, `docs/runbooks/hosted-*.md`, `README`/`SETUP` updates | readiness tool, runbooks | 32 |

---

## Phase 0 — Foundations

### Task 1: Dependencies, deployment modes and hosted config validation

**Files:**
- Modify: `requirements.txt` (add `psycopg[binary]==3.2.*`, `argon2-cffi==23.1.*`, `pyotp==2.9.*`, `pypdf==5.*`, `boto3==1.35.*`), `webapp/config.py`, `webapp/app.py`.
- Create: `webapp/deployment.py`, `tests/webapp/test_deployment_modes.py`.

**Interfaces (Produces):**
- `Settings` gains:
  - `deployment: Literal["local","hosted"]` (`JOBSEARCH_DEPLOYMENT`);
  - `database_url: str | None` (`JOBSEARCH_DATABASE_URL`);
  - `secret_key: str | None`;
  - `public_origin: str | None`;
  - `extension_ids: tuple[str,...]`;
  - `object_store: dict` (`JOBSEARCH_OBJECT_STORE` JSON: `{"kind":"local","root":...}` | `{"kind":"s3","bucket","endpoint_url","region","prefix"}`);
  - `billing_provider: str` (`fake` | adapter name);
  - `email_provider: str` (`console` | `smtp` | adapter name);
  - `smtp: dict`;
  - `metrics_token: str | None`;
  - `plan_catalog_path: Path`;
  - `retention_policy_path: Path`.
- `webapp.deployment.validate_settings(settings) -> list[str]` returns the problems. `create_app` raises `DeploymentConfigError(problems)` if the list isn't empty.
- **Hosted rules:**
  - `database_url` starts with `postgresql://`;
  - `secret_key` is ≥ 32 bytes of entropy (base64/hex ≥ 43 characters);
  - `public_origin` starts with `https://` and has no path;
  - `extension_ids` is non-empty;
  - `object_store.kind == "s3"`;
  - `email_provider != "console"`;
  - the catalog file isn't `plan-catalog.dev.json`;
  - `host` isn't loopback-only-required (any host is allowed).
- **Local rules:** `host` must be loopback (`127.0.0.1`, `::1`, `localhost`).
- `Settings.is_hosted` property.
- In hosted mode `_start_autonomy_driver` returns `(None, None)`.

**Steps:**
- [ ] Tests:
  - the default Settings are local and valid;
  - local with `host="0.0.0.0"` gives the problem `local mode requires a loopback host`;
  - hosted with every field valid gives no problems;
  - a parametrized test drops each required hosted field in turn and asserts one specific problem string per field (§27.1-4);
  - hosted `create_app` doesn't start the autonomy driver (the driver status stays `{"running": False}`).
- [ ] Implement it, install the deps into `.venv`, and run `tests/webapp/test_deployment_modes.py tests/webapp/test_app_factory.py`.
- [ ] Commit `feat(platform): deployment modes with fail-closed hosted configuration`.

### Task 2: `dbapi` connection adapter and portable SQL

**Files:**
- Create: `webapp/persistence/dbapi.py`, `tests/webapp/persistence/test_dbapi.py`, `tests/test_portable_sql_lint.py`.
- Modify:
  - `webapp/persistence/db.py` (`connect` takes `Settings | Path` and returns `dbapi.Connection`);
  - a codemod across `webapp/**/*.py`: `import sqlite3` → `from webapp.persistence import dbapi` where the module only uses types and errors; `sqlite3.Connection` → `dbapi.Connection`; `sqlite3.IntegrityError` → `dbapi.IntegrityError`; `sqlite3.Row` → `dbapi.Row`;
  - rewrite the 13 non-migration SQLite idioms (`INSERT OR …` → `ON CONFLICT`, `lastrowid` → `RETURNING`, `json_extract` → Python).

**Interfaces (Produces):**
```python
class Connection(Protocol):
    dialect: Literal["sqlite", "postgres"]
    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor: ...
    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...
class IntegrityError(Exception): ...          # sqlite3.IntegrityError and psycopg.errors.IntegrityError both map here
class OperationalError(Exception): ...
class DatabaseBusy(OperationalError): ...     # PG 40001/40P01; SQLite "database is locked"
Row = Mapping[str, Any] & Sequence[Any]      # both dialects return rows supporting row["c"], row[0], dict(row), row.keys()
def connect(target: "Settings | Path | str") -> Connection
def translate_placeholders(sql: str) -> str   # '?'→'%s' outside quotes/comments; '%'→'%%'
def lock_account(conn: Connection, account_id: str) -> None
WRITER_LOCK_KEY: int = 0x4A53_0001
WRITER_LOCK_SITES: Mapping[str, int]   # webapp/persistence/writer_lock_sites.py — module → count of BEGIN IMMEDIATE literals (spec §10.7)
class DatabaseBusy(OperationalError): reason: Literal['writer_lock_timeout','serialization','deadlock','sqlite_locked']; site: str
```
- The SQLite implementation subclasses `sqlite3.IntegrityError` into the dbapi hierarchy with a wrapper: `class IntegrityError(sqlite3.IntegrityError)`. The SQLite path raises it by re-raising inside `execute`, so `except sqlite3.IntegrityError` in untouched code still works during the transition.
- On PostgreSQL:
  - `execute("BEGIN IMMEDIATE")` → `BEGIN ISOLATION LEVEL REPEATABLE READ` then `SELECT pg_advisory_xact_lock(WRITER_LOCK_KEY)`;
  - `BEGIN` → `BEGIN ISOLATION LEVEL REPEATABLE READ`;
  - an implicit transaction opens at REPEATABLE READ (psycopg `autocommit=False`, `isolation_level=REPEATABLE_READ`);
  - `PRAGMA foreign_keys|journal_mode|busy_timeout` is a no-op; any other `PRAGMA` raises `OperationalError("pragma not supported on postgres")`.
- Parameter names: psycopg is given `%s` only; mapping params are refused (they aren't used today — the lint asserts that).

**Steps:**
- [ ] Tests (`test_dbapi.py`, parametrized over both dialects, with PostgreSQL skipped if unavailable):
  - the placeholder translation vectors:
    - `SELECT '?' , ? FROM t WHERE a LIKE '%x%'` → `SELECT '?' , %s FROM t WHERE a LIKE '%%x%%'`;
    - `-- ?` comments are untouched;
    - `"col?"` quoted identifiers are untouched;
  - row access in every form;
  - a duplicate primary key raises `dbapi.IntegrityError`, which is also `isinstance` of `sqlite3.IntegrityError` on SQLite;
  - `BEGIN IMMEDIATE` on two PostgreSQL connections: the second blocks until the first commits (thread plus event, 2 s bound);
  - two REPEATABLE READ transactions updating the same row → one raises `DatabaseBusy(reason="serialization")`;
  - **writer-lock bound (spec §10.7):** with `writer_lock_timeout_ms=1000` and a holder that never releases, the waiter raises `DatabaseBusy(reason="writer_lock_timeout")` in 1000–3000 ms, rolls back and writes nothing;
  - wait/hold samples are recorded per site in an in-process `LOCK_STATS` (Task 29 exports them), and a wait over 1000 ms emits a `writer_lock_slow` log record;
  - the lint `tests/test_writer_lock_sites.py`: the per-module counts of `BEGIN IMMEDIATE` literals equal `WRITER_LOCK_SITES` exactly, and no allowlisted transaction block contains a call to an attribute named `propose`, `send`, `create_checkout`, `fetch_subscription` or `extract` (AST scan between the BEGIN and the next commit/rollback in the same function);
  - `RETURNING` works on both.
- [ ] `test_portable_sql_lint.py` AST-scans string constants in `webapp/**/*.py` (excluding `persistence/migrations.py`, `persistence/db.py`, `persistence/pg/`) for:
  - `INSERT OR`, `json_extract`, `json_each`, `strftime(`, `julianday(`, `datetime('now'`;
  - `.lastrowid`, `executescript(` (attribute uses);
  - `AUTOINCREMENT`, `GLOB`;
  - a named-param `:name` pattern inside `execute(` literals.
  
  It asserts zero hits.
- [ ] Run the codemod (a script in the scratchpad, not committed), then `tests/webapp/persistence tests/webapp/services -q -k "not browser"`, restricted to the modules the codemod touched (the git-diff name list) plus `test_dbapi.py`.
- [ ] Commit `feat(persistence): dbapi adapter (sqlite/postgres) and portable-SQL rewrite`.

### Task 3: PostgreSQL baseline schema, parity test and test fixtures

**Files:**
- Create:
  - `webapp/persistence/pg/0001_baseline.sql` (generated once by `webapp/tools/gen_pg_baseline.py` from a migrated SQLite database, then hand-reviewed and committed; the generator is committed too);
  - `webapp/persistence/pg/triggers.sql` (PL/pgSQL functions `jobsearch_append_only()` raising `'<table> is append-only'`, plus the per-table triggers matching the 115 SQLite triggers by table and event);
  - `tests/webapp/persistence/test_schema_parity.py`;
  - `tests/conftest.py` additions (`--db` option, the `pg_template` session fixture, `db_settings` fixture).
- Modify:
  - `webapp/persistence/db.py`: `init_db(settings)` dispatches by dialect; PostgreSQL runs the baseline and triggers when `schema_migrations` is absent, then the Bundle 7 migration bodies;
  - `webapp/persistence/migrations.py`: `apply_migrations(conn)` skips `001`–`021` on PostgreSQL (the baseline inserts those ids into `schema_migrations`); Bundle 7 migrations are declared as `Migration(id, sqlite=fn, postgres=fn, disable_fk=bool)`.

**Interfaces (Produces):**
- `init_db(settings_or_path)`.
- `@dataclass Migration(id: str, sqlite: Callable, postgres: Callable, disable_foreign_keys: bool = False)`.
- `BUNDLE7_MIGRATIONS: list[Migration]`. Later tasks append one migration each, `022_storage`…`037_purge`, in the order of spec §23.1. A committed migration is never edited.
- `schema_catalog(conn) -> dict` (a normalized structure for parity).
- Fixtures:
  - `db_settings` yields a `Settings` for the chosen dialect;
  - with `--db postgres`, each test module gets `CREATE DATABASE jobsearch_test_<uuid> TEMPLATE jobsearch_template`, dropped at teardown;
  - the template is built once per session.

**Steps:**
- [ ] The parity test builds SQLite (`init_db`) and PostgreSQL (`init_db`) and compares `schema_catalog`:
  - tables;
  - columns (name, order, nullability, affinity mapped per §10.3);
  - primary keys;
  - uniques and indexes (partial predicates normalized: lowercase, whitespace collapsed, quotes stripped);
  - foreign keys;
  - CHECK value sets (regex-extracted `IN (...)` lists);
  - append-only trigger coverage `{(table, event)}`.
  
  A diff prints a readable report.
- [ ] A seq and identity test: insert into an append-only table on PostgreSQL without `seq` → `seq` is assigned increasing; UPDATE and DELETE raise `IntegrityError` with the `append-only` message.
- [ ] Run the parity test, then `tests/webapp/persistence/test_migrations*.py` on SQLite and `--db postgres`, plus `test_review_approval*` persistence tests with `--db postgres` as a first real-code probe. Fix any adapter gaps found.
- [ ] Commit `feat(persistence): postgres baseline equal to migration 021, schema-parity test, dual-dialect fixtures`.

### Task 4: Object store and profile source store ports

**Files:**
- Create: `webapp/storage/__init__.py`, `webapp/storage/object_store.py`, `webapp/storage/profile_sources.py`, `tests/webapp/storage/test_object_store.py`, `tests/webapp/storage/test_profile_sources.py`.
- Modify:
  - `webapp/services/document_blob_store.py` (wraps `ObjectStore`; `storage_key(account_id, digest, media_type)`; the legacy key format stays readable);
  - `product/profile_snapshot.py` (`build_snapshot(root_or_reader, *, included_sources)`: accepts a `SourceReader` protocol `read(path) -> str | None`; a `Path`/`str` still works through `FilesystemSourceReader`);
  - `webapp/services/profile_manager.py`, `webapp/services/profile_setup.py`, `webapp/services/pipeline.py::refresh_profile`, `webapp/services/ownership.py` (use `scope.profile_sources`).

**Interfaces (Produces):**
```python
MEDIA_TYPES = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx", "application/pdf": "pdf"}
class ObjectStore(Protocol):
    def put(self, key: str, content: bytes) -> None: ...          # no-clobber; existing identical content OK, different → ObjectStoreError
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete_prefix(self, prefix: str) -> int: ...
class LocalFsObjectStore(ObjectStore): def __init__(self, root: Path)
class S3CompatibleObjectStore(ObjectStore): def __init__(self, *, bucket, endpoint_url, region, prefix, client=None)
def object_store_from_settings(settings) -> ObjectStore
def tenant_document_key(account_id: str, sha256: str, media_type: str) -> str  # accounts/{id}/documents/sha256/{aa}/{digest}.{ext}
class ProfileSourceStore(Protocol):
    def read(self, account_id: str, source_path: str) -> str | None: ...
    def write(self, account_id: str, source_path: str, text: str, *, expected_revision: int | None) -> int: ...
    def revision(self, account_id: str, source_path: str) -> int: ...
    def reader(self, account_id: str) -> SourceReader: ...
class FilesystemProfileSourceStore(ProfileSourceStore): def __init__(self, base_root: Path)   # account_profile_root semantics
class DatabaseProfileSourceStore(ProfileSourceStore): def __init__(self, conn_factory)       # profile_source_revisions
class ProfileSourceConflict(Exception): ...
```
- This task adds migration **`022_storage`**: `profile_source_revisions` and the `user_profile_versions.account_id` backfill.
- `DocumentBlobStore.publish(content, *, account_id, media_type)`; `read(document)` handles both key formats.

**Steps:**
- [ ] Tests:
  - LocalFs: put/get/exists; no-clobber (identical content OK, different content raises); path traversal (`../x`) refused; `delete_prefix` counts.
  - S3: a fake client (an in-memory dict implementing `put_object`/`get_object`/`head_object`/`list_objects_v2`/`delete_objects`) → the same contract.
  - `DocumentBlobStore`: the tenant key layout; the legacy `sha256/aa/d.docx` key still reads; integrity mismatch → `DocumentBlobError`.
  - Profile sources, on both backends:
    - write → revision 1 → a write with `expected_revision=0` → `ProfileSourceConflict`;
    - the reader feeds `build_snapshot`, giving the same snapshot as the filesystem root for the fixture profile.
- [ ] Run the new tests plus `tests/webapp/services/test_profile_manager*.py tests/webapp/api/test_profile_routes.py tests/test_profile_snapshot.py`.
- [ ] Commit `feat(storage): tenant-prefixed object store and profile source store ports`.

### Task 5: Tenant table registry and required account scoping

**Files:**
- Create: `webapp/persistence/tenancy.py`, `tests/webapp/persistence/test_tenancy_registry.py`, `tests/test_no_default_account_lint.py`.
- Modify: every function in `webapp/persistence/*.py` and `webapp/services/*.py` with `account_id: str = DEFAULT_ACCOUNT_ID` (73 sites) or `search_workspace_id: str = DEFAULT_SEARCH_WORKSPACE_ID` (27 sites). They become keyword-only required. Update every caller: routes pass `scope.account_id`; tests pass explicit ids through a `LOCAL = DEFAULT_ACCOUNT_ID` fixture constant.

**Interfaces (Produces):**
```python
@dataclass(frozen=True)
class TableSpec:
    owner: str | tuple[str, str] | Literal["GLOBAL"]   # "account_id" column | (parent_table, fk_column) | GLOBAL
    purge: Literal["DELETE", "PSEUDONYMIZE", "RETAIN", "GLOBAL"]
    retain_class: str | None = None                      # BILLING_FINANCIAL | SECURITY_AUDIT | CONSENT_PROOF | SUPPRESSION
    pseudonymize: tuple[str, ...] = ()
    export: bool = True
TENANT_TABLES: dict[str, TableSpec]
def account_rows_sql(table: str) -> tuple[str, list[str]]   # SELECT for one account's rows via owner path (joins resolved)
def purge_order() -> list[str]                               # children before parents (FK-topological)
```

**Steps:**
- [ ] Test:
  - every table from `schema_catalog` of both dialects (after all migrations present at that time) is in `TENANT_TABLES`, and every registry key exists;
  - owner paths resolve to `account_id` within ≤ 4 hops;
  - `purge_order` is a valid topological order with respect to foreign keys;
  - `current_user_profile` is `RETAIN`, local-only.
  
  Later migrations (Tasks 7–28) must add their tables here. This test enforces it.
- [ ] Lint test: an AST scan finds no parameter default equal to the name `DEFAULT_ACCOUNT_ID` or `DEFAULT_SEARCH_WORKSPACE_ID` in `webapp/`.
- [ ] Refactor, then run the lint test, the registry test, and `tests/webapp/test_account_ownership.py tests/webapp/api tests/webapp/services -q -k "not browser"`. That's the focused set this refactor touches; it's large but necessary, because the signature change is cross-cutting.
- [ ] Commit `refactor(tenancy): tenant table registry; account and search-workspace scope are always explicit`.

### Task 6: Security headers, CSP nonces, request ids, structured logging

**Files:**
- Create: `webapp/security_middleware.py`, `webapp/observability.py`, `tests/webapp/test_security_headers.py`, `tests/webapp/test_observability.py`.
- Modify: `webapp/app.py` (install the middleware; `app.state.templates.env.globals["csp_nonce"]` via request state); every template with inline `<script>` or `on*=` handlers (move them to `webapp/static/*.js` or add `nonce="{{ csp_nonce() }}"`); `webapp/static/app.js` (event delegation replacing the inline handlers).

**Interfaces (Produces):**
- `SecurityHeadersMiddleware(app, *, hosted: bool)` sets the §20.7 headers, with HSTS in hosted mode only.
- `request.state.csp_nonce: str` (16 random bytes, base64).
- `RequestContextMiddleware` sets `request.state.request_id` (a valid inbound UUID `X-Request-ID`, else `uuid4`), echoes it in the response header, and logs one JSON line per request: `{ts, level, request_id, method, route, status, duration_ms, account_id?}`.
- `redact(obj) -> obj` (recursive over the §20.6 keys, case-insensitive).
- `get_logger(name)` emits JSON.
- `ErrorReporter` protocol with `LogErrorReporter`.
- The unhandled-exception handler returns a 500 page or JSON containing `request_id` only.

**Steps:**
- [ ] Tests:
  - every HTML page response (enumerate `GET` views with fixtures) carries the CSP with a nonce, and no template's rendered HTML contains `<script>` without that nonce or an `on[a-z]+=` attribute (regex over the rendered pages);
  - the headers are exact;
  - an inbound `X-Request-ID: not-a-uuid` is replaced;
  - `redact({"Authorization": "x", "nested": [{"refresh_token": "y"}]})` masks both;
  - a forced exception route returns a 500 with a `request_id` and no traceback text.
- [ ] Run the new tests plus `tests/webapp/api/test_views.py tests/webapp/test_browser_smoke.py` (one browser smoke to confirm the pages still work under CSP).
- [ ] Commit `feat(security): security headers, strict CSP with nonces, request ids and redacted JSON logs`.

**Phase 0 exit:** run the §25.2 smoke set on SQLite and on `--db postgres`.

---

## Phase 1 — Identity

### Task 7: Users, identities, accounts lifecycle, sessions (data and services)

**Files:**
- Modify: `webapp/persistence/migrations.py` (add `023_identity` per spec §23.1), `webapp/persistence/tenancy.py`.
- Create: `webapp/persistence/identity.py`, `webapp/services/passwords.py`, `webapp/services/sessions.py`, `product/common_passwords.txt` (the top-10k list, one per line, lowercase), `tests/webapp/persistence/test_identity.py`, `tests/webapp/services/test_passwords.py`, `tests/webapp/services/test_sessions.py`.

**Interfaces (Produces):**
```python
def normalize_email(raw: str) -> str            # strip, NFKC, casefold whole address; must contain one '@' with non-empty parts, ≤ 254 chars → else ValueError
def create_user_with_account(conn, *, email, password_hash, display_name, legal_document_ids, now) -> dict  # §6.3 in one transaction, no commit
def get_user_by_email(conn, email_normalized) -> dict | None
def get_owner_account(conn, user_id) -> dict | None
def set_user_status(conn, user_id, status, *, now) -> None
def hash_password(pw: str) -> str; def verify_password(hash: str, pw: str) -> bool; def password_problems(pw: str) -> list[str]
DUMMY_PASSWORD_HASH: str
class SessionService:
    def create(self, conn, *, user_id, kind: Literal["CUSTOMER","STAFF"], now, ip, user_agent) -> tuple[str, str]  # (session_id, csrf_token) raw values
    def resolve(self, conn, session_id, *, now) -> dict | None       # checks idle/absolute/revoked; touches last_seen_at at most once per 60 s
    def revoke(self, conn, session_id, *, reason) -> None
    def revoke_all_for_user(self, conn, user_id, *, reason, except_session_id=None) -> int
def issue_email_token(conn, *, user_id, purpose, now) -> str; def consume_email_token(conn, *, raw, purpose, now) -> dict | None
```
- Tokens and session ids are `secrets.token_urlsafe(32)`, stored as SHA-256 hex.

**Steps:**
- [ ] Tests:
  - **Review Focus 3:** `normalize_email(" Foo@Example.COM ") == normalize_email("foo@example.com")`, and the fullwidth `ｆｏｏ@example.com` normalizes to the same; a second `create_user_with_account` with an equivalent email → `IntegrityError`.
  - `create_user_with_account` creates every §6.3 row: assert the membership, the profile workspace, the default search workspace, the profile source revision 1, the policy docs rows (created by later tasks — here assert through a registered `ACCOUNT_BOOTSTRAP_HOOKS` list that later tasks append to), the acceptances, and the `VERIFY_EMAIL` token.
  - Password checks: `password_problems("short")` → `["too_short"]`; a common password → `["too_common"]`; 129 characters → `["too_long"]`; argon2 verify works.
  - Sessions: idle expiry at +7 d + 1 s; absolute expiry at +30 d even with activity; STAFF idle expiry at +30 min; revoke-all except current.
  - Email tokens: single use; expiry per purpose; a token for the wrong purpose → None.
  - Both dialects.
- [ ] Commit `feat(identity): users, memberships, sessions, email tokens and password policy`.

### Task 8: Auth routes and pages

**Files:**
- Create: `webapp/services/auth.py`, `webapp/api/auth.py`, `webapp/templates/auth/{signup,login,verify_email,reset_request,reset_confirm,check_email}.html`, `tests/webapp/api/test_auth_routes.py`.
- Modify: `webapp/app.py` (the router), `webapp/templates/base.html` (signed-in or signed-out nav; the account menu), `webapp/api/views.py` (`/` shows the public landing when signed out in hosted mode).

**Interfaces (Produces):**
- `AuthService.signup(...) -> None` (always a uniform result).
- `login(conn, email, password, ...) -> session | None`.
- `verify_email`, `request_password_reset`, `confirm_password_reset`, `change_password`, `request_email_change`, `confirm_email_change`, `logout`, `revoke_all`.
- Route behavior per §7. Cookie `__Host-js_session` in hosted mode; in local mode with auth on for tests, `js_session` without `__Host-` over http.
- **Test-only switch:** `Settings.auth_required_in_local: bool = False`. With it on, local mode behaves like hosted auth for tests and the journey (SQLite/PostgreSQL, http). Hosted always requires auth.
- Outbox enqueue calls go through `comms.enqueue(...)` (Task 19). Until Task 19 lands, a `webapp/comms/__init__.py` stub `enqueue()` records into an in-memory list. Task 19 replaces the stub with the real implementation, and its tests assert the same call sites.

**Steps:**
- [ ] Tests (TestClient, `auth_required_in_local=True`):
  - signup → 200 uniform page; a duplicate signup → the identical body plus an `auth.account_exists` enqueue;
  - login before verification → a session with restricted access (A3); `POST /api/...` AI routes → `EMAIL_NOT_VERIFIED`;
  - verify → `ACTIVE`;
  - login with a wrong password and with an unknown email → the same response, and a timing bound (both call argon2 verify; assert `verify_password` was called once in both paths by spying);
  - password reset flow revokes the other sessions;
  - change email: the old address is notified, the change applies on confirm;
  - logout clears the cookie;
  - **Review Focus 3:** login with `" FOO@example.com"` succeeds.
  - Hosted-auth signup with no published `legal_documents`, or with `SIGNUPS_ENABLED=false`, → `SIGNUP_UNAVAILABLE`, and no user row is created.
- [ ] Commit `feat(auth): signup, verification, login, reset, email change and logout`.

### Task 9: CSRF, rate limiting and the audit log

**Files:**
- Create: `webapp/services/csrf.py`, `webapp/services/rate_limit.py`, `webapp/persistence/audit.py`, `product/audit_actions.py`, `tests/webapp/services/test_csrf.py`, `tests/webapp/services/test_rate_limit.py`, `tests/webapp/persistence/test_audit.py`.
- Modify: `webapp/persistence/migrations.py` (add `024_audit`), `webapp/services/auth.py` (audit and rate-limit calls), templates (a hidden `csrf_token` field in every form), `webapp/static/app.js` (sends `X-CSRF-Token` from `<meta name="csrf-token">` on every non-GET fetch).

**Interfaces (Produces):**
```python
def require_csrf(request) -> None   # FastAPI dependency; unsafe methods on cookie-auth requests; checks token (constant-time) + Origin/Referer == public_origin (local: request host origin)
def hit(conn, *, key: str, limit: int, window_seconds: int, now) -> RateDecision  # RateDecision(allowed: bool, retry_after: int)
RATE_LIMITS: dict[str, tuple[int, int]]  # name → (limit, window_seconds), A8 verbatim
def audit(conn, *, actor_type, actor_id, account_id, action, target_type=None, target_id=None, request=None, detail=None, now) -> None  # no commit
AUDIT_ACTIONS: frozenset[str]  # §20.1 verbatim
```

**Steps:**
- [ ] Tests:
  - a POST with no token → 403 `CSRF_FAILED`; a wrong Origin → 403; a correct token → OK; a bearer-auth request (no cookie) is exempt;
  - rate limiting on both dialects: 5 signups → the 6th returns 429 `RATE_LIMITED` with `Retry-After`; a window roll-over resets; concurrency (10 threads → exactly `limit` allowed);
  - audit: an unknown action → ValueError; rows are append-only (UPDATE raises); `ip_hash` is an HMAC, not the raw IP.
- [ ] Commit `feat(security): CSRF, database-backed rate limits and append-only audit log`.

### Task 10: Mode-aware `AccountScope`, route auth classes, cross-tenant isolation harness

**Files:**
- Create: `webapp/api/route_classes.py`, `tests/webapp/test_route_auth_classes.py`, `tests/webapp/test_cross_tenant_isolation.py`, `tests/webapp/factories.py` (a complete object-graph factory per account).
- Modify: `webapp/api/dependencies.py`, `webapp/services/ownership.py` (`AccountScope(account_id, user_id, profile_sources, object_store)`), every router module (declare its class through `APIRouter(dependencies=[Depends(route_class.USER)])` etc.), `webapp/app.py`.

**Interfaces (Produces):**
```python
class RouteClass: PUBLIC, USER, EXTENSION, ADMIN, WEBHOOK, METRICS   # each a dependency callable, marker attribute `route_class`
def get_account_scope(request, conn) -> AccountScope     # §6.2; local mode unchanged unless auth_required_in_local
def require_verified(scope) -> AccountScope; def require_active(scope) -> AccountScope
def restricted_scope(request, conn) -> AccountScope      # SUSPENDED / DELETION_REQUESTED accounts: only §11.5 routes use this
```

**Steps:**
- [ ] Route-class test: for every route in `create_app().routes` (excluding static mounts), exactly one route-class marker is present among its dependencies. Print any offenders.
- [ ] Isolation harness per §10.6:
  - factories build A and B, each with a complete graph (workspace, artifacts, document versions, approvals, fill run, submit attempt, notifications and devices once those tables exist; the factory grows with later tasks, and its docstring lists the tables it covers);
  - a row-hash snapshot of A before and after;
  - it walks every USER/EXTENSION route with A's ids while authenticated as B;
  - opt-outs have a reason, with a maximum of 5.
- [ ] Run the harness, the route-class test, and `tests/webapp/api` (focused: the routers' dependency change touches every router).
- [ ] Commit `feat(tenancy): authenticated account scope, declared route classes and cross-tenant isolation harness`.

**Phase 1 exit:** the smoke set (both dialects) plus `tests/webapp/test_account_ownership.py`.

---

## Phase 2 — Extension hosted security

### Task 11: Devices, tokens, pairing codes, handoff tickets (server)

**Files:**
- Modify: `webapp/persistence/migrations.py` (`025_extension_devices`: §9.1; revoke all rows in `extension_credentials` and `pairing_secrets`), `webapp/persistence/tenancy.py`, `webapp/api/handoff.py` (the session routes use `get_extension_scope`; the ticket is checked on `POST /sessions`), `webapp/api/fill_extension.py`, `webapp/api/submit_extension.py` (dependency swap only), `webapp/services/handoff.py` (remove `exchange_pairing_secret_for_credential` and `resolve_account_scope_from_extension_credential`; keep the persistence functions for migration reads).
- Create: `webapp/persistence/extension_devices.py`, `webapp/services/extension_auth.py`, `webapp/api/extension_auth.py`, `webapp/templates/settings/extension.html`, `tests/webapp/services/test_extension_auth.py`, `tests/webapp/api/test_extension_auth_routes.py`.

**Interfaces (Produces):**
```python
def create_pairing_code(conn, *, account_id, user_id, now) -> tuple[str, str]   # (code_id, raw 10-char Crockford base32 code shown to user)
def pair_device(conn, *, raw_code, device_label, now) -> PairResult              # PairResult(device_id, access_token, access_expires_at, refresh_token, account_label)
def refresh(conn, *, device_id, raw_refresh, now) -> TokenPair                   # raises DeviceRevoked on reuse (revokes family + device, audit, notify)
def resolve_access(conn, raw_access, *, now) -> ExtensionPrincipal | None        # (device_id, user_id, account_id)
def revoke_device(conn, *, device_id, reason, now) -> None
def revoke_all_devices_for_user(conn, user_id, *, reason, now) -> int
def issue_handoff_ticket(conn, *, account_id, user_id, workspace_id, purpose, now) -> str   # "v1.<id>.<nonce>.<hmac>"
def consume_handoff_ticket(conn, raw, *, principal, workspace_id, purpose, now) -> None     # raises TicketInvalid | AccountMismatch
def get_extension_scope(request, conn) -> ExtensionScope   # AccountScope + device_id; 401 DEVICE_REVOKED / 403 ACCOUNT_*
```
- `auth.change_password`, `confirm_password_reset`, suspension and deletion call `revoke_all_devices_for_user`.

**Steps:**
- [ ] Tests:
  - pairing code: single use; expires at 10 min; bound to its account.
  - Refresh rotates; presenting the old refresh token again → `DeviceRevoked`, the device revoked, the audit row `EXTENSION_TOKEN_REUSE`, and the `security.token_reuse_detected` notification enqueued. Until Task 20, `webapp/services/notifications.py` is a stub whose `notify(conn, *, account_id, kind, subject_type, subject_id, dedupe_key, detail, now)` records into an in-memory list. Task 20 replaces it with the real function, keeping the same signature.
  - An access token expires at 10 min.
  - Ticket: a wrong user → `AccountMismatch`; replay → `TicketInvalid`; a tampered HMAC → `TicketInvalid`; expiry at 5 min.
  - Password change revokes devices.
  - A legacy `X-Handoff-Credential` header → 401.
  - Update the existing `tests/webapp/api/test_handoff_routes.py`, `test_fill_routes.py` and `test_submit_routes.py` fixtures to use an `extension_client(device)` fixture (bearer). Assertions unchanged.
- [ ] Run the new tests plus those three updated files.
- [ ] Commit `feat(extension-auth): device credentials with rotating refresh tokens, bound pairing codes and handoff tickets`.

### Task 12: Extension client, storage hygiene, bridge origin, hosted build

**Files:**
- Create: `extension/src/background/token-client.ts`, `extension/test/token-client.test.ts`, `extension/test/storage-hygiene.test.ts`.
- Modify:
  - `extension/src/background/credential-store.ts` (stores `{deviceId, refreshToken, accountLabel}` in local storage and the access token in session storage);
  - `extension/src/background/server-client.ts`, `extension/src/fill/server.ts`, `extension/src/submit/server.ts` (the `Authorization: Bearer` header from `TokenClient.accessToken()`; on 401 `DEVICE_REVOKED` → `onRevoked`);
  - `extension/src/background/index.ts` (`BASE_URL`/`JOBSEARCH_WEBAPP_ORIGIN` from the build constant `__JOBSEARCH_ORIGIN__`; the revocation handler runs the existing restore/abort path, then wipes);
  - `extension/src/content-bridge/*` (origin check against the constant; forwards `handoff_ticket`);
  - `extension/src/popup/pairing-form.ts` (the new pair endpoint; shows `accountLabel`; a "Sign out" button);
  - `extension/scripts/build.mjs` (`--origin` argument, default `http://127.0.0.1:8420`; `host_permissions = [origin + "/*"]`; hosted builds refuse a non-`https` origin; output directory `dist-hosted/` when an origin is given);
  - `tests/test_extension_production_build.py` (a hosted build case).

**Interfaces (Produces):**
- `class TokenClient { accessToken(): Promise<string>; pair(code, label): Promise<void>; signOut(): Promise<void>; onRevoked(cb): void }`. It serializes concurrent refreshes (a single in-flight promise).
- `STORAGE_LOCAL_KEYS = ["handoff_device", "fill_executor_instance_id", "handoff_events_v1" /* existing queue key name kept */]`.
- `STORAGE_SESSION_KEYS` = the access token plus the existing run-record key prefixes.

**Steps:**
- [ ] Vitest:
  - concurrent `accessToken()` calls during expiry → exactly one refresh request;
  - refresh 401 → `onRevoked` fired once;
  - `signOut` removes every local and session key;
  - storage hygiene: run a fake pairing plus a fill-record write, then assert the local storage keys ⊆ `STORAGE_LOCAL_KEYS` and that no stored value contains a fixture CV byte signature (`PK\x03\x04` base64) or a profile canary string;
  - the bridge ignores messages whose `event.origin !== ORIGIN`.
- [ ] Python production-build test: the default build has `host_permissions == ["http://127.0.0.1:8420/*"]`; the `--origin https://app.example.test` build has `["https://app.example.test/*"]`; the permissions list equals X1 in both; `--origin http://evil` exits non-zero.
- [ ] Run `npm --prefix extension test`, the build, `tests/test_extension_production_build.py`, then one browser file at a time: `tests/webapp/test_extension_pairing_acceptance.py`, `tests/webapp/test_fill_acceptance_browser.py`, `tests/webapp/test_submit_acceptance_browser.py` (the one planned run of these after the auth change, §25.3).
- [ ] Commit `feat(extension): bearer device auth with single-flight refresh, storage allowlist, bound bridge and hosted-origin build`.

**Phase 2 exit:** the smoke set.

---

## Phase 3 — Commercial core

### Task 13: Plan catalog, entitlement resolver, `EntitlementGate`

**Files:**
- Create: `product/plans/plan-catalog.schema.json`, `product/plans/plan-catalog.v1.json` (the production file: every allowance `null`, prices `null`, `trial_days` `0` for free and `null` for paid), `product/plans/plan-catalog.dev.json` (test numbers), `product/entitlements.py`, `webapp/services/entitlements.py`, `tests/product/test_entitlements.py`, `tests/product/test_plan_catalog.py`.
- Modify: `webapp/persistence/migrations.py` (add `026_entitlements`: `plan_catalog_versions`, `entitlement_grants`, `platform_controls`), `webapp/persistence/tenancy.py`.

**Interfaces (Produces):**
```python
FEATURES: tuple[str, ...]      # §11.2 verbatim
ALLOWANCES: tuple[str, ...]    # §11.3 verbatim
HIDDEN_ALLOWANCES = frozenset({"ai.cost_micro_usd"})
GAUGE_ALLOWANCES = frozenset({"library.cv_items", "storage.bytes", "discovery.scheduled_searches"})
def load_catalog(path) -> Catalog   # validates; CatalogError(list[str])
@dataclass(frozen=True) class Entitlements: plan_id; catalog_version; features: Mapping[str,bool]; allowances: Mapping[str,int]; window: Window; source: str
@dataclass(frozen=True) class Window: key: str; start: datetime; end: datetime
@dataclass(frozen=True) class SubscriptionView: state: str; plan_id: str; catalog_version: str; current_period_start: datetime; current_period_end: datetime; past_due_since: datetime | None   # read from a subscriptions row (Task 14 writes them)
def effective_entitlements(catalog, snapshot: SubscriptionView | None, grants: Sequence[Grant], controls: Mapping[str,bool], *, grace: timedelta, now: datetime) -> Entitlements
class FeatureNotInPlan(Exception): feature; plan_id; upgrade_to: str | None
class EntitlementGate:
    def entitlements(self, conn, scope, *, now) -> Entitlements
    def require_feature(self, conn, scope, feature, *, now) -> Entitlements
PLATFORM_CONTROL_KEYS  # §19.3 verbatim
def platform_control(conn, key, *, settings) -> bool   # fail-closed defaults per §19.3
```
- A `null` allowance resolves to `0`.

**Steps:**
- [ ] Catalog tests:
  - the production file loads with every allowance `null`;
  - the validator rejects `-1`, `"unlimited"`, a missing feature key, a non-increasing rank, and paid prices on free;
  - hosted mode refuses the dev file (the Task 1 rule, re-asserted).
- [ ] Resolver tests (table-driven):
  - no subscription → free; `ACTIVE` pro → pro;
  - `PAST_DUE` within grace → pro, beyond grace → free;
  - `CANCEL_SCHEDULED` → pro until the period end;
  - `UNKNOWN` → free;
  - a `PLAN_OVERRIDE` grant to power with expiry → power until expiry;
  - an `ALLOWANCE_BONUS` adds within its window only;
  - a grant never lowers;
  - `AI_ENABLED=false` → `ai.prepare`, `ai.cv_tailor` and `profile.cv_import` are false;
  - Free window = calendar month UTC, paid = the snapshot period;
  - a pinned catalog version is honoured.
  - A hypothesis property: the resolved features ⊆ the union of the plan's features and the granted plan's features.
- [ ] Commit `feat(entitlements): versioned plan catalog and pure entitlement resolver with fail-closed controls`.

### Task 14: Billing port, fake provider, checkout and portal

**Files:**
- Modify: `webapp/persistence/migrations.py` (add `027_billing`), `webapp/persistence/tenancy.py`.
- Create: `webapp/billing/port.py`, `webapp/billing/fake.py`, `webapp/billing/registry.py`, `webapp/persistence/billing.py`, `webapp/services/billing.py`, `webapp/api/billing.py`, `webapp/api/dev_billing.py`, `webapp/templates/dev_billing_checkout.html`, `tests/webapp/services/test_billing_checkout.py`.

**Interfaces (Produces):**
- The §12.2 `BillingProvider` protocol, verbatim.
- Normalized types: `CheckoutRef(provider_session_id, url)`, `SubscriptionSnapshot(provider_subscription_id, provider_customer_id, status, plan_id, interval, current_period_start, current_period_end, cancel_at_period_end, raw_hash)`, `ProviderEvent(provider_event_id, type, subscription_id | None, customer_id | None, occurred_at, raw)`.
- `FakeBillingProvider(state_path: Path | None, secret: bytes, clock)` with `simulate(session_id, outcome: Literal["pay","fail","cancel"])`. It posts a signed webhook to the app through an injected `deliver(headers, body)` callable (TestClient in tests, HTTP in the journey).
- `BillingService.start_checkout(conn, scope, *, plan_id, interval, now) -> str (redirect url)`, `portal_url`, `change_plan`, `cancel`, `resume`, `status(conn, scope) -> dict`.
- Refusals:
  - no provider price (DP-1/DP-3 unresolved) → `PLAN_UNAVAILABLE` (409);
  - the `free` plan → no provider call; the account stays free.

**Steps:**
- [ ] Tests:
  - checkout creates a `checkout_sessions OPEN` row and `billing_customers` once (idempotent across two clicks);
  - the dev billing route is absent when `deployment=hosted`;
  - a paid plan with a `null` price → `PLAN_UNAVAILABLE`;
  - `change_plan` upgrade → `when="now"`, downgrade → `"period_end"`;
  - `cancel` → at period end.
- [ ] Commit `feat(billing): provider-neutral billing port, fake provider and checkout/portal flows`.

### Task 15: Subscription state machine and webhook ingestion

**Files:**
- Create: `product/subscription_state.py`, `webapp/services/billing_webhooks.py`, `webapp/api/webhooks.py`, `tests/product/test_subscription_state.py`, `tests/webapp/services/test_billing_webhooks.py`.

**Interfaces (Produces):**
```python
STATES = ("INCOMPLETE","TRIALING","ACTIVE","PAST_DUE","CANCEL_SCHEDULED","ENDED","INCOMPLETE_EXPIRED","UNKNOWN")
def transition(current: str | None, snapshot: SubscriptionSnapshot, *, now) -> tuple[str, dict]   # (new_state, field updates incl. past_due_since)
def ingest_webhook(conn, provider, *, headers, body, now) -> str          # verify → store raw (idempotent) → enqueue billing.webhook.process; returns event row id
def process_webhook_event(conn, event_row_id, *, provider, now) -> None   # fetch snapshot → transition → subscriptions upsert + subscription_events + notify + audit
```
- `POST /webhooks/billing/{provider}`:
  - a bad signature → 400 with nothing stored except a `billing_webhook_events` row with `signature_verified=0` (for ops visibility; the payload is truncated to 4 KiB);
  - a valid one → 200 quickly.
- Until the worker (Task 18) exists, processing runs synchronously after commit in tests via `process_pending_webhooks(conn)`. Task 18 moves it onto the job.

**Steps:**
- [ ] State table tests: every (current, snapshot status) pair; duplicates are no-ops; out-of-order (a `PAYMENT_FAILED` event processed after a later success) still ends `ACTIVE`, because the snapshot is the source of truth.
- [ ] **Review Focus 1:** a webhook for an unknown `provider_subscription_id` whose customer maps to no account → the event is stored, processing raises `RetryLater`, and the row keeps `processed_at NULL` with `attempts` incremented. After `checkout_sessions`/`billing_customers` exist, reprocessing succeeds. The same event delivered twice → one row.
- [ ] Notifications and outbox: `billing.payment_failed` on the transition into `PAST_DUE`; `billing.subscription_changed` on a plan change; `billing.subscription_canceled` on `ENDED`.
- [ ] Commit `feat(billing): snapshot-derived subscription state machine and idempotent verified webhooks`.

### Task 16: Usage reservation ledger and gate call sites

**Files:**
- Modify: `webapp/persistence/migrations.py` (add `028_usage`), `webapp/persistence/tenancy.py`, the gate call sites of §11.4:
  - `webapp/services/pipeline.py` (understanding, fit, intelligence);
  - `webapp/services/cv_generation_*` (tailor, added in Task 22);
  - `webapp/services/discovery.py`;
  - `webapp/services/fill_runs.py` (feature only);
  - `webapp/services/human_submit.py` (feature only, at authorization);
  - `webapp/api/application_documents.py` (upload: storage gauge).
- Create: `webapp/persistence/usage.py`, `webapp/services/usage.py`, `webapp/api/usage.py`, `tests/webapp/services/test_usage.py`, `tests/webapp/test_gate_call_sites.py`, `tests/webapp/test_never_gated.py`.

**Interfaces (Produces):**
```python
class AllowanceExhausted(Exception): allowance; used; limit; window_end
class UsageService:
    def reserve(self, conn, scope, *, allowance, amount=1, subject_type, subject_id, idempotency_key, now) -> Reservation  # no commit
    def consume(self, conn, reservation_id, *, settlement_ref, now) -> None
    def release(self, conn, reservation_id, *, now) -> None
    def gauge_check(self, conn, scope, *, allowance, adding: int, now) -> None   # raises AllowanceExhausted
    def summary(self, conn, scope, *, now) -> list[dict]                         # visible allowances: used/limit/window_end
def prepare_key(workspace_id: str, window_key: str) -> str   # "prepare:{ws}:{window}"
```
- `GET /api/usage` returns the summary.
- Errors map to the §21.3 codes via one exception handler in `webapp/api/errors.py` (created here).

**Steps:**
- [ ] Tests:
  - reserve/consume/release transitions, including illegal ones through the trigger;
  - `null` limit → exhausted at 0;
  - idempotency returns the same reservation;
  - an expired reservation is released by `sweep_expired(conn, now)`;
  - window boundaries (the last second of the month vs the first second of the next);
  - **Review Focus 2:** 20 threads call the prepare entry for the same workspace → one reservation row, one `CONSUMED`; separately, 20 threads for 20 different workspaces with 1 unit left → exactly 1 succeeds. Both dialects.
  - A stage failure releases, and the next successful stage in the same window consumes once.
- [ ] Gate call-site test: monkeypatch `EntitlementGate.require_feature` and `UsageService.reserve` to record the calls; invoke each §11.4 entry point with fakes; assert the expected (feature, allowance) per entry.
- [ ] Never-gated test: an account on a plan with every feature false and every allowance 0, plus `PAST_DUE` beyond grace, can still call each §11.4/§11.5 route listed (review, approve, revoke, dispositions, resolve ambiguity, cancel before dispatch, kill switch, notifications, preferences, export, delete, sign out, device revoke, billing portal) and gets a non-402 status.
- [ ] Commit `feat(usage): reservation ledger, allowance enforcement at every gated entry point, never-gated safety surfaces`.

### Task 17: Metered AI providers and the cost ceiling

**Files:**
- Create: `webapp/services/metered_provider.py`, `product/policies/ai-pricing.v1.json` (a model → {input_per_mtok_micro_usd, output_per_mtok_micro_usd} table; operator values; the dev file has test values), `tests/webapp/services/test_metered_provider.py`.
- Modify: `webapp/services/autonomy_providers.py`, the provider construction in the routes (`webapp/api/workspaces.py`, `webapp/api/review.py`, or wherever the providers are instantiated; locate with `grep -rn "Provider(" webapp/api`), and the three OpenAI providers (surface `usage` from responses into the returned audit metadata without changing their outputs).

**Interfaces (Produces):**
- `MeteredProvider(inner, *, conn_factory, scope, subject_type, subject_id, pricing, ceiling_micro_usd, clock)` implements the same protocol as `inner`. Before each call it checks the window cost and raises `FairUseLimitReached`. After each call it writes `ai_cost_events` (an unknown model → cost computed at the pricing file's `"default"` rate, which is required).
- `metered(provider, scope, subject) -> provider` is the helper used at the construction sites.

**Steps:**
- [ ] Tests: the ceiling reached → refused before the call (the inner fake records zero calls); the crossing call completes and records; an unknown model uses the default rate; a missing `default` fails pricing file validation; the `AI_ENABLED` control off → `FeatureNotInPlan` before any provider call.
- [ ] Commit `feat(usage): metered AI provider boundary with per-account hidden cost ceiling`.

**Phase 3 exit:** the smoke set (both dialects).

---

## Phase 4 — Worker, communications, notifications

### Task 18: Jobs table and worker

**Files:**
- Modify: `webapp/persistence/migrations.py` (add `029_jobs`), `tenancy.py`, `webapp/services/billing_webhooks.py` (enqueue `billing.webhook.process`).
- Create: `webapp/worker/__init__.py`, `webapp/worker/__main__.py`, `webapp/worker/runner.py`, `webapp/worker/schedule.py`, `webapp/worker/handlers.py`, `tests/webapp/worker/test_runner.py`.

**Interfaces (Produces):**
```python
JOB_KINDS  # §20.2 verbatim
def enqueue(conn, *, kind, payload, account_id=None, run_at=None, dedupe_key=None, max_attempts=8, now) -> str | None  # dedupe → None
class Worker:
    def __init__(self, settings, handlers: Mapping[str, Handler], *, clock, worker_id)
    def run_once(self) -> int          # claims ≤ N due jobs, runs each with timeout, returns count
    def run_forever(self, stop: threading.Event) -> None
Handler = Callable[[JobContext, dict], None]   # raise RetryLater / PermanentFailure / any Exception (= retry)
def enqueue_periodic(conn, *, now) -> None     # schedule.py: usage.sweep 5 min, tokens.sweep 1 h, notify.approval_expiry_scan 1 h, notify.digest hourly slot, retention.expire daily, autonomy.tick every settings.autonomy_tick_interval (hosted only)
```
- `python -m webapp.worker` runs `run_forever`.
- The `autonomy.tick` handler calls the existing 6C `run_driver` single tick (refactor `run_driver` into `tick_once(...)` + the loop, keeping behavior).

**Steps:**
- [ ] Tests:
  - lease claim exclusivity (2 workers, 1 job → 1 run);
  - backoff schedule values;
  - `DEAD` after 8 attempts, with an ops alert recorded;
  - a lease expiry lets another worker reclaim;
  - dedupe of periodic slots;
  - `PermanentFailure` → `FAILED` immediately;
  - `autonomy.tick` invokes `tick_once` (spy).
  - Plus `tests/webapp/test_autonomy_worker.py` (the refactor must keep it green).
- [ ] Commit `feat(ops): durable jobs with leases, backoff, dead letters and a worker process`.

### Task 19: Outbox, email port, templates, suppression, consent

**Files:**
- Modify: `webapp/persistence/migrations.py` (add `030_comms`: the §18.1 tables incl. `announcements`), `tenancy.py`, and replace the `webapp/comms/__init__.py` stub.
- Create: `webapp/comms/outbox.py`, `webapp/comms/email_port.py`, `webapp/comms/console.py`, `webapp/comms/smtp.py`, `webapp/comms/render.py`, `webapp/comms/consent.py`, `webapp/templates/email/<each §18.3 id>/v1/{subject.txt,body.txt,body.html}`, `webapp/api/email_webhooks.py` (generic normalized endpoint; adapters verify), `tests/webapp/comms/test_outbox.py`, `tests/webapp/comms/test_templates.py`, `tests/webapp/comms/test_consent.py`.

**Interfaces (Produces):**
```python
def enqueue(conn, *, category, template_id, to_address, payload, account_id=None, user_id=None, idempotency_key, locale="en", now) -> str | None
class EmailProvider(Protocol):
    name: str
    def send(self, *, to: str, subject: str, text: str, html: str, idempotency_key: str, headers: Mapping[str,str]) -> str  # provider_message_id; raises TransientSendError | PermanentSendError
class ConsoleEmailProvider(EmailProvider): sent: list[dict]    # also appends JSON lines to settings.db_path.parent / "outbox.log"
class SmtpEmailProvider(EmailProvider): def __init__(self, host, port, username, password, starttls: bool, from_address)
def dispatch_one(conn, message_id, *, provider, now) -> str   # §18.2 steps; returns resulting status
def record_consent(conn, *, account_id, user_id, channel, purpose, state, wording_version, source, now) -> None
def current_consent(conn, *, user_id, channel, purpose) -> bool
TEMPLATE_IDS  # §18.3 verbatim
```
- Worker handler `outbox.dispatch`: claims a batch of 20.

**Steps:**
- [ ] Tests:
  - every template id renders with its fixture payload (text and HTML), the HTML autoescapes an injected `<script>`, and each body contains the settings link;
  - PRODUCT bodies contain the preference link; SERVICE bodies contain "service message";
  - `MARKETING` → `CANCELED`;
  - a suppressed `COMPLAINT` address → `SUPPRESSED`, including for SERVICE;
  - a `HARD_BOUNCE` under 30 days old → `SUPPRESSED` for everything; over 30 days → `auth.*` still sends;
  - transient backoff sequence, then `FAILED`;
  - an idempotent enqueue;
  - SMTP adapter against a local `aiosmtpd`-free fake: a `smtplib.SMTP` monkeypatched spy asserting STARTTLS and the headers;
  - consent: the latest row wins; the default is not granted.
  - Replace the Task 8 stub assertions with real outbox rows (`tests/webapp/api/test_auth_routes.py`).
- [ ] Commit `feat(comms): transactional outbox, provider-neutral email port, versioned templates, suppression and consent records`.

### Task 20: Notifications, inbox, notification preferences

**Files:**
- Modify: `webapp/persistence/migrations.py` (add `031_notifications`, incl. the back-projection of `autonomy_notification_events`), `tenancy.py`, `webapp/persistence/autonomy_prepare.py::create_notification` (also calls `notify`), producers (pipeline prepare success; review-ready; blocker creation; fill result; submit results and challenge; usage thresholds in `UsageService.consume`; billing; security; account ops), `webapp/templates/base.html` (the header badge).
- Create: `webapp/services/notifications.py`, `webapp/services/inbox.py`, `webapp/api/notifications.py`, `webapp/templates/inbox.html`, `webapp/templates/settings/communications.html`, `tests/webapp/services/test_notifications.py`, `tests/webapp/services/test_inbox.py`.

**Interfaces (Produces):**
```python
NOTIFICATION_KINDS: Mapping[str, str]   # kind → category, §17.2 verbatim (incl. application.automation_blocked)
def notify(conn, *, account_id, kind, subject_type, subject_id, dedupe_key, detail, now) -> bool   # no commit; False if deduped
def action_required(conn, scope, *, now) -> list[ActionItem]   # §17.4 sources; ActionItem(kind, title_key, href, subject_type, subject_id, created_at)
def header_badge(conn, scope, *, now) -> int
def set_email_mode(conn, *, account_id, category, mode, now) -> None   # refuses non-configurable categories
```
- The `notify.digest` handler collects unsent DIGEST notifications since the last digest per account, at local 07:00.
- The `notify.approval_expiry_scan` handler notifies approvals expiring within 48 h (dedupe per approval).

**Steps:**
- [ ] Tests:
  - `notify` in a rolled-back transaction leaves no notification and no outbox row;
  - dedupe;
  - email fan-out per category and mode (SECURITY always immediate even if a preference row is attempted: `set_email_mode` raises);
  - `submit.challenge_handoff` never enqueues email;
  - action-required derivation: create an open blocker → it's listed; resolve it → gone;
  - an ambiguous submission is listed until resolved;
  - the 6C notification is mirrored with the mapped kind;
  - the digest groups by account timezone (a fixed clock in two zones);
  - `usage.limit_near` fires once at 80%.
  - Extend `tests/webapp/factories.py` with notifications for the isolation harness.
- [ ] Commit `feat(notifications): unified notification log, derived action-required inbox, email preferences and digests`.

**Phase 4 exit:** the smoke set.

---

## Phase 5 — Product surfaces

### Task 21: CV Library

**Files:**
- Modify: `webapp/persistence/migrations.py` (add `032_cv_library` per spec §23.1), `tenancy.py`, `webapp/services/application_documents.py` (media types; PDF validation; tenant keys; populate `document_version_references` on approval/fill/submit — the calls are added in `review_approval`, `fill_runs` and `human_submit` persistence as one insert each, in the same transaction), `webapp/api/handoff.py` (the download filename extension from `media_type`).
- Create: `webapp/persistence/cv_library.py`, `webapp/services/cv_library.py`, `webapp/api/cv_library.py`, `webapp/templates/cvs/{index,item}.html`, `tests/webapp/services/test_cv_library.py`, `tests/webapp/api/test_cv_library_routes.py`.

**Interfaces (Produces):**
```python
def create_item(conn, scope, *, title, description="", now) -> dict                  # gauge library.cv_items
def add_version(conn, scope, *, item_id, content: bytes, filename: str, media_type_hint: str | None, note="", origin="USER_UPLOAD", parent_version_id=None, template_id=None, library_visible=True, now) -> dict  # gauge storage.bytes; validates; publishes blob
def latest_visible_version(conn, *, account_id, item_id) -> dict | None
def archive_item(conn, scope, *, item_id, now) -> None; def unarchive_item(...)
def usage_count(conn, *, account_id, version_id) -> int                                # via document_version_references
class DocumentRejected(Exception): code  # EMPTY, TOO_LARGE, UNSUPPORTED_TYPE, TYPE_MISMATCH, INVALID_DOCX, INVALID_PDF, ENCRYPTED_PDF
def sniff_media_type(content: bytes) -> str | None   # PK zip + [Content_Types].xml wordprocessingml → docx; %PDF- → pdf
```

**Steps:**
- [ ] **Review Focus 4:** PDF bytes named `cv.docx` → `TYPE_MISMATCH`; zero bytes → `EMPTY`; 10 MiB + 1 → `TOO_LARGE`; an encrypted PDF (fixture made with pypdf `encrypt`) → `ENCRYPTED_PDF`; a `.txt` file → `UNSUPPORTED_TYPE`. In every case assert no object-store key was written, no `application_document_versions` row, and no library version.
- [ ] Other tests:
  - version numbering 1, 2, 3; `library_visible=False` versions are excluded from latest;
  - deleting a referenced `application_document_versions` row → `IntegrityError` (the trigger);
  - archive keeps history (the application detail still shows the CV used);
  - the legacy conversion (seed a reusable CV in a 021-era database fixture → migrate → item plus v1 `IMPORTED_LEGACY`);
  - the handoff download of a PDF is named `cv.pdf` with `application/pdf`;
  - the gauge refusal at the Free item limit, while existing items stay selectable.
  - Extend the factories.
- [ ] Commit `feat(cv-library): named CVs with immutable versions, DOCX/PDF uploads, reference-protected history`.

### Task 22: Job families, CV strategy, templates, resolution at prepare

**Files:**
- Modify: `webapp/persistence/migrations.py` (add `033_cv_strategy`), `tenancy.py`, `webapp/services/pipeline.py` (after understanding, call `resolve_for_workspace`), `webapp/services/review_*` (a `NEEDS_USER_CHOICE` review item; an override appends a resolution), the CV generation entry for `TAILOR_FROM` (wraps the existing v1 generation; `ai.cv_tailor` plus a `cv.tailor` reservation), `webapp/templates/workspace*.html` / the review page ("CV used: <title> v<n> (<origin>)").
- Create: `product/job_families.py`, `product/cv_strategy.py`, `product/cv_templates.py`, `webapp/persistence/account_documents.py`, `webapp/services/cv_strategy.py`, `webapp/api/cv_strategy.py` (`/api/cv-strategy`, `/api/job-families`), `tests/product/test_job_families.py`, `tests/product/test_cv_strategy.py`, `tests/webapp/services/test_cv_resolution.py`.

**Interfaces (Produces):**
```python
def normalize_job_families(doc) -> dict; def classify(doc, *, title: str, seniority: str | None) -> FamilyMatch  # FamilyMatch(family_id | "UNKNOWN", matched: list[str], reason)
def normalize_cv_strategy(doc, *, account_items: Mapping[str, ItemView], templates) -> dict
def resolve_rule(strategy, family_id) -> dict
CV_TEMPLATES = {"standard@1": {"renderer": "cv_document_renderer", "formats": ("docx",)}}
def save_account_document(conn, *, account_id, doc_type, doc, created_by, now) -> dict   # append-only; returns {id, doc_hash}
def current_account_document(conn, *, account_id, doc_type) -> dict | None
def resolve_for_workspace(conn, scope, *, workspace_id, now) -> dict   # writes application_cv_resolutions + selection; returns outcome row
```

**Steps:**
- [ ] Pure tests:
  - whole-word match (`"Drilling Engineer"` matches `drilling`, and `"Predrilling"` doesn't);
  - `title_none` excludes;
  - a seniority filter;
  - a tie at the top priority → UNKNOWN;
  - NFKC and casefold;
  - more than 50 families refused.
  - Strategy validation: a foreign item → error; an archived item → error; a missing default → error; an unknown template → error.
- [ ] Service tests:
  - FIXED → exact version;
  - LATEST → the newest visible;
  - TAILOR with Pro → a new `AI_TAILORED` invisible version (fake generator), reservation consumed;
  - TAILOR on Free → fallback, with `rule_json.fallback == "TAILOR_NOT_IN_PLAN"`;
  - `NEEDS_USER_CHOICE` → approval is impossible until chosen (use the existing approval service and assert the refusal code);
  - a user override → a second resolution row with `overridden_by_user=1`;
  - the approval binding's `document_version_id` equals the resolved version.
- [ ] Commit `feat(cv-strategy): job families, CV strategy documents and recorded per-application CV resolution`.

### Task 23: Preferences v2 and application rules v2

**Files:**
- Modify: `product/user_profile.py` (v2 per §16.1; v1 read-upgrade), `product/standing_policy.py` (v2 attributes; well-known rule ids; v1 upgrade), `webapp/services/autonomy_context.py` (supply the new attributes: `job.family` from `classify`, `job.compensation_max_annual`, `job.country`, `job.remote_mode`, and `job.rotation` via the §16.2 regex), migration `034_rules_v2` (the data step re-saving each current standing policy as v2), `webapp/services/review_*` (a manual-mode BLOCK advisory requiring a `rule_acknowledgements` row before approval), `webapp/services/fill_runs.py` (the same check at the fill start), `webapp/api/user_profile.py` (v2 writes only).
- Create: `product/rule_attributes.py` (the pure attribute derivation incl. `rotation_from_text`), `webapp/api/preferences.py` (`/preferences` page; `/api/rules`), `webapp/templates/preferences.html` (three tabs: Preferences, Job families and CVs, Rules), `tests/product/test_user_profile_v2.py`, `tests/product/test_standing_policy_v2.py`, `tests/product/test_rule_attributes.py`, `tests/webapp/services/test_manual_rule_advisories.py`.

**Interfaces (Produces):**
- `USER_PROFILE_VERSION = "user-profile.v2"`.
- `STANDING_POLICY_SCHEMA_VERSION = "standing-policy.v2"`.
- `WELL_KNOWN_RULES = {"pref.salary_floor", "pref.excluded_locations", "pref.rotation", "pref.excluded_employers"}`.
- `build_well_known_rule(rule_id, params) -> dict`.
- `rotation_from_text(text) -> str | UNKNOWN`.
- `annual_compensation_max(compensation, currency) -> int | UNKNOWN` (hour ×2080, day ×260, month ×12; a different currency → UNKNOWN).
- `evaluate_manual(policy, attrs) -> list[Advisory]`, where `Advisory(rule_id, effect, message_key)`.

**Steps:**
- [ ] Pure tests:
  - v1 profile → v2 defaults, and a v1 write refused post-migration;
  - rotation vectors: `"28/28 rotation"` → `28/28`; `"rotation: 14 / 14"` → `14/14`; `"24/7 support"` (no "rotation" within 40 characters) → UNKNOWN; `"12/2025 start ... rotation"` more than 40 characters away → UNKNOWN;
  - salary: a £60k/year floor vs a job of £250/day → 65,000 → no BLOCK; USD vs GBP → UNKNOWN → `on_unknown` applies;
  - a v2 policy with v1 rules evaluates identically to v1 (reuse the 6B policy fixtures);
  - rules still can't grant (the schema rejects any effect outside the three).
- [ ] Service tests:
  - manual: a BLOCK rule → approval refused with `RULE_ACKNOWLEDGEMENT_REQUIRED` until the acknowledgement; REQUIRE_USER → display only;
  - automated (6C screening path): BLOCK → not prepared.
  - A changed rule doesn't invalidate an existing approval.
- [ ] Commit `feat(rules): preferences v2 and standing-policy v2 with job family, compensation, location and rotation rules`.

### Task 24: Onboarding v1, CV import and proposals

**Files:**
- Modify: `webapp/persistence/migrations.py` (add `035_onboarding` per spec §23.1), `tenancy.py`, `product/semantic_subject_registry.py` / its JSON (add missing subjects: `contact.email`, `contact.phone`, `location.current`, `work_authorization.<country>`, `sponsorship.required`, `notice_period`, `relocation`, `rotation.availability`, `start_date.earliest` — only those absent; `grep` first), `webapp/templates/index.html` (the dashboard checklist).
- Create: `product/cv_extraction.py` (the strict schema; proposal validation), `product/cv_extraction_providers.py` (protocol plus fake), `product/openai_cv_extraction_provider.py` (mirrors the existing provider structure), `webapp/services/cv_text.py` (DOCX/PDF text), `webapp/services/cv_import.py`, `webapp/services/onboarding_v1.py`, `webapp/api/onboarding_v1.py`, `webapp/templates/onboarding/{step_about,step_cv,step_import,step_eligibility,step_preferences,step_families,step_rules,step_extension}.html`, tests `tests/product/test_cv_extraction.py`, `tests/webapp/services/test_cv_import.py`, `tests/webapp/services/test_onboarding_v1.py`.

**Interfaces (Produces):**
```python
ONBOARDING_STEPS = ("about", "cv", "import", "eligibility", "preferences", "families", "rules", "extension")
OPTIONAL_STEPS = frozenset({"import", "rules"})
def onboarding_state(conn, scope) -> dict; def mark_step(conn, scope, step, action, *, now) -> dict
def readiness(conn, scope, *, now) -> Readiness   # Readiness(prepare_ok, prepare_missing: list[str], fill_ok, fill_missing)
class OnboardingIncomplete(Exception): missing: list[str]
def start_import(conn, scope, *, document_version_id, now) -> str   # reservation profile.cv_import; enqueue or run
def run_import(conn, scope, *, run_id, provider, now) -> None       # text → provider → validated proposals; consume/release
def resolve_proposal(conn, scope, *, proposal_id, resolution, fields=None, now) -> dict
def resolve_batch(conn, scope, *, items: list[tuple[str, str, dict | None]], now) -> list[dict]   # one profile_manager write
```
- `ai.prepare` entry points call `readiness(...)` first and raise `OnboardingIncomplete`, mapped to 409 `ONBOARDING_INCOMPLETE`.

**Steps:**
- [ ] Tests:
  - a scanned-style PDF with no text → `FAILED: NO_TEXT`, reservation released;
  - fake provider proposals, including one invalid kind → dropped and counted;
  - nothing in the profile before resolution (snapshot unchanged);
  - `EDITED_ACCEPTED` writes the edited fields, with provenance `cv_import:<id>`;
  - a batch accept yields a single source revision;
  - answer proposals create `approved_answers` and `answer_confirmations`;
  - readiness: each missing prerequisite is listed; after the steps are satisfied → OK;
  - a skipped optional step doesn't block;
  - onboarding of the migrated `account_local` is complete.
- [ ] Commit `feat(onboarding): guided onboarding with CV import proposals that require user confirmation`.

### Task 25: Plans, pricing, billing and usage UI

**Files:**
- Create: `webapp/templates/{pricing,plans,checkout_return}.html`, `webapp/templates/settings/{billing,usage,account,security,sessions,devices}.html`, `webapp/api/settings.py`, `tests/webapp/api/test_settings_pages.py`.
- Modify: `webapp/api/views.py`, `webapp/templates/base.html` (the header usage meter for `applications.prepare`; the plan badge; the `PAST_DUE` banner), the prepare buttons (cost disclosure "Uses 1 of your N remaining prepares this period"), `webapp/api/errors.py` (HTML rendering of the §21.3 codes with actions).

**Steps:**
- [ ] Tests:
  - `/pricing` renders the features matrix from the catalog, with "—" for unresolved prices;
  - `/plans` → choose Free → no provider call → the dashboard; Pro with the fake provider → a redirect to the dev checkout → pay → the return page polls status → `ACTIVE`;
  - usage page numbers match `UsageService.summary`;
  - the hidden allowance isn't shown;
  - the cost disclosure is present on the prepare button;
  - the `PAST_DUE` banner is shown;
  - each §21.3 code renders a human message and an action link.
- [ ] Commit `feat(ui): pricing, plan selection, billing, usage and account settings pages`.

### Task 26: Scheduled discovery and Power automation wiring

**Files:**
- Modify: `webapp/persistence/migrations.py` (add `036_search_schedules`), `tenancy.py`; `webapp/worker/handlers.py` (`discovery.scheduled_run`); `webapp/services/autonomy_*` (the effective capability = min(the user's authorization, the entitlement, the deployment ceiling, the platform control) per §11.3; entitlement read per account at each tick); `webapp/services/billing_webhooks.py` and the suspension path (disable schedules on a downgrade below Power or on suspension); `webapp/persistence/discovery_sources.py` (hosted: portal-CLI sources are forced disabled unless an operator-enabled source setting exists — DP-9).
- Create: `webapp/services/search_schedules.py`, `webapp/api/search_schedules.py`, `tests/webapp/services/test_search_schedules.py`, `tests/webapp/services/test_power_automation_entitlements.py`.

**Interfaces (Produces):**
- `enable_schedule(conn, scope, *, search_workspace_id, cadence, now)` (the feature plus the gauge).
- `disable_schedule(...)`.
- `due_schedules(conn, *, now) -> list[dict]`.
- `automation_capability(conn, account_id, *, settings, now) -> Capability`.

**Steps:**
- [ ] Tests:
  - Pro can't enable a schedule (`FEATURE_NOT_IN_PLAN`); Power can, up to the gauge;
  - a scheduled run with fake sources → candidates → 6C screening (Power) → a digest notification;
  - Power with no user autonomy authorization → capability NONE; with authorization → PREPARE; the deployment ceiling NONE → NONE;
  - **Review Focus 5:** a Power account with an in-flight autonomous prepare holding a reservation is downgraded (webhook → state) mid-step. The step completes and consumes, or on failure releases. The next tick starts nothing new. The schedules are `enabled=0` and still present. Re-upgrading plus the user's enable restores them.
  - Suspension has the same effect, with the queue items paused.
- [ ] Commit `feat(power): scheduled saved searches and entitlement-bounded autonomous prepare`.

**Phase 5 exit:** the smoke set (both dialects) plus `tests/webapp/test_review_approval_acceptance.py`.

---

## Phase 6 — Admin and operations

### Task 27: Admin console, staff roles, TOTP, controls, announcements

**Files:**
- Modify: `tenancy.py` (no new tables: `platform_role_assignments` and `staff_totp` come from `023_identity`, `announcements` from `030_comms`).
- Create: `webapp/services/staff_auth.py` (TOTP enrolment/verify; re-auth window), `webapp/services/admin_read_models.py`, `webapp/services/admin_actions.py`, `webapp/api/admin.py`, `webapp/api/admin_api.py`, `webapp/templates/admin/{login,totp,dashboard,accounts,account,billing_events,jobs,outbox,announcements,controls,staff,audit}.html`, `webapp/tools/create_admin.py`, `tests/webapp/api/test_admin_permissions.py`, `tests/webapp/api/test_admin_privacy_canary.py`.

**Interfaces (Produces):**
- `ADMIN_PERMISSIONS: Mapping[str, frozenset[str]]` (role → permissions, the §19.1 table verbatim, with permission ids `accounts.view`, `accounts.resend`, `accounts.suspend`, `accounts.kill_switch`, `billing.view`, `grants.manage`, `ops.retry`, `announcements.manage`, `controls.manage`, `staff.manage`, `accounts.delete`, `audit.all`).
- `get_admin_scope(permission) -> dependency`.
- `create_admin(email, password) -> None` (CLI; prints the TOTP provisioning URI once).

**Steps:**
- [ ] Tests:
  - the permission matrix: every admin API endpoint × every role → allowed exactly per the table;
  - a customer session on `/admin` → 403; a staff session on a USER route → 403;
  - TOTP required before any admin page;
  - destructive actions without re-auth in 5 min → `REAUTH_REQUIRED`;
  - every staff action writes an audit row;
  - **privacy canary:** plant `CANARY-7f3e` in every content table's text columns (profile sources, document filenames, artifacts payloads, answer values, observations, job text), then assert no admin page or API response body contains it;
  - announcements: the sanitized Markdown strips `<script>` and `javascript:` links, and the audience filter works;
  - platform controls: setting `AI_ENABLED=false` → prepare refused platform-wide.
  - **Live submission gate (spec §8.6):** `Settings.human_submit_ceiling(conn)` in hosted mode returns `SUBMIT` only when all of these hold: `human_submit_enabled`, `SUBMIT_ENABLED`, `HOSTED_THREAT_MODEL_SIGNED_OFF`, and the adapter's certification `LIVE_CERTIFIED` with evidence. Test each of those four missing → `FILL`. Greenhouse in hosted mode → `FILL` even with every control on. Local mode behaves as at `b042e0e`. Modify `webapp/config.py` and the one caller in `webapp/services/autonomy_context.py` (pass `conn`).
- [ ] Commit `feat(admin): least-privilege staff console with TOTP, platform controls, announcements and a content privacy boundary`.

### Task 28: Account deletion, purge, retention, data export

**Files:**
- Modify: `webapp/persistence/migrations.py` (add `037_purge`: `purge_in_progress`; recreate every append-only DELETE trigger with the purge guard on both dialects; PSEUDONYMIZE UPDATE allowance triggers for the listed columns), `tenancy.py` (final classification of every table).
- Create: `product/policies/retention-policy.schema.json`, `product/policies/retention-policy.v1.json` (production: grace 0, cooling-off 0, retained periods `null`, export link 7 d), `product/policies/retention-policy.dev.json`, `webapp/services/account_lifecycle.py`, `webapp/services/purge.py`, `webapp/services/export.py`, `webapp/templates/settings/{delete,restricted}.html`, `tests/webapp/services/test_purge.py`, `tests/webapp/services/test_export.py`, `tests/webapp/services/test_account_lifecycle.py`.

**Interfaces (Produces):**
```python
def request_deletion(conn, scope, *, password, typed_email, now) -> None   # §20.4 request effects
def cancel_deletion(conn, scope, *, now) -> None
def purge_account(conn_factory, *, account_id, object_store, now) -> PurgeReport   # idempotent phases
def expire_retained(conn, *, policy, now) -> int
def request_export(conn, scope, *, now) -> str; def build_export(conn, *, export_id, object_store, now) -> None
```

**Steps:**
- [ ] Purge tests, on both dialects:
  - build the full factory graph for A and B, then purge A;
  - every `DELETE`-class table has zero A rows (via `account_rows_sql`);
  - `PSEUDONYMIZE` columns are rewritten;
  - `RETAIN` rows are present and tagged;
  - no B row changed (a row-hash snapshot);
  - the object-store prefix for A is empty;
  - re-running purge is a no-op;
  - append-only UPDATE and DELETE outside purge still raise;
  - a DELETE inside another transaction while a purge is running in a different connection still raises (the guard row isn't visible);
  - the email can sign up again after purge.
- [ ] Lifecycle tests:
  - request → sessions and devices revoked, the provider `cancel(immediately=True)` spy called, the kill switch engaged, the job queued at the cooling-off time;
  - cancel during cooling-off → ACTIVE;
  - the restricted page allows export and delete-cancel only.
- [ ] Export tests: the ZIP contains the JSON for each export-class table (secrets excluded: no `*_hash` or token columns), the documents' original bytes (sha256 matches) and the profile sources; download requires a session; 3/day enforced.
- [ ] Commit `feat(privacy): staged account deletion with guarded purge, retention classes and full data export`.

### Task 29: Readiness, metrics and dead-letter tooling

**Files:**
- Create: `webapp/api/ops.py` (`/ready`, `/metrics`), `tests/webapp/api/test_ops_routes.py`.
- Modify: `webapp/observability.py` (counters and histograms, in-process registry, Prometheus text rendering), `webapp/api/admin_api.py` (retry dead job or outbox; already permissioned in Task 27, wired here to real handlers).

**Steps:**
- [ ] Tests:
  - the writer-lock metrics `db_writer_lock_wait_seconds`, `db_writer_lock_hold_seconds` and `db_writer_lock_timeouts_total` (by site) are present in `/metrics`, and the admin dashboard shows the 24 h timeouts and the top 5 sites by wait;
  - `/ready` is 200 when everything is fine; 503 listing `database`, `migrations`, `object_store`, `catalog`, `email_provider`, `secret_key` individually when each is broken (monkeypatched probes);
  - no secret values in the body;
  - `/metrics` → 404 when the token is unset, 401 with the wrong bearer, and 200 with the §20.6 metric names present;
  - a retried DEAD job goes back to QUEUED with attempts reset, plus an audit row.
- [ ] Commit `feat(ops): readiness and metrics endpoints; dead-letter retry`.

**Phase 6 exit:** the smoke set (both dialects) plus the isolation harness plus the never-gated test.

---

## Phase 7 — Journey and release tooling

### Task 30: Local → hosted import tool

**Files:**
- Create: `webapp/tools/import_local_account.py`, `tests/webapp/tools/test_import_local_account.py`.

**Steps:**
- [ ] Test (PostgreSQL target required; skipped otherwise):
  - seed a SQLite database through the factories for `account_local` plus a filesystem profile and documents;
  - run the import into a fresh PostgreSQL database with `LocalFsObjectStore` as the "hosted" store;
  - assert, for every registry table, that the row counts match with `account_id` re-keyed;
  - `review_hash`/`binding_hash`/`result_hash` values are identical;
  - blobs are readable through the new keys (sha256 verified);
  - the profile snapshot equals the source snapshot;
  - a second run refuses (the email exists);
  - `--dry-run` writes nothing.
- [ ] Commit `feat(tools): lossless local-to-hosted account import`.

### Task 31: Release-journey and Free-journey suites

**Files:**
- Create: `tests/webapp/test_release_journey_browser.py`, `tests/webapp/test_free_journey.py` (API-level), `tests/webapp/fixtures/journey/*` (a fixture job posting with a Greenhouse fixture form; a fixture CV DOCX; fake AI provider outputs keyed to them).
- Modify: `tests/webapp/conftest.py` (a `journey_server` fixture: uvicorn in a thread with `auth_required_in_local=True`, `--db postgres` when available, else SQLite with a visible note, the fake billing and email providers, fake AI providers, the fixture origins enabled, and the extension test-hook build).

**Steps:**
- [ ] The browser test walks spec §22.1 steps 1–15 exactly, reading the verification link from `ConsoleEmailProvider.sent` and the pairing code from the page. The assertions are listed in §25.4.
- [ ] Free journey: sign up → Free → onboard → prepare up to the dev Free allowance → the next prepare returns `ALLOWANCE_EXHAUSTED` with `upgrade_to="pro"` → review/fill of an existing prepared application still succeeds (API-level fill-start acceptance).
- [ ] Run each file alone, as the browser memory rule requires.
- [ ] Commit `test(journey): automated sign-up-to-submit release journey and free-allowance journey`.

### Task 32: Release-readiness tool, runbooks, docs

**Files:**
- Create: `webapp/tools/release_readiness.py`, `tests/webapp/tools/test_release_readiness.py`, `docs/runbooks/hosted-deployment.md` (the processes, env vars, migrations, worker, extension hosted build, first admin), `docs/runbooks/hosted-incident.md` (the platform controls, kill switches, dead letters, webhook replay), `docs/runbooks/decision-points.md` (DP-1…DP-10 with where each value lives).
- Modify: `README.md`, `SETUP.md` (the two modes; local stays the developer path), `CHANGELOG.md`.

**Steps:**
- [ ] Test: with the production catalog and retention files plus no adapters, the tool exits 1 and lists every DP-1…DP-10 item. With every DP resolved but no `LIVE_CERTIFIED` submit adapter, it exits 1 with the finding `COMMERCIAL_GATE_NO_LIVE_SUBMIT_ADAPTER` and reports `deployment_ready: true, commercial_release: false` (spec §27.2-6); with the dev files plus the fake adapters in local mode, it exits 1 only with "not hosted"-class findings; each check has a stable id.
- [ ] Run §27.1 criteria 1–8 as the Bundle 7 exit check: the journey suites, the structural tests (parity, lint, registry, route classes, isolation, never-gated, gate call sites, canary), the hosted-config refusal tests, the extension hosted-build test, the smoke set, and the readiness tool. **Not** the full historical suite.
- [ ] Commit `docs(release): release-readiness tool, hosted runbooks and decision-point register`.

**Bundle 7 complete →** hand back for the single review. Then the final comprehensive release pass (spec §25.5, §27.2).
