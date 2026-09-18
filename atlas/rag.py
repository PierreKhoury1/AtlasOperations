"""Vision RAG: real retrieval over what the cameras saw.

Before this module, "ask the cameras" was a keyword match over the last N hours of the event log. Now every vision
event is embedded (text of the event, and the snapshot image itself when a CLIP model is available), the question
is embedded the same way, and retrieval fuses four channels with reciprocal-rank fusion:

  text semantic   question vector vs. event-text vector      (CLIP text tower, API embeddings, or hashed trigrams)
  image semantic  question vector vs. snapshot vector        (CLIP only: "a van at the back door" finds the frame)
  keyword         token overlap                              (exact camera names, labels, ids)
  recency         newest first                               (tie-break so "what's happening" favours now)

The question's time phrases ("last night", "yesterday afternoon", "between 2 and 4pm", "after 8pm") become a hard
window, so the log is filtered by what the owner meant rather than a fixed 24h.

The answer is grounded: the top retrieved snapshots are sent back to the vision model together with the log lines,
so it re-looks at the actual frames instead of trusting the analyst notes alone. Event ids are cited as [#id].

Embedders (env VISION_EMBED = auto | clip | api | hash):
  clip  open_clip ViT-B-32 laion2b (local; text + image in one space; ~80 ms per item on CPU)
  api   OpenAI-compatible /embeddings (OpenRouter openai/text-embedding-3-small by default; text only; works on Render)
  hash  hashed word+trigram vectors (no model, deterministic; keeps the pipeline honest in tests and offline)
`auto` = clip if importable and loadable, else api if a key exists, else hash.

Vectors live in the `vision_vectors` table (SQLite BLOB / Postgres BYTEA), one row per event per embedder model,
so switching embedders re-indexes lazily rather than corrupting scores.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np

# --------------------------------------------------------------------------- embedders

class Embedder:
    name = "none"
    dim = 0
    images = False
    floor_text = 0.12          # cosine below which a text match is noise for this embedder (strict mode)
    floor_image = 0.22         # same for question -> snapshot (CLIP); unused when images=False

    def embed_texts(self, texts: list[str]) -> np.ndarray:  # (n, dim) L2-normalised float32
        raise NotImplementedError

    def embed_images(self, jpegs: list[bytes]) -> np.ndarray | None:
        return None


def _l2(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    if m.ndim == 1:
        m = m[None, :]
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


class HashEmbedder(Embedder):
    """Words + character trigrams hashed into a fixed-size signed bag. No model, deterministic, offline.
    Semantically blind, but robust to spelling variants (trigrams) and fully compatible with the fusion code."""
    name = "hash-trigram-256"
    dim = 256
    images = False
    floor_text = 0.22          # measured: unrelated short texts reach ~0.16, real matches 0.23-0.55

    @staticmethod
    def _feats(text: str) -> list[str]:
        t = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())
        words = [w for w in t.split() if len(w) > 1]
        feats = [f"w:{w}" for w in words]
        for w in words:
            ww = f" {w} "
            feats += [f"t:{ww[i:i + 3]}" for i in range(len(ww) - 2)]
        return feats

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, txt in enumerate(texts):
            for f in self._feats(txt):
                h = hashlib.blake2b(f.encode("utf-8"), digest_size=4).digest()
                v = int.from_bytes(h, "little")
                out[i, v % self.dim] += 1.0 if (v >> 31) & 1 else -1.0
        return _l2(out)


class ClipEmbedder(Embedder):
    """open_clip ViT-B-32 (laion2b). Text and images share one space, so a question can rank frames directly."""
    name = "clip-vit-b-32"
    dim = 512
    images = True
    floor_text = 0.78          # CLIP text-text cosines are compressed: unrelated ~0.6-0.75, related ~0.85+
    floor_image = 0.22         # CLIP text-image: unrelated ~0.15, a real match ~0.25+

    def __init__(self, model: str = "", pretrained: str = ""):
        self.model_name = model or os.environ.get("CLIP_MODEL", "ViT-B-32")
        self.pretrained = pretrained or os.environ.get("CLIP_PRETRAINED", "laion2b_s34b_b79k")
        self.name = "clip-" + re.sub(r"[^a-z0-9]+", "-", self.model_name.lower()).strip("-")
        self._m = None
        self._pre = None
        self._tok = None
        self._lock = threading.Lock()

    def warm(self) -> None:
        with self._lock:
            if self._m is not None:
                return
            import open_clip  # type: ignore
            import torch  # type: ignore
            torch.set_num_threads(max(1, min(4, os.cpu_count() or 2)))
            m, _, pre = open_clip.create_model_and_transforms(self.model_name, pretrained=self.pretrained)
            m.eval()
            self._m, self._pre, self._tok = m, pre, open_clip.get_tokenizer(self.model_name)
            with torch.no_grad():
                self.dim = int(m.encode_text(self._tok(["x"])).shape[1])

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        self.warm()
        import torch  # type: ignore
        with torch.no_grad():
            v = self._m.encode_text(self._tok([t[:300] for t in texts]))  # type: ignore[union-attr]
        return _l2(v.cpu().numpy())

    def embed_images(self, jpegs: list[bytes]) -> np.ndarray | None:
        self.warm()
        import torch  # type: ignore
        from PIL import Image
        batch = []
        for b in jpegs:
            img = Image.open(io.BytesIO(b)).convert("RGB")
            batch.append(self._pre(img))  # type: ignore[misc]
        if not batch:
            return None
        with torch.no_grad():
            v = self._m.encode_image(torch.stack(batch))  # type: ignore[union-attr]
        return _l2(v.cpu().numpy())


class ApiEmbedder(Embedder):
    """OpenAI-compatible /embeddings endpoint (OpenRouter by default). Text only; runs anywhere with a key."""
    images = False
    floor_text = 0.35

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "", transport=None):
        from . import vision as V
        base, key, _ = V._vlm_cfg()
        self.base = (base_url or os.environ.get("EMBED_BASE_URL") or base).rstrip("/")
        self.key = api_key or os.environ.get("EMBED_API_KEY") or key
        self.model = model or os.environ.get("EMBED_MODEL", "openai/text-embedding-3-small")
        self.name = "api-" + re.sub(r"[^a-z0-9]+", "-", self.model.lower()).strip("-")
        self.dim = 0
        self._transport = transport

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        import httpx
        vecs: list[list[float]] = []
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                   "HTTP-Referer": "https://atlas-ops.onrender.com", "X-Title": "Atlas Desk vision RAG"}
        with httpx.Client(timeout=60, transport=self._transport) as c:
            for i in range(0, len(texts), 64):
                chunk = [t[:2000] or " " for t in texts[i:i + 64]]
                r = c.post(self.base + "/embeddings", headers=headers, json={"model": self.model, "input": chunk})
                if r.status_code >= 400:
                    raise RuntimeError(f"embeddings HTTP {r.status_code}: {r.text[:200]}")
                data = sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))
                vecs += [d["embedding"] for d in data]
        m = _l2(np.asarray(vecs, dtype=np.float32))
        self.dim = int(m.shape[1])
        return m


_EMB: dict[str, Embedder] = {}
_EMB_LOCK = threading.Lock()


def clip_importable() -> bool:
    try:
        import open_clip  # type: ignore  # noqa: F401
        import torch  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def get_embedder(mode: str = "") -> Embedder:
    """The process-wide embedder for `mode` (default env VISION_EMBED, default auto). Falls back down the chain
    clip -> api -> hash when a backend cannot load, so retrieval never stops working; `name` tells you which."""
    mode = (mode or os.environ.get("VISION_EMBED", "auto")).strip().lower()
    with _EMB_LOCK:
        if mode in _EMB:
            return _EMB[mode]
        emb: Embedder | None = None
        order = {"clip": ["clip", "hash"], "api": ["api", "hash"], "hash": ["hash"]}.get(mode, ["clip", "api", "hash"])
        for kind in order:
            try:
                if kind == "clip":
                    if not clip_importable():
                        continue
                    c = ClipEmbedder()
                    c.warm()
                    emb = c
                elif kind == "api":
                    a = ApiEmbedder()
                    if not a.key:
                        continue
                    a.embed_texts(["warm up"])
                    emb = a
                else:
                    emb = HashEmbedder()
                break
            except Exception:
                emb = None
                continue
        _EMB[mode] = emb or HashEmbedder()
        return _EMB[mode]


def reset_embedders() -> None:
    with _EMB_LOCK:
        _EMB.clear()


# --------------------------------------------------------------------------- vector encode / decode

def _pack(v: np.ndarray | None) -> bytes | None:
    return None if v is None else np.asarray(v, dtype=np.float32).tobytes()


def _unpack(b: Any, dim: int) -> np.ndarray | None:
    if b is None:
        return None
    raw = bytes(b)
    if not raw:
        return None
    v = np.frombuffer(raw, dtype=np.float32)
    return v if (dim <= 0 or v.shape[0] == dim) else None


# --------------------------------------------------------------------------- indexing

def event_text(ev: dict[str, Any]) -> str:
    """The text an event is embedded from: camera, what was counted, why it fired, what the analyst said."""
    from . import vision as V
    counts = ev.get("counts") or {}
    parts = [str(ev.get("camera") or ""), V.counts_text(counts) if counts else "",
             str(ev.get("reason") or ""), str(ev.get("question") or ""), str(ev.get("answer") or "")]
    return " | ".join(p for p in parts if p).strip()


def _snapshot_bytes(ev: dict[str, Any], max_side: int = 512) -> bytes | None:
    p = str(ev.get("snapshot") or "")
    if not p or not Path(p).is_file():
        return None
    try:
        from . import vision as V
        return V._shrink(Path(p).read_bytes(), max_side)
    except Exception:
        return None


def index_events(store, events: list[dict[str, Any]], emb: Embedder | None = None, with_images: bool = True) -> int:
    """Embed and store vectors for `events` (rows from the vision_events table). Returns rows written."""
    if not events:
        return 0
    emb = emb or get_embedder()
    texts = emb.embed_texts([event_text(e) for e in events])
    imgs: list[np.ndarray | None] = [None] * len(events)
    if with_images and emb.images:
        jpegs, idx = [], []
        for i, e in enumerate(events):
            b = _snapshot_bytes(e)
            if b:
                jpegs.append(b)
                idx.append(i)
        if jpegs:
            try:
                iv = emb.embed_images(jpegs)
                if iv is not None:
                    for j, i in enumerate(idx):
                        imgs[i] = iv[j]
            except Exception:
                pass
    n = 0
    for i, e in enumerate(events):
        store.put_vision_vector(int(e["id"]), int(e["desk_id"]), float(e["ts"]), emb.name, int(texts.shape[1]),
                               _pack(texts[i]), _pack(imgs[i]))
        n += 1
    return n


class _Indexer:
    """Background indexer: `add_vision_event` hands each new event over; the worker embeds it off the request path.
    VISION_INDEX_SYNC=1 embeds inline (tests, CLI)."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self._t: threading.Thread | None = None
        self._lock = threading.Lock()
        self.errors = 0

    def submit(self, store, ev: dict[str, Any]) -> None:
        if os.environ.get("VISION_EMBED", "").strip().lower() == "off":
            return
        if os.environ.get("VISION_INDEX_SYNC", "").strip() in ("1", "true", "yes"):
            try:
                index_events(store, [ev])
            except Exception:
                self.errors += 1
            return
        self.q.put((store, ev))
        with self._lock:
            if self._t is None or not self._t.is_alive():
                self._t = threading.Thread(target=self._work, name="vision-indexer", daemon=True)
                self._t.start()

    def _work(self) -> None:
        while True:
            try:
                store, ev = self.q.get(timeout=30)
            except queue.Empty:
                return
            try:
                index_events(store, [ev])
            except Exception:
                self.errors += 1
            finally:
                self.q.task_done()

    def flush(self, timeout: float = 30) -> None:
        end = time.time() + timeout
        while not self.q.empty() and time.time() < end:
            time.sleep(0.05)
        if self._t and self._t.is_alive():
            self.q.join()


