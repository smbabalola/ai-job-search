"""Bundle 7 spec §10.7: the transitional writer lock stays on its allowlisted
sites, and no allowlisted transaction holds it across a provider call."""
from __future__ import annotations

import ast
from pathlib import Path

from webapp.persistence.writer_lock_sites import WRITER_LOCK_SITES

REPO = Path(__file__).resolve().parents[1]
PROVIDER_CALLS = {"propose", "send", "create_checkout", "fetch_subscription", "extract"}


def _begin_immediate_nodes(tree):
    """Calls of the form ``<conn>.execute("BEGIN IMMEDIATE")``."""
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                and " ".join(node.args[0].value.split()).upper().startswith("BEGIN IMMEDIATE")):
            yield node


def _module_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted((REPO / "webapp").rglob("*.py")):
        if path.name in ("dbapi.py", "writer_lock_sites.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        n = sum(1 for _ in _begin_immediate_nodes(tree))
        if n:
            counts[path.relative_to(REPO).as_posix()] = n
    return counts


def test_begin_immediate_sites_match_the_allowlist_exactly():
    assert _module_counts() == WRITER_LOCK_SITES


def test_no_provider_call_inside_an_allowlisted_transaction():
    hits = []
    for module in WRITER_LOCK_SITES:
        tree = ast.parse((REPO / module).read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            begins = sorted(n.lineno for n in _begin_immediate_nodes(func))
            if not begins:
                continue
            calls = sorted(
                (n.lineno, n.func.attr) for n in ast.walk(func)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            )
            for begin in begins:
                ends = [line for line, name in calls if line > begin and name in ("commit", "rollback")]
                end = ends[0] if ends else func.end_lineno
                for line, name in calls:
                    if begin < line < end and name in PROVIDER_CALLS:
                        hits.append(f"{module}:{line}: .{name}() inside BEGIN IMMEDIATE at line {begin}")
    assert hits == []
