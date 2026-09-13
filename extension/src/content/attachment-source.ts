import type { AttachmentDocumentKind } from "../adapters/types";
import type { AttachmentAttemptResult } from "./attachment-dom";

export const ATTACHMENT_REQUEST_KEY = "__jobsearch_handoff_attachment_request__";
export const ATTACHMENT_RESULT_KEY = "__jobsearch_handoff_attachment_result__";

export interface AttachmentRequest {
  adapterId: string;
  kind: AttachmentDocumentKind;
  filename: string;
  mimeType: string;
  fileBytes: number[];
}

export function readInjectedAttachmentResult(
  globalObj: Record<string, unknown>,
): AttachmentAttemptResult | null {
  const value = globalObj[ATTACHMENT_RESULT_KEY];
  if (typeof value !== "object" || value === null) return null;
  return value as AttachmentAttemptResult;
}
