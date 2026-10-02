"""Is this deployment ready to release? (Bundle 7 spec §4, §27.2, §27.3)

    python -m webapp.tools.release_readiness --config production.env

Reads the deployment's environment file (KEY=VALUE lines, as the web and
worker processes get them), then reports, with a stable id per finding:

- the open business decision points DP-1...DP-10 (docs/runbooks/decision-points.md
  says where each value lives; DP-7/8/9 are recorded as JOBSEARCH_DECISION_* lines);
- every failing /ready probe (READY_<NAME>);
- the platform-control state;
- the submit certification of every adapter.

Exit 0 only when nothing is open. ``deployment_ready`` is true when the only
findings are commercial-gate ones (live submission off, no LIVE_CERTIFIED
adapter, threat model not signed off): the product may then run with
submission disabled, which is NOT the commercial release (§27.2-6). In local
mode every production check is reported as NOT_HOSTED."""
from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

__all__ = ["CHECK_IDS", "DEFAULT_PROBES", "Finding", "ReadinessReport", "evaluate", "main", "read_env_file"]

# id -> (category, what is open)
CHECK_IDS: dict[str, tuple[str, str]] = {
    "DEPLOYMENT_NOT_HOSTED": ("NOT_HOSTED", "JOBSEARCH_DEPLOYMENT is not 'hosted'"),
    "DP1_PAYMENT_PROVIDER": ("DP", "DP-1: no real payment provider (JOBSEARCH_BILLING_PROVIDER is the fake)"),
    "DP2_EMAIL_PROVIDER": ("DP", "DP-2: no transactional email provider (console, or SMTP without a host)"),
    "DP3_DEV_CATALOG": ("DP", "DP-3: the plan catalog is the development catalog"),
    "DP3_CATALOG_UNRESOLVED": ("DP", "DP-3: plan allowances are still null in the catalog"),
    "DP3_PROVIDER_PRICES": ("DP", "DP-3: a paid plan has no provider price id"),
    "DP4_DEV_RETENTION": ("DP", "DP-4: the retention policy is the development policy"),
    "DP4_RETENTION_UNRESOLVED": ("DP", "DP-4: retained-record periods are still null"),
    "DP5_LEGAL_UNPUBLISHED": ("DP", "DP-5: no published Terms and Privacy Notice"),
    "DP6_TRIAL_POLICY": ("DP", "DP-6: a plan's trial_days is still null"),
    "DP7_REFUND_POLICY": ("DP", "DP-7: the refund decision is not recorded (JOBSEARCH_DECISION_DP7_REFUNDS)"),
    "DP8_GRANDFATHERING": ("DP", "DP-8: the grandfathering decision is not recorded "
                                 "(JOBSEARCH_DECISION_DP8_GRANDFATHERING)"),
    "DP9_DISCOVERY_SOURCES": ("DP", "DP-9: the hosted discovery-source decision is not recorded "
                                    "(JOBSEARCH_DECISION_DP9_DISCOVERY)"),
    "DP10_PRODUCTION_ORIGIN": ("DP", "DP-10: no https production origin (JOBSEARCH_PUBLIC_ORIGIN)"),
    "DP10_EXTENSION_ID": ("DP", "DP-10: no extension store id (JOBSEARCH_EXTENSION_IDS)"),
    "READY_DATABASE": ("READY", "/ready: database"),
    "READY_MIGRATIONS": ("READY", "/ready: migrations"),
    "READY_OBJECT_STORE": ("READY", "/ready: object store"),
    "READY_CATALOG": ("READY", "/ready: plan catalog"),
    "READY_EMAIL_PROVIDER": ("READY", "/ready: email provider"),
    "READY_SECRET_KEY": ("READY", "/ready: secret key"),
    "CONTROL_SUBMIT_DISABLED": ("COMMERCIAL", "the SUBMIT_ENABLED platform control is off"),
    "CONTROL_THREAT_MODEL_NOT_SIGNED_OFF": ("COMMERCIAL", "HOSTED_THREAT_MODEL_SIGNED_OFF is off (§27.2-5)"),
    "COMMERCIAL_GATE_NO_LIVE_SUBMIT_ADAPTER": ("COMMERCIAL", "no submit adapter is LIVE_CERTIFIED with live evidence "
                                                             "(§27.2-6: deployment-ready is not the commercial release)"),
}
DECISION_KEYS = {"DP7_REFUND_POLICY": "JOBSEARCH_DECISION_DP7_REFUNDS",
                 "DP8_GRANDFATHERING": "JOBSEARCH_DECISION_DP8_GRANDFATHERING",
                 "DP9_DISCOVERY_SOURCES": "JOBSEARCH_DECISION_DP9_DISCOVERY"}


