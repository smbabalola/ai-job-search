// Bundle 6D-B read-only observer (spec §7.1). Builds observation v1 from the
// page in the ISOLATED world. It only READS: no property is assigned, no
// attribute written, no event dispatched, no element focused. Values are
// recorded only as fillValueHash digests (file inputs: the hash of the
// SHA-256 hex of their bytes), never as cleartext.

import type { CertifiedAdapter } from "./certified-adapters";
import { canonicalHash, sha256Hex, type Canonical } from "./canonical";
import { fillValueHash } from "./hash";
import type {
  ControlKind, ElementV1, FrameV1, IdentityV1, MultiStepIndicator, ObservationV1, OptionV1, ValueStateV1,
} from "./observation-types";

export interface ObserveContext { canonicalUrl: string; origin: string }

const TEXT_INPUT_KINDS: Record<string, ControlKind> = {
  text: "text", email: "email", tel: "tel", url: "url", number: "number", date: "date", checkbox: "checkbox",
  radio: "radio", file: "file", hidden: "hidden",
};
const ACTION_INPUT_TYPES = new Set(["submit", "image", "reset", "button"]);
const INTERACTIVE_ROLES = new Set(["textbox", "combobox", "listbox", "checkbox", "radio", "switch", "spinbutton",
  "slider", "searchbox", "option", "menuitemcheckbox", "menuitemradio"]);
// Stable ARIA identity only: state that pages flip while validating
// (aria-invalid, aria-expanded, ...) and id references are excluded.
const ARIA_IDENTITY = ["aria-label", "aria-required", "aria-readonly", "aria-disabled", "aria-multiselectable"];
const NEXT_TEXT = /^\s*(next|continue|next\s+step)\b/i;
const STEP_TEXT = /\bstep\s+\d+\s*(of|\/)\s*\d+\b/i;
const SKIP_TEXT_TAGS = new Set(["SELECT", "OPTION", "TEXTAREA", "INPUT", "BUTTON", "SCRIPT", "STYLE"]);

function collapse(text: string | null | undefined): string | null {
  const out = (text ?? "").replace(/\s+/g, " ").trim();
  return out === "" ? null : out;
}

// Visible text of a node, skipping nested controls (a wrapping label's
// select options are not its label).
function textOf(node: Node): string {
  let out = "";
  node.childNodes.forEach((child) => {
    if (child.nodeType === 3) out += child.textContent ?? "";
    else if (child.nodeType === 1 && !SKIP_TEXT_TAGS.has((child as Element).tagName)) out += " " + textOf(child);
  });
  return out;
}

function labelText(el: Element): string | null {
  const labels = (el as HTMLInputElement).labels;
  if (!labels || labels.length === 0) return null;
  return collapse(Array.from(labels).map((label) => textOf(label)).join(" "));
}

function questionText(el: Element, label: string | null): string | null {
  const document = el.ownerDocument;
  const labelledBy = el.getAttribute("aria-labelledby");
  if (labelledBy) {
    const text = collapse(labelledBy.split(/\s+/).map((id) => {
      const target = document.getElementById(id);
      return target ? textOf(target) : "";
    }).join(" "));
    if (text) return text;
  }
  return label ?? collapse(el.getAttribute("aria-label")) ?? collapse(el.getAttribute("placeholder"));
}

function ariaOf(el: Element): Record<string, string> {
  const out: Record<string, string> = {};
  const role = el.getAttribute("role");
  if (role) out.role = role;
  for (const name of ARIA_IDENTITY) {
    const value = el.getAttribute(name);
    if (value !== null) out[name] = value;
  }
  return out;
}

function isVisible(el: Element): boolean {
  if ((el as HTMLInputElement).type === "hidden") return false;
  const view = el.ownerDocument.defaultView;
  for (let node: Element | null = el; node; node = node.parentElement) {
    if ((node as HTMLElement).hidden) return false;
    const style = view?.getComputedStyle(node);
    if (style && (style.display === "none" || style.visibility === "hidden")) return false;
  }
  return true;
}

function formOwner(el: Element): string | null {
  const form = (el as HTMLInputElement).form;
  if (!form) return null;
  if (form.id) return form.id;
  const name = form.getAttribute("name");
  if (name) return `name:${name}`;
  return `form:${Array.from(el.ownerDocument.forms).indexOf(form)}`;
}

function controlKind(el: Element): ControlKind {
  const tag = el.tagName;
  if (tag === "TEXTAREA") return "textarea";
  if (tag === "SELECT") return (el as HTMLSelectElement).multiple ? "custom" : "select";
  if (tag === "INPUT") return TEXT_INPUT_KINDS[(el as HTMLInputElement).type] ?? "custom";
  return "custom";
}

