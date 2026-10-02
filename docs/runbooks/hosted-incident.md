# Hosted incident runbook

What to do when the hosted service misbehaves. Every action below is a staff
console action (`/admin`, password + TOTP; the destructive ones ask you to
re-authenticate if your sign-in is older than five minutes) and is written to the
append-only audit log. The console never shows customers' application content
(job titles, CV text, answers): it shows accounts, states and counts only.

## 1. First look

1. `GET /ready`: which probe fails (database, migrations, object_store, catalog, email_provider, secret_key).
2. `/admin` dashboard: DEAD jobs, FAILED outbox messages, webhook lag, writer-lock timeouts in the last 24 hours, AI cost today.
3. Logs: every request line carries `request_id`; a user-facing "Something went wrong" page quotes the same id.

## 2. Platform controls (stop a whole capability at once)

`/admin/controls` (API `POST /api/admin/controls`, permission `controls.manage`, a
reason is required). The current value is the latest row; history is kept.
In hosted mode a control that was never set reads **off** (fail closed).

| Control | Off means |
|---|---|
| `SIGNUPS_ENABLED` | New sign-ups refused; existing users unaffected. |
| `AI_ENABLED` | No AI call anywhere (prepare, tailoring, CV import). Use when AI spend runs away or the provider misbehaves. Nothing already prepared is lost. |
| `AUTOMATION_ENABLED` | Scheduled preparation (6C) and Power automation stop; manual work continues. |
| `DISCOVERY_ENABLED` | On-demand and scheduled discovery stop; manual job capture still works. |
| `SUBMIT_ENABLED` | The human-authorized submit is refused (fill and review still work). |
| `HOSTED_THREAT_MODEL_SIGNED_OFF` | Also blocks live submission; only turned on with a recorded sign-off. |

## 3. Kill switches and account actions

| Situation | Action |
|---|---|
| One account's automation is doing something wrong | `POST /api/admin/accounts/{id}/kill-switch` (`accounts.kill_switch`): engages that account's autonomy kill switch; the user must acknowledge and resume. |
| Abuse, fraud, a compromised account | Suspend (`/suspend`): status SUSPENDED, every session and extension device revoked, schedules disabled and queued automation paused, the user notified. They can still export and delete. `/unsuspend` restores access; schedules and the paused queue stay off until the user turns them back on. |
| A stolen session or token | `/revoke-sessions` ends every web session. Extension devices: the user (Settings → Devices) or a suspension revokes them; a revoked device's next call gets 401 and the extension wipes itself. A refresh-token reuse is detected automatically, revokes the device and emails the user. |
| A user is locked out of mail | `/resend-verification`, `/password-reset` (`accounts.resend`). |
| Goodwill or a correction to a plan | Entitlement grants (`/grants`, `grants.manage`), revocable. |

## 4. Dead letters

A job that failed `max_attempts` times becomes DEAD (the error reporter fires
once); an outbound email that keeps failing ends FAILED.

1. `/admin/jobs` or `/admin/outbox` (`ops.retry`): read the kind and the last error (no customer content is shown).
2. Fix the cause (provider credentials, a bad deploy, an expired object-store key).
3. Retry: `POST /api/admin/jobs/{id}/retry` or `/api/admin/outbox/{id}/retry`. A retried job or message starts a fresh attempt budget. Retrying is safe: handlers are idempotent (exports, purge phases, dispatch with provider idempotency keys).

## 5. Webhook replay

**Billing.** Every provider event is stored verbatim in `billing_webhook_events`
(signature-verified, deduplicated by the provider's event id) before it is
processed by the `billing.webhook.process` job. If processing failed:

- a DEAD `billing.webhook.process` job: fix the cause, retry it from `/admin/jobs`;
- events never enqueued (e.g. the worker was down): `BillingWebhooks.process_pending` processes every stored, verified, unprocessed event in arrival order; run it from a one-off shell with the production environment.

Re-delivering from the provider's dashboard is also safe: duplicates are ignored
and the subscription state is re-derived from the provider's snapshot, never
from event order.

**Email.** Bounce and complaint events (`POST /webhooks/email/<provider>`) are
stored in `email_provider_events` and processed by `email.webhook.process`;
retry DEAD ones the same way. A hard bounce or complaint suppresses the address
(`email_suppressions`); service mail to a suppressed address is not sent.

## 6. Database pressure

`database_busy_total` and `db_writer_lock_timeouts_total` rising means writers
are waiting on the transitional PostgreSQL writer lock (spec §10.7). The dashboard
names the top sites. Short term: raise `JOBSEARCH_WRITER_LOCK_TIMEOUT_MS` (max
30000) or reduce worker concurrency; the lasting fix is moving that site to the
per-account lock.

## 7. Secrets

- `JOBSEARCH_SECRET_KEY` rotation signs every user out (sessions and CSRF are keyed from it).
- Staff TOTP keys: prepend a new `kid:key` to `JOBSEARCH_TOTP_ENCRYPTION_KEYS`, deploy, run `webapp.services.staff_auth.rotate_totp_secrets(conn, settings)` from a one-off shell, then remove the old key.
- A leaked extension credential: revoke the device; the user re-pairs.

## 8. Data requests

Export: the user requests it (Settings → Account); the link lasts per DP-4.
Deletion: the user requests it; after the cooling-off period the `account.purge`
job deletes application data, pseudonymizes the identity, tags the retained
classes (billing, security audit, consent) and emails the user. A canceled
request within the cooling-off period restores the account.
