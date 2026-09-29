"""Bundle 6E-A timing constants and spike outcomes (spec §20, §21). The one
Python home for every submit timing value. extension/src/submit/constants.ts
declares the same values (milliseconds, `_MS` suffix), and
tests/product/test_submit_constants.py fails on any drift. The 6B
SUBMIT_GRANT_TTL and CLICK_DISPATCH_TTL stay in product.autonomy_contract."""
from __future__ import annotations

from datetime import timedelta

SUBMIT_TIMING_VERSION = "submit-timing.v1"
HUMAN_SUBMIT_CONTRACT = "human-submit.v1"

SUBMIT_REVIEW_OBSERVATION_MAX_AGE = timedelta(seconds=60)
REOBSERVATION_WAIT = timedelta(seconds=20)
SUBMIT_RESULT_WINDOW = timedelta(seconds=30)
SUBMIT_RESULT_POLL = timedelta(milliseconds=250)
CHALLENGE_HANDOFF_WINDOW = timedelta(seconds=300)
DISPATCH_RESULT_TIMEOUT = timedelta(seconds=360)

# Spike outcomes (docs/superpowers/notes/2026-09-29-6e-a-spike-results.md).
# S-E2: getMatchedRules reports an XHR submit's allow-rule match, but the
# browser acceptance suite showed Chrome drops a tab's record when a
# main-frame form POST does not commit a navigation (e.g. a 204). An empty
# list therefore proves nothing: the §21 fallback applies and matched-rule
# feedback is informational evidence only, never proof of non-submission.
# S-E4: a navigation confirmation page is observable after re-injection.
MATCHED_ALLOW_RULES_REPORTED = False
NAVIGATION_SUCCESS_OBSERVABLE = True
