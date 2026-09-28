import { build } from "esbuild";
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const extensionRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
// FILL_TEST_HOOKS=1 builds a separate, test-only variant (6D-B Task 2):
// it exposes the quarantine/sibling modules on the service worker for the
// browser proof suite and grants tabs/webNavigation up front, because
// automated Chrome cannot answer an optional-permission prompt. The
// production build (dist/extension) never contains the hook: the branch is
// compiled out by the __FILL_TEST_HOOKS__ define.
const testHooks = process.env.FILL_TEST_HOOKS === "1";
const outputRoot = resolve(extensionRoot, "dist", testHooks ? "extension-test-hooks" : "extension");
const define = { __FILL_TEST_HOOKS__: String(testHooks) };

export async function buildExtension() {
  await rm(outputRoot, { recursive: true, force: true });
  await mkdir(resolve(outputRoot, "background"), { recursive: true });
  await mkdir(resolve(outputRoot, "content"), { recursive: true });
  await mkdir(resolve(outputRoot, "popup"), { recursive: true });
  await mkdir(resolve(outputRoot, "content-bridge"), { recursive: true });
  await mkdir(resolve(outputRoot, "attachment-runner"), { recursive: true });
  await mkdir(resolve(outputRoot, "fill-page"), { recursive: true });
  await cp(resolve(extensionRoot, "manifest.json"), resolve(outputRoot, "manifest.json"));
  await cp(resolve(extensionRoot, "icons"), resolve(outputRoot, "icons"), { recursive: true });
  await cp(resolve(extensionRoot, "popup.html"), resolve(outputRoot, "popup.html"));
  await build({
    entryPoints: [resolve(extensionRoot, "src", "background", "index.ts")],
    bundle: true,
    format: "esm",
    platform: "browser",
    target: "chrome120",
    define,
    // Syntax-only minification folds the compiled-out `if (false)` test-hook
    // branch away entirely; identifiers and whitespace are left alone.
    minifySyntax: true,
    outfile: resolve(outputRoot, "background", "index.js"),
  });
  await build({
    entryPoints: [resolve(extensionRoot, "src", "content", "index.ts")],
    bundle: true,
    format: "iife",
    platform: "browser",
    target: "chrome120",
    outfile: resolve(outputRoot, "content", "index.js"),
  });
  await build({
    entryPoints: [resolve(extensionRoot, "src", "popup", "index.ts")],
    bundle: true,
    format: "esm",
    platform: "browser",
    target: "chrome120",
    outfile: resolve(outputRoot, "popup", "index.js"),
  });
  await build({
    entryPoints: [resolve(extensionRoot, "src", "content-bridge", "index.ts")],
    bundle: true,
    format: "iife",
    platform: "browser",
    target: "chrome120",
    outfile: resolve(outputRoot, "content-bridge", "index.js"),
  });
  await build({
    entryPoints: [resolve(extensionRoot, "src", "content", "attachment-runner.ts")],
    bundle: true,
    format: "iife",
    platform: "browser",
    target: "chrome120",
    outfile: resolve(outputRoot, "attachment-runner", "index.js"),
  });
  // 6D-B: the observer + executor + detections page bundle, injected on
  // demand into the user-activated tab in the ISOLATED world (spec §18).
  await build({
    entryPoints: [resolve(extensionRoot, "src", "fill", "page-bundle.ts")],
    bundle: true,
    format: "iife",
    platform: "browser",
    target: "chrome120",
    outfile: resolve(outputRoot, "fill-page", "index.js"),
  });
  if (testHooks) {
    const testManifest = JSON.parse(await readFile(resolve(outputRoot, "manifest.json"), "utf8"));
    testManifest.permissions = [...testManifest.permissions, ...(testManifest.optional_permissions ?? [])];
    delete testManifest.optional_permissions;
    await writeFile(resolve(outputRoot, "manifest.json"), JSON.stringify(testManifest, null, 2));
  }
  const manifest = JSON.parse(await readFile(resolve(outputRoot, "manifest.json"), "utf8"));
  if (manifest.background?.service_worker !== "background/index.js") {
    throw new Error("manifest service worker does not match the production bundle path");
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await buildExtension();
}
