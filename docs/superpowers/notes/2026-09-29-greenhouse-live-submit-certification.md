# Greenhouse live submit certification: runbook

`greenhouse@2/submit@1` is **FIXTURE_CERTIFIED**. Its egress manifest, submit control, success/failure signals and challenge selectors were proven only against fixtures (6E-A spikes S-E1..S-E4 and the browser acceptance suite). Until this runbook's evidence exists, `submission_permitted()` refuses every real Greenhouse origin with `adapter_not_live_certified`, and the Submit Review keeps the button disabled.

Flipping the status is a **reviewed code change**, never a runtime toggle.

## Evidence required

Record all of it in a new note: `docs/superpowers/notes/<date>-greenhouse-live-submit-evidence.md`.

1. **A posting you control.** The note records:
   - its URL, tenant key and job id;
   - who owns the employer account;
   - written confirmation that test applications to it are expected and will be discarded.

   Never certify against a real employer's live posting.
2. **Chrome version** and extension build commit.
3. **The complete request list of one real submission**, captured with DevTools or `chrome://net-export`. It must show:
   - every request leaving the tab from the click until the confirmation page settles: method, URL, resource type and initiator;
   - that each one matches exactly one entry of `resolve_egress(...)` for this posting;
   - that no request the submission needs is missing from the manifest (**sufficient**);
   - that no manifest entry is unused, or, if one is, the reason is documented (**minimal**). C1 (reCAPTCHA) is only kept if the posting actually loads a challenge provider.
4. **The success signal.** A screenshot of the confirmation page, its exact URL, and proof that `#application_confirmation` (or the corrected selector) is present only on success.
5. **The failure signal.** A submission rejected by server validation (e.g. a missing required field), with a screenshot and the selector. Record whether the rejection stored anything. Keep `failure_signal_proves_not_submitted = True` only if the employer confirms nothing was stored.
6. **Challenge behaviour.** Whether a visible challenge appeared, what it looked like, which selectors matched it, and the result of completing it by hand during the handoff window.
7. **The submit control.** Proof that `#application_form [type=submit]` matches exactly one control and that its `fill-submit-control` v1 fingerprint is stable across reloads. Also confirm the page accepts an ISOLATED-world `.click()`, i.e. its handler does not check `event.isTrusted`.
8. **Reviewer sign-off**: a person other than the author checked items 1–7.

## Flipping the status

In one commit:

- `product/submit_certification.py`: set `status=LIVE_CERTIFIED` and `live_evidence="docs/superpowers/notes/<date>-greenhouse-live-submit-evidence.md"`. Correct any selector, template or flag that the evidence contradicts.
- `extension/src/submit/certification.ts`: the same values. Then regenerate `tests/fixtures/submit/egress_vectors.json` if the egress changed, and keep both suites green.
- `tests/product/test_submit_certification.py`: update the catalogue test to the new status, and add a test that the live origin is permitted **only** with the evidence path set.
- The deployment switch `JOBSEARCH_HUMAN_SUBMIT_ENABLED` still has to be turned on explicitly per deployment.

If any evidence item fails, the status stays `FIXTURE_CERTIFIED`. The fix goes back through the 6E-A design (spec §9), not into this runbook.
