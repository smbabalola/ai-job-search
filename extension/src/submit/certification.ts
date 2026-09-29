// Bundle 6E-A submit certification (spec §9). Mirrors
// product/submit_certification.py; tests/fixtures/submit/egress_vectors.json
// pins both sides to byte-identical resolved regexes.

export type SubmitStatus = "FIXTURE_CERTIFIED" | "LIVE_CERTIFIED";

export interface EgressEntry { id: string; methods: readonly string[]; types: readonly string[]; template: string }

export interface ResolvedEgress { id: string; methods: string[]; types: string[]; regex: string }

export interface SubmitCertification {
  certificationId: string;
  adapterId: string;
  adapterVersion: string;
  status: SubmitStatus;
  liveEvidence: string | null;
  submitControlSelector: string;
  egress: readonly EgressEntry[];
  successSelectors: readonly string[];
  confirmationTemplate: string;
  failureSelectors: readonly string[];
  challengeSelectors: readonly string[];
  failureSignalProvesNotSubmitted: boolean;
}

export const GREENHOUSE_SUBMIT: SubmitCertification = {
  certificationId: "greenhouse@2/submit@1",
  adapterId: "greenhouse",
  adapterVersion: "greenhouse@2",
  status: "FIXTURE_CERTIFIED",
  liveEvidence: null,
  submitControlSelector: "#application_form [type=submit]",
  egress: [
    { id: "E1_SUBMIT", methods: ["post"], types: ["main_frame", "xmlhttprequest"], template: "{origin}/{tenant}/jobs/{job}" },
    { id: "E2_CONFIRM", methods: ["get"], types: ["main_frame", "xmlhttprequest"],
      template: "{origin}/{tenant}/jobs/{job}/confirmation(\\?.*)?" },
    { id: "E3_RENDER", methods: ["get"], types: ["stylesheet", "script", "image", "font"], template: "{origin}/.*" },
    { id: "C1_RECAPTCHA", methods: ["get", "post"], types: ["script", "sub_frame", "xmlhttprequest", "image"],
      template: "https://www\\.(google|gstatic|recaptcha)\\.(com|net)/recaptcha/.*" },
  ],
  successSelectors: ["#application_confirmation"],
  confirmationTemplate: "{origin}/{tenant}/jobs/{job}/confirmation",
  failureSelectors: ["#error_explanation"],
  challengeSelectors: ['iframe[src*="recaptcha/api2/bframe"]', 'iframe[title*="challenge" i]', "[data-submit-challenge]"],
  failureSignalProvesNotSubmitted: true,
};

export const SUBMIT_CERTIFICATIONS: readonly SubmitCertification[] = [GREENHOUSE_SUBMIT];

export function submitCertificationFor(adapterId: string, adapterVersion: string): SubmitCertification | null {
  return SUBMIT_CERTIFICATIONS.find((c) => c.adapterId === adapterId && c.adapterVersion === adapterVersion) ?? null;
}

export function certificationById(certificationId: string): SubmitCertification | null {
  return SUBMIT_CERTIFICATIONS.find((c) => c.certificationId === certificationId) ?? null;
}

// Python's re.escape character set exactly (so both languages resolve to the
// same bytes); every escaped character is a literal in RE2 and JS RegExp.
const PY_RE_SPECIAL = new Set("()[]{}?*+-|^$\\.&~# \t\n\r\v\f");

export function pyReEscape(value: string): string {
  let out = "";
  for (const ch of value) out += PY_RE_SPECIAL.has(ch) ? "\\" + ch : ch;
  return out;
}

export function resolveEgress(cert: SubmitCertification, origin: string, tenantKey: string,
                              atsJobId: string): ResolvedEgress[] {
  return cert.egress.map((e) => ({
    id: e.id, methods: [...e.methods], types: [...e.types],
    regex: "^" + e.template.split("{origin}").join(pyReEscape(origin)).split("{tenant}").join(pyReEscape(tenantKey))
      .split("{job}").join(pyReEscape(atsJobId)) + "$",
  }));
}

export function confirmationUrl(cert: SubmitCertification, origin: string, tenantKey: string, atsJobId: string): string {
  return cert.confirmationTemplate.split("{origin}").join(origin).split("{tenant}").join(tenantKey)
    .split("{job}").join(atsJobId);
}
