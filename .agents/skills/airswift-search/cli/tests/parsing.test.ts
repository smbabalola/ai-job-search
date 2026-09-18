import { describe, test, expect } from "bun:test"
import { parseJobCards, parseJobDetail, parseResultSummary, extractJobPostingJsonLd, maxPageNum, referenceFromUrlForDisplay } from "../src/helpers"
import { listingCard, listingPage, jobPostingJsonLd, detailPage } from "./fixtures"

describe("parseJobCards", () => {
  test("extracts title, location, date, employmentType, summary, url from a single card", () => {
    const html = listingPage([
      listingCard("1280556", "FPSO Piping Integration Coordinator", {
        location: "Qidong, China",
        date: "18 Sep 2026",
        employmentType: "Contract",
        summary: "Great opportunity offshore.",
      }),
    ])
    const [card] = parseJobCards(html)
    expect(card.title).toBe("FPSO Piping Integration Coordinator")
    expect(card.company).toBe("Airswift")
    expect(card.location).toBe("Qidong, China")
    expect(card.date).toBe("18 Sep 2026")
    expect(card.employmentType).toBe("Contract")
    expect(card.summary).toBe("Great opportunity offshore.")
    expect(card.url).toBe(
      "https://www.airswift.com/jobs/fpso-piping-integration-coordinator-1280556",
    )
    // JobCard.id is the full detail URL, not the bare numeric reference --
    // the discovery dispatcher threads this value straight through as the
    // detail command's only argument, and Airswift's /jobs/<slug>-<id>
    // route 404s on a wrong/placeholder slug, so a bare ID alone is not
    // independently resolvable.
    expect(card.id).toBe(card.url)
  })

  test("parses multiple cards independently", () => {
    const html = listingPage([
      listingCard("100", "Well Engineer"),
      listingCard("101", "Fluids Supervisor"),
      listingCard("102", "Planning Engineer"),
    ])
    const cards = parseJobCards(html)
    expect(cards).toHaveLength(3)
    expect(cards.map((c) => c.title)).toEqual(["Well Engineer", "Fluids Supervisor", "Planning Engineer"])
  })

  test("one malformed card does not break parsing of the rest", () => {
    const malformed = `<article class="c-card-job-item">no title link here</article>`
    const html = listingPage([listingCard("100", "Well Engineer"), malformed, listingCard("101", "Fluids Supervisor")])
    const cards = parseJobCards(html)
    expect(cards.map((c) => referenceFromUrlForDisplay(c.url))).toEqual(["100", "101"])
  })

  test("returns an empty list for a page with no job cards", () => {
    expect(parseJobCards("<html><body>No results</body></html>")).toEqual([])
  })

  test("decodes HTML entities in the title", () => {
    const html = listingPage([listingCard("200", "Senior Engineer &amp; Lead")])
    const [card] = parseJobCards(html)
    expect(card.title).toBe("Senior Engineer & Lead")
  })

  test("skips a card whose URL carries no extractable numeric reference", () => {
    const html = listingPage([listingCard("", "No Reference Job", { slug: "no-reference-job" })])
    expect(parseJobCards(html)).toEqual([])
  })
})

describe("parseResultSummary", () => {
  test("parses the plural 'Found N jobs on M pages' wording", () => {
    const html = listingPage([listingCard("1", "Engineer")], { count: 913, pages: 38 })
    expect(parseResultSummary(html)).toEqual({ count: 913, pages: 38 })
  })

  test("parses the singular 'Found 1 job on 1 page' wording", () => {
    const html = listingPage([listingCard("1", "Engineer")], { count: 1, pages: 1 })
    expect(parseResultSummary(html)).toEqual({ count: 1, pages: 1 })
  })

  test("returns null when the summary line is absent", () => {
    expect(parseResultSummary("<html><body>no summary here</body></html>")).toBeNull()
  })
})

describe("maxPageNum", () => {
  test("finds the highest ?page_num=N value from pager links", () => {
    const html = listingPage([listingCard("1", "Engineer")], { pageNums: [1, 2, 3, 4, 5] })
    expect(maxPageNum(html)).toBe(5)
  })

  test("returns 1 (not 0) when there is no pager, matching the site's 1-indexed convention", () => {
    const html = listingPage([listingCard("1", "Engineer")])
    expect(maxPageNum(html)).toBe(1)
  })
})

