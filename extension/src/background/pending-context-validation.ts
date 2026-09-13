// Pure validation for the set_pending_handoff_context message the
// content-bridge script sends — no chrome.* calls here, so this module
// is safely importable in the test environment (background/index.ts's
// own chrome.runtime.onMessage.addListener(...) top-level call is NOT
// side-effect-free, which is exactly why this validation logic lives
// in its own module rather than being exported directly from there).
import type { PendingHandoffContext } from "./pending-context-store";

function isValidHttpTargetUrl(value: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  return (parsed.protocol === "http:" || parsed.protocol === "https:") && parsed.host.length > 0;
}

// Validates a set_pending_handoff_context message and its sender in
// full before anything is persisted: sender must be a real tab (the
// loopback content-bridge script, never the popup or an arbitrary
// extension page), sender.url must be exactly the JobSearch webapp
// origin (never an employer/ATS page pretending to be it), the message
// must carry ONLY the five expected fields (no unexpected extras
// silently accepted), workspaceId/packArtifactId must be non-empty
// strings, and targetUrl must be a real http(s) URL with a host. This
// is a security boundary — data that will be persisted and later acted
// on — so it is exported and unit-tested directly, not left as
// untested glue the way DOM-wiring code is elsewhere in this codebase.
export function extractValidPendingContext(
  message: unknown,
  sender: chrome.runtime.MessageSender,
  jobSearchWebappOrigin: string,
): PendingHandoffContext | null {
  if (!sender.tab?.id) return null;
  if (!sender.url || !sender.url.startsWith(`${jobSearchWebappOrigin}/`)) return null;
  if (typeof message !== "object" || message === null) return null;

  const allowedKeys = new Set([
    "type", "workspaceId", "packArtifactId", "targetUrl", "requestedAt",
  ]);
  const keys = Object.keys(message as Record<string, unknown>);
  if (keys.some((key) => !allowedKeys.has(key))) return null;

  const candidate = message as {
    type?: unknown; workspaceId?: unknown; packArtifactId?: unknown;
    targetUrl?: unknown; requestedAt?: unknown;
  };
  if (candidate.type !== "set_pending_handoff_context") return null;
  if (typeof candidate.workspaceId !== "string" || candidate.workspaceId.length === 0) return null;
  if (typeof candidate.packArtifactId !== "string" || candidate.packArtifactId.length === 0) return null;
  if (typeof candidate.targetUrl !== "string" || !isValidHttpTargetUrl(candidate.targetUrl)) return null;
  if (typeof candidate.requestedAt !== "number") return null;

  return {
    workspaceId: candidate.workspaceId,
    packArtifactId: candidate.packArtifactId,
    targetUrl: candidate.targetUrl,
    requestedAt: candidate.requestedAt,
  };
}
