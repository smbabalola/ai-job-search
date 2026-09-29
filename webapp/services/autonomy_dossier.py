"""Application dossier (6B spec §13): what did autonomy do for this
application, why, what exactly did it send, where, and what happened.
Read-only and derived; never edited. Attempt evidence is client-supplied
(spec §8: the executor is non-authoritative) and is shown as recorded."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from webapp.persistence.application_blockers import list_application_blockers, list_blocker_resolution_history
from webapp.persistence.autonomy_ledger import list_attempt_events, list_decisions
from webapp.persistence.workspaces import get_workspace
from webapp.persistence import dbapi

DOSSIER_SCHEMA_VERSION = "autonomy-dossier.v1"
_RAW_DECISION_COLUMNS = ("reasons_json", "require_user_json", "completion_blockers_json", "inputs_json")


def _rows(conn, sql: str, params: tuple) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _build_6b_dossier(conn: dbapi.Connection, *, account_id: str, application_workspace_id: str) -> dict[str, Any]:
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


# ---- Bundle 6C additions (spec §12) -------------------------------------------

def _content_hash(payload: Any) -> str:
    """Canonical hash of a pack payload; floats are normalized to Decimal
    first because canonical hashing rejects floats."""
    from decimal import Decimal

    from product.autonomy_contract import canonical_hash

    def safe(value: Any) -> Any:
        if isinstance(value, float):
            return Decimal(str(value))
        if isinstance(value, dict):
            return {k: safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [safe(v) for v in value]
        return value
    return canonical_hash("autonomy-dossier-pack", "v1", safe(payload))


def document_hashes(pack_payload: dict[str, Any], *, artifact_id: str) -> dict[str, str]:
    """SHA-256 of the final application documents: the user-selected files on
    the v2 path; on the legacy path, the rendered application-pack document
    (deterministic for the pack artifact)."""
    if pack_payload.get("schema_version") == "application-pack.v2":
        return {kind: "sha256:" + doc["sha256"]
                for kind, doc in sorted((pack_payload.get("final_documents") or {}).items())}
    from webapp.services.archive_projection import _render_markdown
    rendered = _render_markdown(pack_payload, projection_id=artifact_id).encode("utf-8")
    return {"application_pack_projection": "sha256:" + hashlib.sha256(rendered).hexdigest()}


def _pack_section(conn, ws: str) -> dict[str, Any] | None:
    from webapp.persistence.artifacts import get_current_artifact
    from webapp.services.autonomy_prepare import _revision_of_pack, pack_sources, system_confirmed_revision
    pack = get_current_artifact(conn, ws, "application_pack")
    if pack is None:
        return None
    sources = pack_sources(pack["payload"])
    revision = _revision_of_pack(pack["payload"])
    return {
        "artifact_id": pack["id"], "content_hash": _content_hash(pack["payload"]), "pack_revision": revision,
        # Content-addressed identities of exactly what was confirmed: the pack
        # and every source document it binds.
        "source_content_ids": {name: ref.get("content_id") for name, ref in sources.items()},
        "document_path": "v2" if pack["payload"].get("schema_version") == "application-pack.v2" else "v1",
        "document_hashes": document_hashes(pack["payload"], artifact_id=pack["id"]),
        "system_confirmed": revision is not None and system_confirmed_revision(conn, ws) == revision,
    }


def build_dossier(conn: dbapi.Connection, *, account_id: str, application_workspace_id: str,
                  settings: Any = None) -> dict[str, Any]:
    ws = application_workspace_id
    dossier = _build_6b_dossier(conn, account_id=account_id, application_workspace_id=ws)
    from webapp.persistence import autonomy_prepare as ap
    dossier["pack"] = _pack_section(conn, ws)
    dossier["system_review"] = [
        {**dict(r), "system_basis": json.loads(r["system_basis_json"])}
        for r in conn.execute("SELECT * FROM review_decisions WHERE workspace_id = ? AND decision_provenance = "
                              "'SYSTEM_AUTO_CONFIRMED' ORDER BY rowid", (ws,)).fetchall()
    ]
    for item in dossier["system_review"]:
        item.pop("system_basis_json", None)
    dossier["latches"] = _rows(conn, "SELECT * FROM autonomy_review_latches WHERE application_workspace_id = ? "
                                     "ORDER BY seq", (ws,))
    dossier["enrolments"] = _rows(conn, "SELECT * FROM autonomy_enrolments WHERE application_workspace_id = ? "
                                        "ORDER BY seq", (ws,))
    promotion = conn.execute("SELECT * FROM autonomy_candidate_promotions WHERE application_workspace_id = ? "
                             "ORDER BY seq DESC LIMIT 1", (ws,)).fetchone()
    dossier["origin"] = None
    if promotion is not None:
        dossier["origin"] = {"promotion": dict(promotion),
                             "screening": ap.get_screening(conn, promotion["screening_id"])
                             if promotion["screening_id"] else None}
    dossier["attempt_history_observational"] = ap.attempt_rows(conn, "APPLICATION", ws)
    state = {"enrolled": ap.is_enrolled(conn, ws), "next": None, "reason": None}
    if settings is not None:
        from product.prepare_steps import next_prepare_step
        from webapp.services.autonomy_prepare import prepare_snapshot
        snapshot, _ = prepare_snapshot(conn, settings=settings, account_id=account_id, application_workspace_id=ws)
        step = next_prepare_step(snapshot)
        state.update(next=step.step.value if step.step else step.kind, reason=step.reason or None)
    dossier["current_state_derived"] = state
    dossier["approvals"] = _approvals_section(conn, ws)
    dossier["fill"] = _fill_section(conn, settings=settings, account_id=account_id, ws=ws)
    dossier["submission"] = _submission_section(conn, settings=settings, account_id=account_id, ws=ws)
    return dossier


def _submission_section(conn, *, settings, account_id: str, ws: str) -> dict[str, Any]:
    """6E-A (spec §16.2): the submission status and every attempt with its
    submission-result.v1 summary (hashes and states only, no cleartext)."""
    from datetime import datetime, timezone
    from webapp.persistence import submit as sp
    from webapp.services.human_submit import submission_status
    attempts = []
    for attempt in reversed(sp.attempts_for_application(conn, ws)):
        auth = sp.authorization_for_grant(conn, attempt["grant_id"])
        if auth is None or auth["account_id"] != account_id:
            continue
        result = sp.get_submission_result(conn, attempt["id"])
        attempts.append({"id": attempt["id"], "created_at": attempt["created_at"], "state": attempt["state"],
                         "review_hash": auth["review_hash"],
                         "reason": result["result"]["reason"] if result else None,
                         "result_hash": result["result_hash"] if result else None})
    status = submission_status(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                               now=datetime.now(timezone.utc)) if settings is not None else None
    return {"status": status, "attempts": attempts}


def _fill_section(conn, *, settings, account_id: str, ws: str) -> dict[str, Any]:
    """6D-B: fill status and the run history (identity, state, stop reason,
    result hash; evidence only, no cleartext)."""
    from datetime import datetime, timezone
    from webapp.persistence import fill as f
    from webapp.services.fill_results import fill_summary
    runs = []
    for run in reversed([r for r in f.runs_for_application(conn, ws) if r["account_id"] == account_id]):
        last = f.run_state(conn, run["id"])
        result = f.get_result(conn, run["id"])
        runs.append({"id": run["id"], "created_at": run["created_at"], "state": last["event"] if last else None,
                     "reason": last["reason"] if last else None,
                     "result_hash": result["result_hash"] if result else None,
                     "execution_tab_id": run["execution_tab_id"], "timing_version": run["timing_version"]})
    status = fill_summary(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                          now=datetime.now(timezone.utc)) if settings is not None else None
    return {"status": status, "runs": runs}


def _approvals_section(conn, ws: str) -> dict[str, Any]:
    """6D-A: approvals with the components that changed between successive
    bindings, review deltas and the review event history (all by seq)."""
    from product.review_contract import invalidation_reasons
    from webapp.persistence import review_approval as ra
    rows = conn.execute("SELECT id FROM application_approvals WHERE application_workspace_id = ? ORDER BY seq",
                        (ws,)).fetchall()
    approvals, previous = [], None
    for row in rows:
        approval = ra.get_approval(conn, row["id"])
        approvals.append({
            "id": approval["id"], "binding_hash": approval["binding_hash"], "batch_id": approval["batch_id"],
            "actor": approval["actor"], "created_at": approval["created_at"],
            "supersedes_id": approval["supersedes_id"], "resolved_delta_ids": approval["resolved_delta_ids"],
            "changed_since_previous": invalidation_reasons(previous["binding"], approval["binding"])
            if previous else [],
        })
        previous = approval
    return {"approvals": approvals, "deltas": ra.list_deltas(conn, ws), "events": ra.events(conn, ws)}