describe("extractJobPostingJsonLd", () => {
  test("finds the JobPosting block", () => {
    const html = detailPage(jobPostingJsonLd())
    const jsonLd = extractJobPostingJsonLd(html)
    expect(jsonLd).not.toBeNull()
    expect(jsonLd!["@type"]).toBe("JobPosting")
    expect(jsonLd!["title"]).toBe("FPSO Piping Integration Coordinator")
  })

  test("returns null when no JobPosting block is present", () => {
    const html = `<html><head><script type="application/ld+json">{"@type":"WebSite"}</script></head></html>`
    expect(extractJobPostingJsonLd(html)).toBeNull()
  })

  test("returns null when JSON-LD is malformed, without throwing", () => {
    const html = `<html><head><script type="application/ld+json">{not valid json</script></head></html>`
    expect(extractJobPostingJsonLd(html)).toBeNull()
  })

  test("returns null when there is no JSON-LD script at all", () => {
    expect(extractJobPostingJsonLd("<html><body>plain page</body></html>")).toBeNull()
  })
})

describe("parseJobDetail", () => {
  const card = {
    id: "https://www.airswift.com/jobs/fpso-piping-integration-coordinator-1280556",
    title: "FPSO Piping Integration Coordinator",
    company: "Airswift",
    location: "Qidong, China",
    date: "18 Sep 2026",
    employmentType: "Contract",
    summary: null,
    url: "https://www.airswift.com/jobs/fpso-piping-integration-coordinator-1280556",
  }

  test("prefers structured JSON-LD fields when present, and re-derives the numeric id from the URL", () => {
    const html = detailPage(jobPostingJsonLd())
    const job = parseJobDetail(html, card)
    expect(job.jsonLdFound).toBe(true)
    expect(job.id).toBe("1280556")
    expect(job.title).toBe("FPSO Piping Integration Coordinator")
    expect(job.company).toBe("Airswift")
    expect(job.employmentType).toBe("Contract")
    expect(job.validThrough).toBe("2026-11-17")
    expect(job.industry).toBe("Oil & Gas - Offshore Oil")
    expect(job.addressLocality).toBe("Qidong")
    expect(job.addressCountry).toBe("China")
    expect(job.description).toContain("Piping Integration Coordinator")
    expect(job.date).toBe("2026-09-18")
  })

  test("falls back to the listing card's fields when JSON-LD is absent", () => {
    const html = detailPage(null)
    const job = parseJobDetail(html, card)
    expect(job.jsonLdFound).toBe(false)
    expect(job.id).toBe("1280556")
    expect(job.title).toBe(card.title)
    expect(job.company).toBe(card.company)
    expect(job.location).toBe(card.location)
    expect(job.description).toBeNull()
    expect(job.industry).toBeNull()
  })

  test("falls back to the listing card's fields when JSON-LD is malformed", () => {
    const html = `<html><head><script type="application/ld+json">{broken</script></head></html>`
    const job = parseJobDetail(html, card)
    expect(job.jsonLdFound).toBe(false)
    expect(job.title).toBe(card.title)
    expect(job.id).toBe("1280556")
  })

  test("handles a JobPosting block missing optional fields gracefully", () => {
    const minimal = jobPostingJsonLd({
      employmentType: undefined,
      jobLocation: undefined,
      industry: undefined,
      validThrough: undefined,
    })
    const html = detailPage(minimal)
    const job = parseJobDetail(html, card)
    expect(job.jsonLdFound).toBe(true)
    expect(job.employmentType).toBe(card.employmentType) // falls back to card when absent
    expect(job.validThrough).toBeNull()
    expect(job.addressLocality).toBeNull()
    expect(job.industry).toBeNull()
    expect(job.company).toBe("Airswift") // never overridden by hiringOrganization
  })

  test("hiringOrganization.name is never used to override company, even if present and non-Airswift", () => {
    const jsonLd = jobPostingJsonLd({ hiringOrganization: { "@type": "Organization", name: "Some Client Co" } })
    const html = detailPage(jsonLd)
    const job = parseJobDetail(html, card)
    expect(job.company).toBe("Airswift")
  })

  test("employmentType as an array (like energy-jobline-search's shape) still resolves to a string", () => {
    const jsonLd = jobPostingJsonLd({ employmentType: ["FULL_TIME"] })
    const html = detailPage(jsonLd)
    const job = parseJobDetail(html, card)
    expect(job.employmentType).toBe("FULL_TIME")
  })
})

describe("referenceFromUrlForDisplay", () => {
  test("extracts the trailing numeric reference from a job URL", () => {
    expect(
      referenceFromUrlForDisplay("https://www.airswift.com/jobs/fpso-piping-integration-coordinator-1280556"),
    ).toBe("1280556")
  })

  test("falls back to returning the input unchanged when no reference is found", () => {
    expect(referenceFromUrlForDisplay("https://www.airswift.com/jobs/no-number-here")).toBe(
      "https://www.airswift.com/jobs/no-number-here",
    )
  })
})
