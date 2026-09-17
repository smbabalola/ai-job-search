import { afterEach, describe, expect, test } from "bun:test"
import { runSearch } from "../src/commands/search"
import { listingCard, listingPage } from "./fixtures"

const originalFetch = globalThis.fetch
const originalStdoutWrite = process.stdout.write

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

    const code = await runSearch({ location: "Aberdeen", page: 1, limit: 0, format: "json" })

    expect(code).toBe(0)
    expect(JSON.parse(stdout).results).toHaveLength(0)
  })

  test("builds the path-based URL from query and location slugs", async () => {
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
    expect(capturedUrl).toBe(
      "https://www.energyjobline.com/jobs/drilling-engineer/energy-jobline-zr/aberdeen",
    )
  })

  test("page > 1 appends the site's 0-indexed ?page= param", async () => {
    let capturedUrl = ""
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      capturedUrl = typeof input === "string" ? input : input.toString()
      return new Response(listingPage([]))
    }) as unknown as typeof fetch

    await runSearch({ location: "Aberdeen", page: 3, format: "json" })

    expect(capturedUrl).toContain("?page=2")
  })

  test("returns all parsed results in JSON mode", async () => {
    globalThis.fetch = (async () =>
      new Response(
        listingPage([listingCard("1", "Well Engineer"), listingCard("2", "Fluids Supervisor")]),
      )) as unknown as typeof fetch

    let stdout = ""
    process.stdout.write = ((chunk: string | Uint8Array) => {
      stdout += chunk.toString()
      return true
    }) as typeof process.stdout.write

    const code = await runSearch({ location: "Aberdeen", page: 1, format: "json" })

    expect(code).toBe(0)
    const parsed = JSON.parse(stdout)
    expect(parsed.results).toHaveLength(2)
    expect(parsed.meta.count).toBe(2)
  })

  test("a network failure is reported on stderr and exits 1", async () => {
    globalThis.fetch = (async () => {
      throw new Error("connection refused")
    }) as unknown as typeof fetch

    const code = await runSearch({ location: "Aberdeen", page: 1, format: "json" })
    expect(code).toBe(1)
  })
})
