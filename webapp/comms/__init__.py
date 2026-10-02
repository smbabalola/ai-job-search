"""Outbound communications (Bundle 7 spec §18): the transactional outbox, the
provider-neutral email port, versioned templates, suppression and consent."""
from __future__ import annotations

from webapp.comms.outbox import enqueue
from webapp.comms.render import TEMPLATE_IDS

__all__ = ["TEMPLATE_IDS", "enqueue"]
