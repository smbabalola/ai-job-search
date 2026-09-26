"""Bundle 6C structural boundaries (spec §2, §15): PREPARE code never reaches
the FILL/SUBMIT machinery, the pure layer never imports the web layer, and
new code never orders by created_at (append-only seq is the order)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

PREPARE_MODULES = [
    "webapp/services/autonomy_candidates.py",
    "webapp/services/autonomy_prepare.py",
    "webapp/services/autonomy_scheduler.py",
    "webapp/services/autonomy_inbox.py",
    "webapp/services/autonomy_prepare_auth.py",
    "webapp/autonomy_worker.py",
    "webapp/services/autonomy_fence.py",
]
NEW_MODULES = PREPARE_MODULES + [
    "webapp/services/autonomy_providers.py",
    "webapp/persistence/autonomy_prepare.py",
    "product/prepare_steps.py",
    "product/candidate_promotion.py",
]
FORBIDDEN = {"request_grant", "pre_click_commit"}


def _names(tree: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                found.add(node.asname)
    return found


@pytest.mark.parametrize("module", PREPARE_MODULES)
def test_prepare_modules_never_reach_fill_or_submit(module):
    tree = ast.parse((ROOT / module).read_text(encoding="utf-8"))
    assert not (_names(tree) & FORBIDDEN), module


def test_product_never_imports_webapp():
    offenders = []
    for path in sorted((ROOT / "product").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            if any(m == "webapp" or m.startswith("webapp.") for m in modules):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


@pytest.mark.parametrize("module", NEW_MODULES)
def test_new_modules_never_order_by_created_at(module):
    source = (ROOT / module).read_text(encoding="utf-8")
    assert not re.search(r"ORDER BY[^\"'\n]*created_at", source, re.IGNORECASE), module
