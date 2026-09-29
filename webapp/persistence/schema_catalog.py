"""A dialect-neutral description of the schema, for the parity test
(Bundle 7 spec §10.3). Two schemas are equal when every table has the same
columns (name, order, type class, nullability), primary key, unique keys,
indexes, foreign keys, CHECK constraints and append-only triggers.

Predicates and CHECK expressions are compared by *signature* (the identifiers,
string literals and numbers they mention), because PostgreSQL rewrites
``x IN ('a','b')`` as ``x = ANY (ARRAY['a'::text, ...])``.
"""
from __future__ import annotations

import re
from typing import Any

_KEYWORDS = {
    "and", "or", "not", "is", "null", "in", "any", "array", "text", "bigint", "integer", "between", "like",
    "true", "false", "length", "check",
}
_TRIGGER_RE = re.compile(r"CREATE\s+TRIGGER\s+(\w+)\s+BEFORE\s+(INSERT|DELETE|UPDATE)", re.IGNORECASE)


def extract_checks(create_sql: str) -> list[str]:
    """Every CHECK (...) expression in a CREATE TABLE statement, in order."""
    out: list[str] = []
    i, n = 0, len(create_sql)
    in_string = False
    while i < n:
        ch = create_sql[i]
        if ch == "'":
            in_string = not in_string
        elif (not in_string and create_sql[i:i + 5].upper() == "CHECK"
              and (i == 0 or not (create_sql[i - 1].isalnum() or create_sql[i - 1] == "_"))):
            j = create_sql.index("(", i)
            depth, k, quoted = 0, j, False
            while k < n:
                c = create_sql[k]
                if c == "'":
                    quoted = not quoted
                elif not quoted and c == "(":
                    depth += 1
                elif not quoted and c == ")":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            out.append(" ".join(create_sql[j + 1:k].split()))
            i = k
        i += 1
    return out


