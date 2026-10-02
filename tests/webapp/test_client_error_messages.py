"""Client-side refusal messages (Bundle 7 release pass).

Legacy refusals carry a string ``detail``; the §21.3 contract carries a human
``message`` and an object ``detail``. A page that shows ``body.detail`` as text
shows "[object Object]" for every §21.3 refusal (allowance exhausted, feature
not in plan, document rejected, ...). Every client-side read of ``.detail`` as a
message must therefore check that it is a string first.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
CLIENT_SOURCES = sorted([*(ROOT / "webapp" / "static").glob("*.js"),
                         *(ROOT / "webapp" / "templates").rglob("*.html")])
DETAIL_AS_TEXT = re.compile(r"(\(await r\.json\(\)\)|\b\w+)\.detail\s*\|\|")


def unguarded_detail_reads(text: str) -> list[str]:
    offenders = []
    for match in DETAIL_AS_TEXT.finditer(text):
        guard = f'typeof {match.group(1)}.detail === "string" && '
        if not text[:match.start()].endswith(guard):
            offenders.append(match.group(0))
    return offenders


def test_the_guard_catches_an_unguarded_read_and_accepts_a_guarded_one():
    assert unguarded_detail_reads('throw new Error(body.detail || "Request failed");')
    assert unguarded_detail_reads('window.alert((await r.json()).detail || "Failed");')
    assert not unguarded_detail_reads('(typeof b.detail === "string" && b.detail) || b.message || "Failed"')


def test_no_client_script_shows_an_object_detail_as_text():
    offenders = {path.relative_to(ROOT).as_posix(): found for path in CLIENT_SOURCES
                 if (found := unguarded_detail_reads(path.read_text(encoding="utf-8")))}
    assert offenders == {}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run app.js's errorMessage")
def test_app_js_error_message_shows_the_human_message_for_both_contracts():
    source = (ROOT / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    function = re.search(r"function errorMessage\(body, fallback\) \{.*?\n\}", source, re.S).group(0)
    bodies = [
        {"detail": "workspace not found"},  # legacy HTTPException
        {"error": "ALLOWANCE_EXHAUSTED", "message": "You've used your plan's allowance.",
         "detail": {"allowance": "applications.prepare", "upgrade_to": "pro"}},  # §21.3
        {"detail": [{"loc": ["body"], "msg": "field required"}]},  # validation list
        {},
    ]
    script = f"{function}\nconsole.log(JSON.stringify({json.dumps(bodies)}.map(b => errorMessage(b, 'Request failed'))));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == ["workspace not found", "You've used your plan's allowance.",
                               "Request failed", "Request failed"]
