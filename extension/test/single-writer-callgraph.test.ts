// 6D-B Task 14 (spec §21, D19): the Phase 3 autofill writer is retired and
// extension/src/fill/executor.ts is the ONLY employer-page writer. Every DOM
// mutation in code that can run in or reach an employer page sits in the
// executor; executeAction is reachable only through the page bundle's
// `execute`, and the controller calls that only after the quarantine
// read-back and an issued intent envelope. src/popup is the extension's own
// UI document (not an employer page) and never injects scripts.
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseAst } from "rolldown/parseAst";
import { describe, expect, it } from "vitest";

const here = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.resolve(here, "..", "src");

type Node = { type: string; start: number; end: number; [key: string]: unknown };

const MUTATING_CALLS = new Set(["click", "submit", "requestSubmit", "focus", "blur", "setAttribute",
  "removeAttribute", "setAttributeNS", "removeAttributeNS", "toggleAttribute", "insertAdjacentHTML",
  "insertAdjacentElement", "insertAdjacentText", "appendChild", "removeChild", "insertBefore", "replaceChild",
  "append", "prepend", "replaceWith", "before", "after", "replaceChildren", "setRangeText", "execCommand",
  "dispatchEvent", "write", "writeln"]);
const MUTATING_PROPERTIES = new Set(["value", "checked", "files", "selectedIndex", "innerHTML", "outerHTML",
  "textContent", "innerText", "defaultValue", "defaultChecked"]);

function isNode(value: unknown): value is Node {
  return typeof value === "object" && value !== null && typeof (value as Node).type === "string";
}

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return files(full);
    return name.endsWith(".ts") && !name.endsWith(".d.ts") ? [full] : [];
  });
}

function rel(file: string): string {
  return path.relative(SRC, file).split(path.sep).join("/");
}

function unwrap(node: Node): Node {
  return node?.type === "TSNonNullExpression" ? unwrap(node.expression as Node) : node;
}

function propName(member: Node): string | null {
  return member.computed ? null : ((member.property as Node)?.name as string) ?? null;
}

interface Site { file: string; what: string; start: number; within: string[] }

// Every node visited with the names of its enclosing functions/methods/properties.
function walk(file: string, visit: (node: Node, within: string[]) => void): void {
  const ast = parseAst(readFileSync(file, "utf-8"), { lang: "ts" }) as unknown as Node;
  const recurse = (node: Node, within: string[]) => {
    visit(node, within);
    let name: string | null = null;
    if ((node.type === "FunctionDeclaration" || node.type === "FunctionExpression") && isNode(node.id)) {
      name = node.id.name as string;
    } else if ((node.type === "MethodDefinition" || node.type === "Property") && isNode(node.key)) {
      name = ((node.key as Node).name as string) ?? null;
    }
    const next = name ? [...within, name] : within;
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach((v) => isNode(v) && recurse(v, next));
      else if (isNode(value)) recurse(value, next);
    }
  };
  recurse(ast, []);
}

const PAGE_REACHABLE = files(SRC).filter((f) => !rel(f).startsWith("popup/"));

function mutationSites(): Site[] {
  const sites: Site[] = [];
  for (const file of PAGE_REACHABLE) {
    walk(file, (node, within) => {
      if (node.type === "CallExpression" && (node.callee as Node).type === "MemberExpression") {
        const callee = node.callee as Node;
        const name = propName(callee);
        if (name && MUTATING_CALLS.has(name)) sites.push({ file: rel(file), what: `.${name}(`, start: node.start, within });
        // A realm prototype setter invoked directly: descriptor.set.call(el, v)
        const object = unwrap(callee.object as Node);
        if (name === "call" && object?.type === "MemberExpression" && propName(object) === "set") {
          sites.push({ file: rel(file), what: "setter.call(", start: node.start, within });
        }
      }
      if (node.type === "AssignmentExpression" && (node.left as Node).type === "MemberExpression") {
        const left = node.left as Node;
        const name = propName(left);
        if (name && MUTATING_PROPERTIES.has(name) && (left.object as Node)?.type !== "ThisExpression") {
          sites.push({ file: rel(file), what: `.${name} =`, start: node.start, within });
        }
      }
    });
  }
  return sites;
}

