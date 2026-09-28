// Bundle 6D-B executor (spec §11.3 c, §12.1-12.2). ISOLATED world, the
// realm's own prototype setters, and exactly six mutation primitives:
// SET_TEXT, SET_SELECT, SET_CHECKED, SET_FILES_LOCAL, DISPATCH_INPUT and
// DISPATCH_CHANGE. Never a click, key, focus, blur, navigation, attribute or
// markup write (extension/test/fill-executor-allowlist.test.ts enforces it).
//
// One action = one synchronous task that re-locates the target, re-checks
// its fingerprint and value precondition and applies one primitive group;
// then settle; then readback. A non-blank target that is not already equal
// is never overwritten.

import { canonicalHashSync, sha256Hex, type Canonical } from "./canonical";
import type { CertifiedAdapter } from "./certified-adapters";
import { fillValueHash, fillValueHashSync } from "./hash";
import { scanControls } from "./observer";
import { isRelevantMutation, settle, type SettleResult } from "./settle";

export type ActionKind = "WRITE" | "ATTACH_LOCAL" | "OMIT" | "IGNORE_NON_APPLICATION";

export interface PlanDocument { document_version_id: string; sha256: string; byte_length: number; filename: string;
  media_type: string }

export interface PlanAction {
  page_field_key: string;
  field_fingerprint: string;
  action_kind: ActionKind;
  rendered_value_hash: string | null;
  document: PlanDocument | null;
}

export interface Envelope {
  envelope_id: string | null;
  action_kind: ActionKind;
  rendered_value: string | null;
  rendered_value_hash: string | null;
  document: (PlanDocument & { document_kind?: string }) | null;
}

export type Outcome = "WRITTEN_VERIFIED" | "NOOP_ALREADY_EQUAL" | "OMIT_VERIFIED" | "IGNORE_RECORDED"
  | "ATTACH_LOCAL_VERIFIED" | "READBACK_MISMATCH" | "TARGET_CHANGED" | "FIELD_VALIDITY_FAILED"
  | "FIELD_VALUE_REVERTED" | "ATTACH_LOCAL_MISMATCH";

export interface ActionOutcome {
  outcome: Outcome;
  readbackHash?: string;
  // Why a failure happened (local evidence, e.g. "not_blank", "STRUCTURE_UNSTABLE").
  detail?: string;
  // Whether a primitive was applied (the MAY_HAVE_WRITTEN window was entered).
  mutated: boolean;
}

const TEXT_KINDS = new Set(["text", "email", "tel", "url", "number", "date", "textarea"]);
const VALIDITY_FAILURES: (keyof ValidityState)[] = ["typeMismatch", "patternMismatch", "tooLong", "tooShort",
  "rangeOverflow", "rangeUnderflow", "stepMismatch", "badInput"];

// ---- the six primitives ------------------------------------------------------------------------

function realm(el: Element) {
  return el.ownerDocument.defaultView!;
}

function setText(el: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const win = realm(el);
  const proto = el.tagName === "TEXTAREA" ? win.HTMLTextAreaElement.prototype : win.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
}

function setSelect(el: HTMLSelectElement, value: string): void {
  Object.getOwnPropertyDescriptor(realm(el).HTMLSelectElement.prototype, "value")!.set!.call(el, value);
}

function setChecked(el: HTMLInputElement, checked: boolean): void {
  Object.getOwnPropertyDescriptor(realm(el).HTMLInputElement.prototype, "checked")!.set!.call(el, checked);
}

function setFilesLocal(el: HTMLInputElement, files: FileList): void {
  Object.getOwnPropertyDescriptor(realm(el).HTMLInputElement.prototype, "files")!.set!.call(el, files);
}

function dispatchInput(el: Element): void {
  el.dispatchEvent(new (realm(el).Event)("input", { bubbles: true }));
}

function dispatchChange(el: Element): void {
  el.dispatchEvent(new (realm(el).Event)("change", { bubbles: true }));
}

