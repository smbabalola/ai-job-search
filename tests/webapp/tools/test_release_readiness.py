"""The release-readiness tool (Bundle 7 spec §4, §27.2-3, §27.3): every open
decision point, failing /ready probe, platform-control and certification gap
is a finding with a stable id; deployment-ready and the commercial release
are reported separately (spec §27.2-6)."""
from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from product.submit_certification import GREENHOUSE_SUBMIT, LIVE_CERTIFIED
from webapp.config import Settings
from webapp.persistence.db import connect, init_db

ROOT = Path(__file__).parents[3]
PROD_CATALOG = ROOT / "product/plans/plan-catalog.v1.json"
PROD_RETENTION = ROOT / "product/policies/retention-policy.v1.json"
DECISIONS = {"JOBSEARCH_DECISION_DP7_REFUNDS": "No app-issued refunds; provider dashboard only",
             "JOBSEARCH_DECISION_DP8_GRANDFATHERING": "Pinned catalog versions",
             "JOBSEARCH_DECISION_DP9_DISCOVERY": "Portal-CLI sources off; manual capture only"}
ALL_READY = {name: (lambda settings: True) for name in
             ("database", "migrations", "object_store", "catalog", "email_provider", "secret_key")}


def _db(tmp_path, *, legal=False, controls=()):
    path = tmp_path / "jobsearch.sqlite3"
    init_db(path)
    conn = connect(path)
    if legal:
        from tests.webapp.auth_helpers import publish_legal_documents
        publish_legal_documents(path)
    from webapp.services.entitlements import set_platform_control
    for key in controls:
        set_platform_control(conn, key, True, actor_user_id=None, reason="test", now=datetime.now(timezone.utc))
    conn.commit()
    conn.close()
    return lambda: connect(path)


