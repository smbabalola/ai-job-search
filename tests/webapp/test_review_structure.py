"""Bundle 6D-A structural boundaries (spec §12 G3, Tasks 11 and 13)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = sorted(p for d in ("webapp", "product") for p in (ROOT / d).rglob("*.py"))
AUTONOMY = ROOT / "webapp" / "services" / "autonomy.py"


def _name_uses(tree: ast.AST, name: str) -> list[ast.AST]:
    return [n for n in ast.walk(tree)
            if (isinstance(n, ast.Name) and n.id == name) or (isinstance(n, ast.Attribute) and n.attr == name)
            or (isinstance(n, ast.alias) and name in (n.name.rsplit(".", 1)[-1], n.asname))]


def _enclosing_function(tree: ast.AST, node: ast.AST) -> str | None:
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and any(child is node for child in ast.walk(fn)):
            return fn.name
    return None


def test_private_pre_click_core_has_no_production_caller():
    for path in PRODUCTION:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        uses = _name_uses(tree, "_pre_click_commit_core")
        assert uses == [], f"{path.relative_to(ROOT)} references _pre_click_commit_core"


def test_private_grant_core_is_reached_only_through_the_public_fill_wrapper():
    for path in PRODUCTION:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        uses = _name_uses(tree, "_request_grant_core")
        if path != AUTONOMY:
            assert uses == [], f"{path.relative_to(ROOT)} references _request_grant_core"
            continue
        assert {_enclosing_function(tree, u) for u in uses} == {"request_grant"}
    wrapper = next(n for n in ast.walk(ast.parse(AUTONOMY.read_text(encoding="utf-8")))
                   if isinstance(n, ast.FunctionDef) and n.name == "request_grant")
    first = wrapper.body[1] if isinstance(wrapper.body[0], ast.Expr) else wrapper.body[0]  # after the docstring
    assert isinstance(first, ast.If) and "SUBMIT" in ast.unparse(first.test) and isinstance(first.body[0], ast.Raise)
