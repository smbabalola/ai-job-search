"""Local -> hosted account import (Bundle 7 spec §23.3).

The source is a real local SQLite database (account_local driven through the
6D-A/6D-B/6E-A services to a confirmed submission, plus the isolation-harness
object graph), local document blobs and a filesystem profile root. The target
is a fresh PostgreSQL database migrated as a hosted deployment would be."""
from __future__ import annotations

import pytest

from tests.pg_support import PG_SKIP, fresh_pg_database
from tests.webapp.factories import build_account_graph
from tests.webapp.services.submit_fixtures import (  # noqa: F401
    NOW, SEC, V2_ACCOUNT, filled_world, grant_world, v2_chain,
)
from tests.webapp.services.test_human_submit_results import dispatched, report

pytestmark = [
    pytest.mark.sqlite_only,  # the source must stay a SQLite file (the --db postgres redirect would move it)
    pytest.mark.skipif(bool(PG_SKIP), reason=f"needs a PostgreSQL target: {PG_SKIP}"),
]

EMAIL = "Founder@Example.com"
HASH_COLUMNS = ("review_hash", "binding_hash", "result_hash")


@pytest.fixture
def source(filled_world, tmp_path):
    from webapp.persistence.profile_sources import CANDIDATE_SOURCE

    w = filled_world
    auth, attempt = dispatched(w)
    assert report(w, attempt, success_observed=True)["state"] == "CONFIRMED_SUCCESS"
    build_account_graph(w.conn, account_id=V2_ACCOUNT, documents_root=w.settings.documents_root)
    w.conn.commit()
    from tests.test_profile_snapshot import CANDIDATE_TEMPLATE, CLAUDE_TEMPLATE, CV_TEMPLATE, DEFAULTS
    profile_root = tmp_path / "profile"
    for path, template in (("CLAUDE.md", CLAUDE_TEMPLATE), (CANDIDATE_SOURCE, CANDIDATE_TEMPLATE),
                           ("cv/main_example.tex", CV_TEMPLATE)):
        file = profile_root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        # CRLF on disk: the snapshot reads universal newlines, so must the import
        file.write_bytes(template.format(**DEFAULTS).replace("\n", "\r\n").encode())
    return {"sqlite": w.settings.db_path, "documents": w.settings.documents_root, "profile": profile_root,
            "conn": w.conn}


@pytest.fixture
def target():
    from webapp.persistence.db import _init_postgres

    with fresh_pg_database("jobsearch_import") as url:
        _init_postgres(url, seed_local=False)  # a hosted database never contains account_local (H9)
        yield url


def _run(source, target, store, **kw):
    from webapp.tools.import_local_account import import_local_account

    return import_local_account(sqlite_path=source["sqlite"], profile_root=source["profile"],
                                documents_root=source["documents"], target_dsn=target, object_store=store,
                                email=EMAIL, display_name="Founder Person", assume_verified=True, **kw)


def _order_column(conn, table):
    """seq, else the rowid (SQLite always; PostgreSQL emulates it on the
    baseline tables only), else None: such a table keeps no insertion order."""
    from webapp.persistence.schema_catalog import schema_catalog

    names = {c[0] for c in schema_catalog(conn)[table]["columns"]}
    if conn.dialect != "sqlite":  # the catalog hides the emulated rowid column
        names |= {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE "
                                             "table_name = ? AND column_name = 'rowid'", (table,)).fetchall()}
    return "seq" if "seq" in names else "rowid" if conn.dialect == "sqlite" or "rowid" in names else None


def _ordered_rows(conn, table, account_id, user_ids, *, keep_order=True):
    import json

    from webapp.services.purge import account_filter

    order = _order_column(conn, table)
    where, params = account_filter(table, account_id, user_ids)
    sql = f'SELECT * FROM "{table}" WHERE {where}' + (f" ORDER BY {order}" if order else "")
    rows = [{k: r[k] for k in r.keys() if k not in ("seq", "rowid")}
            for r in conn.execute(sql, tuple(params)).fetchall()]
    if keep_order and order:
        return rows
    return sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str))