def signature(expression: str | None) -> Any:
    if expression is None:
        return None
    literals = sorted(re.findall(r"'((?:[^']|'')*)'", expression))
    stripped = re.sub(r"'(?:[^']|'')*'", " ", expression).replace('"', " ")
    stripped = re.sub(r"::\w+", " ", stripped)
    words = sorted({w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", stripped)} - _KEYWORDS)
    numbers = sorted(re.findall(r"(?<![A-Za-z_])\d+(?![A-Za-z_])", stripped))
    return [literals, words, numbers]


def _type_class(declared: str) -> str:
    declared = (declared or "").lower()
    if declared.startswith("int") or declared in ("bigint", "smallint"):
        return "int"
    if declared in ("", "text"):
        return "text"
    if declared in ("real", "double precision"):
        return "real"
    if declared in ("blob", "bytea"):
        return "blob"
    return declared


def _finish(table: dict) -> dict:
    table["unique"] = sorted(table["unique"], key=repr)
    table["indexes"] = sorted(table["indexes"], key=repr)
    table["foreign_keys"] = sorted(table["foreign_keys"], key=repr)
    table["checks"] = sorted(table["checks"], key=repr)
    table["triggers"] = sorted(table["triggers"])
    return table


def _sqlite_catalog(conn) -> dict[str, dict]:
    catalog: dict[str, dict] = {}
    objects = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    index_sql = {o["name"]: o["sql"] for o in objects if o["type"] == "index"}
    for obj in objects:
        if obj["type"] != "table":
            continue
        name = obj["name"]
        columns = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
        pk = [c["name"] for c in sorted(columns, key=lambda c: c["pk"]) if c["pk"]]
        table = {
            "columns": [[c["name"], _type_class(c["type"]), bool(c["notnull"] or c["name"] in pk)] for c in columns],
            "primary_key": pk, "unique": [], "indexes": [], "foreign_keys": [], "triggers": [],
            "checks": [signature(e) for e in extract_checks(obj["sql"])],
        }
        for index in conn.execute(f'PRAGMA index_list("{name}")').fetchall():
            if index["origin"] == "pk":
                continue
            cols = [r["name"] for r in conn.execute(f'PRAGMA index_info("{index["name"]}")')]
            sql = index_sql.get(index["name"])
            where = re.search(r"\bWHERE\b(.*)$", sql, re.IGNORECASE | re.DOTALL) if sql else None
            predicate = signature(where.group(1)) if where else None
            if index["unique"]:
                table["unique"].append([cols, predicate])
            else:
                table["indexes"].append([index["name"], cols, predicate])
        grouped: dict[int, list] = {}
        for fk in conn.execute(f'PRAGMA foreign_key_list("{name}")').fetchall():
            grouped.setdefault(fk["id"], []).append(fk)
        for rows in grouped.values():
            rows.sort(key=lambda r: r["seq"])
            parent = rows[0]["table"]
            if rows[0]["to"] is None:
                parent_cols = [c["name"] for c in sorted(conn.execute(f'PRAGMA table_info("{parent}")').fetchall(),
                                                         key=lambda c: c["pk"]) if c["pk"]]
            else:
                parent_cols = [r["to"] for r in rows]
            on_delete = rows[0]["on_delete"] or "NO ACTION"
            table["foreign_keys"].append([[r["from"] for r in rows], parent, parent_cols, on_delete])
        catalog[name] = table
    for obj in objects:
        if obj["type"] == "trigger":
            match = _TRIGGER_RE.search(obj["sql"])
            catalog[obj["tbl_name"]]["triggers"].append([match.group(1), match.group(2).upper()])
    return {name: _finish(t) for name, t in catalog.items()}


_PG_DELETE_ACTIONS = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}


def _pg_catalog(conn) -> dict[str, dict]:
    catalog: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
    ).fetchall():
        catalog[row["table_name"]] = {"columns": [], "primary_key": [], "unique": [], "indexes": [],
                                      "foreign_keys": [], "checks": [], "triggers": []}
    for row in conn.execute(
        "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = current_schema() ORDER BY table_name, ordinal_position"
    ).fetchall():
        if row["table_name"] in catalog and row["column_name"] != "rowid":  # synthetic SQLite rowid
            catalog[row["table_name"]]["columns"].append(
                [row["column_name"], _type_class(row["data_type"]), row["is_nullable"] == "NO"])
    for row in conn.execute(
        "SELECT t.relname AS table_name, i.relname AS index_name, ix.indisprimary, ix.indisunique, "
        "pg_get_expr(ix.indpred, ix.indrelid) AS predicate, "
        "ARRAY(SELECT a.attname FROM unnest(ix.indkey) WITH ORDINALITY k(attnum, ord) "
        "      JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum ORDER BY k.ord) AS cols "
        "FROM pg_index ix JOIN pg_class t ON t.oid = ix.indrelid JOIN pg_class i ON i.oid = ix.indexrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = current_schema()"
    ).fetchall():
        table = catalog.get(row["table_name"])
        if table is None:
            continue
        cols = list(row["cols"])
        if row["indisprimary"]:
            table["primary_key"] = cols
        elif row["indisunique"]:
            table["unique"].append([cols, signature(row["predicate"])])
        else:
            table["indexes"].append([row["index_name"], cols, signature(row["predicate"])])
    for row in conn.execute(
        "SELECT c.conrelid::regclass::text AS table_name, c.contype, c.confdeltype, "
        "c.confrelid::regclass::text AS parent, pg_get_constraintdef(c.oid) AS definition, "
        "ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ord) "
        "      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum ORDER BY k.ord) AS cols, "
        "ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(attnum, ord) "
        "      JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum ORDER BY k.ord) AS parent_cols "
        "FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname = current_schema() AND c.contype IN ('f', 'c')"
    ).fetchall():
        table = catalog.get(row["table_name"].strip('"'))
        if table is None:
            continue
        if row["contype"] == "f":
            table["foreign_keys"].append([list(row["cols"]), row["parent"].strip('"'), list(row["parent_cols"]),
                                          _PG_DELETE_ACTIONS[row["confdeltype"]]])
        else:
            definition = re.sub(r"^CHECK\s*\((.*)\)$", r"\1", row["definition"], flags=re.DOTALL)
            table["checks"].append(signature(definition))
    for row in conn.execute(
        "SELECT event_object_table AS table_name, trigger_name, event_manipulation FROM information_schema.triggers "
        "WHERE trigger_schema = current_schema()"
    ).fetchall():
        if row["table_name"] in catalog:
            catalog[row["table_name"]]["triggers"].append([row["trigger_name"], row["event_manipulation"]])
    return {name: _finish(t) for name, t in catalog.items()}


def schema_catalog(conn) -> dict[str, dict]:
    catalog = _sqlite_catalog(conn) if conn.dialect == "sqlite" else _pg_catalog(conn)
    catalog.pop("sqlite_sequence", None)
    return catalog
