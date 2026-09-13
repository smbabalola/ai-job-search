import type { Adapter, AttachmentDocumentKind } from "../adapters/types";

export type AttachmentAttemptOutcome =
  | "selected" | "no_compatible_target" | "write_failed";

export interface AttachmentAttemptResult {
  outcome: AttachmentAttemptOutcome;
  pageFieldKey: string | null;
}

// Writes a real File into a real <input type="file"> via the standard
// DataTransfer technique (assigning .value directly is blocked by every
// browser for file inputs) and dispatches a "change" event so any
// listener the ATS's own page script attached still observes the
// selection exactly as it would for a real user file-picker interaction.
function writeFileToInput(input: HTMLInputElement, file: File): void {
  const dataTransfer = new DataTransfer();
  dataTransfer.items.add(file);
  input.files = dataTransfer.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

// Attempts to attach `file` for the given document kind, using ONLY a
// target the adapter has positively identified via
// findAttachmentTarget — never a generic/guessed file input, and never
// broadened ATS host access to do so. Returns "no_compatible_target"
// (not an error) when the adapter has no verified upload-field
// convention for this kind on this page; that is an honest, expected
// outcome for most ATS forms today, not a failure to guess harder.
export function attemptAttachment(
  document: Document, adapter: Adapter, kind: AttachmentDocumentKind, file: File,
): AttachmentAttemptResult {
  const target = adapter.findAttachmentTarget?.(document, kind) ?? null;
  if (!target) {
    return { outcome: "no_compatible_target", pageFieldKey: null };
  }

  const pageFieldKey = target.id
    ? `${adapter.id}:attachment:${target.id}`
    : `${adapter.id}:attachment:${kind}`;

  try {
    writeFileToInput(target, file);
  } catch {
    return { outcome: "write_failed", pageFieldKey };
  }

  // "selected" proves only that the extension successfully set the
  // input's file list and fired change — it does NOT prove the ATS's
  // own asynchronous upload/parse completed. There is no cross-origin
  // way to observe that without broadening host permissions, so the
  // outcome vocabulary stops at what was genuinely observed (matching
  // buildAttachmentEventPayload's existing "selected" /
  // "upload_confirmed_by_adapter" distinction in attachment.ts).
  if (target.files && target.files.length > 0 && target.files[0] === file) {
    return { outcome: "selected", pageFieldKey };
  }
  return { outcome: "write_failed", pageFieldKey };
}
