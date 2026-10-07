"""Store: runs/events, CRM contacts, approval queue, leads, users and desks. SQLite locally, PostgreSQL when
DATABASE_URL is set (Render's disk is ephemeral - see atlas/db.py).

Multi-tenant: every business object belongs to a desk (`desk_id`). `Store.for_desk(desk_id)` returns a
`DeskStore` view that pre-binds the desk so the orchestrator and the API never pass it explicitly.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import db as DB
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
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER DEFAULT 1, contact_key TEXT, channel TEXT, dir TEXT,
  addr TEXT, actor TEXT DEFAULT '', subject TEXT DEFAULT '', body TEXT DEFAULT '', status TEXT DEFAULT 'sent',
  ts REAL, run_id TEXT DEFAULT '', action_id INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_messages_desk ON messages(desk_id, contact_key, ts);
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
CREATE TABLE IF NOT EXISTS vision_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER, camera TEXT, ts REAL, counts TEXT DEFAULT '{}',
  motion REAL DEFAULT 0, backend TEXT DEFAULT '', reason TEXT DEFAULT '', question TEXT DEFAULT '', answer TEXT DEFAULT '',
  snapshot TEXT DEFAULT '', triggered INTEGER DEFAULT 0, run_id TEXT DEFAULT '', source TEXT DEFAULT 'camera'
);
CREATE INDEX IF NOT EXISTS ix_vision_desk ON vision_events(desk_id, ts);
CREATE TABLE IF NOT EXISTS vision_vectors (
  event_id INTEGER PRIMARY KEY, desk_id INTEGER, ts REAL, model TEXT DEFAULT '', dim INTEGER DEFAULT 0,
  text_vec BLOB, image_vec BLOB
);
CREATE INDEX IF NOT EXISTS ix_vvec_desk ON vision_vectors(desk_id, model, ts);
CREATE TABLE IF NOT EXISTS vision_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER, camera TEXT, track_id INTEGER DEFAULT 0, label TEXT,
  label_conf REAL DEFAULT 0, votes TEXT DEFAULT '{}', first_ts REAL, last_ts REAL, hits INTEGER DEFAULT 0,
  best_conf REAL DEFAULT 0, box TEXT DEFAULT '[]', frame_w INTEGER DEFAULT 0, frame_h INTEGER DEFAULT 0,
  crop TEXT DEFAULT '', path TEXT DEFAULT '[]', status TEXT DEFAULT 'active', verdict TEXT DEFAULT '',
  verdict_by TEXT DEFAULT '', second_label TEXT DEFAULT '', second_score REAL DEFAULT 0, description TEXT DEFAULT '',
  attrs TEXT DEFAULT '{}', watch INTEGER DEFAULT 0, emb BLOB, clip_pos REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_vobj_desk ON vision_objects(desk_id, camera, last_ts);
CREATE TABLE IF NOT EXISTS vision_object_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER, desk_id INTEGER, ts REAL, kind TEXT DEFAULT 'note',
  question TEXT DEFAULT '', text TEXT DEFAULT '', crop TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_vobjn ON vision_object_notes(object_id, ts);
CREATE TABLE IF NOT EXISTS sec_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER, ts REAL, ingested REAL, source TEXT DEFAULT '',
  sensor TEXT DEFAULT '', kind TEXT DEFAULT '', src TEXT DEFAULT '', dst TEXT DEFAULT '', username TEXT DEFAULT '',
  sig TEXT DEFAULT '', severity TEXT DEFAULT 'info', message TEXT DEFAULT '', raw TEXT DEFAULT '',
  attrs TEXT DEFAULT '{}', origin TEXT DEFAULT '', triggered INTEGER DEFAULT 0, run_id TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_sec_desk_ts ON sec_events(desk_id, ts);
CREATE INDEX IF NOT EXISTS ix_sec_desk_src ON sec_events(desk_id, src);
CREATE INDEX IF NOT EXISTS ix_sec_desk_dst ON sec_events(desk_id, dst);
CREATE INDEX IF NOT EXISTS ix_sec_desk_sensor ON sec_events(desk_id, sensor, ts);
CREATE TABLE IF NOT EXISTS sec_detections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER, det_id TEXT, rule TEXT DEFAULT '', severity TEXT DEFAULT 'low',
  title TEXT DEFAULT '', first_ts REAL, last_ts REAL, evidence TEXT DEFAULT '[]', evidence_total INTEGER DEFAULT 0,
  counts TEXT DEFAULT '{}', details TEXT DEFAULT '{}', sources TEXT DEFAULT '[]', sensors TEXT DEFAULT '[]',
  src TEXT DEFAULT '[]', dst TEXT DEFAULT '[]', users TEXT DEFAULT '[]', run_id TEXT DEFAULT '', created REAL, updated REAL
);
CREATE INDEX IF NOT EXISTS ix_secdet_desk ON sec_detections(desk_id, det_id);
CREATE TABLE IF NOT EXISTS named_things (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER, name TEXT, kind TEXT DEFAULT 'object', label TEXT DEFAULT '',
  notes TEXT DEFAULT '', created REAL, exemplars TEXT DEFAULT '[]', emb BLOB, dim INTEGER DEFAULT 0, sightings INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_named_desk ON named_things(desk_id);
"""

# columns added after the first release — applied idempotently on open
_MIGRATIONS = [
    ("vision_objects", "name_id", "INTEGER DEFAULT 0"),          # named things: which one this sighting was matched to
    ("vision_objects", "name_score", "REAL DEFAULT 0"),           # ...and how sure the match is (0-1, cosine)
    ("vision_objects", "name_by", "TEXT DEFAULT ''"),             # owner | match
    ("runs", "desk_id", "INTEGER DEFAULT 1"),
    ("contacts", "desk_id", "INTEGER DEFAULT 1"),
    ("actions", "desk_id", "INTEGER DEFAULT 1"),
    ("actions", "flags", "TEXT DEFAULT ''"),
    ("leads", "desk_id", "INTEGER DEFAULT 1"),
    ("desks", "hook_token", "TEXT"),
    ("jobs", "last_status", "TEXT DEFAULT ''"),
    ("runs", "ended", "REAL"),
    ("events", "data", "TEXT DEFAULT ''"),
    ("actions", "case_id", "INTEGER DEFAULT 0"),                 # the case an approval belongs to (atlas/cases.py)
    ("runs", "case_id", "INTEGER DEFAULT 0"),
]