INDEXER = _Indexer()


def on_event(store, ev: dict[str, Any] | None) -> None:
    """Called by Store.add_vision_event after the row is committed."""
    if ev:
        INDEXER.submit(store, ev)


def backfill(store, desk_id: int, emb: Embedder | None = None, limit: int = 5000, budget_s: float = 0) -> int:
    """Index every event on the desk that has no vector for the current embedder. Bounded by `budget_s` if > 0."""
    emb = emb or get_embedder()
    done = 0
    t0 = time.time()
    while done < limit:
        batch = store.unindexed_vision_events(desk_id, emb.name, min(32, limit - done))
        if not batch:
            break
        done += index_events(store, batch, emb)
        if budget_s and time.time() - t0 > budget_s:
            break
    return done


# --------------------------------------------------------------------------- time windows in the question

_DOW = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _clock(h: str, m: str | None, ampm: str | None, default_pm_after: int = 7) -> float:
    """'8', '8pm', '08:30', '12 noon' -> hour as float. Bare hours 1-7 are read as pm ('between 2 and 4')."""
    hh = int(h)
    mm = int(m) if m else 0
    if ampm:
        ampm = ampm.replace(".", "").lower()
        if ampm.startswith("p") and hh < 12:
            hh += 12
        if ampm.startswith("a") and hh == 12:
            hh = 0
    elif 1 <= hh <= default_pm_after:
        hh += 12
    return hh + mm / 60.0


