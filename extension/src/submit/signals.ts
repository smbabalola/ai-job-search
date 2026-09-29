// Bundle 6E-A certified result and challenge signals (spec §11.1, §12.1).
// Read-only DOM checks; the server, not these signals, decides the result.

import type { SubmitCertification } from "./certification";

export interface SignalContext {
  url: string;              // the document's current URL
  boundUrl: string;         // the authorized application URL
  confirmationUrl: string;  // the certified confirmation URL for the bound target
  rootPresent: boolean;     // the adapter's application root is still in the document
}

export interface Signals { success: boolean; failure: boolean; challenge: boolean }

export function isVisible(el: Element): boolean {
  const view = el.ownerDocument.defaultView;
  const style = view ? view.getComputedStyle(el) : null;
  if (style && (style.display === "none" || style.visibility === "hidden")) return false;
  const box = el.getBoundingClientRect();
  return box.width > 0 && box.height > 0;
}

function anyVisible(doc: Document, selectors: readonly string[]): boolean {
  return selectors.some((sel) => [...doc.querySelectorAll(sel)].some(isVisible));
}

function onUrl(url: string, target: string): boolean {
  const withoutQuery = url.split("#")[0].split("?")[0];
  return withoutQuery === target;
}

export function detectSignals(doc: Document, cert: SubmitCertification, ctx: SignalContext): Signals {
  const marker = cert.successSelectors.some((sel) => doc.querySelector(sel) !== null);
  const success = marker && !ctx.rootPresent && (onUrl(ctx.url, ctx.confirmationUrl) || onUrl(ctx.url, ctx.boundUrl));
  const failure = !success && ctx.rootPresent && anyVisible(doc, cert.failureSelectors);
  const challenge = anyVisible(doc, cert.challengeSelectors);
  return { success, failure, challenge };
}
