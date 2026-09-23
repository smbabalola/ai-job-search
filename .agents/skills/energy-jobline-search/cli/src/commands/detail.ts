import { BASE_URL, htmlFetch, parseJobDetail, writeError, type JobCard } from "../helpers.js"

export interface DetailOpts {
  id: string
  format: "json" | "plain"
}

/** Accept a raw node ID or a full /job/<slug>-<id> detail URL. */
function normalizeId(input: string): string | null {
  const url = input.match(/-(\d+)(?:\?|$)/)
  if (url) return url[1]
  const bare = input.match(/^\d+$/)
  if (bare) return input
  return null
}

export async function runDetail(opts: DetailOpts): Promise<number> {
  const id = normalizeId(opts.id)
  if (!id) {
    writeError(`Could not parse a job ID from "${opts.id}"`, "BAD_ID")
    return 1
  }
  // energyjobline.com detail URLs are /job/<slug>-<id> — the slug portion
  // has no bearing on which node loads (Drupal resolves by node ID), so a
  // bare-ID lookup is passed the ID as its own slug and still resolves.
  const url = opts.id.startsWith("http") ? opts.id : `${BASE_URL}/job/${id}`
  try {
    const html = await htmlFetch(url)
    if (!html) {
      writeError("Job not found", "NOT_FOUND")
      return 1
    }
    const card: JobCard = { id, title: "", company: null, location: null, date: null, url, terms: [] }
    const job = parseJobDetail(html, card)
    if (!job.title) {
      writeError("Job not found", "NOT_FOUND")
      return 1
    }

    if (opts.format === "plain") {
      const lines = [
        job.title,
        `${job.company || "—"} · ${job.location || "—"}`,
        "",
        job.employmentType ? `Employment: ${job.employmentType}` : "",
        job.validThrough ? `Valid through: ${job.validThrough}` : "",
        "",
        job.description || "(no description)",
        "",
        `URL: ${job.url}`,
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
