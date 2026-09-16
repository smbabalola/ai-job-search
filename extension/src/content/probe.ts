import type { Adapter } from "../adapters/types";

export interface ProbedField {
  pageFieldKey: string;
  normalizedFieldType: string;
}

export interface ProbeResult {
  atsAdapterId: string;
  atsAdapterVersion: string;
  detectedFields: ProbedField[];
  normalizedFieldTypes: string[];
}

// Snapshot-free adapter/field discovery: detect -> scan -> classify only.
// Deliberately never calls adapter.map() (which requires a CandidateSnapshot
// that does not exist yet at probe time), never writes to the DOM, and
// never sends a chrome.runtime message itself — the caller (content/index.ts)
// is responsible for reporting the result. This is what makes probing safe
// to run before any handoff session exists or any candidate data has been
// requested (design spec Section 6.2 step 7 / plan Task 8).
export function probePage(document: Document, adapters: Adapter[]): ProbeResult | null {
  const adapter = adapters.find((a) => a.detect(document));
  if (!adapter) return null;

  const fields = adapter.scan(document);
  const detectedFields: ProbedField[] = [];
  const normalizedFieldTypes = new Set<string>();

  for (const field of fields) {
    const decision = adapter.classify(field);
    detectedFields.push({
      pageFieldKey: field.pageFieldKey,
      normalizedFieldType: decision.normalizedFieldType,
    });
    if (decision.behavior === "autofill" || decision.behavior === "suggest") {
      // ask/never fields are never worth requesting candidate data for —
      // there is nothing the snapshot endpoint could return that this
      // field would ever be allowed to use.
      normalizedFieldTypes.add(decision.normalizedFieldType);
    }
  }

  return {
    atsAdapterId: adapter.id,
    atsAdapterVersion: adapter.version,
    detectedFields,
    normalizedFieldTypes: [...normalizedFieldTypes],
  };
}
