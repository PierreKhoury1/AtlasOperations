"""Vision RAG: embedders, the vector index, time-window parsing, hybrid retrieval, the grounded ask pipeline and the
agent tools that use it. Runs with the hashed embedder (conftest sets VISION_EMBED=hash, VISION_INDEX_SYNC=1) so
nothing here needs CLIP, torch or a network."""
import json
import time
from datetime import datetime

import httpx
import numpy as np
import pytest
from PIL import Image

from atlas import rag as R


def J(resp):
    return json.loads(resp.data)


# ------------------------------------------------------------------ embedders

def test_hash_embedder_is_deterministic_and_normalised():
    e = R.HashEmbedder()
    a = e.embed_texts(["delivery van at the back door", "delivery van at the back door", "two people at the counter"])
    assert a.shape == (3, 256)
    assert np.allclose(np.linalg.norm(a, axis=1), 1.0, atol=1e-5)
    assert np.allclose(a[0], a[1])
    assert float(a[0] @ a[2]) < float(a[0] @ a[1])
    # spelling variants stay close thanks to trigrams
    b = e.embed_texts(["deliveries van back-door"])
    assert float(a[0] @ b[0]) > float(a[2] @ b[0])


def test_api_embedder_against_mock_endpoint():
    seen = {}

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen["model"] = body["model"]
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [float(i + 1), 1.0, 0.0]} for i in range(len(body["input"]))]})

    e = R.ApiEmbedder(base_url="https://emb.example/v1", api_key="k", model="openai/text-embedding-3-small", transport=httpx.MockTransport(handler))
    v = e.embed_texts(["a", "b"])
    assert v.shape == (2, 3) and e.dim == 3 and e.name == "api-openai-text-embedding-3-small"
    assert seen == {"model": "openai/text-embedding-3-small", "auth": "Bearer k"}
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)


def test_get_embedder_hash_mode_and_reset():
    R.reset_embedders()
    e = R.get_embedder("hash")
    assert isinstance(e, R.HashEmbedder)
    assert R.get_embedder("hash") is e
    R.reset_embedders()


# ------------------------------------------------------------------ time windows

def test_parse_time_window_phrases():
    now = datetime(2026, 9, 15, 14, 30).timestamp()
    day = datetime(2026, 9, 15).timestamp()
    assert R.parse_time_window("how many people came in?", now) is None
    s, u, label = R.parse_time_window("was anyone at the back door last night?", now)
    assert label.startswith("last night") and s == day - 4 * 3600 and u == day + 6 * 3600
    s, u, label = R.parse_time_window("what happened yesterday afternoon", now)
    assert s == day - 86400 + 12 * 3600 and u == day - 86400 + 18 * 3600
    s, u, _ = R.parse_time_window("how busy was the counter between 12 and 2?", now)
    assert s == day + 12 * 3600 and u == day + 14 * 3600
    s, u, _ = R.parse_time_window("between 2pm and 4pm anyone?", now)
    assert s == day + 14 * 3600 and u == day + 14.5 * 3600      # window end capped at now (14:30)
    s, u, _ = R.parse_time_window("after 8pm anyone at the door", now)
    assert s == day - 86400 + 20 * 3600                          # 20:00 is in the future today -> yesterday evening
    s, u, _ = R.parse_time_window("in the last 3 hours", now)
    assert u - s == 3 * 3600
    s, u, _ = R.parse_time_window("what did the cameras see on monday", now)   # 15 Sep 2026 is a Tuesday
    assert s == day - 86400 and u == day
    s, u, _ = R.parse_time_window("anything today?", now)
    assert s == day and u == now
    s, u, _ = R.parse_time_window("deliveries this morning", now)
    assert s == day + 5 * 3600 and u == day + 12 * 3600


# ------------------------------------------------------------------ index + retrieval on the store

