"""Bundle 7 database adapter (spec H2, H3, §10.1, §10.7).

One sqlite3-shaped connection API over two dialects:

* **sqlite** returns a real ``sqlite3.Connection`` subclass; behaviour is the
  pre-Bundle-7 behaviour, plus busy-error mapping and writer-lock statistics.
* **postgres** wraps psycopg 3 and reproduces Python sqlite3's transaction
  model: SELECTs outside a transaction autocommit, the first INSERT/UPDATE/
  DELETE opens a transaction implicitly, ``BEGIN``/``COMMIT``/``ROLLBACK``
  statements are honoured, and a failed statement inside a transaction does not
  abort the transaction (each DML statement runs under a savepoint).

``BEGIN IMMEDIATE`` on PostgreSQL takes the *transitional* global writer lock
(spec §10.7): a session-level advisory lock acquired **before** the
REPEATABLE READ transaction starts, so the transaction's snapshot always
includes the previous writer's commit. Acquisition is bounded by
``lock_timeout``; waits, holds and timeouts are recorded per call site.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)

# Error classes are the sqlite3 classes themselves, so every existing
# ``except sqlite3.IntegrityError`` keeps working on both dialects.
IntegrityError = sqlite3.IntegrityError
OperationalError = sqlite3.OperationalError
Row = sqlite3.Row  # annotation alias; PostgreSQL rows are PgRow (same access API)

WRITER_LOCK_KEY = 0x4A53_0001
DEFAULT_WRITER_LOCK_TIMEOUT_MS = 10_000
SLOW_WAIT_SECONDS = 1.0
_DML = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})
_ALLOWED_PG_PRAGMAS = frozenset({"foreign_keys", "journal_mode", "busy_timeout"})
# Statements that run under a statement savepoint inside a transaction, so a
# failure leaves the transaction usable (SQLite's statement-level rollback).
_STATEMENT_SAVEPOINT_KEYWORDS = _DML | {"CREATE", "ALTER", "DROP", "WITH"}


class DatabaseBusy(sqlite3.OperationalError):
    """The database could not serve this transaction now; retry later.

    reason is one of writer_lock_timeout, lock_timeout, serialization,
    deadlock, sqlite_locked.
    """

    def __init__(self, reason: str, site: str, message: str = "") -> None:
        super().__init__(message or f"database busy: {reason} at {site}")
        self.reason = reason
        self.site = site


class Connection(Protocol):
    dialect: str
    in_transaction: bool

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any: ...
    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> Any: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


# ---- writer-lock statistics (exported by /metrics, spec §10.7) ------------

class _LockStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sites: dict[str, dict[str, float]] = {}
        self._timeout_times: dict[str, list[float]] = {}  # epoch seconds, for the admin dashboard's 24 h view

    def _site(self, site: str) -> dict[str, float]:
        return self._sites.setdefault(site, {
            "acquisitions": 0, "timeouts": 0, "wait_seconds_total": 0.0, "wait_seconds_max": 0.0,
            "hold_seconds_total": 0.0, "hold_seconds_max": 0.0,
        })

    def acquired(self, site: str, wait: float) -> None:
        with self._lock:
            s = self._site(site)
            s["acquisitions"] += 1
            s["wait_seconds_total"] += wait
            s["wait_seconds_max"] = max(s["wait_seconds_max"], wait)

    def released(self, site: str, hold: float) -> None:
        with self._lock:
            s = self._site(site)
            s["hold_seconds_total"] += hold
            s["hold_seconds_max"] = max(s["hold_seconds_max"], hold)

    def timed_out(self, site: str) -> None:
        import time
        with self._lock:
            self._site(site)["timeouts"] += 1
            times = self._timeout_times.setdefault(site, [])
            times.append(time.time())
            del times[:-1000]  # bounded

    def timeouts_since(self, cutoff: float) -> dict[str, int]:
        with self._lock:
            return {site: sum(1 for t in times if t >= cutoff) for site, times in self._timeout_times.items()}

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {site: dict(values) for site, values in self._sites.items()}

    def reset(self) -> None:
        with self._lock:
            self._sites.clear()
            self._timeout_times.clear()


LOCK_STATS = _LockStats()


def _call_site() -> str:
    frame = sys._getframe(1)
    while frame is not None and frame.f_globals.get("__name__") == __name__:
        frame = frame.f_back
    if frame is None:
        return "unknown:unknown"
    return f"{frame.f_globals.get('__name__', '?')}:{frame.f_code.co_name}"


def _normalized_statement(sql: str) -> str:
    return " ".join(sql.split()).rstrip(";").upper()


def _first_keyword(sql: str) -> str:
    match = re.match(r"\s*([A-Za-z]+)", sql)
    return match.group(1).upper() if match else ""


# ---- SQLite ---------------------------------------------------------------

class SqliteConnection(sqlite3.Connection):
    dialect = "sqlite"
    _hold: tuple[str, float] | None = None

    def execute(self, sql: str, params: Sequence[Any] = ()):  # type: ignore[override]
        immediate = _first_keyword(sql) == "BEGIN" and _normalized_statement(sql).startswith("BEGIN IMMEDIATE")
        site = _call_site() if immediate else ""
        started = time.monotonic()
        try:
            cursor = super().execute(sql, params)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc):
                if immediate:
                    LOCK_STATS.timed_out(site)
                raise DatabaseBusy("sqlite_locked", site or _call_site(), str(exc)) from exc
            raise
        if immediate:
            LOCK_STATS.acquired(site, time.monotonic() - started)
            self._hold = (site, time.monotonic())
        return cursor

    def begin_account(self, account_id: str) -> None:
        """SQLite serializes writers already: an immediate transaction is the
        account lock. Not a writer-lock site (spec §10.7 counts literals only)."""
        try:
            sqlite3.Connection.execute(self, "BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc):
                raise DatabaseBusy("sqlite_locked", _call_site(), str(exc)) from exc
            raise

    def _end_hold(self) -> None:
        if self._hold is not None:
            LOCK_STATS.released(self._hold[0], time.monotonic() - self._hold[1])
            self._hold = None

    def commit(self) -> None:  # type: ignore[override]
        try:
            super().commit()
        finally:
            self._end_hold()

    def rollback(self) -> None:  # type: ignore[override]
        try:
            super().rollback()
        finally:
            self._end_hold()


def _connect_sqlite(db_path: Path) -> SqliteConnection:
    # check_same_thread=False: FastAPI/Starlette resolve a sync generator
    # dependency (webapp/api/dependencies.py::get_conn) via
    # anyio.to_thread.run_sync twice per request -- once to advance to the
    # yield, once more to run the finally block -- and anyio's threadpool
    # may service those two calls on different worker threads. Each
    # connection here is still used by exactly one request at a time
    # (never shared across requests), so disabling sqlite3's same-thread
    # check is safe: it only relaxes *which* OS thread may touch a given
    # connection, not how many callers may touch it concurrently.
    conn = sqlite3.connect(str(db_path), check_same_thread=False, factory=SqliteConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Bundle 6C: the UI, the in-app scheduler thread and a CLI worker may
    # write concurrently. WAL lets readers proceed while a writer is open;
    # the busy timeout makes contending writers wait instead of failing with
    # "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# ---- PostgreSQL -----------------------------------------------------------

def translate_placeholders(sql: str) -> str:
    """Rewrite '?' to '%s' outside string literals, quoted identifiers and
    comments; escape literal '%' everywhere; turn ``IS ?`` / ``IS NOT ?`` into
    the null-safe ``IS [NOT] DISTINCT FROM`` that SQLite's ``IS`` means."""
    out: list[str] = []
    i, n = 0, len(sql)
    state = "code"
    while i < n:
        ch = sql[i]
        if ch == "%":
            out.append("%%")
            i += 1
            continue
        if state == "code":
            if ch == "'":
                state = "single"
            elif ch == '"':
                state = "double"
            elif sql.startswith("--", i):
                state = "line"
            elif sql.startswith("/*", i):
                state = "block"
            elif ch == "?":
                text = "".join(out)
                is_not = re.search(r"\bIS\s+NOT\s+$", text, re.IGNORECASE)
                is_ = None if is_not else re.search(r"\bIS\s+$", text, re.IGNORECASE)
                if is_not:
                    out = [text[: is_not.start()], "IS DISTINCT FROM "]
                elif is_:
                    out = [text[: is_.start()], "IS NOT DISTINCT FROM "]
                out.append("%s")
                i += 1
                continue
        elif state == "single" and ch == "'":
            state = "code"
        elif state == "double" and ch == '"':
            state = "code"
        elif state == "line" and ch == "\n":
            state = "code"
        elif state == "block" and sql.startswith("*/", i):
            out.append("*/")
            i += 2
            state = "code"
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class PgRow(tuple):
    """A row supporting row["col"] (case-insensitive fallback), row[0],
    dict(row) and row.keys(), like sqlite3.Row."""

    def __new__(cls, values: Sequence[Any], names: list[str], index: dict[str, int], lower: dict[str, int]):
        row = super().__new__(cls, values)
        row._names = names
        row._index = index
        row._lower = lower
        return row

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, str):
            position = self._index.get(key)
            if position is None:
                position = self._lower.get(key.lower())
            if position is None:
                raise IndexError("No item with that key")
            return tuple.__getitem__(self, position)
        return tuple.__getitem__(self, key)

    def keys(self) -> list[str]:
        return list(self._names)


