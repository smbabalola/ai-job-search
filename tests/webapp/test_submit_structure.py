"""6E-A structural invariants (spec J1, §8.3, acceptance 17), by AST over
webapp/: SUBMIT authority is reachable only through the human path.

- the 6B SUBMIT cores (_pre_click_commit_core, and _request_grant_core with a
  SUBMIT stage) are called only from the human entry points;
- request_human_submit_grant is called only from human_submit.authorize,
  whose body also inserts the human authorization (same transaction);
- the public autonomous entry points raise SubmissionNotAvailable before
  doing anything when asked for SUBMIT."""
from __future__ import annotations

import ast
from pathlib import Path

WEBAPP = Path(__file__).parents[2] / "webapp"

_EXT = "/api/handoff/sessions/{session_id}/fill/runs/{run_id}/submit"
_APP = "/api/workspaces/{workspace_id}/submit"
# The complete 6E-A human submit route inventory (spec §18). Every other
# route containing "submit" is the pre-existing Phase 3 confirm-submission.
SUBMIT_ROUTES_6E_A = frozenset({
    f"{_EXT}/observations", f"{_EXT}/pre-click", f"{_EXT}/{{attempt_id}}/dispatch", f"{_EXT}/{{attempt_id}}/events",
    f"{_EXT}/{{attempt_id}}/result", f"{_EXT}/{{attempt_id}}/cancel",
    f"{_APP}/state", f"{_APP}/authorize", f"{_APP}/cancel", f"{_APP}/attempts/{{attempt_id}}/resolve",
    "/workspaces/{workspace_id}/submit",
})
CONFIRM_SUBMISSION = "/api/handoff/sessions/{session_id}/confirm-submission"


def _functions():
    """(module, enclosing top-level function, called name) for every call."""
    out = []
    for path in WEBAPP.rglob("*.py"):
        module = path.relative_to(WEBAPP.parent).with_suffix("").as_posix().replace("/", ".")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for top in tree.body:
            if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(top):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                    if name:
                        out.append((module, top.name, name, node))
    return out


CALLS = _functions()


def callers(name):
    return {(m, f) for m, f, n, _ in CALLS if n == name}


def test_the_pre_click_core_is_called_only_from_the_human_pre_click():
    assert callers("_pre_click_commit_core") == {("webapp.services.human_submit", "human_pre_click_commit")}


def test_the_grant_core_is_called_only_from_request_grant_and_the_human_grant():
    assert callers("_request_grant_core") == {("webapp.services.autonomy", "request_grant"),
                                              ("webapp.services.autonomy", "request_human_submit_grant")}


def test_the_human_grant_is_issued_only_by_authorize_which_inserts_the_authorization():
    assert callers("request_human_submit_grant") == {("webapp.services.human_submit", "authorize")}
    assert ("webapp.services.human_submit", "authorize") in callers("insert_authorization")


def _function(module_path: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((WEBAPP.parent / module_path).read_text(encoding="utf-8"))
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


def test_request_grant_refuses_submit_before_anything_else():
    first = _function("webapp/services/autonomy.py", "request_grant").body[1]  # [0] is the docstring
    assert isinstance(first, ast.If) and "SUBMIT" in ast.unparse(first.test)
    assert isinstance(first.body[0], ast.Raise) and "SubmissionNotAvailable" in ast.unparse(first.body[0])


def test_the_public_pre_click_commit_is_an_unconditional_refusal():
    body = [n for n in _function("webapp/services/autonomy.py", "pre_click_commit").body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    assert len(body) == 1 and isinstance(body[0], ast.Raise) and "SubmissionNotAvailable" in ast.unparse(body[0])


def test_the_human_pre_click_passes_human_authority_and_the_human_intent_source():
    call = next(node for m, f, n, node in CALLS if n == "_pre_click_commit_core")
    kwargs = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert kwargs["authority"] == "AuthorityKind.HUMAN_SUBMIT" and kwargs["intent_source"] == "'HUMAN_AUTHORIZED'"


def test_submit_routes_reach_submit_only_through_human_submit():
    for route_module in ("webapp.api.submit_extension", "webapp.api.submit_app"):
        called = {n for m, f, n, _ in CALLS if m == route_module}
        assert not called & {"_pre_click_commit_core", "_request_grant_core", "request_grant",
                             "record_click_dispatched", "record_submission_result", "resolve_ambiguous"}
