import { htmlFetch, parseJobDetail, writeError, type JobCard } from "../helpers.js"

export interface DetailOpts {
  id: string
  format: "json" | "plain"
}

/** Accept a full /jobs/<slug>-<id> detail URL only. Airswift's detail path
 * has no separate "slug lookup" endpoint -- confirmed directly that an
 * arbitrary or placeholder slug 404s even with the correct numeric ID, so
 * a bare-ID lookup cannot reconstruct the slug. This is why
 * helpers.ts's parseJobCards puts the full URL (not the bare numeric
 * reference) in each search result's "id" field: the discovery
 * dispatcher (product/discovery_search.py) threads that value straight
 * through as this command's only argument, so it must be
 * self-sufficient to resolve a working URL on its own. */
function normalizeInput(input: string): { url: string; id: string } | null {
  if (input.startsWith("http")) {
    const m = input.match(/-(\d+)\/?(?:[?#]|$)/)
    return m ? { url: input, id: m[1] } : null
  }
  return null
}

export async function runDetail(opts: DetailOpts): Promise<number> {
  const normalized = normalizeInput(opts.id)
  if (!normalized) {
    writeError(
      `could not resolve a job detail URL from "${opts.id}" -- pass the full https://www.airswift.com/jobs/<slug>-<id> URL from a search result`,
      "BAD_ID",
    )
    return 1
  }
  try {
    const html = await htmlFetch(normalized.url)
    if (!html) {
      writeError("Job not found", "NOT_FOUND")
      return 1
    }
    const card: JobCard = {
      id: normalized.id,
      title: "",
      company: "Airswift",
      location: null,
      date: null,
      employmentType: null,
      summary: null,
      url: normalized.url,
    }
    const job = parseJobDetail(html, card)
    if (!job.title) {
      writeError("Job not found", "NOT_FOUND")
      return 1
    }

    if (opts.format === "plain") {
      const lines = [
        job.title,
        `${job.company} · ${job.location || "—"}`,
        "",
        job.employmentType ? `Employment: ${job.employmentType}` : "",
        job.industry ? `Industry: ${job.industry}` : "",
        job.validThrough ? `Valid through: ${job.validThrough}` : "",
        "",
        job.description || "(no description)",
        "",
        `URL: ${job.url}`,
        `Job reference: ${job.id}`,
      ].filter((l) => l !== "")
      process.stdout.write(lines.join("\n") + "\n")
    } else {
      process.stdout.write(JSON.stringify(job, null, 2) + "\n")
    }
    return 0
  } catch (e) {
    writeError(e instanceof Error ? e.message : String(e), "DETAIL_FAILED")
    return 1
  }
}
