"""Account-initiated billing flows (Bundle 7 spec §12.3): choose a plan
(checkout), manage (portal), change plan, cancel, and status.

The provider is the source of truth for subscriptions. This service only asks
it for things; the subscription row changes when the provider's webhook is
processed (Task 15). No billing state ever locks a workspace (B3).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from product.entitlements import Catalog
from webapp.api.route_classes import ScopeRefused
from webapp.billing.port import INTERVALS, BillingProvider
from webapp.persistence import billing as rows
from webapp.persistence import dbapi
from webapp.persistence.audit import audit

# An OPEN checkout older than this is treated as abandoned and a fresh one is opened.
CHECKOUT_REUSE_WINDOW = timedelta(hours=23)


class BillingRefused(ScopeRefused):
    """PLAN_UNAVAILABLE, INVALID_PLAN, SUBSCRIPTION_EXISTS, NO_SUBSCRIPTION (§21.3)."""


def _unavailable() -> BillingRefused:
    return BillingRefused("PLAN_UNAVAILABLE", 409, "That plan is not available to buy yet.")


class BillingService:
    def __init__(self, provider: BillingProvider | None, catalog: Catalog, *, settings: Any,
                 request_id: str | None = None) -> None:
        self.provider = provider
        self.catalog = catalog
        self.settings = settings
        self.request_id = request_id

    def _url(self, path: str) -> str:
        return f"{self.settings.app_origin}{path}"

    def _price(self, plan_id: str, interval: str) -> str:
        if plan_id not in self.catalog.plans or interval not in INTERVALS:
            raise BillingRefused("INVALID_PLAN", 400, "Choose one of the listed plans.")
        price = self.catalog.plan(plan_id).provider_prices.get(interval)
        if not price or self.provider is None:
            raise _unavailable()  # DP-1/DP-3 unresolved, or no live adapter in this deployment
        return price

    def _email(self, conn: dbapi.Connection, scope: Any) -> str:
        if scope.user_id is None:
            return ""
        row = conn.execute("SELECT email_display FROM users WHERE id = ?", (scope.user_id,)).fetchone()
        return "" if row is None else row[0]

    def _customer_id(self, conn: dbapi.Connection, scope: Any, *, now: datetime) -> str:
        existing = rows.customer(conn, scope.account_id)
        if existing is not None:
            return existing["provider_customer_id"]
        customer_id = self.provider.ensure_customer(account_id=scope.account_id, email=self._email(conn, scope))
        try:
            rows.insert_customer(conn, account_id=scope.account_id, provider=self.provider.name,
                                 provider_customer_id=customer_id, now=now)
        except dbapi.IntegrityError:
            existing = rows.customer(conn, scope.account_id)  # a concurrent request recorded it first
            if existing is None:
                raise
            return existing["provider_customer_id"]
        return customer_id

    def _live(self, conn: dbapi.Connection, scope: Any) -> dict[str, Any]:
        live = rows.live_subscription(conn, scope.account_id)
        if live is None or self.provider is None:
            raise BillingRefused("NO_SUBSCRIPTION", 409, "There is no subscription to manage.")
        return live

    # ---- flows --------------------------------------------------------------------
    def start_checkout(self, conn: dbapi.Connection, scope: Any, *, plan_id: str, interval: str,
                       now: datetime) -> str:
        """Returns where to send the user. Free needs no provider call."""
        if plan_id == "free":
            return self._url("/settings/billing")
        price = self._price(plan_id, interval)
        live = rows.live_subscription(conn, scope.account_id)
        if live is not None and live["state"] != "INCOMPLETE":  # a failed first payment may be retried
            raise BillingRefused("SUBSCRIPTION_EXISTS", 409,
                                 "You already have a subscription. Change plan from Billing instead.")
        open_row = rows.open_checkout(conn, scope.account_id, plan_id, interval)
        if open_row is not None and now - datetime.fromisoformat(open_row["created_at"]) > CHECKOUT_REUSE_WINDOW:
            rows.expire_checkout(conn, open_row["id"])
            open_row = None
        customer_id = self._customer_id(conn, scope, now=now)
        checkout_id = open_row["id"] if open_row is not None else f"chk_{uuid.uuid4().hex[:20]}"
        trial = 0 if rows.has_subscription_history(conn, scope.account_id) \
            else self.catalog.plan(plan_id).trial_days or 0
        # The row id is the idempotency key: a second click reaches the same provider session.
        ref = self.provider.create_checkout(
            customer_id=customer_id, price_id=price,
            success_url=self._url(f"/settings/billing?checkout={checkout_id}"), cancel_url=self._url("/plans"),
            trial_days=trial, idempotency_key=checkout_id)
        if open_row is None:
            try:
                rows.insert_checkout(conn, checkout_id=checkout_id, account_id=scope.account_id,
                                     provider=self.provider.name, provider_session_id=ref.provider_session_id,
                                     plan_id=plan_id, interval=interval, now=now)
            except dbapi.IntegrityError:
                # A concurrent click opened the session first: send both to that one.
                winner = rows.open_checkout(conn, scope.account_id, plan_id, interval)
                if winner is None:
                    raise
                return self.provider.create_checkout(
                    customer_id=customer_id, price_id=price,
                    success_url=self._url(f"/settings/billing?checkout={winner['id']}"),
                    cancel_url=self._url("/plans"), trial_days=trial, idempotency_key=winner["id"]).url
            audit(conn, actor_type="USER", actor_id=scope.user_id, account_id=scope.account_id,
                  action="PLAN_CHECKOUT_STARTED", now=now, target_type="checkout_session", target_id=checkout_id,
                  request_id=self.request_id, detail={"plan_id": plan_id, "interval": interval})
        return ref.url

    def portal_url(self, conn: dbapi.Connection, scope: Any, *, now: datetime) -> str:
        customer = rows.customer(conn, scope.account_id)
        if customer is None or self.provider is None:
            raise BillingRefused("NO_SUBSCRIPTION", 409, "There is no billing account to manage yet.")
        return self.provider.create_portal(customer_id=customer["provider_customer_id"],
                                           return_url=self._url("/settings/billing"))

    def change_plan(self, conn: dbapi.Connection, scope: Any, *, plan_id: str, interval: str,
                    now: datetime) -> dict[str, str]:
        """B3: upgrades apply now (the provider prorates); downgrades, interval
        changes to a cheaper plan, and moving to Free apply at period end."""
        live = self._live(conn, scope)
        if plan_id == "free":
            self.provider.cancel(subscription_id=live["provider_subscription_id"], immediately=False)
            return {"when": "period_end"}
        price = self._price(plan_id, interval)
        current_rank = self.catalog.plan(live["plan_id"]).rank if live["plan_id"] in self.catalog.plans else -1
        when = "now" if self.catalog.plan(plan_id).rank > current_rank else "period_end"
        self.provider.change_plan(subscription_id=live["provider_subscription_id"], price_id=price, when=when)
        return {"when": when}

    def cancel(self, conn: dbapi.Connection, scope: Any, *, now: datetime) -> dict[str, str]:
        live = self._live(conn, scope)
        self.provider.cancel(subscription_id=live["provider_subscription_id"], immediately=False)
        return {"when": "period_end"}

    def resume(self, conn: dbapi.Connection, scope: Any, *, now: datetime) -> str:
        """Undoing a scheduled cancellation happens in the provider's portal; the
        port (§12.2) has no separate call for it."""
        self._live(conn, scope)
        return self.portal_url(conn, scope, now=now)

    def status(self, conn: dbapi.Connection, scope: Any) -> dict[str, Any]:
        live = rows.live_subscription(conn, scope.account_id)
        checkout = rows.latest_checkout(conn, scope.account_id)
        return {
            "plan_id": live["plan_id"] if live else "free",
            "state": live["state"] if live else None,
            "interval": live["interval"] if live else None,
            "current_period_end": live["current_period_end"] if live else None,
            "cancel_at_period_end": bool(live["cancel_at_period_end"]) if live else False,
            "checkout": None if checkout is None else {"id": checkout["id"], "status": checkout["status"],
                                                       "plan_id": checkout["plan_id"]},
        }
