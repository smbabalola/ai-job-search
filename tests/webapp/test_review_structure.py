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


def test_private_pre_click_core_is_reached_only_through_the_human_pre_click():
    # 6E-A: the one caller is human_submit.human_pre_click_commit (HUMAN_SUBMIT
    # authority); the autonomous pre_click_commit stays an unconditional refusal.
    human = ROOT / "webapp" / "services" / "human_submit.py"
    for path in PRODUCTION:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        uses = _name_uses(tree, "_pre_click_commit_core")
        if path == human:
            assert {_enclosing_function(tree, u) for u in uses} == {"human_pre_click_commit"}
            continue
        assert uses == [], f"{path.relative_to(ROOT)} references _pre_click_commit_core"


def test_private_grant_core_is_reached_only_through_the_public_fill_wrapper():
    for path in PRODUCTION:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        uses = _name_uses(tree, "_request_grant_core")
        if path != AUTONOMY:
            assert uses == [], f"{path.relative_to(ROOT)} references _request_grant_core"
            continue
        # 6E-A adds exactly one more: the human SUBMIT grant entry point.
        assert {_enclosing_function(tree, u) for u in uses} == {"request_grant", "request_human_submit_grant"}
    wrapper = next(n for n in ast.walk(ast.parse(AUTONOMY.read_text(encoding="utf-8")))
                   if isinstance(n, ast.FunctionDef) and n.name == "request_grant")
    first = wrapper.body[1] if isinstance(wrapper.body[0], ast.Expr) else wrapper.body[0]  # after the docstring
    assert isinstance(first, ast.If) and "SUBMIT" in ast.unparse(first.test) and isinstance(first.body[0], ast.Raise)


REVIEW_MODULES = [
    "product/review_contract.py", "webapp/persistence/review_approval.py", "webapp/services/review_fields.py",
    "webapp/services/review_application.py", "webapp/services/review_documents.py",
    "webapp/services/review_approval.py", "webapp/services/review_answers.py", "webapp/api/review_approval.py",
    "webapp/api/applications.py",
]
AUTOMATION_MODULES = [
    "webapp/services/autonomy_scheduler.py", "webapp/services/autonomy_prepare.py",
    "webapp/services/autonomy_candidates.py", "webapp/services/autonomy_inbox.py",
    "webapp/services/autonomy_prepare_auth.py", "webapp/autonomy_worker.py", "webapp/services/pipeline.py",
    "webapp/services/discovery.py",
]


def _names(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.alias):
            out.add(n.name.rsplit(".", 1)[-1])
    return out


@pytest.mark.parametrize("module", REVIEW_MODULES)
def test_review_modules_never_reach_submission_authority(module):
    assert not (_names(module) & {"pre_click_commit", "request_grant", "_pre_click_commit_core",
                                  "_request_grant_core"}), module


def test_the_pure_review_contract_imports_no_webapp():
    tree = ast.parse((ROOT / "product/review_contract.py").read_text(encoding="utf-8"))
    modules = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module] + \
        [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not [m for m in modules if m == "webapp" or m.startswith("webapp.")]


@pytest.mark.parametrize("module", REVIEW_MODULES)
def test_review_modules_never_order_by_created_at(module):
    source = (ROOT / module).read_text(encoding="utf-8")
    assert not re.search(r"ORDER BY[^\"'\n]*created_at", source, re.IGNORECASE), module


@pytest.mark.parametrize("module", AUTOMATION_MODULES)
def test_automation_never_moves_review_document_selections(module):
    assert not (_names(module) & {"select_application_document", "apply_selection", "set_selection", "save_changes",
                                  "replace_document", "select_document"}), module
