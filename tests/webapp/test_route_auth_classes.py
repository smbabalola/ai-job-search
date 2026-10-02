"""Bundle 7 spec A7: every route declares exactly one auth class."""
from __future__ import annotations

from tests.webapp.route_inventory import ROUTE_CLASSES, api_routes, assert_inventory_complete, class_counts
from webapp.app import create_app
from webapp.config import Settings


def test_the_route_inventory_is_complete_and_every_class_is_present(tmp_path):
    """Release pass: the invariants below read the effective inventory; it must
    be the whole application (OpenAPI cross-check, per-class floors)."""
    app = create_app(Settings(db_path=tmp_path / "db.sqlite3"))
    routes = assert_inventory_complete(app)
    assert set(class_counts(routes)) == set(ROUTE_CLASSES), class_counts(routes)


def test_every_route_declares_exactly_one_auth_class(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "db.sqlite3"))
    offenders = []
    for route in assert_inventory_complete(app):  # API routes only: static mounts and docs have no class
        if len(route.route_class) != 1:
            offenders.append(f"{sorted(route.methods)} {route.path}: {list(route.route_class)}")
    assert offenders == [], "\n".join(offenders)


def test_known_routes_have_the_expected_class(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "db.sqlite3"))
    by_path = {(route.path, route.methods): list(route.route_class) for route in assert_inventory_complete(app)}
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


def test_the_inventory_guard_fails_loudly_on_an_empty_reduced_or_partial_inventory(tmp_path):
    """Release pass: an enumeration that stops seeing routes must fail, never pass vacuously."""
    import pytest

    app = create_app(Settings(db_path=tmp_path / "db.sqlite3"))
    routes = api_routes(app)
    with pytest.raises(AssertionError, match="empty"):
        assert_inventory_complete(app, [])
    one_admin_fewer = [r for r in routes if r.route_class != ("ADMIN",)] + \
        [r for r in routes if r.route_class == ("ADMIN",)][1:]
    with pytest.raises(AssertionError, match="OpenAPI operations absent|below their floor"):
        assert_inventory_complete(app, one_admin_fewer)
    from tests.webapp.route_inventory import CLASS_FLOORS
    with pytest.raises(AssertionError, match="below their floor"):  # the floor alone, on the full inventory
        assert_inventory_complete(app, routes, floors={**CLASS_FLOORS, "ADMIN": class_counts(routes)["ADMIN"] + 1})
    without_metrics = [r for r in routes if r.route_class != ("METRICS",)]
    with pytest.raises(AssertionError, match="OpenAPI operations absent|below their floor"):
        assert_inventory_complete(app, without_metrics)
    published_route = next(r for r in routes if r.path == "/api/search-workspaces" and "GET" in r.methods)
    with pytest.raises(AssertionError, match="OpenAPI operations absent"):
        assert_inventory_complete(app, [r for r in routes if r is not published_route], floors={})
