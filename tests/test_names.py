"""Named things: the owner names a sighting, later sightings are matched by appearance (never silently), the journal
uses the name, and the daily report tells the story per name."""
import json
import time

import numpy as np
import pytest

from atlas import journal as JR
from atlas import objects as OBJ
from atlas import report as REP
from atlas import vision as V
from test_journal import FakeVLM


def _emb(seed: float, dim: int = 8) -> bytes:
    """A unit vector; close seeds are close vectors (so 1.0 and 1.02 match, 1.0 and 5.0 do not)."""
    v = np.array([np.cos(seed + k) for k in range(dim)], dtype="float32")
    return (v / np.linalg.norm(v)).tobytes()


def _sighting(ds, camera, label="person", t0=1000.0, dur=30.0, emb=None, **f):
    f.setdefault("status", "gone")
    oid = ds.add_vision_object(camera, label=label, first_ts=t0, last_ts=t0 + dur, hits=5, best_conf=0.9, **f)
    if emb is not None:
        ds.update_vision_object(oid, emb=emb, second_label=label, second_score=0.9)
    return oid


def test_name_match_and_unname(store):
    ds = store.for_desk(1)
    a = _sighting(ds, "overview", emb=_emb(1.0))
    o = ds.vision_object(a)
    thing = OBJ.name_object(store, o, "Marco", notes="chef, black apron")
    assert thing["name"] == "Marco" and thing["kind"] == "person" and thing["label"] == "person"
    o = ds.vision_object(a)
    assert o["name_id"] == thing["id"] and o["name_by"] == "owner" and o["name_score"] == 1.0
    assert ds.object_notes(a)[0]["text"].startswith("Owner named this person: Marco")
    # a look-alike matches with its score; a different look does not; a van never matches a person's name
    m = OBJ.match_named(store, 1, _emb(1.02), "person")
    assert m and m["name"] == "Marco" and 0.86 <= m["score"] <= 1.0
    assert OBJ.match_named(store, 1, _emb(5.0), "person") is None
    assert OBJ.match_named(store, 1, _emb(1.02), "truck") is None
    # naming a second sighting adds an exemplar and moves the registry vector to the mean
    b = _sighting(ds, "counters", t0=2000.0, emb=_emb(1.1))
    OBJ.name_object(store, ds.vision_object(b), "marco")                      # case-insensitive: same Marco
    n = ds.named(thing["id"], emb=True)
    assert len(n["exemplars"]) == 2 and n["dim"] == 8 and len(ds.named_things()) == 1
    # "not him": the sighting loses the name and stops being an exemplar
    OBJ.unname_object(store, ds.vision_object(b))
    o = ds.vision_object(b)
    assert o["name_id"] == 0 and o["name_by"] == "owner-no"
    assert len(ds.named(thing["id"])["exemplars"]) == 1
    # delete the name: sightings are released
    ds.delete_named(thing["id"])
    assert ds.vision_object(a)["name_id"] == 0 and ds.named_things() == []


def test_known_in_view_feeds_the_journal_prompt(store, monkeypatch):
    ds = store.for_desk(1)
    now = time.time()
    a = _sighting(ds, "overview", t0=now - 10, dur=8, emb=_emb(1.0), status="active")
    thing = OBJ.name_object(store, ds.vision_object(a), "Marco")
    b = _sighting(ds, "overview", t0=now - 5, dur=4, emb=_emb(1.03), status="active")
    ds.update_vision_object(b, name_id=thing["id"], name_score=0.88, name_by="match")
    c = _sighting(ds, "overview", t0=now - 5, dur=4, label="truck", emb=_emb(3.0), status="active")
    OBJ.name_object(store, ds.vision_object(c), "Bidfood van", kind="vehicle")
    known = OBJ.known_in_view(store, 1, "overview", now=now)
    assert sorted(k["name"] for k in known) == ["Bidfood van", "Marco"] and all(k["sure"] and k["by"] == "owner" for k in known)
    assert OBJ.known_in_view(store, 1, "sink", now=now) == []                    # other camera: nothing known there
    assert OBJ.known_in_view(store, 1, "overview", now=now + 600) == []          # long gone
    # the note prompt carries the names and the certainty
    vlm = FakeVLM()
    monkeypatch.setattr(V, "chat_images", vlm)
    jc = JR.config({"journal": "1", "journal_verify": "0"})
    JR._state.pop((1, "overview"), None)
    JR.write_note((1, "overview"), "overview", jc, b"\xff\xd8x", {"person": 1}, now=now,
                  known=[{"name": "Marco", "kind": "person", "score": 0.88, "sure": False, "by": "match", "notes": "chef"},
                         {"name": "Bidfood van", "kind": "vehicle", "score": 1.0, "sure": True, "by": "owner", "notes": ""}])
    t = vlm.notes()[-1]["text"]
    assert "Known in view right now" in t and "Marco (person, chef) likely, 88%" in t and "Bidfood van (vehicle) certain" in t
    assert "not by face" in t


