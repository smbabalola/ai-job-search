"""The extension's own MV3 service worker, for the 6D-B browser suites.

Playwright occasionally does not report an extension worker in a freshly
launched persistent context (observed as a rare 15 s "serviceworker" timeout
across the spike, quarantine and acceptance suites, on both builds). Opening
the extension's popup page, which messages the background, starts the worker
again. A worker that cannot start still fails: waking cannot revive broken
code, so this never hides an extension defect."""
from __future__ import annotations

import hashlib
from pathlib import Path


def extension_id(build: Path) -> str:
    # Chrome's unpacked-extension id: sha256 of the absolute path (UTF-16LE on
    # Windows), first 32 hex digits mapped to a-p.
    digest = hashlib.sha256(str(build.resolve()).encode("utf-16-le")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def _is_extension(worker) -> bool:
    return worker.url.startswith("chrome-extension://")


def extension_worker(context, build: Path, timeout_ms: int = 15_000):
    workers = [w for w in context.service_workers if _is_extension(w)]
    if workers:
        return workers[0]
    try:
        return context.wait_for_event("serviceworker", predicate=_is_extension, timeout=timeout_ms)
    except Exception:
        print("[fill-e2e] extension worker not reported at launch; waking it from the popup page")
        page = context.new_page()
        page.goto(f"chrome-extension://{extension_id(build)}/popup.html")
        workers = [w for w in context.service_workers if _is_extension(w)]
        return workers[0] if workers else context.wait_for_event("serviceworker", predicate=_is_extension,
                                                                 timeout=timeout_ms)
