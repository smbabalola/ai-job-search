---
name: airswift-search
version: 1.0.0
description: >
  Use this skill whenever the user wants to search Airswift's public job
  listings — energy, oil & gas, drilling, subsea, planning/project controls,
  and broader technical staffing roles. Location and keywords are both
  optional. Trigger phrases: airswift jobs, find an airswift vacancy, energy
  staffing jobs, "are there any airswift jobs in <place>".
context: fork
enabled: true  # set to false to keep this portal installed but have /scrape skip it
allowed-tools: Bash(bun run .agents/skills/airswift-search/cli/src/cli.ts *)
---

# Airswift Search Skill

Search live job listings from Airswift's public job board — energy, oil & gas, drilling,
subsea, and planning/project controls staffing roles. No authentication, no API key, and
**zero runtime dependencies** — it runs with just `bun`.

## When to use this skill

- Search for energy, oil & gas, drilling, subsea, or planning/project controls staffing
  roles, optionally filtered by location
- Get the full description of a specific job listing

## Commands

### Search job listings

```bash
bun run .agents/skills/airswift-search/cli/src/cli.ts search [flags]
```

Key flags:
- `--query <text>` / `-q <text>` — keyword search (title, role). Optional — omitted
  returns all jobs.
- `--location <text>` / `-l <text>` — a place name, e.g. `"Aberdeen"`, `"Houston"`.
  Optional.
- `--page <n>` — page number (1-indexed).
- `--limit <n>` / `-n <n>` — cap total results emitted (client-side).
- `--format json|table|plain` — default `json`.

### Fetch full job detail

```bash
bun run .agents/skills/airswift-search/cli/src/cli.ts detail <url> [--format json|plain]
```

`<url>` is the full `https://www.airswift.com/jobs/<slug>-<id>` URL from a search
result's `id`/`url` field. Airswift's detail route requires the exact slug — a wrong or
placeholder slug 404s even with the correct numeric job reference — so a bare ID alone
cannot be resolved; always pass the full URL. Returns the full description, employment
type, industry, and validity date from the page's structured `schema.org/JobPosting`
data when present.

## Usage examples

```bash
# Drilling engineer roles in Aberdeen
bun run .agents/skills/airswift-search/cli/src/cli.ts search -q "drilling engineer" -l Aberdeen --format table

# All current listings
bun run .agents/skills/airswift-search/cli/src/cli.ts search --format table

# Full details for a specific job
bun run .agents/skills/airswift-search/cli/src/cli.ts detail https://www.airswift.com/jobs/field-telecoms-engineer-1277009 --format plain
```

## Output formats

| Format | Best for |
|--------|----------|
| `json` | Default — programmatic use, passing URLs to `detail` |
| `table` | Quick human-readable scanning |
| `plain` | Reading a single job's full detail (`detail` command) |

All errors are written to **stderr** as `{ "error": "...", "code": "..." }` and the process exits with code `1`.

## Notes

- Data is from Airswift's public, server-rendered listing and detail pages — no
  credentials required.
- Detail pages carry a `schema.org/JobPosting` JSON-LD block when present, which this
  skill prefers over scraping visible text; a page without it still returns the fields
  available from the search listing (title, location, date, employment type, URL).
- Airswift is a staffing agency — the `company` field is always `"Airswift"` itself; the
  end client is not exposed as a discrete field on the listing or detail page.
- The site's `robots.txt` sets no crawl delay, but this skill self-imposes a conservative
  one between requests as a courtesy. Keep volume low.
- This is a **discovery-only** skill: it finds and describes listings. It does not apply,
  submit, or authenticate — Airswift's own "Apply Now" flow is a separate concern.
