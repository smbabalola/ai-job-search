// Bundle 6D-B page bundle: injected on demand into the user-activated tab,
// ISOLATED world (spec §18), and driven by the background run controller
// through chrome.scripting.executeScript. It exposes read-only observation,
// the six-primitive executor and the detections; nothing here talks to the
// server (the background does). Page scripts cannot reach this world.

import { CERTIFIED_ADAPTERS, certifiedAdapterFor, type CertifiedAdapter } from "./certified-adapters";
import { installDetections, type DetectionHandle, type DetectionKind } from "./detections";
import { executeAction } from "./executor";
import { FILL_PAGE_KEY, type FillPageApi } from "./page-api";
import { observe, submitControlPayload } from "./observer";
import { canonicalHash } from "./canonical";
import { certificationById } from "../submit/certification";
import { detectSignals } from "../submit/signals";
import { submitClick } from "../submit/submit-executor";

function adapterFor(adapterId: string): CertifiedAdapter {
  const adapter = CERTIFIED_ADAPTERS.find((a) => a.id === adapterId);
  if (!adapter) throw new Error(`not a certified adapter: ${adapterId}`);
  return adapter;
}

// 6E-A: the certified submit control whose fill-submit-control v1
// fingerprint equals the bound one -- exactly one, or none.
async function boundSubmitControl(certificationId: string, fingerprint: string): Promise<Element | null> {
  const cert = certificationById(certificationId);
  if (!cert) return null;
  const matches: Element[] = [];
  for (const el of document.querySelectorAll(cert.submitControlSelector)) {
    if (await canonicalHash("fill-submit-control", "v1", submitControlPayload(el)) === fingerprint) matches.push(el);
  }
  return matches.length === 1 ? matches[0] : null;
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
    // The page's actual current URL, never the one remembered at start: a
    // history or URL change is then a §13 target change in the observation.
    observe: (adapterId, context) => observe(document, adapterFor(adapterId),
      { ...context, canonicalUrl: location.href, origin: location.origin }),
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
    detected: () => detections?.reported() ?? [],
    enablePostFill(adapterId) {
      detections?.enablePostFill(adapterFor(adapterId).applicationRoot(document) ?? document);
    },
    findSubmitControl: async (certificationId, fingerprint) =>
      (await boundSubmitControl(certificationId, fingerprint)) !== null,
    async clickSubmit(certificationId, fingerprint) {
      const el = await boundSubmitControl(certificationId, fingerprint);
      if (el === null) return "SUBMIT_CONTROL_MISSING";
      // The one-shot pass through the 6D-B submit guard covers exactly this
      // click: a submit button's submit event fires synchronously inside
      // click(), and the pass is disarmed on every path afterwards.
      detections?.allowNextSubmit();
      try {
        submitClick(el);
      } finally {
        detections?.disarmSubmit();
      }
      return "CLICKED";
    },
    signals(adapterId, certificationId, context) {
      const cert = certificationById(certificationId);
      if (!cert) return { success: false, failure: false, challenge: false };
      return detectSignals(document, cert, { ...context, url: location.href,
        rootPresent: adapterFor(adapterId).applicationRoot(document) !== null });
    },
    watchSubmitContent(adapterId) {
      detections?.watchContent(adapterFor(adapterId).applicationRoot(document) ?? document);
    },
    contentChanged: () => detections?.contentChanged() ?? false,
  };
  scope[FILL_PAGE_KEY] = api;
}

install();
