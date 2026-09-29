"""Bundle 7 spec H9: account and search-workspace scope is always explicit.
A missed argument must be a TypeError, never a silent fall-back to the local
account."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "webapp"
FORBIDDEN = {"DEFAULT_ACCOUNT_ID", "DEFAULT_SEARCH_WORKSPACE_ID"}


def test_no_parameter_defaults_to_the_local_account_or_search_workspace():
    hits = []
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
            for default in defaults:
                if isinstance(default, ast.Name) and default.id in FORBIDDEN:
                    hits.append(f"{path.relative_to(ROOT.parent)}:{node.lineno} {node.name}() defaults to {default.id}")
    assert hits == []