def test_daily_report_by_name(store):
    ds = store.for_desk(1)
    day = time.strftime("%Y-%m-%d")                                        # today: the alert/digest events below are stamped now
    t0 = time.mktime(time.strptime(day, "%Y-%m-%d")) + 9 * 3600          # 09:00
    a = _sighting(ds, "overview", t0=t0, dur=60, emb=_emb(1.0))
    thing = OBJ.name_object(store, ds.vision_object(a), "Marco", notes="chef")
    b = _sighting(ds, "overview", t0=t0 + 90, dur=60)                       # 30 s after a: same presence interval
    ds.update_vision_object(b, name_id=thing["id"], name_score=0.9, name_by="match")
    c = _sighting(ds, "stove", t0=t0 + 3600, dur=120)                        # an hour later on another camera
    ds.update_vision_object(c, name_id=thing["id"], name_score=0.95, name_by="match")
    _sighting(ds, "overview", t0=t0 + 200, dur=10)                          # someone unnamed
    OBJ.name_object(store, ds.vision_object(_sighting(ds, "sink", t0=t0 - 86400, emb=_emb(7.0))), "Aisha")   # yesterday
    ds.add_vision_event("overview", {"person": 3}, reason="3 people", question="", answer="Queue at the pass", triggered=True)
    ds.add_vision_event("stove", {}, reason="summary 09:00-09:15", answer="Quiet. 09:02 Marco at the hob.", source="digest")
    ds.add_vision_event("sink", {}, backend="error", reason="grab failed: camera offline")
    d = REP.daily(store, 1, day, now=t0 + 7200)
    marco = next(n for n in d["names"] if n["name"] == "Marco")
    assert marco["sightings"] == 3 and marco["confirmed"] == 1 and marco["likely"] == 2
    assert list(marco["cameras"]) == ["overview", "stove"]
    ov = marco["cameras"]["overview"]
    assert len(ov["intervals"]) == 1 and ov["intervals"][0]["sightings"] == 2 and ov["seconds"] == 150.0
    assert marco["cameras"]["stove"]["intervals"][0]["best_score"] == 0.95 and marco["seconds"] == 270.0
    assert next(n for n in d["names"] if n["name"] == "Aisha")["sightings"] == 0
    assert d["unnamed_people"]["overview"]["tracks"] == 1 and len(d["alerts"]) == 1 and len(d["digests"]) == 1
    assert d["failures"][0]["camera"] == "sink" and d["journal_notes"] == 0
    md = REP.markdown(d, "Brasserie Lumière")
    assert md.startswith("# Camera report - Brasserie Lumière, " + time.strftime("%A %d %B %Y", time.strptime(day, "%Y-%m-%d")))
    assert "### Marco · person · chef" in md and "First seen 09:00, last seen 10:02, 4 min in view across 2 cameras" in md
    assert "- **overview**: 2 min · 09:00-09:02" in md and "- **stove**: 2 min · 10:00-10:02 (~95%)" in md
    assert "Not seen today: Aisha." in md and "1 unnamed person track" in md
    assert "**overview** · 3 people" in md and "Queue at the pass" in md and "Marco at the hob" in md
    assert "camera offline" in md and "never a face" in md
    with pytest.raises(ValueError):
        REP.daily(store, 1, "29/09/2026")


def test_names_api(app_client):
    c = app_client
    c.post("/signup", json={"name": "N", "company": "Names Co", "email": "names@example.com", "password": "password1"})
    c.post("/login", json={"email": "names@example.com", "password": "password1"})
    c.post("/api/desks", json={"name": "Names Co", "template": "site_watch"})
    import atlas.desk.app as A
    desk = A.store.desks_for(A.store.user_by_email("names@example.com")["id"])[-1]
    ds = A.store.for_desk(desk["id"])
    oid = _sighting(ds, "door", emb=_emb(1.0))
    r = c.post(f"/api/objects/{oid}/name", json={"name": "  Marco  ", "kind": "person", "notes": "chef"}).get_json()
    assert r["ok"] and r["thing"]["name"] == "Marco" and r["object"]["name"] == "Marco" and r["object"]["name_by"] == "owner"
    assert c.post(f"/api/objects/{oid}/name", json={"name": ""}).status_code == 400
    assert c.post(f"/api/objects/{oid}/name", json={"name": "x", "kind": "alien"}).status_code == 400
    names = c.get("/api/names").get_json()["names"]
    assert len(names) == 1 and names[0]["sightings"] == 1 and names[0]["cameras"] == ["door"] and names[0]["exemplars"][0]["object_id"] == oid
    nid = names[0]["id"]
    lst = c.get("/api/objects?minutes=0").get_json()
    assert lst["facets"]["name"] == {"Marco": 1} and lst["names"][0]["name"] == "Marco" and lst["objects"][0]["name"] == "Marco"
    assert [o["id"] for o in c.get(f"/api/objects?minutes=0&name={nid}").get_json()["objects"]] == [oid]
    r = c.patch(f"/api/names/{nid}", json={"name": "Marco R.", "notes": "head chef"}).get_json()
    assert r["thing"]["name"] == "Marco R." and r["thing"]["notes"] == "head chef"
    today = time.strftime("%Y-%m-%d", time.localtime(1000.0))
    rep = c.get(f"/api/report/day?date={today}").get_json()
    assert rep["names"][0]["name"] == "Marco R." and rep["names"][0]["sightings"] == 1
    md = c.get(f"/api/report/day?date={today}&format=md")
    assert md.status_code == 200 and md.mimetype == "text/markdown" and "### Marco R. · person · head chef" in md.get_data(as_text=True)
    assert c.get("/api/report/day?date=nope").status_code == 400
    r = c.delete(f"/api/objects/{oid}/name").get_json()
    assert r["ok"] and r["object"]["name"] == "" and r["object"]["name_by"] == "owner-no"
    assert c.get("/api/names").get_json()["names"][0]["sightings"] == 0
    assert c.delete(f"/api/names/{nid}").get_json()["ok"] and c.get("/api/names").get_json()["names"] == []
    assert c.delete(f"/api/names/{nid}").status_code == 404
