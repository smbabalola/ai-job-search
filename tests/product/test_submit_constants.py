"""6E-A spec §20: submit-timing.v1 is named, versioned and identical in
Python and TypeScript (extension/src/submit/constants.ts is parsed here).
The spike outcome flags (spec §21) are mirrored too."""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from product import submit_constants as c

TS_PATH = Path(__file__).parents[2] / "extension" / "src" / "submit" / "constants.ts"
TIMING = {
    "SUBMIT_REVIEW_OBSERVATION_MAX_AGE": timedelta(seconds=60),
    "REOBSERVATION_WAIT": timedelta(seconds=20),
    "SUBMIT_RESULT_WINDOW": timedelta(seconds=30),
    "SUBMIT_RESULT_POLL": timedelta(milliseconds=250),
    "CHALLENGE_HANDOFF_WINDOW": timedelta(seconds=300),
    "DISPATCH_RESULT_TIMEOUT": timedelta(seconds=360),
}
FLAGS = {"MATCHED_ALLOW_RULES_REPORTED": False, "NAVIGATION_SUCCESS_OBSERVABLE": True}
STRINGS = {"SUBMIT_TIMING_VERSION": "submit-timing.v1", "HUMAN_SUBMIT_CONTRACT": "human-submit.v1"}


def _ts() -> dict[str, str]:
    source = TS_PATH.read_text(encoding="utf-8")
    return dict(re.findall(r"^export const ([A-Z_]+)\s*=\s*([^;]+);", source, flags=re.MULTILINE))


def test_python_declares_the_frozen_values():
    for name, value in {**TIMING, **FLAGS, **STRINGS}.items():
        assert getattr(c, name) == value, name


def test_dispatch_result_timeout_is_the_challenge_window_plus_a_minute():
    assert c.DISPATCH_RESULT_TIMEOUT == c.CHALLENGE_HANDOFF_WINDOW + timedelta(seconds=60)


def test_typescript_declares_the_same_values():
    ts = _ts()
    for name, value in TIMING.items():
        assert int(ts[f"{name}_MS"].replace("_", "")) == int(value / timedelta(milliseconds=1)), name
    for name, value in FLAGS.items():
        assert ts[name] == ("true" if value else "false"), name
    for name, value in STRINGS.items():
        assert ts[name] == f'"{value}"', name


def test_no_undeclared_constant_on_either_side():
    assert set(_ts()) == {f"{n}_MS" for n in TIMING} | set(FLAGS) | set(STRINGS)
    assert {n for n in vars(c) if n.isupper()} == set(TIMING) | set(FLAGS) | set(STRINGS)
