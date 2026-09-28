// Structural allowlist (spec §12.1): no code under extension/src/fill may
// click, submit, send keyboard/pointer/focus events, navigate, write markup
// or attributes, mutate the DOM tree, or call a DOM method through a
// computed member / Reflect. dispatchEvent is allowed only inside
// dispatchInput/dispatchChange. TypeScript 7 ships no JS compiler API, so
// the sources are parsed with the oxc parser bundled as rolldown/parseAst
// (ESTree + TS nodes).
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseAst } from "rolldown/parseAst";
import { describe, expect, it } from "vitest";

const here = path.dirname(fileURLToPath(import.meta.url));
const FILL_SRC = path.resolve(here, "..", "src", "fill");

const BANNED_CALLS = new Set(["click", "submit", "requestSubmit", "focus", "blur", "setAttribute", "removeAttribute",
  "setAttributeNS", "removeAttributeNS", "toggleAttribute", "setAttributeNode", "insertAdjacentHTML",
  "insertAdjacentElement", "insertAdjacentText", "appendChild", "removeChild", "insertBefore", "replaceChild",
  "append", "prepend", "replaceWith", "before", "after", "replaceChildren", "showPicker", "setRangeText",
  "setSelectionRange", "execCommand", "scrollIntoView", "write", "writeln"]);
const BANNED_EVENTS = new Set(["KeyboardEvent", "MouseEvent", "PointerEvent", "FocusEvent", "InputEvent", "TouchEvent",
  "WheelEvent", "SubmitEvent", "DragEvent", "CompositionEvent", "ClipboardEvent"]);
const BANNED_PROPERTIES = new Set(["innerHTML", "outerHTML", "outerText", "innerText", "srcdoc"]);
const BANNED_ASSIGN_TARGETS = new Set(["location", "href", "action", "formAction", "textContent", "nodeValue",
  "className", "id", "name"]);
const DISPATCHERS = new Set(["dispatchInput", "dispatchChange"]);

type Node = { type: string; [key: string]: unknown };

function isNode(value: unknown): value is Node {
  return typeof value === "object" && value !== null && typeof (value as Node).type === "string";
}

function propName(member: Node): string | null {
  const property = member.property as Node;
  if (member.computed) return null;
  return (property?.name as string) ?? null;
}

function objectName(member: Node): string | null {
  const object = member.object as Node;
  if (object?.type === "Identifier") return object.name as string;
  if (object?.type === "MemberExpression") return propName(object);
  return null;
}

export function violations(source: string, file = "snippet.ts"): string[] {
  const out: string[] = [];
  const functions: string[] = [];
  const visit = (node: Node) => {
    const named = (node.type === "FunctionDeclaration" || node.type === "FunctionExpression")
      && isNode(node.id) ? (node.id.name as string) : null;
    if (named) functions.push(named);
    if (node.type === "CallExpression" || node.type === "NewExpression") {
      const callee = node.callee as Node;
      if (callee.type === "MemberExpression") {
        const name = propName(callee);
        const object = objectName(callee);
        if (callee.computed && node.type === "CallExpression") out.push(`${file}: computed member call`);
        if (name && BANNED_CALLS.has(name)) out.push(`${file}: .${name}(`);
        if (name === "dispatchEvent" && !functions.some((f) => DISPATCHERS.has(f))) {
          out.push(`${file}: dispatchEvent outside dispatchInput/dispatchChange`);
        }
        if (object === "Reflect" && (name === "apply" || name === "construct")) out.push(`${file}: Reflect.${name}`);
        if (object === "location" && (name === "assign" || name === "replace" || name === "reload")) {
          out.push(`${file}: location.${name}(`);
        }
        if (name === "open" && (object === "window" || object === "self" || object === "globalThis")) {
          out.push(`${file}: window.open(`);
        }
        if (node.type === "NewExpression" && name && BANNED_EVENTS.has(name)) out.push(`${file}: new ${name}`);
      } else if (callee.type === "Identifier") {
        const name = callee.name as string;
        if (node.type === "NewExpression" && BANNED_EVENTS.has(name)) out.push(`${file}: new ${name}`);
        if (["open", "eval", "Function"].includes(name)) out.push(`${file}: ${name}(`);
      }
    }
    if (node.type === "MemberExpression") {
      const name = propName(node);
      if (name && BANNED_PROPERTIES.has(name)) out.push(`${file}: .${name}`);
    }
    if (node.type === "AssignmentExpression") {
      const left = node.left as Node;
      const onThis = left.type === "MemberExpression" && (left.object as Node)?.type === "ThisExpression";
      const name = onThis ? null : left.type === "Identifier" ? (left.name as string) : left.type === "MemberExpression" ? propName(left) : null;
      if (name && BANNED_ASSIGN_TARGETS.has(name)) out.push(`${file}: assignment to ${name}`);
    }
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach((v) => isNode(v) && visit(v));
      else if (isNode(value)) visit(value);
    }
    if (named) functions.pop();
  };
  visit(parseAst(source, { lang: "ts" }) as unknown as Node);
  return out;
}

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return sources(full);
    return name.endsWith(".ts") && !name.endsWith(".d.ts") ? [full] : [];
  });
}