SYNTHETIC_ROWID = "rowid"


def _pg_row_factory(cursor):
    """Rows as PgRow. The synthetic ``rowid`` identity column (the PostgreSQL
    stand-in for SQLite's implicit rowid, used only in ORDER BY) is never
    returned, so ``SELECT *`` yields the same keys on both dialects."""
    description = cursor.description
    if description is None:
        return tuple
    all_names = [column.name for column in description]
    keep = [i for i, name in enumerate(all_names) if name != SYNTHETIC_ROWID]
    names = [all_names[i] for i in keep]
    index = {name: i for i, name in enumerate(names)}
    lower = {name.lower(): i for i, name in enumerate(names)}
    if len(keep) == len(all_names):
        return lambda values: PgRow(values, names, index, lower)
    return lambda values: PgRow([values[i] for i in keep], names, index, lower)


class _Cursor:
    def __init__(self, cursor=None) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return -1 if self._cursor is None else self._cursor.rowcount

    @property
    def description(self):
        return None if self._cursor is None else self._cursor.description

    def fetchone(self):
        if self._cursor is None or self._cursor.description is None:
            return None
        return self._cursor.fetchone()

    def fetchall(self) -> list:
        if self._cursor is None or self._cursor.description is None:
            return []
        return self._cursor.fetchall()

    def fetchmany(self, size: int = 1) -> list:
        if self._cursor is None or self._cursor.description is None:
            return []
        return self._cursor.fetchmany(size)

    def __iter__(self) -> Iterator:
        return iter(self.fetchall())


