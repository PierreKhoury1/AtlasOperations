"""Is each camera agent's written account true? Cross-check the journal notes against the labels and the other cameras.

    py -m atlas mcam-notes [--cams C1,C2,C3,C6] [--instants 6] [--model google/gemini-3.1-flash-lite]

Runs the real camera agent (journal.write_note: previous note + frames + close-up + the verify pass, exactly what a live
camera writes) on several WILDTRACK cameras at the same moments, a few seconds apart so each note follows the previous
one. Every note is then checked three ways:

  1. the count, against the human labels for that camera (deterministic);
  2. every claim, against the camera's own frame (an independent judge model rereads the image);
  3. every claim about a person or the scene, against the OTHER cameras at the same instant (they see the same people
     from other sides: a coat colour, a suitcase, a direction of walking must agree).

The judge is a different, stronger model than the agent, but it is still a model: its verdicts are a second opinion,
not ground truth. The count check is the only one graded against human labels. Output under
workspace/vision-eval/wildtrack/: notes.json (every note and verdict, read by the Review page; stays local, it describes
dataset frames) and notes-report.md (the summary).
"""
from __future__ import annotations

import argparse
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import mcam_eval as M

JUDGE = "anthropic/claude-sonnet-5"
JUDGE_SYSTEM = """You fact-check a camera agent's journal note. You get the note, the frame it was written from (THIS CAMERA),
and frames of the SAME instant from other cameras that look at the same public square from other sides (the same people
appear in several of them). Split the note into its individual factual claims (at most 14; merge trivial repeats; skip
pure hedges like "nothing else visible"). For each claim decide:
- "own": "supported" (clearly visible in THIS CAMERA's frame), "contradicted" (the frame shows otherwise, or it is not
  there at all: an invented detail), or "unclear" (too small or ambiguous to tell either way);
- "others": for claims about a specific person, a group, an object or the scene: "consistent" (the other views agree,
  e.g. the same red coat or suitcase seen from another side), "contradicted" (the other views show otherwise), or
  "not_visible" (the other cameras do not show that part of the square or that person); "n/a" for claims only this
  camera can judge (exact image positions, "left of the frame").
- "kind": "count", "person" (appearance or action of one person), "group", "object", "movement" (change since the last
  note), or "scene".
Hedged claims ("appears to", "unclear") are supported when what they hedge is plausible in the frame. Be strict about
invented specifics (named items, colours, numbers). Also give "count": the TOTAL number of people the note says are in
view of the whole frame, only when it states one ("about 30 people", "25-30 people": use the midpoint); null when it only
counts a subset ("three adults in the foreground", "a group of five") or uses words ("busy", "several", "a crowd").
Reply with JSON only: {"count": <int|null>, "claims": [{"claim": "<short>", "kind": "...", "own": "...", "others": "...",
"why": "<under 12 words>"}]}"""


def _jpeg(path: Path, max_w: int = 0) -> bytes:
    from PIL import Image
    im = Image.open(path).convert("RGB")
    if max_w and im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=88)
    return b.getvalue()


def frame_path(data: Path, cam: str, frame: str) -> tuple[Path, float]:
    """The full-resolution frame when it was fetched (--hd), else the 960x540 one; and its scale vs the labels."""
    hd = data / "frames-hd" / cam / f"{frame}.jpg"
    return (hd, 2.0) if hd.is_file() else (data / "frames" / cam / f"{frame}.jpg", 1.0)


def pick_instants(D: dict[str, Any], cams: list[int], n: int, step: int) -> list[int]:
    """n instants `step` apart, starting where the most labelled people are visible on every chosen camera at once."""
    N = len(D["frames"])
    span = step * (n - 1)
    best, bi = -1, 0
    for i in range(0, max(1, N - span)):
        shared = sum(len(set.intersection(*[{p for p, _ in D["gt"][j][c]} for c in cams])) for j in range(i, i + span + 1, step))
        if shared > best:
            best, bi = shared, i
    return [bi + k * step for k in range(n)]


