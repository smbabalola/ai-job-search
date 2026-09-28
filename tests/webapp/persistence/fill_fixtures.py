"""Test helpers for 6D-B persistence. `insert_one_row` writes one minimal,
otherwise-valid row into a fill table so CHECK constraints and append-only
triggers can be exercised in isolation. Foreign keys are switched off for
the insert (this helper tests vocabularies and triggers, not referential
integrity, which the services' tests cover with real parent rows)."""
from __future__ import annotations

import itertools
import uuid

_counter = itertools.count()


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _defaults(table: str) -> dict:
    now = "2026-09-28T12:00:00.000000+00:00"
    base = {"id": _uid("row"), "created_at": now}
    rows = {
        "fill_runs": {"account_id": "account_local", "application_workspace_id": "ws_x", "handoff_session_id": "hs",
                      "executor_instance_id": "exe", "browser_session_id": "bs", "execution_tab_id": 7,
                      "timing_version": "fill-timing.v1"},
        "fill_run_events": {"fill_run_id": "run_x", "event": "OBSERVING", "reason": None, "detail_json": "{}"},
        "fill_observations": {"account_id": "account_local", "application_workspace_id": "ws_x", "fill_run_id": None,
                              "phase": "INITIAL", "action_index": None, "structure_fingerprint": "sha256:s",
                              "observation_fingerprint": "sha256:o", "observation_json": "{}"},
        "fill_plans": {"account_id": "account_local", "application_workspace_id": "ws_x",
                       "plan_hash": _uid("sha256:plan"), "approval_id": "apr_x", "approval_binding_hash": "sha256:b",
                       "observation_id": "obs_x", "plan_json": "{}"},
        "fill_plan_mapping_choices": {"account_id": "account_local", "application_workspace_id": "ws_x",
                                      "observation_id": "obs_x", "page_field_key": "k", "field_fingerprint": "sha256:f",
                                      "answer_key": "subject:x", "choice": "MAP", "actor": "u"},
        "fill_plan_confirmations": {"account_id": "account_local", "application_workspace_id": "ws_x",
                                    "plan_hash": "sha256:p", "approval_id": "apr_x",
                                    "approval_binding_hash": "sha256:b", "actor": "u"},
        "delta_classification_proposals": {"account_id": "account_local", "application_workspace_id": "ws_x",
                                           "delta_id": "dlt_x", "subject": "employment.notice_period",
                                           "basis": "HEURISTIC"},
        "delta_classification_confirmations": {"account_id": "account_local", "application_workspace_id": "ws_x",
                                               "delta_id": _uid("dlt"), "proposal_id": "prop_x",
                                               "subject": "employment.notice_period", "successor_delta_id": "dlt_y",
                                               "actor": "u"},
        "fill_run_grant_bindings": {"fill_run_id": _uid("run"), "grant_id": "grant_x", "approval_id": "apr_x",
                                    "approval_binding_hash": "sha256:b", "plan_hash": "sha256:p",
                                    "structure_fingerprint": "sha256:s", "observation_fingerprint": "sha256:o",
                                    "ruleset_hash": "sha256:r"},
        "fill_action_events": {"fill_run_id": "run_x", "action_index": next(_counter) + 1000, "event": "PRECHECK",
                               "outcome": None, "envelope_id": None, "readback_hash": None, "detail_json": "{}"},
        "fill_quarantine_events": {"fill_run_id": "run_x", "phase": "PRELOAD_INSTALLED", "ruleset_hash": "sha256:r",
                                   "action_index": None},
        "fill_detection_events": {"fill_run_id": "run_x", "kind": "SUBMIT_ATTEMPT_OBSERVED", "detail_json": "{}"},
        "fill_results": {"fill_run_id": _uid("run"), "result_hash": "sha256:r", "result_json": "{}"},
    }
    return {**base, **rows[table]}


def insert_one_row(conn, table: str, **overrides) -> dict:
    row = {**_defaults(table), **overrides}
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        cols = ", ".join(row)
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
        conn.commit()
    finally:
        conn.rollback()
        conn.execute("PRAGMA foreign_keys = ON")
    return row