def _map_pg_error(exc: Exception, site: str) -> Exception:
    import psycopg
    from psycopg import errors

    if isinstance(exc, errors.SerializationFailure):
        return DatabaseBusy("serialization", site, str(exc))
    if isinstance(exc, errors.DeadlockDetected):
        return DatabaseBusy("deadlock", site, str(exc))
    if isinstance(exc, errors.LockNotAvailable):
        return DatabaseBusy("lock_timeout", site, str(exc))
    if isinstance(exc, psycopg.IntegrityError):
        return IntegrityError(str(exc))
    return OperationalError(str(exc))


class PgConnection:
    dialect = "postgres"

    def __init__(self, dsn: str, *, writer_lock_timeout_ms: int) -> None:
        import psycopg
        from psycopg.adapt import Loader

        class _NumericLoader(Loader):
            # SQLite returns int for integral aggregates and float for REAL.
            def load(self, data):
                text = bytes(data).decode()
                return float(text) if any(c in text for c in ".eEN") else int(text)

        self.dsn = dsn  # another connection to the same database (6C fenced steps)
        self._conn = psycopg.connect(dsn, autocommit=True, row_factory=_pg_row_factory)
        self._conn.adapters.register_loader("numeric", _NumericLoader)
        self._conn.execute(f"SET lock_timeout = '{int(writer_lock_timeout_ms)}ms'")
        self._in_tx = False
        self._outer_savepoint: str | None = None
        self._writer_hold: tuple[str, float] | None = None
        self._account_key: int | None = None
        self.row_factory = None  # accepted for sqlite3 API compatibility; rows are always PgRow

    @property
    def in_transaction(self) -> bool:
        return self._in_tx

    # -- transactions --
    def _begin(self, *, writer: bool) -> None:
        if self._in_tx:
            raise OperationalError("cannot start a transaction within a transaction")
        import psycopg
        from psycopg import errors

        if writer:
            site = _call_site()
            started = time.monotonic()
            try:
                self._conn.execute("SELECT pg_advisory_lock(%s)", (WRITER_LOCK_KEY,))
            except errors.LockNotAvailable as exc:
                LOCK_STATS.timed_out(site)
                logger.warning("writer_lock_timeout site=%s", site)
                raise DatabaseBusy("writer_lock_timeout", site, str(exc)) from exc
            except psycopg.Error as exc:
                raise _map_pg_error(exc, site) from exc
            wait = time.monotonic() - started
            LOCK_STATS.acquired(site, wait)
            if wait > SLOW_WAIT_SECONDS:
                logger.warning("writer_lock_slow site=%s wait_ms=%d", site, int(wait * 1000))
            self._writer_hold = (site, time.monotonic())
        try:
            self._conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        except Exception:
            self._release_writer()
            raise
        self._in_tx = True

    def begin_account(self, account_id: str) -> None:
        """Take the account's session-level advisory lock, then BEGIN: the
        REPEATABLE READ snapshot is taken after the lock, so it includes the
        previous holder's commit. Bounded by lock_timeout."""
        if self._in_tx:
            raise OperationalError("cannot start a transaction within a transaction")
        import psycopg
        from psycopg import errors

        key = account_lock_key(account_id)
        site = _call_site()
        try:
            self._conn.execute("SELECT pg_advisory_lock(%s, %s)", (ACCOUNT_LOCK_NAMESPACE, key))
        except errors.LockNotAvailable as exc:
            logger.warning("account_lock_timeout site=%s", site)
            raise DatabaseBusy("account_lock_timeout", site, str(exc)) from exc
        except psycopg.Error as exc:
            raise _map_pg_error(exc, site) from exc
        self._account_key = key
        try:
            self._conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        except Exception:
            self._release_account()
            raise
        self._in_tx = True

    def _release_account(self) -> None:
        if self._account_key is None:
            return
        key, self._account_key = self._account_key, None
        try:
            self._conn.execute("SELECT pg_advisory_unlock(%s, %s)", (ACCOUNT_LOCK_NAMESPACE, key))
        except Exception:  # the session ends on close, which releases it anyway
            logger.exception("account lock release failed")

    def _release_writer(self) -> None:
        if self._writer_hold is None:
            return
        site, since = self._writer_hold
        self._writer_hold = None
        try:
            self._conn.execute("SELECT pg_advisory_unlock(%s)", (WRITER_LOCK_KEY,))
        except Exception:  # the session ends on close, which releases it anyway
            logger.exception("writer lock release failed site=%s", site)
        LOCK_STATS.released(site, time.monotonic() - since)

    def _end(self, statement: str) -> None:
        if not self._in_tx:
            return
        import psycopg

        try:
            self._conn.execute(statement)
        except psycopg.Error as exc:
            raise _map_pg_error(exc, _call_site()) from exc
        finally:
            self._in_tx = False
            self._outer_savepoint = None
            self._release_writer()
            self._release_account()

    def commit(self) -> None:
        self._end("COMMIT")

    def rollback(self) -> None:
        self._end("ROLLBACK")

    def close(self) -> None:
        try:
            if self._in_tx:
                self.rollback()
        finally:
            self._release_writer()
            self._release_account()
            self._conn.close()

    # -- statements --
    def _control(self, sql: str) -> _Cursor | None:
        keyword = _first_keyword(sql)
        if keyword == "BEGIN":
            statement = _normalized_statement(sql)
            self._begin(writer=statement.startswith(("BEGIN IMMEDIATE", "BEGIN EXCLUSIVE")))
            return _Cursor()
        if keyword in ("COMMIT", "END"):
            self.commit()
            return _Cursor()
        if keyword == "ROLLBACK" and _normalized_statement(sql) in ("ROLLBACK", "ROLLBACK TRANSACTION"):
            self.rollback()
            return _Cursor()
        if keyword in ("SAVEPOINT", "RELEASE") or (keyword == "ROLLBACK" and " TO " in _normalized_statement(sql)):
            return self._savepoint(sql, keyword)
        if keyword == "PRAGMA":
            name = re.match(r"\s*PRAGMA\s+([A-Za-z_]+)", sql, re.IGNORECASE)
            if name and name.group(1).lower() in _ALLOWED_PG_PRAGMAS:
                return _Cursor()
            raise OperationalError("pragma not supported on postgres")
        return None

    def _savepoint(self, sql: str, keyword: str) -> _Cursor:
        """SQLite semantics: a SAVEPOINT outside a transaction opens one, and
        RELEASE of that outermost savepoint commits it."""
        import psycopg

        name = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql)[-1].lower()
        if keyword == "SAVEPOINT" and not self._in_tx:
            self._begin(writer=False)
            self._outer_savepoint = name
        try:
            self._conn.execute(sql)
        except psycopg.Error as exc:
            raise _map_pg_error(exc, _call_site()) from exc
        if keyword == "RELEASE" and name == self._outer_savepoint:
            self.commit()
        return _Cursor()

    def _run(self, runner, sql: str, params, *, savepoint: bool):
        import psycopg

        if savepoint:
            self._conn.execute("SAVEPOINT dbapi_statement")
        try:
            result = runner(sql, params)
        except psycopg.Error as exc:
            site = _call_site()
            if savepoint:
                try:
                    self._conn.execute("ROLLBACK TO SAVEPOINT dbapi_statement")
                    self._conn.execute("RELEASE SAVEPOINT dbapi_statement")
                except psycopg.Error:
                    pass
            raise _map_pg_error(exc, site) from exc
        if savepoint:
            self._conn.execute("RELEASE SAVEPOINT dbapi_statement")
        return result

    @staticmethod
    def _prepare(sql: str, params) -> tuple[str, Any]:
        if isinstance(params, Mapping):
            raise OperationalError("named parameters are not supported; use '?' placeholders")
        params = list(params) if params is not None else []
        if not params:
            return sql, None
        return translate_placeholders(sql), params

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Cursor:
        control = self._control(sql)
        if control is not None:
            return control
        keyword = _first_keyword(sql)
        if keyword in _DML and not self._in_tx:
            self._begin(writer=False)
        text, values = self._prepare(sql, params)
        cursor = self._run(lambda s, p: self._conn.execute(s, p), text, values,
                           savepoint=self._in_tx and keyword in _STATEMENT_SAVEPOINT_KEYWORDS)
        return _Cursor(cursor)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> _Cursor:
        rows = [list(r) for r in seq]
        if not rows:
            return _Cursor()
        if _first_keyword(sql) in _DML and not self._in_tx:
            self._begin(writer=False)
        text = translate_placeholders(sql)

        def runner(s, p):
            cursor = self._conn.cursor()
            cursor.executemany(s, p)
            return cursor

        return _Cursor(self._run(runner, text, rows, savepoint=self._in_tx))

    def executescript(self, script: str) -> None:
        raise OperationalError("executescript is not supported on postgres")


