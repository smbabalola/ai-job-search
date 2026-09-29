// 6E-A spec E13/§19: exactly one submit click exists. It is submitClick in
// submit/submit-executor.ts, reached only through the page bundle's
// clickSubmit (which re-finds the one certified control by its bound
// fingerprint), and clickSubmit is invoked only by the SubmitController
// (asserted in submit-controller.test.ts once the controller exists).
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseAst } from "rolldown/parseAst";
import { describe, expect, it } from "vitest";

const here = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.resolve(here, "..", "src");

type Node = { type: string; start: number; end: number; [key: string]: unknown };

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

const rel = (file: string) => path.relative(SRC, file).split(path.sep).join("/");

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

export function identifierRefs(name: string): string[] {
  const refs: string[] = [];
  for (const file of files(SRC)) {
    walk(file, (node, within) => {
      if (node.type === "Identifier" && node.name === name) refs.push(`${rel(file)}#${within.join(">")}`);
    });
  }
  return refs;
}

describe("one SUBMIT_CLICK (6E-A spec E13)", () => {
  it("submitClick is declared in submit-executor.ts and called only by the page bundle's clickSubmit", () => {
    expect(new Set(identifierRefs("submitClick"))).toEqual(new Set([
      "submit/submit-executor.ts#submitClick",  // its declaration
      "fill/page-bundle.ts#",  // the import
      "fill/page-bundle.ts#install>clickSubmit",  // the one call
    ]));
  });

  it("the submit executor is nothing but the click", () => {
    const source = readFileSync(path.join(SRC, "submit", "submit-executor.ts"), "utf-8");
    const code = source.split("\n").filter((l) => !l.trim().startsWith("//") && l.trim()).join("\n");
    expect(code).toMatch(/export function submitClick\(el: Element\): void \{\s*\(el as HTMLElement\)\.click\(\);\s*\}/);
    expect(code.match(/export /g)).toHaveLength(1);
  });

  it("clickSubmit exists only in the page API contract, the page bundle and the SubmitController's port", () => {
    const allowed = new Set(["fill/page-api.ts", "fill/page-bundle.ts", "background/fill-wiring.ts",
      "submit/submit-controller.ts"]);
    const files_ = new Set(identifierRefs("clickSubmit").map((r) => r.split("#")[0]));
    expect([...files_].filter((f) => !allowed.has(f))).toEqual([]);
  });
});
