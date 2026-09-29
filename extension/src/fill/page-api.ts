// Bundle 6D-B page API contract (the injected page bundle implements it; the
// background calls it through chrome.scripting.executeScript). Types only,
// so the service worker never imports the page bundle itself.
import type { ActionOutcome, Envelope, PlanAction } from "./executor";
import type { ObservationV1 } from "./observation-types";
import type { Signals } from "../submit/signals";

export const FILL_PAGE_KEY = "__jobsearch_fill_page__";

export interface AttachmentPayload { bytes: number[]; filename: string; mediaType: string }

export interface FillPageApi {
  version: 1;
  detect(): { adapterId: string; adapterVersion: string } | null;
  rootPresent(adapterId: string): boolean;
  observe(adapterId: string, context: { canonicalUrl: string; origin: string }): Promise<ObservationV1>;
  execute(adapterId: string, action: PlanAction, envelope: Envelope | null,
          attachment: AttachmentPayload | null): Promise<ActionOutcome>;
  installDetections(runId: string): void;
  detected(): string[];
  enablePostFill(adapterId: string): void;
  // 6E-A (spec §10, §19): read-only submit proofs and signals, and the one
  // SUBMIT_CLICK on exactly one certified control with the bound fingerprint.
  findSubmitControl(certificationId: string, fingerprint: string): Promise<boolean>;
  clickSubmit(certificationId: string, fingerprint: string): Promise<"CLICKED" | "SUBMIT_CONTROL_MISSING">;
  signals(adapterId: string, certificationId: string, context: { boundUrl: string; confirmationUrl: string }): Signals;
  watchSubmitContent(adapterId: string): void;
  contentChanged(): boolean;
  allowSubmit(): void;
}