# ---- entry point ----------------------------------------------------------

def _writer_lock_timeout(settings: Any, override: int | None) -> int:
    value = override if override is not None else getattr(settings, "writer_lock_timeout_ms", None)
    return DEFAULT_WRITER_LOCK_TIMEOUT_MS if value is None else int(value)


# The dual-dialect test harness (tests/conftest.py, ``--db postgres``) maps
# SQLite file paths used by existing tests to per-path PostgreSQL databases.
# Production never sets it.
_sqlite_redirect: Callable[[Path], str | None] | None = None


ACCOUNT_LOCK_NAMESPACE = 0x4A53  # the two-key advisory lock space for per-account locks


def account_lock_key(account_id: str) -> int:
    import hashlib

    return int.from_bytes(hashlib.sha256(account_id.encode()).digest()[:4], "big", signed=True)


@contextmanager
def account_transaction(conn: Any, account_id: str) -> Iterator[Any]:
    """Start a transaction holding ``account_id``'s lock; commit on success,
    roll back on error. The per-account lock of spec §10.7 for new invariants
    (usage reservations, ...).

    Invariant: this primitive owns the transaction boundary and takes the
    account lock before the first protected read. On PostgreSQL the lock is
    acquired, then ``BEGIN ISOLATION LEVEL REPEATABLE READ`` takes the
    snapshot, so every holder sees the previous holder's commit. A lock taken
    inside a transaction that had already read would keep a stale snapshot and
    lose updates, so a caller already inside a transaction is refused
    (``OperationalError``) rather than silently joined. Acquisition is bounded
    by ``lock_timeout`` → ``DatabaseBusy("account_lock_timeout")``. Different
    accounts take different locks on PostgreSQL; SQLite serializes all writers."""
    if conn.in_transaction:
        raise OperationalError("account_transaction must start its own transaction")
    conn.begin_account(account_id)
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def set_sqlite_redirect(redirect: Callable[[Path], str | None] | None) -> None:
    global _sqlite_redirect
    _sqlite_redirect = redirect


def resolve(target: Any) -> tuple[str, Any, Any]:
    """(dialect, location, settings) for a Settings, postgresql:// URL or SQLite path."""
    settings = None
    if hasattr(target, "db_path"):
        settings = target
        target = settings.database_url or settings.db_path
    if isinstance(target, str) and target.startswith("postgresql://"):
        return "postgres", target, settings
    path = Path(target)
    if _sqlite_redirect is not None:
        url = _sqlite_redirect(path)
        if url:
            return "postgres", url, settings
    return "sqlite", path, settings


def connect(target: Any, *, writer_lock_timeout_ms: int | None = None) -> Connection:
    """target: a Settings object, a postgresql:// URL, or a SQLite file path."""
    dialect, location, settings = resolve(target)
    if dialect == "postgres":
        return PgConnection(location, writer_lock_timeout_ms=_writer_lock_timeout(settings, writer_lock_timeout_ms))
    return _connect_sqlite(location)
