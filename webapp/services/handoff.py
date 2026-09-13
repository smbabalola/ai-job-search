from __future__ import annotations

import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from webapp.persistence.artifacts import get_artifact
from webapp.persistence.handoff import (
    append_handoff_event,
    create_extension_credential,
    create_handoff_session,
    create_session_token,
    create_submission_confirmation,
    find_in_progress_handoff_sessions,
    get_extension_credential_by_hash,
    get_handoff_session,
    get_session_token_row,
    hash_pairing_secret,
    list_handoff_events,
    refresh_session_activity,
    revoke_session_tokens,
    set_handoff_session_status,
)
from webapp.persistence.workflow import record_status_change
from webapp.services.ownership import AccountScope, account_profile_root


class HandoffError(RuntimeError):
    pass


class PairingSecretInvalid(HandoffError):
    pass


class PairingSecretExpired(HandoffError):
    pass


def generate_pairing_secret(
    conn: sqlite3.Connection, *, account_id: str, commit: bool = True,
) -> str:
    secret = secrets.token_urlsafe(32)
    secret_id = f"pairsec_{uuid.uuid4().hex[:20]}"
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=10)).isoformat()
    conn.execute(
        "INSERT INTO pairing_secrets "
        "(id, account_id, secret_hash, created_at, expires_at, consumed_at) "
        "VALUES (?, ?, ?, ?, ?, NULL)",
        (secret_id, account_id, hash_pairing_secret(secret), now.isoformat(), expires_at),
    )
    if commit:
        conn.commit()
    return secret


def exchange_pairing_secret_for_credential(
    conn: sqlite3.Connection, *, one_time_secret: str,
) -> dict[str, Any]:
    secret_hash = hash_pairing_secret(one_time_secret)
    row = conn.execute(
        "SELECT * FROM pairing_secrets WHERE secret_hash = ?", (secret_hash,)
    ).fetchone()
    if row is None:
        raise PairingSecretInvalid("pairing code not recognized")
    if row["consumed_at"] is not None:
        raise PairingSecretInvalid("pairing code already used")
    now_iso = datetime.now(timezone.utc).isoformat()
    if row["expires_at"] < now_iso:
        raise PairingSecretExpired("pairing code expired")

    conn.execute(
        "UPDATE pairing_secrets SET consumed_at = ? WHERE id = ?",
        (now_iso, row["id"]),
    )
    durable_secret = secrets.token_urlsafe(32)
    credential = create_extension_credential(
        conn, account_id=row["account_id"],
        secret_hash=hash_pairing_secret(durable_secret),
    )
    conn.commit()
    return {"credential_id": credential["id"], "durable_secret": durable_secret}


def resolve_account_scope_from_extension_credential(
    conn: sqlite3.Connection, *, presented_secret: str, base_profile_root: str,
) -> AccountScope:
    secret_hash = hash_pairing_secret(presented_secret)
    credential = get_extension_credential_by_hash(conn, secret_hash)
    if credential is None:
        raise PairingSecretInvalid("extension credential not recognized or revoked")
    return AccountScope(
        account_id=credential["account_id"],
        profile_root=account_profile_root(base_profile_root, credential["account_id"]),
    )


class HandoffPackNotFound(HandoffError):
    pass


def start_handoff_session(
    conn: sqlite3.Connection,
    scope: AccountScope,
    *,
    workspace_id: str,
    pack_artifact_id: str,
    target_url: str,
    target_domain: str,
    ats_adapter_id: str,
    ats_adapter_version: str,
) -> dict[str, Any]:
    # Ownership is checked before the pack artifact is ever read, matching
    # the existing render route's order exactly (design spec Section 17).
    scope.require_job_workspace(conn, workspace_id)

    artifact = get_artifact(conn, pack_artifact_id)
    if (
        artifact is None
        or artifact["workspace_id"] != workspace_id
        or artifact["artifact_type"] != "application_pack"
    ):
        raise HandoffPackNotFound(
            f"application pack artifact {pack_artifact_id!r} does not "
            f"belong to workspace {workspace_id!r}"
        )

    return create_handoff_session(
        conn,
        account_id=scope.account_id,
        workspace_id=workspace_id,
        pack_artifact_id=pack_artifact_id,
        target_url=target_url,
        target_domain=target_domain,
        ats_adapter_id=ats_adapter_id,
        ats_adapter_version=ats_adapter_version,
    )


