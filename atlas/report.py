"""The daily site report: what the cameras saw, by name.

Built from the catalogue and the journal, never from a model guess: a named thing's timeline is the union of the
sightings the owner named or the matcher attached (with the match score), alerts are the rule firings, the day's
summaries are the journal rollups, and anything the pipeline itself marked as disputed, rejected or unconfirmed is
listed as such instead of being smoothed over.

    report.daily(store, desk_id, "2026-09-29") -> dict      # the data
    report.markdown(data) -> str                             # the readable version (GET /api/report/day)
"""
from __future__ import annotations

import time
from typing import Any

GAP_S = 90.0            # two sightings closer than this on one camera are one presence interval


def _day_bounds(date: str) -> tuple[float, float]:
    t0 = time.mktime(time.strptime(date, "%Y-%m-%d"))
    return t0, t0 + 86400


def _intervals(sightings: list[dict[str, Any]], gap: float = GAP_S) -> list[dict[str, Any]]:
    """Merge one camera's sightings into presence intervals: [{start, end, sightings, best_score}]."""
    out: list[dict[str, Any]] = []
    for o in sorted(sightings, key=lambda o: o.get("first_ts") or 0):
        a, b = float(o.get("first_ts") or 0), float(o.get("last_ts") or 0)
        score = 1.0 if o.get("name_by") == "owner" else float(o.get("name_score") or 0)
        if out and a - out[-1]["end"] <= gap:
            out[-1]["end"] = max(out[-1]["end"], b)
            out[-1]["sightings"] += 1
            out[-1]["best_score"] = max(out[-1]["best_score"], score)
            out[-1]["object_ids"].append(o["id"])
        else:
            out.append({"start": a, "end": b, "sightings": 1, "best_score": score, "object_ids": [o["id"]]})
    return out


def daily(store, desk_id: int, date: str, now: float | None = None) -> dict[str, Any]:
    ds = store.for_desk(desk_id)
    t0, t1 = _day_bounds(date)
    now = now or time.time()
    objs = [o for o in ds.vision_objects(since=t0, limit=5000) if (o.get("first_ts") or 0) < t1]
    events = [e for e in ds.vision_events(since=t0, limit=5000) if e["ts"] < t1]
    cams = sorted({o["camera"] for o in objs} | {e["camera"] for e in events})

    # named things: a timeline per name, per camera
    names = []
    for n in ds.named_things():
        mine = [o for o in objs if o.get("name_id") == n["id"] and o.get("name_by") != "owner-no"]
        per_cam = {}
        for cam in sorted({o["camera"] for o in mine}):
            iv = _intervals([o for o in mine if o["camera"] == cam])
            per_cam[cam] = {"intervals": iv, "seconds": round(sum(i["end"] - i["start"] for i in iv), 1)}
        all_iv = [i for c in per_cam.values() for i in c["intervals"]]
        names.append({"id": n["id"], "name": n["name"], "kind": n["kind"], "notes": n.get("notes") or "",
                      "first_seen": min((i["start"] for i in all_iv), default=None),
                      "last_seen": max((i["end"] for i in all_iv), default=None),
                      "sightings": len(mine), "confirmed": sum(1 for o in mine if o.get("name_by") == "owner"),
                      "likely": sum(1 for o in mine if o.get("name_by") == "match"),
                      "cameras": per_cam, "seconds": round(sum(c["seconds"] for c in per_cam.values()), 1)})

    # people and things nobody named: peak per camera and how many distinct tracks
    unnamed = [o for o in objs if not o.get("name_id") or o.get("name_by") == "owner-no"]
    unnamed_people = {}
    for cam in cams:
        rows = [o for o in unnamed if o["camera"] == cam and o["label"] == "person" and o.get("verdict") != "rejected"]
        unnamed_people[cam] = {"tracks": len(rows), "seconds": round(sum((o.get("last_ts") or 0) - (o.get("first_ts") or 0) for o in rows), 1)}

    alerts = [{"ts": e["ts"], "camera": e["camera"], "reason": e.get("reason") or "", "answer": (e.get("answer") or "")[:400],
               "run_id": e.get("run_id") or "", "id": e["id"]} for e in events if e.get("triggered")]
    digests = [{"ts": e["ts"], "camera": e["camera"], "text": e.get("answer") or "", "id": e["id"]} for e in events if e.get("source") == "digest"]
    notes = sum(1 for e in events if e.get("source") == "journal")
    disputed = [{"id": o["id"], "camera": o["camera"], "label": o["label"], "second_label": o.get("second_label") or "",
                 "status": o.get("status"), "verdict": o.get("verdict") or ""}
                for o in objs if o.get("verdict") == "disputed" or (o.get("verdict") == "rejected" and o.get("verdict_by") == "owner")]
    failures = [{"ts": e["ts"], "camera": e["camera"], "reason": e.get("reason") or ""} for e in events if e.get("backend") == "error"]
    return {"date": date, "generated": now, "cameras": cams, "names": names, "unnamed_people": unnamed_people,
            "alerts": alerts, "digests": digests, "journal_notes": notes, "disputed": disputed, "failures": failures,
            "objects": len(objs), "events": len(events)}


