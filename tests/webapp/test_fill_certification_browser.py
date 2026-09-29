"""6D-B certification (spec §7.4, §10.8): each certified adapter version's
fixture pages use no channel the communications reset and Q1/Q2 can't
contain (the Task 2 certify.uncontained_channels checker), and the
refusal fixture is flagged, so the check isn't vacuous."""
from __future__ import annotations

import pytest

from product.fill_certification import CATALOGUE
from tests.webapp.fixtures.fill.certify import uncontained_channels
from tests.webapp.fixtures.fill.recording_server import PORT, Recorder, RunningServer

BASE = f"http://127.0.0.1:{PORT}"
FIXTURE_PAGES = {("greenhouse", "greenhouse@2"): "certified_greenhouse.html",
                 ("lever", "lever@2"): "certified_lever.html"}


@pytest.fixture(scope="module")
def server():
    with RunningServer(Recorder()):
        yield BASE


def test_every_catalogue_entry_has_a_fixture_page():
    assert set(FIXTURE_PAGES) == set(CATALOGUE)


@pytest.mark.parametrize("key", sorted(FIXTURE_PAGES))
def test_certified_fixture_pages_have_no_uncontained_channel(context, server, key):
    page = context.new_page()
    page.goto(f"{server}/{FIXTURE_PAGES[key]}")
    page.wait_for_selector("#application_form")
    page.close()
    assert uncontained_channels(context, f"{server}/{FIXTURE_PAGES[key]}") == []


def test_the_checker_is_not_vacuous(context, server):
    assert uncontained_channels(context, f"{server}/sw_socket_page.html") == ["SW_PERSISTENT_CHANNEL"]