def _resolved_files(tmp_path):
    catalog = json.loads(PROD_CATALOG.read_text(encoding="utf-8"))
    for plan_id, plan in catalog["plans"].items():
        plan["trial_days"] = 0
        for allowance in plan["allowances"].values():
            allowance["limit"] = 10
        if plan.get("provider_prices"):
            plan["provider_prices"] = {k: f"price_{plan_id}_{k}" for k in plan["provider_prices"]}
    retention = json.loads(PROD_RETENTION.read_text(encoding="utf-8"))
    retention["retained_periods_days"] = {k: 2555 for k in retention["retained_periods_days"]}
    (tmp_path / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    (tmp_path / "retention.json").write_text(json.dumps(retention), encoding="utf-8")
    return tmp_path / "catalog.json", tmp_path / "retention.json"


def _hosted(tmp_path, **overrides):
    values = dict(db_path=tmp_path / "jobsearch.sqlite3", deployment="hosted", plan_catalog_path=PROD_CATALOG,
                  retention_policy_path=PROD_RETENTION, billing_provider="fake", email_provider="console",
                  public_origin=None, extension_ids=())
    values.update(overrides)
    return Settings(**values)


def _resolved(tmp_path):
    catalog, retention = _resolved_files(tmp_path)
    return _hosted(tmp_path, plan_catalog_path=catalog, retention_policy_path=retention, billing_provider="stripe",
                   email_provider="smtp", smtp={"host": "smtp.example.com", "from_address": "hi@example.com"},
                   public_origin="https://app.example.com", extension_ids=("abcdefghijklmnopabcdefghijklmnop",))


def test_the_production_files_with_no_adapters_list_every_decision_point(tmp_path):
    from webapp.tools.release_readiness import evaluate
    report = evaluate(_hosted(tmp_path), decisions={}, conn_factory=_db(tmp_path), probes=ALL_READY)
    assert not report.ok and not report.deployment_ready and not report.commercial_release
    open_dps = {int(m.group(1)) for f in report.findings if (m := re.match(r"DP(\d+)_", f.id))}
    assert open_dps == set(range(1, 11)), sorted(f.id for f in report.findings)
    assert all(f.category == "DP" for f in report.findings if f.id.startswith("DP"))


def test_every_decision_resolved_but_no_live_submit_adapter_is_deployment_ready_only(tmp_path):
    from webapp.tools.release_readiness import evaluate
    report = evaluate(_resolved(tmp_path), decisions=DECISIONS, conn_factory=_db(tmp_path, legal=True),
                      probes=ALL_READY)
    ids = {f.id for f in report.findings}
    assert "COMMERCIAL_GATE_NO_LIVE_SUBMIT_ADAPTER" in ids
    assert {f.category for f in report.findings} == {"COMMERCIAL"}, ids
    assert (report.ok, report.deployment_ready, report.commercial_release) == (False, True, False)
    assert report.certifications == [{"certification_id": "greenhouse@2/submit@1", "adapter": "greenhouse@2",
                                      "status": "FIXTURE_CERTIFIED", "live_evidence": False}]


def test_a_live_certified_adapter_and_the_controls_on_pass_the_commercial_gate(tmp_path):
    from webapp.tools.release_readiness import evaluate
    live = dataclasses.replace(GREENHOUSE_SUBMIT, status=LIVE_CERTIFIED, live_evidence="evidence/greenhouse-live.md")
    report = evaluate(_resolved(tmp_path), decisions=DECISIONS,
                      conn_factory=_db(tmp_path, legal=True, controls=("SUBMIT_ENABLED", "HOSTED_THREAT_MODEL_SIGNED_OFF")),
                      probes=ALL_READY, certifications=[live])
    assert report.findings == [] and report.ok and report.deployment_ready and report.commercial_release
    assert report.controls["SUBMIT_ENABLED"] is True


def test_a_failing_ready_probe_is_a_finding_by_name(tmp_path):
    from webapp.tools.release_readiness import evaluate
    probes = {**ALL_READY, "object_store": lambda settings: False}
    report = evaluate(_resolved(tmp_path), decisions=DECISIONS, conn_factory=_db(tmp_path, legal=True), probes=probes)
    assert "READY_OBJECT_STORE" in {f.id for f in report.findings} and not report.deployment_ready


def test_local_mode_with_the_dev_files_fails_only_with_not_hosted_findings(tmp_path):
    from webapp.tools.release_readiness import evaluate
    settings = Settings(db_path=tmp_path / "jobsearch.sqlite3", documents_root=tmp_path / "documents")
    report = evaluate(settings, decisions={}, conn_factory=_db(tmp_path))  # the real /ready probes
    assert not report.ok and report.findings
    assert {f.category for f in report.findings} == {"NOT_HOSTED"}, [(f.id, f.category) for f in report.findings]
    assert "DEPLOYMENT_NOT_HOSTED" in {f.id for f in report.findings}


def test_every_finding_id_is_stable_and_registered(tmp_path):
    from webapp.tools.release_readiness import CHECK_IDS, evaluate
    report = evaluate(_hosted(tmp_path), decisions={}, conn_factory=_db(tmp_path), probes=ALL_READY)
    for finding in report.findings:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]+", finding.id) and finding.id in CHECK_IDS, finding.id


def test_the_command_reads_an_env_file_and_exits_non_zero(tmp_path, capsys, monkeypatch):
    from webapp.tools import release_readiness
    env = tmp_path / "production.env"
    env.write_text("# production\nJOBSEARCH_DEPLOYMENT=hosted\nJOBSEARCH_DECISION_DP7_REFUNDS='none'\n",
                   encoding="utf-8")
    monkeypatch.setattr(release_readiness, "DEFAULT_PROBES", ALL_READY)
    code = release_readiness.main(["--config", str(env)], conn_factory=_db(tmp_path))
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{"):])
    assert code == 1 and report["ok"] is False
    assert "DP7_REFUND_POLICY" not in {f["id"] for f in report["findings"]}  # read from the file
    assert "DP8_GRANDFATHERING" in {f["id"] for f in report["findings"]}
