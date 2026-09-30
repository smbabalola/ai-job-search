"""Bundle 7 spec A7: every route declares exactly one auth class."""
from __future__ import annotations

from fastapi.routing import APIRoute

from webapp.api.route_classes import route_class_of
from webapp.app import create_app
from webapp.config import Settings


def test_every_route_declares_exactly_one_auth_class(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "db.sqlite3"))
    offenders = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue  # static mounts and the OpenAPI/docs routes
        classes = route_class_of(route)
        if len(classes) != 1:
            offenders.append(f"{sorted(route.methods)} {route.path}: {classes}")
    assert offenders == [], "\n".join(offenders)


def test_known_routes_have_the_expected_class(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "db.sqlite3"))
    by_path = {(route.path, frozenset(route.methods)): route_class_of(route)
               for route in app.routes if isinstance(route, APIRoute)}
    expect = {
        ("/health", "GET"): "PUBLIC", ("/auth/login", "POST"): "PUBLIC", ("/signup", "GET"): "PUBLIC",
        ("/auth/me", "GET"): "USER", ("/settings/password", "POST"): "USER",
        ("/api/search-workspaces", "GET"): "USER", ("/", "GET"): "USER",
        ("/api/handoff/sessions", "POST"): "EXTENSION", ("/api/ext/pairing-codes", "POST"): "USER",
        ("/api/ext/pair", "POST"): "PUBLIC", ("/api/ext/whoami", "GET"): "EXTENSION",
    }
    for (path, method), klass in expect.items():
        matches = [classes for (p, methods), classes in by_path.items() if p == path and method in methods]
        assert matches and matches[0] == [klass], (path, method, matches)
