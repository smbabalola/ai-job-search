export type AttachmentOutcome =
  | "selected" | "upload_confirmed_by_adapter" | "rejected" | "unknown";

export interface RenderedDocument {
  kind: "cv" | "cover_letter";
  filename: string;
  mimeType: string;
  sha256: string;
  byteLength: number;
  bytes: ArrayBuffer;
}

function parseFilename(contentDisposition: string): string {
  const match = contentDisposition.match(/filename="([^"]+)"/);
  return match ? match[1] : "document.docx";
}

// Calls the dedicated session-scoped document endpoint
// (GET /api/handoff/sessions/{sessionId}/documents/{kind}) fresh every
// time — no caching, no reuse of bytes fetched earlier in the
// application lifecycle. Authority is session id + session token +
// document kind ONLY: there is no workspaceId/packArtifactId/durable-
// credential parameter at all, so a caller cannot select a different
// workspace or pack even by mistake — the server derives both from the
// session token alone (design spec Section 7 / Task 11).
export async function fetchExactPackDocument(
  baseUrl: string,
  sessionId: string,
  sessionToken: string,
  kind: "cv" | "cover_letter",
): Promise<RenderedDocument> {
  const url = `${baseUrl}/api/handoff/sessions/${sessionId}/documents/${kind}`;
  const response = await fetch(url, {
    headers: { "X-Handoff-Session-Token": sessionToken },
  });
  if (!response.ok) {
    throw new Error(`failed to fetch ${kind} for session ${sessionId}: ${response.status}`);
  }
  const bytes = await response.arrayBuffer();
  return {
    kind,
    filename: parseFilename(response.headers.get("content-disposition") ?? ""),
    mimeType: response.headers.get("content-type") ?? "application/octet-stream",
    sha256: response.headers.get("x-content-hash") ?? "",
    byteLength: bytes.byteLength,
    bytes,
  };
}

// Builds the event_payload for an upload_selected / upload_confirmed /
// upload_failed event (design spec Section 7, Section 11). The outcome
// vocabulary is deliberately not a boolean: "selected" proves only that
// the extension attempted the attachment, not that the ATS's own
// asynchronous upload completed.
export function buildAttachmentEventPayload(
  document: RenderedDocument,
  packArtifactId: string,
  rendererVersion: string,
  outcome: AttachmentOutcome,
): Record<string, unknown> {
  return {
    kind: document.kind,
    filename: document.filename,
    mime_type: document.mimeType,
    sha256: document.sha256,
    byte_length: document.byteLength,
    pack_artifact_id: packArtifactId,
    renderer_version: rendererVersion,
    outcome,
  };
}
