"""Bundle 6D-B structural boundaries (spec §17, §20): the fill modules never
reach SUBMIT authority, no route path submits (except the pre-existing
handoff confirm-submission), and nothing lifts a quarantine."""
from __future__ import annotations

from tests.webapp.route_inventory import all_routes
import ast
from pathlib import Path

import pytest

from webapp.app import create_app
from webapp.config import Settings

ROOT = Path(__file__).resolve().parents[2]
FILL_MODULES = sorted([*(ROOT / "webapp" / "services").glob("fill_*.py"), *(ROOT / "webapp" / "api").glob("fill_*.py"),
                       *(ROOT / "product").glob("fill_*.py"), ROOT / "webapp" / "persistence" / "fill.py"])
FORBIDDEN = {"pre_click_commit", "_pre_click_commit_core", "_request_grant_core"}


def test_the_fill_module_set_is_what_we_think():
    names = {p.name for p in FILL_MODULES}
    assert {"fill_runs.py", "fill_actions.py", "fill_plans.py", "fill_extension.py", "fill_app.py",
            "fill_plan.py", "fill.py"} <= names


@pytest.mark.parametrize("path", FILL_MODULES, ids=lambda p: p.name)
def test_fill_modules_never_reach_submit_authority(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else \
            node.name.rsplit(".", 1)[-1] if isinstance(node, ast.alias) else None
        assert name not in FORBIDDEN, f"{path.name} references {name}"
        if isinstance(node, ast.Attribute) and node.attr == "SUBMIT" and isinstance(node.value, ast.Name):
            assert node.value.id != "Capability", f"{path.name} uses Capability.SUBMIT"


def _routes(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "s.sqlite3"))
    return [(sorted(r.methods), r.path) for r in all_routes(app)]


def test_no_route_path_submits_except_the_confirmation_and_the_6e_a_human_routes(tmp_path):
    from tests.webapp.test_submit_structure import SUBMIT_ROUTES_6E_A
    paths = {p for _, p in _routes(tmp_path)}
    assert SUBMIT_ROUTES_6E_A <= paths, "the inventory sees the 6E-A routes (never vacuous)"
    offenders = [p for p in sorted(paths) if "submit" in p.lower() and not p.endswith("/confirm-submission")
                 and p not in SUBMIT_ROUTES_6E_A]
    assert offenders == []


def test_no_fill_route_removes_or_lifts_a_quarantine(tmp_path):
    fill = [(m, p) for m, p in _routes(tmp_path) if "/fill" in p]
    assert fill, "the fill routes are registered"
    assert not [p for m, p in fill if "DELETE" in m]
    assert not [p for _, p in fill if any(w in p.lower() for w in ("release", "lift", "unquarantine", "remove"))]
    from product.fill_vocab import QUARANTINE_PHASES
    assert not [q for q in QUARANTINE_PHASES if any(w in q for w in ("RELEASE", "LIFT", "REMOVE"))]
