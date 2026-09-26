"""Shared world-building for Bundle 6C tests. Later tasks APPEND helpers here;
never rewrite existing ones."""
from __future__ import annotations

from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, db_path, make_workspace  # noqa: F401


def make_screening_row(conn, *, candidate_id, outcome="PROMOTE", could_unlock=False, fingerprint="fp",
                       search_workspace_id="search_default", now=NOW):
    from webapp.persistence.autonomy_prepare import insert_screening
    return insert_screening(
        conn, account_id=ACCOUNT, search_workspace_id=search_workspace_id, candidate_id=candidate_id,
        discovery_run_id=None, discovery_fit_id=None, outcome=outcome, reason_code=outcome.lower(),
        reasons=[], require_user=[], could_unlock=could_unlock, retry_at=None, input_fingerprint=fingerprint,
        authority={"deployment": "PREPARE", "account": "PREPARE", "workspace": "PREPARE"},
        policy_version_hash=None, subject_policy_hash=None, engine_version="candidate-promotion.v1", now=now)
