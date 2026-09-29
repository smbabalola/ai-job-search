"""Bundle 6D-B Task 1 spikes (spec §22 S1, S3). These are experiments, not
product tests: they answer, against real React- and Vue-controlled pages in
the packed extension's ISOLATED world, the two questions the design depends
on.

S1 (a gate): can an ISOLATED-world script place a File in an
<input type=file> through DataTransfer so that the page sees it, and then
read the exact bytes back and hash them?

S3: do ISOLATED-realm prototype setters plus input/change (never click)
register in controlled text, select, checkbox and radio fields? The S3
table is recorded, not asserted, because a click-only kind is simply
not certifiable."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from io import BytesIO
from pathlib import Path

import pytest

from tests.webapp.fixtures.fill.extension_worker import extension_worker

ROOT = Path(__file__).parents[3]
EXTENSION_ROOT = ROOT / "extension"
BUILD_ROOT = EXTENSION_ROOT / "dist" / "extension"
FIXTURES = ROOT / "tests" / "webapp" / "fixtures" / "fill"
PORT = 8420  # the one loopback origin in the extension's host_permissions
RESULTS = ROOT / ".superpowers" / "sdd" / "2026-09-28-bundle6d-b-fill" / "spike-results"

# The experiment, run inside the extension's ISOLATED world. Only the
# isolated realm's own prototype setters are used, and events are limited
# to input/change: no click, no key, no focus.
INJECT = """
async ({tabId, plan, fileBytes, fileName, fileType}) => {
  const [{result}] = await chrome.scripting.executeScript({
    target: {tabId}, world: "ISOLATED", args: [plan, fileBytes, fileName, fileType],
    func: async (plan, fileBytes, fileName, fileType) => {
      const fire = (el) => {
        el.dispatchEvent(new Event("input", {bubbles: true}));
        el.dispatchEvent(new Event("change", {bubbles: true}));
      };
      const set = (proto, prop, el, value) =>
        Object.getOwnPropertyDescriptor(proto, prop).set.call(el, value);
      const out = {world: typeof chrome !== "undefined" && !!chrome.runtime ? "ISOLATED" : "MAIN"};
      if (plan.includes("text")) {
        const el = document.getElementById("q_text");
        set(HTMLInputElement.prototype, "value", el, "1 month"); fire(el);
      }
      if (plan.includes("select")) {
        const el = document.getElementById("q_select");
        set(HTMLSelectElement.prototype, "value", el, "GB"); fire(el);
      }
      if (plan.includes("checkbox")) {
        const el = document.getElementById("q_checkbox");
        set(HTMLInputElement.prototype, "checked", el, true); fire(el);
      }
      if (plan.includes("radio")) {
        const el = document.getElementById("q_radio_yes");
        set(HTMLInputElement.prototype, "checked", el, true); fire(el);
      }
      if (plan.includes("file")) {
        const el = document.getElementById("q_file");
        const placed = new File([new Uint8Array(fileBytes)], fileName, {type: fileType});
        const dt = new DataTransfer();
        dt.items.add(placed);
        el.files = dt.files;
        fire(el);
        const got = el.files[0];
        const digest = await crypto.subtle.digest("SHA-256", await got.arrayBuffer());
        out.file = {
          count: el.files.length, name: got.name, size: got.size, type: got.type,
          sha256: [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join(""),
        };
      }
      return out;
    },
  });
  return result;
}
"""

MAIN_WORLD_FILE_HASH = """
async () => {
  const el = document.getElementById("q_file");
  if (!el.files.length) return null;
  const digest = await crypto.subtle.digest("SHA-256", await el.files[0].arrayBuffer());
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
"""

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture(scope="module", autouse=True)
def production_extension_build():
    npm = shutil.which("npm")
    assert npm is not None, "npm is required to build the extension"
    subprocess.run([npm, "run", "build"], cwd=EXTENSION_ROOT, check=True)


@pytest.fixture(scope="module")
def fixture_server():
    """The same uvicorn + Starlette StaticFiles stack the other extension
    tests use. Python's SimpleHTTPRequestHandler reset connections midway
    through the ~140 KB framework files on Windows (ERR_CONNECTION_RESET),
    so the pages never mounted."""
    import time
    import socket
    import uvicorn
    from starlette.applications import Starlette
    from starlette.staticfiles import StaticFiles
    app = Starlette()
    app.mount("/", StaticFiles(directory=str(FIXTURES)), name="fill_fixtures")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.25):
                break
        except OSError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{PORT}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def extension_context(playwright, tmp_path):
    extension_path = str(BUILD_ROOT.resolve())
    context = playwright.chromium.launch_persistent_context(
        str(tmp_path / "chromium-profile"), headless=False,
        args=["--headless=new", f"--disable-extensions-except={extension_path}",
              f"--load-extension={extension_path}"])
    try:
        yield context
    finally:
        context.close()


def _worker(context):
    return extension_worker(context, BUILD_ROOT)


def _docx() -> bytes:
    from docx import Document
    stream = BytesIO()
    document = Document()
    for i in range(400):  # a realistic, multi-kilobyte CV body
        document.add_paragraph(f"Paragraph {i}: evidence-backed experience line with Unicode – é ü ✓.")
    document.save(stream)
    return stream.getvalue()


def _open(context, base, page_name):
    page = context.new_page()
    page.goto(f"{base}/{page_name}")
    # Mounted, not merely parsed: the Vue template's raw markup (including
    # #q_file and a "{{ ... }}" state line) exists before Vue mounts. Both
    # apps define __forceRender only once mounted and rendered.
    page.wait_for_function("typeof window.__forceRender === 'function'", timeout=15_000)
    page.wait_for_function("document.getElementById('state').textContent.startsWith('{\"')", timeout=15_000)
    worker = _worker(context)
    tab_id = worker.evaluate(
        "async (name) => (await chrome.tabs.query({})).find(t => t.url && t.url.includes(name))?.id ?? null",
        page_name)
    assert tab_id is not None
    return page, worker, tab_id


def _state(page):
    return json.loads(page.locator("#state").inner_text() or "{}")


FRAMEWORKS = ["spike_react.html", "spike_vue.html"]


@pytest.mark.parametrize("page_name", FRAMEWORKS)
def test_s1_isolated_file_placement_and_exact_byte_readback(extension_context, fixture_server, page_name):
    content = _docx()
    expected = hashlib.sha256(content).hexdigest()
    page, worker, tab_id = _open(extension_context, fixture_server, page_name)
    result = worker.evaluate(INJECT, {"tabId": tab_id, "plan": ["file"], "fileBytes": list(content),
                                      "fileName": "cv.docx", "fileType": DOCX})
    page.wait_for_timeout(150)
    # (b) exact byte readback in the ISOLATED world
    assert result["world"] == "ISOLATED"
    assert result["file"]["sha256"] == expected
    # (c) name, size and type
    assert (result["file"]["count"], result["file"]["name"], result["file"]["size"], result["file"]["type"]) == \
        (1, "cv.docx", len(content), DOCX)
    # (a) the page's own framework saw the selection
    assert _state(page)["file"] == {"name": "cv.docx", "size": len(content), "type": DOCX}
    # the page's MAIN world sees the identical bytes, also after a forced re-render
    assert page.evaluate(MAIN_WORLD_FILE_HASH) == expected
    page.evaluate("window.__forceRender()")
    page.wait_for_timeout(150)
    assert page.evaluate(MAIN_WORLD_FILE_HASH) == expected
    _record(page_name, "S1", {"expected_sha256": expected, "isolated": result["file"], "page_state": _state(page)["file"]})


EXPECTED = {"text": ("q_text", "value", "1 month", "text"), "select": ("q_select", "value", "GB", "choice"),
            "checkbox": ("q_checkbox", "checked", True, "agree"), "radio": ("q_radio_yes", "checked", True, "radio")}


@pytest.mark.parametrize("page_name", FRAMEWORKS)
def test_s3_controlled_inputs_without_click(extension_context, fixture_server, page_name):
    page, worker, tab_id = _open(extension_context, fixture_server, page_name)
    worker.evaluate(INJECT, {"tabId": tab_id, "plan": list(EXPECTED), "fileBytes": [], "fileName": "",
                             "fileType": ""})
    page.wait_for_timeout(150)
    registered = _state(page)
    page.evaluate("window.__forceRender()")
    page.wait_for_timeout(150)
    after = _state(page)
    table = {}
    for kind, (element_id, prop, dom_value, state_key) in EXPECTED.items():
        state_value = {"radio": "yes"}.get(kind, dom_value)
        table[kind] = {
            "registered_in_framework_state": registered.get(state_key) == state_value,
            "state_after_rerender": after.get(state_key) == state_value,
            "dom_after_rerender": page.locator(f"#{element_id}").evaluate(f"el => el.{prop}") == dom_value,
        }
    _record(page_name, "S3", table)
    assert set(table) == set(EXPECTED)  # the table is complete; certifiability is decided from it


def _record(page_name: str, spike: str, data) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{spike}-{page_name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
