"""Two workers, one authorization: exactly one may proceed (spec §17)."""
from __future__ import annotations

import threading

from product.autonomy_contract import Capability
from webapp.persistence.db import connect
from webapp.services.autonomy import pre_click_commit, request_grant
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn  # noqa: F401
from tests.webapp.services.test_autonomy_context import patch_external_reads, seed_workspace, settings  # noqa: F401
from tests.webapp.services.test_autonomy_decide import OBS, authorize_all, manifest
from tests.webapp.services.test_autonomy_preclick import RUN, T, ensure_run, verification


def race(settings, calls):
    barrier = threading.Barrier(len(calls))
    results = [None] * len(calls)
    errors = []

    def run(i, grant_id, ws):
        c = connect(settings.db_path)
        try:
            barrier.wait()
            results[i] = pre_click_commit(c, settings=settings, grant_id=grant_id,
                                          verification=verification(ws), now=T, observation=OBS, run_id=RUN)
        except Exception as exc:  # surfaced below; a lock error must fail the test, not vanish
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=run, args=(i, g, ws)) for i, (g, ws) in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return results


def _grant(conn, settings, ws):
    return request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=ws,
                         stage=Capability.SUBMIT, now=NOW, fill_manifest=manifest(ws), observation=OBS,
                         run_id=ensure_run(conn)).grant["id"]


def _authority_rows(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("submission_intents", "submission_attempts")}


def test_two_workers_one_grant(conn, settings, monkeypatch):
    patch_external_reads(monkeypatch)
    ws = seed_workspace(conn)
    authorize_all(conn)
    gid = _grant(conn, settings, ws)
    results = race(settings, [(gid, ws), (gid, ws)])
    assert sorted(r.authorized for r in results) == [False, True]
    assert _authority_rows(conn) == {"submission_intents": 1, "submission_attempts": 1}


def test_two_workers_last_daily_slot(conn, settings, monkeypatch):
    patch_external_reads(monkeypatch)
    ws1 = seed_workspace(conn, record_id="1", company="Acme")
    ws2 = seed_workspace(conn, record_id="2", company="Globex")
    authorize_all(conn)
    g1, g2 = _grant(conn, settings, ws1), _grant(conn, settings, ws2)
    results = race(settings, [(g1, ws1), (g2, ws2)])  # canary cap = 1/day
    assert sorted(r.authorized for r in results) == [False, True]
    loser = next(r for r in results if not r.authorized)
    assert loser.reason == "limit"
    assert _authority_rows(conn) == {"submission_intents": 1, "submission_attempts": 1}


def test_kill_switch_racing_pre_click_is_never_lost(conn, settings, monkeypatch):
    """Either the kill switch committed first (no click) or the attempt
    committed first and the later engagement is recorded after it."""
    from webapp.services.autonomy_controls import engage_kill_switch
    patch_external_reads(monkeypatch)
    ws = seed_workspace(conn)
    authorize_all(conn)
    gid = _grant(conn, settings, ws)
    barrier = threading.Barrier(2)
    out, errors = {}, []

    def click():
        c = connect(settings.db_path)
        try:
            barrier.wait()
            out["click"] = pre_click_commit(c, settings=settings, grant_id=gid, verification=verification(ws),
                                            now=T, observation=OBS, run_id=RUN)
        except Exception as exc:
            errors.append(exc)
        finally:
            c.close()

    def kill():
        c = connect(settings.db_path)
        try:
            barrier.wait()
            engage_kill_switch(c, account_id=ACCOUNT, actor="u", reason="race", now=T)
        except Exception as exc:
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=click), threading.Thread(target=kill)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    result = out["click"]
    kill_seq = conn.execute("SELECT MAX(seq) FROM autonomy_kill_switch WHERE engaged = 1").fetchone()[0]
    assert kill_seq is not None
    if result.authorized:
        attempt_seq = conn.execute("SELECT seq FROM submission_attempts WHERE id = ?",
                                   (result.attempt_id,)).fetchone()[0]
        assert attempt_seq is not None  # the attempt exists and keeps its lifecycle
    else:
        assert result.reason in ("kill_switch", "grant_not_consumable")
        assert _authority_rows(conn) == {"submission_intents": 0, "submission_attempts": 0}
