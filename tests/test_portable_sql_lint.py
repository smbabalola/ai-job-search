"""Bundle 7 spec §10.2: webapp SQL stays in the subset both SQLite and PostgreSQL run."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "webapp"
EXCLUDED = {
    ROOT / "persistence" / "migrations.py",
    ROOT / "persistence" / "db.py",
    ROOT / "persistence" / "dbapi.py",
}
SQL_PATTERNS = {
    "INSERT OR": re.compile(r"\bINSERT\s+OR\b", re.IGNORECASE),
    "json_extract": re.compile(r"\bjson_extract\s*\(", re.IGNORECASE),
    "json_each": re.compile(r"\bjson_each\s*\(", re.IGNORECASE),
    "strftime(": re.compile(r"\bstrftime\s*\(\s*'", re.IGNORECASE),  # SQL form: strftime('%...
    "julianday(": re.compile(r"\bjulianday\s*\(", re.IGNORECASE),
    "datetime('now'": re.compile(r"\bdatetime\s*\(\s*'now'", re.IGNORECASE),
    "AUTOINCREMENT": re.compile(r"\bAUTOINCREMENT\b", re.IGNORECASE),
    "GLOB": re.compile(r"\bGLOB\b"),
}
NAMED_PARAM = re.compile(r"(?<![:\w]):[a-z_][a-z0-9_]*\b")
BANNED_ATTRIBUTES = {"lastrowid", "executescript", "total_changes"}


def _files():
    for path in sorted(ROOT.rglob("*.py")):
        if path not in EXCLUDED and "persistence/pg" not in path.as_posix():
            yield path


def _string_constants(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node


def test_no_sqlite_only_sql_outside_migrations():
    hits = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _string_constants(tree):
            for name, pattern in SQL_PATTERNS.items():
                if pattern.search(node.value):
                    hits.append(f"{path.relative_to(ROOT.parent)}:{node.lineno}: {name}")
    assert hits == []


def test_no_sqlite_only_connection_attributes():
    hits = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRIBUTES:
                hits.append(f"{path.relative_to(ROOT.parent)}:{node.lineno}: .{node.attr}")
    assert hits == []


def test_no_named_parameters_in_execute_literals():
    hits = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("execute", "executemany") and node.args):
                for const in _string_constants(node.args[0]):
                    if NAMED_PARAM.search(const.value.replace("::", "")):
                        hits.append(f"{path.relative_to(ROOT.parent)}:{const.lineno}")
    assert hits == []