def judge_note(caller, judge: str, note: str, cam: str, own: bytes, others: list[tuple[str, bytes]]) -> dict[str, Any]:
    import base64
    def img(b: bytes) -> dict[str, Any]:
        return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode()}}
    content: list[dict[str, Any]] = [{"type": "text", "text": f"NOTE written by the agent of camera {cam}:\n{note}\n\nTHIS CAMERA ({cam}):"}, img(own)]
    for name, b in others:
        content += [{"type": "text", "text": f"OTHER CAMERA {name}, same instant:"}, img(b)]
    content.append({"type": "text", "text": "Return the JSON."})
    res = caller.chat(judge, JUDGE_SYSTEM, content, max_tokens=2200)
    raw = res.get("text", "")
    try:
        j = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except ValueError:
        return {"error": res.get("error") or "judge reply not parseable", "raw": raw[:300], "cost": res.get("cost")}
    claims = [c for c in (j.get("claims") or []) if isinstance(c, dict) and c.get("claim")]
    cnt = j.get("count")
    return {"count": cnt if isinstance(cnt, (int, float)) else None, "claims": claims, "cost": res.get("cost")}


def summarise(rows: list[dict[str, Any]], cams: list[str]) -> dict[str, Any]:
    def agg(rs: list[dict[str, Any]]) -> dict[str, Any]:
        cl = [c for r in rs for c in r.get("check", {}).get("claims", [])]
        own = [c.get("own") for c in cl]
        oth = [c.get("others") for c in cl if c.get("others") in ("consistent", "contradicted")]
        cnt = [(r["check"]["count"], r["labelled"]) for r in rs if r.get("check", {}).get("count") is not None]
        n = max(1, len(cl))
        return {"notes": len(rs), "claims": len(cl),
                "supported": round(own.count("supported") / n, 3), "contradicted": round(own.count("contradicted") / n, 3),
                "unclear": round(own.count("unclear") / n, 3),
                "wrong_per_note": round(own.count("contradicted") / max(1, len(rs)), 2),
                "cross_checked": len(oth), "cross_consistent": round(oth.count("consistent") / max(1, len(oth)), 3) if oth else None,
                "count_notes": len(cnt), "count_mae": round(sum(abs(a - b) for a, b in cnt) / len(cnt), 1) if cnt else None,
                "count_bias": round(sum(a - b for a, b in cnt) / len(cnt), 1) if cnt else None,
                "detector_mae": round(sum(abs(r["detected"] - r["labelled"]) for r in rs) / max(1, len(rs)), 1),
                "removed_by_verify": sum(len(r.get("removed") or []) for r in rs)}
    return {"all": agg(rows), "per_camera": {c: agg([r for r in rows if r["camera"] == c]) for c in cams}}


