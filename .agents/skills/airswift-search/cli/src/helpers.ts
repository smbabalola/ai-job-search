// Data source: Airswift's public, server-rendered job listing and detail
// pages (airswift.com). No authentication required for discovery. Both the
// search results (?search=&location=, server-rendered on every GET — the
// query is applied server-side, confirmed by response size/content varying
// per query rather than a static SPA shell) and the individual job detail
// pages are ordinary server-rendered HTML, no API key. Detail pages carry a
// schema.org/JobPosting JSON-LD block, which we prefer over HTML scraping
// wherever it is present. robots.txt (fetched 2026-09-18) has no
// Crawl-delay directive and does not disallow /jobs or /jobs/*; we still
// self-impose a conservative delay as a courtesy, matching this repo's
// other adapters.

export const BASE_URL = "https://www.airswift.com"

export function writeError(error: string, code: string): void {
  process.stderr.write(JSON.stringify({ error, code }) + "\n")
}

const UA = "airswift-search-skill/1.0"

// No site-declared Crawl-delay exists for this source, unlike
// energy-jobline-search's robots.txt-mandated 10s. A shorter, self-imposed
// delay is still applied as a courtesy rather than firing requests back to
// back. Enforced as a minimum gap between the start of one request and the
// start of the next from this process — not a per-call sleep, so a single
// request pays no tax.
const CRAWL_DELAY_MS = 3_000

// Mutable holder (not a bare module-level `let`) so tests can reset it
// between cases via resetCrawlDelayState() — see energy-jobline-search's
// helpers.ts for the full rationale (Bun runs every test file in one
// process, so a real wait in one test would otherwise leak into another).
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

/** Fetch HTML with exponential backoff on 429/5xx and a self-imposed delay
 * enforced between requests. Returns "" on a 404 (matches the other
 * portal-skill adapters' htmlFetch contract). */
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
  employmentType: string | null
  summary: string | null
  url: string
}

