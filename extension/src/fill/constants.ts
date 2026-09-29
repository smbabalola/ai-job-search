// Bundle 6D-B timing constants (spec §11.5), in milliseconds. The one
// TypeScript home for every fill timing value. product/fill_constants.py
// declares the same version and values, and
// tests/product/test_fill_constants.py parses this file and fails on any
// drift. Keep each declaration on one line in this exact form.

export const FILL_TIMING_VERSION = "fill-timing.v1";

export const HEARTBEAT_INTERVAL_MS = 10_000;
export const RUN_LEASE_TTL_MS = 45_000;
export const VALUE_ENVELOPE_TTL_MS = 30_000;
export const SETTLE_QUIET_PERIOD_MS = 50;
export const SETTLE_CAP_MS = 1_000;
export const REVALIDATION_MOUNT_TIMEOUT_MS = 10_000;
