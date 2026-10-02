# Business decision points (DP-1 … DP-10)

Bundle 7 built every mechanism with a safe default; the business values are open.
`python -m webapp.tools.release_readiness --config production.env` lists exactly
the ones still open (finding ids in brackets) and exits non-zero until each is
resolved. Spec: `docs/superpowers/specs/2026-09-29-bundle7-productization-design.md` §4.

| DP | Decision | Where the value lives | Safe default until resolved | Readiness finding |
|---|---|---|---|---|
| DP-1 | Payment provider | `JOBSEARCH_BILLING_PROVIDER` plus that adapter's credentials (the `BillingProvider` port, `webapp/billing/`) | Hosted mode refuses paid checkout; Free works | `DP1_PAYMENT_PROVIDER` |
| DP-2 | Transactional email provider | `JOBSEARCH_EMAIL_PROVIDER=smtp` and `JOBSEARCH_SMTP` (`webapp/comms/`) | Hosted mode refuses to start without it | `DP2_EMAIL_PROVIDER` |
| DP-3 | Plan prices and allowances (incl. AI cost ceilings, Free's allowances) | The production catalog `product/plans/plan-catalog.v1.json` (`JOBSEARCH_PLAN_CATALOG`): every allowance `limit`, and each paid plan's `provider_prices` | `null` limits → refused; prices shown as "Shown at checkout" | `DP3_CATALOG_UNRESOLVED`, `DP3_PROVIDER_PRICES`, `DP3_DEV_CATALOG` |
| DP-4 | Retention and timing: payment grace, deletion cooling-off, retained-record periods, export link lifetime | `product/policies/retention-policy.v1.json` (`JOBSEARCH_RETENTION_POLICY`) | Grace 0 d, cooling-off 0 d, retained periods `null` (kept, flagged, never destroyed early) | `DP4_RETENTION_UNRESOLVED`, `DP4_DEV_RETENTION` |
| DP-5 | Legal copy (Terms, Privacy Notice) and versions | `legal_documents` (published from the admin console) and acceptance records | Sign-up refused while none is published | `DP5_LEGAL_UNPUBLISHED` |
| DP-6 | Trial policy | Catalog `trial_days` per plan | 0 (no trial) | `DP6_TRIAL_POLICY` |
| DP-7 | Refunds on cancellation or deletion | Provider side; the app never refunds (`cancel(refund=False)`). Record the decision as `JOBSEARCH_DECISION_DP7_REFUNDS` | No app-issued refunds | `DP7_REFUND_POLICY` |
| DP-8 | Grandfathering on catalog changes | Subscriptions pin `catalog_version`; operator migration command. Record as `JOBSEARCH_DECISION_DP8_GRANDFATHERING` | Pinned | `DP8_GRANDFATHERING` |
| DP-9 | Discovery sources in hosted production | `discovery_source_settings.operator_enabled` (admin) plus the per-plan feature. Record as `JOBSEARCH_DECISION_DP9_DISCOVERY` | Portal-CLI sources off; manual capture always on | `DP9_DISCOVERY_SOURCES` |
| DP-10 | Production domain and extension listing | `JOBSEARCH_PUBLIC_ORIGIN` (https), the extension build `--origin`, `JOBSEARCH_EXTENSION_IDS` | Dev origin only | `DP10_PRODUCTION_ORIGIN`, `DP10_EXTENSION_ID` |

## Beyond the decision points

The readiness tool also reports, separately:

- failing `/ready` probes (`READY_*`);
- the platform controls, with `CONTROL_SUBMIT_DISABLED` and `CONTROL_THREAT_MODEL_NOT_SIGNED_OFF`;
- every submit adapter's certification, with `COMMERCIAL_GATE_NO_LIVE_SUBMIT_ADAPTER` until one adapter is `LIVE_CERTIFIED` with recorded live evidence.

Those three are **commercial-gate** findings: with only them open the deployment
is ready to run with submission disabled (`deployment_ready: true`), which is not
the commercial release (spec §27.2-6).

## Known release blockers outside the DPs (from the Bundle 7 journey)

- CV Quality v2 (`JOBSEARCH_ENABLE_CV_QUALITY_V2`) must be on for any 6D-A approval; its candidate-facing CV is not yet presentable.
- The user-started fill needs the 6B deployment ceiling `JOBSEARCH_AUTONOMY_MAX_CAPABILITY=FILL` and the user's own FILL authority (Autonomy settings).
