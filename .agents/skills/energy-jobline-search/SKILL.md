---
name: energy-jobline-search
version: 1.0.0
description: >
  Use this skill whenever the user wants to search for oil & gas, drilling,
  offshore, or energy engineering jobs — drilling engineer, well engineer,
  completions, fluids, planning engineer, company man, and related roles.
  The location is always supplied explicitly by the user. Trigger phrases:
  find a drilling job, oil and gas jobs, energy jobs, offshore jobs, drilling
  engineer vacancies, "are there any energy jobs in <place>".
context: fork
enabled: true  # set to false to keep this portal installed but have /scrape skip it
allowed-tools: Bash(bun run .agents/skills/energy-jobline-search/cli/src/cli.ts *)
---

# Energy Jobline Search Skill

Search live job listings from Energy Jobline's public job board — oil & gas, drilling,
offshore, and broader energy-sector roles, with particularly strong Aberdeen/North Sea
coverage. No authentication, no API key, and **zero runtime dependencies** — it runs with
just `bun`. The location is always passed explicitly.

## When to use this skill

- Search for drilling, well engineering, completions, fluids, planning, or other
  oil & gas / energy roles in a given location
- Get the full description of a specific job listing

## Commands

### Search job listings

```bash
bun run .agents/skills/energy-jobline-search/cli/src/cli.ts search --location "<place>" [flags]
```

Key flags:
- `--location <text>` / `-l <text>` — **required.** e.g. `"Aberdeen"`, `"Houston"`.
- `--query <text>` / `-q <text>` — keyword search (title, role). Default `"engineering"`.
- `--page <n>` — page number (1-indexed).
- `--limit <n>` / `-n <n>` — cap total results emitted (client-side).
- `--format json|table|plain` — default `json`.

### Fetch full job detail

```bash
bun run .agents/skills/energy-jobline-search/cli/src/cli.ts detail <id|url> [--format json|plain]
```

`id` is the numeric job ID from `search` results (e.g. `31512381`). You may also pass a
full `https://www.energyjobline.com/job/...` URL. Returns the full description,
employment type, and validity date from the page's structured `schema.org/JobPosting`
data when present.

## Usage examples

```bash
# Drilling engineer roles in Aberdeen
bun run .agents/skills/energy-jobline-search/cli/src/cli.ts search -q "drilling engineer" -l Aberdeen --format table

# Well engineering roles in Houston
bun run .agents/skills/energy-jobline-search/cli/src/cli.ts search -q "well engineer" -l Houston --format table

# Full details for a specific job
bun run .agents/skills/energy-jobline-search/cli/src/cli.ts detail 31512381 --format plain
```

## Output formats

| Format | Best for |
|--------|----------|
| `json` | Default — programmatic use, passing IDs to `detail` |
| `table` | Quick human-readable scanning |
| `plain` | Reading a single job's full detail (`detail` command) |

All errors are written to **stderr** as `{ "error": "...", "code": "..." }` and the process exits with code `1`.

## Notes

- Data is from Energy Jobline's public, server-rendered listing and detail pages — no
  credentials required.
- Detail pages carry a `schema.org/JobPosting` JSON-LD block when present, which this
  skill prefers over scraping visible text; a page without it still returns the fields
  available from the search listing (title, company, location, date, URL).
- The site's `robots.txt` sets a 10-second crawl delay for all user-agents; this skill
  enforces that delay between requests. Keep volume low.
- This is a **discovery-only** skill: it finds and describes listings. It does not apply,
  submit, or authenticate — Energy Jobline's own apply flow is a separate concern.