// ---- helpers -----------------------------------------------------------------------------------

function currentHashSync(el: Element, kind: string): string | null {
  if (kind === "checkbox" || kind === "radio") {
    const input = el as HTMLInputElement;
    return input.checked ? fillValueHashSync(input.value) : null;
  }
  const value = (el as HTMLInputElement).value;
  return value === "" ? null : fillValueHashSync(value);
}

function radioGroup(el: HTMLInputElement): HTMLInputElement[] {
  const scope: ParentNode = el.form ?? el.ownerDocument;
  return Array.from(scope.querySelectorAll<HTMLInputElement>("input[type=radio]"))
    .filter((radio) => radio.name === el.name && radio.form === el.form);
}

function validityFailure(el: Element): string | null {
  const validity = (el as HTMLInputElement).validity;
  if (!validity) return null;
  return VALIDITY_FAILURES.find((flag) => validity[flag]) ?? null;
}

async function filesHash(files: FileList | null): Promise<{ hash: string | null; digests: string[] }> {
  const list = Array.from(files ?? []);
  if (list.length === 0) return { hash: null, digests: [] };
  const digests = await Promise.all(list.map(async (file) => sha256Hex(await file.arrayBuffer())));
  return { hash: await fillValueHash(digests.join("\n")), digests };
}

export interface ExecuteOptions {
  // Builds a FileList in the page realm (DataTransfer in Chrome); injected so
  // the executor itself holds no page-specific construction.
  makeFileList?: (file: File) => FileList;
  settleRoot?: Node;
  isRelevant?: (record: MutationRecord) => boolean;
  settleTiming?: { quietMs: number; capMs: number };
}

function defaultFileList(el: Element, file: File): FileList {
  const transfer = new (realm(el) as unknown as { DataTransfer: typeof DataTransfer }).DataTransfer();
  transfer.items.add(file);
  return transfer.files;
}

// ---- the action ----------------------------------------------------------------------------------

