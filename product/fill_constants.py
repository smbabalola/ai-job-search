"""Bundle 6D-B timing constants (spec §11.5). The one Python home for every
fill timing value. extension/src/fill/constants.ts declares the same
version and values (in milliseconds, with an `_MS` suffix), and
tests/product/test_fill_constants.py fails on any drift. A run records
FILL_TIMING_VERSION in its identity."""
from __future__ import annotations

from datetime import timedelta

FILL_TIMING_VERSION = "fill-timing.v1"

HEARTBEAT_INTERVAL = timedelta(seconds=10)
RUN_LEASE_TTL = timedelta(seconds=45)
VALUE_ENVELOPE_TTL = timedelta(seconds=30)
SETTLE_QUIET_PERIOD = timedelta(milliseconds=50)
SETTLE_CAP = timedelta(seconds=1)
REVALIDATION_MOUNT_TIMEOUT = timedelta(seconds=10)
