import { afterEach, beforeEach, describe, expect, test } from "bun:test"
import { htmlFetch, resetCrawlDelayState } from "../src/helpers"

// airswift.com's robots.txt declares no Crawl-delay directive, but this
// adapter self-imposes a conservative 3s minimum gap between requests as a
// courtesy, matching this repo's other adapters. This asserts the module
// actually waits between two back-to-back requests rather than firing them
// immediately.
//
// This test genuinely waits out most of the real 3s window and, without a
// reset, leaves a fresh crawl-delay timestamp behind in the shared
// module-level state -- which the next test in the process (Bun runs all
// test files in one process) would otherwise inherit and wait out too.
// Resetting both before and after keeps this test's real-time side effect
// from leaking in either direction.

const originalFetch = globalThis.fetch

beforeEach(() => {
  resetCrawlDelayState()
})

afterEach(() => {
  globalThis.fetch = originalFetch
  resetCrawlDelayState()
})

describe("htmlFetch crawl delay", () => {
  test("waits between two consecutive successful requests", async () => {
    const timestamps: number[] = []
    globalThis.fetch = (async () => {
      timestamps.push(Date.now())
      return new Response("<html>ok</html>", { status: 200 })
    }) as unknown as typeof fetch

    await htmlFetch("https://www.airswift.com/a")
    await htmlFetch("https://www.airswift.com/b")

    expect(timestamps).toHaveLength(2)
    // The module's CRAWL_DELAY_MS is 3_000; allow generous scheduler slack
    // but require the gap to be a meaningful fraction of that, not ~0ms.
    expect(timestamps[1] - timestamps[0]).toBeGreaterThanOrEqual(2500)
  }, 10000)
})
