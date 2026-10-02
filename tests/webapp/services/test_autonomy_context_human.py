"""6E-A spec §8.2/§9.5: build_context(authority=HUMAN_SUBMIT) takes the
deployment ceiling from the human-submit switch and the adapter's submit
capability from the submit certification (not the 6B adapter env list)."""
from __future__ import annotations

import dataclasses

from product.autonomy_contract import AuthorityKind, Capability, Mode
from webapp.config import Settings
from webapp.services.autonomy_context import ApplyTargetObservation, build_context
from tests.webapp.services.fill_fixtures import NOW, V2_ACCOUNT, grant_world, v2_chain  # noqa: F401

OBS = ApplyTargetObservation(adapter_id="greenhouse", adapter_version="greenhouse@2", landing_within_redirect_set=True,
                             tenant_key="acme", tenant_matches_employer=True, ats_job_id_matches=True,
                             unexplained_redirect=False)


def ctx(w, *, authority=AuthorityKind.HUMAN_SUBMIT, origin="http://127.0.0.1:8430", **settings):
    s = dataclasses.replace(w.settings, **settings)
    return build_context(w.conn, settings=s, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                         requested_stage=Capability.SUBMIT, mode=Mode.LIVE, now=NOW, sentinel_present=False,
                         observation=OBS, authority=authority, submit_origin=origin)


def test_settings_default_both_switches_off(monkeypatch):
    monkeypatch.delenv("JOBSEARCH_HUMAN_SUBMIT_ENABLED", raising=False)
    monkeypatch.delenv("JOBSEARCH_SUBMIT_FIXTURE_ORIGINS", raising=False)
    s = Settings()
    assert s.human_submit_enabled is False and s.submit_fixture_origins_enabled is False
    assert s.human_submit_ceiling() is Capability.FILL
    monkeypatch.setenv("JOBSEARCH_HUMAN_SUBMIT_ENABLED", "1")
    monkeypatch.setenv("JOBSEARCH_SUBMIT_FIXTURE_ORIGINS", "1")
    s = Settings()
    assert s.human_submit_enabled and s.submit_fixture_origins_enabled
    assert s.human_submit_ceiling() is Capability.SUBMIT


def test_human_context_uses_the_human_switch_and_the_submit_certification(grant_world):
    c = ctx(grant_world, human_submit_enabled=True, submit_fixture_origins_enabled=True)
    assert c.authority is AuthorityKind.HUMAN_SUBMIT
    assert c.deployment_ceiling is Capability.SUBMIT
    assert c.apply_target.adapter_submit_capable is True


def test_human_switch_off_caps_the_deployment_ceiling_at_fill(grant_world):
    assert ctx(grant_world, human_submit_enabled=False,
               submit_fixture_origins_enabled=True).deployment_ceiling is Capability.FILL


def test_a_non_loopback_origin_is_not_submit_capable_for_a_fixture_certified_adapter(grant_world):
    c = ctx(grant_world, origin="https://boards.greenhouse.io", human_submit_enabled=True,
            submit_fixture_origins_enabled=True)
    assert c.apply_target.adapter_submit_capable is False


def test_standing_policy_contexts_are_unchanged(grant_world):
    c = ctx(grant_world, authority=AuthorityKind.STANDING_POLICY, human_submit_enabled=True,
            submit_fixture_origins_enabled=True, autonomy_submit_capable_adapters=())
    assert c.authority is AuthorityKind.STANDING_POLICY
    assert c.deployment_ceiling is grant_world.settings.autonomy_deployment_ceiling()
    assert c.apply_target.adapter_submit_capable is False  # the 6B env list, not the certification
