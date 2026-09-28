// Bundle 6D-B certified adapter versions (spec §7.4). Mirrors
// product/fill_certification.py CATALOGUE: only these versions may run safe
// FILL. The Phase 3 greenhouse@1 / lever@1 autofill adapters are not
// certified and are never used here.

export interface TargetIdentity { tenantKey: string | null; atsJobId: string | null }

export interface CertifiedAdapter {
  id: string;
  adapterVersion: string;
  keyPrefix: string;
  detect(document: Document): boolean;
  // The application root, identified deterministically: exactly one match,
  // otherwise null (UNSUPPORTED_FORM: NO_APPLICATION_ROOT).
  applicationRoot(document: Document): Element | null;
  nonApplicationRule: { ruleId: string; hiddenNames: readonly string[] };
  targetIdentity(url: URL): TargetIdentity;
}

function single(document: Document, selector: string): Element | null {
  const matches = document.querySelectorAll(selector);
  return matches.length === 1 ? matches[0] : null;
}

export const greenhouseFill: CertifiedAdapter = {
  id: "greenhouse",
  adapterVersion: "greenhouse@2",
  keyPrefix: "gh",
  // Lever's form also carries id=application_form; its class is the more
  // specific marker, so a Lever page is never detected as Greenhouse.
  detect: (document) => document.querySelector("#application_form") !== null
    && document.querySelector("form.application-form") === null,
  applicationRoot: (document) => single(document, "#application_form"),
  nonApplicationRule: { ruleId: "greenhouse.site_state_hidden@1", hiddenNames: ["_method", "authenticity_token", "utf8"] },
  targetIdentity(url) {
    const match = /^\/([^/]+)\/jobs\/(\d+)/.exec(url.pathname);
    return { tenantKey: match ? match[1] : null, atsJobId: match ? match[2] : null };
  },
};

export const leverFill: CertifiedAdapter = {
  id: "lever",
  adapterVersion: "lever@2",
  keyPrefix: "lever",
  detect: (document) => document.querySelector("form.application-form") !== null,
  applicationRoot: (document) => single(document, "form.application-form"),
  nonApplicationRule: { ruleId: "lever.site_state_hidden@1", hiddenNames: ["_csrf"] },
  targetIdentity(url) {
    const match = /^\/([^/]+)\/([0-9a-f-]{36})/.exec(url.pathname);
    return { tenantKey: match ? match[1] : null, atsJobId: match ? match[2] : null };
  },
};

export const CERTIFIED_ADAPTERS: readonly CertifiedAdapter[] = [greenhouseFill, leverFill];

export function certifiedAdapterFor(document: Document): CertifiedAdapter | null {
  const detected = CERTIFIED_ADAPTERS.filter((adapter) => adapter.detect(document));
  return detected.length === 1 ? detected[0] : null;
}