def parse_time_window(question: str, now: float | None = None) -> tuple[float, float, str] | None:
    """Turn time phrases in the question into (since, until, label). None when the question names no time."""
    q = " " + re.sub(r"\s+", " ", re.sub(r"[?!,;()\"']", " ", (question or "").lower())) + " "
    nowdt = datetime.fromtimestamp(now if now is not None else time.time())
    today = nowdt.replace(hour=0, minute=0, second=0, microsecond=0)

    def span(day: datetime, h1: float, h2: float, label: str):
        a = day + timedelta(hours=h1)
        b = day + timedelta(hours=h2)
        return a.timestamp(), min(b, nowdt).timestamp() if b > nowdt and a < nowdt else b.timestamp(), label

    m = re.search(r" (?:in the )?last (\d+) (hour|hours|minute|minutes|day|days|week|weeks) ", q) or \
        re.search(r" (?:in the )?past (\d+) (hour|hours|minute|minutes|day|days|week|weeks) ", q)
    if m:
        n = int(m.group(1))
        unit = m.group(2).rstrip("s")
        secs = {"hour": 3600, "minute": 60, "day": 86400, "week": 604800}[unit]
        return nowdt.timestamp() - n * secs, nowdt.timestamp(), f"last {n} {unit}{'s' if n != 1 else ''}"
    if " last hour " in q or " past hour " in q:
        return nowdt.timestamp() - 3600, nowdt.timestamp(), "last hour"
    if " last night " in q:
        return span(today - timedelta(days=1), 20, 30, "last night (20:00-06:00)")
    if " overnight " in q or " tonight " in q:
        start = (today - timedelta(days=1)) if nowdt.hour < 20 else today
        return span(start, 20, 30, "overnight (20:00-06:00)")
    if " this morning " in q:
        return span(today, 5, 12, "this morning (05:00-12:00)")
    if " this afternoon " in q:
        return span(today, 12, 18, "this afternoon (12:00-18:00)")
    if " this evening " in q:
        return span(today, 18, 24, "this evening (18:00-24:00)")
    if " yesterday morning " in q:
        return span(today - timedelta(days=1), 5, 12, "yesterday morning")
    if " yesterday afternoon " in q:
        return span(today - timedelta(days=1), 12, 18, "yesterday afternoon")
    if " yesterday evening " in q:
        return span(today - timedelta(days=1), 18, 24, "yesterday evening")
    if " yesterday " in q:
        return span(today - timedelta(days=1), 0, 24, "yesterday")
    tm = r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?"
    m = re.search(rf" between {tm} and {tm} ", q)
    if m:
        h1 = _clock(m.group(1), m.group(2), m.group(3) or m.group(6))
        h2 = _clock(m.group(4), m.group(5), m.group(6))
        if h2 < h1:
            h2 += 12 if h2 + 12 <= 24 else 0
        day = today if today + timedelta(hours=h1) <= nowdt else today - timedelta(days=1)
        if " yesterday " in q:
            day = today - timedelta(days=1)
        return span(day, h1, h2, f"{day.strftime('%a')} {int(h1):02d}:{int(round((h1 % 1) * 60)):02d}-{int(h2):02d}:{int(round((h2 % 1) * 60)):02d}")
    m = re.search(rf" after {tm} ", q)
    if m:
        h1 = _clock(m.group(1), m.group(2), m.group(3))
        day = today if today + timedelta(hours=h1) <= nowdt else today - timedelta(days=1)
        return span(day, h1, 24, f"{day.strftime('%a')} after {int(h1):02d}:00")
    m = re.search(rf" before {tm} ", q)
    if m:
        h2 = _clock(m.group(1), m.group(2), m.group(3), default_pm_after=0)
        return span(today, 0, h2, f"today before {int(h2):02d}:00")
    m = re.search(rf" (?:at|around) {tm} ", q)
    if m:
        h = _clock(m.group(1), m.group(2), m.group(3))
        day = today if today + timedelta(hours=h) <= nowdt else today - timedelta(days=1)
        return span(day, h - 0.75, h + 0.75, f"{day.strftime('%a')} around {int(h):02d}:00")
    for i, d in enumerate(_DOW):
        if f" {d} " in q or f" on {d} " in q:
            back = (today.weekday() - i) % 7
            day = today - timedelta(days=back or 7 if " last " in q else back)
            return span(day, 0, 24, d.title())
    if " today " in q or " so far today " in q:
        return today.timestamp(), nowdt.timestamp(), "today"
    return None


