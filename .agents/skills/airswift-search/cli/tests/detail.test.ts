import { afterEach, beforeEach, describe, expect, test } from "bun:test"
import { runDetail } from "../src/commands/detail"
import { detailPage, jobPostingJsonLd } from "./fixtures"
import { resetCrawlDelayState } from "../src/helpers"

const originalFetch = globalThis.fetch
const originalStdoutWrite = process.stdout.write

beforeEach(() => {
  resetCrawlDelayState()
})

afterEach(() => {
  globalThis.fetch = originalFetch
  process.stdout.write = originalStdoutWrite
})

describe("runDetail", () => {
  test("rejects a bare numeric id with BAD_ID -- Airswift's route requires the exact slug", async () => {
    const code = await runDetail({ id: "1280556", format: "json" })
    expect(code).toBe(1)
  })

  test("fetches and parses a full detail URL, re-deriving the numeric id from it", async () => {
    globalThis.fetch = (async () => new Response(detailPage(jobPostingJsonLd()))) as unknown as typeof fetch

    let stdout = ""
    process.stdout.write = ((chunk: string | Uint8Array) => {
      stdout += chunk.toString()
      return true
    }) as typeof process.stdout.write

    const url = "https://www.airswift.com/jobs/fpso-piping-integration-coordinator-1280556"
    const code = await runDetail({ id: url, format: "json" })

    expect(code).toBe(0)
    const job = JSON.parse(stdout)
    expect(job.id).toBe("1280556")
    expect(job.url).toBe(url)
    expect(job.title).toBe("FPSO Piping Integration Coordinator")
  })

  test("a 404 (empty body) is reported as NOT_FOUND", async () => {
    globalThis.fetch = (async () => new Response("", { status: 404 })) as unknown as typeof fetch

    const url = "https://www.airswift.com/jobs/does-not-exist-9999999"
    const code = await runDetail({ id: url, format: "json" })
    expect(code).toBe(1)
  })
})