def _seed(store, desk_id, tmp_path):
    """Six events over a day on two cameras, one with a snapshot on disk."""
    snap = tmp_path / "snap.jpg"
    Image.new("RGB", (64, 48), (200, 30, 30)).save(snap, "JPEG")
    now = time.time()
    rows = [
        ("counter", {"person": 2}, "queue forming", "", "Two customers waiting at the till", False, now - 6 * 3600),
        ("back-door", {"person": 1, "truck": 1}, "person + truck", "", "A delivery van is parked at the rear door, driver unloading boxes", True, now - 5 * 3600),
        ("counter", {"person": 1}, "", "", "One staff member wiping the counter", False, now - 4 * 3600),
        ("back-door", {}, "", "", "Empty yard, bins closed", False, now - 3 * 3600),
        ("counter", {"person": 4}, "queue of 4", "", "Four people queueing, one at the till", True, now - 2 * 3600),
        ("street", {"car": 3}, "", "", "Three cars passing, quiet pavement", False, now - 1 * 3600),
    ]
    ids = []
    for cam, counts, reason, q, ans, trig, ts in rows:
        ev = store.add_vision_event(desk_id, cam, counts, motion=0.3, backend="test", reason=reason, question=q, answer=ans,
                                    snapshot=str(snap) if cam == "back-door" and trig else "", triggered=trig)
        store._conn.execute("UPDATE vision_events SET ts=? WHERE id=?", (ts, ev["id"]))
        store._conn.execute("UPDATE vision_vectors SET ts=? WHERE event_id=?", (ts, ev["id"]))
        store._conn.commit()
        ids.append(ev["id"])
    return ids


def test_events_are_indexed_on_insert_and_backfill_is_idempotent(store, tmp_path):
    ids = _seed(store, 1, tmp_path)
    emb = R.get_embedder()
    assert store.vision_vector_count(1, emb.name) == len(ids)
    vecs = store.vision_vectors(1, 0, emb.name)
    assert {v["event_id"] for v in vecs} == set(ids)
    assert all(v["dim"] == 256 and len(bytes(v["text_vec"])) == 256 * 4 for v in vecs)
    assert store.unindexed_vision_events(1, emb.name) == []
    assert R.backfill(store, 1, emb) == 0
    # a different embedder name means "not indexed yet" for that model, without touching the existing rows
    assert len(store.unindexed_vision_events(1, "other-model", 10)) == len(ids)


def test_retrieve_ranks_by_meaning_and_honours_time_and_camera(store, tmp_path):
    ids = _seed(store, 1, tmp_path)
    van = ids[1]
    ret = R.retrieve(store, 1, "when did the delivery van come to the rear door?", hours=24)
    assert ret["embedder"] == "hash-trigram-256" and ret["considered"] == 6 and ret["indexed"] == 6
    assert ret["channels"] == ["text", "keyword", "recency"]
    best = max(ret["scores"], key=ret["scores"].get)
    assert best == van
    assert "text" in ret["why"][van] or "keyword" in ret["why"][van]
    assert van in ret["matched"]
    # camera filter
    ret = R.retrieve(store, 1, "queue at the till", camera="counter")
    assert {r["camera"] for r in ret["rows"]} == {"counter"}
    # time window from the question beats the hours argument: 'last 2 hours' -> only the newest two
    ret = R.retrieve(store, 1, "what happened in the last 150 minutes", hours=24)
    assert ret["window"] == "last 150 minutes" and ret["considered"] == 2
    # strict mode drops non-matches; a nonsense query matches nothing
    ret = R.retrieve(store, 1, "zebra crossing giraffe", strict=True)
    assert ret["rows"] == [] and ret["matched"] == []
    ret = R.retrieve(store, 1, "delivery van", strict=True)
    assert [r["id"] for r in ret["rows"]] == [van]


def test_ask_demo_mode_cites_best_events_and_reports_retrieval(store, tmp_path):
    ids = _seed(store, 1, tmp_path)
    res = R.ask(store, 1, "did a delivery arrive at the back door?", mode="demo", business={"name": "Corner Shop"})
    assert f"[#{ids[1]}]" in res["answer"]
    m = res["retrieval"]
    assert m["method"] == "hybrid-rrf" and m["grounding"] == "demo" and m["considered"] == 6 and m["matched"] >= 1
    assert m["looked_at"] == [ids[1]]                     # the only event with a snapshot on disk
    assert res["events_considered"] == 6 and len(res["evidence"]) == 6


