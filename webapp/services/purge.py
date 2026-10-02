"""Account purge and retention expiry (Bundle 7 spec §20.4).

``purge_account`` runs in phases, each idempotent:

1. revalidate: the account is DELETION_REQUESTED, its deletion was not
   canceled, and the cooling-off period has passed;
2. one database transaction that first re-checks (1) under the account lock
   (a cancel that committed meanwhile wins): insert the ``purge_in_progress``
   guard (the append-only DELETE triggers of 037 let deletes through only
   while it exists, and it is never visible to another transaction), walk the
   tenant registry children-first: DELETE-class rows are deleted, PSEUDONYMIZE
   columns overwritten, RETAIN rows kept and tagged with their class; remove
   the guard; tombstone the account and its users (``PURGED``, the email
   replaced by ``purged:{user_id}:{sha256}`` so the address can sign up, and
   be purged, again); audit ``ACCOUNT_PURGED``; queue the service email
   ``account.deletion_completed`` to the address captured before the purge;
3. after the commit, delete the account's object-store prefix (safe to
   repeat: an already-purged account finishes it on the next run).

Rows are reached through the registry's owner chain as nested ``IN``
subqueries, so no table needs a single-column key and both dialects run the
same SQL."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from webapp.persistence import dbapi
from webapp.persistence.tenancy import TENANT_TABLES, owner_chain

__all__ = ["PurgeReport", "account_filter", "expire_retained", "purge_account", "purge_sequence"]

TOMBSTONE_ACCOUNT_NAME = "Deleted account"
TOMBSTONE_USER_NAME = "Deleted user"
# The purge's own records stay as the minimal proof that it happened.
_UNTAGGED_RETAINED = frozenset({"purge_retention_tags", "account_deletions"})


@dataclass
class PurgeReport:
    account_id: str
    status: str  # purged | not_requested | not_due | already_purged
    deleted: dict[str, int] = field(default_factory=dict)
    pseudonymized: list[str] = field(default_factory=list)
    retained: dict[str, int] = field(default_factory=dict)
    objects_deleted: int = 0


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def account_filter(table: str, account_id: str, user_ids: list[str]) -> tuple[str, list[Any]]:
    """A WHERE clause (no alias) selecting ``table``'s rows of the account."""
    chain = owner_chain(table)
    *links, last = chain
    last_table, last_column, last_kind = last
    if last_kind == "USER":
        if not user_ids:
            condition, params = "1 = 0", []
        else:
            condition = f'"{last_column}" IN ({", ".join("?" for _ in user_ids)})'
            params = list(user_ids)
    else:
        condition, params = f'"{last_column}" = ?', [account_id]
    # walk back from the account-holding table to ``table``
    for index in range(len(links) - 1, -1, -1):
        current, column, parent_key = links[index]
        parent = chain[index + 1][0]
        condition = f'"{column}" IN (SELECT "{parent_key}" FROM "{parent}" WHERE {condition})'
    return condition, params


def purge_sequence(conn: dbapi.Connection) -> list[str]:
    """Registry tables present in the database, each before every table it
    references (foreign keys) and every table on its owner chain."""
    from webapp.persistence.schema_catalog import schema_catalog
    catalog = schema_catalog(conn)
    tables = [t for t in TENANT_TABLES if t in catalog and TENANT_TABLES[t].owner != "GLOBAL"]
    after: dict[str, set[str]] = {t: set() for t in tables}  # t must come before each of after[t]
    for table in tables:
        for fk in catalog[table]["foreign_keys"]:
            if fk[1] != table and fk[1] in after:
                after[table].add(fk[1])
        for link in owner_chain(table)[1:]:
            if link[0] in after and link[0] != table:
                after[table].add(link[0])
    order: list[str] = []
    remaining = set(tables)
    while remaining:
        ready = sorted(t for t in remaining if not any(t in after[o] for o in remaining if o != t))
        if not ready:
            raise ValueError(f"purge ordering cycle among {sorted(remaining)}")
        order.extend(ready)
        remaining -= set(ready)
    return order


def _owner_users(conn: dbapi.Connection, account_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT u.id, u.email_normalized, u.email_display, u.status FROM account_memberships m "
        "JOIN users u ON u.id = m.user_id WHERE m.account_id = ? ORDER BY u.id", (account_id,)).fetchall()]


def _pseudonymize(conn: dbapi.Connection, table: str, account_id: str, users: list[dict[str, Any]]) -> None:
    if table == "accounts":
        conn.execute("UPDATE accounts SET display_name = ? WHERE id = ?", (TOMBSTONE_ACCOUNT_NAME, account_id))
    elif table == "users":
        for user in users:
            if user["status"] == "PURGED":
                continue
            digest = hashlib.sha256(user["email_normalized"].encode()).hexdigest()
            # the user id keeps the tombstone unique: the same address may sign up and be purged again
            conn.execute("UPDATE users SET email_normalized = ?, email_display = ?, display_name = ? WHERE id = ?",
                         (f"purged:{user['id']}:{digest}", f"deleted-{user['id']}@invalid", TOMBSTONE_USER_NAME,
                          user["id"]))
    else:
        raise ValueError(f"no pseudonymization rule for {table}")


class _Withdrawn(Exception):
    """Raised inside the purge transaction to roll it back: the request changed meanwhile."""


