import {
  BASE_URL,
  htmlFetch,
  parseJobCards,
  parseResultSummary,
  referenceFromUrlForDisplay,
  writeError,
  type JobCard,
} from "../helpers.js"

export interface SearchOpts {
  query?: string
  location?: string
  page: number
  limit?: number
  format: "json" | "table" | "plain"
}

// Airswift's listing URL is plain query params against /jobs -- both
// `search` and `location` are optional and server-applied (confirmed
// during reconnaissance: response content/size varies per query rather
// than being a static SPA shell). Unlike energy-jobline-search, no keyword
// default is needed -- an empty `search` still returns every job, filtered
// by `location` alone when only that's given.
function buildUrl(opts: SearchOpts): string {
  const params = new URLSearchParams()
  if (opts.query) params.set("search", opts.query)
  if (opts.location) params.set("location", opts.location)
  if (opts.page > 1) params.set("page_num", String(opts.page))
  const qs = params.toString()
  return qs ? `${BASE_URL}/jobs?${qs}` : `${BASE_URL}/jobs`
}

function renderTable(cards: JobCard[]): string {
  if (cards.length === 0) return "No results."
  const rows = cards.map((c) => {
    const title = (c.title || "").slice(0, 42).padEnd(42)
    const loc = (c.location || "—").slice(0, 28).padEnd(28)
    const type = (c.employmentType || "—").slice(0, 12).padEnd(12)
    const date = c.date || "—"
    return `${referenceFromUrlForDisplay(c.url).padEnd(11)} ${title} ${loc} ${type} ${date}`
  })
  const header =
    "ID".padEnd(11) +
    " " +
    "TITLE".padEnd(42) +
    " " +
    "LOCATION".padEnd(28) +
    " " +
    "TYPE".padEnd(12) +
    " DATE"
  return [header, "-".repeat(header.length), ...rows].join("\n")
}

export async function runSearch(opts: SearchOpts): Promise<number> {
  try {
    const html = await htmlFetch(buildUrl(opts))
    let cards = parseJobCards(html)
    if (opts.limit !== undefined && opts.limit >= 0) cards = cards.slice(0, opts.limit)
    const summary = parseResultSummary(html)

    if (opts.format === "table") {
      process.stdout.write(renderTable(cards) + "\n")
    } else if (opts.format === "plain") {
      process.stdout.write(
        cards
          .map(
            (c) =>
              `${c.title}\n  ${c.location || "—"} · ${c.employmentType || "—"} · ${c.date || "—"}\n  reference: ${referenceFromUrlForDisplay(c.url)}\n  ${c.url}`,
          )
          .join("\n\n") + "\n",
      )
    } else {
      process.stdout.write(
        JSON.stringify(
          {
            meta: {
              count: cards.length,
              page: opts.page,
              totalCount: summary?.count ?? cards.length,
              totalPages: summary?.pages ?? 1,
            },
            results: cards,
          },
          null,
          2,
        ) + "\n",
      )
    }
    return 0
  } catch (e) {
    writeError(e instanceof Error ? e.message : String(e), "SEARCH_FAILED")
    return 1
  }
}
