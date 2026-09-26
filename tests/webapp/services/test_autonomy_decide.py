from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from product.autonomy_contract import Capability, Mode, ResultKind
from product.fill_manifest import value_hash
from webapp.persistence.autonomy_ledger import count_usage, get_grant, list_decisions
from webapp.services.autonomy import binding_drift, decide_and_record, request_grant
from webapp.services.autonomy_context import ApplyTargetObservation, day_window
from webapp.services.autonomy_controls import (
    enable_autonomous_preparation, engage_kill_switch, set_capability,
)
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401
from tests.webapp.services.test_autonomy_context import URL, seeded, settings  # noqa: F401

OBS = ApplyTargetObservation(adapter_id="greenhouse", adapter_version="1.0.0", landing_within_redirect_set=True,
                             tenant_key=None, tenant_matches_employer=True, ats_job_id_matches=True,
                             unexplained_redirect=False)


def manifest(ws):
    return {"schema_version": "fill-manifest.v1", "application_workspace_id": ws, "adapter_id": "greenhouse",
            "adapter_version": "1.0.0", "pages": [{"page_key": "p1", "entries": [
                {"page_field_key": "email", "normalized_field_type": "email", "subject": None,
                 "source": {"kind": "EVIDENCE", "ref": "contact.email", "confirmation_id": None},
                 "transform_id": "identity", "value_hash": value_hash("a@b.c"), "required": True}]}]}


def _run(conn, run_id="run_1"):
    # SUBMIT needs an active run when submit_per_run is configured (Task 15 ruling).
    from webapp.persistence.autonomy_authority import get_run, start_run
    if get_run(conn, run_id) is None:
        start_run(conn, account_id=ACCOUNT, started_by="SCHEDULER", now=NOW, run_id=run_id)
    return run_id


def authorize_all(conn):
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    for scope_type, scope_id in (("ACCOUNT_MAX", ACCOUNT), ("WORKSPACE_CEILING", "sw_1")):
        set_capability(conn, account_id=ACCOUNT, scope_type=scope_type, scope_id=scope_id,
                       capability=Capability.SUBMIT, actor="u", now=NOW)


def test_every_evaluation_is_recorded_including_denials(conn, settings, seeded):
    decide_and_record(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                      requested_stage=Capability.PREPARE, mode=Mode.SHADOW, now=NOW)
    engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    decide_and_record(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                      requested_stage=Capability.PREPARE, mode=Mode.LIVE, now=NOW)
    rows = list_decisions(conn, seeded)
    assert [(r["mode"], r["result"]) for r in rows] == [("SHADOW", "ALLOW"), ("LIVE", "DENY")]


def test_fill_grant_issued_with_ttl_and_reservation(conn, settings, seeded):
    authorize_all(conn)
    out = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                        stage=Capability.FILL, now=NOW, fill_manifest=manifest(seeded), observation=OBS)
    assert out.decision.grantable and out.grant["stage"] == "FILL"
    assert out.grant["expires_at"].startswith("2026-09-24T12:30:00")
    day, _ = day_window(NOW, "Europe/London")
    assert count_usage(conn, account_id=ACCOUNT, counter_name="fill_per_day", window_key=day) == 1


def test_no_grant_when_not_grantable(conn, settings, seeded):
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    out = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                        stage=Capability.FILL, now=NOW, fill_manifest=manifest(seeded), observation=OBS)
    assert out.grant is None and out.decision.effective_capability == Capability.PREPARE


def test_submit_grant_binds_manifest_target_identity_and_policy(conn, settings, seeded):
    authorize_all(conn)
    out = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                        stage=Capability.SUBMIT, now=NOW, fill_manifest=manifest(seeded), observation=OBS,
                        run_id=_run(conn))
    assert out.decision.grantable, out.decision.reasons
    binding = get_grant(conn, out.grant["id"])["binding"]
    assert set(binding) >= {"stage", "pack_artifact_id", "fill_manifest", "fill_manifest_hash", "answers",
                            "apply_target", "identity_key", "policy_version_hash", "subject_policy_hash",
                            "engine_version", "account_id", "application_workspace_id"}
    assert out.grant["expires_at"].startswith("2026-09-24T12:02:00")


def test_submit_requires_manifest(conn, settings, seeded):
    authorize_all(conn)
    with pytest.raises(ValueError):
        request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                      stage=Capability.SUBMIT, now=NOW, fill_manifest=None, observation=OBS)


