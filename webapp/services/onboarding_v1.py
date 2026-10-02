"""Guided onboarding (Bundle 7 spec §15.2).

``onboarding.v1`` is mutable UI progress, not evidence: each step is TODO,
DONE or SKIPPED; every step except "about" can be skipped and resumed from
the dashboard checklist. Readiness is what the product actually needs, read
from the real data (not from the checklist): ai.prepare requires a verified
email, a candidate name, an active CV, a default CV rule that resolves and a
target role; a fill needs a paired extension.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from webapp.persistence import dbapi
from webapp.persistence.bundle7_migrations import ONBOARDING_STEP_IDS

__all__ = ["ONBOARDING_STEPS", "OPTIONAL_STEPS", "OnboardingIncomplete", "Readiness", "mark_step",
           "onboarding_state", "readiness", "require_prepare_ready"]

ONBOARDING_STEPS = ONBOARDING_STEP_IDS
OPTIONAL_STEPS = frozenset({"import", "rules"})
STATES = ("TODO", "DONE", "SKIPPED")
VERSION = "onboarding.v1"
STEP_FOR_MISSING = {"email_verified": None, "identity_name": "about", "cv": "cv", "default_cv_rule": "cv",
                    "target_role": "preferences", "extension_device": "extension"}


class OnboardingIncomplete(Exception):
    def __init__(self, missing: list[str]):
        super().__init__("onboarding incomplete: " + ", ".join(missing))
        self.missing = missing


@dataclass(frozen=True)
class Readiness:
    prepare_ok: bool
    prepare_missing: list[str] = field(default_factory=list)
    fill_ok: bool = False
    fill_missing: list[str] = field(default_factory=list)


def ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def onboarding_state(conn: dbapi.Connection, scope: Any) -> dict[str, Any]:
    row = conn.execute("SELECT state_json FROM account_onboarding WHERE account_id = ?", (scope.account_id,)).fetchone()
    steps = {step: "TODO" for step in ONBOARDING_STEPS}
    if row is not None:
        steps.update({k: v for k, v in json.loads(row[0]).get("steps", {}).items() if k in steps and v in STATES})
    required_done = all(steps[s] in ("DONE", "SKIPPED") for s in ONBOARDING_STEPS if s not in OPTIONAL_STEPS)
    next_step = next((s for s in ONBOARDING_STEPS if steps[s] == "TODO"), None)
    return {"version": VERSION, "steps": steps, "complete": required_done, "next_step": next_step}


def mark_step(conn: dbapi.Connection, scope: Any, step: str, action: str, *, now: datetime) -> dict[str, Any]:
    """No commit. The first step ("about") cannot be skipped."""
    if step not in ONBOARDING_STEPS or action not in STATES:
        raise ValueError("unknown onboarding step or state")
    if step == "about" and action == "SKIPPED":
        raise ValueError("the first step cannot be skipped")
    steps = onboarding_state(conn, scope)["steps"]
    steps[step] = action
    state = json.dumps({"version": VERSION, "steps": steps}, sort_keys=True)
    conn.execute("INSERT INTO account_onboarding (account_id, state_json, updated_at) VALUES (?, ?, ?) "
                 "ON CONFLICT (account_id) DO UPDATE SET state_json = excluded.state_json, "
                 "updated_at = excluded.updated_at", (scope.account_id, state, ts(now)))
    return onboarding_state(conn, scope)


# ---- readiness --------------------------------------------------------------------------------

def _email_verified(conn: dbapi.Connection, scope: Any) -> bool:
    user_id = getattr(scope, "user_id", None)
    if user_id is None:
        return True  # the local operator
    row = conn.execute("SELECT email_verified_at FROM users WHERE id = ?", (user_id,)).fetchone()
    return row is not None and row[0] is not None


def identity_name_present(conn: dbapi.Connection, scope: Any) -> bool:
    from product.profile_snapshot import build_snapshot
    from webapp.persistence.profile_sources import included_profile_sources
    from webapp.services.pipeline import get_current_profile_snapshot
    from webapp.services.profile_setup import profile_snapshot_is_ready
    from webapp.storage.profile_sources import as_profile_sources
    if profile_snapshot_is_ready(get_current_profile_snapshot(conn, account_id=scope.account_id)):
        return True
    try:
        snapshot = build_snapshot(as_profile_sources(scope.profile_sources(conn)).reader(),
                                  included_sources=included_profile_sources(conn, account_id=scope.account_id))
    except Exception:  # noqa: BLE001 - no readable profile source is "missing", not an error
        return False
    return profile_snapshot_is_ready({"payload": snapshot})


def _has_active_cv(conn: dbapi.Connection, account_id: str) -> bool:
    from webapp.persistence import cv_library as rows
    return any(item["status"] == "ACTIVE" and rows.latest_visible_version(conn, account_id=account_id,
                                                                           item_id=item["id"])
               for item in rows.list_items(conn, account_id=account_id))


def _default_rule_resolves(conn: dbapi.Connection, account_id: str) -> bool:
    from webapp.persistence import cv_library as rows
    from webapp.services.cv_strategy import current_cv_strategy
    rule = current_cv_strategy(conn, account_id)[0]["default"]
    if rule["mode"] == "FIXED_VERSION":
        version = rows.get_version(conn, account_id=account_id, version_id=rule["version_id"])
        item = rows.get_item(conn, account_id=account_id, item_id=version["item_id"]) if version else None
        return item is not None and item["status"] == "ACTIVE"
    item = rows.get_item(conn, account_id=account_id, item_id=rule["item_id"]) if rule.get("item_id") else None
    return item is not None and item["status"] == "ACTIVE" and \
        rows.latest_visible_version(conn, account_id=account_id, item_id=item["id"]) is not None


def _has_target_role(conn: dbapi.Connection, account_id: str) -> bool:
    from webapp.persistence.search_workspaces import list_search_workspaces
    from webapp.persistence.user_profile import get_current_user_profile
    for workspace in list_search_workspaces(conn, account_id=account_id):
        if workspace["status"] != "active":
            continue
        profile = get_current_user_profile(conn, workspace["id"], account_id=account_id)
        if profile and (profile["payload"].get("target_roles") or []):
            return True
    return False


def _has_device(conn: dbapi.Connection, account_id: str) -> bool:
    return conn.execute("SELECT 1 FROM extension_devices WHERE account_id = ? AND revoked_at IS NULL LIMIT 1",
                        (account_id,)).fetchone() is not None


def readiness(conn: dbapi.Connection, scope: Any, *, now: datetime) -> Readiness:
    checks = [("email_verified", lambda: _email_verified(conn, scope)),
              ("identity_name", lambda: identity_name_present(conn, scope)),
              ("cv", lambda: _has_active_cv(conn, scope.account_id)),
              ("default_cv_rule", lambda: _default_rule_resolves(conn, scope.account_id)),
              ("target_role", lambda: _has_target_role(conn, scope.account_id))]
    missing = [name for name, check in checks if not check()]
    fill_missing = [] if _has_device(conn, scope.account_id) else ["extension_device"]
    return Readiness(prepare_ok=not missing, prepare_missing=missing, fill_ok=not fill_missing,
                     fill_missing=fill_missing)


def require_prepare_ready(conn: dbapi.Connection, scope: Any, *, now: datetime) -> None:
    """Called by every ai.prepare entry point (after the entitlement check)."""
    ready = readiness(conn, scope, now=now)
    if not ready.prepare_ok:
        raise OnboardingIncomplete(ready.prepare_missing)
