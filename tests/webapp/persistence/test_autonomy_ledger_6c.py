from __future__ import annotations

from decimal import Decimal

import pytest

from webapp.persistence.autonomy_ledger import (
    budget_usage, get_reservation, reserve_within_cap, settle_reservation, settlement_ref_for, try_reserve,
)
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, NOW, conn  # noqa: F401

DAY = dict(account_id=ACCOUNT, counter_name="budget:LLM:day", window_key="2026-09-24")


def test_capped_admission_counts_unsettled_reservations_at_full_amount(conn):
    r1 = reserve_within_cap(conn, cap=Decimal("1.00"), amount=Decimal("0.60"), subject_type="CANDIDATE",
                            subject_id="c1", now=NOW, **DAY)
    assert r1 and get_reservation(conn, r1)["subject_type"] == "CANDIDATE"
    assert reserve_within_cap(conn, cap=Decimal("1.00"), amount=Decimal("0.60"), subject_type="CANDIDATE",
                              subject_id="c2", now=NOW, **DAY) is None
    assert budget_usage(conn, **DAY) == Decimal("0.60")


def test_settlement_is_exactly_once_immutable_and_charges_actual(conn):
    r1 = reserve_within_cap(conn, cap=Decimal("1.00"), amount=Decimal("0.60"), subject_type="CANDIDATE",
                            subject_id="c1", now=NOW, **DAY)
    assert settle_reservation(conn, reservation_id=r1, attempt_id="att_1", amount=Decimal("0.10"), now=NOW) is True
    assert budget_usage(conn, **DAY) == Decimal("0.10")  # usage reflects actual after settlement
    assert settle_reservation(conn, reservation_id=r1, attempt_id="att_1", amount=Decimal("0.05"), now=NOW) is False
    row = get_reservation(conn, r1)
    assert (row["settled_amount"], row["status"]) == ("0.10", "CONSUMED")
    assert row["settlement_ref"] == settlement_ref_for("att_1", r1)


def test_two_reservations_of_one_attempt_get_distinct_stable_refs(conn):
    a = reserve_within_cap(conn, cap=Decimal("5"), amount=Decimal("1"), subject_type="APPLICATION",
                           subject_id="ws1", now=NOW, **DAY)
    b = reserve_within_cap(conn, account_id=ACCOUNT, counter_name="budget:LLM:application", window_key="ws1",
                           cap=Decimal("5"), amount=Decimal("1"), subject_type="APPLICATION", subject_id="ws1", now=NOW)
    assert settlement_ref_for("att_9", a) == settlement_ref_for("att_9", a) != settlement_ref_for("att_9", b)
    assert settle_reservation(conn, reservation_id=a, attempt_id="att_9", amount=Decimal("1"), now=NOW)
    assert settle_reservation(conn, reservation_id=b, attempt_id="att_9", amount=Decimal("1"), now=NOW)


def test_overage_is_recorded_truthfully(conn):
    r = reserve_within_cap(conn, cap=Decimal("1"), amount=Decimal("0.20"), subject_type="CANDIDATE", subject_id="c",
                           now=NOW, **DAY)
    assert settle_reservation(conn, reservation_id=r, attempt_id="att", amount=Decimal("0.35"), now=NOW)
    assert budget_usage(conn, **DAY) == Decimal("0.35")  # never clamped to the reservation


def test_existing_try_reserve_is_unchanged_and_accepts_subjects(conn):
    kw = dict(account_id=ACCOUNT, counter_name="promotions:day", window_key="2026-09-24", limit=1, now=NOW)
    first = try_reserve(conn, subject_type="CANDIDATE", subject_id="c1", **kw)
    assert first and try_reserve(conn, subject_type="CANDIDATE", subject_id="c2", **kw) is None
    with pytest.raises(Exception):
        try_reserve(conn, subject_type="CANDIDATE", **{**kw, "window_key": "other"})  # pair enforced by trigger