export interface JobDetail extends JobCard {
  description: string | null
  validThrough: string | null
  industry: string | null
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

/** Extract the trailing numeric job reference from an Airswift job URL or
 * path, e.g. "/jobs/field-telecoms-engineer-1277009" -> "1277009". This
 * same number is independently confirmed on the detail page as the
 * labelled "Job reference" field — one number, one source of truth, unlike
 * energy-jobline-search's node ID (URL) vs identifier.value (JSON-LD)
 * mismatch. */
function referenceFromUrl(url: string): string | null {
  const m = url.match(/-(\d+)\/?(?:[?#]|$)/)
  return m ? m[1] : null
}

/** Public wrapper for referenceFromUrl, for callers (e.g. the search
 * command's table/plain renderers) that need the human-readable numeric
 * reference for display even though JobCard.id itself now carries the
 * full detail URL. */
export function referenceFromUrlForDisplay(url: string): string {
  return referenceFromUrl(url) ?? url
}

/**
 * Parse the search-results listing: a flat list of
 * `<article class="c-card-job-item">` cards. We split on the article
 * boundary and parse each chunk independently so one malformed card cannot
 * break the rest.
 */
export function parseJobCards(html: string): JobCard[] {
  const results: JobCard[] = []
  const chunks = html.split(/<article class="c-card-job-item">/).slice(1)

  for (const rawChunk of chunks) {
    // Each chunk runs to the end of the document; bound it at this card's
    // own closing tag so a later card's markup is never accidentally
    // matched by this card's field regexes.
    const closeIdx = rawChunk.indexOf("</article>")
    const chunk = closeIdx === -1 ? rawChunk : rawChunk.slice(0, closeIdx)

    const titleMatch = chunk.match(
      /class="c-card-job-item__title"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/i,
    )
    if (!titleMatch) continue
    const hrefRaw = decodeHtmlEntities(titleMatch[1])
    const url = hrefRaw.startsWith("http") ? hrefRaw : `${BASE_URL}${hrefRaw}`
    const title = clean(titleMatch[2])
    if (!title) continue

    // JobCard.id carries the full detail URL, not the bare numeric
    // reference. The discovery dispatcher (product/discovery_search.py)
    // threads a search result's "id" field straight through as the
    // detail command's only argument -- it never sees "url" separately.
    // Unlike energy-jobline-search's Drupal backend (which resolves a
    // detail page by node ID alone, slug is cosmetic), Airswift's
    // /jobs/<slug>-<id> route 404s on a wrong or placeholder slug
    // (confirmed directly), so a bare numeric ID is not independently
    // resolvable to a working URL. detail.ts's normalizeInput() consumes
    // this URL directly, and parseJobDetail() below re-derives the true
    // numeric reference from it for JobDetail.id, so the persisted
    // source_record_id still ends up as the clean number.
    if (!referenceFromUrl(url)) continue
    const id = url

    const employmentTypeMatch = chunk.match(
      /alt="Employment Type">([\s\S]*?)<\/p>/i,
    )
    const employmentType = employmentTypeMatch ? clean(employmentTypeMatch[1]) || null : null

    const dateMatch = chunk.match(/alt="Date Published">([\s\S]*?)<\/p>/i)
    const date = dateMatch ? clean(dateMatch[1]) || null : null

    const locationMatch = chunk.match(
      /class="c-card-job-item__location"[^>]*>[\s\S]*?alt="Location">([\s\S]*?)<\/p>/i,
    )
    const location = locationMatch ? clean(locationMatch[1]) || null : null

    const summaryMatch = chunk.match(
      /class="c-card-job-item__summary"[^>]*>([\s\S]*?)<\/p>/i,
    )
    const summary = summaryMatch ? clean(summaryMatch[1]) || null : null

    // No structured company field on the listing card or in JSON-LD --
    // Airswift's own name is always the only value available (it's a
    // staffing agency; the end client is not exposed as a discrete field).
    const company = "Airswift"

    results.push({ id, title, company, location, date, employmentType, summary, url })
  }

  return results
}

/** Parse the "Found N job(s) on M page(s)" summary line, handling the
 * singular/plural wording difference ("1 job" vs "N jobs") that a naive
 * "jobs" substring match would miss for exactly one result. Returns null
 * when the summary line isn't present (e.g. an unexpected page shape). */
export function parseResultSummary(html: string): { count: number; pages: number } | null {
  const m = html.match(
    /class="c-card-job-header__summary"[^>]*>\s*Found (\d+) jobs? on (\d+) pages?/i,
  )
  if (!m) return null
  return { count: parseInt(m[1], 10), pages: parseInt(m[2], 10) }
}

/** Extract the highest page_num referenced by the listing page's own
 * pagination links (1-indexed by the site, unlike energy-jobline-search's
 * 0-indexed ?page=). Returns 1 when no pager is present (a single page of
 * results) rather than 0, matching Airswift's 1-indexed convention. */
export function maxPageNum(html: string): number {
  const matches = [...html.matchAll(/[?&]page_num=(\d+)/g)].map((m) => parseInt(m[1], 10))
  return matches.length ? Math.max(...matches) : 1
}

/** Extract the first schema.org/JobPosting block from a detail page's
 * JSON-LD scripts, or null if none is present or none parses. Airswift
 * detail pages carry exactly one JSON-LD block in practice, but this
 * still filters by "@type":"JobPosting" defensively rather than assuming
 * position, matching energy-jobline-search's approach. */
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

function stringField(obj: Record<string, unknown>, key: string): string | null {
  const value = obj[key]
  return typeof value === "string" && value.trim() ? value : null
}

/**
 * Parse a job detail page: prefers the schema.org/JobPosting JSON-LD block
 * when present (structured, reliable — title/description/location come
 * from there); falls back to the listing-card fields passed in when
 * JSON-LD is absent or malformed, so a page without JSON-LD still yields a
 * usable (if thinner) detail record rather than failing outright.
 */
export function parseJobDetail(html: string, card: JobCard): JobDetail {
  const jsonLd = extractJobPostingJsonLd(html)
  // card.id is the detail URL (see parseJobCards) -- re-derive the stable
  // numeric job reference from it here so JobDetail.id (and therefore the
  // persisted source_record_id) is always the clean number, never a URL.
  const id = referenceFromUrl(card.url) ?? card.id

  if (!jsonLd) {
    return {
      ...card,
      id,
      description: null,
      validThrough: null,
      industry: null,
      addressLocality: null,
      addressRegion: null,
      addressCountry: null,
      jsonLdFound: false,
    }
  }

  const title = stringField(jsonLd, "title") || card.title
  const description = stringField(jsonLd, "description")
  const validThrough = stringField(jsonLd, "validThrough")

  const employmentTypeRaw = jsonLd["employmentType"]
  const employmentType = Array.isArray(employmentTypeRaw)
    ? (employmentTypeRaw.find((v) => typeof v === "string") as string | undefined) ?? null
    : typeof employmentTypeRaw === "string"
      ? employmentTypeRaw
      : card.employmentType

  const industryRaw = jsonLd["industry"]
  const industry =
    industryRaw && typeof industryRaw === "object"
      ? stringField(industryRaw as Record<string, unknown>, "name")
      : typeof industryRaw === "string"
        ? industryRaw
        : null

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

  // hiringOrganization.name on Airswift's own JSON-LD is always "Airswift"
  // itself (confirmed during reconnaissance) -- there is no structured
  // end-client field to prefer over the card's own "Airswift" value, so
  // this intentionally never overrides card.company the way
  // energy-jobline-search's hiringOrganization handling does.
  const company = card.company

  const datePosted = stringField(jsonLd, "datePosted")

  return {
    ...card,
    id,
    title,
    company,
    date: datePosted || card.date,
    description,
    employmentType,
    validThrough,
    industry,
    addressLocality,
    addressRegion,
    addressCountry,
    jsonLdFound: true,
  }
}
