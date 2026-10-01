"""Route auth classes (Bundle 7 spec A7, §21.1).

Every route declares exactly one class through a router- or route-level
dependency. The classes are not only labels:

* ``USER`` requires a signed-in session whenever auth is enabled, so a route
  that forgot to take the account scope still cannot be reached anonymously;
* ``EXTENSION`` routes authenticate the extension (bearer device tokens from
  Task 11);
* ``PUBLIC`` is reachable by anyone (these routes are rate-limited where
  they act);
* ``ADMIN`` requires a STAFF session (Task 27); ``WEBHOOK`` and ``METRICS``
  are used by later tasks.
"""
from __future__ import annotations

from typing import Any

from fastapi import Request


class ScopeRefused(Exception):
    """A refusal with a stable code (spec §21.3); rendered as JSON for API
    callers and as a redirect for page requests."""

    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.message = message


SIGN_IN_REQUIRED = ("SIGN_IN_REQUIRED", 401, "Please sign in.")


def _marker(name: str, check=None):
    async def dependency(request: Request) -> None:
        if check is not None:
            check(request)

    dependency.route_class = name  # type: ignore[attr-defined]
    dependency.__name__ = f"route_class_{name.lower()}"
    return dependency


STAFF_SIGN_IN_REQUIRED = ("STAFF_SIGN_IN_REQUIRED", 401, "Please sign in to the staff console.")
PERMISSION_DENIED = ("PERMISSION_DENIED", 403, "You don't have access to this.")


def _session_kind(request: Request) -> str | None:
    session = getattr(request.state, "session", None)
    return session["kind"] if session else None


def _require_session(request: Request) -> None:
    if not request.app.state.settings.auth_enabled:
        return
    if not getattr(request.state, "user", None):
        raise ScopeRefused(*SIGN_IN_REQUIRED)
    if _session_kind(request) != "CUSTOMER":  # spec §7: a staff session is never accepted on USER routes
        raise ScopeRefused(*PERMISSION_DENIED)


def _require_staff_session(request: Request) -> None:
    """Spec §7: ADMIN routes take a STAFF session only (created after TOTP);
    a customer session is refused. Without auth there is no staff console."""
    if not request.app.state.settings.auth_enabled or not getattr(request.state, "user", None):
        raise ScopeRefused(*STAFF_SIGN_IN_REQUIRED)
    if _session_kind(request) != "STAFF":
        raise ScopeRefused(*PERMISSION_DENIED)


PUBLIC = _marker("PUBLIC")
USER = _marker("USER", _require_session)
EXTENSION = _marker("EXTENSION")
ADMIN = _marker("ADMIN", _require_staff_session)
WEBHOOK = _marker("WEBHOOK")
METRICS = _marker("METRICS")


def route_class_of(route: Any) -> list[str]:
    return [getattr(d.dependency, "route_class") for d in route.dependencies
            if hasattr(d.dependency, "route_class")]
