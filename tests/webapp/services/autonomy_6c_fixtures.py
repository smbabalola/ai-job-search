"""Shared world-building for Bundle 6C tests. Later tasks APPEND helpers here;
never rewrite existing ones."""
from __future__ import annotations

import pytest

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


def build_chain(tmp_path, *, ai_units=None, decide=False):
    """A real application workspace driven through understand -> fit ->
    intelligence with the acceptance-suite fakes, returning (client, app,
    settings, workspace_id). decide=True also records USER review decisions
    for every item (the full-journey helper)."""
    from tests.webapp.fixtures.acceptance.fixtures import completion_ready_content_units
    from tests.webapp.test_full_journey_acceptance import _build_chain, _decide_current_review_surface
    client, app, settings, ws = _build_chain(tmp_path, ai_units=ai_units or completion_ready_content_units())
    if decide:
        _decide_current_review_surface(client, ws)
    return client, app, settings, ws


@pytest.fixture
def prepared_chain(tmp_path):
    from webapp.persistence.db import connect
    from tests.webapp.test_full_journey_acceptance import _close
    client, _, settings, ws = build_chain(tmp_path)
    conn = connect(settings.db_path)
    yield conn, ws, settings
    conn.close()
    _close(client)


@pytest.fixture
def ready_chain(tmp_path):
    from webapp.persistence.db import connect
    from tests.webapp.test_full_journey_acceptance import _close
    client, _, settings, ws = build_chain(tmp_path, decide=True)
    conn = connect(settings.db_path)
    yield conn, ws, settings
    conn.close()
    _close(client)
