// Self-executing content-bundle entry point for the attachment step,
// mirroring content/index.ts's globalThis-bridge pattern exactly
// (probe-source.ts / snapshot-source.ts): the background worker writes
// the file bytes + adapter id + kind to globalThis under
// ATTACHMENT_REQUEST_KEY immediately before injecting this bundle, then
// reads the outcome back from ATTACHMENT_RESULT_KEY via a second, tiny
// executeScript call — no chrome.runtime.sendMessage round trip needed.
//
// This is a genuinely separate bundle (not reusing content/index.ts)
// because it must run in the page's own MAIN world: assigning a real
// File to an <input type="file"> via DataTransfer only takes effect
// when observed by the page's own scripts if it happens in that same
// world, not the ISOLATED world every other content script in this
// extension runs in.
import { greenhouseAdapter, leverAdapter, genericAdapter } from "../adapters";
import { attemptAttachment } from "./attachment-dom";
import {
  ATTACHMENT_REQUEST_KEY, ATTACHMENT_RESULT_KEY, type AttachmentRequest,
} from "./attachment-source";

const ADAPTERS_BY_ID: Record<string, typeof greenhouseAdapter> = {
  [greenhouseAdapter.id]: greenhouseAdapter,
  [leverAdapter.id]: leverAdapter,
  [genericAdapter.id]: genericAdapter,
};

const globalObj = globalThis as unknown as Record<string, unknown>;
const request = globalObj[ATTACHMENT_REQUEST_KEY] as AttachmentRequest | undefined;

if (request) {
  const adapter = ADAPTERS_BY_ID[request.adapterId];
  if (!adapter) {
    globalObj[ATTACHMENT_RESULT_KEY] = { outcome: "no_compatible_target", pageFieldKey: null };
  } else {
    const file = new File(
      [new Uint8Array(request.fileBytes)], request.filename, { type: request.mimeType },
    );
    const result = attemptAttachment(document, adapter, request.kind, file);
    globalObj[ATTACHMENT_RESULT_KEY] = result;
  }
}
