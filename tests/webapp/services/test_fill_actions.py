"""6D-B per-action protocol (spec §11.3, §12.4, §13): authority-rechecked
envelopes, at most one outcome, stops before the next action, final
validation and detections."""
from __future__ import annotations

import sqlite3
import threading

from webapp.persistence.db import connect
from datetime import timedelta

import pytest

from product.fill_constants import VALUE_ENVELOPE_TTL
from product.fill_hash import fill_value_hash
from tests.product.fill_observation_fixtures import element
from tests.webapp.services.fill_fixtures import (  # noqa: F401
    NOW, V2_ACCOUNT, grant_world, observation_doc, v2_chain,
)
from tests.webapp.services.test_fill_runs import RULESET, events, to_active
from webapp.persistence import fill as f
from webapp.persistence import review_approval as ra
from webapp.persistence.autonomy_ledger import get_grant
from webapp.services import fill_actions as fa
from webapp.services import fill_runs as fr
from webapp.services.autonomy_controls import engage_kill_switch, pause, set_capability

MS = timedelta(milliseconds=1)
KEYS = ["gh:email", "gh:notice", "gh:resume", "gh:cover", "gh:csrf"]


def filling(w):
    run = to_active(w)
    assert fr.request_fill_grant(w.conn, settings=w.settings, run_id=run["id"], now=NOW).granted
    return run


def plan(w):
    return f.get_plan_by_hash(w.conn, w.plan_hash)["plan"]


def precheck(w, run, i, **overrides):
    binding = f.get_grant_binding(w.conn, run["id"])
    out = {"ruleset_hash": RULESET, "structure_fingerprint": binding["structure_fingerprint"],
           "field_fingerprint": plan(w)["actions"][i]["field_fingerprint"], "siblings_contained": True}
    out.update(overrides)
    return out


def intent(w, run, i, now=NOW, **overrides):
    return fa.request_intent(w.conn, settings=w.settings, run_id=run["id"], action_index=i,
                             precheck=precheck(w, run, i, **overrides), now=now)


def page_after(w, done, *extra):
    """The page with the first `done` actions' values in place."""
    doc = observation_doc(*extra)
    actions = plan(w)["actions"]
    for e in doc["elements"]:
        index = KEYS.index(e["page_field_key"]) if e["page_field_key"] in KEYS else None
        if index is not None and index < done and actions[index]["rendered_value_hash"]:
            e["value_state"] = {"state": "NONBLANK", "current_value_hash": actions[index]["rendered_value_hash"]}
    return doc


def outcome(w, run, i, env, value=None, readback=None, doc=None, now=NOW):
    action = plan(w)["actions"][i]
    value = value or {"WRITE": "WRITTEN_VERIFIED", "ATTACH_LOCAL": "ATTACH_LOCAL_VERIFIED", "OMIT": "OMIT_VERIFIED",
                      "IGNORE_NON_APPLICATION": "IGNORE_RECORDED"}[action["action_kind"]]
    readback = readback if readback is not None else action["rendered_value_hash"]
    return fa.record_outcome(w.conn, run_id=run["id"], action_index=i, envelope_id=env.envelope_id if env else None,
                             outcome=value, readback_hash=readback, post_observation=doc or page_after(w, i + 1),
                             now=now)


def do(w, run, i):
    env = intent(w, run, i).envelope
    return outcome(w, run, i, env)


def envelopes(w):
    return w.conn.execute("SELECT COUNT(*) FROM fill_action_events WHERE event = 'ENVELOPE_ISSUED'").fetchone()[0]


# ---- intents ------------------------------------------------------------------------------

def test_a_write_intent_returns_one_envelope_with_the_rendered_value(grant_world):
    run = filling(grant_world)
    out = intent(grant_world, run, 0)
    env = out.envelope
    assert (env.action_kind, env.rendered_value) == ("WRITE", "ada@example.com")
    assert env.rendered_value_hash == fill_value_hash("ada@example.com") and env.expires_at
    kinds = [e["event"] for e in f.action_events(grant_world.conn, run["id"], 0)]
    assert kinds == ["PRECHECK", "WRITE_INTENT", "ENVELOPE_ISSUED"]


