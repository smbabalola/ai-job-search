// Data source: Energy Jobline's public, server-rendered job listing and
// detail pages (energyjobline.com). No authentication required for
// discovery. Search returns server-rendered HTML (a Drupal Views listing);
// detail pages additionally carry a schema.org/JobPosting JSON-LD block,
// which we prefer over HTML scraping wherever it is present. robots.txt
// (fetched 2026-09-17) sets `Crawl-delay: 10` for all user-agents and does
// not disallow /jobs/ or /job/ paths — htmlFetch enforces that delay
// between requests from this process.

export const BASE_URL = "https://www.energyjobline.com"

export function writeError(error: string, code: string): void {
  process.stderr.write(JSON.stringify({ error, code }) + "\n")
}

const UA = "energy-jobline-search-skill/1.0"

// robots.txt: `Crawl-delay: 10` (User-agent: *). Enforced as a minimum gap
// between the start of one request and the start of the next from this
// process — not a per-call sleep, so a single request pays no tax.
const CRAWL_DELAY_MS = 10_000

// Mutable holder (not a bare module-level `let`) so tests can reset it
// between cases via resetCrawlDelayState(). Without this, `lastRequestAt`
// is shared process-wide state: Bun runs every test file in one process, so
// a real 10s wait in one test (crawl-delay.test.ts) leaves a recent
// timestamp behind that a later, unrelated test (e.g. in search.test.ts)
// inherits — making it wait out the remainder of a window it never
// triggered, past Bun's 5s per-test default and into a false failure. This
// is a test-isolation fix, not a change to the production delay behavior:
// a real CLI invocation is a fresh process every time and never observes
// this state across "requests" in the way a test run does.
const crawlDelayState = { lastRequestAt: 0 }

/** Test-only: reset crawl-delay state so fixture-driven tests don't inherit
 * a real elapsed-time window left behind by an earlier test in the same
 * Bun process. Never called from production code paths. */
export function resetCrawlDelayState(): void {
  crawlDelayState.lastRequestAt = 0
}

async function respectCrawlDelay(): Promise<void> {
  const now = Date.now()
  const elapsed = now - crawlDelayState.lastRequestAt
  if (crawlDelayState.lastRequestAt !== 0 && elapsed < CRAWL_DELAY_MS) {
    await sleep(CRAWL_DELAY_MS - elapsed)
  }
  crawlDelayState.lastRequestAt = Date.now()
}

/** Fetch HTML with exponential backoff on 429/5xx and the site's crawl-delay
 * enforced between requests. Returns "" on a 404 (mirrors linkedin-search's
 * htmlFetch contract). */
export async function htmlFetch(url: string): Promise<string> {
  const maxRetries = 6
  let delay = 500
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    await respectCrawlDelay()
    const response = await fetch(url, {
      headers: {
        "User-Agent": UA,
        Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      },
      redirect: "follow",
      signal: AbortSignal.timeout(15000),
    })
    if (response.status === 429 || response.status >= 500) {
      if (attempt === maxRetries) {
        throw new Error(`Request failed: ${response.status} ${response.statusText}`)
      }
      const jitter = Math.floor(Math.random() * 500)
      await sleep(delay + jitter)
      delay = Math.min(delay * 2, 8000)
      continue
    }
    if (response.status === 404) return ""
    if (!response.ok) {
      throw new Error(`Request failed: ${response.status} ${response.statusText}`)
    }
    return response.text()
  }
  throw new Error("Request failed after max retries")
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

export interface JobCard {
  id: string
  title: string
  company: string | null
  location: string | null
  date: string | null
  url: string
  terms: string[]
}

export interface JobDetail extends JobCard {
  description: string | null
  employmentType: string | null
  validThrough: string | null
  addressLocality: string | null
  addressRegion: string | null
  addressCountry: string | null
  jsonLdFound: boolean
}

function numericEntity(cp: number): string {
  return cp >= 0 && cp <= 0x10ffff ? String.fromCodePoint(cp) : ""
}

