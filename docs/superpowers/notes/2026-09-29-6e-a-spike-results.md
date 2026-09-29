# Bundle 6E-A spike results (Task 1)

These were run on fixtures in Playwright's Chromium 131.0.6778.33, with `--headless=new` and the packed `FILL_TEST_HOOKS` build. The suite is `tests/webapp/spikes/test_submit_spikes_browser.py`. It ran twice, and passed 5/5 both times.

| Spike | Result | Evidence |
|---|---|---|
| **S-E1 (gate)** | **PASS** | Under TOTAL (9111 + 9121) plus one priority-2000 tab-scoped `allow` rule (9201: `post`, main_frame and xmlhttprequest, `^http://127\.0\.0\.1:8420/acme/jobs/123$`), the React XHR POST reached the server. In the same run, a same-origin POST to `/acme/jobs/999` and an image GET to the third-party port 8431 recorded nothing. A tab-scoped allow for `/record/sw_fetch` let the page's own GET through, while the service worker's identical `fetch` (TAB_ID_NONE, blocked by Q2) recorded nothing. |
| **S-E2** | **PASS** | `getMatchedRules({tabId})`, with `declarativeNetRequestFeedback`, returned `[9201]` after the allowed POST. So allow-rule matches are reported, and `MATCHED_ALLOW_RULES_REPORTED = True`. |
| **S-E3 (gate)** | **PASS** | (a) An ISOLATED-world `element.click()` on the native form's submit button performed the main-frame POST, and the 303 then took the page to `/confirmation`. (b) The same click on a React-controlled form fired `onSubmit`, which sent the XHR POST and swapped in `#application_confirmation`. Neither page checks `event.isTrusted`. A page that does is only discoverable during live certification. |
| **S-E4** | **PASS** | With E1 (POST) and E2 (GET `/confirmation`) allowed, `tabs.onUpdated` reported the confirmation URL `complete`. A re-injected ISOLATED script read `#application_confirmation`. So `NAVIGATION_SUCCESS_OBSERVABLE = True`. |

The recording server logged exactly `POST submit:acme/123` then `GET confirm:acme/123`, and nothing else.

The spikes needed no spike bundle. The test-hook build's service worker calls `chrome.declarativeNetRequest` and `chrome.scripting.executeScript({world: "ISOLATED"})` directly, which are the same APIs production uses.
