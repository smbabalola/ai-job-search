// Pure logic for the loopback webapp->extension pending-context
// bridge — no document/chrome.* calls here, so this module is safely
// importable in the test environment. content-bridge/index.ts (never
// imported by a test, matching content/index.ts's own precedent) wires
// this up to real DOM events and chrome.runtime.sendMessage.

export interface PendingContextLaunchMessage {
  type: "set_pending_handoff_context";
  workspaceId: string;
  packArtifactId: string;
  targetUrl: string;
  requestedAt: number;
}

export interface PendingContextLaunchAck {
  ok: boolean;
  error?: string;
}

export interface ApplyButtonLike {
  dataset: {
    workspaceId?: string;
    packArtifactId?: string;
    targetUrl?: string;
  };
}

export function isHttpUrl(value: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  return (parsed.protocol === "http:" || parsed.protocol === "https:") && parsed.host.length > 0;
}

// Returns null for any missing/empty attribute or a target URL that
// isn't http(s) with a real host, so a malformed launch context can
// never reach the background worker at all (defense in depth alongside
// the background's own validation).
export function buildPendingContextMessage(
  button: ApplyButtonLike,
): PendingContextLaunchMessage | null {
  const { workspaceId, packArtifactId, targetUrl } = button.dataset;
  if (!workspaceId || !packArtifactId || !targetUrl) return null;
  if (!isHttpUrl(targetUrl)) return null;
  return {
    type: "set_pending_handoff_context",
    workspaceId,
    packArtifactId,
    targetUrl,
    requestedAt: Date.now(),
  };
}

export type SendLaunchMessage = (
  message: PendingContextLaunchMessage,
) => Promise<PendingContextLaunchAck>;

// The core invariant this whole bridge exists for: the user must never
// be sent to the ATS unless the extension has already durably
// persisted the exact workspace + exact pack + exact target context.
// shouldNavigate is only ever true after an explicit { ok: true } ack
// from the background worker — a rejected promise, an { ok: false }
// ack, or a malformed button both fail closed (never navigate).
export async function handleApplyClick(
  button: ApplyButtonLike,
  sendLaunchMessage: SendLaunchMessage,
): Promise<{ shouldNavigate: boolean; targetUrl?: string }> {
  const message = buildPendingContextMessage(button);
  if (!message) return { shouldNavigate: false };

  try {
    const ack = await sendLaunchMessage(message);
    if (!ack.ok) return { shouldNavigate: false };
  } catch {
    return { shouldNavigate: false };
  }
  return { shouldNavigate: true, targetUrl: message.targetUrl };
}