def test_a_retried_intent_returns_the_same_unused_envelope(grant_world):
    run = filling(grant_world)
    first = intent(grant_world, run, 0).envelope
    again = intent(grant_world, run, 0).envelope
    assert again.envelope_id == first.envelope_id and again.rendered_value == first.rendered_value
    assert envelopes(grant_world) == 1


def test_attach_local_unlocks_only_the_approved_document(grant_world):
    run = filling(grant_world)
    do(grant_world, run, 0)
    do(grant_world, run, 1)
    env = intent(grant_world, run, 2).envelope
    action = plan(grant_world)["actions"][2]
    assert env.rendered_value is None and env.document["sha256"] == action["document"]["sha256"]
    assert env.document["document_version_id"] == action["document"]["document_version_id"]


def test_ignore_gets_an_intent_but_no_value_envelope(grant_world):
    run = filling(grant_world)
    for i in range(4):
        do(grant_world, run, i)
    before = envelopes(grant_world)
    env = intent(grant_world, run, 4).envelope
    assert (env.envelope_id, env.rendered_value, env.document) == (None, None, None)
    assert envelopes(grant_world) == before


def test_out_of_order_is_refused_without_a_stop(grant_world):
    run = filling(grant_world)
    with pytest.raises(fr.FillRefused) as refused:
        intent(grant_world, run, 1)
    assert refused.value.reason == "out_of_order" and events(grant_world, run)[-1] == ("FILLING", None)


def _revoke(w, run):
    from webapp.services.review_approval import revoke
    revoke(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws, actor="u", now=NOW)


def _pause(w, run):
    pause(w.conn, account_id=V2_ACCOUNT, scope_type="APPLICATION", scope_id=w.ws, actor="u", reason="h", now=NOW)


def _kill(w, run):
    engage_kill_switch(w.conn, account_id=V2_ACCOUNT, actor="u", reason="stop", now=NOW)


def _ceiling(w, run):
    from product.autonomy_contract import Capability
    set_capability(w.conn, account_id=V2_ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=V2_ACCOUNT,
                   capability=Capability.PREPARE, actor="u", now=NOW)


def _policy(w, run):
    from webapp.persistence.autonomy_authority import current_policy
    from webapp.services.autonomy_controls import save_standing_policy
    doc = dict(current_policy(w.conn, V2_ACCOUNT)["doc"])
    doc["limits"] = {**doc["limits"], "fill_per_day": 9}
    save_standing_policy(w.conn, account_id=V2_ACCOUNT, doc=doc, actor="u", now=NOW)


def _stale(w, run, monkeypatch):
    monkeypatch.setattr(fa.f, "plan_confirmed", lambda *a, **k: False)


def _expired(w, run):
    w.conn.execute("UPDATE autonomy_grants SET expires_at = ? WHERE stage = 'FILL'", ("2000-01-01T00:00:00.000000Z",))
    w.conn.commit()


@pytest.mark.parametrize("breaker,reason,detail", [
    (_revoke, "APPROVAL_NOT_EFFECTIVE", {}),
    (_pause, "AUTHORITY_REDUCED", {"reduction": "PAUSED"}),
    (_kill, "AUTHORITY_REDUCED", {"reduction": "KILL_SWITCH"}),
    (_ceiling, "AUTHORITY_REDUCED", {"reduction": "CEILING"}),
    (_policy, "AUTHORITY_REDUCED", {"reduction": "POLICY"}),
    (_stale, "PLAN_CONFIRMATION_STALE", {}),
    (_expired, "GRANT_EXPIRED", {"status": "ISSUED"}),
])
def test_every_intent_refusal_stops_with_zero_envelopes(grant_world, monkeypatch, breaker, reason, detail):
    run = filling(grant_world)
    breaker(*((grant_world, run, monkeypatch) if breaker is _stale else (grant_world, run)))
    out = intent(grant_world, run, 0)
    assert (out.envelope, out.stop_reason, out.detail) == (None, reason, detail)
    assert envelopes(grant_world) == 0
    assert events(grant_world, run)[-1] == ("FILL_STOPPED", reason)
    grant_id = f.get_grant_binding(grant_world.conn, run["id"])["grant_id"]
    assert get_grant(grant_world.conn, grant_id)["status"] in ("REVOKED",) or breaker is _expired


