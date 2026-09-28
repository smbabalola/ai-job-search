import { describe, expect, it, beforeAll } from "vitest";
import { execFileSync } from "node:child_process";
import { readFileSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Script } from "node:vm";
import { transformSync } from "esbuild";
import { createHash } from "node:crypto";

const extensionRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const distDir = path.join(extensionRoot, "dist", "extension");

// Runs the real build once for this whole file, exactly as `npm run build`
// would from a clean checkout, then asserts on the actual files it wrote —
// not a simulation of the build.
// 6D-B Task 2: the production build, then the separate FILL_TEST_HOOKS
// build, in ONE sequence (never in parallel files: both write under dist/).
const hooksDir = path.join(extensionRoot, "dist", "extension-test-hooks");
const sha = (file: string) => createHash("sha256").update(readFileSync(file)).digest("hex");
let prodBackgroundHashBeforeHooksBuild = "";

beforeAll(() => {
  execFileSync("node", ["scripts/build.mjs"], { cwd: extensionRoot, stdio: "pipe",
    env: { ...process.env, FILL_TEST_HOOKS: "" } });
  prodBackgroundHashBeforeHooksBuild = sha(path.join(distDir, "background", "index.js"));
  execFileSync("node", ["scripts/build.mjs"], { cwd: extensionRoot, stdio: "pipe",
    env: { ...process.env, FILL_TEST_HOOKS: "1" } });
}, 120_000);

describe("extension build", () => {
  it("produces a manifest.json that parses as valid JSON", () => {
    const raw = readFileSync(path.join(distDir, "manifest.json"), "utf8");
    expect(() => JSON.parse(raw)).not.toThrow();
  });

  it("every local file the manifest references exists in dist/", () => {
    const manifest = JSON.parse(readFileSync(path.join(distDir, "manifest.json"), "utf8"));

    const referenced: string[] = [];
    if (manifest.background?.service_worker) referenced.push(manifest.background.service_worker);
    if (manifest.action?.default_icon) referenced.push(...Object.values(manifest.action.default_icon) as string[]);
    if (manifest.icons) referenced.push(...Object.values(manifest.icons) as string[]);
    for (const cs of manifest.content_scripts ?? []) {
      referenced.push(...(cs.js ?? []), ...(cs.css ?? []));
    }

    expect(referenced.length).toBeGreaterThan(0);
    for (const relPath of referenced) {
      const abs = path.join(distDir, relPath);
      expect(existsSync(abs), `manifest references ${relPath}, expected at ${abs}`).toBe(true);
    }
  });

  it("bundles the background service worker as a non-empty, parseable JS file", () => {
    const content = readFileSync(path.join(distDir, "background/index.js"), "utf8");
    expect(content.length).toBeGreaterThan(0);
    // The background bundle is a real ES module (manifest declares
    // "type": "module", and it has a top-level `export` since Task 7 added
    // runAutofillOnTab) — node:vm's Script only parses classic scripts, so
    // an ESM-aware parse check is needed here instead. esbuild's own
    // transform (already a devDependency) parses without executing.
    expect(() => transformSync(content, { format: "esm", loader: "js" })).not.toThrow();
  });

  it("bundles the content script as a non-empty, parseable JS file", () => {
    const content = readFileSync(path.join(distDir, "content/index.js"), "utf8");
    expect(content.length).toBeGreaterThan(0);
    expect(() => new Script(content)).not.toThrow();
  });

  it("bundled background worker has no unresolved relative imports", () => {
    const content = readFileSync(path.join(distDir, "background/index.js"), "utf8");
    // A real esbuild bundle failure for an unresolved import throws at build
    // time (already asserted by beforeAll not throwing); this is a second,
    // static check that no bare `from "./...">` specifier survived
    // bundling, which would indicate esbuild treated something as external
    // rather than inlining it.
    expect(content).not.toMatch(/from\s+["']\.\.?\//);
  });

  it("bundled content script has no unresolved relative imports", () => {
    const content = readFileSync(path.join(distDir, "content/index.js"), "utf8");
    expect(content).not.toMatch(/from\s+["']\.\.?\//);
  });

  it("copies all three declared icon sizes as valid PNGs", () => {
    for (const size of [16, 48, 128]) {
      const iconPath = path.join(distDir, "icons", `icon${size}.png`);
      expect(existsSync(iconPath)).toBe(true);
      const buf = readFileSync(iconPath);
      const pngSignature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
      expect(buf.subarray(0, 8).equals(pngSignature)).toBe(true);
    }
  });
});

describe("6D-B manifest and test-hook isolation (spec §10.1, §18)", () => {
  it("production manifest: declarativeNetRequest required; tabs/webNavigation optional only; hosts unchanged", () => {
    const manifest = JSON.parse(readFileSync(path.join(distDir, "manifest.json"), "utf8"));
    expect(manifest.permissions).toContain("declarativeNetRequest");
    expect(manifest.permissions).not.toContain("tabs");
    expect(manifest.permissions).not.toContain("webNavigation");
    expect(manifest.optional_permissions).toEqual(["tabs", "webNavigation"]);
    expect(manifest.host_permissions).toEqual(["http://127.0.0.1:8420/*"]);
  });

  it("production background bundle contains no fill test hook", () => {
    expect(readFileSync(path.join(distDir, "background", "index.js"), "utf8")).not.toContain("__fillTest");
  });

  it("the test-hook build is separate and never overwrites the production build", () => {
    expect(sha(path.join(distDir, "background", "index.js"))).toBe(prodBackgroundHashBeforeHooksBuild);
    expect(readFileSync(path.join(hooksDir, "background", "index.js"), "utf8")).toContain("__fillTest");
  });

  it("only the test-hook manifest grants tabs/webNavigation up front (automation cannot answer a prompt)", () => {
    const manifest = JSON.parse(readFileSync(path.join(hooksDir, "manifest.json"), "utf8"));
    expect(manifest.permissions).toEqual(expect.arrayContaining(["declarativeNetRequest", "tabs", "webNavigation"]));
    expect(manifest.optional_permissions ?? []).toEqual([]);
  });
});
