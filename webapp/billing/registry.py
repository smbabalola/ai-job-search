"""Choosing the billing adapter for a deployment (Bundle 7 spec §12.2).

The fake provider serves local development and tests only. Hosted mode has no
live adapter until one is certified, so paid plans report PLAN_UNAVAILABLE
there (and release_readiness reports it) rather than faking a payment.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from webapp.billing.fake import FakeBillingProvider
from webapp.billing.port import BillingProvider


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fake_webhook_secret(settings: Any) -> bytes:
    return hashlib.sha256(f"fake-billing:{settings.secret_key or 'local-development'}".encode()).digest()


def provider_for(settings: Any, catalog: Any, *, deliver=None) -> BillingProvider | None:
    if settings.billing_provider != "fake" or settings.is_hosted:
        return None
    state_path = settings.db_path.parent / "fake-billing.json" if settings.db_path else None
    return FakeBillingProvider(state_path, fake_webhook_secret(settings), _now, price_map=catalog,
                               deliver=deliver, base_url=settings.app_origin)