# --------------------------------------------------------------------------- retrieval

_STOP = {"the", "was", "were", "what", "when", "how", "many", "did", "there", "any", "last", "this", "that", "and", "with",
         "from", "who", "has", "have", "been", "are", "for", "you", "see", "saw", "seen", "anyone", "anything", "camera",
         "cameras", "did", "does", "night", "today", "yesterday", "morning", "afternoon", "evening", "hours", "hour"}


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{2,}", (s or "").lower()) if w not in _STOP}


def _rrf(rank_lists: list[tuple[float, list[int]]], k: int = 60) -> dict[int, float]:
    score: dict[int, float] = {}
    for w, ids in rank_lists:
        for r, eid in enumerate(ids):
            score[eid] = score.get(eid, 0.0) + w / (k + r + 1)
    return score


def _signal(sim: dict[int, float], floor: float) -> set[int]:
    """Ids whose similarity is a real signal: above the embedder's floor, or clearly above the pack (mean + 1 sd)."""
    if not sim:
        return set()
    vals = np.asarray(list(sim.values()), dtype=np.float32)
    hi = float(vals.mean() + vals.std()) if len(vals) >= 8 else 9.0
    return {eid for eid, v in sim.items() if v >= floor or v >= hi}


def retrieve(store, desk_id: int, question: str, hours: float = 24, camera: str = "", k: int = 40,
             alerts_only: bool = False, emb: Embedder | None = None, now: float | None = None,
             index_budget_s: float = 2.5, strict: bool = False) -> dict[str, Any]:
    """Hybrid retrieval over the desk's vision log. Returns {rows (chronological top-k), scores, why, window, matched, ...}.
    `matched` = ids with a real relevance signal (keyword hit or similarity above the embedder's floor); strict=True
    returns only those, which is what the agent's camera_events(query=...) wants; the ask pipeline keeps the best
    context regardless and tells the model how strong the match was."""
    now = now if now is not None else time.time()
    emb = emb or get_embedder()
    window = parse_time_window(question, now)
    since, until, label = window if window else (now - float(hours) * 3600, now, f"last {hours:g}h")
    rows = [r for r in store.vision_events(desk_id, camera, since, "", 1500) if r["ts"] <= until + 1]
    if alerts_only:
        rows = [r for r in rows if r.get("triggered")]
    out: dict[str, Any] = {"rows": [], "scores": {}, "why": {}, "window": label, "since": since, "until": until,
                           "embedder": emb.name, "considered": len(rows), "indexed": 0, "channels": [], "matched": []}
    if not rows:
        return out
    by_id = {int(r["id"]): r for r in rows}
    # make sure the candidates are embedded (bounded: text is cheap, images ~80ms each on CPU)
    vecs = {int(v["event_id"]): v for v in store.vision_vectors(desk_id, since, emb.name, 3000)}
    missing = [r for r in rows if int(r["id"]) not in vecs]
    if missing:
        t0 = time.time()
        for i in range(0, len(missing), 16):
            try:
                index_events(store, missing[i:i + 16], emb)
            except Exception:
                break
            if index_budget_s and time.time() - t0 > index_budget_s:
                for r in missing[i + 16:]:
                    INDEXER.submit(store, r)
                break
        vecs = {int(v["event_id"]): v for v in store.vision_vectors(desk_id, since, emb.name, 3000)}
    out["indexed"] = sum(1 for eid in by_id if eid in vecs)
    qv = emb.embed_texts([question])[0]
    lists: list[tuple[float, list[int]]] = []
    channels = []
    # text semantic
    tids, tvs = [], []
    iids, ivs = [], []
    for eid, v in vecs.items():
        if eid not in by_id:
            continue
        tv = _unpack(v.get("text_vec"), int(v.get("dim") or 0))
        if tv is not None and tv.shape[0] == qv.shape[0]:
            tids.append(eid)
            tvs.append(tv)
        iv = _unpack(v.get("image_vec"), int(v.get("dim") or 0))
        if iv is not None and iv.shape[0] == qv.shape[0]:
            iids.append(eid)
            ivs.append(iv)
    text_sim: dict[int, float] = {}
    if tids:
        sims = np.stack(tvs) @ qv
        text_sim = {eid: float(s) for eid, s in zip(tids, sims)}
        lists.append((1.0, [eid for eid, _ in sorted(text_sim.items(), key=lambda x: -x[1])]))
        channels.append("text")
    image_sim: dict[int, float] = {}
    if iids:
        sims = np.stack(ivs) @ qv
        image_sim = {eid: float(s) for eid, s in zip(iids, sims)}
        lists.append((1.0, [eid for eid, _ in sorted(image_sim.items(), key=lambda x: -x[1])]))
        channels.append("image")
    # keyword
    qt = _tokens(question)
    kw: dict[int, float] = {}
    if qt:
        for eid, r in by_id.items():
            blob = _tokens(event_text(r) + " " + json.dumps(r.get("counts") or {}))
            hit = len(qt & blob)
            if hit:
                kw[eid] = hit / len(qt)
        if kw:
            lists.append((0.7, [eid for eid, _ in sorted(kw.items(), key=lambda x: -x[1])]))
            channels.append("keyword")
    # recency (+ alerts nudge)
    lists.append((0.3, [eid for eid, _ in sorted(by_id.items(), key=lambda x: -x[1]["ts"])]))
    channels.append("recency")
    score = _rrf(lists)
    for eid, r in by_id.items():
        if r.get("triggered"):
            score[eid] = score.get(eid, 0.0) + 0.004
    matched = set(kw) | _signal(text_sim, emb.floor_text) | _signal(image_sim, emb.floor_image)
    ranked = sorted(score.items(), key=lambda x: -x[1])
    if strict:
        ranked = [(eid, s) for eid, s in ranked if eid in matched]
    ranked = ranked[:k]
    why: dict[int, list[str]] = {}
    tops = {"text": set(lists_top(text_sim)) & _signal(text_sim, emb.floor_text),
            "image": set(lists_top(image_sim)) & _signal(image_sim, emb.floor_image), "keyword": set(lists_top(kw))}
    for eid, _ in ranked:
        why[eid] = [c for c in ("text", "image", "keyword") if eid in tops[c]] + (["alert"] if by_id[eid].get("triggered") else [])
    picked = [by_id[eid] for eid, _ in ranked]
    picked.sort(key=lambda r: r["ts"])
    out.update({"rows": picked, "scores": {eid: s for eid, s in ranked}, "why": why, "channels": channels,
                "text_sim": text_sim, "image_sim": image_sim, "matched": sorted(matched)})
    return out


