import { describe, expect, it, vi } from "vitest";
import { buildAttachmentEventPayload, fetchExactPackDocument } from "../src/background/attachment";

describe("fetchExactPackDocument", () => {
  it("calls the session-scoped document endpoint with the session token and reads the hash header", async () => {
    const bytes = new TextEncoder().encode("fake docx bytes").buffer;
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      headers: new Map([
        ["content-disposition", 'attachment; filename="Acme_Engineer_CV.docx"'],
        ["content-type", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
        ["x-content-hash", "sha256:abc123"],
      ]),
      arrayBuffer: async () => bytes,
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchExactPackDocument(
      "http://127.0.0.1:8420", "hs_1", "session-tok-1", "cv",
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8420/api/handoff/sessions/hs_1/documents/cv",
      expect.objectContaining({
        headers: expect.objectContaining({ "X-Handoff-Session-Token": "session-tok-1" }),
      }),
    );
    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(options.headers).not.toHaveProperty("X-Handoff-Credential");
    expect(result.sha256).toBe("sha256:abc123");
    expect(result.filename).toBe("Acme_Engineer_CV.docx");
    expect(result.byteLength).toBe(bytes.byteLength);
    vi.unstubAllGlobals();
  });

  it("has no workspaceId/packArtifactId/durable-credential parameter at all — authority is session id + session token only", () => {
    expect(fetchExactPackDocument.length).toBe(4); // baseUrl, sessionId, sessionToken, kind
  });

  it("re-fetches fresh on every call rather than caching (no internal memoization across calls)", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      headers: new Map([
        ["content-disposition", 'attachment; filename="x.docx"'],
        ["content-type", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
        ["x-content-hash", "sha256:def456"],
      ]),
      arrayBuffer: async () => new ArrayBuffer(0),
    });
    vi.stubGlobal("fetch", fetchMock);

    await fetchExactPackDocument("http://127.0.0.1:8420", "hs_1", "tok", "cover_letter");
    await fetchExactPackDocument("http://127.0.0.1:8420", "hs_1", "tok", "cover_letter");

    expect(fetchMock).toHaveBeenCalledTimes(2);
    vi.unstubAllGlobals();
  });

  it("throws when the request fails (e.g. wrong/expired session token)", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 401 });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      fetchExactPackDocument("http://127.0.0.1:8420", "hs_1", "bad-tok", "cv"),
    ).rejects.toThrow();
    vi.unstubAllGlobals();
  });
});

describe("buildAttachmentEventPayload", () => {
  it("carries the exact pack id, renderer version, hash, and outcome — never a bare boolean", () => {
    const doc = {
      kind: "cv" as const, filename: "Acme_Engineer_CV.docx",
      mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      sha256: "sha256:abc123", byteLength: 42, bytes: new ArrayBuffer(42),
    };
    const payload = buildAttachmentEventPayload(
      doc, "art_XYZ", "application-pack-renderer.v2", "selected",
    );
    expect(payload).toEqual({
      kind: "cv", filename: "Acme_Engineer_CV.docx",
      mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      sha256: "sha256:abc123", byte_length: 42,
      pack_artifact_id: "art_XYZ", renderer_version: "application-pack-renderer.v2",
      outcome: "selected",
    });
  });

  it("supports the unknown outcome as an honest default, never upgraded to selected", () => {
    const doc = {
      kind: "cover_letter" as const, filename: "x.docx", mimeType: "x",
      sha256: "sha256:x", byteLength: 1, bytes: new ArrayBuffer(1),
    };
    const payload = buildAttachmentEventPayload(doc, "art_1", "v2", "unknown");
    expect(payload.outcome).toBe("unknown");
  });
});
