import { describe, expect, it } from "vitest";
import {
  FILL_RULE_IDS, QUARANTINE_RESOURCE_TYPES_V1, buildPreloadRules, buildTotalRules, canonicalRulesetHash,
  installRuleset, verifyRuleset, type RulesApi,
} from "../src/fill/quarantine";

type Rule = chrome.declarativeNetRequest.Rule;

// A faithful in-memory stand-in for chrome.declarativeNetRequest's session
// and dynamic rule stores (updateSessionRules is atomic: removals, then additions).
function fakeApi(initialSession: Rule[] = [], dynamic: Rule[] = []): RulesApi & { session: Rule[] } {
  const api = {
    session: [...initialSession],
    async getSessionRules() { return structuredClone(api.session); },
    async getDynamicRules() { return structuredClone(dynamic); },
    async updateSessionRules(opts: { removeRuleIds?: number[]; addRules?: Rule[] }) {
      const remove = new Set(opts.removeRuleIds ?? []);
      const next = api.session.filter((r) => !remove.has(r.id));
      for (const rule of opts.addRules ?? []) {
        if (next.some((r) => r.id === rule.id)) throw new Error(`duplicate rule id ${rule.id}`);
        next.push(structuredClone(rule));
      }
      api.session = next;
    },
  };
  return api;
}

const TAB = 42;
const HOST = "jobs.example.test";

describe("quarantine rulesets (spec §10.2-10.3)", () => {
  it("names every resource type of QUARANTINE_RESOURCE_TYPES_V1, including main_frame", () => {
    expect(QUARANTINE_RESOURCE_TYPES_V1).toEqual([
      "main_frame", "sub_frame", "stylesheet", "script", "image", "font", "object", "xmlhttprequest", "ping",
      "csp_report", "media", "websocket", "webtransport", "webbundle", "other",
    ]);
  });

  it("TOTAL: Q1 blocks every type and method from the tab; Q2 blocks employer background requests", () => {
    const rules = buildTotalRules(TAB, HOST);
    expect(rules.map((r) => r.id)).toEqual([FILL_RULE_IDS.Q1_TOTAL, FILL_RULE_IDS.Q2]);
    const [q1, q2] = rules;
    expect(q1.action).toEqual({ type: "block" });
    expect(q1.condition).toEqual({ tabIds: [TAB], resourceTypes: [...QUARANTINE_RESOURCE_TYPES_V1] });
    expect(q1.condition.requestMethods).toBeUndefined();  // every method
    expect(q2.action).toEqual({ type: "block" });
    expect(q2.condition).toEqual({ tabIds: [-1], initiatorDomains: [HOST],
      resourceTypes: [...QUARANTINE_RESOURCE_TYPES_V1] });
  });

  it("PRELOAD: blocks state-changing methods and socket/beacon types, allows GET/HEAD, and includes Q2", () => {
    const rules = buildPreloadRules(TAB, HOST);
    expect(rules.map((r) => r.id)).toEqual([FILL_RULE_IDS.Q1_PRE_METHODS, FILL_RULE_IDS.Q1_PRE_TYPES, FILL_RULE_IDS.Q2]);
    const [methods, types] = rules;
    expect(methods.condition).toEqual({ tabIds: [TAB], requestMethods: ["post", "put", "patch", "delete", "connect", "options"],
      resourceTypes: [...QUARANTINE_RESOURCE_TYPES_V1] });
    expect(types.condition).toEqual({ tabIds: [TAB], resourceTypes: ["websocket", "webtransport", "ping"] });
    const allMethods = rules.flatMap((r) => r.condition.requestMethods ?? []);
    expect(allMethods).not.toContain("get");
    expect(allMethods).not.toContain("head");
  });

  it("has no Q3 (no all-tab employer rule)", () => {
    for (const rule of [...buildPreloadRules(TAB, HOST), ...buildTotalRules(TAB, HOST)]) {
      expect(rule.condition.tabIds, `rule ${rule.id} must be tab-scoped`).toBeDefined();
    }
  });

  it("hashes canonically: rule order and key order do not matter; any field does", async () => {
    const rules = buildTotalRules(TAB, HOST);
    const reverseKeys = (v: unknown): unknown => Array.isArray(v) ? v.map(reverseKeys)
      : v !== null && typeof v === "object"
        ? Object.fromEntries(Object.keys(v as object).reverse().map((k) => [k, reverseKeys((v as Record<string, unknown>)[k])]))
        : v;
    const reordered = [rules[1], rules[0]].map(reverseKeys) as typeof rules;
    expect(JSON.stringify(reordered[1])).not.toBe(JSON.stringify(rules[0]));  // really reordered
    expect(await canonicalRulesetHash(reordered)).toBe(await canonicalRulesetHash(rules));
    const altered = structuredClone(rules);
    altered[0].condition.tabIds = [TAB + 1];
    expect(await canonicalRulesetHash(altered)).not.toBe(await canonicalRulesetHash(rules));
    expect(await canonicalRulesetHash(rules)).toMatch(/^sha256:[0-9a-f]{64}$/);
  });
});

