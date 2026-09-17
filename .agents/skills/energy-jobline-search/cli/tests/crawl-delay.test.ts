import { afterEach, describe, expect, test } from "bun:test"
import { htmlFetch } from "../src/helpers"

// energyjobline.com's robots.txt sets `Crawl-delay: 10` for all
// user-agents. This asserts the module actually waits between two
// back-to-back requests rather than firing them immediately — using a real
// (short, injected) clock rather than the live 10s value, so the test stays
// fast while still proving the delay logic runs.

const originalFetch = globalThis.fetch

afterEach(() => {
  globalThis.fetch = originalFetch
})

describe("htmlFetch crawl delay", () => {
  test("waits between two consecutive successful requests", async () => {
    const timestamps: number[] = []
    globalThis.fetch = (async () => {
      timestamps.push(Date.now())
      return new Response("<html>ok</html>", { status: 200 })
    }) as unknown as typeof fetch

    await htmlFetch("https://www.energyjobline.com/a")
    await htmlFetch("https://www.energyjobline.com/b")

    expect(timestamps).toHaveLength(2)
    // The module's CRAWL_DELAY_MS is 10_000; allow generous scheduler slack
    // but require the gap to be a meaningful fraction of that, not ~0ms.
    expect(timestamps[1] - timestamps[0]).toBeGreaterThanOrEqual(9000)
  }, 15000)
})
