// Fixture HTML shaped after real airswift.com markup captured during
// reconnaissance and implementation (2026-09-18) -- a HubSpot-rendered
// job-listing card and a detail page's schema.org/JobPosting JSON-LD block.

export function listingCard(
  id: string,
  title: string,
  opts: {
    location?: string
    date?: string
    employmentType?: string
    summary?: string
    slug?: string
  } = {},
): string {
  const location = opts.location ?? "Aberdeen, United Kingdom"
  const date = opts.date ?? "18 Sep 2026"
  const employmentType = opts.employmentType ?? "Contract"
  const summary = opts.summary ?? ""
  const slug = opts.slug ?? title.toLowerCase().replace(/[^a-z0-9]+/g, "-")
  return `<article class="c-card-job-item">
                    <div class="c-card-job-item__top">
                        <p class="c-card-job-item__top-cell c-card-job-item__top-cell--left">
                            <img src="https://www.airswift.com/hubfs/__Carbon/svg/icons/contract.svg" class="c-card-job-item__top-icon" width="17" height="24" alt="Employment Type">
                            ${employmentType}
                        </p>
                        <p class="c-card-job-item__top-cell c-card-job-item__top-cell--right">
                            <img src="https://www.airswift.com/hubfs/__Carbon/svg/icons/calendar.svg" class="c-card-job-item__top-icon" width="23" height="24" alt="Date Published">
                            ${date}
                        </p>
                    </div>

                    <div>
                        <p class="c-card-job-item__location">
                            <img src="https://www.airswift.com/hubfs/__Carbon/svg/icons/location.svg" class="c-card-job-item__top-icon" width="17" height="24" alt="Location">
                            ${location}
                        </p>

                        <p class="c-card-job-item__title">
                            <a href="/jobs/${slug}-${id}">
                                ${title}
                            </a>
                        </p>

                        <p class="c-card-job-item__summary">
                            ${summary}
                        </p>
                    </div>

                    <div class="c-card-job-item__bottom">
                        <a class="c-button c-button--alpha" href="/jobs/${slug}-${id}" aria-label="Read more about ${title}">
                            View Job and Apply
                        </a>
                    </div>
                </article>`
}

export function listingPage(
  cards: string[],
  opts: { count?: number; pages?: number; pageNums?: number[] } = {},
): string {
  const count = opts.count ?? cards.length
  const pages = opts.pages ?? 1
  const pageNums = opts.pageNums ?? []
  const pager = pageNums.map((p) => `<a href="/jobs?page_num=${p}">${p}</a>`).join("\n")
  return `<!doctype html><html><body>
  <header>
    <p class="c-card-job-header__summary">
                Found ${count} job${count === 1 ? "" : "s"} on ${pages} page${pages === 1 ? "" : "s"}
            </p>
  </header>
  <div class="c-card-job__list">
    ${cards.join("\n")}
  </div>
  <div class="pager">${pager}</div>
  </body></html>`
}

export function jobPostingJsonLd(overrides: Record<string, unknown> = {}): string {
  const base = {
    "@context": "https://schema.org/",
    "@type": "JobPosting",
    title: "FPSO Piping Integration Coordinator",
    description: "<p>We are seeking an experienced Piping Integration Coordinator.</p>",
    datePosted: "2026-09-18",
    validThrough: "2026-11-17",
    employmentType: "Contract",
    hiringOrganization: {
      "@type": "Organization",
      name: "Airswift",
      sameAs: "https://www.airswift.com/",
      logo: "https://www.airswift.com/hubfs/mj-assets/images/airswift-logo%202.svg",
    },
    industry: { name: "Oil & Gas - Offshore Oil" },
    jobLocation: {
      "@type": "Place",
      address: {
        "@type": "PostalAddress",
        addressLocality: "Qidong",
        addressRegion: "",
        postalCode: "2221",
        addressCountry: "China",
      },
    },
    ...overrides,
  }
  return JSON.stringify(base)
}

export function detailPage(jsonLd: string | null, cardHtml = ""): string {
  const jsonLdBlock = jsonLd ? `<script type="application/ld+json">${jsonLd}</script>` : ""
  return `<!doctype html><html><head>${jsonLdBlock}</head><body>${cardHtml}</body></html>`
}