def test_binding_drift_lists_changed_keys():
    assert binding_drift({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == ("b", "c")


# ---- hardening beyond the plan's brief ---------------------------------------

def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("autonomy_decisions", "autonomy_grants", "limit_reservations")}


@pytest.mark.parametrize("scope_type, scope_id", [("APPLICATION", "ws"), ("SEARCH_WORKSPACE", "sw_1")])
def test_pause_means_no_new_decisions_and_no_new_grants(conn, settings, seeded, scope_type, scope_id):
    from webapp.services.autonomy import AutonomyPaused
    from webapp.services.autonomy_controls import pause, resume
    authorize_all(conn)
    scope = seeded if scope_id == "ws" else scope_id
    pause(conn, account_id=ACCOUNT, scope_type=scope_type, scope_id=scope, actor="u", reason="r", now=NOW)
    before = _counts(conn)
    with pytest.raises(AutonomyPaused):
        decide_and_record(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                          requested_stage=Capability.PREPARE, mode=Mode.SHADOW, now=NOW)
    with pytest.raises(AutonomyPaused):
        request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                      stage=Capability.FILL, now=NOW, fill_manifest=manifest(seeded), observation=OBS)
    assert _counts(conn) == before
    resume(conn, account_id=ACCOUNT, scope_type=scope_type, scope_id=scope, actor="u", reason="r", now=NOW)
    out = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                        stage=Capability.FILL, now=NOW, fill_manifest=manifest(seeded), observation=OBS)
    assert out.grant is not None


@pytest.mark.parametrize("account_id, workspace", [("acct_missing", "ws"), (ACCOUNT, "ws_missing")])
def test_invalid_identity_fails_atomically_with_no_decision_or_grant(conn, settings, seeded, account_id, workspace):
    # Carried acceptance (user, 2026-09-26): the complete authorization-to-grant
    # operation fails atomically and leaves zero new decision or grant rows.
    import sqlite3
    authorize_all(conn)
    ws = seeded if workspace == "ws" else workspace
    before = _counts(conn)
    with pytest.raises(sqlite3.IntegrityError):
        request_grant(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                      stage=Capability.FILL, now=NOW, fill_manifest=manifest(ws), observation=OBS)
    with pytest.raises(sqlite3.IntegrityError):
        decide_and_record(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                          requested_stage=Capability.FILL, mode=Mode.LIVE, now=NOW)
    assert _counts(conn) == before
    assert not conn.in_transaction


def test_sentinel_observed_inside_the_grant_transaction(conn, settings, seeded):
    from webapp.persistence.autonomy_authority import kill_switch_state
    authorize_all(conn)
    settings.autonomy_sentinel_path.write_text("halt")
    out = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                        stage=Capability.FILL, now=NOW, fill_manifest=manifest(seeded), observation=OBS)
    assert out.grant is None and out.decision.deny_reason == "kill_switch"
    assert kill_switch_state(conn, ACCOUNT)["engaged"] is True
    assert list_decisions(conn, seeded)[-1]["deny_reason"] == "kill_switch"


def test_fill_grant_requires_a_manifest(conn, settings, seeded):
    authorize_all(conn)
    with pytest.raises(ValueError):
        request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                      stage=Capability.FILL, now=NOW, fill_manifest=None, observation=OBS)


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(application_workspace_id="ws_other"),
    lambda m: m.update(adapter_id="lever"),
    lambda m: m.update(adapter_version="2.0.0"),
])
def test_manifest_must_match_request_workspace_and_observed_adapter(conn, settings, seeded, mutate):
    authorize_all(conn)
    m = manifest(seeded)
    mutate(m)
    before = _counts(conn)
    with pytest.raises(ValueError):
        request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                      stage=Capability.FILL, now=NOW, fill_manifest=m, observation=OBS)
    assert _counts(conn) == before


def test_binding_includes_target_url_adapter_version_and_tenant(conn, settings, seeded):
    from webapp.services.autonomy_context import canonical_target_url
    authorize_all(conn)
    out = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                        stage=Capability.SUBMIT, now=NOW, fill_manifest=manifest(seeded), observation=OBS,
                        run_id=_run(conn))
    target = get_grant(conn, out.grant["id"])["binding"]["apply_target"]
    assert target["canonical_url"] == canonical_target_url(URL)
    assert (target["adapter_version"], target["tenant_key"]) == ("1.0.0", None)


def test_refuses_to_run_inside_a_caller_transaction(conn, settings, seeded):
    conn.execute("UPDATE workspaces SET title = 'x' WHERE id = ?", (seeded,))
    with pytest.raises(RuntimeError):
        decide_and_record(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                          requested_stage=Capability.PREPARE, mode=Mode.SHADOW, now=NOW)
    conn.rollback()