def test_ask_live_uses_vision_model_with_retrieved_frames(store, tmp_path, monkeypatch):
    ids = _seed(store, 1, tmp_path)
    calls = {}

    def fake_chat_images(system, text, images, model="", max_tokens=500, transport=None):
        calls["images"] = [lab for lab, _ in images]
        calls["text"] = text
        return f"A van unloaded at the rear door [#{ids[1]}]."

    from atlas import vision as V
    monkeypatch.setattr(V, "chat_images", fake_chat_images)
    monkeypatch.setattr(V, "vlm_ready", lambda: True)
    res = R.ask(store, 1, "did a delivery van come?", mode="live", text_answer=lambda s, p: "text fallback")
    assert res["answer"].startswith("A van unloaded")
    assert calls["images"] and calls["images"][0].startswith(f"[#{ids[1]}]")
    assert "of 6 events match" in calls["text"]
    assert res["retrieval"]["grounding"].startswith("vision model re-looked at 1")
    # no vision key -> text-only fallback on the desk model
    monkeypatch.setattr(V, "vlm_ready", lambda: False)
    res = R.ask(store, 1, "did a delivery van come?", mode="live", text_answer=lambda s, p: "text fallback")
    assert res["answer"] == "text fallback" and "text only" in res["retrieval"]["grounding"]


# ------------------------------------------------------------------ portal + agent tools

def test_portal_ask_endpoint_returns_retrieval_block(app_client):
    c = app_client
    c.post("/signup", json={"name": "Rag Tester", "company": "Rag Ltd", "email": "rag@example.com", "password": "password1"})
    c.post("/login", json={"email": "rag@example.com", "password": "password1"})
    d = J(c.post("/api/desks", json={"name": "Rag Ltd", "template": "site_watch", "tier": "free"}))
    import atlas.desk.app as A
    ev = A.store.add_vision_event(d["id"], "yard", {"truck": 1}, reason="truck seen", answer="A lorry reversing into the yard", triggered=True)
    A.store.add_vision_event(d["id"], "yard", {}, answer="Empty yard")
    r = J(c.post("/api/vision/ask", json={"question": "when did the lorry come?", "hours": 24}))
    assert r["mode"] == "demo" and f"[#{ev['id']}]" in r["answer"]
    assert r["retrieval"]["embedder"] == "hash-trigram-256" and r["retrieval"]["considered"] == 2
    assert r["events_considered"] == 2 and r["evidence"][0]["camera"] == "yard"
    assert c.post("/api/vision/ask", json={"question": ""}).status_code == 400


def test_agent_camera_ask_and_semantic_camera_events(app_client):
    """camera_ask / camera_events(query=) through the orchestrator's tool dispatch, on a DeskStore."""
    import atlas.desk.app as A
    from atlas.orchestrator import Orchestrator
    from atlas.providers import ToolCall
    from atlas.store import DeskStore
    c = app_client
    c.post("/login", json={"email": "rag@example.com", "password": "password1"})
    did = J(c.get("/api/desks"))["current"]
    desk = A.store.desk(did)
    ds = DeskStore(A.store, did)
    ds.add_vision_event("yard", {"person": 1}, reason="after-hours person", answer="Someone at the gate at night", triggered=True)
    configs = A.desk_configs(desk)
    orch = Orchestrator(configs, ds, lambda ev: None)
    atlas_agent = orch.agents["atlas"]
    assert "camera_ask" in atlas_agent["tools"]
    out = orch._tool(atlas_agent, ToolCall(id="1", name="camera_ask", args={"question": "was anyone at the gate?"}), 0)
    assert "[retrieval: hash-trigram-256" in out and "Demo mode" in out
    out = orch._tool(atlas_agent, ToolCall(id="2", name="camera_events", args={"query": "gate at night"}), 0)
    assert "yard" in out and "after-hours" in out
    assert orch._tool(atlas_agent, ToolCall(id="3", name="camera_events", args={"query": "zebra"}), 0).startswith("No camera events")
    assert orch._tool(atlas_agent, ToolCall(id="4", name="camera_ask", args={"question": ""}), 0).startswith("ERROR")