def discover_resumable_handoff_sessions(
    conn: sqlite3.Connection,
    scope: AccountScope,
    *,
    workspace_id: str,
    target_domain: str,
) -> list[dict[str, Any]]:
    # Discovery only — never used to resolve a session's identity or to
    # grant authorization (design spec Section 5.2).
    scope.require_job_workspace(conn, workspace_id)
    candidates = find_in_progress_handoff_sessions(
        conn, account_id=scope.account_id, workspace_id=workspace_id,
        target_domain=target_domain,
    )
    # The server is the authority on expiry, not the browser extension
    # (design spec Section 4) — an expired session must never be offered
    # as a resumable candidate here, even though resume_handoff_session
    # would separately reject it if a caller tried anyway. Filtering here
    # too means a client never even sees a stale session to choose from.
    now = datetime.now(timezone.utc)
    return [
        session for session in candidates
        if now - datetime.fromisoformat(session["last_activity_at"])
        <= HANDOFF_SESSION_INACTIVITY_TIMEOUT
    ]


class HandoffSessionNotFound(HandoffError):
    pass


class HandoffSessionNotActive(HandoffError):
    pass


class HandoffSessionTokenInvalid(HandoffError):
    pass


class HandoffSessionExpired(HandoffError):
    pass


# Not user-configurable, not a settings-table row — one internal constant.
# Long enough for a real application with interruptions, short enough
# that an abandoned handoff is not resumable indefinitely (design spec
# Section 4).
HANDOFF_SESSION_INACTIVITY_TIMEOUT = timedelta(hours=2)


@dataclass(frozen=True)
class SessionScope:
    account_id: str
    handoff_session_id: str
    workspace_id: str
    pack_artifact_id: str


def mint_session_token(conn: sqlite3.Connection, *, handoff_session_id: str) -> str:
    return create_session_token(conn, handoff_session_id=handoff_session_id)


def rotate_session_token(conn: sqlite3.Connection, *, handoff_session_id: str) -> str:
    # Revoking every prior live token before minting the new one keeps
    # exactly one live token per session at a time — an old, possibly
    # leaked token stops working the moment the session is re-authorized
    # (design spec Section 3.2).
    revoke_session_tokens(conn, handoff_session_id=handoff_session_id, commit=False)
    return create_session_token(conn, handoff_session_id=handoff_session_id)


def resolve_session_scope(conn: sqlite3.Connection, *, raw_token: str) -> SessionScope:
    token_hash = hash_pairing_secret(raw_token)
    row = get_session_token_row(conn, token_hash=token_hash)
    if row is None:
        raise HandoffSessionTokenInvalid("session token not recognized")
    if row["status"] != "in_progress":
        raise HandoffSessionTokenInvalid("session is not active")
    last_activity = datetime.fromisoformat(row["last_activity_at"])
    if datetime.now(timezone.utc) - last_activity > HANDOFF_SESSION_INACTIVITY_TIMEOUT:
        raise HandoffSessionExpired("session has expired")
    refresh_session_activity(conn, handoff_session_id=row["handoff_session_id"])
    return SessionScope(
        account_id=row["account_id"],
        handoff_session_id=row["handoff_session_id"],
        workspace_id=row["workspace_id"],
        pack_artifact_id=row["pack_artifact_id"],
    )


def resume_handoff_session(
    conn: sqlite3.Connection, scope: AccountScope, *, handoff_session_id: str,
) -> str:
    # Ownership failure and not-found are deliberately indistinguishable
    # here (both raise HandoffSessionNotFound), matching
    # _require_owned_session's existing convention below — this avoids an
    # account-enumeration side channel a distinct "not owned" error would
    # create.
    session = get_handoff_session(conn, handoff_session_id)
    if session is None or session["account_id"] != scope.account_id:
        raise HandoffSessionNotFound(f"handoff session {handoff_session_id!r} not found")
    if session["status"] != "in_progress":
        # A terminal session is not resumable; surfaced identically to
        # "not found" rather than as a distinct state, since there is
        # nothing a caller can act on differently for either case.
        raise HandoffSessionNotFound(f"handoff session {handoff_session_id!r} not found")
    last_activity = datetime.fromisoformat(session["last_activity_at"])
    if datetime.now(timezone.utc) - last_activity > HANDOFF_SESSION_INACTIVITY_TIMEOUT:
        raise HandoffSessionExpired(f"handoff session {handoff_session_id!r} has expired")

    token = rotate_session_token(conn, handoff_session_id=handoff_session_id)
    refresh_session_activity(conn, handoff_session_id=handoff_session_id)
    return token


