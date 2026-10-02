"""Bundle 7 spec §12.3: the pure, snapshot-derived subscription state machine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from product.subscription_state import STATES, TERMINAL, transition
from webapp.billing.port import SubscriptionSnapshot

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _snap(status: str) -> SubscriptionSnapshot:
    return SubscriptionSnapshot(provider_subscription_id="sub_1", provider_customer_id="cus_1", status=status,
                                plan_id="pro", interval="month", current_period_start=NOW - timedelta(days=3),
                                current_period_end=NOW + timedelta(days=27), cancel_at_period_end=False, raw_hash="h")


@pytest.mark.parametrize("current", (None,) + STATES)
@pytest.mark.parametrize("status", STATES)
def test_every_current_state_and_snapshot_status_pair(current, status):
    new_state, updates = transition(current, _snap(status), now=NOW)
    if current in TERMINAL:
        assert (new_state, updates) == (current, {}), "an ended subscription never comes back"
        return
    assert new_state == status, "the provider snapshot is the source of truth"
    if status == "PAST_DUE" and current != "PAST_DUE":
        assert updates == {"past_due_since": NOW}
    elif status != "PAST_DUE" and current == "PAST_DUE":
        assert updates == {"past_due_since": None}
    else:
        assert updates == {}, "PAST_DUE -> PAST_DUE keeps the original past_due_since"


def test_duplicates_are_no_ops():
    for status in STATES:
        if status in TERMINAL:
            continue
        state, _ = transition(None, _snap(status), now=NOW)
        assert transition(state, _snap(status), now=NOW + timedelta(minutes=1)) == (state, {})


def test_out_of_order_events_end_at_the_snapshot_state():
    # A late PAYMENT_FAILED processed after a later success re-fetches the
    # snapshot, which still says ACTIVE.
    state, _ = transition(None, _snap("PAST_DUE"), now=NOW)
    state, updates = transition(state, _snap("ACTIVE"), now=NOW + timedelta(hours=1))
    assert (state, updates) == ("ACTIVE", {"past_due_since": None})
    assert transition(state, _snap("ACTIVE"), now=NOW + timedelta(hours=2)) == ("ACTIVE", {})


def test_an_unrecognised_status_is_unknown():
    assert transition("ACTIVE", _snap("SOMETHING_NEW"), now=NOW)[0] == "UNKNOWN"
