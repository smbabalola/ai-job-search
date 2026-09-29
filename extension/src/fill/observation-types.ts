// Bundle 6D-B observation v1 wire format (spec §7.1). The exact shape
// product/fill_observation.py validates; no cleartext value ever appears.

export type ControlKind = "text" | "email" | "tel" | "url" | "number" | "date" | "textarea" | "select" | "checkbox"
  | "radio" | "file" | "hidden" | "custom";
export type MultiStepIndicator = "NEXT_BUTTON" | "STEP_INDICATOR" | "PAGINATED_FORM";
export type NonApplicationProofKind = "OUTSIDE_APPLICATION_ROOT" | "ADAPTER_NON_APPLICATION_RULE"
  | "SUBMIT_CLASS_CONTROL";

export interface FrameV1 { frame_path: string; origin: string }

export interface ContextV1 {
  canonical_url: string;
  origin: string;
  adapter_id: string;
  adapter_version: string;
  tenant_key: string | null;
  ats_job_id: string | null;
  frames: FrameV1[];
  application_root_found: boolean;
  multi_step_indicators: MultiStepIndicator[];
}

export interface OptionV1 { option_value: string; option_text: string }

export interface IdentityV1 {
  [key: string]: string | number | boolean | null | OptionV1[] | Record<string, string>;
  tag: string;
  type: string | null;
  name: string | null;
  id: string | null;
  form_owner: string | null;
  label: string | null;
  question: string | null;
  aria: Record<string, string>;
  required: boolean;
  disabled: boolean;
  readonly: boolean;
  visible: boolean;
  options: OptionV1[];
  accept: string | null;
  multiple: boolean;
  maxlength: number | null;
  pattern: string | null;
  min: string | null;
  max: string | null;
  frame_path: string;
}

export type ValueStateV1 = { state: "BLANK" } | { state: "NONBLANK"; current_value_hash: string };

export interface ElementV1 {
  page_field_key: string;
  control_kind: ControlKind;
  identity: IdentityV1;
  field_fingerprint: string;
  classification: "APPLICATION" | "NON_APPLICATION";
  proof: null | { kind: NonApplicationProofKind; rule: string | null };
  value_state: ValueStateV1;
}

export interface ObservationV1 {
  schema_version: "fill-observation.v1";
  context: ContextV1;
  elements: ElementV1[];
  submit_controls: { control_fingerprint: string }[];
}