class HandoffPackArtifactInvalid(HandoffError):
    pass


# Closed, server-owned mapping: the ONLY set of normalized_field_types
# this endpoint can ever release candidate data for, and the ONLY place
# that decides which candidate_snapshot path each one reads. A request
# can select a subset of this mapping; it can never name a path outside
# it (design spec Section 7.2 step 3) — this is what makes "cannot
# become a bulk dump" structural rather than conventional.
#
# Enumerated directly from every real adapter on this branch
# (extension/src/adapters/{generic,greenhouse,lever}.ts) as of this
# task: the six shared safe-catalog types (name, email, phone, linkedin,
# github, location) every adapter can emit, plus Greenhouse's one
# adapter-specific rule (employment[0].employer). "legal_declaration"
# and "unknown" are deliberately absent — neither is ever candidate-
# backed (behavior never/ask, sourceKind none). "years_of_experience"
# is ALSO deliberately absent: Greenhouse's classify() emits it, but its
# map() has no derivation for it today (returns null with a "Task 15"
# comment marking it unimplemented) - adding a mapping entry here would
# invent a derivation this codebase doesn't have, not merely wire one
# up. A request for it degrades to "no value released" (same as any
# other unrecognized type), not an error and not a fabricated value.
_NORMALIZED_FIELD_TYPE_TO_CANDIDATE_PATH: dict[str, tuple[str, ...]] = {
    "name": ("identity", "name"),
    "email": ("contact", "email"),
    "phone": ("contact", "phone"),
    "linkedin": ("contact", "linkedin"),
    "github": ("contact", "github"),
    "location": ("contact", "location"),
    "employment[0].employer": ("employment", 0, "employer"),
}


def _resolve_candidate_path(candidate_snapshot: dict[str, Any], path: tuple) -> Any | None:
    value: Any = candidate_snapshot
    for segment in path:
        if isinstance(segment, int):
            if not isinstance(value, list) or segment >= len(value):
                return None
            value = value[segment]
        else:
            if not isinstance(value, dict) or segment not in value:
                return None
            value = value[segment]
    return value


def project_session_snapshot(
    conn: sqlite3.Connection, scope: SessionScope, *, normalized_field_types: list[str],
) -> dict[str, Any]:
    # scope.pack_artifact_id is the session's own immutable pin, resolved
    # entirely from the presented session token — never a fresh "current
    # pack for this workspace" lookup. A session started before a newer
    # pack was confirmed continues to see its own original pack for its
    # entire lifetime (design spec Section 7.2 step 1).
    artifact = get_artifact(conn, scope.pack_artifact_id)
    if artifact is None or artifact["artifact_type"] != "application_pack":
        raise HandoffPackArtifactInvalid(
            f"session's pinned pack artifact {scope.pack_artifact_id!r} "
            "is missing or is not an application_pack"
        )
    candidate_snapshot = artifact["payload"].get("candidate_snapshot")
    if not isinstance(candidate_snapshot, dict):
        raise HandoffPackArtifactInvalid(
            f"pack artifact {scope.pack_artifact_id!r} has no candidate_snapshot"
        )

    projection: dict[str, Any] = {}
    for field_type in normalized_field_types:
        if field_type in projection:
            continue  # duplicate requested type: never re-derived, never re-expanded
        path = _NORMALIZED_FIELD_TYPE_TO_CANDIDATE_PATH.get(field_type)
        if path is None:
            continue  # not in the closed mapping - silently dropped, never an error
        value = _resolve_candidate_path(candidate_snapshot, path)
        if value is not None:
            projection[field_type] = value
    return projection


