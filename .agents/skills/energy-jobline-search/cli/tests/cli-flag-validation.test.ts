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
  test("search without --location fails with NO_LOCATION", async () => {
    const result = await runCLI(["search", "--query", "drilling engineer"])
    expect(result.exitCode).toBe(1)
    const err = JSON.parse(result.stderr)
    expect(err.code).toBe("NO_LOCATION")
  })

  test("detail without an id fails with NO_ID", async () => {
    const result = await runCLI(["detail"])
    expect(result.exitCode).toBe(1)
    const err = JSON.parse(result.stderr)
    expect(err.code).toBe("NO_ID")
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
    expect(result.stdout).toContain("energy-jobline-cli")
  })

  test("--help exits 0 and prints usage", async () => {
    const result = await runCLI(["--help"])
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain("USAGE")
  })
})