function isActionControl(el: Element): boolean {
  if (el.tagName === "BUTTON") return true;
  return el.tagName === "INPUT" && ACTION_INPUT_TYPES.has((el as HTMLInputElement).type);
}

function intAttr(el: Element, name: string): number | null {
  const raw = el.getAttribute(name);
  if (raw === null || !/^\d+$/.test(raw.trim())) return null;
  return Number.parseInt(raw.trim(), 10);
}

function identityOf(el: Element, framePath: string): IdentityV1 {
  const label = labelText(el);
  const input = el as HTMLInputElement;
  const options: OptionV1[] = el.tagName === "SELECT"
    ? Array.from((el as HTMLSelectElement).options).map((o) => ({ option_value: o.value, option_text: collapse(o.text) ?? "" }))
    : [];
  return {
    tag: el.tagName.toLowerCase(),
    type: el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA" ? input.type : el.getAttribute("type"),
    name: el.getAttribute("name"),
    id: el.id || null,
    form_owner: formOwner(el),
    label,
    question: questionText(el, label),
    aria: ariaOf(el),
    required: Boolean(input.required) || el.getAttribute("aria-required") === "true",
    disabled: Boolean(input.disabled) || el.getAttribute("aria-disabled") === "true",
    readonly: Boolean(input.readOnly) || el.getAttribute("aria-readonly") === "true",
    visible: isVisible(el),
    options,
    accept: el.getAttribute("accept"),
    multiple: el.hasAttribute("multiple"),
    maxlength: intAttr(el, "maxlength"),
    pattern: el.getAttribute("pattern"),
    min: el.getAttribute("min"),
    max: el.getAttribute("max"),
    frame_path: framePath,
  };
}

async function valueStateOf(el: Element, kind: ControlKind): Promise<ValueStateV1> {
  if (kind === "file") {
    const files = Array.from((el as HTMLInputElement).files ?? []);
    if (files.length === 0) return { state: "BLANK" };
    const digests = await Promise.all(files.map(async (file) => sha256Hex(await file.arrayBuffer())));
    return { state: "NONBLANK", current_value_hash: await fillValueHash(digests.join("\n")) };
  }
  if (kind === "checkbox" || kind === "radio") {
    const input = el as HTMLInputElement;
    return input.checked ? { state: "NONBLANK", current_value_hash: await fillValueHash(input.value) }
      : { state: "BLANK" };
  }
  if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
    const value = (el as HTMLInputElement).value;
    return value === "" ? { state: "BLANK" } : { state: "NONBLANK", current_value_hash: await fillValueHash(value) };
  }
  // A custom widget or frame: its state is not observable as a value.
  return { state: "BLANK" };
}

function frameOrigin(frame: HTMLIFrameElement, fallback: string): string {
  const src = frame.getAttribute("src");
  if (!src || src === "about:blank") return fallback;
  try {
    return new URL(src, frame.ownerDocument.baseURI).origin;
  } catch {
    return "null";
  }
}

function inRoot(el: Element, root: Element | null): boolean {
  if (!root) return true;  // no root: nothing can be proved outside it (NO_APPLICATION_ROOT)
  if (root.contains(el)) return true;
  const form = (el as HTMLInputElement).form;
  return Boolean(form && (form === root || root.contains(form)));
}

function multiStepIndicators(document: Document, root: Element | null): MultiStepIndicator[] {
  const scope: ParentNode = root ?? document;
  const out: MultiStepIndicator[] = [];
  const actions = Array.from(scope.querySelectorAll("button, input"))
    .filter(isActionControl)
    .map((el) => (el.tagName === "BUTTON" ? el.textContent : (el as HTMLInputElement).value) ?? "");
  if (actions.some((text) => NEXT_TEXT.test(text))) out.push("NEXT_BUTTON");
  const stepText = STEP_TEXT.test((root ?? document.body)?.textContent ?? "");
  if (scope.querySelector('[aria-current="step"]') || stepText) out.push("STEP_INDICATOR");
  if (scope.querySelectorAll("[data-step], [data-page]").length >= 2) out.push("PAGINATED_FORM");
  return out;
}

function uniqueKey(used: Set<string>, base: string): string {
  let key = base;
  for (let n = 2; used.has(key); n++) key = `${base}#${n}`;
  used.add(key);
  return key;
}

export interface ScannedControl {
  pageFieldKey: string;
  element: Element;
  kind: ControlKind;
  identity: IdentityV1;
  classification: ElementV1["classification"];
  proof: ElementV1["proof"];
}

export interface PageScan {
  root: Element | null;
  frames: FrameV1[];
  iframes: HTMLIFrameElement[];
  controls: ScannedControl[];
  actionControls: Element[];
}

