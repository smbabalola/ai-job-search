from __future__ import annotations

from decimal import Decimal

from webapp.config import Settings


def test_defaults_are_off_and_fail_closed(monkeypatch, tmp_path):
    for var in ("JOBSEARCH_AUTONOMY_SCHEDULER", "JOBSEARCH_AUTONOMY_STEP_COST_MAX"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(db_path=tmp_path / "db.sqlite3")
    assert s.autonomy_scheduler_enabled is False
    assert s.autonomy_step_cost_max == {}
    assert s.autonomy_step_envelope("FIT") is None
    assert (s.autonomy_tick_interval, s.autonomy_max_items_per_tick) == (30.0, 4)
    assert (s.autonomy_step_timeout, s.autonomy_lease_margin) == (600.0, 120.0)
    assert s.autonomy_max_promotions_per_day == 5
    assert s.autonomy_retry_delays == (60.0, 300.0, 900.0)


def test_envelopes_parse_and_invalid_input_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBSEARCH_AUTONOMY_SCHEDULER", "1")
    monkeypatch.setenv("JOBSEARCH_AUTONOMY_STEP_COST_MAX", '{"EVALUATE": "0.05", "FIT": "0.10"}')
    s = Settings(db_path=tmp_path / "db.sqlite3")
    assert s.autonomy_scheduler_enabled is True
    assert s.autonomy_step_envelope("FIT") == Decimal("0.10")
    assert s.autonomy_step_envelope("UNDERSTAND") is None
    for bad in ("not json", '{"FIT": "-1"}', '{"FIT": "NaN"}', '{"FIT": 0.1}', '["FIT"]', '{"BOGUS": "1"}'):
        monkeypatch.setenv("JOBSEARCH_AUTONOMY_STEP_COST_MAX", bad)
        assert Settings(db_path=tmp_path / "db.sqlite3").autonomy_step_cost_max == {}, bad