describe("install and exact read-back verification", () => {
  it("installs TOTAL atomically over PRELOAD and verifies the exact set", async () => {
    const api = fakeApi();
    await installRuleset("PRELOAD", TAB, HOST, api);
    const { rulesetHash } = await installRuleset("TOTAL", TAB, HOST, api);
    expect(api.session.map((r) => r.id).sort()).toEqual([FILL_RULE_IDS.Q1_TOTAL, FILL_RULE_IDS.Q2].sort());
    expect(await verifyRuleset(rulesetHash, api)).toBe(true);
    expect(rulesetHash).toBe(await canonicalRulesetHash(buildTotalRules(TAB, HOST)));
  });

  it("fails verification when a rule is missing", async () => {
    const api = fakeApi();
    const { rulesetHash } = await installRuleset("TOTAL", TAB, HOST, api);
    api.session = api.session.filter((r) => r.id !== FILL_RULE_IDS.Q2);
    expect(await verifyRuleset(rulesetHash, api)).toBe(false);
  });

  it("fails verification when an extra fill rule is present", async () => {
    const api = fakeApi();
    const { rulesetHash } = await installRuleset("TOTAL", TAB, HOST, api);
    api.session.push(buildPreloadRules(TAB, HOST)[0]);  // a stale PRELOAD rule left behind
    expect(await verifyRuleset(rulesetHash, api)).toBe(false);
  });

  it("fails verification when a condition was altered", async () => {
    const api = fakeApi();
    const { rulesetHash } = await installRuleset("TOTAL", TAB, HOST, api);
    api.session[0].condition.resourceTypes = ["xmlhttprequest"];
    expect(await verifyRuleset(rulesetHash, api)).toBe(false);
  });

  it("fails verification when any session or dynamic rule of this extension could allow or rewrite traffic", async () => {
    const allow: Rule = { id: 1, priority: 99, action: { type: "allow" }, condition: { tabIds: [TAB] } };
    const api = fakeApi([allow]);
    const { rulesetHash } = await installRuleset("TOTAL", TAB, HOST, api);
    expect(await verifyRuleset(rulesetHash, api)).toBe(false);
    const dynamicAllow = fakeApi([], [{ ...allow, action: { type: "allowAllRequests" } }]);
    const installed = await installRuleset("TOTAL", TAB, HOST, dynamicAllow);
    expect(await verifyRuleset(installed.rulesetHash, dynamicAllow)).toBe(false);
  });

  it("fails verification against a hash the builder did not produce", async () => {
    const api = fakeApi();
    await installRuleset("TOTAL", TAB, HOST, api);
    expect(await verifyRuleset("sha256:" + "0".repeat(64), api)).toBe(false);
  });
});