// Synchronous: the executor re-locates its target and re-checks the
// fingerprint in the same task as the write (spec §11.3 c.2). observe()
// builds the observation from exactly this scan.
export function scanControls(document: Document, adapter: CertifiedAdapter, origin: string): PageScan {
  const root = adapter.applicationRoot(document);
  const frames: FrameV1[] = [{ frame_path: "0", origin }];
  const iframes = Array.from(document.querySelectorAll("iframe"));
  iframes.forEach((frame, i) => frames.push({ frame_path: `0.${i}`, origin: frameOrigin(frame, origin) }));

  const candidates = new Set<Element>(Array.from(document.querySelectorAll("input, select, textarea, button")));
  if (root) {
    root.querySelectorAll("[role], [contenteditable]").forEach((el) => {
      const role = el.getAttribute("role");
      const editable = el.getAttribute("contenteditable");
      if ((role && INTERACTIVE_ROLES.has(role)) || (editable !== null && editable !== "false")) candidates.add(el);
    });
  }
  const ordered = Array.from(document.querySelectorAll("*")).filter((el) => candidates.has(el));

  const used = new Set<string>();
  const hidden = new Set(adapter.nonApplicationRule.hiddenNames);
  const controls: ScannedControl[] = [];
  const actionControls: Element[] = [];
  for (const el of ordered) {
    const application = inRoot(el, root);
    if (isActionControl(el)) {
      if (application) actionControls.push(el);
      continue;
    }
    const kind = controlKind(el);
    const name = el.getAttribute("name");
    const base = !application ? `page:${el.id || name || el.tagName.toLowerCase()}`
      : el.id ? `${adapter.keyPrefix}:${el.id}`
        : name ? `${adapter.keyPrefix}:name:${name}` : `${adapter.keyPrefix}:${el.tagName.toLowerCase()}`;
    let classification: ElementV1["classification"] = "APPLICATION";
    let proof: ElementV1["proof"] = null;
    if (!application) {
      classification = "NON_APPLICATION";
      proof = { kind: "OUTSIDE_APPLICATION_ROOT", rule: null };
    } else if (kind === "hidden" && name !== null && hidden.has(name)) {
      classification = "NON_APPLICATION";
      proof = { kind: "ADAPTER_NON_APPLICATION_RULE", rule: adapter.nonApplicationRule.ruleId };
    }
    controls.push({ pageFieldKey: uniqueKey(used, base), element: el, kind, identity: identityOf(el, "0"),
      classification, proof });
  }
  // A frame inside the application root is part of the application surface
  // the observer cannot read: reported as an application element in that
  // frame, so the server fails closed (CROSS_ORIGIN_APPLICATION_FRAME, or an
  // unlabeled/unsupported element), never silently ignored.
  for (const [i, frame] of iframes.entries()) {
    if (!root || !root.contains(frame)) continue;
    controls.push({ pageFieldKey: uniqueKey(used, `${adapter.keyPrefix}:frame:${i}`), element: frame, kind: "custom",
      identity: identityOf(frame, `0.${i}`), classification: "APPLICATION", proof: null });
  }
  return { root, frames, iframes, controls, actionControls };
}

export function submitControlPayload(el: Element): Canonical {
  const input = el as HTMLInputElement;
  return {
    tag: el.tagName.toLowerCase(), type: input.type ?? null, name: el.getAttribute("name"), id: el.id || null,
    text: collapse(el.tagName === "BUTTON" ? el.textContent : input.value), form_owner: formOwner(el),
    frame_path: "0",
  };
}

export async function observe(document: Document, adapter: CertifiedAdapter,
                              context: ObserveContext): Promise<ObservationV1> {
  const scan = scanControls(document, adapter, context.origin);
  const target = adapter.targetIdentity(new URL(context.canonicalUrl));
  const elements: ElementV1[] = [];
  for (const control of scan.controls) {
    elements.push({
      page_field_key: control.pageFieldKey, control_kind: control.kind, identity: control.identity,
      field_fingerprint: await canonicalHash("fill-field", "v1", control.identity as unknown as Canonical),
      classification: control.classification, proof: control.proof,
      value_state: control.element.tagName === "IFRAME" ? { state: "BLANK" } : await valueStateOf(control.element, control.kind),
    });
  }
  const submitControls = [];
  for (const el of scan.actionControls) {
    submitControls.push({ control_fingerprint: await canonicalHash("fill-submit-control", "v1", submitControlPayload(el)) });
  }
  return {
    schema_version: "fill-observation.v1",
    context: {
      canonical_url: context.canonicalUrl, origin: context.origin, adapter_id: adapter.id,
      adapter_version: adapter.adapterVersion, tenant_key: target.tenantKey, ats_job_id: target.atsJobId,
      frames: scan.frames, application_root_found: scan.root !== null,
      multi_step_indicators: multiStepIndicators(document, scan.root),
    },
    elements,
    submit_controls: submitControls,
  };
}
