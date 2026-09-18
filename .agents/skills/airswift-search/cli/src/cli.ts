#!/usr/bin/env bun
// Self-contained CLI for searching Airswift's public job listings
// (airswift.com) — energy, oil & gas, drilling, subsea, planning/project
// controls staffing. No external CLI framework, zero runtime dependencies.
//
// Public, unauthenticated HTML endpoint: search results (?search=&location=)
// and job detail pages are ordinary server-rendered pages, no API key.
// robots.txt (fetched 2026-09-18) has no Crawl-delay directive and does not
// disallow /jobs or /jobs/*; helpers.ts still self-imposes a delay.

import { runSearch, type SearchOpts } from "./commands/search.js"
import { runDetail, type DetailOpts } from "./commands/detail.js"
import { BASE_URL } from "./helpers.js"

interface Flags {
  _: string[]
  [k: string]: string | boolean | string[]
}

const ALIAS: Record<string, string> = { q: "query", l: "location", n: "limit" }

function parseFlags(argv: string[]): Flags {
  const flags: Flags = { _: [] }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (!a.startsWith("-")) {
      ;(flags._ as string[]).push(a)
      continue
    }
    const name = a.replace(/^-+/, "")
    const key = ALIAS[name] ?? name
    const next = argv[i + 1]
    let value: string | boolean = true
    if (next !== undefined && !next.startsWith("-")) {
      value = next
      i++
    }
    flags[key] = value
  }
  return flags
}

function stringFlag(raw: string | boolean | string[] | undefined): string | undefined {
  return typeof raw === "string" ? raw : undefined
}

const HELP = `airswift-cli — search Airswift's public job listings (energy, oil & gas, drilling, subsea, planning/project controls)

USAGE
  bun run src/cli.ts search [flags]
  bun run src/cli.ts detail <url> [--format json|plain]

SEARCH FLAGS
  --query, -q <text>      Keywords (title, role). Optional -- omitted returns all jobs.
  --location, -l <text>   A place name, e.g. "Aberdeen", "Houston". Optional.
  --page <n>              1-indexed page. Default 1.
  --limit, -n <n>         Cap total results emitted (client-side).
  --format <fmt>          json (default) | table | plain.

DETAIL
  <url>                   The full https://www.airswift.com/jobs/<slug>-<id> URL
                          from a search result's "id"/"url" field. Airswift's
                          detail route requires the exact slug (a wrong or
                          placeholder slug 404s even with the correct numeric
                          job reference), so a bare ID alone cannot be resolved
                          -- always pass the URL.

EXAMPLES
  bun run src/cli.ts search -q "drilling engineer" -l Aberdeen --format table
  bun run src/cli.ts detail https://www.airswift.com/jobs/field-telecoms-engineer-1277009 --format plain

Reads are public (no API key). Source: ${BASE_URL}.
`

function parseIntFlag(name: string, raw: string | boolean | string[]): number | null {
  const val = parseInt(raw as string, 10)
  if (isNaN(val)) {
    process.stderr.write(JSON.stringify({ error: `--${name} must be a number, got "${raw}"`, code: "BAD_ARG" }) + "\n")
    return null
  }
  return val
}

async function main(): Promise<number> {
  const argv = process.argv.slice(2)
  const flags = parseFlags(argv)
  const cmd = (flags._ as string[])[0]

  const helpRequested = Boolean(flags.help || flags.h)
  if (!cmd || helpRequested) {
    process.stdout.write(HELP)
    // Explicit --help/-h is always success (exit 0), independent of whether
    // a command happened to precede it. Only the true "no arguments at all"
    // usage-error case exits 1.
    return helpRequested || cmd ? 0 : 1
  }

  if (cmd === "search") {
    const fmt = (flags.format as string) || "json"
    for (const name of ["page", "limit"] as const) {
      if (flags[name] !== undefined) {
        const v = parseIntFlag(name, flags[name])
        if (v === null) return 1
        flags[name] = String(v)
      }
    }
    const opts: SearchOpts = {
      query: stringFlag(flags.query),
      location: stringFlag(flags.location),
      page: flags.page ? Math.max(1, parseInt(flags.page as string, 10)) : 1,
      limit: flags.limit !== undefined ? Math.max(0, parseInt(flags.limit as string, 10)) : undefined,
      format: (["json", "table", "plain"].includes(fmt) ? fmt : "json") as SearchOpts["format"],
    }
    return runSearch(opts)
  }

  if (cmd === "detail") {
    const id = (flags._ as string[])[1]
    if (!id) {
      process.stderr.write(JSON.stringify({ error: "detail requires a <url>", code: "NO_ID" }) + "\n")
      return 1
    }
    const fmt = (flags.format as string) || "json"
    const opts: DetailOpts = { id, format: fmt === "plain" ? "plain" : "json" }
    return runDetail(opts)
  }

  process.stderr.write(JSON.stringify({ error: `Unknown command "${cmd}"`, code: "BAD_CMD" }) + "\n")
  return 1
}

main()
  .then((code) => process.exit(code))
  .catch((e) => {
    process.stderr.write(
      JSON.stringify({
        error: e instanceof Error ? e.message : String(e),
        code: "INTERNAL_ERROR",
      }) + "\n",
    )
    process.exit(1)
  })