function decodeHtmlEntities(text: string): string {
  return text
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    .replace(/&#(\d+);/g, (_, dec) => numericEntity(parseInt(dec, 10)))
    .replace(/&#[xX]([0-9a-fA-F]+);/g, (_, hex) => numericEntity(parseInt(hex, 16)))
    .replace(/&nbsp;/g, " ")
}

function stripTags(html: string): string {
  return html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim()
}

function clean(html: string): string {
  return decodeHtmlEntities(stripTags(html))
}

/**
 * Parse the search-results listing: a flat list of Drupal `article
 * id="node-<id>"` job cards (one per `views-row`). We split on the article
 * boundary and parse each chunk independently so one malformed card cannot
 * break the rest.
 */
export function parseJobCards(html: string): JobCard[] {
  const results: JobCard[] = []
  const chunks = html.split(/<article id="node-(\d+)"/).slice(1)

  // .split() with a capturing group interleaves [id, chunk, id, chunk, ...].
  for (let i = 0; i < chunks.length; i += 2) {
    const id = chunks[i]
    const chunk = chunks[i + 1] ?? ""
    if (!id || !chunk) continue

    // Attribute order on the real markup is `href="..." class="recruiter-job-link" title="...">`
    // (href precedes class) — matched order-independently via lookaheads so
    // either attribute ordering still resolves.
    const linkMatch = chunk.match(
      /<a(?=[^>]*class="recruiter-job-link")(?=[^>]*href="([^"]+)")[^>]*>([\s\S]*?)<\/a>/i,
    )
    if (!linkMatch) continue
    const url = decodeHtmlEntities(linkMatch[1])
    const title = clean(linkMatch[2])
    if (!title) continue

    const companyMatch = chunk.match(
      /class="recruiter-company-profile-job-organization"[^>]*>([\s\S]*?)<\/span>/i,
    )
    const company = companyMatch ? clean(companyMatch[1]) || null : null

    const dateMatch = chunk.match(/class="date"[^>]*>([\s\S]*?)<\/span>/i)
    const date = dateMatch ? clean(dateMatch[1]).replace(/,\s*$/, "") || null : null

    const locationMatch = chunk.match(/class="location"[^>]*>\s*<span>([\s\S]*?)<\/span>/i)
    const location = locationMatch ? clean(locationMatch[1]) || null : null

    const termsMatch = chunk.match(/class="terms"[^>]*>([\s\S]*?)<\/div>/i)
    const terms = termsMatch
      ? clean(termsMatch[1])
          .split("|")
          .map((t) => t.trim())
          .filter(Boolean)
      : []

    results.push({ id, title, company, location, date, url, terms })
  }

  return results
}

/** Extract the pagination bounds from a listing page's page-number links
 * (`?page=N`, 0-indexed by the site). Returns the highest page index found,
 * or 0 when no pager is present (a single page of results). */
export function maxPageIndex(html: string): number {
  const matches = [...html.matchAll(/[?&]page=(\d+)/g)].map((m) => parseInt(m[1], 10))
  return matches.length ? Math.max(...matches) : 0
}

/** Extract the first schema.org/JobPosting block from a detail page's
 * JSON-LD scripts, or null if none is present or none parses. Detail pages
 * commonly carry several unrelated JSON-LD blocks (WebSite, Organization) —
 * this picks the one with "@type":"JobPosting" specifically rather than
 * assuming position. */
export function extractJobPostingJsonLd(html: string): Record<string, unknown> | null {
  const blocks = [...html.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/gi)]
  for (const block of blocks) {
    let parsed: unknown
    try {
      parsed = JSON.parse(block[1])
    } catch {
      continue
    }
    if (
      parsed !== null &&
      typeof parsed === "object" &&
      (parsed as Record<string, unknown>)["@type"] === "JobPosting"
    ) {
      return parsed as Record<string, unknown>
    }
  }
  return null
}

/** Strip a JobPosting JSON-LD description's HTML into readable prose. */
function cleanDescriptionHtml(html: string): string {
  const withBreaks = html
    .replace(/<\s*br\s*\/?>/gi, "\n")
    .replace(/<\/(p|li|ul|ol|div|h\d)>/gi, "\n")
  return decodeHtmlEntities(stripTags(withBreaks)).replace(/\n{3,}/g, "\n\n").trim()
}

function stringField(obj: Record<string, unknown>, key: string): string | null {
  const value = obj[key]
  return typeof value === "string" && value.trim() ? value : null
}

/**
 * Parse a job detail page: prefers the schema.org/JobPosting JSON-LD block
 * when present (structured, reliable — title/company/location/description
 * come from there); falls back to the listing-card fields passed in when
 * JSON-LD is absent or malformed, so a page without JSON-LD still yields a
 * usable (if thinner) detail record rather than failing outright.
 */
export function parseJobDetail(html: string, card: JobCard): JobDetail {
  const jsonLd = extractJobPostingJsonLd(html)

  if (!jsonLd) {
    return {
      ...card,
      description: null,
      employmentType: null,
      validThrough: null,
      addressLocality: null,
      addressRegion: null,
      addressCountry: null,
      jsonLdFound: false,
    }
  }

  const title = stringField(jsonLd, "title") || card.title
  const rawDescription = stringField(jsonLd, "description")
  const description = rawDescription ? cleanDescriptionHtml(rawDescription) || null : null

  const employmentTypeRaw = jsonLd["employmentType"]
  const employmentType = Array.isArray(employmentTypeRaw)
    ? (employmentTypeRaw.find((v) => typeof v === "string") as string | undefined) ?? null
    : typeof employmentTypeRaw === "string"
      ? employmentTypeRaw
      : null

  const validThrough = stringField(jsonLd, "validThrough")

  let addressLocality: string | null = null
  let addressRegion: string | null = null
  let addressCountry: string | null = null
  const jobLocation = jsonLd["jobLocation"]
  const firstLocation = Array.isArray(jobLocation) ? jobLocation[0] : jobLocation
  if (firstLocation && typeof firstLocation === "object") {
    const address = (firstLocation as Record<string, unknown>)["address"]
    if (address && typeof address === "object") {
      const a = address as Record<string, unknown>
      addressLocality = stringField(a, "addressLocality")
      addressRegion = stringField(a, "addressRegion")
      addressCountry = stringField(a, "addressCountry")
    }
  }

  const hiringOrganization = jsonLd["hiringOrganization"]
  const company =
    hiringOrganization && typeof hiringOrganization === "object"
      ? stringField(hiringOrganization as Record<string, unknown>, "name") || card.company
      : card.company

  const datePosted = stringField(jsonLd, "datePosted")

  return {
    ...card,
    title,
    company,
    date: datePosted || card.date,
    description,
    employmentType,
    validThrough,
    addressLocality,
    addressRegion,
    addressCountry,
    jsonLdFound: true,
  }
}