@pytest.mark.parametrize("override,reason", [
    ({"ruleset_hash": "sha256:" + "9" * 64}, "QUARANTINE_RULESET_CHANGED"),
    ({"siblings_contained": False}, "SIBLING_EMPLOYER_CONTEXT_OPEN"),
    ({"structure_fingerprint": "sha256:" + "8" * 64}, "STRUCTURE_CHANGED"),
    ({"field_fingerprint": "sha256:" + "7" * 64}, "TARGET_CHANGED"),
])
def test_a_failing_precheck_stops_with_zero_envelopes(grant_world, override, reason):
    run = filling(grant_world)
    out = intent(grant_world, run, 0, **override)
    assert (out.envelope, out.stop_reason) == (None, reason) and envelopes(grant_world) == 0


def test_precheck_keys_are_closed(grant_world):
    run = filling(grant_world)
    with pytest.raises(ValueError):
        fa.request_intent(grant_world.conn, settings=grant_world.settings, run_id=run["id"], action_index=0,
                          precheck={**precheck(grant_world, run, 0), "extra": 1}, now=NOW)


# ---- envelope lifetime ----------------------------------------------------------------------

def test_envelope_ttl_boundary(grant_world):
    run = filling(grant_world)
    env = intent(grant_world, run, 0).envelope
    edge = NOW + VALUE_ENVELOPE_TTL
    assert outcome(grant_world, run, 0, env, now=edge)["state"] == "FILLING"  # used exactly at its TTL
    env = intent(grant_world, run, 1, now=edge).envelope
    late = outcome(grant_world, run, 1, env, now=edge + VALUE_ENVELOPE_TTL + MS)
    assert late == {"state": "FILL_STOPPED", "reason": "ENVELOPE_EXPIRED"}


def test_a_retried_intent_after_the_ttl_stops_instead_of_reissuing(grant_world):
    run = filling(grant_world)
    intent(grant_world, run, 0)
    out = intent(grant_world, run, 0, now=NOW + VALUE_ENVELOPE_TTL + MS)
    assert out.stop_reason == "ENVELOPE_EXPIRED" and envelopes(grant_world) == 1


# ---- outcomes ---------------------------------------------------------------------------------

def test_at_most_one_outcome_under_concurrent_reports(grant_world, tmp_path):
    run = filling(grant_world)
    env = intent(grant_world, run, 0).envelope
    db = tmp_path / "jobsearch.sqlite3"  # the v2_chain database (a PostgreSQL database under --db postgres)
    post = page_after(grant_world, 1)  # built here: the fixture connection is never shared with the threads
    barrier, results = threading.Barrier(2), []

    def report(value):
        conn = connect(db)
        barrier.wait()
        try:
            results.append(("ok", fa.record_outcome(conn, run_id=run["id"], action_index=0,
                                                   envelope_id=env.envelope_id, outcome=value,
                                                   readback_hash=fill_value_hash("ada@example.com"),
                                                   post_observation=post, now=NOW)))
        except fr.FillRefused as exc:
            results.append(("refused", exc.reason))
        except Exception as exc:  # recorded, so an unexpected error fails the assertion visibly
            results.append(("error", repr(exc)))
        finally:
            conn.close()

    threads = [threading.Thread(target=report, args=(v,)) for v in ("WRITTEN_VERIFIED", "NOOP_ALREADY_EQUAL")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r[0] for r in results) == ["ok", "refused"], results
    rows = grant_world.conn.execute("SELECT COUNT(*) FROM fill_action_events WHERE event = 'OUTCOME'").fetchone()[0]
    assert rows == 1


def test_a_repeated_identical_outcome_is_idempotent(grant_world):
    run = filling(grant_world)
    env = intent(grant_world, run, 0).envelope
    outcome(grant_world, run, 0, env)
    assert outcome(grant_world, run, 0, env)["duplicate"] is True


