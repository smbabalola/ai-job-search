// Bundle 6E-A timing constants and spike outcomes (spec §20, §21), in
// milliseconds. product/submit_constants.py declares the same values, and
// tests/product/test_submit_constants.py parses this file and fails on any
// drift. Keep each declaration on one line in this exact form.

export const SUBMIT_TIMING_VERSION = "submit-timing.v1";
export const HUMAN_SUBMIT_CONTRACT = "human-submit.v1";

export const SUBMIT_REVIEW_OBSERVATION_MAX_AGE_MS = 60_000;
export const REOBSERVATION_WAIT_MS = 20_000;
export const SUBMIT_RESULT_WINDOW_MS = 30_000;
export const SUBMIT_RESULT_POLL_MS = 250;
export const CHALLENGE_HANDOFF_WINDOW_MS = 300_000;
export const DISPATCH_RESULT_TIMEOUT_MS = 360_000;

export const MATCHED_ALLOW_RULES_REPORTED = false;
export const NAVIGATION_SUCCESS_OBSERVABLE = true;
