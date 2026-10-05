"""Records: the desk's live model of the business (people, organisations, bookings, orders, cameras...).

Agents used to start every run nearly blank - a CRM row, a few remembered facts, a camera log. Records give them a
shared, typed picture of the business that every run reads from and writes to:

  records       one row per real thing: type + key (its natural id: an email, a booking ref, a camera name) + props
  record_links  typed edges between records (person works_at organisation, booking for person, incident seen_on camera)
  timeline      everything that happened to a record or a case, in order: messages sent, replies, notes, state changes

Records are kept in sync with what the desk already knows (CRM contacts, camera connectors) so they are real from the
first day, and agents add the rest with the record_* tools. Cases (atlas/cases.py) hang off records.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER, type TEXT, key TEXT, title TEXT DEFAULT '',
  props TEXT DEFAULT '{}', status TEXT DEFAULT 'active', source TEXT DEFAULT '', created REAL, updated REAL
);
CREATE INDEX IF NOT EXISTS ix_records_desk ON records(desk_id, type, key);
CREATE TABLE IF NOT EXISTS record_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER, src INTEGER, rel TEXT, dst INTEGER, source TEXT DEFAULT '', created REAL
);
CREATE INDEX IF NOT EXISTS ix_rlinks_src ON record_links(desk_id, src);
CREATE INDEX IF NOT EXISTS ix_rlinks_dst ON record_links(desk_id, dst);
CREATE TABLE IF NOT EXISTS timeline (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER, ts REAL, record_id INTEGER DEFAULT 0, case_id INTEGER DEFAULT 0,
  kind TEXT DEFAULT 'note', actor TEXT DEFAULT '', text TEXT DEFAULT '', data TEXT DEFAULT '{}', run_id TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_timeline_rec ON timeline(desk_id, record_id, ts);
CREATE INDEX IF NOT EXISTS ix_timeline_case ON timeline(desk_id, case_id, ts);
"""
TABLES = ("records", "record_links", "timeline")

# Built-in types. `key` = which props identify one (first non-empty wins); anything else an agent invents becomes a
# custom type with free-form props. Labels are what the Operations page shows.
TYPES: dict[str, dict[str, Any]] = {
    "person":       {"label": "Person", "plural": "People", "key": ["email", "phone", "name"],
                     "props": ["name", "email", "phone", "company", "stage", "notes"]},
    "organisation": {"label": "Organisation", "plural": "Organisations", "key": ["name", "website"],
                     "props": ["name", "website", "sector", "notes"]},
    "booking":      {"label": "Booking", "plural": "Bookings", "key": ["ref", "when"],
                     "props": ["ref", "when", "service", "status", "price", "notes"]},
    "order":        {"label": "Order", "plural": "Orders", "key": ["ref", "number"],
                     "props": ["ref", "items", "total", "status", "placed", "notes"]},
    "invoice":      {"label": "Invoice", "plural": "Invoices", "key": ["number", "ref"],
                     "props": ["number", "amount", "due", "status", "customer", "notes"]},
    "product":      {"label": "Product", "plural": "Products", "key": ["sku", "name"],
                     "props": ["name", "sku", "price", "stock", "notes"]},
    "place":        {"label": "Place", "plural": "Places", "key": ["name", "address"],
                     "props": ["name", "address", "notes"]},
    "camera":       {"label": "Camera", "plural": "Cameras", "key": ["name"],
                     "props": ["name", "notes", "focus"]},
}
TITLE_PROPS = ("name", "title", "ref", "number", "sku", "email", "phone", "when")
MAX_PROPS = 40
MAX_VALUE = 2000


def _rows(cur) -> list[dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _base(store) -> tuple[Any, int | None]:
    """(Store, desk_id) from a Store or a DeskStore view."""
    if hasattr(store, "s") and hasattr(store, "desk_id"):
        return store.s, store.desk_id
    return store, None


def slug(s: str, n: int = 32) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(s or "").strip().lower()).strip("_")[:n]


def _digits(phone: str) -> str:
    d = re.sub(r"\D", "", str(phone or ""))
    if d.startswith("00"):
        d = d[2:]
    elif str(phone or "").strip().startswith("0") and len(d) == 11:
        d = "44" + d[1:]
    return d