@pytest.mark.parametrize("value", ["READBACK_MISMATCH", "TARGET_CHANGED", "FIELD_VALIDITY_FAILED",
                                   "FIELD_VALUE_REVERTED"])
def test_each_reported_failure_stops_the_run(grant_world, value):
    run = filling(grant_world)
    env = intent(grant_world, run, 0).envelope
    out = outcome(grant_world, run, 0, env, value=value, doc=page_after(grant_world, 0))
    assert out == {"state": "FILL_STOPPED", "reason": value}
    if value == "FIELD_VALIDITY_FAILED":  # spec §13: the page rejected the value → TRANSFORM_FAILURE delta
        [delta] = ra.open_deltas(grant_world.conn, grant_world.ws)
        assert delta["kind"] == "TRANSFORM_FAILURE" and delta["observed"]["field_key"] == "gh:email"


def test_a_readback_that_is_not_the_plan_hash_is_a_mismatch(grant_world):
    run = filling(grant_world)
    env = intent(grant_world, run, 0).envelope
    out = outcome(grant_world, run, 0, env, readback=fill_value_hash("someone@else.test"))
    assert out["reason"] == "READBACK_MISMATCH"


def test_post_action_new_fields_open_all_deltas_together_and_stop_once(grant_world):
    run = filling(grant_world)
    env = intent(grant_world, run, 0).envelope
    hear = element("gh:hear", label="How did you hear about us?", question="How did you hear about us?",
                   name="hear", id="hear", required=True)
    decl = element("gh:certify", label="I certify the information is true",
                   question="I certify the information is true", name="certify", id="certify", required=True)
    out = outcome(grant_world, run, 0, env, doc=page_after(grant_world, 1, hear, decl))
    assert (out["state"], out["reason"]) == ("FILL_STOPPED", "DELTA_OPENED")
    assert sorted(d["kind"] for d in ra.open_deltas(grant_world.conn, grant_world.ws)) == ["DECLARATION",
                                                                                         "NEW_QUESTION"]
    assert [e for e in events(grant_world, run) if e[0] == "FILL_STOPPED"] == [("FILL_STOPPED", "DELTA_OPENED")]


def test_write_outcome_unknown_is_never_retried(grant_world):
    run = filling(grant_world)
    intent(grant_world, run, 0)
    out = fa.record_unknown_outcome(grant_world.conn, run_id=run["id"], action_index=0, now=NOW)
    assert out["reason"] == "WRITE_OUTCOME_UNKNOWN"
    with pytest.raises(fr.FillRefused):
        intent(grant_world, run, 0)


# ---- final validation, detections -------------------------------------------------------------

def run_all(w, run):
    for i in range(5):
        last = do(w, run, i)
    return last


def test_the_whole_plan_reaches_final_validating_then_filled(grant_world):
    run = filling(grant_world)
    assert run_all(grant_world, run) == {"state": "FINAL_VALIDATING"}
    out = fa.final_validate(grant_world.conn, run_id=run["id"], observation=page_after(grant_world, 5), now=NOW)
    assert out == {"state": "FILLED_AWAITING_SUBMISSION"}
    assert f.get_lease(grant_world.conn, run["id"]) is not None


def test_final_validation_catches_a_question_revealed_by_the_last_write(grant_world):
    run = filling(grant_world)
    run_all(grant_world, run)
    hear = element("gh:hear", label="How did you hear about us?", question="How did you hear about us?",
                   name="hear", id="hear", required=True)
    out = fa.final_validate(grant_world.conn, run_id=run["id"], observation=page_after(grant_world, 5, hear), now=NOW)
    assert (out["state"], out["reason"]) == ("FILL_STOPPED", "DELTA_OPENED")


def test_final_validation_catches_a_reverted_value(grant_world):
    run = filling(grant_world)
    run_all(grant_world, run)
    doc = page_after(grant_world, 5)
    doc["elements"][1]["value_state"] = {"state": "BLANK"}
    out = fa.final_validate(grant_world.conn, run_id=run["id"], observation=doc, now=NOW)
    assert out["reason"] == "FIELD_VALUE_REVERTED"