STAGES = ("New", "Contacted", "Qualified", "Proposal", "Won", "Lost")

# security log (sec_events / sec_detections). Copies of atlas.cyber's limits: the store must not import cyber.
SEC_FIELD_LIMITS = {"source": 20, "sensor": 60, "kind": 24, "src": 64, "dst": 64, "user": 128,
                    "sig": 200, "message": 300, "raw": 4000, "origin": 80}
SEC_ATTRS_MAX = 2000
SEVERITIES = ("info", "low", "medium", "high", "critical")
_SEV_ORDER = "CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END"
# `user` is a reserved word in PostgreSQL, so the column is `username`; dicts and filters still say `user`
_SEC_COLS = "id,ts,ingested,source,sensor,kind,src,dst,username,sig,severity,message,attrs,origin,triggered,run_id"
_SEC_GROUP_COLS = {"src": "src", "dst": "dst", "user": "username", "sig": "sig", "kind": "kind", "sensor": "sensor",
                   "source": "source", "severity": "severity"}
_DET_JSON = {"evidence": [], "counts": {}, "details": {}, "sources": [], "sensors": [], "src": [], "dst": [], "users": []}
_ID_CHUNK = 500


def _rows(cur) -> list[dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _clip(value: Any, limit: int) -> str:
    """Defensive text for a sec_* column: str, no NUL (PostgreSQL TEXT cannot hold it), no lone surrogate (UTF-8
    cannot encode it; JSON posted to the hook may carry one), cut to the limit."""
    s = str(value if value is not None else "").replace("\x00", "")[:limit]
    return s if s.isascii() else s.encode("utf-8", "replace").decode("utf-8")


def _num(value: Any) -> float | None:
    """A finite float, or None."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _as_ids(ids) -> list[int]:
    out: list[int] = []
    for i in ids or []:
        try:
            out.append(int(i))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def _id_chunks(ids):
    """[None] = no id filter; otherwise the ids in IN-list chunks (an empty list yields no chunk at all)."""
    if ids is None:
        return [None]
    ids = _as_ids(ids)
    return [ids[i:i + _ID_CHUNK] for i in range(0, len(ids), _ID_CHUNK)]


def _json_load(text: Any, default: Any) -> Any:
    try:
        v = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def _sec_row(r: dict[str, Any]) -> dict[str, Any]:
    out = {("user" if k == "username" else k): v for k, v in r.items()}
    out["user"] = out.get("user") or ""
    out["attrs"] = _json_load(out.get("attrs"), {})
    out["triggered"] = int(out.get("triggered") or 0)
    out["run_id"] = out.get("run_id") or ""
    return out


def _det_row(r: dict[str, Any]) -> dict[str, Any]:
    out = {"id": r["det_id"], "rule": r.get("rule") or "", "severity": r.get("severity") or "low", "title": r.get("title") or ""}
    for k in ("src", "dst", "users", "sources", "sensors", "evidence"):
        out[k] = _json_load(r.get(k), [])
    out["evidence_total"] = int(r.get("evidence_total") or 0)
    out["counts"] = _json_load(r.get("counts"), {})
    out["details"] = _json_load(r.get("details"), {})
    out.update({"first_ts": r.get("first_ts"), "last_ts": r.get("last_ts"), "row_id": int(r["id"]),
                "run_id": r.get("run_id") or "", "created": r.get("created"), "updated": r.get("updated")})
    return out


class Store:
    STAGES = STAGES

    def __init__(self, path=DB_PATH, url: str | None = None):
        """SQLite file at `path`, or PostgreSQL when `url` is a postgres:// URL (see atlas/db.py)."""
        self._lock = threading.Lock()
        self._conn = DB.connect(path, url)
        self.backend = "postgres" if isinstance(self._conn, DB.PgConn) else "sqlite"
        from . import cases as _C, records as _R            # records + cases own their tables; created with the rest
        self._conn.executescript(_SCHEMA + _R.SCHEMA + _C.SCHEMA)
        for table, col, decl in _MIGRATIONS:
            if col not in DB.columns(self._conn, table):
                DB.add_column(self._conn, table, col, decl)
        # the old SQLite schema had a UNIQUE index on contacts.email; drop it so two desks can hold the same person
        if self.backend == "sqlite":
            for r in self._conn.execute("PRAGMA index_list(contacts)").fetchall():
                if r[2] == 1 and r[1].startswith("sqlite_autoindex"):
                    self._rebuild_contacts()
                    break
        self._conn.commit()

    def ping(self) -> bool:
        try:
            return self._conn.execute("SELECT 1").fetchone()[0] == 1
        except Exception:
            return False

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
        from . import secure as SEC
        with self._lock:
            cur = self._conn.execute("INSERT INTO connectors(desk_id,kind,name,config,auto,created) VALUES(?,?,?,?,?,?)",
                                     (desk_id, kind, name.strip(), SEC.encrypt_config(config), 1 if auto else 0, time.time()))
            self._conn.commit()
            return self.connector(cur.lastrowid)  # type: ignore[return-value]

    def connector(self, cid: int) -> dict[str, Any] | None:
        from . import secure as SEC
        rows = _rows(self._conn.execute("SELECT * FROM connectors WHERE id=?", (cid,)))
        if not rows:
            return None
        c = rows[0]
        c["config"] = SEC.decrypt_config(c.get("config"))
        return c

    def connectors(self, desk_id: int) -> list[dict[str, Any]]:
        from . import secure as SEC
        out = []
        for c in _rows(self._conn.execute("SELECT * FROM connectors WHERE desk_id=? ORDER BY created", (desk_id,))):
            c["config"] = SEC.decrypt_config(c.get("config"))
            out.append(c)
        return out

    def encrypt_legacy_connectors(self) -> int:
        """Boot migration: encrypt any connector row still stored as plaintext JSON. Returns rows converted."""
        from . import secure as SEC
        n = 0
        with self._lock:
            for cid, cfg in self._conn.execute("SELECT id, config FROM connectors").fetchall():
                if cfg and not SEC.is_encrypted(cfg):
                    self._conn.execute("UPDATE connectors SET config=? WHERE id=?", (SEC.encrypt_config(SEC.decrypt_config(cfg)), cid))
                    n += 1
            self._conn.commit()
        return n

    def connector_by_name(self, desk_id: int, name: str) -> dict[str, Any] | None:
        n = (name or "").strip().lower()
        return next((c for c in self.connectors(desk_id) if c["name"].lower() == n), None)

    def update_connector(self, cid: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in ("name", "config", "auto", "status", "last_test")}
        if "config" in fields:
            from . import secure as SEC
            cfg = fields["config"]
            fields["config"] = SEC.encrypt_config(cfg if isinstance(cfg, dict) else SEC.decrypt_config(cfg))
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

    # ------------------------------------------------------------------ vision events (cameras / sensors)
    def add_vision_event(self, desk_id: int, camera: str, counts: dict[str, int], motion: float = 0.0, backend: str = "",
                         reason: str = "", question: str = "", answer: str = "", snapshot: str = "", triggered: bool = False,
                         run_id: str = "", source: str = "camera", ts: float | None = None) -> dict[str, Any]:
        """`ts` = when it happened (a replayed or forwarded event keeps its own time); default now."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO vision_events(desk_id,camera,ts,counts,motion,backend,reason,question,answer,snapshot,triggered,run_id,source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (desk_id, camera, float(ts) if ts is not None else time.time(), json.dumps(counts or {}), float(motion or 0),
                 backend, reason, question, answer, snapshot, 1 if triggered else 0, run_id or "", source))
            self._conn.commit()
            vid = cur.lastrowid
        ev = self.vision_event(vid)
        try:                                            # embed for retrieval (background unless VISION_INDEX_SYNC=1)
            from . import rag
            rag.on_event(self, ev)
        except Exception:
            pass
        return ev  # type: ignore[return-value]

    # ------------------------------------------------------------------ vision objects (the catalogue of things seen)
    _OBJ_JSON = ("votes", "box", "path", "attrs")

    def _orow(self, r: dict[str, Any], emb: bool = False) -> dict[str, Any]:
        for k in self._OBJ_JSON:
            try:
                r[k] = json.loads(r.get(k) or ("{}" if k in ("votes", "attrs") else "[]"))
            except (TypeError, json.JSONDecodeError):
                r[k] = {} if k in ("votes", "attrs") else []
        if not emb:
            r.pop("emb", None)
        elif r.get("emb") is not None:
            r["emb"] = bytes(r["emb"])
        return r

    def add_vision_object(self, desk_id: int, camera: str, **f: Any) -> int:
        f = {**f, **{k: json.dumps(f[k]) for k in self._OBJ_JSON if k in f}}
        cols = ["desk_id", "camera"] + list(f)
        with self._lock:
            cur = self._conn.execute(f"INSERT INTO vision_objects({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                                     [desk_id, camera] + list(f.values()))
            self._conn.commit()
            return int(cur.lastrowid)

    def update_vision_object(self, oid: int, **f: Any) -> None:
        if not f:
            return
        f = {**f, **{k: json.dumps(f[k]) for k in self._OBJ_JSON if k in f}}
        with self._lock:
            self._conn.execute(f"UPDATE vision_objects SET {','.join(k + '=?' for k in f)} WHERE id=?", list(f.values()) + [oid])
            self._conn.commit()

    def vision_object(self, oid: int, emb: bool = False) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT * FROM vision_objects WHERE id=?", (oid,)))
        return self._orow(rows[0], emb) if rows else None

    def vision_objects(self, desk_id: int, camera: str = "", label: str = "", since: float = 0, status: str = "",
                       watch: bool = False, limit: int = 300, emb: bool = False, name_id: int = 0,
                       named: bool = False) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM vision_objects WHERE desk_id=? AND last_ts>=?", [desk_id, since]
        for col, val in (("camera", camera), ("label", label), ("status", status)):
            if val:
                sql += f" AND {col}=?"; args.append(val)
        if watch:
            sql += " AND watch=1"
        if name_id:
            sql += " AND name_id=?"; args.append(int(name_id))
        elif named:
            sql += " AND name_id>0"
        sql += " ORDER BY last_ts DESC LIMIT ?"; args.append(int(limit))
        return [self._orow(r, emb) for r in _rows(self._conn.execute(sql, args))]

    def close_vision_objects(self, desk_id: int, camera: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE vision_objects SET status='gone' WHERE desk_id=? AND camera=? AND status='active'", (desk_id, camera))
            self._conn.commit()

    def add_object_note(self, object_id: int, desk_id: int, kind: str, text: str, question: str = "", crop: str = "") -> int:
        with self._lock:
            cur = self._conn.execute("INSERT INTO vision_object_notes(object_id,desk_id,ts,kind,question,text,crop) VALUES(?,?,?,?,?,?,?)",
                                     (object_id, desk_id, time.time(), kind, question, text, crop))
            self._conn.commit()
            return int(cur.lastrowid)

    def object_notes(self, object_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return _rows(self._conn.execute("SELECT * FROM vision_object_notes WHERE object_id=? ORDER BY ts DESC LIMIT ?", (object_id, int(limit))))

    # ------------------------------------------------------------------ vision vectors (RAG index)
    def put_vision_vector(self, event_id: int, desk_id: int, ts: float, model: str, dim: int,
                          text_vec: bytes | None, image_vec: bytes | None) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM vision_vectors WHERE event_id=?", (event_id,))
            self._conn.execute("INSERT INTO vision_vectors(event_id,desk_id,ts,model,dim,text_vec,image_vec) VALUES(?,?,?,?,?,?,?)",
                               (event_id, desk_id, ts, model, dim, text_vec, image_vec))
            self._conn.commit()

    def vision_vectors(self, desk_id: int, since: float = 0, model: str = "", limit: int = 5000) -> list[dict[str, Any]]:
        sql = "SELECT event_id, ts, model, dim, text_vec, image_vec FROM vision_vectors WHERE desk_id=? AND ts>=?"
        args: list[Any] = [desk_id, since]
        if model:
            sql += " AND model=?"; args.append(model)
        sql += " ORDER BY ts DESC LIMIT ?"; args.append(limit)
        return _rows(self._conn.execute(sql, args))

    def vision_vector_count(self, desk_id: int, model: str = "") -> int:
        sql = "SELECT COUNT(*) FROM vision_vectors WHERE desk_id=?"
        args: list[Any] = [desk_id]
        if model:
            sql += " AND model=?"; args.append(model)
        return int(self._conn.execute(sql, args).fetchone()[0])

    def unindexed_vision_events(self, desk_id: int, model: str, limit: int = 32) -> list[dict[str, Any]]:
        """Newest events on the desk that have no vector row for `model`."""
        rows = _rows(self._conn.execute(
            "SELECT e.* FROM vision_events e LEFT JOIN vision_vectors v ON v.event_id=e.id AND v.model=? "
            "WHERE e.desk_id=? AND v.event_id IS NULL ORDER BY e.ts DESC LIMIT ?", (model, desk_id, limit)))
        return [self._vrow(r) for r in rows]

    def update_vision_event_reason(self, vid: int, reason: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE vision_events SET reason=? WHERE id=?", (reason[:300], vid))
            self._conn.commit()

    def vision_events_by_ids(self, ids: list[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        q = ",".join("?" for _ in ids)
        return [self._vrow(r) for r in _rows(self._conn.execute(f"SELECT * FROM vision_events WHERE id IN ({q})", list(ids)))]

    def vision_event(self, vid: int) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT * FROM vision_events WHERE id=?", (vid,)))
        return self._vrow(rows[0]) if rows else None

    @staticmethod
    def _vrow(r: dict[str, Any]) -> dict[str, Any]:
        try:
            r["counts"] = json.loads(r.get("counts") or "{}")
        except Exception:
            r["counts"] = {}
        return r

    def vision_events(self, desk_id: int, camera: str = "", since: float = 0, query: str = "", limit: int = 100,
                      triggered_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM vision_events WHERE desk_id=? AND ts>=?"
        args: list[Any] = [desk_id, since]
        if camera:
            sql += " AND camera=?"; args.append(camera)
        if triggered_only:
            sql += " AND triggered=1"
        if query:
            q = f"%{query.strip()}%"
            sql += " AND (camera LIKE ? OR counts LIKE ? OR reason LIKE ? OR answer LIKE ? OR question LIKE ?)"
            args += [q, q, q, q, q]
        sql += " ORDER BY ts DESC LIMIT ?"; args.append(limit)
        return [self._vrow(r) for r in _rows(self._conn.execute(sql, args))]

    def hook_cameras(self, desk_id: int, since: float, exclude: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Cameras that report through /hook/<token>/vision (vision nodes, PIR sensors, NVRs) - they are not connectors,
        so the Cameras page learns about them from the log: per camera the last event plus 24h counts."""
        rows = _rows(self._conn.execute(
            "SELECT camera, COUNT(*) AS n, SUM(triggered) AS alerts, MAX(ts) AS last_ts, MAX(backend) AS backend FROM vision_events "
            "WHERE desk_id=? AND source='hook' AND ts>=? GROUP BY camera ORDER BY MAX(ts) DESC", (desk_id, since)))
        out = []
        for r in rows:
            if r["camera"] in exclude:
                continue
            last = self.last_vision_event(desk_id, r["camera"])
            out.append({"name": r["camera"], "events": int(r["n"] or 0), "alerts": int(r["alerts"] or 0), "backend": r["backend"] or "external",
                        "last_ts": float(r["last_ts"] or 0), "last_event": last})
        return out

    def last_vision_event(self, desk_id: int, camera: str, triggered_only: bool = False) -> dict[str, Any] | None:
        rows = self.vision_events(desk_id, camera, 0, "", 1, triggered_only)
        return rows[0] if rows else None

    def set_vision_run(self, vid: int, run_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE vision_events SET run_id=?, triggered=1 WHERE id=?", (run_id, vid))
            self._conn.commit()

    # ------------------------------------------------------------------ named things (the owner's registry)
    def add_named(self, desk_id: int, name: str, kind: str = "object", label: str = "", notes: str = "",
                  exemplars: list | None = None, emb: bytes | None = None, dim: int = 0) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute("INSERT INTO named_things(desk_id,name,kind,label,notes,created,exemplars,emb,dim,sightings) "
                                     "VALUES(?,?,?,?,?,?,?,?,?,0)",
                                     (desk_id, name.strip(), kind, label, notes, time.time(), json.dumps(exemplars or []), emb, dim))
            self._conn.commit()
            return self.named(int(cur.lastrowid), emb=True)

    def _nrow(self, r: dict[str, Any], emb: bool) -> dict[str, Any]:
        try:
            r["exemplars"] = json.loads(r.get("exemplars") or "[]")
        except (TypeError, json.JSONDecodeError):
            r["exemplars"] = []
        if not emb:
            r.pop("emb", None)
        elif r.get("emb") is not None:
            r["emb"] = bytes(r["emb"])
        return r

    def named(self, nid: int, emb: bool = False) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT * FROM named_things WHERE id=?", (nid,)))
        return self._nrow(rows[0], emb) if rows else None

    def named_things(self, desk_id: int, emb: bool = False) -> list[dict[str, Any]]:
        return [self._nrow(r, emb) for r in _rows(self._conn.execute(
            "SELECT * FROM named_things WHERE desk_id=? ORDER BY name COLLATE NOCASE", (desk_id,)))]

    def named_by_name(self, desk_id: int, name: str) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT * FROM named_things WHERE desk_id=? AND lower(name)=lower(?)", (desk_id, name.strip())))
        return self._nrow(rows[0], False) if rows else None

    def update_named(self, nid: int, **f: Any) -> None:
        if not f:
            return
        if "exemplars" in f:
            f["exemplars"] = json.dumps(f["exemplars"])
        with self._lock:
            self._conn.execute(f"UPDATE named_things SET {', '.join(k + '=?' for k in f)} WHERE id=?", list(f.values()) + [nid])
            self._conn.commit()

    def delete_named(self, nid: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE vision_objects SET name_id=0, name_score=0, name_by='' WHERE name_id=?", (nid,))
            self._conn.execute("DELETE FROM named_things WHERE id=?", (nid,))
            self._conn.commit()

    def vision_stats(self, desk_id: int, since: float) -> dict[str, Any]:
        row = self._conn.execute("SELECT COUNT(*), SUM(triggered) FROM vision_events WHERE desk_id=? AND ts>=?", (desk_id, since)).fetchone()
        return {"events": int(row[0] or 0), "triggered": int(row[1] or 0)}

    # ------------------------------------------------------------------ security log (sec_events, sec_detections)
    # ts = the ORIGINAL event time (a replay keeps it); ingested = when the row arrived. Every field is log data.
    @staticmethod
    def _sec_values(desk_id: int, ev: dict[str, Any], origin: str, now: float) -> tuple:
        ts = _num(ev.get("ts"))
        lim = SEC_FIELD_LIMITS
        attrs = "{}"
        if isinstance(ev.get("attrs"), dict):
            try:
                attrs = json.dumps(ev["attrs"], separators=(",", ":"), default=str)
            except (TypeError, ValueError):
                attrs = "{}"
            if len(attrs) > SEC_ATTRS_MAX:
                attrs = "{}"
        return (desk_id, now if ts is None else ts, now, _clip(ev.get("source"), lim["source"]), _clip(ev.get("sensor"), lim["sensor"]),
                _clip(ev.get("kind"), lim["kind"]), _clip(ev.get("src"), lim["src"]), _clip(ev.get("dst"), lim["dst"]),
                _clip(ev.get("user"), lim["user"]), _clip(ev.get("sig"), lim["sig"]),
                ev.get("severity") if ev.get("severity") in SEVERITIES else "info", _clip(ev.get("message"), lim["message"]),
                _clip(ev.get("raw"), lim["raw"]), attrs, _clip(origin or ev.get("origin"), lim["origin"]), 0, "")

    def add_sec_events(self, desk_id: int, events: list[dict[str, Any]], origin: str = "") -> list[int]:
        """Insert parsed log events (one lock, one commit). Returns the new ids in input order. An entry that is not
        a dict is not an event: it is skipped (no row, no id), never stored as an empty event."""
        now = time.time()
        rows = [self._sec_values(desk_id, ev, origin, now) for ev in events or [] if isinstance(ev, dict)]
        ids: list[int] = []
        if not rows:
            return ids
        sql = ("INSERT INTO sec_events(desk_id,ts,ingested,source,sensor,kind,src,dst,username,sig,severity,message,raw,attrs,"
               "origin,triggered,run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        with self._lock:
            try:
                for r in rows:
                    ids.append(int(self._conn.execute(sql, r).lastrowid))
            except Exception:
                if self.backend == "sqlite":           # never leave half a batch for the next writer's commit
                    self._conn.rollback()
                raise
            self._conn.commit()
        return ids

    def add_sec_event(self, desk_id: int, event: dict[str, Any], origin: str = "") -> int:
        if not isinstance(event, dict):
            raise ValueError("a security event must be a dict")
        return self.add_sec_events(desk_id, [event], origin)[0]

    @staticmethod
    def _sec_where(desk_id: int, *, since: float | None = None, until: float | None = None, after_id: int = 0,
                   ids: list[int] | None = None, sensor: str = "", source: str = "", kind: str | list[str] = "",
                   exclude_kinds: list[str] | tuple = (), src: str = "", dst: str = "", ip: str = "", user: str = "",
                   sig: str = "", min_severity: str = "", triggered: bool | None = None) -> tuple[str, list[Any]]:
        w, a = ["desk_id=?"], [desk_id]
        if since is not None:
            w.append("ts>=?"); a.append(float(since))
        if until is not None:
            w.append("ts<=?"); a.append(float(until))
        if after_id:
            w.append("id>?"); a.append(int(after_id))
        if ids is not None:                           # one chunk (<= 500) from _id_chunks
            w.append(f"id IN ({','.join('?' * len(ids))})"); a.extend(ids)
        for col, val in (("sensor", sensor), ("source", source), ("src", src), ("dst", dst), ("username", user)):
            if val:
                w.append(f"{col}=?"); a.append(str(val))
        kinds = [k for k in ([kind] if isinstance(kind, str) else list(kind or [])) if k]
        if kinds:
            w.append(f"kind IN ({','.join('?' * len(kinds))})"); a.extend(str(k) for k in kinds)
        ex = [k for k in (exclude_kinds or ()) if k]
        if ex:
            w.append(f"kind NOT IN ({','.join('?' * len(ex))})"); a.extend(str(k) for k in ex)
        if ip:
            w.append("(src=? OR dst=?)"); a += [str(ip), str(ip)]
        if sig:
            w.append("LOWER(sig) LIKE LOWER(?)"); a.append(f"%{sig}%")
        if min_severity in SEVERITIES:
            sevs = SEVERITIES[SEVERITIES.index(min_severity):]
            w.append(f"severity IN ({','.join('?' * len(sevs))})"); a.extend(sevs)
        if triggered is not None:
            w.append("triggered=?"); a.append(1 if triggered else 0)
        return " AND ".join(w), a

    def sec_events(self, desk_id: int, *, since: float | None = None, until: float | None = None, after_id: int = 0,
                   ids: list[int] | None = None, sensor: str = "", source: str = "", kind: str | list[str] = "",
                   exclude_kinds: list[str] | tuple = (), src: str = "", dst: str = "", ip: str = "", user: str = "",
                   sig: str = "", min_severity: str = "", triggered: bool | None = None, order: str = "asc",
                   limit: int = 500, with_raw: bool = False) -> list[dict[str, Any]]:
        """Desk-scoped log rows. order: asc = (ts, id), desc = (ts, id) newest first, id = arrival order."""
        filters = dict(since=since, until=until, after_id=after_id, sensor=sensor, source=source, kind=kind,
                       exclude_kinds=exclude_kinds, src=src, dst=dst, ip=ip, user=user, sig=sig,
                       min_severity=min_severity, triggered=triggered)
        limit = max(1, min(int(500 if limit is None else limit), 300000))
        order_sql = {"desc": "ts DESC, id DESC", "id": "id"}.get(order, "ts, id")
        cols = _SEC_COLS + (",raw" if with_raw else "")
        chunks = _id_chunks(ids)
        rows: list[dict[str, Any]] = []
        for chunk in chunks:
            w, a = self._sec_where(desk_id, ids=chunk, **filters)
            rows += _rows(self._conn.execute(f"SELECT {cols} FROM sec_events WHERE {w} ORDER BY {order_sql} LIMIT ?", a + [limit]))
        if len(chunks) > 1:                           # several IN chunks: merge in the requested order, then cut
            if order == "id":
                rows.sort(key=lambda r: r["id"])
            else:
                rows.sort(key=lambda r: (r["ts"], r["id"]), reverse=(order == "desc"))
            rows = rows[:limit]
        return [_sec_row(r) for r in rows]

    def sec_event_stats(self, desk_id: int, **filters) -> dict[str, Any]:
        """{"total", "first_ts", "last_ts", "max_id"} over the same filters as sec_events."""
        for k in ("order", "limit", "with_raw"):
            filters.pop(k, None)
        ids = filters.pop("ids", None)
        total, first, last, max_id = 0, None, None, 0
        for chunk in _id_chunks(ids):
            w, a = self._sec_where(desk_id, ids=chunk, **filters)
            n, lo, hi, mx = self._conn.execute(f"SELECT COUNT(*), MIN(ts), MAX(ts), MAX(id) FROM sec_events WHERE {w}", a).fetchone()
            if not n:
                continue
            total += int(n)
            first = lo if first is None else min(first, lo)
            last = hi if last is None else max(last, hi)
            max_id = max(max_id, int(mx or 0))
        return {"total": total, "first_ts": first, "last_ts": last, "max_id": max_id}

    def sec_event_counts(self, desk_id: int, field: str, *, limit: int = 20, **filters) -> list[tuple[str, int]]:
        """Top values of one field with their counts (empty values excluded), most frequent first."""
        col = _SEC_GROUP_COLS.get(field)
        if not col:
            raise ValueError(f"cannot count by {field!r}: use one of {', '.join(_SEC_GROUP_COLS)}")
        for k in ("order", "with_raw"):
            filters.pop(k, None)
        limit = max(1, min(int(limit or 20), 10000))
        chunks = _id_chunks(filters.pop("ids", None))
        merged: dict[str, int] = {}
        for chunk in chunks:
            w, a = self._sec_where(desk_id, ids=chunk, **filters)
            sql = f"SELECT {col}, COUNT(*) FROM sec_events WHERE {w} AND {col}<>'' GROUP BY {col}"
            if len(chunks) == 1:
                sql += f" ORDER BY COUNT(*) DESC, {col} LIMIT ?"
                a = a + [limit]
            for v, n in self._conn.execute(sql, a).fetchall():
                merged[v] = merged.get(v, 0) + int(n)
        return sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    def sec_event_points(self, desk_id: int, since: float, until: float, *, limit: int = 300000) -> list[tuple]:
        """(ts, sensor, severity, source) in time order: the timeline's input, nothing else."""
        rows = self._conn.execute("SELECT ts, sensor, severity, source FROM sec_events WHERE desk_id=? AND ts>=? AND ts<=? "
                                  "ORDER BY ts, id LIMIT ?", (desk_id, float(since), float(until), max(1, int(limit)))).fetchall()
        return [tuple(r) for r in rows]

    def sec_sensors(self, desk_id: int) -> list[dict[str, Any]]:
        rows = _rows(self._conn.execute(
            "SELECT sensor, source, COUNT(*) AS events, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM sec_events "
            "WHERE desk_id=? GROUP BY sensor, source ORDER BY MIN(ts), sensor, source", (desk_id,)))
        return [{"sensor": r["sensor"] or "", "source": r["source"] or "", "events": int(r["events"] or 0),
                 "first_ts": r["first_ts"], "last_ts": r["last_ts"]} for r in rows]

    def sec_events_by_ids(self, desk_id: int, ids: list[int], *, with_raw: bool = False) -> list[dict[str, Any]]:
        """Rows of THIS desk only, in (ts, id) order: an id from another desk simply is not there."""
        ids = _as_ids(ids)
        if not ids:
            return []
        return self.sec_events(desk_id, ids=ids, order="asc", limit=len(ids), with_raw=with_raw)

    def mark_sec_events(self, desk_id: int, ids: list[int], run_id: str) -> None:
        chunks = [c for c in _id_chunks(ids) if c]
        if not chunks:
            return
        with self._lock:
            for chunk in chunks:
                self._conn.execute(f"UPDATE sec_events SET triggered=1, run_id=? WHERE desk_id=? AND id IN ({','.join('?' * len(chunk))})",
                                   [str(run_id or ""), desk_id, *chunk])
            self._conn.commit()

    def sec_detection(self, desk_id: int, det_id: str) -> dict[str, Any] | None:
        rows = _rows(self._conn.execute("SELECT * FROM sec_detections WHERE desk_id=? AND det_id=? ORDER BY id LIMIT 1",
                                        (desk_id, str(det_id))))
        return _det_row(rows[0]) if rows else None

    def upsert_sec_detection(self, desk_id: int, det: dict[str, Any]) -> tuple[dict[str, Any], bool, bool]:
        """Insert or update one detection (key: desk + det id). Returns (row, is_new, escalated). run_id and created
        are never touched by an update: a detection that already started a run keeps it."""
        det_id = str(det.get("id") or "").strip()[:512]
        if not det_id:
            raise ValueError("detection has no id")
        sev = det.get("severity") if det.get("severity") in SEVERITIES else "low"
        vals: dict[str, Any] = {"rule": _clip(det.get("rule"), 40), "severity": sev, "title": _clip(det.get("title"), 200),
                                "first_ts": _num(det.get("first_ts")), "last_ts": _num(det.get("last_ts")),
                                "evidence_total": int(_num(det.get("evidence_total")) or 0)}
        for k, empty in _DET_JSON.items():
            v = det.get(k)
            vals[k] = json.dumps(v if isinstance(v, type(empty)) else empty, separators=(",", ":"), default=str)
        now = time.time()
        with self._lock:
            row = self._conn.execute("SELECT id, severity FROM sec_detections WHERE desk_id=? AND det_id=? ORDER BY id LIMIT 1",
                                     (desk_id, det_id)).fetchone()
            if row is None:
                cols = ["desk_id", "det_id", *vals, "run_id", "created", "updated"]
                cur = self._conn.execute(f"INSERT INTO sec_detections({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                                         [desk_id, det_id, *vals.values(), "", now, now])
                rid, is_new, escalated = int(cur.lastrowid), True, False
            else:
                rid, old = int(row[0]), row[1]
                self._conn.execute(f"UPDATE sec_detections SET {','.join(k + '=?' for k in vals)}, updated=? WHERE id=?",
                                   [*vals.values(), now, rid])
                is_new = False
                escalated = SEVERITIES.index(sev) > (SEVERITIES.index(old) if old in SEVERITIES else 0)
            self._conn.commit()
        return _det_row(_rows(self._conn.execute("SELECT * FROM sec_detections WHERE id=?", (rid,)))[0]), is_new, escalated

    def sec_detections(self, desk_id: int, *, since: float | None = None, until: float | None = None, min_severity: str = "",
                       rule: str = "", pending_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        """Detections overlapping [since, until], most severe first, then the biggest, then the newest."""
        w, a = ["desk_id=?"], [desk_id]
        if since is not None:
            w.append("last_ts>=?"); a.append(float(since))
        if until is not None:
            w.append("first_ts<=?"); a.append(float(until))
        if min_severity in SEVERITIES:
            sevs = SEVERITIES[SEVERITIES.index(min_severity):]
            w.append(f"severity IN ({','.join('?' * len(sevs))})"); a.extend(sevs)
        if rule:
            w.append("rule=?"); a.append(str(rule))
        if pending_only:
            w.append("run_id=''")
        rows = _rows(self._conn.execute(
            f"SELECT * FROM sec_detections WHERE {' AND '.join(w)} ORDER BY {_SEV_ORDER} DESC, evidence_total DESC, "
            "last_ts DESC, det_id LIMIT ?", a + [max(1, min(int(limit or 100), 5000))]))
        return [_det_row(r) for r in rows]

    def set_sec_detection_run(self, desk_id: int, det_ids: list[str], run_id: str) -> None:
        ids = list(dict.fromkeys(str(d) for d in det_ids or [] if str(d or "").strip()))
        if not ids:
            return
        with self._lock:
            for i in range(0, len(ids), _ID_CHUNK):
                chunk = ids[i:i + _ID_CHUNK]
                self._conn.execute(f"UPDATE sec_detections SET run_id=? WHERE desk_id=? AND det_id IN ({','.join('?' * len(chunk))})",
                                   [str(run_id or ""), desk_id, *chunk])
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
            for t in ("runs", "actions", "leads", "contacts", "jobs", "memories", "vision_events", "sec_events", "sec_detections",
                      "records", "record_links", "timeline", "cases", "case_records"):
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

    def add_event(self, run_id: str, kind: str, agent: str, text: str, data: str = "") -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events(run_id,ts,kind,agent,text,data) VALUES(?,?,?,?,?,?)", (run_id, time.time(), kind, agent, text, data or ""))
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

    @staticmethod
    def message_key(addr: str) -> str:
        """One key per customer across channels: a lowercased email, or '+' and the digits of a phone number."""
        a = (addr or "").strip()
        if a.lower().startswith("ig:"):                  # an Instagram-scoped sender id keeps its own namespace
            return a.lower()
        if "@" in a:
            return a.lower()
        digits = "".join(ch for ch in a if ch.isdigit())
        return ("+" + digits) if digits else a.lower()

    def contact_for(self, addr: str, desk_id: int = 1) -> dict[str, Any] | None:
        """The contact an address belongs to: email matched exactly, phones matched on their digits."""
        a = (addr or "").strip()
        if not a:
            return None
        if "@" in a:
            rows = _rows(self._conn.execute("SELECT * FROM contacts WHERE desk_id=? AND lower(email)=lower(?) LIMIT 1", (desk_id, a)))
            return rows[0] if rows else None
        digits = "".join(ch for ch in a if ch.isdigit())
        if not digits:
            return None
        for c in _rows(self._conn.execute("SELECT * FROM contacts WHERE desk_id=? AND phone IS NOT NULL AND phone != ''", (desk_id,))):
            if "".join(ch for ch in str(c["phone"]) if ch.isdigit()) == digits:
                return c
        return None

    def add_message(self, channel: str, direction: str, addr: str, body: str, subject: str = "", actor: str = "",
                    status: str = "sent", run_id: str = "", action_id: int = 0, desk_id: int = 1, ts: float | None = None) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages(desk_id,contact_key,channel,dir,addr,actor,subject,body,status,ts,run_id,action_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (desk_id, self.message_key(addr), channel, direction, (addr or "").strip(), actor or "", subject or "",
                 (body or "")[:8000], status, ts if ts is not None else time.time(), run_id or "", int(action_id or 0)))
            mid = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._conn.commit()
        return int(mid)

    def messages(self, contact_key: str, limit: int = 300, desk_id: int = 1) -> list[dict[str, Any]]:
        return _rows(self._conn.execute("SELECT * FROM messages WHERE desk_id=? AND contact_key=? ORDER BY ts DESC, id DESC LIMIT ?",
                                        (desk_id, contact_key, limit)))[::-1]

    def message_threads(self, desk_id: int = 1, limit: int = 120) -> list[dict[str, Any]]:
        """Newest message per customer, with the conversation's size and channels."""
        rows = _rows(self._conn.execute(
            "SELECT * FROM messages WHERE desk_id=? ORDER BY ts DESC, id DESC LIMIT 4000", (desk_id,)))
        out: dict[str, dict[str, Any]] = {}
        for m in rows:
            t = out.get(m["contact_key"])
            if not t:
                if len(out) >= limit:
                    continue
                out[m["contact_key"]] = t = {**m, "count": 0, "channels": [], "last_in_ts": 0.0}
            t["count"] += 1
            if m["channel"] not in t["channels"]:
                t["channels"].append(m["channel"])
            if m["dir"] == "in" and m["ts"] > t["last_in_ts"]:
                t["last_in_ts"] = m["ts"]
        return list(out.values())

    def message_count(self, desk_id: int = 1) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM messages WHERE desk_id=?", (desk_id,)).fetchone()[0])

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
        row = _rows(self._conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)))[0]
        try:                                                  # every CRM contact is also a person record (atlas/records.py)
            from . import records as _R
            _R.mirror_contact(self, desk_id, row)
        except Exception:
            pass
        return row

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

    def tag_action_case(self, aid: int, case_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE actions SET case_id=? WHERE id=?", (int(case_id or 0), aid))
            self._conn.commit()

    def set_run_case(self, run_id: str, case_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE runs SET case_id=? WHERE id=?", (int(case_id or 0), run_id))
            self._conn.commit()

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
    def contact_for(self, addr): return self.s.contact_for(addr, self.desk_id)
    def add_message(self, channel, direction, addr, body, **k): return self.s.add_message(channel, direction, addr, body, desk_id=self.desk_id, **k)
    def messages(self, contact_key, limit=300): return self.s.messages(contact_key, limit, self.desk_id)
    def message_threads(self, limit=120): return self.s.message_threads(self.desk_id, limit)
    def message_count(self): return self.s.message_count(self.desk_id)
    def add_action(self, run_id, agent, kind, to, subject, body, reason, flags=""):
        return self.s.add_action(run_id, agent, kind, to, subject, body, reason, self.desk_id, flags)
    def actions(self, status="", limit=200): return self.s.actions(status, limit, self.desk_id)
    def action(self, aid): return self.s.action(aid)
    def decide_action(self, *a, **k): return self.s.decide_action(*a, **k)
    def tag_action_case(self, aid, case_id): return self.s.tag_action_case(aid, case_id)
    def set_run_case(self, run_id, case_id): return self.s.set_run_case(run_id, case_id)
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
    def add_vision_event(self, camera, counts, **k): return self.s.add_vision_event(self.desk_id, camera, counts, **k)
    def vision_events(self, camera="", since=0, query="", limit=100, triggered_only=False):
        return self.s.vision_events(self.desk_id, camera, since, query, limit, triggered_only)
    def vision_event(self, vid): return self.s.vision_event(vid)
    def put_vision_vector(self, *a, **k): return self.s.put_vision_vector(*a, **k)
    def vision_vectors(self, since=0, model="", limit=5000): return self.s.vision_vectors(self.desk_id, since, model, limit)
    def vision_vector_count(self, model=""): return self.s.vision_vector_count(self.desk_id, model)
    def unindexed_vision_events(self, model, limit=32): return self.s.unindexed_vision_events(self.desk_id, model, limit)
    def vision_events_by_ids(self, ids): return self.s.vision_events_by_ids(ids)
    def update_vision_event_reason(self, vid, reason): return self.s.update_vision_event_reason(vid, reason)
    def last_vision_event(self, camera, triggered_only=False): return self.s.last_vision_event(self.desk_id, camera, triggered_only)
    def hook_cameras(self, since, exclude=()): return self.s.hook_cameras(self.desk_id, since, exclude)
    def set_vision_run(self, vid, run_id): return self.s.set_vision_run(vid, run_id)
    def add_vision_object(self, camera, **f): return self.s.add_vision_object(self.desk_id, camera, **f)
    def update_vision_object(self, oid, **f): return self.s.update_vision_object(oid, **f)
    def vision_object(self, oid, emb=False):
        o = self.s.vision_object(oid, emb)
        return o if o and o["desk_id"] == self.desk_id else None
    def vision_objects(self, **k): return self.s.vision_objects(self.desk_id, **k)
    def close_vision_objects(self, camera): return self.s.close_vision_objects(self.desk_id, camera)
    def add_object_note(self, object_id, kind, text, question="", crop=""): return self.s.add_object_note(object_id, self.desk_id, kind, text, question, crop)
    def object_notes(self, object_id, limit=100): return self.s.object_notes(object_id, limit)
    def vision_stats(self, since): return self.s.vision_stats(self.desk_id, since)
    # security log
    def add_sec_events(self, events, origin=""): return self.s.add_sec_events(self.desk_id, events, origin)
    def add_sec_event(self, event, origin=""): return self.s.add_sec_event(self.desk_id, event, origin)
    def sec_events(self, **k): return self.s.sec_events(self.desk_id, **k)
    def sec_event_stats(self, **k): return self.s.sec_event_stats(self.desk_id, **k)
    def sec_event_counts(self, field, **k): return self.s.sec_event_counts(self.desk_id, field, **k)
    def sec_event_points(self, since, until, **k): return self.s.sec_event_points(self.desk_id, since, until, **k)
    def sec_sensors(self): return self.s.sec_sensors(self.desk_id)
    def sec_events_by_ids(self, ids, **k): return self.s.sec_events_by_ids(self.desk_id, ids, **k)
    def mark_sec_events(self, ids, run_id): return self.s.mark_sec_events(self.desk_id, ids, run_id)
    def sec_detection(self, det_id): return self.s.sec_detection(self.desk_id, det_id)
    def upsert_sec_detection(self, det): return self.s.upsert_sec_detection(self.desk_id, det)
    def sec_detections(self, **k): return self.s.sec_detections(self.desk_id, **k)
    def set_sec_detection_run(self, det_ids, run_id): return self.s.set_sec_detection_run(self.desk_id, det_ids, run_id)
    def add_named(self, name, **f): return self.s.add_named(self.desk_id, name, **f)
    def named(self, nid, emb=False):
        n = self.s.named(nid, emb)
        return n if n and n["desk_id"] == self.desk_id else None
    def named_things(self, emb=False): return self.s.named_things(self.desk_id, emb)
    def named_by_name(self, name): return self.s.named_by_name(self.desk_id, name)
    def update_named(self, nid, **f): return self.s.update_named(nid, **f)
    def delete_named(self, nid): return self.s.delete_named(nid)
