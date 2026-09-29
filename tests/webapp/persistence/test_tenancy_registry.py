"""Bundle 7 spec H8, §10.5: every table is classified, owner paths resolve,
and purge order is a valid children-first foreign-key order."""
from __future__ import annotations

import pytest

from webapp.persistence.accounts import create_account
from webapp.persistence.db import connect, init_db
from webapp.persistence.schema_catalog import schema_catalog
from webapp.persistence.tenancy import (
    MAX_OWNER_HOPS,
    TENANT_TABLES,
    account_rows_sql,
    owner_chain,
    purge_order,
)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    connection = connect(path)
    yield connection
    connection.close()


def test_every_table_is_classified_and_every_entry_exists(conn):
    tables = set(schema_catalog(conn))
    assert sorted(tables - set(TENANT_TABLES)) == [], "unclassified tables"
    assert sorted(set(TENANT_TABLES) - tables) == [], "registry entries without a table"


def test_owner_paths_reach_an_account_column_within_the_hop_limit(conn):
    catalog = schema_catalog(conn)
    for table, spec in TENANT_TABLES.items():
        if spec.owner == "GLOBAL":
            continue
        chain = owner_chain(table)
        assert len(chain) <= MAX_OWNER_HOPS + 1, table
        for name, column, _ in chain:
            assert column in [c[0] for c in catalog[name]["columns"]], (table, name, column)


def test_account_rows_sql_selects_exactly_one_accounts_rows(conn):
    create_account(conn, display_name="Other", account_id="acct_other")
    for table, spec in TENANT_TABLES.items():
        if spec.owner == "GLOBAL":
            continue
        sql, _ = account_rows_sql(table)
        conn.execute(sql, ("acct_other",)).fetchall()  # every generated query is valid
    rows = conn.execute(account_rows_sql("search_workspaces")[0], ("account_local",)).fetchall()
    assert [r["id"] for r in rows] == ["search_default"]
    assert conn.execute(account_rows_sql("search_workspaces")[0], ("acct_other",)).fetchall() == []


def test_purge_order_puts_children_before_parents(conn):
    order = purge_order(conn)
    position = {t: i for i, t in enumerate(order)}
    catalog = schema_catalog(conn)
    for table, spec in catalog.items():
        for fk in spec["foreign_keys"]:
            parent = fk[1]
            if parent != table and parent in position and table in position:
                assert position[table] < position[parent], f"{table} must be purged before {parent}"


def test_legacy_singleton_profile_is_retained_local_only():
    spec = TENANT_TABLES["current_user_profile"]
    assert (spec.purge, spec.retain_class, spec.export) == ("RETAIN", "LOCAL_ONLY", False)


def test_secret_and_lease_tables_are_never_exported():
    for table in ("extension_credentials", "pairing_secrets", "handoff_session_tokens", "fill_run_leases"):
        assert TENANT_TABLES[table].export is False, table
