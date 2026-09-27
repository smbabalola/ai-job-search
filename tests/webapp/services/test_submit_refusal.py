"""Bundle 6D-A Task 11 / spec §12 G2: the public SUBMIT entry points refuse
unconditionally and write nothing; the preserved engine is never invoked.
FILL is unaffected."""
from __future__ import annotations

import pytest

from product.autonomy_contract import Capability
from webapp.services import autonomy
from webapp.services.autonomy import SubmissionNotAvailable, pre_click_commit, request_grant
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn  # noqa: F401
from tests.webapp.services.test_autonomy_context import seeded, settings  # noqa: F401
from tests.webapp.services.test_autonomy_decide import OBS, authorize_all, manifest
from tests.webapp.services.test_autonomy_preclick import ensure_run, submit_grant, verification

TABLES = ("autonomy_decisions", "autonomy_grants", "limit_reservations", "submission_intents", "submission_attempts",
          "autonomy_grant_events")


def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def _core_must_not_run(*a, **k):
    raise AssertionError("the private SUBMIT core was invoked")


def test_public_submit_grant_is_refused_without_writes_or_core(conn, settings, seeded, monkeypatch):
    authorize_all(conn)
    run_id = ensure_run(conn)
    before = _counts(conn)
    monkeypatch.setattr(autonomy, "_request_grant_core", _core_must_not_run)
    with pytest.raises(SubmissionNotAvailable) as caught:
        request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                      stage=Capability.SUBMIT, now=NOW, fill_manifest=manifest(seeded), observation=OBS,
                      run_id=run_id)
    assert str(caught.value) == "submission_not_available"
    assert _counts(conn) == before


def test_public_pre_click_is_refused_without_writes_or_core(conn, settings, seeded, monkeypatch):
    grant_id = submit_grant(conn, settings, seeded)  # a real ISSUED SUBMIT grant, created through the core
    before = _counts(conn)
    monkeypatch.setattr(autonomy, "_pre_click_commit_core", _core_must_not_run)
    with pytest.raises(SubmissionNotAvailable) as caught:
        pre_click_commit(conn, settings=settings, grant_id=grant_id, verification=verification(seeded), now=NOW,
                         observation=OBS, run_id="run_1")
    assert str(caught.value) == "submission_not_available"
    assert _counts(conn) == before


def test_public_fill_grant_still_reaches_the_engine(conn, settings, seeded):
    authorize_all(conn)
    out = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                        stage=Capability.FILL, now=NOW, fill_manifest=manifest(seeded), observation=OBS)
    assert out.grant is not None and out.grant["stage"] == "FILL"
