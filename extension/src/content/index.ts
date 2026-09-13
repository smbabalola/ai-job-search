import { genericAdapter, greenhouseAdapter, leverAdapter } from "../adapters";
import { runContentScript } from "./content-script";
import { readInjectedSnapshot } from "./snapshot-source";
import { probePage } from "./probe";
import { INJECTED_PROBE_RESULT_KEY } from "./probe-source";
import type { ContentScriptMessage } from "./messages";

// Greenhouse/Lever tried first (each has a specific, narrow detect()),
// generic last as the catch-all fallback — content-script.test.ts only
// ever exercises a single adapter at a time, so there is no existing
// multi-adapter ordering precedent to match; this ordering is chosen here
// because runContentScript takes the first adapter whose detect() returns
// true (content-script.ts:102), so the most specific match must precede
// the always-true generic fallback.
const adapters = [greenhouseAdapter, leverAdapter, genericAdapter];

const snapshot = readInjectedSnapshot(globalThis as unknown as Record<string, unknown>);

if (snapshot) {
  runContentScript(
    document,
    snapshot,
    adapters,
    (message: ContentScriptMessage) => {
      chrome.runtime.sendMessage(message);
    },
  );
} else {
  // No snapshot was injected yet — this is the probe-only injection
  // (background/index.ts's runAutofillOnTab injects this same bundle a
  // first time, before any session/snapshot exists, specifically to run
  // detect/scan/classify without candidate data). Store the result on
  // globalThis for the background worker to read back via a second
  // chrome.scripting.executeScript call, mirroring how the snapshot
  // itself is handed over (snapshot-source.ts) — no DOM write, no
  // handoff event, no server call happens in this branch.
  const probeResult = probePage(document, [greenhouseAdapter, leverAdapter, genericAdapter]);
  (globalThis as unknown as Record<string, unknown>)[INJECTED_PROBE_RESULT_KEY] = probeResult;
}
