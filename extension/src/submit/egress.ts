// Bundle 6E-A SUBMIT egress (spec §9.4, E4, J4/J5). TOTAL is never lifted:
// the certified allow rules are ADDED above it (ids 9201-9209, priority
// 2000, tab-scoped, exact method/type/regex), so everything the adapter's
// certification does not name stays blocked by the unchanged TOTAL rules,
// and Q2 keeps blocking the employer's service worker. Allow rules are built
// here and ONLY here. Verification is exact, and restoreTotal removes every
// allow and re-verifies the TOTAL hash recorded at FILL.

import {
  FILL_RULE_IDS, buildTotalRules, canonicalRulesetHash, verifyRuleset, type RulesApi,
} from "../fill/quarantine";
import type { ResolvedEgress } from "./certification";

type Rule = chrome.declarativeNetRequest.Rule;
type ResourceType = chrome.declarativeNetRequest.ResourceType;
type RequestMethod = chrome.declarativeNetRequest.RequestMethod;

export const SUBMIT_ALLOW_RULE_BASE = 9201;
export const SUBMIT_ALLOW_PRIORITY = 2000;
export const SUBMIT_ALLOW_RULE_IDS: readonly number[] = Array.from({ length: 9 }, (_, i) => SUBMIT_ALLOW_RULE_BASE + i);
const FILL_IDS: readonly number[] = Object.values(FILL_RULE_IDS);

function chromeApi(): RulesApi {
  return chrome.declarativeNetRequest as unknown as RulesApi;
}

function allowRules(tabId: number, egress: readonly ResolvedEgress[]): Rule[] {
  if (egress.length > SUBMIT_ALLOW_RULE_IDS.length) throw new Error("egress exceeds the reserved allow rule ids");
  return egress.map((entry, i) => ({
    id: SUBMIT_ALLOW_RULE_BASE + i,
    priority: SUBMIT_ALLOW_PRIORITY,
    action: { type: "allow" as chrome.declarativeNetRequest.RuleActionType },
    condition: { tabIds: [tabId], requestMethods: entry.methods as RequestMethod[],
                 resourceTypes: entry.types as ResourceType[], regexFilter: entry.regex },
  }));
}

export function buildSubmitEgressRules(tabId: number, employerHost: string, egress: readonly ResolvedEgress[]): Rule[] {
  return [...buildTotalRules(tabId, employerHost), ...allowRules(tabId, egress)];
}

export async function installSubmitEgress(tabId: number, employerHost: string, egress: readonly ResolvedEgress[],
                                          api: RulesApi = chromeApi()): Promise<{ rulesetHash: string }> {
  // Only the allows change; the TOTAL rules already verified at FILL stay.
  await api.updateSessionRules({ removeRuleIds: [...SUBMIT_ALLOW_RULE_IDS], addRules: allowRules(tabId, egress) });
  return { rulesetHash: await canonicalRulesetHash(buildSubmitEgressRules(tabId, employerHost, egress)) };
}

export async function verifySubmitEgress(expectedHash: string, api: RulesApi = chromeApi()): Promise<boolean> {
  const [session, dynamic] = await Promise.all([api.getSessionRules(), api.getDynamicRules()]);
  if (dynamic.some((rule) => rule.action.type !== "block")) return false;
  if (session.some((rule) => rule.action.type !== "block" && !SUBMIT_ALLOW_RULE_IDS.includes(rule.id))) return false;
  const ours = session.filter((rule) => FILL_IDS.includes(rule.id) || SUBMIT_ALLOW_RULE_IDS.includes(rule.id));
  return (await canonicalRulesetHash(ours)) === expectedHash;
}

export async function restoreTotal(totalHash: string, api: RulesApi = chromeApi()): Promise<boolean> {
  await api.updateSessionRules({ removeRuleIds: [...SUBMIT_ALLOW_RULE_IDS] });
  return verifyRuleset(totalHash, api);
}