def _due(conn: dbapi.Connection, account_id: str, now: datetime, report: PurgeReport) -> bool:
    """True when the account is due for purge; otherwise False with report.status set."""
    account = conn.execute("SELECT status FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if account is not None and account[0] == "PURGED":
        report.status = "already_purged"
        return False
    deletion = conn.execute("SELECT purge_after, canceled_at FROM account_deletions WHERE account_id = ?",
                            (account_id,)).fetchone()
    if account is None or account[0] != "DELETION_REQUESTED" or deletion is None or deletion[1] is not None:
        report.status = "not_requested"
        return False
    if _iso(now) < deletion[0]:
        report.status = "not_due"
        return False
    return True


def purge_account(conn_factory: Callable[[], dbapi.Connection], *, account_id: str, object_store: Any,
                  now: datetime, settings: Any) -> PurgeReport:
    report = PurgeReport(account_id=account_id, status="purged")
    conn = conn_factory()
    try:
        # 1. revalidate
        if not _due(conn, account_id, now, report):
            if report.status == "already_purged":  # finish an interrupted object phase (idempotent)
                report.objects_deleted = object_store.delete_prefix(f"accounts/{account_id}/")
            return report
        users = _owner_users(conn, account_id)
        user_ids = [u["id"] for u in users]
        mailbox = next((u["email_display"] for u in users if u["status"] != "PURGED"), None)

        # 2. the database, in one transaction under the purge guard. The request is re-checked
        #    under the account lock, so a cancel that committed meanwhile wins and nothing is deleted.
        order = purge_sequence(conn)
        with dbapi.account_transaction(conn, account_id):
            if not _due(conn, account_id, now, report):
                raise _Withdrawn()
            conn.execute("INSERT INTO purge_in_progress (account_id, started_at) VALUES (?, ?)",
                         (account_id, _iso(now)))
            for table in order:
                spec = TENANT_TABLES[table]
                where, params = account_filter(table, account_id, user_ids)
                if spec.purge == "DELETE":
                    count = conn.execute(f'DELETE FROM "{table}" WHERE {where}', tuple(params)).rowcount
                    if count:
                        report.deleted[table] = count
                elif spec.purge == "PSEUDONYMIZE":
                    _pseudonymize(conn, table, account_id, users)
                    report.pseudonymized.append(table)
                elif spec.purge == "RETAIN" and table not in _UNTAGGED_RETAINED:
                    count = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {where}', tuple(params)).fetchone()[0]
                    if count:
                        report.retained[table] = count
                        conn.execute(
                            "INSERT INTO purge_retention_tags (id, account_id, table_name, retain_class, row_count, "
                            "owner_user_ids_json, tagged_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT (account_id, table_name) DO NOTHING",
                            (f"rtag_{uuid.uuid4().hex[:20]}", account_id, table, spec.retain_class, count,
                             json.dumps(user_ids), _iso(now)))
            conn.execute("DELETE FROM purge_in_progress WHERE account_id = ?", (account_id,))
            conn.execute("UPDATE accounts SET status = 'PURGED', updated_at = ? WHERE id = ?", (_iso(now), account_id))
            for user_id in user_ids:
                conn.execute("UPDATE users SET status = 'PURGED', updated_at = ? WHERE id = ?", (_iso(now), user_id))
            conn.execute("UPDATE account_deletions SET completed_at = ? WHERE account_id = ?", (_iso(now), account_id))
            from webapp.persistence.audit import audit
            audit(conn, actor_type="SYSTEM", actor_id="purge", account_id=account_id, action="ACCOUNT_PURGED", now=now,
                  target_type="account", target_id=account_id,
                  detail={"deleted_tables": len(report.deleted), "retained": report.retained},
                  secret=getattr(settings, "secret_key", None))
            # 4. tell the person, at the address captured before the purge
            if mailbox:
                from webapp import comms
                comms.enqueue(conn, category="SERVICE", template_id="account.deletion_completed", to_address=mailbox,
                              payload={}, idempotency_key=f"account.deletion_completed:{account_id}", now=now)
        # 3. the object store (documents, exports), only once the rows are committed; if this fails the
        #    account is already PURGED and the next run (already_purged) finishes it
        report.objects_deleted = object_store.delete_prefix(f"accounts/{account_id}/")
        return report
    except _Withdrawn:
        return report
    finally:
        conn.close()


def expire_retained(conn: dbapi.Connection, *, policy: Any, now: datetime) -> int:
    """Deletes a purged account's retained rows once their class's period has
    passed (a ``null`` period keeps them). Returns the number of tags expired."""
    expired = 0
    tags = conn.execute("SELECT id, account_id, table_name, retain_class, owner_user_ids_json, tagged_at "
                        "FROM purge_retention_tags WHERE expired_at IS NULL ORDER BY seq").fetchall()
    for tag_id, account_id, table, retain_class, owner_user_ids, tagged_at in tags:
        period = policy.retained_periods.get(retain_class)
        if period is None or datetime.fromisoformat(tagged_at) + period > now:
            continue
        where, params = account_filter(table, account_id, json.loads(owner_user_ids))
        with dbapi.account_transaction(conn, account_id):
            conn.execute("INSERT INTO purge_in_progress (account_id, started_at) VALUES (?, ?)",
                         (account_id, _iso(now)))
            conn.execute(f'DELETE FROM "{table}" WHERE {where}', tuple(params))
            conn.execute("DELETE FROM purge_in_progress WHERE account_id = ?", (account_id,))
            conn.execute("UPDATE purge_retention_tags SET expired_at = ? WHERE id = ?", (_iso(now), tag_id))
        expired += 1
    return expired
