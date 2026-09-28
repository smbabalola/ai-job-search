// Bundle 6D-B network quarantine (spec §10.2-10.3, §10.8). The rulesets are
// built here and ONLY here, and verification is exact: the extension's
// session rules with fill ids must hash to exactly what the builder
// produced for this tab and employer host, and no rule of this extension
// (session or dynamic) may allow or rewrite traffic. Anything else means
// the quarantine is not verified and no action may execute.

type Rule = chrome.declarativeNetRequest.Rule;
type ResourceType = chrome.declarativeNetRequest.ResourceType;
type RequestMethod = chrome.declarativeNetRequest.RequestMethod;

export const QUARANTINE_RESOURCE_TYPES_V1 = [
  "main_frame", "sub_frame", "stylesheet", "script", "image", "font", "object", "xmlhttprequest", "ping",
  "csp_report", "media", "websocket", "webtransport", "webbundle", "other",
] as const;

// PRELOAD needs two Q1 rules: a single DNR condition cannot express
// "these methods, OR these types for any method".
export const FILL_RULE_IDS = {
  Q1_PRE_METHODS: 9101,
  Q1_PRE_TYPES: 9102,
  Q1_TOTAL: 9111,
  Q2: 9121,
} as const;
const ALL_FILL_IDS: number[] = Object.values(FILL_RULE_IDS);

// High priority as defence in depth; verification separately refuses any
// non-block rule of this extension, which is what actually rules out an
// allow rule winning.
const PRIORITY = 1000;
const PRELOAD_BLOCKED_METHODS: RequestMethod[] = ["post", "put", "patch", "delete", "connect", "options"] as RequestMethod[];
const PRELOAD_BLOCKED_TYPES: ResourceType[] = ["websocket", "webtransport", "ping"] as ResourceType[];
const TAB_ID_NONE = -1;

export type RulesetKind = "PRELOAD" | "TOTAL";

export interface RulesApi {
  getSessionRules(): Promise<Rule[]>;
  getDynamicRules(): Promise<Rule[]>;
  updateSessionRules(options: { removeRuleIds?: number[]; addRules?: Rule[] }): Promise<void>;
}

function allTypes(): ResourceType[] {
  return [...QUARANTINE_RESOURCE_TYPES_V1] as ResourceType[];
}

function q2(employerHost: string): Rule {
  return {
    id: FILL_RULE_IDS.Q2, priority: PRIORITY, action: { type: "block" as chrome.declarativeNetRequest.RuleActionType },
    condition: { tabIds: [TAB_ID_NONE], initiatorDomains: [employerHost], resourceTypes: allTypes() },
  };
}

export function buildPreloadRules(tabId: number, employerHost: string): Rule[] {
  const block = { type: "block" as chrome.declarativeNetRequest.RuleActionType };
  return [
    { id: FILL_RULE_IDS.Q1_PRE_METHODS, priority: PRIORITY, action: block,
      condition: { tabIds: [tabId], requestMethods: PRELOAD_BLOCKED_METHODS, resourceTypes: allTypes() } },
    { id: FILL_RULE_IDS.Q1_PRE_TYPES, priority: PRIORITY, action: block,
      condition: { tabIds: [tabId], resourceTypes: PRELOAD_BLOCKED_TYPES } },
    q2(employerHost),
  ];
}

export function buildTotalRules(tabId: number, employerHost: string): Rule[] {
  return [
    // No requestMethods: every method. resourceTypes is explicit because an
    // omitted list would exclude main_frame.
    { id: FILL_RULE_IDS.Q1_TOTAL, priority: PRIORITY, action: { type: "block" as chrome.declarativeNetRequest.RuleActionType },
      condition: { tabIds: [tabId], resourceTypes: allTypes() } },
    q2(employerHost),
  ];
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      const v = (value as Record<string, unknown>)[key];
      if (v !== undefined) out[key] = canonical(v);
    }
    return out;
  }
  return value;
}

export async function canonicalRulesetHash(rules: Rule[]): Promise<string> {
  const sorted = [...rules].sort((a, b) => a.id - b.id).map(canonical);
  const bytes = new TextEncoder().encode(JSON.stringify({ schema: "fill-quarantine-ruleset", schema_version: "v1", rules: sorted }));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return "sha256:" + [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function chromeApi(): RulesApi {
  return chrome.declarativeNetRequest as unknown as RulesApi;
}

export async function installRuleset(kind: RulesetKind, tabId: number, employerHost: string,
                                     api: RulesApi = chromeApi()): Promise<{ rulesetHash: string }> {
  const rules = kind === "PRELOAD" ? buildPreloadRules(tabId, employerHost) : buildTotalRules(tabId, employerHost);
  // One atomic update: every fill rule is replaced, so PRELOAD rules never
  // linger under TOTAL (verification would refuse them if they did).
  await api.updateSessionRules({ removeRuleIds: ALL_FILL_IDS, addRules: rules });
  return { rulesetHash: await canonicalRulesetHash(rules) };
}

export async function verifyRuleset(expectedHash: string, api: RulesApi = chromeApi()): Promise<boolean> {
  const [session, dynamic] = await Promise.all([api.getSessionRules(), api.getDynamicRules()]);
  if ([...session, ...dynamic].some((rule) => rule.action.type !== "block")) {
    return false;  // an allow/allowAllRequests/redirect/modify rule could defeat the block
  }
  const fill = session.filter((rule) => ALL_FILL_IDS.includes(rule.id));
  return (await canonicalRulesetHash(fill)) === expectedHash;
}

export async function removeRuleset(api: RulesApi = chromeApi()): Promise<void> {
  await api.updateSessionRules({ removeRuleIds: ALL_FILL_IDS });
}
