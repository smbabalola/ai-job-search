from __future__ import annotations

from typing import Any

from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID

from webapp.persistence.workflow import list_workflow_events
from webapp.services.http_api import require_job_workspace
from webapp.persistence import dbapi


def list_events(
    conn: dbapi.Connection, workspace_id: str, *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> list[dict[str, Any]]:
    require_job_workspace(conn, workspace_id, account_id=account_id)
    return list_workflow_events(conn, workspace_id)
