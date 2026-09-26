from __future__ import annotations

import json

import pytest

from webapp.persistence.autonomy_ledger import add_intent_override, live_intent
from webapp.services.autonomy import record_click_dispatched, record_submission_result
from webapp.services.autonomy_dossier import build_dossier
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401
from tests.webapp.services.test_autonomy_context import seeded, settings  # noqa: F401
from tests.webapp.services.test_autonomy_preclick import IDENT, RUN, T, click, submit_grant


def test_dossier_reconstructs_a_scripted_submission(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = click(conn, settings, seeded, gid).attempt_id
    record_click_dispatched(conn, attempt_id=attempt, now=T)
    record_submission_result(conn, attempt_id=attempt, state="CONFIRMED_SUCCESS", source="EXECUTOR",
                             evidence={"confirmation_text": "Thanks for applying"}, now=T)
    add_intent_override(conn, intent_id=live_intent(conn, account_id=ACCOUNT, job_identity_key=IDENT)["id"],
                        actor="u", reason="re-apply", now=T)
    dossier = build_dossier(conn, account_id=ACCOUNT, application_workspace_id=seeded)
    json.dumps(dossier)  # exportable
    assert dossier["schema_version"] == "autonomy-dossier.v1"
    assert [d["requested_stage"] for d in dossier["decisions"]] == ["SUBMIT", "SUBMIT"]
    assert all("reasons_json" not in d and "completion_blockers_json" not in d and "inputs" in d
               for d in dossier["decisions"])
    (grant,) = dossier["grants"]
    assert grant["status"] == "CONSUMED" and grant["binding"]["fill_manifest"]["pages"][0]["entries"][0]["page_field_key"] == "email"
    assert grant["binding"]["run_id"] == RUN
    assert [e["state"] for e in grant["events"]] == ["ISSUED", "CONSUMED"]
    (att,) = dossier["attempts"]
    assert [e["state"] for e in att["events"]] == ["AUTHORIZED", "CLICK_DISPATCHED", "CONFIRMED_SUCCESS"]
    assert att["events"][-1]["evidence"] == {"confirmation_text": "Thanks for applying"}
    (intent,) = dossier["intents"]
    assert intent["state"] == "CONFIRMED" and [o["reason"] for o in intent["overrides"]] == ["re-apply"]


def test_dossier_is_account_scoped(conn):
    ws = make_workspace(conn)
    with pytest.raises(LookupError):
        build_dossier(conn, account_id="someone_else", application_workspace_id=ws)
