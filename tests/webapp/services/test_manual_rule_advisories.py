"""Bundle 7 Task 23 (spec §16.2-§16.3): where rules act. Automated paths
evaluate standing-policy v2 as 6B/6C evaluate v1 (BLOCK → not prepared);
manual mode shows advisories and a BLOCK needs the user's acknowledgement
before approval; a rule changed after an approval never invalidates it."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from product.autonomy_contract import Capability, Mode, ResultKind
from product.autonomy_gate import evaluate_authorization
from product.standing_policy import build_well_known_rule, default_policy_document
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401
from tests.webapp.services.review_fixtures import V2_ACCOUNT, v2_chain  # noqa: F401
from tests.webapp.services.test_autonomy_context import patch_external_reads, seed_workspace
from webapp.persistence.artifacts import get_current_artifact, save_artifact
from webapp.persistence.autonomy_authority import save_policy_version
from webapp.services import cv_strategy
from webapp.services import manual_rules as mr
from webapp.services.autonomy_context import build_context
from webapp.services.autonomy_controls import enable_autonomous_preparation

BLOCK_TITLE = {"id": "no-title-x", "description": "d",
               "when": {"not": {"attr": "job.title", "op": "eq", "value": "zzz"}},
               "effect": {"type": "BLOCK"}, "on_unknown": {"type": "REQUIRE_USER"}}
ASK_TITLE = {**BLOCK_TITLE, "id": "ask", "effect": {"type": "REQUIRE_USER"}}


def _policy(conn, account, *rules, now=NOW, currency=None):
    doc = default_policy_document("Europe/London")
    doc["rules"] = list(rules)
    if currency:
        doc["currency"] = currency
    save_policy_version(conn, account_id=account, doc=doc, created_by="u", now=now)


# ---- v2 attributes and the automated path -------------------------------------------------

@pytest.fixture
def settings(tmp_path):
    from webapp.config import Settings
    return Settings(db_path=tmp_path / "autonomy.sqlite3", autonomy_max_capability="PREPARE")


def _seed_v2(conn, monkeypatch):
    patch_external_reads(monkeypatch)
    ws = seed_workspace(conn)
    posting = dict(get_current_artifact(conn, ws, "job_posting_snapshot")["payload"])
    posting.update({"compensation": {"currency": "GBP", "max": 250, "period": "day"},
                    "description": "Offshore, 28/28 rotation, helicopter transfers."})
    save_artifact(conn, workspace_id=ws, artifact_type="job_posting_snapshot", payload=posting)
    save_artifact(conn, workspace_id=ws, artifact_type="job_understanding_result",
                  payload={"title": "Drilling Fluids Engineer", "location": {"country_code": "gb"},
                           "remote_mode": "onsite"})
    cv_strategy.save_job_families(conn, SimpleNamespace(account_id=ACCOUNT, user_id=None), {"families": [
        {"id": "drilling", "name": "Drilling", "match": {"title_any": ["drilling"]}, "priority": 0}]}, now=NOW)
    conn.commit()
    return ws


def _ctx(conn, settings, ws):
    return build_context(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=ws,
                         requested_stage=Capability.PREPARE, mode=Mode.LIVE, now=NOW, sentinel_present=False)


def test_the_context_supplies_the_v2_attributes(conn, settings, monkeypatch):
    ws = _seed_v2(conn, monkeypatch)
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    _policy(conn, ACCOUNT, currency="GBP")
    attrs = _ctx(conn, settings, ws).attributes
    assert (attrs["job.family"], attrs["job.compensation_max_annual"], attrs["job.country"], attrs["job.remote_mode"],
            attrs["job.rotation"]) == ("drilling", 65_000, "GB", "onsite", "28/28")


def test_an_automated_block_rule_means_not_prepared(conn, settings, monkeypatch):
    ws = _seed_v2(conn, monkeypatch)
    enable_autonomous_preparation(conn, account_id=ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    allowed = evaluate_authorization(_ctx(conn, settings, ws))
    assert allowed.effective_capability == Capability.PREPARE
    family_block = {"id": "no-drilling", "description": "d",
                    "when": {"attr": "job.family", "op": "eq", "value": "drilling"},
                    "effect": {"type": "BLOCK"}, "on_unknown": {"type": "REQUIRE_USER"}}
    _policy(conn, ACCOUNT, family_block, now=NOW + timedelta(seconds=1))
    blocked = evaluate_authorization(_ctx(conn, settings, ws))
    assert blocked.result is not ResultKind.ALLOW or blocked.effective_capability == Capability.NONE


def test_a_salary_floor_below_the_day_rate_is_not_an_advisory(conn, settings, monkeypatch):
    ws = _seed_v2(conn, monkeypatch)
    floor = build_well_known_rule("pref.salary_floor", {"amount": 60_000, "effect": "BLOCK",
                                                        "on_unknown": "REQUIRE_USER"})
    _policy(conn, ACCOUNT, floor, currency="GBP")
    assert mr.advisories(conn, account_id=ACCOUNT, workspace_id=ws) == []
    _policy(conn, ACCOUNT, floor, currency="USD", now=NOW + timedelta(seconds=1))  # USD vs GBP → unknown
    assert [a.effect for a in mr.advisories(conn, account_id=ACCOUNT, workspace_id=ws)] == ["REQUIRE_USER"]


# ---- manual mode on the 6D-A chain ---------------------------------------------------------

def test_a_block_advisory_refuses_approval_until_acknowledged(v2_chain):
    from webapp.services.review_approval import ReviewRefused
    _policy(v2_chain.conn, V2_ACCOUNT, BLOCK_TITLE)
    v2_chain.make_approvable()
    assert [a.rule_id for a in mr.unacknowledged_blocks(v2_chain.conn, account_id=V2_ACCOUNT,
                                                        workspace_id=v2_chain.ws)] == ["no-title-x"]
    with pytest.raises(ReviewRefused, match="rule_acknowledgement_required"):
        v2_chain.approve()
    mr.acknowledge(v2_chain.conn, account_id=V2_ACCOUNT, workspace_id=v2_chain.ws, rule_id="no-title-x", actor="u",
                   now=NOW)
    v2_chain.conn.commit()
    assert mr.unacknowledged_blocks(v2_chain.conn, account_id=V2_ACCOUNT, workspace_id=v2_chain.ws) == []
    assert v2_chain.approve()["approval_id"]


def test_require_user_is_displayed_only(v2_chain):
    _policy(v2_chain.conn, V2_ACCOUNT, ASK_TITLE)
    v2_chain.make_approvable()
    assert [(a.rule_id, a.effect) for a in mr.advisories(v2_chain.conn, account_id=V2_ACCOUNT,
                                                         workspace_id=v2_chain.ws)] == [("ask", "REQUIRE_USER")]
    assert v2_chain.approve()["approval_id"]


def test_a_rule_added_after_approval_does_not_invalidate_it_or_stop_the_fill(v2_chain):
    _policy(v2_chain.conn, V2_ACCOUNT, ASK_TITLE, now=NOW - timedelta(minutes=1))  # the policy at approval time
    v2_chain.make_approvable()
    v2_chain.approve()
    _policy(v2_chain.conn, V2_ACCOUNT, BLOCK_TITLE, now=NOW + timedelta(minutes=5))
    later = NOW + timedelta(minutes=6)
    assert v2_chain.state(now=later).approval_effective is True
    assert mr.fill_start_refusal(v2_chain.conn, account_id=V2_ACCOUNT, workspace_id=v2_chain.ws, now=later) is None


def test_the_fill_start_refuses_a_block_that_existed_when_approved_without_acknowledgement(v2_chain):
    """Defense in depth: an approval that never passed the acknowledgement check
    (e.g. made before Bundle 7) cannot start a fill while its BLOCK stands."""
    from webapp.persistence import review_approval as ra
    _policy(v2_chain.conn, V2_ACCOUNT, BLOCK_TITLE)
    v2_chain.make_approvable()
    state = v2_chain.state()
    ra.insert_approval(v2_chain.conn, account_id=V2_ACCOUNT, application_workspace_id=v2_chain.ws,
                       binding=state.binding, binding_hash=state.binding_hash, supersedes_id=None, batch_id=None,
                       resolved_delta_ids=[], actor="u", now=NOW + timedelta(seconds=1))
    v2_chain.conn.commit()
    later = NOW + timedelta(seconds=2)
    assert mr.fill_start_refusal(v2_chain.conn, account_id=V2_ACCOUNT, workspace_id=v2_chain.ws,
                                 now=later) == "rule_acknowledgement_required"
    mr.acknowledge(v2_chain.conn, account_id=V2_ACCOUNT, workspace_id=v2_chain.ws, rule_id="no-title-x", actor="u",
                   now=later)
    assert mr.fill_start_refusal(v2_chain.conn, account_id=V2_ACCOUNT, workspace_id=v2_chain.ws, now=later) is None


def test_an_unacknowledged_block_is_an_action_required_item(v2_chain):
    from webapp.config import Settings
    from webapp.services import inbox
    _policy(v2_chain.conn, V2_ACCOUNT, BLOCK_TITLE)
    items = inbox.action_required(v2_chain.conn, SimpleNamespace(account_id=V2_ACCOUNT, user_id=None),
                                  settings=v2_chain.settings, now=NOW)
    assert ("rule_conflict", v2_chain.ws) in [(i.kind, i.subject_id) for i in items]


def test_the_refusal_is_the_rule_acknowledgement_required_contract():
    import json as _json
    from webapp.api.review_approval import call
    from webapp.services.review_application import ReviewRefused

    def refuse():
        raise ReviewRefused("rule_acknowledgement_required")

    response = call(refuse)
    assert response.status_code == 409 and _json.loads(response.body)["error"] == "RULE_ACKNOWLEDGEMENT_REQUIRED"


def test_migration_034_resaves_each_current_v1_policy_as_v2_once(conn):
    import json as _json
    from webapp.persistence.autonomy_authority import current_policy
    from webapp.persistence.bundle7_migrations import _resave_policies_as_v2
    doc = default_policy_document("Europe/London")
    doc["schema_version"] = "standing-policy.v1"
    doc["rules"] = [BLOCK_TITLE]
    conn.execute("INSERT INTO standing_policy_versions (id, account_id, policy_json, policy_hash, created_by, "
                 "created_at) VALUES ('pol_v1', ?, ?, 'h', 'u', ?)", (ACCOUNT, _json.dumps(doc), NOW.isoformat()))
    conn.commit()
    for _ in range(2):
        _resave_policies_as_v2(conn)
    conn.commit()
    current = current_policy(conn, ACCOUNT)["doc"]
    assert current["schema_version"] == "standing-policy.v2" and current["rules"] == [BLOCK_TITLE]
    assert conn.execute("SELECT COUNT(*) FROM standing_policy_versions").fetchone()[0] == 2
