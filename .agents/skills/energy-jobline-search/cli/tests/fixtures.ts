// Fixture HTML shaped after real energyjobline.com markup observed during
// reconnaissance (2026-09-17) — a Drupal Views job-listing card and a
// detail page's schema.org/JobPosting JSON-LD block.

export function listingCard(
  id: string,
  title: string,
  opts: { company?: string; location?: string; date?: string; terms?: string[] } = {},
): string {
  const company = opts.company ?? "Example Recruiter"
  const location = opts.location ?? "Aberdeen, UK"
  const date = opts.date ?? "09/03/2026,"
  const terms = (opts.terms ?? ["engineer", "Drilling"]).join(" | ")
  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, "-")
  return `<article id="node-${id}"  about="/job/${slug}-${id}" typeof="sioc:Item foaf:Document" class="node node--job-per-template node-teaser">
  <div class="job__content clearfix" class="node__content">
    <h2 class="node__title">
      <a href="https://www.energyjobline.com/job/${slug}-${id}" class="recruiter-job-link" title="${title}">
        ${title}      </a>
    </h2>
    <div class="description">
      <span class="date">
                  ${date}               </span>
        <span class="recruiter-company-profile-job-organization"><a href="https://www.energyjobline.com/company/example">${company}</a></span>    </div>
          <div class="location">
        <span>${location}</span>      </div>
    <div class="terms">
      ${terms}    </div>
        </div>
</article>`
}

export function listingPage(cards: string[], opts: { pagerLinks?: number[] } = {}): string {
  const pager = (opts.pagerLinks ?? []).map((p) => `<a href="/jobs/engineering/energy-jobline-zr/aberdeen?page=${p}">${p + 1}</a>`).join("\n")
  return `<!doctype html><html><body>
  <div class="view-content">
    ${cards.join("\n")}
  </div>
  <div class="pager">${pager}</div>
  </body></html>`
}

export function jobPostingJsonLd(overrides: Record<string, unknown> = {}): string {
  const base = {
    "@context": "http://schema.org",
    "@type": "JobPosting",
    title: "Senior Drilling Engineer in Aberdeen",
    datePosted: "2026-09-03",
    validThrough: "2026-10-02",
    hiringOrganization: { "@type": "Organization", name: "Cammach Recruitment" },
    employmentType: ["FULL_TIME"],
    jobLocation: [
      {
        "@type": "Place",
        address: {
          "@type": "PostalAddress",
          addressCountry: "GB",
          addressLocality: "Aberdeen",
          addressRegion: "Scotland",
        },
      },
    ],
    description:
      "<p>Job Description</p><p>Leading Oil &amp; Gas Operator seeking a Senior Drilling Engineer.</p><ul><li>Lead well planning</li><li>Coordinate the well engineering team</li></ul>",
    ...overrides,
  }
  return JSON.stringify(base)
}

export function detailPage(jsonLd: string | null, cardHtml = ""): string {
  const jsonLdBlock = jsonLd
    ? `<script type="application/ld+json">${JSON.stringify({ "@context": "http://schema.org", "@type": "WebSite", name: "Energy Jobline" })}</script>
       <script type="application/ld+json">${jsonLd}</script>`
    : ""
  return `<!doctype html><html><head>${jsonLdBlock}</head><body>${cardHtml}</body></html>`
}
