import { describe, expect, test } from "bun:test"
import { join } from "path"

const CLI_PATH = join(import.meta.dir, "../src/cli.ts")

async function runCLI(args: string[]): Promise<{ stdout: string; stderr: string; exitCode: number }> {
  const proc = Bun.spawn(["bun", "run", CLI_PATH, ...args], { stdout: "pipe", stderr: "pipe" })
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
    proc.exited,
  ])
  return { stdout: stdout.trim(), stderr: stderr.trim(), exitCode }
}

describe("cli flag validation", () => {
  test("detail without an id fails with NO_ID", async () => {
    const result = await runCLI(["detail"])
    expect(result.exitCode).toBe(1)
    const err = JSON.parse(result.stderr)
    expect(err.code).toBe("NO_ID")
  })

  test("detail with a bare numeric reference (no URL) fails with BAD_ID", async () => {
    // Airswift's /jobs/<slug>-<id> route requires the exact slug (confirmed
    // directly: a wrong/placeholder slug 404s even with the correct numeric
    // ID), unlike energy-jobline-search where a bare ID is resolvable. This
    // is why helpers.ts's parseJobCards puts the full URL, not the bare
    // reference, in each search result's "id" field.
    const result = await runCLI(["detail", "1280556"])
    expect(result.exitCode).toBe(1)
    const err = JSON.parse(result.stderr)
    expect(err.code).toBe("BAD_ID")
  })

  test("an unknown command fails with BAD_CMD", async () => {
    const result = await runCLI(["frobnicate"])
    expect(result.exitCode).toBe(1)
    const err = JSON.parse(result.stderr)
    expect(err.code).toBe("BAD_CMD")
  })

  test("no command prints help and exits 1", async () => {
    const result = await runCLI([])
    expect(result.exitCode).toBe(1)
    expect(result.stdout).toContain("airswift-cli")
  })

  test("--help exits 0 and prints usage", async () => {
    const result = await runCLI(["--help"])
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain("USAGE")
  })
})