export async function executeAction(document: Document, adapter: CertifiedAdapter, action: PlanAction,
                                    envelope: Envelope | null, attachment: File | null = null,
                                    options: ExecuteOptions = {}): Promise<ActionOutcome> {
  // The local bytes are hashed first (no page state is read, so awaiting here
  // opens no window between the page check and the write below).
  const attachmentDigest = attachment ? await sha256Hex(await attachment.arrayBuffer()) : null;
  // ---- one synchronous task: locate, re-check, precondition, one primitive group ----
  const scan = scanControls(document, adapter, document.location?.origin ?? "null");
  const control = scan.controls.find((c) => c.pageFieldKey === action.page_field_key);
  if (!control || !control.element.isConnected
      || canonicalHashSync("fill-field", "v1", control.identity as unknown as Canonical) !== action.field_fingerprint) {
    return { outcome: "TARGET_CHANGED", detail: "fingerprint", mutated: false };
  }
  const el = control.element as HTMLInputElement;
  const kind = control.kind;
  if (action.action_kind === "IGNORE_NON_APPLICATION") return { outcome: "IGNORE_RECORDED", mutated: false };
  if (action.action_kind === "OMIT") {
    const blank = kind === "file" ? (el.files?.length ?? 0) === 0 : currentHashSync(el, kind) === null;
    return blank ? { outcome: "OMIT_VERIFIED", mutated: false }
      : { outcome: "READBACK_MISMATCH", detail: "omit_not_blank", mutated: false };
  }
  if (el.disabled || el.readOnly) return { outcome: "TARGET_CHANGED", detail: "not_editable", mutated: false };

  if (action.action_kind === "WRITE") {
    if (!envelope || envelope.action_kind !== "WRITE" || envelope.rendered_value === null
        || envelope.rendered_value_hash !== action.rendered_value_hash
        || fillValueHashSync(envelope.rendered_value) !== action.rendered_value_hash) {
      return { outcome: "READBACK_MISMATCH", detail: "envelope_mismatch", mutated: false };
    }
    if (!(TEXT_KINDS.has(kind) || kind === "select" || kind === "checkbox" || kind === "radio")) {
      return { outcome: "TARGET_CHANGED", detail: "ineligible_control", mutated: false };
    }
    const current = currentHashSync(el, kind);
    if (current !== null && current === action.rendered_value_hash) return { outcome: "NOOP_ALREADY_EQUAL", readbackHash: current,
      mutated: false };
    if (current !== null) return { outcome: "TARGET_CHANGED", detail: "not_blank", mutated: false };
    const value = envelope.rendered_value;
    if (kind === "select") {
      const option = Array.from((el as unknown as HTMLSelectElement).options).find((o) => o.value === value);
      if (!option) return { outcome: "TARGET_CHANGED", detail: "option_missing", mutated: false };
      setSelect(el as unknown as HTMLSelectElement, value);
    } else if (kind === "checkbox" || kind === "radio") {
      if (el.value !== value) return { outcome: "TARGET_CHANGED", detail: "option_missing", mutated: false };
      setChecked(el, true);
    } else {
      setText(el, value);
    }
    dispatchInput(el);
    dispatchChange(el);
  } else {
    // ATTACH_LOCAL: the exact approved bytes, verified before anything is set.
    if (kind !== "file" || !envelope?.document || !attachment || !action.document
        || envelope.document.sha256 !== action.document.sha256 || attachmentDigest !== action.document.sha256) {
      return { outcome: "ATTACH_LOCAL_MISMATCH", detail: "no_approved_document", mutated: false };
    }
    if ((el.files?.length ?? 0) > 0) return { outcome: "TARGET_CHANGED", detail: "not_blank", mutated: false };
    setFilesLocal(el, (options.makeFileList ?? ((file: File) => defaultFileList(el, file)))(attachment));
    dispatchChange(el);
  }

  // ---- settle, then read back ----
  const settled: SettleResult = await settle(options.settleRoot ?? scan.root ?? document,
    options.isRelevant ?? isRelevantMutation, options.settleTiming);
  if (settled === "STRUCTURE_UNSTABLE") return { outcome: "TARGET_CHANGED", detail: "STRUCTURE_UNSTABLE", mutated: true };
  const after = scanControls(document, adapter, document.location?.origin ?? "null").controls
    .find((c) => c.pageFieldKey === action.page_field_key);
  if (!after || after.element !== el || !el.isConnected) {
    return { outcome: "TARGET_CHANGED", detail: "target_replaced", mutated: true };
  }
  if (action.action_kind === "ATTACH_LOCAL") {
    const { hash, digests } = await filesHash(el.files);
    const file = el.files?.[0];
    const doc = action.document!;
    const exact = digests.length === 1 && digests[0] === doc.sha256 && file?.name === doc.filename
      && file?.size === doc.byte_length && file?.type === doc.media_type;
    return exact ? { outcome: "ATTACH_LOCAL_VERIFIED", readbackHash: hash!, mutated: true }
      : { outcome: "ATTACH_LOCAL_MISMATCH", readbackHash: hash ?? undefined, mutated: true };
  }
  const invalid = validityFailure(el);
  if (invalid) return { outcome: "FIELD_VALIDITY_FAILED", detail: invalid, mutated: true };
  const readback = currentHashSync(el, kind);
  if (kind === "radio" && radioGroup(el).some((radio) => radio !== el && radio.checked)) {
    return { outcome: "READBACK_MISMATCH", detail: "radio_group", readbackHash: readback ?? undefined, mutated: true };
  }
  if (readback === action.rendered_value_hash) return { outcome: "WRITTEN_VERIFIED", readbackHash: readback ?? undefined, mutated: true };
  return readback === null ? { outcome: "FIELD_VALUE_REVERTED", mutated: true }
    : { outcome: "READBACK_MISMATCH", readbackHash: readback, mutated: true };
}
