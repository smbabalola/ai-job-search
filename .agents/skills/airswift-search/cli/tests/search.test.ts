import { afterEach, beforeEach, describe, expect, test } from "bun:test"
import { runSearch } from "../src/commands/search"
import { listingCard, listingPage } from "./fixtures"
import { resetCrawlDelayState } from "../src/helpers"

const originalFetch = globalThis.fetch
const originalStdoutWrite = process.stdout.write

// Bun runs all test files in one process, so the module-level crawl-delay
// clock (helpers.ts) persists across files. Without this reset, a real
// elapsed-time window left behind by crawl-delay.test.ts (or an earlier
// test here) makes the next fetch call here wait out the remainder of that
// window -- a fixture-driven test has no network latency of its own to
// blame that on, so it just times out against Bun's 5s per-test default.
beforeEach(() => {
  resetCrawlDelayState()
})

afterEach(() => {
  globalThis.fetch = originalFetch
  process.stdout.write = originalStdoutWrite
})

describe("runSearch", () => {
  test("--limit 0 emits zero results", async () => {
    globalThis.fetch = (async () =>
      new Response(listingPage([listingCard("1", "Drilling Engineer")]))) as unknown as typeof fetch

    let stdout = ""
    process.stdout.write = ((chunk: string | Uint8Array) => {
      stdout += chunk.toString()
      return true
    }) as typeof process.stdout.write

    const code = await runSearch({ page: 1, limit: 0, format: "json" })

    expect(code).toBe(0)
    expect(JSON.parse(stdout).results).toHaveLength(0)
  })

  test("builds a bare /jobs URL when no query or location is given", async () => {
    let capturedUrl = ""
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      capturedUrl = typeof input === "string" ? input : input.toString()
      return new Response(listingPage([]))
    }) as unknown as typeof fetch

    const code = await runSearch({ page: 1, format: "json" })

    expect(code).toBe(0)
    expect(capturedUrl).toBe("https://www.airswift.com/jobs")
  })

  test("builds the query-param URL from query and location", async () => {
    let capturedUrl = ""
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      capturedUrl = typeof input === "string" ? input : input.toString()
      return new Response(listingPage([]))
    }) as unknown as typeof fetch

    const code = await runSearch({
      query: "Drilling Engineer",
      location: "Aberdeen",
      page: 1,
      format: "json",
    })

    expect(code).toBe(0)
    const url = new URL(capturedUrl)
    expect(url.pathname).toBe("/jobs")
    expect(url.searchParams.get("search")).toBe("Drilling Engineer")
    expect(url.searchParams.get("location")).toBe("Aberdeen")
    expect(url.searchParams.has("page_num")).toBe(false)
  })

  test("location alone (no query) still filters server-side via the location param", async () => {
    let capturedUrl = ""
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      capturedUrl = typeof input === "string" ? input : input.toString()
      return new Response(listingPage([]))
    }) as unknown as typeof fetch

    await runSearch({ location: "Houston", page: 1, format: "json" })

    const url = new URL(capturedUrl)
    expect(url.searchParams.get("location")).toBe("Houston")
    expect(url.searchParams.has("search")).toBe(false)
  })

  test("page > 1 appends the site's 1-indexed page_num param", async () => {
    let capturedUrl = ""
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      capturedUrl = typeof input === "string" ? input : input.toString()
      return new Response(listingPage([]))
    }) as unknown as typeof fetch

    await runSearch({ page: 3, format: "json" })

    expect(new URL(capturedUrl).searchParams.get("page_num")).toBe("3")
  })

  test("page 1 omits page_num entirely", async () => {
    let capturedUrl = ""
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      capturedUrl = typeof input === "string" ? input : input.toString()
      return new Response(listingPage([]))
    }) as unknown as typeof fetch

    await runSearch({ page: 1, format: "json" })

    expect(new URL(capturedUrl).searchParams.has("page_num")).toBe(false)
  })

  test("returns all parsed results in JSON mode, including totalCount/totalPages meta", async () => {
    globalThis.fetch = (async () =>
      new Response(
        listingPage(
          [listingCard("1", "Well Engineer"), listingCard("2", "Fluids Supervisor")],
          { count: 913, pages: 38 },
        ),
      )) as unknown as typeof fetch

    let stdout = ""
    process.stdout.write = ((chunk: string | Uint8Array) => {
      stdout += chunk.toString()
      return true
    }) as typeof process.stdout.write

    const code = await runSearch({ page: 1, format: "json" })

    expect(code).toBe(0)
    const parsed = JSON.parse(stdout)
    expect(parsed.results).toHaveLength(2)
    expect(parsed.meta.count).toBe(2)
    expect(parsed.meta.totalCount).toBe(913)
    expect(parsed.meta.totalPages).toBe(38)
  })

  test("singular 'Found 1 job on 1 page' result count is parsed correctly through runSearch", async () => {
    globalThis.fetch = (async () =>
      new Response(listingPage([listingCard("1", "Well Engineer")], { count: 1, pages: 1 }))) as unknown as typeof fetch

    let stdout = ""
    process.stdout.write = ((chunk: string | Uint8Array) => {
      stdout += chunk.toString()
      return true
    }) as typeof process.stdout.write

    await runSearch({ page: 1, format: "json" })

    const parsed = JSON.parse(stdout)
    expect(parsed.meta.totalCount).toBe(1)
    expect(parsed.meta.totalPages).toBe(1)
  })

  test("each result's id is its full detail URL, not a bare reference", async () => {
    globalThis.fetch = (async () =>
      new Response(listingPage([listingCard("1280556", "FPSO Piping Integration Coordinator")]))) as unknown as typeof fetch

    let stdout = ""
    process.stdout.write = ((chunk: string | Uint8Array) => {
      stdout += chunk.toString()
      return true
    }) as typeof process.stdout.write

    await runSearch({ page: 1, format: "json" })

    const parsed = JSON.parse(stdout)
    expect(parsed.results[0].id).toBe(parsed.results[0].url)
    expect(parsed.results[0].id).toContain("1280556")
  })

  test("a network failure is reported on stderr and exits 1", async () => {
    globalThis.fetch = (async () => {
      throw new Error("connection refused")
    }) as unknown as typeof fetch

    const code = await runSearch({ page: 1, format: "json" })
    expect(code).toBe(1)
  })
})