def _default_probes() -> dict[str, Callable[[Any], bool]]:
    from webapp.api.ops import READINESS_PROBES
    return dict(READINESS_PROBES)


DEFAULT_PROBES: Mapping[str, Callable[[Any], bool]] | None = None  # None: the live /ready probes


@dataclass(frozen=True)
class Finding:
    id: str
    category: str
    message: str


@dataclass
class ReadinessReport:
    deployment: str
    findings: list[Finding] = field(default_factory=list)
    controls: dict[str, bool] = field(default_factory=dict)
    certifications: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def deployment_ready(self) -> bool:
        return self.deployment == "hosted" and all(f.category == "COMMERCIAL" for f in self.findings)

    @property
    def commercial_release(self) -> bool:
        return self.deployment == "hosted" and self.ok

    def as_dict(self) -> dict[str, Any]:
        return {"deployment": self.deployment, "ok": self.ok, "deployment_ready": self.deployment_ready,
                "commercial_release": self.commercial_release, "findings": [asdict(f) for f in self.findings],
                "controls": self.controls, "certifications": self.certifications}


def _raw_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _catalog_checks(settings: Any) -> Iterable[str]:
    path = Path(settings.plan_catalog_path)
    if ".dev." in path.name:
        yield "DP3_DEV_CATALOG"
    catalog = _raw_json(path) or {}
    plans = catalog.get("plans") or {}
    if any(a is None or (isinstance(a, dict) and a.get("limit") is None)
           for plan in plans.values() for a in (plan.get("allowances") or {}).values()):
        yield "DP3_CATALOG_UNRESOLVED"
    if any(plan.get("provider_prices") and any(v is None for v in plan["provider_prices"].values())
           for plan in plans.values()):
        yield "DP3_PROVIDER_PRICES"
    if any(plan.get("trial_days", None) is None for plan in plans.values()) or not plans:
        yield "DP6_TRIAL_POLICY"


def _retention_checks(settings: Any) -> Iterable[str]:
    path = Path(settings.retention_policy_path)
    if ".dev." in path.name:
        yield "DP4_DEV_RETENTION"
    policy = _raw_json(path) or {}
    periods = policy.get("retained_periods_days")
    if not isinstance(periods, dict) or any(v is None for v in periods.values()):
        yield "DP4_RETENTION_UNRESOLVED"


def _provider_checks(settings: Any) -> Iterable[str]:
    if settings.billing_provider in (None, "", "fake"):
        yield "DP1_PAYMENT_PROVIDER"
    if settings.email_provider != "smtp" or not (settings.smtp or {}).get("host"):
        yield "DP2_EMAIL_PROVIDER"
    origin = settings.public_origin or ""
    if not origin.startswith("https://"):
        yield "DP10_PRODUCTION_ORIGIN"
    if not settings.extension_ids:
        yield "DP10_EXTENSION_ID"


