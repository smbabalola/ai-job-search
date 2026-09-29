"""6D-B spec §11.5: the timing constants are named, versioned and identical
in Python and TypeScript. The TypeScript file is parsed here, so any drift in
either language fails this test."""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from product import fill_constants as c

TS_PATH = Path(__file__).parents[2] / "extension" / "src" / "fill" / "constants.ts"
EXPECTED = {
    "HEARTBEAT_INTERVAL": timedelta(seconds=10),
    "RUN_LEASE_TTL": timedelta(seconds=45),
    "VALUE_ENVELOPE_TTL": timedelta(seconds=30),
    "SETTLE_QUIET_PERIOD": timedelta(milliseconds=50),
    "SETTLE_CAP": timedelta(seconds=1),
    "REVALIDATION_MOUNT_TIMEOUT": timedelta(seconds=10),
}


def _ts_declarations() -> dict[str, str]:
    source = TS_PATH.read_text(encoding="utf-8")
    return dict(re.findall(r"^export const ([A-Z_]+)\s*=\s*([^;]+);", source, flags=re.MULTILINE))


def test_python_declares_the_frozen_v1_values():
    assert c.FILL_TIMING_VERSION == "fill-timing.v1"
    for name, value in EXPECTED.items():
        assert getattr(c, name) == value, name


def test_typescript_declares_the_same_version_and_values():
    ts = _ts_declarations()
    assert ts["FILL_TIMING_VERSION"] == '"fill-timing.v1"'
    for name, value in EXPECTED.items():
        literal = ts[f"{name}_MS"].replace("_", "")
        assert int(literal) == int(value / timedelta(milliseconds=1)), name


def test_no_undeclared_timing_constant_on_either_side():
    ts_names = {name for name in _ts_declarations() if name != "FILL_TIMING_VERSION"}
    assert ts_names == {f"{name}_MS" for name in EXPECTED}
    py_names = {name for name in vars(c) if name.isupper() and name != "FILL_TIMING_VERSION"}
    assert py_names == set(EXPECTED)
