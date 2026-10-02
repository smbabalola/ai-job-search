from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID, validate_account_id
from webapp.persistence.search_workspaces import get_search_workspace
from webapp.persistence.workspaces import (
    ensure_profile_workspace,
    get_profile_workspace_id,
    get_workspace,
)
from webapp.persistence import dbapi


class OwnedResourceNotFound(LookupError):
    """A missing and a cross-owner resource deliberately share one result."""


@dataclass(frozen=True)
class AccountScope:
    account_id: str
    profile_root: Path
    # Bundle 7 (spec H7): where this account's profile sources live. None
    # means the filesystem root above (local mode, pre-Bundle-7 behaviour).
    profile_store: Any = None
    user_id: str | None = None  # the signed-in user (None in local single-user mode)

    def profile_sources(self, conn: dbapi.Connection):
        """This account's profile sources, bound to ``conn``."""
        from webapp.storage.profile_sources import FilesystemProfileSources

        if self.profile_store is None:
            return FilesystemProfileSources(self.profile_root)
        return self.profile_store.for_account(conn, self.account_id)

    def require_search_workspace(
        self, conn: dbapi.Connection, search_workspace_id: str
    ) -> dict[str, Any]:
        workspace = get_search_workspace(
            conn, search_workspace_id, account_id=self.account_id
        )
        if workspace is None:
            raise OwnedResourceNotFound("search workspace not found")
        return workspace

    def require_job_workspace(
        self, conn: dbapi.Connection, workspace_id: str
    ) -> dict[str, Any]:
        workspace = get_workspace(
            conn, workspace_id, account_id=self.account_id
        )
        if workspace is None or workspace["kind"] != "job":
            raise OwnedResourceNotFound("job workspace not found")
        return workspace

    def profile_workspace_id(
        self, conn: dbapi.Connection, *, ensure: bool = False
    ) -> str | None:
        if ensure:
            return ensure_profile_workspace(
                conn, account_id=self.account_id
            )["id"]
        return get_profile_workspace_id(conn, self.account_id)


def account_profile_root(base_root: str | Path, account_id: str) -> Path:
    root = Path(base_root).resolve()
    if account_id == DEFAULT_ACCOUNT_ID:
        return root
    return root / ".jobsearch" / "accounts" / validate_account_id(account_id)
