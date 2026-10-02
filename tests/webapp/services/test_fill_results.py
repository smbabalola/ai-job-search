"""6D-B fill-result.v1 and fill status (spec §15, §16.2)."""
from __future__ import annotations

import json

from product.autonomy_contract import canonical_hash
from product.fill_constants import RUN_LEASE_TTL
from tests.webapp.services.fill_fixtures import (  # noqa: F401
    NOW, V2_ACCOUNT, fill_world, grant_world, observation_doc, v2_chain,
)
from tests.webapp.services.test_fill_actions import filling, page_after, run_all
from tests.webapp.services.test_fill_runs import MS, observe, start
from webapp.persistence import fill as f
from webapp.services import fill_actions as fa
from webapp.services import fill_runs as fr
from webapp.services.fill_results import NON_CLAIMS, fill_status

SENTINELS = ("ada@example.com", "1 month")


def status(w, now=NOW):
    return fill_status(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws, now=now)


def pair(w):
    from webapp.persistence.handoff import create_extension_credential
    create_extension_credential(w.conn, account_id=V2_ACCOUNT, secret_hash="h" * 64)


def filled(w):
    run = filling(w)
    run_all(w, run)
    fa.final_validate(w.conn, run_id=run["id"], observation=page_after(w, 5), now=NOW)
    return run


def test_the_result_carries_the_exact_non_claims_all_false(grant_world):
    run = filled(grant_world)
    row = f.get_result(grant_world.conn, run["id"])
    result = row["result"]
    assert result["non_claims"] == NON_CLAIMS and set(NON_CLAIMS) == {
        "employer_received_answers", "employer_received_documents", "employer_persisted_application",
        "employer_accepted_application", "submitted", "submission_authorized",
        "fit_for_submission_without_6E_revalidation"}
    assert all(v is False for v in result["non_claims"].values())
    assert result["terminal_state"] == "FILLED_AWAITING_SUBMISSION" and result["plan_hash"] == grant_world.plan_hash
    assert [a["outcome"] for a in result["actions"]] == ["WRITTEN_VERIFIED", "WRITTEN_VERIFIED",
                                                         "ATTACH_LOCAL_VERIFIED", "ATTACH_LOCAL_VERIFIED",
                                                         "IGNORE_RECORDED"]
    assert result["quarantine_status"] == "TOTAL_VERIFIED" and result["grant_id"]
    assert row["result_hash"] == canonical_hash("fill-result", "v1", result)


def test_no_sentinel_cleartext_in_any_table_after_a_full_run(grant_world):
    filled(grant_world)
    conn = grant_world.conn
    from webapp.persistence.schema_catalog import schema_catalog

    tables = sorted(t for t in schema_catalog(conn)
                    if t.startswith(("fill_", "delta_classification_")) or t in ("active_fill_runs", "autonomy_grants"))
    assert "fill_action_events" in tables and "autonomy_grants" in tables
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table}").fetchall():
            text = json.dumps([str(v) for v in tuple(row)])
            for sentinel in SENTINELS:
                assert sentinel not in text, f"{sentinel!r} stored in {table}"


def test_status_not_started_then_ready_to_fill_needs_all_four(fill_world):
    assert status(fill_world) == {"status": "NOT_STARTED", "stale": False, "ready_to_fill": False,
                                  "run_id": None, "stop_reason": None}
    from webapp.services import fill_plans as fp
    shown = fp.fill_plan_presentation(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT,
                                      application_workspace_id=fill_world.ws,
                                      observation_id=fill_world.observation["id"], now=NOW)["displayed_plan_hash"]
    fp.confirm_plan(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT,
                    application_workspace_id=fill_world.ws, observation_id=fill_world.observation["id"],
                    displayed_plan_hash=shown, actor="u", now=NOW)
    assert status(fill_world)["ready_to_fill"] is False  # no paired extension yet
    pair(fill_world)
    assert status(fill_world)["ready_to_fill"] is True
    from webapp.services.review_approval import revoke
    revoke(fill_world.conn, settings=fill_world.settings, account_id=V2_ACCOUNT, application_workspace_id=fill_world.ws,
           actor="u", now=NOW)
    assert status(fill_world)["ready_to_fill"] is False


def test_ready_to_fill_needs_a_supported_observation(fill_world):
    pair(fill_world)
    doc = observation_doc()
    doc["context"]["multi_step_indicators"] = ["NEXT_BUTTON"]
    from tests.webapp.services.fill_fixtures import store_observation
    store_observation(fill_world, doc)
    assert status(fill_world)["ready_to_fill"] is False


def test_status_for_each_run_state(grant_world):
    run = start(grant_world)
    assert status(grant_world)["status"] == "FILLING"
    fr.stop_run(grant_world.conn, run_id=run["id"], reason="EXECUTION_CONTEXT_CLOSED", detail={}, now=NOW)
    assert (status(grant_world)["status"], status(grant_world)["stop_reason"]) == ("FILL_STOPPED",
                                                                                   "EXECUTION_CONTEXT_CLOSED")
    run = filled(grant_world)
    assert status(grant_world)["status"] == "FILLED_AWAITING_SUBMISSION"
    assert status(grant_world, now=NOW + RUN_LEASE_TTL + MS)["status"] == "FILLED_CONTEXT_UNVERIFIED"
    fr.reap_expired_leases(grant_world.conn, now=NOW + RUN_LEASE_TTL + MS)
    assert status(grant_world)["status"] == "FILLED_CONTEXT_UNVERIFIED"


def test_status_plan_needs_review_and_unsupported(fill_world):
    run = start(fill_world)
    observe(fill_world, run, "INITIAL")
    assert status(fill_world)["status"] == "PLAN_NEEDS_REVIEW"
    doc = observation_doc()
    doc["context"]["multi_step_indicators"] = ["NEXT_BUTTON"]
    run = start(fill_world, tab=2)
    observe(fill_world, run, "INITIAL", doc)
    assert status(fill_world)["status"] == "UNSUPPORTED_FORM"


def test_status_is_stale_after_a_new_approval(grant_world):
    filled(grant_world)
    assert status(grant_world)["stale"] is False
    from webapp.services.review_approval import revoke
    revoke(grant_world.conn, settings=grant_world.settings, account_id=V2_ACCOUNT,
           application_workspace_id=grant_world.ws, actor="u", now=NOW)
    grant_world.world.make_approvable()
    grant_world.world.approve()
    assert status(grant_world)["stale"] is True


def test_a_post_fill_change_marks_the_filled_surface_stale(grant_world):
    run = filled(grant_world)
    fa.record_detection(grant_world.conn, run_id=run["id"], kind="POST_FILL_CHANGE_OBSERVED", detail={}, now=NOW)
    assert status(grant_world)["stale"] is True and status(grant_world)["status"] == "FILLED_AWAITING_SUBMISSION"
