"""SQLite store: runs/events, CRM contacts, approval queue, leads, users and desks.

Multi-tenant: every business object belongs to a desk (`desk_id`). `Store.for_desk(desk_id)` returns a
`DeskStore` view that pre-binds the desk so the orchestrator and the API never pass it explicitly.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .config import DATA_DIR

DB_PATH = DATA_DIR / "atlas.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, created REAL, task TEXT, mode TEXT, status TEXT,
  summary TEXT, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, run_dir TEXT, desk_id INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT, ts REAL, kind TEXT, agent TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, company TEXT, email TEXT, phone TEXT,
  stage TEXT DEFAULT 'New', notes TEXT DEFAULT '', next_action TEXT DEFAULT '', updated REAL, desk_id INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, created REAL, agent TEXT, kind TEXT, "to" TEXT,
  subject TEXT, body TEXT, reason TEXT, status TEXT DEFAULT 'pending', decided_at REAL, decided_by TEXT, note TEXT,
  flags TEXT DEFAULT '', desk_id INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, name TEXT, company TEXT, pw_hash TEXT, created REAL, last_login REAL
);
CREATE TABLE IF NOT EXISTS desks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, name TEXT, template TEXT, tier TEXT DEFAULT 'free',
  config TEXT DEFAULT '{}', created REAL
);
CREATE TABLE IF NOT EXISTS connectors (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER, kind TEXT, name TEXT, config TEXT DEFAULT '{}',
  auto INTEGER DEFAULT 0, status TEXT DEFAULT '', last_test REAL, created REAL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER, kind TEXT, name TEXT, task TEXT DEFAULT '',
  every_min INTEGER DEFAULT 0, next_run REAL, last_run REAL, last_result TEXT DEFAULT '', enabled INTEGER DEFAULT 1, created REAL
);
CREATE TABLE IF NOT EXISTS design_sessions (
  sid TEXT PRIMARY KEY, data TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER, key TEXT, value TEXT, source TEXT DEFAULT '', created REAL, updated REAL
);
CREATE INDEX IF NOT EXISTS ix_mem_desk ON memories(desk_id);
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, name TEXT, company TEXT, email TEXT, phone TEXT,
  source TEXT, notes TEXT, status TEXT DEFAULT 'new', run_id TEXT, desk_id INTEGER DEFAULT 1
);
"""

# columns added after the first release — applied idempotently on open
_MIGRATIONS = [
    ("runs", "desk_id", "INTEGER DEFAULT 1"),
    ("contacts", "desk_id", "INTEGER DEFAULT 1"),
    ("actions", "desk_id", "INTEGER DEFAULT 1"),
    ("actions", "flags", "TEXT DEFAULT ''"),
    ("leads", "desk_id", "INTEGER DEFAULT 1"),
    ("desks", "hook_token", "TEXT"),
    ("jobs", "last_status", "TEXT DEFAULT ''"),
    ("runs", "ended", "REAL"),
]

STAGES = ("New", "Contacted", "Qualified", "Proposal", "Won", "Lost")


