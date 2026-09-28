// Bundle 6D-B page bundle: injected on demand into the user-activated tab,
// ISOLATED world (spec §18), and driven by the background run controller
// through chrome.scripting.executeScript. It exposes read-only observation,
// the six-primitive executor and the detections; nothing here talks to the
// server (the background does). Page scripts cannot reach this world.

import { CERTIFIED_ADAPTERS, certifiedAdapterFor, type CertifiedAdapter } from "./certified-adapters";
import { installDetections, type DetectionHandle, type DetectionKind } from "./detections";
import { executeAction } from "./executor";
import { FILL_PAGE_KEY, type FillPageApi } from "./page-api";
import { observe } from "./observer";

function adapterFor(adapterId: string): CertifiedAdapter {
  const adapter = CERTIFIED_ADAPTERS.find((a) => a.id === adapterId);
  if (!adapter) throw new Error(`not a certified adapter: ${adapterId}`);
  return adapter;
}

function install(): void {
  const scope = globalThis as unknown as Record<string, unknown>;
  if (scope[FILL_PAGE_KEY]) return;
  let detections: DetectionHandle | null = null;
  const api: FillPageApi = {
    version: 1,
    detect() {
      const adapter = certifiedAdapterFor(document);
      return adapter ? { adapterId: adapter.id, adapterVersion: adapter.adapterVersion } : null;
    },
    rootPresent: (adapterId) => adapterFor(adapterId).applicationRoot(document) !== null,
    observe: (adapterId, context) => observe(document, adapterFor(adapterId), context),
    execute: (adapterId, action, envelope, attachment) => {
      const file = attachment
        ? new File([Uint8Array.from(attachment.bytes)], attachment.filename, { type: attachment.mediaType })
        : null;
      return executeAction(document, adapterFor(adapterId), action, envelope, file);
    },
    installDetections(runId) {
      detections?.dispose();
      detections = installDetections(window, (kind: DetectionKind, detail) => {
        void chrome.runtime.sendMessage({ type: "fill_detection", runId, kind, detail });
      });
    },
    enablePostFill(adapterId) {
      detections?.enablePostFill(adapterFor(adapterId).applicationRoot(document) ?? document);
    },
  };
  scope[FILL_PAGE_KEY] = api;
}

install();