describe("a single employer-page writer (spec §21)", () => {
  it("the legacy writer modules are gone", () => {
    const names = files(SRC).map(rel);
    for (const retired of ["content/content-script.ts", "content/attachment-runner.ts", "content/attachment-dom.ts",
      "content/attachment-source.ts", "content/snapshot-source.ts", "background/snapshot-projection.ts",
      "background/attachment.ts"]) {
      expect(names).not.toContain(retired);
    }
    const bundle = readFileSync(path.join(SRC, "content", "index.ts"), "utf-8");
    expect(bundle).not.toMatch(/snapshot|runContentScript|approveSuggestion/);
  });

  it("every DOM mutation reachable from a page lives in fill/executor.ts (plus 6E-A's one submit click)", () => {
    const all = mutationSites();
    // 6E-A (spec E13): the single SUBMIT_CLICK primitive is the one declared
    // exception -- `.click(` inside submitClick in submit/submit-executor.ts.
    const submitClick = all.filter((s) => s.file === "submit/submit-executor.ts");
    expect(submitClick.map((s) => `${s.what} in ${s.within.join(">")}`)).toEqual([".click( in submitClick"]);
    const sites = all.filter((s) => s.file !== "submit/submit-executor.ts");
    expect(sites.filter((s) => s.file !== "fill/executor.ts").map((s) => `${s.file}: ${s.what}`)).toEqual([]);
    // ... and inside the executor only in the six primitives; the fill executor never clicks.
    expect(sites.filter((s) => s.what === ".click(" || s.what === ".submit(" || s.what === ".requestSubmit("))
      .toEqual([]);
    const primitives = new Set(["setText", "setSelect", "setChecked", "setFilesLocal", "dispatchInput",
      "dispatchChange"]);
    const outside = sites.filter((s) => !s.within.some((w) => primitives.has(w)));
    expect(outside.map((s) => `${s.what} in ${s.within.join(">")}`)).toEqual([]);
    expect(sites.length).toBeGreaterThanOrEqual(6);
  });

  it("executeAction is reached only through the page bundle's execute", () => {
    const refs: string[] = [];
    for (const file of files(SRC)) {
      walk(file, (node, within) => {
        if (node.type === "Identifier" && node.name === "executeAction") refs.push(`${rel(file)}#${within.join(">")}`);
      });
    }
    expect(new Set(refs)).toEqual(new Set(["fill/executor.ts#executeAction",  // its declaration
      "fill/page-bundle.ts#",  // the import
      "fill/page-bundle.ts#install>execute"]));  // the one call
  });

  it("the controller executes only after the ruleset read-back and an issued envelope", () => {
    const calls: Site[] = [];
    const guards: Record<string, number[]> = { verify: [], intent: [], envelope: [], checkAborted: [] };
    const file = path.join(SRC, "fill", "run-controller.ts");
    walk(file, (node, within) => {
      if (node.type === "CallExpression" && (node.callee as Node).type === "MemberExpression") {
        const name = propName(node.callee as Node);
        if (name === "execute") calls.push({ file: "run-controller", what: "execute", start: node.start, within });
        if (within.includes("act") && (name === "verify" || name === "intent")) guards[name].push(node.start);
      }
      if (within.includes("act") && node.type === "CallExpression" && (node.callee as Node).type === "MemberExpression"
          && propName(node.callee as Node) === "checkAborted") guards.checkAborted.push(node.start);
      if (within.includes("act") && node.type === "IfStatement") {
        const test = readFileSync(file, "utf-8").slice((node.test as Node).start, (node.test as Node).end);
        if (test === "!intent.envelope") guards.envelope.push(node.start);
      }
    });
    expect(calls).toHaveLength(1);
    const [call] = calls;
    expect(call.within).toEqual(["FillRunController", "act"].filter((n) => call.within.includes(n)));
    expect(call.within).toContain("act");
    for (const guard of ["verify", "intent", "envelope", "checkAborted"]) {
      expect(guards[guard].length, guard).toBeGreaterThan(0);
      expect(Math.min(...guards[guard]), guard).toBeLessThan(call.start);
    }
    // Other callers of `.execute(` exist only as the port forwarder in the background.
    const others: string[] = [];
    for (const f of files(SRC).filter((x) => rel(x) !== "fill/run-controller.ts")) {
      walk(f, (node, within) => {
        if (node.type === "CallExpression" && (node.callee as Node).type === "MemberExpression"
            && propName(node.callee as Node) === "execute") others.push(`${rel(f)}#${within.join(">")}`);
      });
    }
    expect(others).toEqual(["background/fill-wiring.ts#pagePort>execute>func"]);
  });

  it("the popup never injects into a page", () => {
    for (const file of files(path.join(SRC, "popup"))) {
      expect(readFileSync(file, "utf-8")).not.toMatch(/scripting\.executeScript|executeScript\(/);
    }
  });
});