@pytest.mark.parametrize("kind,reason", [("SUBMIT_ATTEMPT_OBSERVED", "SUBMIT_ATTEMPT_OBSERVED"),
                                         ("NAVIGATION_ATTEMPT_OBSERVED", "NAVIGATION_ATTEMPT_OBSERVED"),
                                         ("EXECUTION_CONTEXT_CLOSED", "EXECUTION_CONTEXT_CLOSED")])
def test_a_detection_stops_an_active_run(grant_world, kind, reason):
    run = filling(grant_world)
    assert fa.record_detection(grant_world.conn, run_id=run["id"], kind=kind, detail={}, now=NOW)["reason"] == reason


def test_after_filled_a_detection_is_an_event_only(grant_world):
    run = filling(grant_world)
    run_all(grant_world, run)
    fa.final_validate(grant_world.conn, run_id=run["id"], observation=page_after(grant_world, 5), now=NOW)
    out = fa.record_detection(grant_world.conn, run_id=run["id"], kind="EXECUTION_CONTEXT_CLOSED", detail={}, now=NOW)
    assert out == {"state": "FILLED_AWAITING_SUBMISSION"}
    assert events(grant_world, run)[-1] == ("FILLED_AWAITING_SUBMISSION", None)


# ---- Task 13: pre-action re-observation and executor-reported stops ------------------------

def pre_action(w, run, doc, i=0):
    return fr.record_observation(w.conn, settings=w.settings, run_id=run["id"], phase="PRE_ACTION", action_index=i,
                                 observation=doc, now=NOW)


def test_the_user_typing_into_a_pending_target_is_a_prefilled_conflict(grant_world):
    run = filling(grant_world)
    doc = page_after(grant_world, 0)
    doc["elements"][0]["value_state"] = {"state": "NONBLANK", "current_value_hash": fill_value_hash("me@typed.test")}
    assert pre_action(grant_world, run, doc)["reason"] == "PREFILLED_VALUE_CONFLICT"
    assert events(grant_world, run)[-1] == ("FILL_STOPPED", "PREFILLED_VALUE_CONFLICT")


def test_the_user_editing_a_completed_field_is_field_value_reverted(grant_world):
    run = filling(grant_world)
    do(grant_world, run, 0)
    doc = page_after(grant_world, 1)
    doc["elements"][0]["value_state"] = {"state": "NONBLANK", "current_value_hash": fill_value_hash("edited@x.test")}
    assert pre_action(grant_world, run, doc, 1)["reason"] == "FIELD_VALUE_REVERTED"


def test_a_clean_pre_action_observation_continues(grant_world):
    run = filling(grant_world)
    do(grant_world, run, 0)
    assert pre_action(grant_world, run, page_after(grant_world, 1), 1)["state"] == "FILLING"


@pytest.mark.parametrize("reason", ["PERMISSIONS_MISSING", "SIBLING_EMPLOYER_CONTEXT_OPEN", "STRUCTURE_UNSTABLE",
                                    "QUARANTINE_RULESET_CHANGED", "EXECUTOR_LOST", "PREFILLED_VALUE_CONFLICT",
                                    "FIELD_VALUE_REVERTED", "OMIT_FIELD_NOT_BLANK"])
def test_the_executor_can_only_stop_with_observable_reasons(grant_world, reason):
    run = filling(grant_world)
    out = fr.executor_stop(grant_world.conn, run_id=run["id"], reason=reason, detail={"local": True}, now=NOW)
    assert out == {"state": "FILL_STOPPED", "reason": reason}


@pytest.mark.parametrize("reason", ["APPROVAL_NOT_EFFECTIVE", "GRANT_REFUSED", "DELTA_OPENED", "NOT_A_REASON"])
def test_server_decided_reasons_are_not_executor_reportable(grant_world, reason):
    run = filling(grant_world)
    with pytest.raises(ValueError):
        fr.executor_stop(grant_world.conn, run_id=run["id"], reason=reason, detail={}, now=NOW)
    assert events(grant_world, run)[-1] == ("FILLING", None)
