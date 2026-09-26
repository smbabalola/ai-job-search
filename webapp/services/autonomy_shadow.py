"""Shadow mode (6B spec §14.2 stage 1): evaluate and record what autonomy
WOULD decide at existing workflow points. Never creates executable
authority. A shadow failure is logged and swallowed -- it must never break the
user's real workflow.

Deviation from the task-17 brief: a paused application or search workspace
records nothing (pause means no new decisions, user ruling 2026-09-26) and is
not logged as a failure."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from product.autonomy_contract import Capability, Mode
from webapp.config import Settings
from webapp.services.autonomy import AutonomyPaused, decide_and_record

logger = logging.getLogger(__name__)


def record_shadow_decision(conn: sqlite3.Connection, *, settings: Settings, account_id: str, workspace_id: str,
                           stage: Capability, now: datetime | None = None) -> dict[str, Any] | None:
    if not settings.autonomy_shadow_enabled:
        return None
    try:
        _, row = decide_and_record(conn, settings=settings, account_id=account_id,
                                   application_workspace_id=workspace_id, requested_stage=stage,
                                   mode=Mode.SHADOW, now=now or datetime.now(timezone.utc))
        return row
    except AutonomyPaused:
        logger.debug("autonomy shadow skipped for paused workspace %s", workspace_id)
        return None
    except Exception:
        logger.exception("autonomy shadow evaluation failed for workspace %s", workspace_id)
        return None
