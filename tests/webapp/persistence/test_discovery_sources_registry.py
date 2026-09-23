from __future__ import annotations

import pytest

from webapp.persistence.db import connect, init_db
from webapp.persistence.discovery_sources import (
    list_discovery_source_settings,
    list_enabled_discovery_source_ids,
    set_discovery_source_enabled,
)


def _connection(tmp_path):
    db_path = tmp_path / "jobsearch.sqlite3"
    init_db(db_path)
    return connect(db_path)


def test_list_discovery_source_settings_returns_display_names(tmp_path):
    conn = _connection(tmp_path)
    rows = list_discovery_source_settings(conn)
    by_id = {row["source_id"]: row for row in rows}
    assert by_id["freehire-search"]["display_name"] == "Freehire"
    assert by_id["linkedin-search"]["display_name"] == "LinkedIn"
    assert by_id["energy-jobline-search"]["display_name"] == "Energy Jobline"
    assert by_id["airswift-search"]["display_name"] == "Airswift"
    assert all(row["enabled"] is True for row in rows)


def test_list_enabled_discovery_source_ids_reflects_all_enabled_by_default(tmp_path):
    conn = _connection(tmp_path)
    ids = list_enabled_discovery_source_ids(conn)
    assert set(ids) == {
        "freehire-search",
        "linkedin-search",
        "energy-jobline-search",
        "airswift-search",
    }


def test_set_discovery_source_enabled_disables_and_reenables(tmp_path):
    conn = _connection(tmp_path)

    updated = set_discovery_source_enabled(conn, "energy-jobline-search", False)
    assert updated["enabled"] is False
    ids = list_enabled_discovery_source_ids(conn)
    assert "energy-jobline-search" not in ids
    assert set(ids) == {"freehire-search", "linkedin-search", "airswift-search"}

    updated = set_discovery_source_enabled(conn, "energy-jobline-search", True)
    assert updated["enabled"] is True
    ids = list_enabled_discovery_source_ids(conn)
    assert set(ids) == {
        "freehire-search",
        "linkedin-search",
        "energy-jobline-search",
        "airswift-search",
    }


def test_airswift_can_be_disabled_and_disappears_from_enabled_ids(tmp_path):
    conn = _connection(tmp_path)

    updated = set_discovery_source_enabled(conn, "airswift-search", False)
    assert updated["enabled"] is False
    ids = list_enabled_discovery_source_ids(conn)
    assert "airswift-search" not in ids
    assert set(ids) == {"freehire-search", "linkedin-search", "energy-jobline-search"}

    updated = set_discovery_source_enabled(conn, "airswift-search", True)
    assert updated["enabled"] is True
    ids = list_enabled_discovery_source_ids(conn)
    assert "airswift-search" in ids


def test_set_discovery_source_enabled_updates_updated_at(tmp_path):
    conn = _connection(tmp_path)
    before = {row["source_id"]: row["updated_at"] for row in list_discovery_source_settings(conn)}
    set_discovery_source_enabled(conn, "freehire-search", False)
    after = {row["source_id"]: row["updated_at"] for row in list_discovery_source_settings(conn)}
    assert after["freehire-search"] != before["freehire-search"]


def test_set_discovery_source_enabled_unknown_source_raises_key_error(tmp_path):
    # A row is never created by this function -- an unregistered source_id
    # (no code-level adapter, or simply a typo) must fail loudly rather than
    # silently insert a new, potentially-inert row.
    conn = _connection(tmp_path)
    with pytest.raises(KeyError):
        set_discovery_source_enabled(conn, "rigzone-search", True)
    ids = {row["source_id"] for row in list_discovery_source_settings(conn)}
    assert "rigzone-search" not in ids
