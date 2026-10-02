// AST helpers for the 6E-A call-graph tests (not a test file itself).
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseAst } from "rolldown/parseAst";

const here = path.dirname(fileURLToPath(import.meta.url));
export const SRC = path.resolve(here, "..", "src");

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

export const rel = (file: string) => path.relative(SRC, file).split(path.sep).join("/");

export function walk(file: string, visit: (node: Node, within: string[]) => void): void {
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