def write_report(out: Path, R: dict[str, Any]) -> None:
    S = R["summary"]
    L = ["# Camera agents vs the truth: WILDTRACK", "",
         f"{len(R['cameras'])} camera agents ({', '.join(R['cameras'])}) each wrote a journal note at {len(R['instants'])} shared instants, "
         f"{R['step_s']:.0f} s apart (the busiest stretch). Agent: `{R['model']}` with close-up and verify pass, exactly as a live camera. "
         f"Judge: `{R['judge']}`, shown the camera's own frame plus the other {len(R['cameras']) - 1} cameras at the same instant.", "",
         "| camera | notes | claims | true in own frame | invented / wrong | unclear | wrong per note | other cameras agree | count error (labels) | detector count error | removed by verify pass |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, a in [*S["per_camera"].items(), ("all", S["all"])]:
        cc = f"{a['cross_consistent'] * 100:.0f}% of {a['cross_checked']}" if a["cross_consistent"] is not None else "-"
        ce = f"{a['count_mae']} ({a['count_bias']:+}, {a['count_notes']} of {a['notes']} notes give a total)" if a["count_mae"] is not None else "no total given"
        L.append(f"| {'**all**' if k == 'all' else k} | {a['notes']} | {a['claims']} | {a['supported'] * 100:.0f}% | {a['contradicted'] * 100:.0f}% | "
                 f"{a['unclear'] * 100:.0f}% | {a['wrong_per_note']} | {cc} | {ce} | {a['detector_mae']} | {a['removed_by_verify']} |")
    L += ["", "Count error compares the number of people the note states with the human labels for that camera. The labels only "
          "cover people standing on the labelled square, so people on the stairs or far away that the note rightly counts "
          "make it look high: read the bias with that in mind.", "", "## What the agents got wrong", ""]
    wrong = [(r["camera"], r["frame"], c) for r in R["rows"] for c in r.get("check", {}).get("claims", []) if c.get("own") == "contradicted"]
    for cam, fr, c in wrong[:25]:
        L.append(f"- **{cam} @ {fr}**: \"{c['claim']}\" ({c.get('kind', '?')}): {c.get('why', '')}")
    if not wrong:
        L.append("- nothing the judge could contradict")
    xs = [(r["camera"], r["frame"], c) for r in R["rows"] for c in r.get("check", {}).get("claims", []) if c.get("others") == "contradicted"]
    if xs:
        L += ["", "## Where another camera disagreed", ""] + [f"- **{cam} @ {fr}**: \"{c['claim']}\": {c.get('why', '')}" for cam, fr, c in xs[:15]]
    L += ["", f"Cost: agent ${R['cost_agent']:.3f} (estimate), judge ${R['cost_judge']:.3f}. _Generated {time.strftime('%Y-%m-%d %H:%M')}. "
          "Data: WILDTRACK (Chavdarova et al., CVPR 2018), non-commercial research use. The judge is a model too: a second opinion, "
          "not ground truth._"]
    (out / "notes-report.md").write_text("\n".join(L), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas mcam-notes", description=__doc__.split("\n")[0])
    ap.add_argument("--data", default=str(Path.home() / "AtlasDemo" / "wildtrack"))
    ap.add_argument("--cams", default="C1,C2,C3,C6")
    ap.add_argument("--instants", type=int, default=6)
    ap.add_argument("--step", type=int, default=8, help="instants between notes (2 per second)")
    ap.add_argument("--model", default="google/gemini-3.1-flash-lite", help="the camera agent's vision model")
    ap.add_argument("--judge", default=JUDGE)
    ap.add_argument("--no-verify", action="store_true", help="skip the agent's own verify pass")
    ap.add_argument("--out", default=str(M.OUT_DIR))
    ap.add_argument("--rejudge", action="store_true", help="keep the notes in --out/notes.json, run only the checks again")
    a = ap.parse_args(argv)
    from . import config as cfg
    from . import journal as JR
    from .vision_eval import Caller, _key
    data, out = Path(a.data).expanduser(), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    D = M.load(data, 0)
    cams = [c.strip() for c in a.cams.split(",") if c.strip()]
    ci = [M.CAMS.index(c) for c in cams]
    preds = M.roi_filter(D, M.run_detector(D, str(cfg.DATA_DIR / "models" / "yolo11n.pt"), track=False, cache=out))
    idx = pick_instants(D, ci, a.instants, a.step)
    print(f"cameras {cams}, instants {[D['frames'][i] for i in idx]} ({a.step / 2:.0f} s apart)", flush=True)
    base, key = _key()
    caller = Caller(base, key)
    jc = {"closeup": True, "verify": not a.no_verify}
    t0 = 1_790_000_000.0                                         # a fixed clock: notes say "8 s ago", not today's date

    def agent(cam: str) -> list[dict[str, Any]]:
        """One camera's agent: its notes in order, each written from the previous one, like a live journal."""
        c = M.CAMS.index(cam)
        JR._state.pop((0, cam), None)
        rows = []
        for i in idx:
            fr = D["frames"][i]
            p, s = frame_path(data, cam, fr)
            jpg = _jpeg(p)
            dets = [{"label": "person", "box": [v * s for v in d["box"]], "conf": d["conf"]} for d in preds[i][c]]
            v_log: dict[str, Any] = {}
            orig = JR.verify_note
            def spy(text, frames, model="", transport=None):
                r = orig(text, frames, model=model, transport=transport)
                v_log.update(r)
                return r
            JR.verify_note = spy
            note, err = "", ""
            try:
                for attempt in range(3):                     # providers return the odd 502: retry, as the live journal does next tick
                    try:
                        note = JR.write_note((0, cam), cam, jc, jpg, {"person": len(dets)}, notes="a public square seen from a fixed camera",
                                             model=a.model, now=t0 + i / 2, stream=False, dets=dets)
                        err = ""
                        break
                    except Exception as exc:
                        err = f"{type(exc).__name__}: {str(exc)[:160]}"
                        time.sleep(3 * (attempt + 1))
            finally:
                JR.verify_note = orig
            rows.append({"camera": cam, "frame": fr, "instant": i, "note": note, "error": err,
                         "removed": v_log.get("removed", []), "softened": v_log.get("softened", []),
                         "labelled": len(D["gt"][i][c]), "detected": len(dets)})
            print(f"  {cam} @ {fr}: {len(note.split())} words{' ERROR ' + err if err else ''}", flush=True)
        return rows

    if a.rejudge:
        prev = json.loads((out / "notes.json").read_text())
        rows, idx = prev["rows"], [D["frames"].index(f) for f in prev["instants"]]
        a.model, a.step = prev["model"], int(prev["step_s"] * 2)
    else:
        with ThreadPoolExecutor(len(cams)) as ex:               # the agents run side by side, one per camera
            rows = [r for rs in ex.map(agent, cams) for r in rs]

    def check(r: dict[str, Any]) -> dict[str, Any]:
        if not r["note"]:
            return {"error": "no note"}
        own = _jpeg(frame_path(data, r["camera"], r["frame"])[0], 1280)
        others = [(c, _jpeg(frame_path(data, c, r["frame"])[0], 960)) for c in cams if c != r["camera"]]
        return judge_note(caller, a.judge, r["note"], r["camera"], own, others)

    with ThreadPoolExecutor(4) as ex:
        for r, chk in zip(rows, ex.map(check, rows)):
            r["check"] = chk
            a_ = chk.get("claims", [])
            print(f"  check {r['camera']} @ {r['frame']}: {sum(c.get('own') == 'supported' for c in a_)}/{len(a_)} supported, "
                  f"{sum(c.get('own') == 'contradicted' for c in a_)} wrong; count {chk.get('count')} vs labelled {r['labelled']}"
                  + (f" ERROR {chk['error']}" if chk.get("error") else ""), flush=True)
    R = {"cameras": cams, "instants": [D["frames"][i] for i in idx], "step_s": a.step / 2, "model": a.model, "judge": a.judge,
         "verify": not a.no_verify, "rows": rows, "summary": summarise(rows, cams),
         "cost_judge": round(sum(r.get("check", {}).get("cost") or 0 for r in rows), 4),
         "cost_agent": round(len(rows) * (0.0019 if not a.no_verify else 0.0011), 4)}
    (out / "notes.json").write_text(json.dumps(R, indent=1), encoding="utf-8")
    write_report(out, R)
    s = R["summary"]["all"]
    print(f"claims {s['claims']}: {s['supported'] * 100:.0f}% supported, {s['contradicted'] * 100:.0f}% wrong, "
          f"other cameras agree {s['cross_consistent']}; count error {s['count_mae']} ({s['count_bias']})", flush=True)
    print(f"-> {out / 'notes-report.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
