# Hosted deployment runbook

How to run JobSearch as the hosted, multi-tenant service (Bundle 7). Local
single-user mode (`JOBSEARCH_DEPLOYMENT=local`, SQLite, files on disk) stays the
developer path and is described in `SETUP.md`.

Before anything goes live, run the release-readiness check (end of this page) and
read `decision-points.md`: a hosted deployment with open decision points is not
releasable.

## 1. What runs

| Process | Command | Notes |
|---|---|---|
| Web | `uvicorn --factory webapp.app:create_app --host 0.0.0.0 --port 8000` | Behind TLS termination. `create_app()` reads every setting from the environment and refuses to start on an incomplete hosted configuration (`webapp/deployment.py`). Run two or more for availability. |
| Worker | `python -m webapp.worker` | Exactly the same environment as the web process. Runs the durable jobs: outbox dispatch, billing and email webhooks, usage sweeps, scheduled discovery, exports, purge, retention expiry, the 6C automation driver. Run one or more; jobs are leased, so extra workers are safe. |
| One-off | `python -m webapp.worker --once` | Enqueue due periodic jobs, run one batch, exit. Useful in a cron-only platform or for a smoke test. |

Health endpoints: `GET /ready` (database,
migrations, object store, plan catalog, email provider, secret key; 503 with the
failing names), `GET /metrics` (Prometheus text; 404 unless
`JOBSEARCH_METRICS_TOKEN` is set, then `Authorization: Bearer <token>`).

## 2. Infrastructure

- **PostgreSQL** 15+ with automated backups and point-in-time recovery. One database per environment.
- **Object storage**, S3-compatible (AWS S3, Cloudflare R2, MinIO), encryption at rest on, versioning recommended. Documents and data exports are stored under `accounts/<account_id>/`.
- **Email**: an SMTP relay from a transactional provider with the sending domain verified (SPF, DKIM, DMARC). Point its bounce/complaint webhook at `POST /webhooks/email/smtp`.
- **Payments**: the billing provider's webhook at `POST /webhooks/billing/<provider>` (DP-1 decides the provider; until an adapter exists, hosted mode refuses paid checkout and Free still works).
- **Secrets** in a secret manager, injected as environment variables.
- **Logs**: JSON lines on stdout (one per request, with `request_id` and `account_id`); ship them.
- **Alerts** on: `/ready` failing; DEAD jobs (`jobs{status="DEAD"}`); `billing_webhook_lag_seconds`; FAILED outbox messages (`outbox_messages{status="FAILED"}`); `db_writer_lock_timeouts_total`; error-reporter events; `database_busy_total` growth.

## 3. Environment

Required in hosted mode (the process refuses to start without each one):

| Variable | Value |
|---|---|
| `JOBSEARCH_DEPLOYMENT` | `hosted` |
| `JOBSEARCH_DATABASE_URL` | `postgresql://user:password@host:5432/jobsearch` |
| `JOBSEARCH_SECRET_KEY` | at least 43 characters (32 random bytes, base64url). Signs sessions, CSRF and pending staff sign-ins; rotating it signs everyone out. |
| `JOBSEARCH_TOTP_ENCRYPTION_KEYS` | `kid:base64url-32-byte-key[,kid2:key2...]`; the first encrypts staff TOTP secrets (AES-256-GCM), all decrypt. Rotate by prepending a new key and re-encrypting (`webapp.services.staff_auth.rotate_totp_secrets`, see the incident runbook). |
| `JOBSEARCH_PUBLIC_ORIGIN` | `https://app.example.com` (no path). Used in mail links and must match the extension build's origin. |
| `JOBSEARCH_EXTENSION_IDS` | the published extension's store id(s), comma separated |
| `JOBSEARCH_OBJECT_STORE` | `{"kind": "s3", "bucket": "...", "endpoint_url": "...", "region": "...", "prefix": "prod"}` |
| `JOBSEARCH_EMAIL_PROVIDER` | `smtp` |
| `JOBSEARCH_SMTP` | `{"host": "...", "port": 587, "username": "...", "password": "...", "starttls": true, "from_address": "JobSearch <hello@example.com>"}` |
| `JOBSEARCH_PLAN_CATALOG` | the production catalog (never `plan-catalog.dev.json`) |
| `JOBSEARCH_RETENTION_POLICY` | the production retention policy (never `retention-policy.dev.json`) |
| `JOBSEARCH_AI_PRICING` | the production AI pricing table (never `ai-pricing.dev.json`) |
| `OPENAI_API_KEY` | the operator's key (AI is operator-funded; quotas are enforced server-side) |

