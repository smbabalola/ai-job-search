import { describe, test, expect } from "bun:test"
import { parseJobCards, parseJobDetail, extractJobPostingJsonLd, maxPageIndex } from "../src/helpers"
import { listingCard, listingPage, jobPostingJsonLd, detailPage } from "./fixtures"

describe("parseJobCards", () => {
  test("extracts title, company, location, date, url, terms from a single card", () => {
    const html = listingPage([
      listingCard("31512381", "Senior Drilling Engineer in Aberdeen", {
        company: "Cammach",
        location: "Aberdeen, UK",
        date: "09/03/2026,",
        terms: ["engineer", "Senior", "Drilling"],
      }),
    ])
    const [card] = parseJobCards(html)
    expect(card.id).toBe("31512381")
    expect(card.title).toBe("Senior Drilling Engineer in Aberdeen")
    expect(card.company).toBe("Cammach")
    expect(card.location).toBe("Aberdeen, UK")
    expect(card.date).toBe("09/03/2026")
    expect(card.url).toBe(
      "https://www.energyjobline.com/job/senior-drilling-engineer-in-aberdeen-31512381",
    )
    expect(card.terms).toEqual(["engineer", "Senior", "Drilling"])
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
    const malformed = `<article id="node-999">no recruiter-job-link here</article>`
    const html = listingPage([listingCard("100", "Well Engineer"), malformed, listingCard("101", "Fluids Supervisor")])
    const cards = parseJobCards(html)
    expect(cards.map((c) => c.id)).toEqual(["100", "101"])
  })

  test("returns an empty list for a page with no job cards", () => {
    expect(parseJobCards("<html><body>No results</body></html>")).toEqual([])
  })

  test("decodes HTML entities in title and company", () => {
    const html = listingPage([
      listingCard("200", "Senior Engineer &amp; Lead", { company: "N&#xF8;rrebro ApS" }),
    ])
    const [card] = parseJobCards(html)
    expect(card.title).toBe("Senior Engineer & Lead")
    expect(card.company).toBe("Nørrebro ApS")
  })
})

describe("maxPageIndex", () => {
  test("finds the highest ?page=N value from pager links", () => {
    const html = listingPage([listingCard("1", "Engineer")], { pagerLinks: [1, 2, 3, 4] })
    expect(maxPageIndex(html)).toBe(4)
  })

  test("returns 0 when there is no pager", () => {
    const html = listingPage([listingCard("1", "Engineer")])
    expect(maxPageIndex(html)).toBe(0)
  })
})

describe("extractJobPostingJsonLd", () => {
  test("finds the JobPosting block among multiple JSON-LD scripts", () => {
    const html = detailPage(jobPostingJsonLd())
    const jsonLd = extractJobPostingJsonLd(html)
    expect(jsonLd).not.toBeNull()
    expect(jsonLd!["@type"]).toBe("JobPosting")
    expect(jsonLd!["title"]).toBe("Senior Drilling Engineer in Aberdeen")
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
    id: "31512381",
    title: "Senior Drilling Engineer in Aberdeen",
    company: "Cammach",
    location: "Aberdeen, UK",
    date: "09/03/2026",
    url: "https://www.energyjobline.com/job/senior-drilling-engineer-in-aberdeen-31512381",
    terms: ["engineer", "Drilling"],
  }

  test("prefers structured JSON-LD fields when present", () => {
    const html = detailPage(jobPostingJsonLd())
    const job = parseJobDetail(html, card)
    expect(job.jsonLdFound).toBe(true)
    expect(job.title).toBe("Senior Drilling Engineer in Aberdeen")
    expect(job.company).toBe("Cammach Recruitment")
    expect(job.employmentType).toBe("FULL_TIME")
    expect(job.validThrough).toBe("2026-10-02")
    expect(job.addressLocality).toBe("Aberdeen")
    expect(job.addressRegion).toBe("Scotland")
    expect(job.addressCountry).toBe("GB")
    expect(job.description).toContain("Leading Oil & Gas Operator")
    expect(job.description).toContain("Lead well planning")
  })

  test("falls back to the listing card's fields when JSON-LD is absent", () => {
    const html = detailPage(null)
    const job = parseJobDetail(html, card)
    expect(job.jsonLdFound).toBe(false)
    expect(job.title).toBe(card.title)
    expect(job.company).toBe(card.company)
    expect(job.location).toBe(card.location)
    expect(job.description).toBeNull()
    expect(job.employmentType).toBeNull()
  })

  test("falls back to the listing card's fields when JSON-LD is malformed", () => {
    const html = `<html><head><script type="application/ld+json">{broken</script></head></html>`
    const job = parseJobDetail(html, card)
    expect(job.jsonLdFound).toBe(false)
    expect(job.title).toBe(card.title)
  })

  test("handles a JobPosting block missing optional fields gracefully", () => {
    const minimal = jobPostingJsonLd({
      employmentType: undefined,
      jobLocation: undefined,
      hiringOrganization: undefined,
      validThrough: undefined,
    })
    const html = detailPage(minimal)
    const job = parseJobDetail(html, card)
    expect(job.jsonLdFound).toBe(true)
    expect(job.employmentType).toBeNull()
    expect(job.validThrough).toBeNull()
    expect(job.addressLocality).toBeNull()
    expect(job.company).toBe(card.company) // falls back to card when hiringOrganization absent
  })
})
