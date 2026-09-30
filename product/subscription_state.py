"""Subscription state machine (Bundle 7 spec §12.3). Pure.

Every processed provider event re-fetches the subscription snapshot, so the
snapshot's (already normalized) status is the new state. That makes duplicate
and out-of-order events harmless. Two rules sit on top: a terminal state is
never left (the provider does not revive an ended subscription, and the account
may already have a new live one), and ``past_due_since`` is set on entering
PAST_DUE, kept while it lasts, and cleared on leaving.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from webapp.billing.port import SUBSCRIPTION_STATES, TERMINAL_STATES, SubscriptionSnapshot

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