describe("the fill executor allowlist (spec §12.1)", () => {
  it("covers every fill source file", () => {
    const names = sources(FILL_SRC).map((f) => path.basename(f));
    expect(names).toEqual(expect.arrayContaining(["executor.ts", "observer.ts", "settle.ts", "quarantine.ts",
      "siblings.ts", "canonical.ts", "hash.ts"]));
  });

  it("finds no forbidden construct in extension/src/fill", () => {
    const found = sources(FILL_SRC).flatMap((file) => violations(readFileSync(file, "utf-8"), path.basename(file)));
    expect(found).toEqual([]);
  });

  it("dispatchEvent appears only in the two dispatch primitives", () => {
    const executor = readFileSync(path.join(FILL_SRC, "executor.ts"), "utf-8");
    expect((executor.match(/\.dispatchEvent\(/g) ?? []).length).toBe(2);
  });

  it.each([
    ["el.click();", ".click("],
    ["form.submit();", ".submit("],
    ["form.requestSubmit();", ".requestSubmit("],
    ["el.focus();", ".focus("],
    ["el.blur();", ".blur("],
    ["new KeyboardEvent('keydown');", "new KeyboardEvent"],
    ["new window.MouseEvent('click');", "new MouseEvent"],
    ["new PointerEvent('pointerdown');", "new PointerEvent"],
    ["new FocusEvent('focus');", "new FocusEvent"],
    ["new InputEvent('input');", "new InputEvent"],
    ["function other(e: Element) { e.dispatchEvent(new Event('x')); }", "dispatchEvent outside"],
    ["el.innerHTML = '<b>x</b>';", ".innerHTML"],
    ["const x = el.outerHTML;", ".outerHTML"],
    ["el.insertAdjacentHTML('beforeend', 'x');", ".insertAdjacentHTML("],
    ["el.setAttribute('value', 'x');", ".setAttribute("],
    ["el.removeAttribute('disabled');", ".removeAttribute("],
    ["location = 'https://x';", "assignment to location"],
    ["window.location.href = 'https://x';", "assignment to href"],
    ["location.assign('https://x');", "location.assign("],
    ["location.replace('https://x');", "location.replace("],
    ["window.open('https://x');", "window.open("],
    ["form.action = 'https://x';", "assignment to action"],
    ["Reflect.apply(fn, el, []);", "Reflect.apply"],
    ["el[method]();", "computed member call"],
    ["root.appendChild(node);", ".appendChild("],
    ["el.name = 'x';", "assignment to name"],
  ])("flags %s (positive control)", (source, expected) => {
    expect(violations(source).join(" | ")).toContain(expected);
  });

  it("allows the dispatch primitives themselves", () => {
    const ok = "function dispatchInput(el: Element): void { el.dispatchEvent(new Event('input', { bubbles: true })); }";
    expect(violations(ok)).toEqual([]);
  });
});
