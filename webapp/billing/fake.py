"""FakeBillingProvider (Bundle 7 spec §12.2): a complete in-process provider for
local development, tests and the journey suites. Never used in hosted mode
(see ``registry.provider_for``).

It keeps customers, checkout sessions and subscriptions in memory, or in a JSON
file when ``state_path`` is given, and emits signed webhooks through the
injected ``deliver(headers, body)`` callable. Provider statuses use a
Stripe-like vocabulary and are normalized to internal states on the way out.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Mapping
from urllib.parse import quote

from webapp.billing.port import CheckoutRef, ProviderEvent, SubscriptionSnapshot, WebhookSignatureInvalid

__all__ = ["FakeBillingProvider", "WebhookSignatureInvalid", "SIGNATURE_HEADER"]

SIGNATURE_HEADER = "Fake-Signature"
TOLERANCE = timedelta(minutes=5)
PERIOD = {"month": timedelta(days=30), "year": timedelta(days=365)}

Deliver = Callable[[dict[str, str], bytes], None]


def _status(provider_status: str, cancel_at_period_end: bool) -> str:
    if provider_status in ("active", "trialing") and cancel_at_period_end:
        return "CANCEL_SCHEDULED"
    return {"active": "ACTIVE", "trialing": "TRIALING", "past_due": "PAST_DUE", "canceled": "ENDED",
            "incomplete": "INCOMPLETE", "incomplete_expired": "INCOMPLETE_EXPIRED"}.get(provider_status, "UNKNOWN")


class FakeBillingProvider:
    name = "fake"

    def __init__(self, state_path: Path | None, secret: bytes, clock: Callable[[], datetime], *,
                 price_map: Any = None, deliver: Deliver | None = None, base_url: str = "") -> None:
        self.state_path = Path(state_path) if state_path is not None else None
        self.secret = secret
        self.clock = clock
        self.base_url = base_url.rstrip("/")
        self.outbox: list[tuple[dict[str, str], bytes]] = []
        self.deliver: Deliver = deliver or (lambda headers, body: self.outbox.append((headers, body)))
        # price id -> (plan_id, interval), from a Catalog's provider_prices.
        self.prices: dict[str, tuple[str, str]] = {}
        if price_map is not None:
            for plan in price_map.ranked():
                for interval, price in plan.provider_prices.items():
                    if price:
                        self.prices[price] = (plan.plan_id, interval)
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {"customers": {}, "sessions": {}, "idempotency": {}, "subscriptions": {},
                                       "event_seq": 0}
        if self.state_path is not None and self.state_path.exists():
            self._state = json.loads(self.state_path.read_text(encoding="utf-8"))

    # ---- bookkeeping ------------------------------------------------------------
    def _save(self) -> None:
        if self.state_path is not None:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8")

    def _next(self, prefix: str) -> str:
        self._state["event_seq"] += 1
        return f"{prefix}_fake_{self._state['event_seq']:06d}"

    def _emit(self, event_type: str, *, subscription_id: str | None, customer_id: str | None,
              session_id: str | None = None) -> None:
        event = {"id": self._next("evt"), "type": event_type, "created": self.clock().isoformat(),
                 "data": {"subscription": subscription_id, "customer": customer_id, "session": session_id}}
        body = json.dumps(event, sort_keys=True).encode("utf-8")
        self._save()
        self.deliver(self.sign(body), body)

    def sign(self, body: bytes, *, at: datetime | None = None) -> dict[str, str]:
        timestamp = int((at or self.clock()).timestamp())
        digest = hmac.new(self.secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
        return {SIGNATURE_HEADER: f"t={timestamp},v1={digest}", "Content-Type": "application/json"}

    # ---- the port ---------------------------------------------------------------
    def ensure_customer(self, *, account_id: str, email: str) -> str:
        with self._lock:
            customer = self._state["customers"].get(account_id)
            if customer is None:
                customer = {"id": self._next("cus"), "email": email}
                self._state["customers"][account_id] = customer
                self._save()
            return customer["id"]

    def create_checkout(self, *, customer_id: str, price_id: str, success_url: str, cancel_url: str,
                        trial_days: int, idempotency_key: str) -> CheckoutRef:
        with self._lock:
            session_id = self._state["idempotency"].get(idempotency_key)
            if session_id is None:
                if price_id not in self.prices:
                    raise ValueError(f"unknown price {price_id}")
                session_id = self._next("cs")
                self._state["sessions"][session_id] = {
                    "customer": customer_id, "price": price_id, "success_url": success_url,
                    "cancel_url": cancel_url, "trial_days": trial_days, "status": "open", "subscription": None}
                self._state["idempotency"][idempotency_key] = session_id
                self._save()
            return CheckoutRef(provider_session_id=session_id,
                               url=f"{self.base_url}/dev/billing/checkout/{session_id}")

    def create_portal(self, *, customer_id: str, return_url: str) -> str:
        return f"{self.base_url}/dev/billing/portal/{customer_id}?return_to={quote(return_url, safe='')}"

    def change_plan(self, *, subscription_id: str, price_id: str, when: Literal["now", "period_end"]) -> None:
        with self._lock:
            subscription = self._subscription(subscription_id)
            if price_id not in self.prices:
                raise ValueError(f"unknown price {price_id}")
            if when == "now":
                subscription["price"], subscription["scheduled_price"] = price_id, None
            else:
                subscription["scheduled_price"] = price_id
            self._emit("customer.subscription.updated", subscription_id=subscription_id,
                       customer_id=subscription["customer"])

    def cancel(self, *, subscription_id: str, immediately: bool) -> None:
        with self._lock:
            subscription = self._subscription(subscription_id)
            if immediately:
                subscription["status"] = "canceled"
            else:
                subscription["cancel_at_period_end"] = True
            self._emit("customer.subscription.updated" if not immediately else "customer.subscription.deleted",
                       subscription_id=subscription_id, customer_id=subscription["customer"])

    def fetch_subscription(self, subscription_id: str) -> SubscriptionSnapshot:
        with self._lock:
            sub = self._subscription(subscription_id)
            plan_id, interval = self.prices.get(sub["price"], ("free", "month"))
            status = _status(sub["status"], sub["cancel_at_period_end"])
            if sub["price"] not in self.prices:
                status = "UNKNOWN"
            raw = json.dumps(sub, sort_keys=True).encode("utf-8")
            return SubscriptionSnapshot(
                provider_subscription_id=subscription_id, provider_customer_id=sub["customer"], status=status,
                plan_id=plan_id, interval=interval,
                current_period_start=datetime.fromisoformat(sub["period_start"]),
                current_period_end=datetime.fromisoformat(sub["period_end"]),
                cancel_at_period_end=bool(sub["cancel_at_period_end"]),
                raw_hash=hashlib.sha256(raw).hexdigest(),
            )

    def verify_webhook(self, *, headers: Mapping[str, str], body: bytes, now: datetime) -> ProviderEvent:
        lowered = {key.lower(): value for key, value in headers.items()}
        try:
            parts = dict(item.split("=", 1) for item in lowered[SIGNATURE_HEADER.lower()].split(","))
            timestamp, signature = int(parts["t"]), parts["v1"]
        except (KeyError, ValueError) as exc:
            raise WebhookSignatureInvalid("missing or malformed signature") from exc
        expected = hmac.new(self.secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise WebhookSignatureInvalid("signature mismatch")
        if abs(now.timestamp() - timestamp) > TOLERANCE.total_seconds():
            raise WebhookSignatureInvalid("signature timestamp outside tolerance")
        event = json.loads(body)
        data = event.get("data") or {}
        return ProviderEvent(provider_event_id=event["id"], type=event["type"],
                             subscription_id=data.get("subscription"), customer_id=data.get("customer"),
                             occurred_at=datetime.fromisoformat(event["created"]), raw=event)

    # ---- simulation (the dev checkout page, tests, journeys) --------------------
    def _subscription(self, subscription_id: str) -> dict[str, Any]:
        try:
            return self._state["subscriptions"][subscription_id]
        except KeyError:
            raise ValueError(f"unknown subscription {subscription_id}") from None

    def session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._state["sessions"].get(session_id)
            return None if session is None else dict(session)

    def simulate(self, session_id: str, outcome: Literal["pay", "fail", "cancel"]) -> str:
        """Complete a checkout as the customer would; returns the redirect url."""
        with self._lock:
            session = self._state["sessions"].get(session_id)
            if session is None:
                raise ValueError(f"unknown checkout session {session_id}")
            if session["status"] != "open":
                return session["success_url"] if session["status"] == "complete" else session["cancel_url"]
            if outcome == "cancel":
                session["status"] = "canceled"
                self._emit("checkout.session.canceled", subscription_id=None, customer_id=session["customer"],
                           session_id=session_id)
                return session["cancel_url"]
            now = self.clock()
            plan_interval = self.prices[session["price"]][1]
            trial = session["trial_days"] if outcome == "pay" else 0
            subscription_id = self._next("sub")
            self._state["subscriptions"][subscription_id] = {
                "customer": session["customer"], "price": session["price"], "scheduled_price": None,
                "status": ("trialing" if trial else "active") if outcome == "pay" else "incomplete",
                "period_start": now.isoformat(),
                "period_end": (now + (timedelta(days=trial) if trial else PERIOD[plan_interval])).isoformat(),
                "cancel_at_period_end": False,
            }
            session["subscription"] = subscription_id
            session["status"] = "complete" if outcome == "pay" else "failed"
            self._emit("checkout.session.completed" if outcome == "pay" else "invoice.payment_failed",
                       subscription_id=subscription_id, customer_id=session["customer"], session_id=session_id)
            return session["success_url"] if outcome == "pay" else session["cancel_url"]

    def fail_renewal(self, subscription_id: str) -> None:
        with self._lock:
            subscription = self._subscription(subscription_id)
            subscription["status"] = "past_due"
            self._emit("invoice.payment_failed", subscription_id=subscription_id,
                       customer_id=subscription["customer"])

    def advance_period(self, subscription_id: str) -> None:
        """Roll to the next period: end a cancelled subscription, apply a
        scheduled change, or renew."""
        with self._lock:
            sub = self._subscription(subscription_id)
            if sub["cancel_at_period_end"]:
                sub["status"] = "canceled"
                self._emit("customer.subscription.deleted", subscription_id=subscription_id,
                           customer_id=sub["customer"])
                return
            if sub.get("scheduled_price"):
                sub["price"], sub["scheduled_price"] = sub["scheduled_price"], None
            start = datetime.fromisoformat(sub["period_end"])
            sub["period_start"] = start.isoformat()
            sub["period_end"] = (start + PERIOD[self.prices.get(sub["price"], ("", "month"))[1]]).isoformat()
            sub["status"] = "active"
            self._emit("invoice.paid", subscription_id=subscription_id, customer_id=sub["customer"])

    def subscription_for_session(self, session_id: str) -> str | None:
        with self._lock:
            session = self._state["sessions"].get(session_id)
            return None if session is None else session["subscription"]

    def scheduled_change(self, subscription_id: str) -> str | None:
        with self._lock:
            price = self._subscription(subscription_id).get("scheduled_price")
            return None if price is None else self.prices[price][0]

    def customer_count(self) -> int:
        with self._lock:
            return len(self._state["customers"])