def _counts(conn):
    from webapp.persistence.schema_catalog import schema_catalog

    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in sorted(schema_catalog(conn))}


def test_import_is_lossless_and_rekeyed(source, target, tmp_path):
    from product.profile_snapshot import build_snapshot
    from product.profile_snapshot import FilesystemSourceReader
    from webapp.persistence import dbapi
    from webapp.persistence.profile_sources import included_profile_sources
    from webapp.services.document_blob_store import DocumentBlobStore
    from webapp.storage.object_store import LocalFsObjectStore
    from webapp.storage.profile_sources import DatabaseProfileSources
    from webapp.tools.import_local_account import NOT_COPIED

    store = LocalFsObjectStore(tmp_path / "hosted-objects")
    result = _run(source, target, store)
    new_id = result.account_id
    assert new_id != V2_ACCOUNT and result.dry_run is False

    src = source["conn"]
    dst = dbapi.connect(target)
    try:
        user = dst.execute("SELECT * FROM users WHERE email_normalized = ?", ("founder@example.com",)).fetchone()
        assert user["status"] == "ACTIVE" and user["email_verified_at"]
        assert dst.execute("SELECT role FROM account_memberships WHERE account_id = ? AND user_id = ?",
                           (new_id, user["id"])).fetchone()[0] == "OWNER"
        account = dst.execute("SELECT kind, status, display_name FROM accounts WHERE id = ?", (new_id,)).fetchone()
        assert tuple(account) == ("candidate", "ACTIVE", "Founder Person")
        assert dst.execute("SELECT COUNT(*) FROM accounts WHERE id = ?", (V2_ACCOUNT,)).fetchone()[0] == 0

        # every copied registry table: same rows, same order, account re-keyed, document keys rewritten
        keys = {}
        for table in result.copied:
            keep = _order_column(dst, table) is not None  # the order is kept wherever the target can hold one
            expected = _ordered_rows(src, table, V2_ACCOUNT, [], keep_order=keep)
            actual = _ordered_rows(dst, table, new_id, [user["id"]], keep_order=keep)
            if table == "profile_source_revisions":
                continue  # the tool appends the filesystem sources (checked below)
            if table == "audit_log":  # plus the import's own record
                assert [r["action"] for r in actual].count("ACCOUNT_IMPORTED") == 1
                actual = [r for r in actual if r["action"] != "ACCOUNT_IMPORTED"]
            assert len(actual) == len(expected) == result.copied[table], table
            for before, after in zip(expected, actual):
                rekeyed = {k: (new_id if v == V2_ACCOUNT else v) for k, v in before.items()}
                if table == "application_document_versions":
                    assert after["storage_key"].startswith(f"accounts/{new_id}/documents/sha256/")
                    keys[before["storage_key"]] = after["storage_key"]
                    rekeyed["storage_key"] = after["storage_key"]
                assert after == rekeyed, table
        # nothing the account owns was silently left behind
        from webapp.services.purge import purge_sequence
        for table in purge_sequence(src):
            if table in NOT_COPIED or table in ("accounts",):
                continue
            if _ordered_rows(src, table, V2_ACCOUNT, []):
                assert table in result.copied, table
        assert {"application_approvals", "fill_runs", "submission_results", "cv_library_versions"} <= {
            t for t, n in result.copied.items() if n}
        # the bound hashes are byte-identical
        for table in result.copied:
            columns = [c for c in HASH_COLUMNS if c in (_ordered_rows(src, table, V2_ACCOUNT, []) or [{}])[0]]
            for column in columns:
                before = [r[column] for r in _ordered_rows(src, table, V2_ACCOUNT, [])]
                after = [r[column] for r in _ordered_rows(dst, table, new_id, [user["id"]])]
                assert before == after and any(before), (table, column)

        # blobs: readable through the new keys, sha256-verified
        blobs = DocumentBlobStore(store)
        for row in dst.execute("SELECT * FROM application_document_versions WHERE account_id = ?", (new_id,)):
            assert blobs.read(dict(row))
        assert keys and result.blobs == len(keys)

        # the profile snapshot equals the source snapshot
        before = build_snapshot(FilesystemSourceReader(source["profile"]),
                                included_sources=included_profile_sources(src, account_id=V2_ACCOUNT))
        after = build_snapshot(DatabaseProfileSources(dst, new_id),
                               included_sources=included_profile_sources(dst, account_id=new_id))
        assert after == before
    finally:
        dst.close()