def lists_top(d: dict[int, float], n: int = 6) -> list[int]:
    return [eid for eid, _ in sorted(d.items(), key=lambda x: -x[1])[:n]]


# --------------------------------------------------------------------------- grounded answer

def log_lines(rows: list[dict[str, Any]], why: dict[int, list[str]] | None = None) -> list[str]:
    from . import vision as V
    out = []
    for r in rows:
        tag = f" | matched: {','.join(why[int(r['id'])])}" if why and why.get(int(r["id"])) else ""
        src = r.get("source") or ""
        label = {"journal": "journal note", "digest": "SUMMARY"}.get(src, "analyst")
        cap = 900 if src in ("journal", "digest") else 220
        out.append(f"[#{r['id']}] {time.strftime('%a %d %b %H:%M:%S', time.localtime(r['ts']))} | {r['camera']} | {V.counts_text(r['counts'])} | motion {float(r.get('motion') or 0):.2f}"
                   + (" | ALERT " + (r.get("reason") or "") if r.get("triggered") else (" | " + r["reason"] if r.get("reason") else ""))
                   + (f" | {label}: {str(r['answer'])[:cap]}" if r.get("answer") else "") + tag)
    return out


def _system(business: dict[str, Any] | None, window: str) -> str:
    b = business or {}
    return ("You answer the owner's questions about what their cameras and sensors saw. You get (1) the retrieved event log, "
            "one line per event, and (2) the actual snapshot frames of the most relevant events, each labelled with its event id. "
            "Look at the frames: they are the ground truth; the analyst notes were written by a smaller model and can be wrong. "
            "'journal note' lines are the camera's running diary (written on every scene change and at least once a minute, "
            "comparing with the previous frame); 'SUMMARY' lines condense a window of notes. Use them for times, durations and "
            "sequences (who arrived when, how long someone waited, what happened in order). "
            "Give times, cameras and counts. Cite event ids in square brackets like [#12]. If the log and frames do not contain "
            "the answer, say so plainly and say what the closest evidence is. Never identify people by name. Plain text, no markdown. "
            f"Time window searched: {window}. Today is {time.strftime('%A %d %B %Y %H:%M')}. "
            f"Business: {b.get('name', '')} — {str(b.get('extra_context', '') or b.get('description', ''))[:400]}")