def _rows(cur) -> list[dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class Store:
    STAGES = STAGES

    def __init__(self, path=DB_PATH):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        for table, col, decl in _MIGRATIONS:
            have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        # the old schema had a UNIQUE index on contacts.email; drop it so two desks can hold the same person
        for r in self._conn.execute("PRAGMA index_list(contacts)").fetchall():
            if r[2] == 1 and r[1].startswith("sqlite_autoindex"):
                self._rebuild_contacts()
                break
        self._conn.commit()

    def _rebuild_contacts(self):
        c = self._conn
        c.executescript("""
        CREATE TABLE contacts_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, company TEXT, email TEXT, phone TEXT,
          stage TEXT DEFAULT 'New', notes TEXT DEFAULT '', next_action TEXT DEFAULT '', updated REAL, desk_id INTEGER DEFAULT 1);
        INSERT INTO contacts_new(id,name,company,email,phone,stage,notes,next_action,updated,desk_id)
          SELECT id,name,company,email,phone,stage,notes,next_action,updated,desk_id FROM contacts;
        DROP TABLE contacts; ALTER TABLE contacts_new RENAME TO contacts;""")

    def for_desk(self, desk_id: int) -> "DeskStore":
        return DeskStore(self, int(desk_id))

    # ------------------------------------------------------------------ desks
    def add_desk(self, owner_id: int, name: str, template: str, tier: str, config: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute("INSERT INTO desks(owner_id,name,template,tier,config,created) VALUES(?,?,?,?,?,?)",
                                     (owner_id, name.strip(), template, tier, json.dumps(config), time.time()))
            self._conn.commit()
            return self.desk(cur.lastrowid)  # type: ignore[return-value]

    def desk(self, desk_id: int) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT * FROM desks WHERE id=?", (desk_id,)))
        if not rows:
            return None
        d = rows[0]
        d["config"] = json.loads(d.get("config") or "{}")
        return d

    def desks_for(self, owner_id: int) -> list[dict[str, Any]]:
        out = []
        for d in _rows(self._conn.execute("SELECT * FROM desks WHERE owner_id=? ORDER BY created", (owner_id,))):
            d["config"] = json.loads(d.get("config") or "{}")
            out.append(d)
        return out

    def desk_by_token(self, token: str) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT * FROM desks WHERE hook_token=?", (token,)))
        if not rows:
            return None
        d = rows[0]
        d["config"] = json.loads(d.get("config") or "{}")
        return d

    def ensure_hook_token(self, desk_id: int) -> str:
        import secrets as _s
        row = self._conn.execute("SELECT hook_token FROM desks WHERE id=?", (desk_id,)).fetchone()
        if row and row[0]:
            return row[0]
        tok = _s.token_urlsafe(18)
        with self._lock:
            self._conn.execute("UPDATE desks SET hook_token=? WHERE id=?", (tok, desk_id))
            self._conn.commit()
        return tok

    # ------------------------------------------------------------------ connectors
    def add_connector(self, desk_id: int, kind: str, name: str, config: dict[str, Any], auto: bool = False) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute("INSERT INTO connectors(desk_id,kind,name,config,auto,created) VALUES(?,?,?,?,?,?)",
                                     (desk_id, kind, name.strip(), json.dumps(config), 1 if auto else 0, time.time()))
            self._conn.commit()
            return self.connector(cur.lastrowid)  # type: ignore[return-value]

    def connector(self, cid: int) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT * FROM connectors WHERE id=?", (cid,)))
        if not rows:
            return None
        c = rows[0]
        c["config"] = json.loads(c.get("config") or "{}")
        return c

    def connectors(self, desk_id: int) -> list[dict[str, Any]]:
        out = []
        for c in _rows(self._conn.execute("SELECT * FROM connectors WHERE desk_id=? ORDER BY created", (desk_id,))):
            c["config"] = json.loads(c.get("config") or "{}")
            out.append(c)
        return out

    def connector_by_name(self, desk_id: int, name: str) -> dict[str, Any] | None:
        n = (name or "").strip().lower()
        return next((c for c in self.connectors(desk_id) if c["name"].lower() == n), None)

    def update_connector(self, cid: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in ("name", "config", "auto", "status", "last_test")}
        if "config" in fields and not isinstance(fields["config"], str):
            fields["config"] = json.dumps(fields["config"])
        if not fields:
            return
        with self._lock:
            self._conn.execute(f"UPDATE connectors SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), cid))
            self._conn.commit()

    def delete_connector(self, cid: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM connectors WHERE id=?", (cid,))
            self._conn.commit()

    # ------------------------------------------------------------------ memories (desk-wide facts)
    def remember(self, desk_id: int, key: str, value: str, source: str = "") -> dict[str, Any]:
        key = (key or "").strip()[:120]
        with self._lock:
            row = self._conn.execute("SELECT id FROM memories WHERE desk_id=? AND key=?", (desk_id, key)).fetchone()
            if row:
                self._conn.execute("UPDATE memories SET value=?, source=?, updated=? WHERE id=?", (value, source, time.time(), row[0]))
                mid = row[0]
            else:
                cur = self._conn.execute("INSERT INTO memories(desk_id,key,value,source,created,updated) VALUES(?,?,?,?,?,?)",
                                         (desk_id, key, value, source, time.time(), time.time()))
                mid = cur.lastrowid
            self._conn.commit()
        return _rows(self._conn.execute("SELECT * FROM memories WHERE id=?", (mid,)))[0]

    def recall(self, desk_id: int, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        q = f"%{(query or '').strip()}%"
        return _rows(self._conn.execute(
            "SELECT * FROM memories WHERE desk_id=? AND (key LIKE ? OR value LIKE ?) ORDER BY updated DESC LIMIT ?", (desk_id, q, q, limit)))

    def forget(self, desk_id: int, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM memories WHERE desk_id=? AND key=?", (desk_id, key))
            self._conn.commit()

    # ------------------------------------------------------------------ jobs (automations)
    def add_job(self, desk_id: int, kind: str, name: str, task: str, every_min: int = 0, next_run: float | None = None) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs(desk_id,kind,name,task,every_min,next_run,enabled,created) VALUES(?,?,?,?,?,?,1,?)",
                (desk_id, kind, name.strip(), task or "", int(every_min or 0), next_run if next_run is not None else time.time(), time.time()))
            self._conn.commit()
            return self.job(cur.lastrowid)  # type: ignore[return-value]

    def job(self, jid: int) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)))
        return rows[0] if rows else None

    def jobs(self, desk_id: int) -> list[dict[str, Any]]:
        return _rows(self._conn.execute("SELECT * FROM jobs WHERE desk_id=? ORDER BY created", (desk_id,)))

    def due_jobs(self, now: float) -> list[dict[str, Any]]:
        return _rows(self._conn.execute("SELECT * FROM jobs WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=? ORDER BY next_run", (now,)))

    def update_job(self, jid: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in ("name", "task", "every_min", "next_run", "last_run", "last_result", "last_status", "enabled")}
        if not fields:
            return
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), jid))
            self._conn.commit()

    def delete_job(self, jid: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
            self._conn.commit()

    def all_desks(self) -> list[dict[str, Any]]:
        out = []
        for d in _rows(self._conn.execute("SELECT * FROM desks ORDER BY created")):
            d["config"] = json.loads(d.get("config") or "{}")
            out.append(d)
        return out

    def update_desk(self, desk_id: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in ("name", "template", "tier", "config")}
        if "config" in fields and not isinstance(fields["config"], str):
            fields["config"] = json.dumps(fields["config"])
        if not fields:
            return
        with self._lock:
            self._conn.execute(f"UPDATE desks SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), desk_id))
            self._conn.commit()

    def delete_desk_data(self, desk_id: int) -> None:
        with self._lock:
            run_ids = [r[0] for r in self._conn.execute("SELECT id FROM runs WHERE desk_id=?", (desk_id,)).fetchall()]
            for rid in run_ids:
                self._conn.execute("DELETE FROM events WHERE run_id=?", (rid,))
            for t in ("runs", "actions", "leads", "contacts", "jobs", "memories"):
                self._conn.execute(f"DELETE FROM {t} WHERE desk_id=?", (desk_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ runs
    def create_run(self, run_id: str, task: str, mode: str, run_dir: str, desk_id: int = 1) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs(id,created,task,mode,status,summary,run_dir,desk_id) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, time.time(), task, mode, "running", "", run_dir, desk_id))
            self._conn.commit()

    def finish_run(self, run_id: str, status: str, summary: str, tin: int, tout: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE runs SET status=?, summary=?, tokens_in=?, tokens_out=?, ended=? WHERE id=?",
                               (status, summary, tin, tout, time.time(), run_id))
            self._conn.commit()

    def running_runs(self, older_than_s: float = 0) -> list[dict[str, Any]]:
        """Runs still marked running (optionally only those older than N seconds)."""
        return _rows(self._conn.execute("SELECT id,created,task,mode,status,desk_id FROM runs WHERE status='running' AND created<=? ORDER BY created",
                                        (time.time() - older_than_s,)))

    def mark_interrupted(self, reason: str = "server restarted") -> int:
        """Anything still 'running' when the process starts cannot be running - mark it so it never shows as live."""
        with self._lock:
            cur = self._conn.execute("UPDATE runs SET status='interrupted', summary=?, ended=? WHERE status='running'", (reason, time.time()))
            self._conn.commit()
            return cur.rowcount

    def runs_between(self, since: float, desk_id: int | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT id,created,ended,status,tokens_in,tokens_out,desk_id,summary FROM runs WHERE created>=?", [since]
        if desk_id is not None:
            q += " AND desk_id=?"; args.append(desk_id)
        return _rows(self._conn.execute(q + " ORDER BY created", args))

    def oldest_pending_action(self, desk_id: int | None = None) -> float | None:
        q, args = "SELECT MIN(created) FROM actions WHERE status='pending'", []
        if desk_id is not None:
            q += " AND desk_id=?"; args.append(desk_id)
        v = self._conn.execute(q, args).fetchone()[0]
        return float(v) if v else None

    def save_design_session(self, sid: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO design_sessions(sid,data,updated) VALUES(?,?,?)",
                               (sid, json.dumps(data, ensure_ascii=False), time.time()))
            self._conn.execute("DELETE FROM design_sessions WHERE updated < ?", (time.time() - 30 * 86400,))
            self._conn.commit()

    def load_design_session(self, sid: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT data FROM design_sessions WHERE sid=?", (sid,)).fetchone()
        try:
            return json.loads(row[0]) if row else None
        except (TypeError, json.JSONDecodeError):
            return None

    def add_event(self, run_id: str, kind: str, agent: str, text: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events VALUES(?,?,?,?,?)", (run_id, time.time(), kind, agent, text))
            self._conn.commit()

    def runs(self, limit: int = 200, desk_id: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT id,created,task,mode,status,summary,tokens_in,tokens_out,run_dir,desk_id FROM runs"
        args: tuple = ()
        if desk_id is not None:
            q += " WHERE desk_id=?"
            args = (desk_id,)
        return _rows(self._conn.execute(q + " ORDER BY created DESC LIMIT ?", (*args, limit)))

    def run(self, run_id: str) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)))
        return rows[0] if rows else None

    def events(self, run_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute("SELECT ts,kind,agent,text FROM events WHERE run_id=? ORDER BY ts", (run_id,))
        return [{"ts": r[0], "kind": r[1], "agent": r[2], "text": r[3]} for r in cur.fetchall()]

    def delete_run(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE run_id=?", (run_id,))
            self._conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ contacts (CRM)
    def contacts(self, query: str = "", desk_id: int = 1) -> list[dict[str, Any]]:
        q = f"%{query.strip()}%"
        cur = self._conn.execute(
            "SELECT * FROM contacts WHERE desk_id=? AND (name LIKE ? OR company LIKE ? OR email LIKE ?) ORDER BY updated DESC",
            (desk_id, q, q, q))
        return _rows(cur)

    def upsert_contact(self, contact: str, fields: dict[str, Any], desk_id: int = 1) -> dict[str, Any]:
        contact = (contact or "").strip()
        fields = {k: v for k, v in (fields or {}).items() if k in ("name", "company", "email", "phone", "stage", "notes", "next_action")}
        if "stage" in fields and fields["stage"] not in STAGES:
            fields["stage"] = "New"
        with self._lock:
            row = self._conn.execute("SELECT id FROM contacts WHERE desk_id=? AND (email=? OR name=?) LIMIT 1",
                                     (desk_id, contact, contact)).fetchone()
            if row is None:
                email = fields.get("email") or (contact if "@" in contact else "")
                name = fields.get("name") or ("" if "@" in contact else contact)
                self._conn.execute(
                    "INSERT INTO contacts(name,company,email,phone,stage,notes,next_action,updated,desk_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (name, fields.get("company", ""), email or None, fields.get("phone", ""), fields.get("stage", "New"),
                     fields.get("notes", ""), fields.get("next_action", ""), time.time(), desk_id))
                cid = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            else:
                cid = row[0]
                sets = ", ".join(f"{k}=?" for k in fields)
                if sets:
                    self._conn.execute(f"UPDATE contacts SET {sets}, updated=? WHERE id=?", (*fields.values(), time.time(), cid))
            self._conn.commit()
        return _rows(self._conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)))[0]

    # ------------------------------------------------------------------ approval queue
    def add_action(self, run_id: str, agent: str, kind: str, to: str, subject: str, body: str, reason: str,
                   desk_id: int = 1, flags: str = "") -> int:
        with self._lock:
            self._conn.execute(
                'INSERT INTO actions(run_id,created,agent,kind,"to",subject,body,reason,desk_id,flags) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (run_id, time.time(), agent, kind, to, subject, body, reason, desk_id, flags))
            aid = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._conn.commit()
        return aid

    def actions(self, status: str = "", limit: int = 200, desk_id: int | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if status:
            where.append("status=?"); args.append(status)
        if desk_id is not None:
            where.append("desk_id=?"); args.append(desk_id)
        q = "SELECT * FROM actions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created DESC LIMIT ?"
        return _rows(self._conn.execute(q, (*args, limit)))

    def action(self, aid: int) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT * FROM actions WHERE id=?", (aid,)))
        return rows[0] if rows else None

    def decide_action(self, aid: int, status: str, by: str = "owner", note: str = "", body: str | None = None,
                      subject: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if body is not None:
                self._conn.execute("UPDATE actions SET body=? WHERE id=?", (body, aid))
            if subject is not None:
                self._conn.execute("UPDATE actions SET subject=? WHERE id=?", (subject, aid))
            self._conn.execute("UPDATE actions SET status=?, decided_at=?, decided_by=?, note=? WHERE id=?",
                               (status, time.time(), by, note, aid))
            self._conn.commit()
        return self.action(aid)

    # ------------------------------------------------------------------ leads
    def add_lead(self, name: str, company: str, email: str, phone: str, source: str, notes: str, desk_id: int = 1) -> int:
        with self._lock:
            self._conn.execute("INSERT INTO leads(created,name,company,email,phone,source,notes,desk_id) VALUES(?,?,?,?,?,?,?,?)",
                               (time.time(), name, company, email, phone, source, notes, desk_id))
            lid = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._conn.commit()
        return lid

    def leads(self, limit: int = 200, desk_id: int | None = None) -> list[dict[str, Any]]:
        if desk_id is None:
            return _rows(self._conn.execute("SELECT * FROM leads ORDER BY created DESC LIMIT ?", (limit,)))
        return _rows(self._conn.execute("SELECT * FROM leads WHERE desk_id=? ORDER BY created DESC LIMIT ?", (desk_id, limit)))

    def lead(self, lid: int) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT * FROM leads WHERE id=?", (lid,)))
        return rows[0] if rows else None

    def set_lead(self, lid: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in ("status", "run_id", "notes")}
        if not fields:
            return
        with self._lock:
            self._conn.execute(f"UPDATE leads SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), lid))
            self._conn.commit()

    # ------------------------------------------------------------------ users
    def add_user(self, email: str, name: str, company: str, pw_hash: str) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute("INSERT INTO users(email,name,company,pw_hash,created) VALUES(?,?,?,?,?)",
                                     (email.strip().lower(), name.strip(), company.strip(), pw_hash, time.time()))
            self._conn.commit()
            return self.user(cur.lastrowid)  # type: ignore[return-value]

    def user(self, uid: int) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT id,email,name,company,created,last_login FROM users WHERE id=?", (uid,)))
        return rows[0] if rows else None

    def user_by_email(self, email: str) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)))
        return rows[0] if rows else None

    def touch_login(self, uid: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), uid))
            self._conn.commit()

    def user_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    # ------------------------------------------------------------------ reporting
    def all_events(self, limit: int = 300, desk_id: int | None = None) -> list[dict[str, Any]]:
        if desk_id is None:
            return _rows(self._conn.execute("SELECT run_id,ts,kind,agent,text FROM events ORDER BY ts DESC LIMIT ?", (limit,)))
        return _rows(self._conn.execute(
            "SELECT e.run_id,e.ts,e.kind,e.agent,e.text FROM events e JOIN runs r ON r.id=e.run_id WHERE r.desk_id=? "
            "ORDER BY e.ts DESC LIMIT ?", (desk_id, limit)))

    def stats(self, desk_id: int | None = None) -> dict[str, Any]:
        c = self._conn
        w = "" if desk_id is None else f" WHERE desk_id={int(desk_id)}"
        a = " AND" if w else " WHERE"
        one = lambda q: c.execute(q).fetchone()[0]
        return {
            "leads": one(f"SELECT COUNT(*) FROM leads{w}"),
            "runs": one(f"SELECT COUNT(*) FROM runs{w}"),
            "runs_done": one(f"SELECT COUNT(*) FROM runs{w}{a} status='done'"),
            "pending": one(f"SELECT COUNT(*) FROM actions{w}{a} status='pending'"),
            "approved": one(f"SELECT COUNT(*) FROM actions{w}{a} status IN ('approved','sent')"),
            "rejected": one(f"SELECT COUNT(*) FROM actions{w}{a} status='rejected'"),
            "contacts": one(f"SELECT COUNT(*) FROM contacts{w}"),
            "qualified": one(f"SELECT COUNT(*) FROM contacts{w}{a} stage IN ('Qualified','Proposal','Won')"),
            "tokens_in": one(f"SELECT COALESCE(SUM(tokens_in),0) FROM runs{w}"),
            "tokens_out": one(f"SELECT COALESCE(SUM(tokens_out),0) FROM runs{w}"),
        }


class DeskStore:
    """Store view bound to one desk. Same method names the orchestrator and API use, desk pre-filled."""
    STAGES = STAGES

    def __init__(self, store: Store, desk_id: int):
        self.s = store
        self.desk_id = desk_id

    # passthroughs that carry the desk
    def create_run(self, run_id, task, mode, run_dir): return self.s.create_run(run_id, task, mode, run_dir, self.desk_id)
    def finish_run(self, *a, **k): return self.s.finish_run(*a, **k)
    def add_event(self, *a, **k): return self.s.add_event(*a, **k)
    def runs(self, limit=200): return self.s.runs(limit, self.desk_id)
    def run(self, run_id): return self.s.run(run_id)
    def events(self, run_id): return self.s.events(run_id)
    def contacts(self, query=""): return self.s.contacts(query, self.desk_id)
    def upsert_contact(self, contact, fields): return self.s.upsert_contact(contact, fields, self.desk_id)
    def add_action(self, run_id, agent, kind, to, subject, body, reason, flags=""):
        return self.s.add_action(run_id, agent, kind, to, subject, body, reason, self.desk_id, flags)
    def actions(self, status="", limit=200): return self.s.actions(status, limit, self.desk_id)
    def action(self, aid): return self.s.action(aid)
    def decide_action(self, *a, **k): return self.s.decide_action(*a, **k)
    def add_lead(self, name, company, email, phone, source, notes):
        return self.s.add_lead(name, company, email, phone, source, notes, self.desk_id)
    def leads(self, limit=200): return self.s.leads(limit, self.desk_id)
    def lead(self, lid): return self.s.lead(lid)
    def set_lead(self, lid, **f): return self.s.set_lead(lid, **f)
    def all_events(self, limit=300): return self.s.all_events(limit, self.desk_id)
    def stats(self): return self.s.stats(self.desk_id)
    def reset(self): return self.s.delete_desk_data(self.desk_id)
    def connectors(self): return self.s.connectors(self.desk_id)
    def connector_by_name(self, name): return self.s.connector_by_name(self.desk_id, name)
    def add_job(self, kind, name, task, every_min=0, next_run=None):
        return self.s.add_job(self.desk_id, kind, name, task, every_min, next_run)
    def jobs(self): return self.s.jobs(self.desk_id)
    def remember(self, key, value, source=""): return self.s.remember(self.desk_id, key, value, source)
    def recall(self, query="", limit=20): return self.s.recall(self.desk_id, query, limit)
    def forget(self, key): return self.s.forget(self.desk_id, key)
