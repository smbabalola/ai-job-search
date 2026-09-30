"""The provider-neutral billing port (Bundle 7 spec §12.2).

Adapters normalize everything they return: a subscription's status is already
the internal state (§12.3) and its plan comes from the reverse price map. A
provider status an adapter cannot map is ``UNKNOWN``, which resolves to Free.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping, Protocol

SUBSCRIPTION_STATES = ("INCOMPLETE", "TRIALING", "ACTIVE", "PAST_DUE", "CANCEL_SCHEDULED", "ENDED",
                       "INCOMPLETE_EXPIRED", "UNKNOWN")
TERMINAL_STATES = frozenset({"ENDED", "INCOMPLETE_EXPIRED"})
INTERVALS = ("month", "year")


@dataclass(frozen=True)
class CheckoutRef:
    provider_session_id: str
    url: str


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


@dataclass(frozen=True)
class ProviderEvent:
    provider_event_id: str
    type: str
    subscription_id: str | None
    customer_id: str | None
    occurred_at: datetime
    raw: Mapping[str, Any]


class WebhookSignatureInvalid(Exception):
    """The webhook's signature, or its timestamp tolerance, did not verify."""


class BillingProvider(Protocol):
    name: str

    def ensure_customer(self, *, account_id: str, email: str) -> str: ...

    def create_checkout(self, *, customer_id: str, price_id: str, success_url: str, cancel_url: str,
                        trial_days: int, idempotency_key: str) -> CheckoutRef: ...

    def create_portal(self, *, customer_id: str, return_url: str) -> str: ...

    def change_plan(self, *, subscription_id: str, price_id: str, when: Literal["now", "period_end"]) -> None: ...

    def cancel(self, *, subscription_id: str, immediately: bool) -> None: ...

    def fetch_subscription(self, subscription_id: str) -> SubscriptionSnapshot: ...

    def verify_webhook(self, *, headers: Mapping[str, str], body: bytes, now: datetime) -> ProviderEvent: ...
