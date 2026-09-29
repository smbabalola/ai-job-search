import { describe, expect, it } from "vitest";

import { GREENHOUSE_SUBMIT, resolveEgress } from "../src/submit/certification";
import {
  SUBMIT_ALLOW_PRIORITY, SUBMIT_ALLOW_RULE_BASE, buildSubmitEgressRules, installSubmitEgress, matchedRuleIds,
  restoreTotal, verifySubmitEgress,
} from "../src/submit/egress";
import { buildTotalRules, canonicalRulesetHash, installRuleset, type RulesApi } from "../src/fill/quarantine";

type Rule = chrome.declarativeNetRequest.Rule;

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
const HOST = "127.0.0.1";
const EGRESS = resolveEgress(GREENHOUSE_SUBMIT, "http://127.0.0.1:8430", "acme", "123");

async function underTotal() {
  const api = fakeApi();
  const { rulesetHash } = await installRuleset("TOTAL", TAB, HOST, api);
  return { api, totalHash: rulesetHash };
}

describe("SUBMIT egress (6E-A spec §9.4)", () => {
  it("is TOTAL unchanged plus one tab-scoped priority-2000 allow per certified entry", () => {
    const rules = buildSubmitEgressRules(TAB, HOST, EGRESS);
    expect(rules.slice(0, 2)).toEqual(buildTotalRules(TAB, HOST));
    const allows = rules.slice(2);
    expect(allows.map((r) => r.id)).toEqual([9201, 9202, 9203, 9204]);
    expect(SUBMIT_ALLOW_RULE_BASE).toBe(9201);
    for (const [i, rule] of allows.entries()) {
      expect(rule.priority).toBe(SUBMIT_ALLOW_PRIORITY);
      expect(rule.action).toEqual({ type: "allow" });
      expect(rule.condition).toEqual({ tabIds: [TAB], requestMethods: EGRESS[i].methods,
                                       resourceTypes: EGRESS[i].types, regexFilter: EGRESS[i].regex });
    }
  });

  it("refuses more allow entries than its reserved id range", () => {
    const many = Array.from({ length: 10 }, (_, i) => ({ ...EGRESS[0], id: `X${i}` }));
    expect(() => buildSubmitEgressRules(TAB, HOST, many)).toThrow();
  });

  it("installs only the allows, leaving TOTAL in place, and verifies exactly", async () => {
    const { api } = await underTotal();
    const total = structuredClone(api.session);
    const { rulesetHash } = await installSubmitEgress(TAB, HOST, EGRESS, api);
    expect(api.session.filter((r) => r.id < 9200)).toEqual(total);
    expect(rulesetHash).toBe(await canonicalRulesetHash(buildSubmitEgressRules(TAB, HOST, EGRESS)));
    expect(await verifySubmitEgress(rulesetHash, api)).toBe(true);
  });

  it("verification fails on a missing, extra or altered allow, a removed TOTAL rule, or a foreign allow", async () => {
    const cases: ((api: ReturnType<typeof fakeApi>) => void)[] = [
      (api) => { api.session = api.session.filter((r) => r.id !== 9202); },
      (api) => { api.session.push({ id: 9205, priority: 2000, action: { type: "allow" } as never,
                                    condition: { tabIds: [TAB], regexFilter: ".*" } }); },
      (api) => { (api.session.find((r) => r.id === 9201)!.condition as { regexFilter: string }).regexFilter = ".*"; },
      (api) => { api.session = api.session.filter((r) => r.id !== 9111); },
      (api) => { api.session.push({ id: 5, priority: 1, action: { type: "allow" } as never, condition: {} }); },
    ];
    for (const mutate of cases) {
      const { api } = await underTotal();
      const { rulesetHash } = await installSubmitEgress(TAB, HOST, EGRESS, api);
      mutate(api);
      expect(await verifySubmitEgress(rulesetHash, api)).toBe(false);
    }
  });

  it("a dynamic allow rule defeats verification", async () => {
    const api = fakeApi([], [{ id: 1, priority: 1, action: { type: "allow" } as never, condition: {} }]);
    await installRuleset("TOTAL", TAB, HOST, api);
    const { rulesetHash } = await installSubmitEgress(TAB, HOST, EGRESS, api);
    expect(await verifySubmitEgress(rulesetHash, api)).toBe(false);
  });

  it("restoreTotal removes every allow and re-verifies the recorded TOTAL hash", async () => {
    const { api, totalHash } = await underTotal();
    await installSubmitEgress(TAB, HOST, EGRESS, api);
    expect(await restoreTotal(totalHash, api)).toBe(true);
    expect(api.session.map((r) => r.id).sort()).toEqual([9111, 9121]);
    expect(await restoreTotal("sha256:" + "0".repeat(64), api)).toBe(false);
  });

  it("matched-rule evidence reports availability and the matched ids", async () => {
    const ok = await matchedRuleIds(TAB, 0, { getMatchedRules: async () => ({
      rulesMatchedInfo: [{ rule: { ruleId: 9201 }, tabId: TAB, timeStamp: 5 }] }) });
    expect(ok).toEqual({ available: true, ids: [9201] });
    const failed = await matchedRuleIds(TAB, 0, { getMatchedRules: async () => { throw new Error("quota"); } });
    expect(failed).toEqual({ available: false, ids: [] });
  });
});
