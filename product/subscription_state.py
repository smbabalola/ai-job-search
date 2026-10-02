"""Subscription state machine (Bundle 7 spec §12.3). Pure.

Every processed provider event re-fetches the subscription snapshot, so the
snapshot's (already normalized) status is the new state. That makes duplicate
and out-of-order events harmless. Two rules sit on top: a terminal state is
never left (the provider does not revive an ended subscription, and the account
may already have a new live one), and ``past_due_since`` is set on entering
PAST_DUE, kept while it lasts, and cleared on leaving.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# The internal state vocabulary and the normalized snapshot live here (pure) and
# are re-exported by webapp.billing.port: product never imports webapp.
SUBSCRIPTION_STATES = ("INCOMPLETE", "TRIALING", "ACTIVE", "PAST_DUE", "CANCEL_SCHEDULED", "ENDED",
                       "INCOMPLETE_EXPIRED", "UNKNOWN")
TERMINAL_STATES = frozenset({"ENDED", "INCOMPLETE_EXPIRED"})


@dataclass(frozen=True)
class SubscriptionSnapshot:
    provider_subscription_id: str
    provider_customer_id: str
    status: str  # an internal state, one of SUBSCRIPTION_STATES
    plan_id: str
    interval: str
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool
    raw_hash: str


STATES: tuple[str, ...] = SUBSCRIPTION_STATES
TERMINAL = TERMINAL_STATES


def transition(current: str | None, snapshot: SubscriptionSnapshot, *, now: datetime) -> tuple[str, dict[str, Any]]:
    if current in TERMINAL:
        return current, {}
    new_state = snapshot.status if snapshot.status in STATES else "UNKNOWN"
    updates: dict[str, Any] = {}
    if new_state == "PAST_DUE" and current != "PAST_DUE":
        updates["past_due_since"] = now
    elif new_state != "PAST_DUE" and current == "PAST_DUE":
        updates["past_due_since"] = None
    return new_state, updates
