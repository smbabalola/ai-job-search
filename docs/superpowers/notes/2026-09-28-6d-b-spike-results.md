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

# S2 and S4 results (Task 2)

- **Harness:** `tests/webapp/test_fill_quarantine_browser.py`.
- **Extension build:** the `FILL_TEST_HOOKS` build in `dist/extension-test-hooks`. It uses the production quarantine and sibling modules, exposed on the extension service worker. The production `dist/extension` never contains the hook.
- **Servers:**
  - an HTTP recorder on `127.0.0.1:8420`;
  - a stdlib WebSocket recorder on `127.0.0.1:8421`. uvicorn has no WebSocket library installed here, and I added none.
- **Proof of blocking:** a blocked request never reaches the recorder.
- **Stability:** 18 consecutive clean runs, 8/8 tests each.

## S2: quarantine enforcement: PASS

| Probe from the execution tab | PRELOAD | TOTAL |
|---|---|---|
| form POST (into a sub-frame), `fetch` POST, XHR PUT | blocked | blocked |
| `sendBeacon` | blocked | blocked |
| WebSocket handshake (with a positive control showing it connects when unquarantined) | blocked | blocked |
| employer service-worker `fetch()` (`TAB_ID_NONE`, employer initiator: Q2) | blocked | blocked |
| GET `fetch`, image beacon, script, stylesheet, sub-frame GET, GET after `pushState` | **allowed** | blocked |
| top-level GET navigation | **allowed** | blocked |

Also proved:
- **The extension's own server calls are unaffected under TOTAL.** They originate in the extension service worker, not the tab.
- **The reset closes a document-owned WebSocket.** The recorder sees the close, and no frame arrives afterwards. Under PRELOAD, the reloaded page's new socket is blocked.
- **`window.open` from the page** (no user activation) is either blocked, or the new tab is detected by sibling containment.
- **Certification refusal:** a page whose service worker opens a WebSocket is flagged `SW_PERSISTENT_CHANNEL` by `tests/webapp/fixtures/fill/certify.py`. The probe page, whose service worker only fetches, and a document-owned socket are not flagged.

**Limitation: WebTransport.** There's no local HTTP/3 server, and both an unquarantined and a quarantined handshake fail with the identical `Opening handshake failed.`, so the browser test can't tell blocking apart from refusal. It asserts only that the handshake never becomes ready. The `webtransport` type in both rulesets is asserted exactly in the unit suite (`extension/test/fill-quarantine.test.ts`).

## S4: sibling enumeration: PASS

With `tabs` + `webNavigation` (granted up front only in the test-hook build):
- a second top-level tab on the employer origin is detected;
- an employer-origin frame embedded in a tab on a different origin (`localhost` embedding `127.0.0.1`) is detected;
- closing the sibling restores `ok`.

**Blind spots**, as the spec §10.5 documents: service workers and SharedWorkers (contained by Q2 and certification), prerendered pages not yet activated, and incognito tabs when the extension isn't allowed in incognito.
