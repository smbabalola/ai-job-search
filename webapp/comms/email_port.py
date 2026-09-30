"""The provider-neutral email port (Bundle 7 spec §18, DP-2). Adapters map
their errors onto the two send outcomes the outbox understands."""
from __future__ import annotations

from typing import Mapping, Protocol


class TransientSendError(Exception):
    """Retry later (4xx, timeouts, a dropped connection)."""


class PermanentSendError(Exception):
    """Never retry (5xx recipient refusal, a malformed message)."""


class EmailProvider(Protocol):
    name: str

    def send(self, *, to: str, subject: str, text: str, html: str, idempotency_key: str,
             headers: Mapping[str, str]) -> str:
        """Hand the message to the provider; returns its message id."""
