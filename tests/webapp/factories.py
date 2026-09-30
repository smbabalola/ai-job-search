"""Per-account object graphs for tenant-isolation tests (Bundle 7 spec §10.6).

``build_account_graph`` gives one account a representative set of owned rows,
each carrying a unique canary string, and returns the ids keyed by the path
parameter names routes use. Tables covered today:

  search_workspaces, search_workspace_user_profiles, user_profile_versions,
  workspaces (job), artifacts, workflow_events, application_document_versions,
  application_document_selections, handoff_sessions, extension_devices (+ tokens).

Later tasks extend this graph as their tables arrive (CV library, approvals,
fill runs, notifications, devices ...).
"""
from __future__ import annotations

from datetime import datetime, timezone

import uuid
from typing import Any

from tests.webapp.services.review_fixtures import docx_bytes
from webapp.persistence.artifacts import save_artifact
from tests.webapp.extension_helpers import device_bearer
from webapp.persistence.handoff import create_handoff_session
from webapp.persistence.search_workspaces import create_search_workspace
from webapp.persistence.user_profile import save_user_profile
from webapp.persistence.workflow import record_status_change
from webapp.persistence.workspaces import create_workspace
from webapp.services.application_documents import record_uploaded_version, store_upload_blob


def build_account_graph(conn, *, account_id: str, documents_root) -> dict[str, Any]:
    canary = f"CANARY-{uuid.uuid4().hex[:10]}"
    search = create_search_workspace(conn, name=f"{canary} search", account_id=account_id)
    save_user_profile(conn, {"schema_version": "user-profile.v2", "target_roles": [f"{canary} role"]},
                      search_workspace_id=search["id"], account_id=account_id)
    job = create_workspace(conn, company=f"{canary} Company", title=f"{canary} Title", account_id=account_id)
    posting = save_artifact(conn, workspace_id=job["id"], artifact_type="job_posting_snapshot",
                            payload={"title": f"{canary} Title", "description": f"{canary} description"})
    blob = store_upload_blob(kind="cv", filename=f"{canary}.docx", content=docx_bytes(f"{canary} CV"),
                             documents_root=documents_root, account_id=account_id)
    conn.execute("BEGIN IMMEDIATE")
    document = record_uploaded_version(conn, job["id"], kind="cv", filename=f"{canary}.docx", blob=blob,
                                       account_id=account_id)
    conn.commit()
    record_status_change(conn, workspace_id=job["id"], new_status="withdrawn", effective_date="2026-09-30",
                         note=f"{canary} note", account_id=account_id)
    session = create_handoff_session(conn, account_id=account_id, workspace_id=job["id"],
                                     pack_artifact_id=posting["id"], target_url="https://jobs.example.test/1",
                                     target_domain="jobs.example.test", ats_adapter_id="generic",
                                     ats_adapter_version="1")
    from types import SimpleNamespace
    from webapp.services import cv_library
    cv_scope = SimpleNamespace(account_id=account_id, user_id=None)
    cv_item = cv_library.create_item(conn, cv_scope, title=f"{canary} CV library item", now=datetime.now(timezone.utc))
    cv_version = cv_library.add_version(conn, cv_scope, item_id=cv_item["id"], content=docx_bytes(f"{canary} library CV"),
                                        filename=f"{canary}-library.docx", media_type_hint=None,
                                        documents_root=documents_root, note=f"{canary} note",
                                        now=datetime.now(timezone.utc))
    conn.commit()
    from webapp.services.notifications import notify
    notify(conn, account_id=account_id, kind="fill.failed", subject_type="workspace", subject_id=job["id"],
           dedupe_key=f"{canary}-notification", detail={"message": f"{canary} notification"},
           now=datetime.now(timezone.utc))
    notification_id = conn.execute("SELECT id FROM notifications WHERE account_id = ? AND dedupe_key = ?",
                                   (account_id, f"{canary}-notification")).fetchone()[0]
    user = conn.execute("SELECT user_id FROM account_memberships WHERE account_id = ? AND role = 'OWNER'",
                        (account_id,)).fetchone()
    extension_bearer = device_bearer(conn, account_id, user["user_id"] if user else None)
    conn.commit()
    return {
        "canary": canary,
        "extension_bearer": extension_bearer,
        "ids": {
            "workspace_id": job["id"], "search_workspace_id": search["id"], "document_version_id": document["id"],
            "session_id": session["id"], "pack_artifact_id": posting["id"], "kind": "cv",
            "notification_id": notification_id, "item_id": cv_item["id"], "version_id": cv_version["id"],
        },
    }