def _database_checks(settings: Any, conn_factory: Callable[[], Any], report: ReadinessReport) -> Iterable[str]:
    from webapp.services.entitlements import platform_controls
    try:
        conn = conn_factory()
    except Exception:  # noqa: BLE001 - READY_DATABASE reports the cause
        yield "DP5_LEGAL_UNPUBLISHED"
        return
    try:
        kinds = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM legal_documents WHERE published_at IS NOT "
                                            "NULL").fetchall()}
        if not {"TERMS", "PRIVACY"} <= kinds:
            yield "DP5_LEGAL_UNPUBLISHED"
        report.controls = platform_controls(conn, settings=settings)
    except Exception:  # noqa: BLE001 - an unmigrated database: READY_MIGRATIONS reports it
        yield "DP5_LEGAL_UNPUBLISHED"
    finally:
        conn.close()
    if not report.controls.get("SUBMIT_ENABLED"):
        yield "CONTROL_SUBMIT_DISABLED"
    if not report.controls.get("HOSTED_THREAT_MODEL_SIGNED_OFF"):
        yield "CONTROL_THREAT_MODEL_NOT_SIGNED_OFF"


def evaluate(settings: Any, *, decisions: Mapping[str, str], conn_factory: Callable[[], Any] | None = None,
             probes: Mapping[str, Callable[[Any], bool]] | None = None,
             certifications: Iterable[Any] | None = None) -> ReadinessReport:
    from product.submit_certification import LIVE_CERTIFIED, SUBMIT_CATALOGUE
    from webapp.persistence.db import connect

    hosted = settings.deployment == "hosted"
    report = ReadinessReport(deployment=settings.deployment)
    ids: list[str] = [] if hosted else ["DEPLOYMENT_NOT_HOSTED"]
    ids += list(_provider_checks(settings))
    ids += list(_catalog_checks(settings))
    ids += list(_retention_checks(settings))
    ids += [check for check, key in DECISION_KEYS.items() if not str(decisions.get(key) or "").strip()]
    ids += list(_database_checks(settings, conn_factory or (lambda: connect(settings)), report))
    for name, probe in (probes if probes is not None else DEFAULT_PROBES or _default_probes()).items():
        try:
            healthy = probe(settings)
        except Exception:  # noqa: BLE001 - a failing probe is reported by name
            healthy = False
        if not healthy:
            ids.append(f"READY_{name.upper()}")
    certs = list(certifications if certifications is not None else SUBMIT_CATALOGUE.values())
    report.certifications = [{"certification_id": c.certification_id, "adapter": c.adapter_version,
                              "status": c.status, "live_evidence": bool(c.live_evidence)} for c in certs]
    if not any(c.status == LIVE_CERTIFIED and c.live_evidence for c in certs):
        ids.append("COMMERCIAL_GATE_NO_LIVE_SUBMIT_ADAPTER")
    order = list(CHECK_IDS)
    for check in sorted(dict.fromkeys(ids), key=order.index):
        category, message = CHECK_IDS[check]
        if not hosted and check.startswith(("DP", "CONTROL_", "COMMERCIAL_")):
            category = "NOT_HOSTED"  # production decisions; local mode runs on the dev files by design
        report.findings.append(Finding(check, category, message))
    return report


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@contextmanager
def _environment(values: Mapping[str, str]):
    saved = dict(os.environ)
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def main(argv: list[str] | None = None, *, conn_factory: Callable[[], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, help="the deployment's environment file (KEY=VALUE lines)")
    args = parser.parse_args(argv)
    from webapp.config import Settings

    values = read_env_file(args.config) if args.config else {}
    with _environment(values):
        settings = Settings()
        decisions = {k: v for k, v in os.environ.items() if k.startswith("JOBSEARCH_DECISION_")}
        report = evaluate(settings, decisions=decisions, conn_factory=conn_factory)
    for finding in report.findings:
        print(f"[{finding.category}] {finding.id}: {finding.message}")
    print(f"deployment_ready: {str(report.deployment_ready).lower()}, "
          f"commercial_release: {str(report.commercial_release).lower()}")
    print(json.dumps(report.as_dict(), indent=1, sort_keys=True))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
