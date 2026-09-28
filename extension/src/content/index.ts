import { genericAdapter, greenhouseAdapter, leverAdapter } from "../adapters";
import { probePage } from "./probe";
import { INJECTED_PROBE_RESULT_KEY } from "./probe-source";

// Probe-only (6D-B Task 14): the Phase 3 autofill writer is retired. This
// bundle detects/scans/classifies without candidate data and never writes
// to the page; the ONLY employer-page writer is extension/src/fill/executor.ts
// (proven by test/single-writer-callgraph.test.ts). The result is stored on
// globalThis for the background worker to read back.
const probeResult = probePage(document, [greenhouseAdapter, leverAdapter, genericAdapter]);
(globalThis as unknown as Record<string, unknown>)[INJECTED_PROBE_RESULT_KEY] = probeResult;
