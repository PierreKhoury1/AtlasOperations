"""Database connection factory: SQLite by default, PostgreSQL when a URL is given.

`Store` was written against the sqlite3 module (``?`` placeholders, ``INSERT OR REPLACE``, ``last_insert_rowid()``,
``lastrowid``, ``executescript``, ``PRAGMA table_info``). `PgConn` speaks that same small dialect on top of psycopg 3
so the store code stays single-sourced. Only the handful of constructs the store actually uses are translated.

Why Postgres at all: Render's filesystem is ephemeral, so an SQLite file there is wiped on every deploy and restart -
accounts, desks and approvals vanished. Point ``DATABASE_URL`` at a Supabase / Render / Neon Postgres and they persist.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from typing import Any, Iterable

# tables whose ``id`` is an auto-increment key (INSERTs into these get ``RETURNING id`` so lastrowid works)
SERIAL_TABLES = {"contacts", "actions", "users", "desks", "connectors", "jobs", "memories", "leads", "vision_events"}
# primary key per table for INSERT OR REPLACE -> ON CONFLICT
PRIMARY_KEYS = {"runs": "id", "design_sessions": "sid"}
PRIMARY_KEYS.update({t: "id" for t in SERIAL_TABLES})


def is_postgres_url(url: str | None) -> bool:
    return bool(url) and url.split(":", 1)[0].lower() in ("postgres", "postgresql")


def translate_ddl(sql: str) -> str:
    """SQLite schema -> PostgreSQL schema (types and auto-increment only)."""
    sql = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", sql)
    sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)
    return sql


_INSERT_OR_REPLACE = re.compile(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)", re.I)
_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)\b", re.I)


def translate_sql(sql: str) -> tuple[str, bool]:
    """SQLite statement -> PostgreSQL statement. Returns (sql, wants_returning_id)."""
    s = sql.strip()
    if s.lower().startswith("select last_insert_rowid()"):
        return "__LAST_ID__", False
    m = _INSERT_OR_REPLACE.match(s)
    if m:
        table, cols = m.group(1), [c.strip() for c in m.group(2).split(",")]
        pk = PRIMARY_KEYS.get(table, "id")
        sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c.strip('"') != pk)
        s = re.sub(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I) + f" ON CONFLICT({pk}) DO UPDATE SET {sets}"
    s = s.replace("?", "%s")
    wants_id = False
    m2 = _INSERT.match(s)
    if m2 and m2.group(1) in SERIAL_TABLES and "RETURNING" not in s.upper() and "ON CONFLICT" not in s.upper():
        s += " RETURNING id"
        wants_id = True
    return s, wants_id


class _Result:
    """The slice of the DB-API cursor surface the store uses, with rows fetched eagerly (thread-safe hand-off)."""

    def __init__(self, rows: list[tuple], description, lastrowid: int | None, rowcount: int):
        self._rows = rows
        self._i = 0
        self.description = description
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchall(self) -> list[tuple]:
        out = self._rows[self._i:]
        self._i = len(self._rows)
        return out

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        r = self._rows[self._i]
        self._i += 1
        return r

    def __iter__(self):
        return iter(self.fetchall())


class PgConn:
    """psycopg 3 connection wrapped to look like the sqlite3 connection the store expects.

    Autocommit, one connection shared across threads (psycopg 3 serialises access; results are fetched eagerly so
    no cursor outlives the call), automatic reconnect once on a dropped connection, server-side prepared statements
    off (Supabase's transaction pooler rejects them)."""

    def __init__(self, url: str):
        import psycopg
        self._psycopg = psycopg
        self._url = url
        self._lock = threading.RLock()
        self._last_id: int | None = None
        self._conn = None
        self._connect()

    def _connect(self) -> None:
        self._conn = self._psycopg.connect(self._url, autocommit=True, prepare_threshold=None, connect_timeout=15)

    def _run(self, sql: str, params: Iterable[Any] | None) -> _Result:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(sql, tuple(params) if params is not None else None)
                rows = cur.fetchall() if cur.description else []
                desc = [(c.name,) for c in cur.description] if cur.description else None
                return _Result(rows, desc, None, cur.rowcount)

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> _Result:
        pg_sql, wants_id = translate_sql(sql)
        if pg_sql == "__LAST_ID__":
            return _Result([(self._last_id,)], [("last_insert_rowid()",)], self._last_id, 1)
        try:
            res = self._run(pg_sql, params)
        except (self._psycopg.OperationalError, self._psycopg.InterfaceError):
            with self._lock:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._connect()
            res = self._run(pg_sql, params)
        if wants_id:
            row = res.fetchone()
            self._last_id = int(row[0]) if row else None
            return _Result([], None, self._last_id, res.rowcount)
        return res

    def executescript(self, script: str) -> None:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(translate_ddl(script))

    def commit(self) -> None:          # autocommit: nothing to do
        pass

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # schema introspection used by Store.__init__ migrations
    def columns(self, table: str) -> set[str]:
        res = self._run("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s", (table,))
        return {r[0] for r in res.fetchall()}

    def add_column(self, table: str, col: str, decl: str) -> None:
        self._run(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {translate_ddl(decl)}", None)


def connect(path=None, url: str | None = None):
    """Open the store's connection. A Postgres URL wins; otherwise the SQLite file."""
    if is_postgres_url(url):
        return PgConn(url)
    return sqlite3.connect(str(path), check_same_thread=False)


def columns(conn, table: str) -> set[str]:
    if isinstance(conn, PgConn):
        return conn.columns(table)
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def add_column(conn, table: str, col: str, decl: str) -> None:
    if isinstance(conn, PgConn):
        conn.add_column(table, col, decl)
    else:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
