"""Route auth classes (Bundle 7 spec A7, §21.1).

Every route declares exactly one class through a router- or route-level
dependency. The classes are not only labels:

* ``USER`` requires a signed-in session whenever auth is enabled, so a route
  that forgot to take the account scope still cannot be reached anonymously;
* ``EXTENSION`` routes authenticate the extension (bearer device tokens from
  Task 11);
* ``PUBLIC`` is reachable by anyone (these routes are rate-limited where
  they act);
* ``ADMIN``, ``WEBHOOK`` and ``METRICS`` are used by later tasks.
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


def _require_session(request: Request) -> None:
    if request.app.state.settings.auth_enabled and not getattr(request.state, "user", None):
        raise ScopeRefused(*SIGN_IN_REQUIRED)


PUBLIC = _marker("PUBLIC")
USER = _marker("USER", _require_session)
EXTENSION = _marker("EXTENSION")
ADMIN = _marker("ADMIN")
WEBHOOK = _marker("WEBHOOK")
METRICS = _marker("METRICS")


def route_class_of(route: Any) -> list[str]:
    return [getattr(d.dependency, "route_class") for d in route.dependencies
            if hasattr(d.dependency, "route_class")]
