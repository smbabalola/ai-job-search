"""The application's effective route inventory, for every route-security invariant.

Newer FastAPI keeps each included router as one lazy ``_IncludedRouter`` entry in
``app.routes``; the routes it actually serves (prefixed, with the router's and the
include's dependencies) exist only as effective route contexts. A test that walks
``app.routes`` directly then sees no API routes at all and its invariant passes
vacuously. Every route-security test reads the inventory through here instead, and
``assert_inventory_complete`` refuses an inventory that is empty, smaller than
expected, or missing an operation FastAPI itself serves.
"""
from __future__ import annotations

import collections
import re
from dataclasses import dataclass
from typing import Any

from fastapi.routing import APIRoute

from webapp.api.route_classes import route_class_of

try:
    from fastapi.routing import iter_route_contexts
except ImportError:  # older FastAPI: app.routes is already the flat effective list
    iter_route_contexts = None

ROUTE_CLASSES = ("PUBLIC", "USER", "EXTENSION", "ADMIN", "WEBHOOK", "METRICS")

# The local-mode inventory at the release pass (2026-10-02, FastAPI 0.115 and
# 0.142 enumerate it identically). A class may grow; a class that shrinks, or
# routes lost to an enumeration change, fails until this is edited on purpose.
CLASS_FLOORS = {"USER": 205, "ADMIN": 38, "PUBLIC": 28, "EXTENSION": 27, "WEBHOOK": 2, "METRICS": 1}


_CONVERTOR = re.compile(r"\{(\w+):[^}]+\}")  # "{p:path}" is published in OpenAPI as "{p}"


@dataclass(frozen=True)
class Route:
    path: str
    methods: frozenset[str]
    is_api: bool
    route_class: tuple[str, ...]  # the declared auth classes (exactly one when the A7 invariant holds)
    dependency_calls: frozenset[Any]  # the endpoint's direct dependencies, router-level ones included
    effective: Any  # the effective route (or route context), for anything else a test needs


def all_routes(app) -> list[Route]:
    """Every route the application serves: API routes as served (full path,
    effective dependencies) plus mounts, docs and other plain routes."""
    entries = list(iter_route_contexts(app.router.routes)) if iter_route_contexts else list(app.routes)
    routes = []
    for entry in entries:
        original = getattr(entry, "original_route", entry)
        is_api = isinstance(original, APIRoute)
        dependant = getattr(entry, "dependant", None) if is_api else None
        routes.append(Route(
            path=entry.path,
            methods=frozenset(getattr(entry, "methods", None) or ()),
            is_api=is_api,
            route_class=tuple(route_class_of(entry)) if is_api else (),
            dependency_calls=frozenset(d.call for d in dependant.dependencies) if dependant else frozenset(),
            effective=entry,
        ))
    return routes


def api_routes(app) -> list[Route]:
    return [route for route in all_routes(app) if route.is_api]


def class_counts(routes: list[Route]) -> dict[str, int]:
    return dict(collections.Counter(route.route_class[0] if len(route.route_class) == 1 else "UNCLASSIFIED"
                                    for route in routes if route.is_api))


def assert_inventory_complete(app, routes: list[Route] | None = None, *, floors: dict[str, int] = CLASS_FLOORS):
    """Loud failure instead of a vacuous pass: the inventory is non-empty, holds
    every operation FastAPI publishes in its OpenAPI document (an independent
    traversal), and no route class is below its floor."""
    routes = api_routes(app) if routes is None else [route for route in routes if route.is_api]
    assert routes, "the route inventory is empty: the enumeration no longer sees the application's routes"
    served = {(method, _CONVERTOR.sub(r"{\1}", route.path)) for route in routes for method in route.methods}
    published = {(method.upper(), path) for path, operations in app.openapi()["paths"].items()
                 for method in operations}
    missing = sorted(published - served)
    assert not missing, f"OpenAPI operations absent from the route inventory: {missing[:10]}"
    counts = class_counts(routes)
    short = {klass: (counts.get(klass, 0), floor) for klass, floor in floors.items() if counts.get(klass, 0) < floor}
    assert not short, f"route classes below their floor (found, floor): {short}"
    return routes