Optional:

| Variable | Default | Meaning |
|---|---|---|
| `JOBSEARCH_BILLING_PROVIDER` | `fake` | The payment adapter (DP-1). `fake` is refused for paid checkout in hosted mode. |
| `JOBSEARCH_METRICS_TOKEN` | unset | Enables `/metrics`. |
| `JOBSEARCH_WRITER_LOCK_TIMEOUT_MS` | 10000 (1000-30000) | The transitional PostgreSQL writer lock's bound (spec §10.7). |
| `JOBSEARCH_ENABLE_CV_QUALITY_V2` | off | **Required for approval.** 6D-A approval binds exact document files, which needs CV Quality v2; with it off no application can be approved, filled or submitted. Turn it on once its candidate-facing CV is presentable (an open release item). |
| `JOBSEARCH_AUTONOMY_MAX_CAPABILITY` | `NONE` | The deployment ceiling for the 6B gate. The user-started fill needs `FILL`; leave automation features to the platform controls. |
| `JOBSEARCH_HUMAN_SUBMIT_ENABLED` | off | Lets the human-authorized submit (6E-A) run at all. In hosted mode submission additionally needs the `SUBMIT_ENABLED` and `HOSTED_THREAT_MODEL_SIGNED_OFF` platform controls and a `LIVE_CERTIFIED` adapter. |
| `JOBSEARCH_SUBMIT_FIXTURE_ORIGINS` | off | Test only: fixture-certified adapters on loopback origins. Never on in production. |
| `JOBSEARCH_DECISION_DP7_REFUNDS`, `..._DP8_GRANDFATHERING`, `..._DP9_DISCOVERY` | unset | Recorded business decisions, read by the release-readiness check (`decision-points.md`). |

## 4. Database migrations

Migrations run when the web process starts (`init_db`): a fresh PostgreSQL database
gets the parity-verified baseline, then the Bundle 7 chain; an existing one gets
only what it lacks. Concurrent starters serialize on an advisory lock. A hosted
database never contains the local account (`account_local`).

For a release with new migrations: take a backup, deploy one web instance, wait
for `/ready` (the `migrations` probe), then roll the rest and the workers.

## 5. First admin

```
JOBSEARCH_ADMIN_PASSWORD='...' python -m webapp.tools.create_admin --email ops@example.com --name "Ops"
```

It prints an authenticator provisioning URI once. Sign in at `/admin/login`; the
first sign-in confirms the TOTP enrolment. Staff accounts are separate from
customer accounts and need password + TOTP for every console session.

Then, in the console: publish the Terms and Privacy Notice (DP-5), review the
platform controls (sign-ups, AI, automation, discovery, submit), and enable the
discovery sources DP-9 allows.

## 6. Extension (hosted build)

```
cd extension
npm ci
npm run build -- --origin https://app.example.com
```

The hosted build bakes in the one backend origin (`host_permissions` is exactly
`https://app.example.com/*`). Publish it to the store and put its id in
`JOBSEARCH_EXTENSION_IDS`. Users pair with the code shown at `/pairing`; local
builds never talk to a hosted origin.

## 7. Moving the local founder account in

```
python -m webapp.tools.import_local_account --sqlite .jobsearch/jobsearch.sqlite3 --profile-root . \
    --documents-root documents --target-dsn "$JOBSEARCH_DATABASE_URL" \
    --object-store "$JOBSEARCH_OBJECT_STORE" --email founder@example.com --display-name "Founder Name" \
    --assume-verified --dry-run --report import-report.json
```

Run with `--dry-run` first, read the report, then without it. The import refuses
when the email already exists, so a repeat does nothing. The owner sets a
password with "Forgot password" and re-pairs the extension once.

## 8. Release readiness

```
python -m webapp.tools.release_readiness --config production.env
```

Exit 0 only when nothing is open. `deployment_ready: true` with
`commercial_release: false` means the service can run with submission disabled;
that is **not** the commercial release (spec §27.2-6), which also needs a
`LIVE_CERTIFIED` submit adapter, the threat-model sign-off and the `SUBMIT_ENABLED`
control.