def _hm(ts: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "-"


def _mins(s: float) -> str:
    return f"{s / 60:.0f} min" if s >= 60 else f"{s:.0f} s"


def markdown(d: dict[str, Any], business: str = "") -> str:
    L = [f"# Camera report{(' - ' + business) if business else ''}, {time.strftime('%A %d %B %Y', time.strptime(d['date'], '%Y-%m-%d'))}", ""]
    L.append(f"{len(d['cameras'])} cameras: {', '.join(d['cameras']) or 'none'} · {d['objects']} tracked objects · "
             f"{d['journal_notes']} journal notes · {len(d['alerts'])} alerts")
    L.append("")
    L.append("## Named people and things")
    L.append("")
    seen = [n for n in d["names"] if n["sightings"]]
    if not seen:
        L.append("Nothing named was seen today." + (" (No names in the registry yet: open Objects and name a person or thing.)" if not d["names"] else ""))
    for n in seen:
        sure = "" if n["likely"] == 0 else f" ({n['confirmed']} confirmed by you, {n['likely']} matched by appearance)"
        L.append(f"### {n['name']} · {n['kind']}{(' · ' + n['notes']) if n['notes'] else ''}")
        L.append(f"First seen {_hm(n['first_seen'])}, last seen {_hm(n['last_seen'])}, {_mins(n['seconds'])} in view across "
                 f"{len(n['cameras'])} camera{'s' if len(n['cameras']) != 1 else ''}{sure}.")
        for cam, c in n["cameras"].items():
            spans = ", ".join(f"{_hm(i['start'])}-{_hm(i['end'])}" + ("" if i["best_score"] >= 0.999 else f" (~{i['best_score']:.0%})") for i in c["intervals"])
            L.append(f"- **{cam}**: {_mins(c['seconds'])} · {spans}")
        L.append("")
    unseen = [n["name"] for n in d["names"] if not n["sightings"]]
    if unseen:
        L.append(f"Not seen today: {', '.join(unseen)}.")
        L.append("")
    L.append("## Everyone else")
    L.append("")
    for cam, u in d["unnamed_people"].items():
        L.append(f"- **{cam}**: {u['tracks']} unnamed person track{'s' if u['tracks'] != 1 else ''}, {_mins(u['seconds'])} in view")
    L.append("")
    L.append("## Alerts")
    L.append("")
    if not d["alerts"]:
        L.append("None.")
    for a in d["alerts"]:
        L.append(f"- {_hm(a['ts'])} **{a['camera']}** · {a['reason']}" + (f" · run {a['run_id']}" if a["run_id"] else "") + f" [#{a['id']}]")
        if a["answer"]:
            L.append(f"  {a['answer'].splitlines()[0][:300]}")
    L.append("")
    L.append("## What the cameras wrote (summaries)")
    L.append("")
    if not d["digests"]:
        L.append("No rollups yet (they are written every journal_rollup_min minutes per camera).")
    for g in d["digests"]:
        L.append(f"**{_hm(g['ts'])} {g['camera']}** [#{g['id']}]  ")
        L.append(g["text"].strip())
        L.append("")
    L.append("## Not taken at face value")
    L.append("")
    if not d["disputed"] and not d["failures"]:
        L.append("Nothing disputed, nothing failed.")
    for x in d["disputed"]:
        L.append(f"- object #{x['id']} on {x['camera']}: detector said *{x['label']}*" +
                 (f", second opinion *{x['second_label']}*" if x["second_label"] else "") + f" · {x['verdict'] or x['status']}")
    for f in d["failures"][:20]:
        L.append(f"- {_hm(f['ts'])} {f['camera']}: {f['reason']}")
    L.append("")
    L.append(f"_Names come from the owner; a sighting carries a name only when it was named directly or matched by appearance "
             f"(clothing, build, colour - never a face) with the score shown. Generated {time.strftime('%H:%M', time.localtime(d['generated']))}._")
    return "\n".join(L)