class HandoffEventRejected(HandoffError):
    pass


_PRESENCE_ONLY_EVENT_TYPES = frozenset({"user_value_present_observed"})


def _require_owned_session(
    conn: sqlite3.Connection, scope: SessionScope, handoff_session_id: str,
) -> dict[str, Any]:
    # A session token is bound to exactly one handoff_session_id — a
    # token minted for session A must never be usable against session
    # B's path, even for the same account, so this checks scope's own
    # bound session id, not merely account ownership (design spec
    # Section 7.1).
    if scope.handoff_session_id != handoff_session_id:
        raise HandoffSessionNotFound(f"handoff session {handoff_session_id!r} not found")
    session = get_handoff_session(conn, handoff_session_id)
    if session is None or session["account_id"] != scope.account_id:
        raise HandoffSessionNotFound(f"handoff session {handoff_session_id!r} not found")
    return session


def record_handoff_event(
    conn: sqlite3.Connection,
    scope: SessionScope,
    *,
    handoff_session_id: str,
    event_id: str,
    event_type: str,
    event_payload: dict[str, Any],
    normalized_field_type: str | None = None,
    page_field_key: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    session = _require_owned_session(conn, scope, handoff_session_id)
    if session["status"] != "in_progress":
        raise HandoffSessionNotActive(
            f"handoff session {handoff_session_id!r} is {session['status']!r}, "
            "not in_progress"
        )
    if event_type in _PRESENCE_ONLY_EVENT_TYPES and "value" in event_payload:
        raise HandoffEventRejected(
            f"{event_type!r} events must never carry a 'value' field "
            "(sensitive-value minimization, design spec Section 11.3)"
        )
    return append_handoff_event(
        conn,
        handoff_session_id=handoff_session_id,
        event_id=event_id,
        event_type=event_type,
        event_payload=event_payload,
        normalized_field_type=normalized_field_type,
        page_field_key=page_field_key,
        observed_at=observed_at,
    )


def replay_handoff_session(
    conn: sqlite3.Connection, scope: SessionScope, handoff_session_id: str,
) -> dict[str, Any]:
    session = _require_owned_session(conn, scope, handoff_session_id)
    return {"session": session, "events": list_handoff_events(conn, handoff_session_id)}


def confirm_handoff_submission(
    conn: sqlite3.Connection,
    scope: SessionScope,
    *,
    handoff_session_id: str,
    mark_workflow_applied: bool = False,
    effective_date: str | None = None,
) -> dict[str, Any]:
    # This is the ONLY function in this module that may call
    # record_status_change or create a submission_confirmations row — it is
    # the terminal, explicit "the user confirmed they actually submitted
    # this application" action for the whole handoff feature (design spec
    # Section 12, Section 18). No other function here may transition a
    # workspace to "applied", and mark_workflow_applied=False must result
    # in zero calls to record_status_change, not merely a no-op transition.
    session = _require_owned_session(conn, scope, handoff_session_id)
    if session["status"] != "in_progress":
        raise HandoffSessionNotActive(
            f"handoff session {handoff_session_id!r} is {session['status']!r}, "
            "not in_progress"
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    workflow_event = None
    if mark_workflow_applied:
        # Reused exactly as-is; this module adds no new path to "applied"
        # and no automatic transition (design spec Section 12, Section 18).
        workflow_event = record_status_change(
            conn,
            workspace_id=session["workspace_id"],
            new_status="applied",
            effective_date=effective_date or now_iso[:10],
            submitted_pack_artifact_id=session["pack_artifact_id"],
            account_id=scope.account_id,
            commit=False,
        )

    updated_session = set_handoff_session_status(
        conn, handoff_session_id, status="user_confirmed_submitted",
        user_confirmed_submitted_at=now_iso, commit=False,
    )
    confirmation = create_submission_confirmation(
        conn,
        handoff_session_id=handoff_session_id,
        workflow_event_id=workflow_event["id"] if workflow_event else None,
        commit=False,
    )
    conn.commit()
    return {
        "session": updated_session,
        "confirmation": confirmation,
        "workflow_event": workflow_event,
    }
