"""Application dossier (6B spec §13): what did autonomy do for this
application, why, what exactly did it send, where, and what happened.
Read-only and derived; never edited. Attempt evidence is client-supplied
(spec §8: the executor is non-authoritative) and is shown as recorded."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from webapp.persistence.application_blockers import list_application_blockers, list_blocker_resolution_history
from webapp.persistence.autonomy_ledger import list_attempt_events, list_decisions
from webapp.persistence.workspaces import get_workspace

DOSSIER_SCHEMA_VERSION = "autonomy-dossier.v1"
_RAW_DECISION_COLUMNS = ("reasons_json", "require_user_json", "completion_blockers_json", "inputs_json")


def _rows(conn, sql: str, params: tuple) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def build_dossier(conn: sqlite3.Connection, *, account_id: str, application_workspace_id: str) -> dict[str, Any]:
    ws = application_workspace_id
    workspace = get_workspace(conn, ws, account_id=account_id)
    if workspace is None:
        raise LookupError(ws)
    decisions = [
        {k: v for k, v in d.items() if k not in _RAW_DECISION_COLUMNS} | {"inputs": json.loads(d["inputs_json"])}
        for d in list_decisions(conn, ws)
    ]
    grants = []
    for g in _rows(conn, "SELECT * FROM autonomy_grants WHERE application_workspace_id = ? ORDER BY seq", (ws,)):
        g["binding"] = json.loads(g.pop("binding_json"))
        g["events"] = _rows(conn, "SELECT status AS state, reason, created_at FROM autonomy_grant_events "
                                  "WHERE grant_id = ? ORDER BY seq", (g["id"],))
        grants.append(g)
    attempts = []
    for a in _rows(conn, "SELECT * FROM submission_attempts WHERE application_workspace_id = ? ORDER BY seq", (ws,)):
        a["events"] = [
            {"state": e["state"], "source": e["source"], "created_at": e["created_at"],
             "evidence": json.loads(e["evidence_json"])}
            for e in list_attempt_events(conn, a["id"])
        ]
        attempts.append(a)
    intents = []
    for i in _rows(conn, "SELECT * FROM submission_intents WHERE application_workspace_id = ? ORDER BY seq", (ws,)):
        i["overrides"] = _rows(conn, "SELECT actor, reason, created_at FROM intent_overrides WHERE intent_id = ? "
                                     "ORDER BY seq", (i["id"],))
        intents.append(i)
    exceptions = [
        {**b, "resolutions": list_blocker_resolution_history(conn, b["id"])}
        for b in list_application_blockers(conn, ws)
    ]
    return {
        "schema_version": DOSSIER_SCHEMA_VERSION,
        "application_workspace_id": ws,
        "company": workspace.get("company"),
        "title": workspace.get("title"),
        "decisions": decisions,
        "grants": grants,
        "attempts": attempts,
        "intents": intents,
        "rule_acknowledgements": _rows(conn, "SELECT * FROM rule_acknowledgements WHERE application_workspace_id = ? "
                                             "ORDER BY seq", (ws,)),
        "apply_target_confirmations": _rows(conn, "SELECT * FROM apply_target_confirmations "
                                                  "WHERE application_workspace_id = ? ORDER BY seq", (ws,)),
        "dry_run_cases": _rows(conn, "SELECT * FROM dry_run_submission_cases WHERE application_workspace_id = ? "
                                     "ORDER BY seq", (ws,)),
        "exceptions": exceptions,
        "kill_switch_events": _rows(conn, "SELECT engaged, reason, actor, created_at FROM autonomy_kill_switch "
                                          "WHERE account_id = ? ORDER BY seq", (account_id,)),
    }
