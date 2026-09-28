# 6D-B spike results: S1 and S3 (Task 1)

- **Date:** 2026-09-28.
- **Branch:** `bundle6/6d-b-fill`.
- **Harness:** `tests/webapp/spikes/test_fill_spikes_browser.py`.
- **Browser:** Chrome 154.0.8037.57 (Playwright `--browser-channel chrome`), with the production extension build loaded unpacked.
- **Fixtures:** `tests/webapp/fixtures/fill/spike_react.html` (React 18.3.1) and `spike_vue.html` (Vue 3.4.38), served from `127.0.0.1:8420`, which is inside the extension's `host_permissions`.
- **Stability:** 4/4 tests passed on three consecutive runs.

The experiment runs in the extension's **ISOLATED** world through `chrome.scripting.executeScript({world: "ISOLATED", func})`. It uses only the isolated realm's own prototype setters, then dispatches `input` + `change`. It never clicks, sends keys or changes focus.

## S1: file placement with exact byte readback (a gate): PASS

On both React and Vue:
- **(b) Exact byte readback in the ISOLATED world.** `await input.files[0].arrayBuffer()` SHA-256 equals the source document's SHA-256. The document was a 37 881-byte DOCX: React `08390284…1cf7`, Vue `fe490a3a…750d`, each matching exactly.
- **(c)** `files.length = 1`; name `cv.docx`; size 37 881; type `application/vnd.openxmlformats-officedocument.wordprocessingml.document`.
- **(a) The page's own framework saw the selection.** Its `onChange` fired and its state holds the name, size and type.
- The page's **MAIN world** hashes the identical bytes, both immediately and after a forced framework re-render.

**Conclusion:** D6 and I5 (`ATTACH_LOCAL` verified by the byte SHA-256 of `input.files[0]`) are implementable as specified in the ISOLATED world. MAIN-world injection isn't needed.

## S3: controlled inputs without a click: recorded

| Control kind | React 18.3.1 | Vue 3.4.38 |
|---|---|---|
| text (`input` type text) | registered, and persists after re-render | registered, persists |
| single `select` | registered, persists | registered, persists |
| checkbox | **not registered** (reverted on re-render) | registered, persists |
| radio | **not registered** (reverted on re-render) | registered, persists |

React routes checkbox and radio changes through its `click` handling, which the executor never sends (D10). So on React-rendered forms, **checkbox and radio aren't certifiable**. Per spec §7.4 and §12.2 that's a certification outcome, not a design change: those kinds simply aren't in `supported_control_kinds`, and the executor would detect such a write as a readback failure anyway (`FIELD_VALUE_REVERTED`).

**Input to Task 5:** each certified adapter version's `supported_control_kinds` must reflect the framework its real forms use:
- React-rendered forms: `{text, email, tel, url, number, date, textarea, select}`, with no checkbox or radio;
- Vue or plain-DOM forms can also include checkbox and radio.

Task 5 establishes each adapter's framework from its fixtures, and Task 16 re-proves it in the certification browser tests.
