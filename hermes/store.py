"""SQLite run history."""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

from .config import DATA_DIR

DB_PATH = DATA_DIR / "hermes.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, created REAL, task TEXT, mode TEXT, status TEXT,
  summary TEXT, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, run_dir TEXT
);
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT, ts REAL, kind TEXT, agent TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, company TEXT, email TEXT UNIQUE, phone TEXT,
  stage TEXT DEFAULT 'New', notes TEXT DEFAULT '', next_action TEXT DEFAULT '', updated REAL
);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, created REAL, agent TEXT, kind TEXT, "to" TEXT,
  subject TEXT, body TEXT, reason TEXT, status TEXT DEFAULT 'pending', decided_at REAL, decided_by TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, name TEXT, company TEXT, email TEXT, phone TEXT,
  source TEXT, notes TEXT, status TEXT DEFAULT 'new', run_id TEXT
);
"""


def _rows(cur) -> list[dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class Store:
    def __init__(self, path=DB_PATH):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def create_run(self, run_id: str, task: str, mode: str, run_dir: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs(id,created,task,mode,status,summary,run_dir) VALUES(?,?,?,?,?,?,?)",
                (run_id, time.time(), task, mode, "running", "", run_dir))
            self._conn.commit()

    def finish_run(self, run_id: str, status: str, summary: str, tin: int, tout: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE runs SET status=?, summary=?, tokens_in=?, tokens_out=? WHERE id=?",
                               (status, summary, tin, tout, run_id))
            self._conn.commit()

    def add_event(self, run_id: str, kind: str, agent: str, text: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events VALUES(?,?,?,?,?)", (run_id, time.time(), kind, agent, text))
            self._conn.commit()

    def runs(self, limit: int = 200) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT id,created,task,mode,status,summary,tokens_in,tokens_out,run_dir FROM runs ORDER BY created DESC LIMIT ?",
            (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def events(self, run_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute("SELECT ts,kind,agent,text FROM events WHERE run_id=? ORDER BY ts", (run_id,))
        return [{"ts": r[0], "kind": r[1], "agent": r[2], "text": r[3]} for r in cur.fetchall()]

    def delete_run(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE run_id=?", (run_id,))
            self._conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ contacts (CRM)
    STAGES = ("New", "Contacted", "Qualified", "Proposal", "Won", "Lost")

    def contacts(self, query: str = "") -> list[dict[str, Any]]:
        q = f"%{query.strip()}%"
        cur = self._conn.execute(
            "SELECT * FROM contacts WHERE name LIKE ? OR company LIKE ? OR email LIKE ? ORDER BY updated DESC", (q, q, q))
        return _rows(cur)

    def upsert_contact(self, contact: str, fields: dict[str, Any]) -> dict[str, Any]:
        contact = (contact or "").strip()
        fields = {k: v for k, v in (fields or {}).items() if k in ("name", "company", "email", "phone", "stage", "notes", "next_action")}
        if "stage" in fields and fields["stage"] not in self.STAGES:
            fields["stage"] = "New"
        with self._lock:
            row = self._conn.execute("SELECT * FROM contacts WHERE email=? OR name=? LIMIT 1", (contact, contact)).fetchone()
            if row is None:
                email = fields.get("email") or (contact if "@" in contact else "")
                name = fields.get("name") or ("" if "@" in contact else contact)
                self._conn.execute(
                    "INSERT INTO contacts(name,company,email,phone,stage,notes,next_action,updated) VALUES(?,?,?,?,?,?,?,?)",
                    (name, fields.get("company", ""), email or None, fields.get("phone", ""), fields.get("stage", "New"),
                     fields.get("notes", ""), fields.get("next_action", ""), time.time()))
                cid = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            else:
                cid = row[0]
                sets = ", ".join(f"{k}=?" for k in fields)
                if sets:
                    self._conn.execute(f"UPDATE contacts SET {sets}, updated=? WHERE id=?", (*fields.values(), time.time(), cid))
            self._conn.commit()
        return _rows(self._conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)))[0]

    # ------------------------------------------------------------------ approval queue
    def add_action(self, run_id: str, agent: str, kind: str, to: str, subject: str, body: str, reason: str) -> int:
        with self._lock:
            self._conn.execute(
                'INSERT INTO actions(run_id,created,agent,kind,"to",subject,body,reason) VALUES(?,?,?,?,?,?,?,?)',
                (run_id, time.time(), agent, kind, to, subject, body, reason))
            aid = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._conn.commit()
        return aid

    def actions(self, status: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if status:
            cur = self._conn.execute("SELECT * FROM actions WHERE status=? ORDER BY created DESC LIMIT ?", (status, limit))
        else:
            cur = self._conn.execute("SELECT * FROM actions ORDER BY created DESC LIMIT ?", (limit,))
        return _rows(cur)

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
        rows = _rows(self._conn.execute("SELECT * FROM actions WHERE id=?", (aid,)))
        return rows[0] if rows else None

    # ------------------------------------------------------------------ leads
    def add_lead(self, name: str, company: str, email: str, phone: str, source: str, notes: str) -> int:
        with self._lock:
            self._conn.execute("INSERT INTO leads(created,name,company,email,phone,source,notes) VALUES(?,?,?,?,?,?,?)",
                               (time.time(), name, company, email, phone, source, notes))
            lid = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._conn.commit()
        return lid

    def leads(self, limit: int = 200) -> list[dict[str, Any]]:
        return _rows(self._conn.execute("SELECT * FROM leads ORDER BY created DESC LIMIT ?", (limit,)))

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

    def all_events(self, limit: int = 300) -> list[dict[str, Any]]:
        return _rows(self._conn.execute("SELECT run_id,ts,kind,agent,text FROM events ORDER BY ts DESC LIMIT ?", (limit,)))

    def stats(self) -> dict[str, Any]:
        c = self._conn
        one = lambda q, *a: c.execute(q, a).fetchone()[0]
        return {
            "leads": one("SELECT COUNT(*) FROM leads"),
            "runs": one("SELECT COUNT(*) FROM runs"),
            "runs_done": one("SELECT COUNT(*) FROM runs WHERE status='done'"),
            "pending": one("SELECT COUNT(*) FROM actions WHERE status='pending'"),
            "approved": one("SELECT COUNT(*) FROM actions WHERE status IN ('approved','sent')"),
            "rejected": one("SELECT COUNT(*) FROM actions WHERE status='rejected'"),
            "contacts": one("SELECT COUNT(*) FROM contacts"),
            "qualified": one("SELECT COUNT(*) FROM contacts WHERE stage IN ('Qualified','Proposal','Won')"),
            "tokens_in": one("SELECT COALESCE(SUM(tokens_in),0) FROM runs"),
            "tokens_out": one("SELECT COALESCE(SUM(tokens_out),0) FROM runs"),
        }