def test_a_second_import_refuses_because_the_email_exists(source, target, tmp_path):
    from webapp.storage.object_store import LocalFsObjectStore
    from webapp.tools.import_local_account import ImportRefused

    store = LocalFsObjectStore(tmp_path / "hosted-objects")
    _run(source, target, store)
    with pytest.raises(ImportRefused) as refused:
        _run(source, target, store)
    assert refused.value.code == "EMAIL_EXISTS"


def test_dry_run_writes_nothing(source, target, tmp_path):
    import json

    from webapp.persistence import dbapi
    from webapp.tools.import_local_account import main

    objects = tmp_path / "hosted-objects"
    conn = dbapi.connect(target)
    before = _counts(conn)
    conn.close()
    report_path = tmp_path / "import-report.json"
    code = main(["--sqlite", str(source["sqlite"]), "--profile-root", str(source["profile"]),
                 "--documents-root", str(source["documents"]), "--target-dsn", target,
                 "--object-store", json.dumps({"kind": "local", "root": str(objects)}), "--email", EMAIL,
                 "--display-name", "Founder Person", "--assume-verified", "--dry-run", "--report", str(report_path)])
    assert code == 0
    conn = dbapi.connect(target)
    assert _counts(conn) == before
    conn.close()
    assert not objects.exists() or not any(p.is_file() for p in objects.rglob("*"))
    written = json.loads(report_path.read_text())
    assert written["dry_run"] is True and written["copied"]["fill_runs"] >= 1 and written["blobs"] >= 1


def test_refuses_without_the_operator_verification_assertion(source, target, tmp_path):
    from webapp.storage.object_store import LocalFsObjectStore
    from webapp.tools.import_local_account import ImportRefused, import_local_account

    with pytest.raises(ImportRefused) as refused:
        import_local_account(sqlite_path=source["sqlite"], profile_root=source["profile"],
                             documents_root=source["documents"], target_dsn=target,
                             object_store=LocalFsObjectStore(tmp_path / "o"), email=EMAIL,
                             display_name="Founder Person", assume_verified=False)
    assert refused.value.code == "VERIFICATION_NOT_ASSERTED"


def test_the_local_only_profile_pointer_is_not_carried(source, target, tmp_path):
    """current_user_profile is the legacy local singleton (RETAIN LOCAL_ONLY): a hosted
    database must not get it (a retained pointer would block the account's purge)."""
    from webapp.persistence import dbapi
    from webapp.storage.object_store import LocalFsObjectStore
    src = source["conn"]
    version = src.execute("SELECT id FROM user_profile_versions WHERE account_id = ? ORDER BY rowid LIMIT 1",
                          (V2_ACCOUNT,)).fetchone()
    assert version is not None
    src.execute("INSERT INTO current_user_profile (id, version_id, updated_at) VALUES ('current', ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET version_id = excluded.version_id", (version[0], NOW.isoformat()))
    src.commit()
    result = _run(source, target, LocalFsObjectStore(tmp_path / "hosted-objects"))
    assert result.not_copied.get("current_user_profile") == 1 and "current_user_profile" not in result.copied
    dst = dbapi.connect(target)
    try:
        assert dst.execute("SELECT COUNT(*) FROM current_user_profile").fetchone()[0] == 0
    finally:
        dst.close()