def ask(store, desk_id: int, question: str, hours: float = 24, camera: str = "", business: dict[str, Any] | None = None,
        mode: str = "live", text_answer: Callable[[str, str], str] | None = None, vlm_model: str = "",
        look_frames: int = 4, k: int = 30, emb: Embedder | None = None, transport=None, now: float | None = None) -> dict[str, Any]:
    """Retrieve, re-look at the best frames, answer with citations. `text_answer(system, prompt)` is the text-only
    fallback (the desk's Atlas model) when no vision model key exists or no snapshots survived."""
    from . import vision as V
    ret = retrieve(store, desk_id, question, hours, camera, k=k, emb=emb, now=now)
    rows = ret["rows"]
    meta = {"method": "hybrid-rrf", "embedder": ret["embedder"], "window": ret["window"], "considered": ret["considered"],
            "indexed": ret["indexed"], "channels": ret["channels"], "matched": len(ret["matched"]), "looked_at": [], "grounding": "none"}
    if not rows:
        return {"answer": f"The camera log has nothing for {ret['window']}" + (f" on {camera}" if camera else "") + ".",
                "evidence": [], "retrieval": meta, "events_considered": 0}
    lines = log_lines(rows, ret["why"])
    # frames to re-look at: best by fused score that still have a snapshot on disk
    best = sorted(rows, key=lambda r: -ret["scores"].get(int(r["id"]), 0))
    frames: list[tuple[str, bytes]] = []
    for r in best:
        if len(frames) >= look_frames:
            break
        b = _snapshot_bytes(r, 768)
        if b:
            frames.append((f"[#{r['id']}] {r['camera']} {time.strftime('%a %H:%M', time.localtime(r['ts']))}", b))
            meta["looked_at"].append(int(r["id"]))
    strength = (f"{len(ret['matched'])} of {ret['considered']} events match the question" if ret["matched"]
                else "NO event matched the question's words or meaning - the lines below are just the nearest by time; say so if they do not answer it")
    prompt = f"Event log (oldest first; {strength}):\n" + "\n".join(lines) + f"\n\nQuestion: {question}"
    if mode == "demo":
        cams = sorted({r["camera"] for r in rows})
        alerts = [r for r in rows if r.get("triggered")]
        tot: dict[str, int] = {}
        for r in rows:
            for kk, v in (r["counts"] or {}).items():
                tot[kk] = tot.get(kk, 0) + int(v)
        top = best[:3]
        answer = (f"Demo mode (no live model). Searched {ret['window']}: {ret['considered']} event(s) on {', '.join(cams)}, "
                  f"{len(alerts)} woke the desk; totals {V.counts_text(tot) or 'nothing counted'}. Best matches: "
                  + "; ".join(f"[#{r['id']}] {r['camera']} {time.strftime('%H:%M', time.localtime(r['ts']))} "
                              f"{V.counts_text(r['counts']) or (r.get('reason') or '')[:60]}" for r in top) + ".")
        meta["grounding"] = "demo"
    elif frames and V.vlm_ready():
        try:
            answer = V.chat_images(_system(business, ret["window"]), prompt, frames, model=vlm_model, max_tokens=1000, transport=transport)
            meta["grounding"] = f"vision model re-looked at {len(frames)} frame(s)"
        except Exception as exc:
            if not text_answer:
                raise
            answer = text_answer(_system(business, ret["window"]), prompt)
            meta["grounding"] = f"text only (vision model failed: {type(exc).__name__})"
    elif text_answer:
        answer = text_answer(_system(business, ret["window"]), prompt)
        meta["grounding"] = "text only (no snapshots to re-look at)" if not frames else "text only (no vision model key)"
    else:
        raise RuntimeError("no model available to answer")
    answer = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), answer or "")
    answer = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", answer)
    return {"answer": answer.strip() or "(no answer)", "evidence": rows[-12:], "retrieval": meta,
            "events_considered": ret["considered"], "scores": ret["scores"]}


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    import argparse
    from . import config as cfg
    from .store import Store
    p = argparse.ArgumentParser(prog="atlas rag", description="Vision RAG: index the camera log, ask it questions")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("status", help="embedder + index coverage per desk")
    s.add_argument("--desk", type=int, default=0)
    i = sub.add_parser("index", help="backfill vectors for events without one")
    i.add_argument("--desk", type=int, default=0, help="desk id (default: every desk)")
    i.add_argument("--limit", type=int, default=5000)
    a = sub.add_parser("ask", help="ask the cameras (retrieval + grounded answer)")
    a.add_argument("question")
    a.add_argument("--desk", type=int, default=1)
    a.add_argument("--hours", type=float, default=24)
    a.add_argument("--camera", default="")
    a.add_argument("--demo", action="store_true", help="deterministic answer, no model call")
    a.add_argument("--json", action="store_true")
    for sp in (s, i, a):
        sp.add_argument("--db", default="", help="SQLite file (default: the desk portal's data/desk.db); DATABASE_URL wins")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    from .config import DATA_DIR
    db_path = Path(args.db) if getattr(args, "db", "") else (DATA_DIR / "desk.db" if (DATA_DIR / "desk.db").exists() else None)
    store = Store(db_path, url=os.environ.get("DATABASE_URL") or None) if db_path else Store(url=os.environ.get("DATABASE_URL") or None)
    emb = get_embedder()
    if args.cmd in (None, "status"):
        print(f"embedder: {emb.name} (dim {emb.dim}, images={emb.images})")
        desks = [d for d in store.all_desks() if not args.desk or d["id"] == args.desk]
        for d in desks:
            n = len(store.vision_events(d["id"], "", 0, "", 100000))
            v = store.vision_vector_count(d["id"], emb.name)
            print(f"desk {d['id']} {d.get('name', '')!r}: {n} events, {v} indexed with {emb.name}")
        return 0
    if args.cmd == "index":
        desks = [d["id"] for d in store.all_desks() if not args.desk or d["id"] == args.desk]
        for did in desks:
            t0 = time.time()
            n = backfill(store, did, emb, args.limit)
            print(f"desk {did}: indexed {n} event(s) in {time.time() - t0:.1f}s with {emb.name}")
        return 0
    if args.cmd == "ask":
        desk = store.desk(args.desk)
        business = ((desk or {}).get("config") or {}).get("business") or {}
        from .providers import ProviderPool
        pool = ProviderPool(cfg.load("providers", cfg.DEFAULT_PROVIDERS))

        def text_answer(system: str, prompt: str) -> str:
            prov = pool.get()
            return prov.chat(system, [prov.user_message(prompt)], [], "").text or ""

        res = ask(store, args.desk, args.question, args.hours, args.camera, business,
                  mode="demo" if args.demo else "live", text_answer=text_answer)
        if args.json:
            print(json.dumps({k: v for k, v in res.items() if k != "scores"}, default=str, indent=2, ensure_ascii=False))
            return 0
        r = res["retrieval"]
        print(f"[{r['embedder']} · {r['window']} · {r['considered']} events, {r['indexed']} indexed · {r['grounding']}]")
        print(res["answer"])
        print("\nevidence:")
        for e in res["evidence"]:
            print("  " + log_lines([e])[0])
        return 0
    p.print_help()
    return 2
