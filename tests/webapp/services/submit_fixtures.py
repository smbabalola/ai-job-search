"""A real 6E-A service world: the 6D-B grant world driven through the real
fill services to FILLED_AWAITING_SUBMISSION, with human submission enabled.

The 6D-B world's employer origin (https://jobs.example.test) stands in for a
loopback fixture origin: submission_permitted's loopback pattern is widened
to include it for these service tests only. The production loopback rule is
proven by tests/product/test_submit_certification.py and the browser suite."""
from __future__ import annotations

import dataclasses
import re
from datetime import timedelta

import pytest

from product import submit_certification
from tests.webapp.services.fill_fixtures import NOW, V2_ACCOUNT, grant_world, v2_chain  # noqa: F401
from tests.webapp.services.test_fill_actions import filling, page_after, run_all

ORIGIN = "https://jobs.example.test"
SEC = timedelta(seconds=1)


def widen_loopback(monkeypatch):
    monkeypatch.setattr(submit_certification, "_LOOPBACK",
                        re.compile(r"^(http://(127\.0\.0\.1|localhost)(:\d{1,5})?|https://jobs\.example\.test)$"))


def review_observation(w, *extra):
    """The page exactly as the fill left it (equal to the FINAL observation)."""
    return page_after(w, 5, *extra)


@pytest.fixture
def filled_world(grant_world, monkeypatch):  # noqa: F811
    from webapp.services import fill_actions as fa
    widen_loopback(monkeypatch)
    grant_world.settings = dataclasses.replace(grant_world.settings, human_submit_enabled=True,
                                               submit_fixture_origins_enabled=True)
    run = filling(grant_world)
    run_all(grant_world, run)
    assert fa.final_validate(grant_world.conn, run_id=run["id"], observation=review_observation(grant_world),
                             now=NOW) == {"state": "FILLED_AWAITING_SUBMISSION"}
    grant_world.run = run
    grant_world.account_id = V2_ACCOUNT
    return grant_world