def norm_key(rtype: str, key: str) -> str:
    """One spelling per real thing: emails lower-cased, phones as digits, everything else trimmed + lower-cased."""
    k = str(key or "").strip()
    if not k:
        return ""
    if "@" in k:
        return k.lower()
    if re.fullmatch(r"[+()\d\s.-]{7,}", k):
        return "+" + _digits(k)
    return re.sub(r"\s+", " ", k).lower()[:200]


def derive_key(rtype: str, props: dict[str, Any], title: str = "") -> str:
    spec = TYPES.get(rtype) or {}
    for p in list(spec.get("key") or []) + ["key", "id", "ref", "name", "title", "email", "phone"]:
        v = props.get(p)
        if v not in (None, "") and not isinstance(v, (dict, list)):
            return norm_key(rtype, str(v))
    return norm_key(rtype, title)


def derive_title(rtype: str, props: dict[str, Any], key: str) -> str:
    for p in TITLE_PROPS:
        v = props.get(p)
        if v not in (None, "") and not isinstance(v, (dict, list)):
            return str(v)[:160]
    return key[:160]


def _clean_props(props: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(props, dict):
        return out
    for k, v in list(props.items())[:MAX_PROPS]:
        name = slug(k, 40)
        if not name:
            continue
        if isinstance(v, str):
            v = v.strip()[:MAX_VALUE]
            if name == "email" and "@" in v:
                v = v.lower()                            # one spelling per address, or every CRM sync looks like a change
            elif name == "phone" and re.fullmatch(r"[+()\d\s.-]{7,}", v):
                v = "+" + _digits(v)                     # +447700900123: replies by SMS/WhatsApp match the same person
        elif isinstance(v, (dict, list)):
            v = json.loads(json.dumps(v, ensure_ascii=False)[:MAX_VALUE]) if len(json.dumps(v)) <= MAX_VALUE else str(v)[:MAX_VALUE]
        elif not isinstance(v, (int, float, bool)) and v is not None:
            v = str(v)[:MAX_VALUE]
        out[name] = v
    return out


def _decode(r: dict[str, Any]) -> dict[str, Any]:
    r = dict(r)
    try:
        r["props"] = json.loads(r.get("props") or "{}")
    except (TypeError, json.JSONDecodeError):
        r["props"] = {}
    return r


# ---------------------------------------------------------------------------------------------- records
def get(store, rid: int) -> dict[str, Any] | None:
    s, did = _base(store)
    rows = _rows(s._conn.execute("SELECT * FROM records WHERE id=?", (int(rid),)))
    if not rows or (did is not None and rows[0]["desk_id"] != did):
        return None
    return _decode(rows[0])


def by_key(store, desk_id: int | None, rtype: str, key: str) -> dict[str, Any] | None:
    s, did = _base(store)
    did = did if did is not None else desk_id
    k = norm_key(rtype, key)
    if not k:
        return None
    rows = _rows(s._conn.execute("SELECT * FROM records WHERE desk_id=? AND type=? AND key=? LIMIT 1", (did, slug(rtype), k)))
    return _decode(rows[0]) if rows else None


def upsert(store, desk_id: int | None, rtype: str, props: dict[str, Any] | None = None, key: str = "", title: str = "",
           source: str = "", actor: str = "", run_id: str = "", log: bool = True) -> tuple[dict[str, Any], list[str]]:
    """Create or merge a record. Returns (record, changed prop names). New props are merged over the old ones; an
    empty string clears nothing (agents often send blanks for fields they do not know)."""
    s, did = _base(store)
    did = did if did is not None else int(desk_id or 0)
    rtype = slug(rtype) or "thing"
    props = _clean_props(props or {})
    k = norm_key(rtype, key) if key else derive_key(rtype, props, title)
    if not k:
        raise ValueError(f"a {rtype} record needs a key or an identifying prop ({', '.join((TYPES.get(rtype) or {}).get('key') or ['name'])})")
    now = time.time()
    with s._lock:
        row = s._conn.execute("SELECT id, props, title FROM records WHERE desk_id=? AND type=? AND key=? LIMIT 1", (did, rtype, k)).fetchone()
        if row:
            rid = row[0]
            try:
                old = json.loads(row[1] or "{}")
            except (TypeError, json.JSONDecodeError):
                old = {}
            changed = [p for p, v in props.items() if v not in (None, "") and old.get(p) != v]
            merged = {**old, **{p: v for p, v in props.items() if v not in (None, "")}}
            new_title = (title or "").strip()[:160] or row[2] or derive_title(rtype, merged, k)
            if changed or new_title != row[2]:
                s._conn.execute("UPDATE records SET props=?, title=?, updated=? WHERE id=?",
                                (json.dumps(merged, ensure_ascii=False), new_title, now, rid))
            created = False
        else:
            merged = {p: v for p, v in props.items() if v not in (None, "")}
            new_title = (title or "").strip()[:160] or derive_title(rtype, merged, k)
            cur = s._conn.execute("INSERT INTO records(desk_id,type,key,title,props,source,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                                  (did, rtype, k, new_title, json.dumps(merged, ensure_ascii=False), source, now, now))
            rid = cur.lastrowid or s._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            changed = list(merged)
            created = True
        s._conn.commit()
    if log and (created or changed):
        add_timeline(s, did, record_id=rid, kind="created" if created else "updated", actor=actor or source or "system",
                     text=("created" if created else "updated " + ", ".join(changed[:8])), run_id=run_id,
                     data={"fields": changed[:20]})
    return get(s, rid), changed  # type: ignore[return-value]


def update(store, rid: int, props: dict[str, Any] | None = None, title: str | None = None, status: str | None = None,
           actor: str = "owner") -> dict[str, Any] | None:
    s, _ = _base(store)
    rec = get(store, rid)
    if not rec:
        return None
    merged = dict(rec["props"])
    changed = []
    for p, v in _clean_props(props or {}).items():
        if v in (None, ""):
            if p in merged:
                merged.pop(p)
                changed.append(p)
        elif merged.get(p) != v:
            merged[p] = v
            changed.append(p)
    fields: dict[str, Any] = {"props": json.dumps(merged, ensure_ascii=False), "updated": time.time()}
    if title is not None and title.strip():
        fields["title"] = title.strip()[:160]
    if status in ("active", "archived"):
        fields["status"] = status
    with s._lock:
        s._conn.execute(f"UPDATE records SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), rid))
        s._conn.commit()
    if changed or "title" in fields or "status" in fields:
        add_timeline(s, rec["desk_id"], record_id=rid, kind="updated", actor=actor,
                     text="updated " + ", ".join(changed or [k for k in ("title", "status") if k in fields]))
    return get(store, rid)


def delete(store, rid: int) -> None:
    s, _ = _base(store)
    with s._lock:
        s._conn.execute("DELETE FROM record_links WHERE src=? OR dst=?", (rid, rid))
        s._conn.execute("DELETE FROM records WHERE id=?", (rid,))
        s._conn.commit()


def search(store, desk_id: int | None = None, rtype: str = "", query: str = "", limit: int = 50,
           include_archived: bool = False) -> list[dict[str, Any]]:
    s, did = _base(store)
    did = did if did is not None else desk_id
    where, args = ["desk_id=?"], [did]
    if rtype:
        where.append("type=?"); args.append(slug(rtype))
    if not include_archived:
        where.append("status='active'")
    q = (query or "").strip().lower()
    if q:
        where.append("(LOWER(title) LIKE ? OR LOWER(key) LIKE ? OR LOWER(props) LIKE ?)")
        args += [f"%{q}%"] * 3
    rows = _rows(s._conn.execute(f"SELECT * FROM records WHERE {' AND '.join(where)} ORDER BY updated DESC LIMIT ?",
                                 (*args, max(1, min(int(limit or 50), 500)))))
    return [_decode(r) for r in rows]


def type_counts(store, desk_id: int | None = None) -> dict[str, int]:
    s, did = _base(store)
    did = did if did is not None else desk_id
    return {r[0]: r[1] for r in s._conn.execute(
        "SELECT type, COUNT(*) FROM records WHERE desk_id=? AND status='active' GROUP BY type", (did,)).fetchall()}


def types_for(store, desk_id: int | None = None) -> list[dict[str, Any]]:
    """Built-in types plus every custom type in use, with counts (the Records tab sidebar)."""
    counts = type_counts(store, desk_id)
    out = [{"type": t, "label": spec["label"], "plural": spec["plural"], "props": spec["props"], "count": counts.get(t, 0),
            "builtin": True} for t, spec in TYPES.items()]
    for t, n in sorted(counts.items()):
        if t not in TYPES:
            nice = t.replace("_", " ").title()
            out.append({"type": t, "label": nice, "plural": nice + "s", "props": [], "count": n, "builtin": False})
    return out


# ---------------------------------------------------------------------------------------------- links
def resolve_ref(store, desk_id: int | None, ref: Any) -> dict[str, Any] | None:
    """A record from 12, "#12", "person:jane@x.com" or {"type":..,"key":..}."""
    if isinstance(ref, dict):
        if ref.get("id"):
            return resolve_ref(store, desk_id, ref["id"])
        return by_key(store, desk_id, str(ref.get("type") or ""), str(ref.get("key") or ""))
    if isinstance(ref, int) or (isinstance(ref, str) and re.fullmatch(r"#?\d+", ref.strip())):
        return get(store, int(str(ref).strip().lstrip("#")))
    if isinstance(ref, str) and ":" in ref:
        t, k = ref.split(":", 1)
        return by_key(store, desk_id, t, k)
    return None


def link(store, desk_id: int | None, src: int, rel: str, dst: int, source: str = "", actor: str = "") -> bool:
    """Add src -[rel]-> dst once. Returns True when the edge is new."""
    s, did = _base(store)
    did = did if did is not None else int(desk_id or 0)
    rel = slug(rel, 40) or "related_to"
    if int(src) == int(dst):
        return False
    with s._lock:
        if s._conn.execute("SELECT 1 FROM record_links WHERE desk_id=? AND src=? AND rel=? AND dst=?", (did, src, rel, dst)).fetchone():
            return False
        s._conn.execute("INSERT INTO record_links(desk_id,src,rel,dst,source,created) VALUES(?,?,?,?,?,?)",
                        (did, int(src), rel, int(dst), source, time.time()))
        s._conn.commit()
    if actor:
        dst_rec = get(s, dst)
        add_timeline(s, did, record_id=src, kind="linked", actor=actor,
                     text=f"{rel.replace('_', ' ')} {(dst_rec or {}).get('title') or '#' + str(dst)}", data={"rel": rel, "dst": dst})
    return True


def unlink(store, link_id: int) -> None:
    s, _ = _base(store)
    with s._lock:
        s._conn.execute("DELETE FROM record_links WHERE id=?", (link_id,))
        s._conn.commit()


def links(store, rid: int) -> list[dict[str, Any]]:
    """Both directions, each with the record on the other end."""
    s, _ = _base(store)
    out = []
    for r in _rows(s._conn.execute(
            "SELECT l.id, l.rel, l.src, l.dst, r.id AS oid, r.type, r.title, r.key FROM record_links l JOIN records r ON r.id=l.dst "
            "WHERE l.src=? UNION ALL "
            "SELECT l.id, l.rel, l.src, l.dst, r.id AS oid, r.type, r.title, r.key FROM record_links l JOIN records r ON r.id=l.src "
            "WHERE l.dst=?", (rid, rid))):
        out.append({"id": r["id"], "rel": r["rel"], "direction": "out" if r["src"] == rid else "in",
                    "record": {"id": r["oid"], "type": r["type"], "title": r["title"], "key": r["key"]}})
    return out


def graph(store, rid: int, depth: int = 2, max_nodes: int = 60) -> dict[str, Any]:
    """Neighbourhood of one record for the Records graph view."""
    seen = {int(rid)}
    frontier = [int(rid)]
    edges: dict[int, dict[str, Any]] = {}
    for _ in range(max(1, min(depth, 3))):
        nxt = []
        for node in frontier:
            for l in links(store, node):
                edges[l["id"]] = {"id": l["id"], "rel": l["rel"],
                                  "src": node if l["direction"] == "out" else l["record"]["id"],
                                  "dst": l["record"]["id"] if l["direction"] == "out" else node}
                oid = l["record"]["id"]
                if oid not in seen and len(seen) < max_nodes:
                    seen.add(oid)
                    nxt.append(oid)
        frontier = nxt
    nodes = [r for r in (get(store, n) for n in seen) if r]
    keep = {n["id"] for n in nodes}
    return {"center": int(rid), "nodes": [{"id": n["id"], "type": n["type"], "title": n["title"]} for n in nodes],
            "edges": [e for e in edges.values() if e["src"] in keep and e["dst"] in keep]}


# ---------------------------------------------------------------------------------------------- timeline
def add_timeline(store, desk_id: int | None, record_id: int = 0, case_id: int = 0, kind: str = "note", actor: str = "",
                 text: str = "", data: dict[str, Any] | None = None, run_id: str = "") -> int:
    s, did = _base(store)
    did = did if did is not None else int(desk_id or 0)
    with s._lock:
        cur = s._conn.execute(
            "INSERT INTO timeline(desk_id,ts,record_id,case_id,kind,actor,text,data,run_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (did, time.time(), int(record_id or 0), int(case_id or 0), slug(kind, 24) or "note", (actor or "")[:60],
             (text or "")[:4000], json.dumps(data or {}, ensure_ascii=False)[:8000], run_id or ""))
        tid = cur.lastrowid or s._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        s._conn.commit()
    return tid


def _tl(r: dict[str, Any]) -> dict[str, Any]:
    r = dict(r)
    try:
        r["data"] = json.loads(r.get("data") or "{}")
    except (TypeError, json.JSONDecodeError):
        r["data"] = {}
    return r


def timeline_for(store, record_id: int = 0, case_ids: list[int] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Newest first. A record's timeline also shows what happened on the cases about it."""
    s, _ = _base(store)
    case_ids = [int(c) for c in (case_ids or [])]
    if record_id and case_ids:
        marks = ",".join("?" * len(case_ids))
        q, args = f"SELECT * FROM timeline WHERE record_id=? OR case_id IN ({marks}) ORDER BY ts DESC, id DESC LIMIT ?", (record_id, *case_ids, limit)
    elif record_id:
        q, args = "SELECT * FROM timeline WHERE record_id=? ORDER BY ts DESC, id DESC LIMIT ?", (record_id, limit)
    elif case_ids:
        marks = ",".join("?" * len(case_ids))
        q, args = f"SELECT * FROM timeline WHERE case_id IN ({marks}) ORDER BY ts DESC, id DESC LIMIT ?", (*case_ids, limit)
    else:
        return []
    return [_tl(r) for r in _rows(s._conn.execute(q, args))]


def recent_timeline(store, desk_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    s, did = _base(store)
    did = did if did is not None else desk_id
    return [_tl(r) for r in _rows(s._conn.execute("SELECT * FROM timeline WHERE desk_id=? ORDER BY ts DESC, id DESC LIMIT ?", (did, limit)))]


# ---------------------------------------------------------------------------------------------- sync from what the desk knows
def mirror_contact(store, desk_id: int, contact: dict[str, Any]) -> dict[str, Any] | None:
    """A CRM contact as a person record (+ its organisation). Called on every CRM upsert, so records never lag."""
    props = {k: contact.get(k) for k in ("name", "email", "phone", "company", "stage", "notes", "next_action") if contact.get(k)}
    if not (props.get("email") or props.get("phone") or props.get("name")):
        return None
    key = props.get("email") or props.get("phone") or props.get("name")
    existing = person_for(store, desk_id, email=props.get("email") or "", phone=props.get("phone") or "")
    if existing:
        key = existing["key"]
    rec, _ = upsert(store, desk_id, "person", props, key=str(key), source="crm", log=False)
    if props.get("company") and props.get("company") != props.get("name"):
        org, _ = upsert(store, desk_id, "organisation", {"name": props["company"]}, source="crm", log=False)
        link(store, desk_id, rec["id"], "works_at", org["id"], source="crm")
    return rec


def person_for(store, desk_id: int | None, email: str = "", phone: str = "") -> dict[str, Any] | None:
    """The person record behind an email address or phone number (either may be the key or just a prop)."""
    s, did = _base(store)
    did = did if did is not None else desk_id
    if email:
        r = by_key(store, did, "person", email)
        if r:
            return r
        rows = _rows(s._conn.execute("SELECT * FROM records WHERE desk_id=? AND type='person' AND LOWER(props) LIKE ? LIMIT 5",
                                     (did, f"%{email.strip().lower()}%")))
        for row in rows:
            p = _decode(row)
            if str(p["props"].get("email", "")).strip().lower() == email.strip().lower():
                return p
    if phone and _digits(phone):
        r = by_key(store, did, "person", phone)
        if r:
            return r
        d = _digits(phone)
        rows = _rows(s._conn.execute("SELECT * FROM records WHERE desk_id=? AND type='person' AND props LIKE ? LIMIT 20",
                                     (did, f"%{d[-7:]}%")))
        for row in rows:
            p = _decode(row)
            if _digits(str(p["props"].get("phone", ""))) == d:
                return p
    return None


def sync_desk(store, desk_id: int) -> dict[str, int]:
    """Backfill from the desk's CRM contacts and camera connectors (idempotent)."""
    s, _ = _base(store)
    n_people = n_cams = 0
    for c in s.contacts("", desk_id):
        if mirror_contact(s, desk_id, c):
            n_people += 1
    for c in s.connectors(desk_id):
        if c.get("kind") == "camera":
            cfg = c.get("config") or {}
            upsert(s, desk_id, "camera", {"name": c["name"], "notes": cfg.get("notes") or "", "focus": cfg.get("focus") or ""},
                   key=c["name"], source="cameras", log=False)
            n_cams += 1
    return {"people": n_people, "cameras": n_cams}


_synced: set[int] = set()


def ensure_synced(store, desk_id: int) -> None:
    """Once per process per desk: older desks get their CRM + cameras as records without anyone pressing a button."""
    if desk_id in _synced:
        return
    _synced.add(desk_id)
    try:
        sync_desk(store, desk_id)
    except Exception:
        _synced.discard(desk_id)


# ---------------------------------------------------------------------------------------------- text for agents
def brief(rec: dict[str, Any], with_props: bool = True) -> str:
    p = rec.get("props") or {}
    bits = ", ".join(f"{k}={str(v)[:120]}" for k, v in p.items() if k not in ("name",) and v not in (None, ""))
    return f"{rec['type']} #{rec['id']} {rec['title']}" + (f" ({bits})" if with_props and bits else "")


def describe(store, rid: int, timeline_n: int = 12) -> str:
    """Full text view of one record for a tool result: props, links, cases, recent timeline."""
    from . import cases as C
    rec = get(store, rid)
    if not rec:
        return f"no record #{rid}"
    lines = [f"{rec['type'].upper()} #{rec['id']} — {rec['title']}  (key {rec['key']})"]
    for k, v in (rec["props"] or {}).items():
        lines.append(f"  {k}: {str(v)[:400]}")
    ls = links(store, rid)
    if ls:
        lines.append("Links:")
        for l in ls[:30]:
            arrow = "→" if l["direction"] == "out" else "←"
            lines.append(f"  {arrow} {l['rel']} {l['record']['type']} #{l['record']['id']} {l['record']['title']}")
    cs = C.for_record(store, rid)
    if cs:
        lines.append("Cases:")
        for c in cs[:10]:
            lines.append(f"  case #{c['id']} {c['type']} '{c['title']}' state={c['state']}" + (" (closed)" if c.get("closed_at") else ""))
    tl = timeline_for(store, rid, [c["id"] for c in cs], timeline_n)
    if tl:
        lines.append("Recent history (newest first):")
        for t in tl:
            lines.append(f"  {time.strftime('%d %b %H:%M', time.localtime(t['ts']))} {t['kind']} by {t['actor'] or '-'}: {t['text'][:200]}")
    return "\n".join(lines)


def summary_text(store, desk_id: int | None = None) -> str:
    """One line per type for the agents' system prompt."""
    counts = type_counts(store, desk_id)
    if not counts:
        return ""
    parts = [f"{n} {((TYPES.get(t) or {}).get('plural') or t).lower()}" for t, n in sorted(counts.items(), key=lambda x: -x[1])]
    return ("Business records on this desk: " + ", ".join(parts) + ". Look things up with record_find / record_get before "
            "asking or guessing; save what you learn with record_save (people, bookings, orders, anything that matters later).")
