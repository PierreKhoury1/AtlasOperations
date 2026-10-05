"""Cyber desk page (atlas/desk/static/cyber.html, cyber.css, cyber.js): static contract checks, the pure core under
Node, and the page itself in headless Chromium: REPLAY from disk, LIVE and record mode against intercepted routes.

No network anywhere: Chromium sees only file:// pages or the fake origin http://atlas.test, whose every request is
answered (or aborted) by the test. The bundles and route answers below are SYNTHETIC UI test data, shaped like the
route responses in docs/cyber-desk-spec.md section 6.2; they are not detections and appear in no film."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "atlas" / "desk" / "static"
FONTS = STATIC / "fonts"
JS = STATIC / "cyber.js"
PAGE_URI = (STATIC / "cyber.html").resolve().as_uri()
ORIGIN = "http://atlas.test"

TOKENS = {"bg": "#0b0e13", "panel": "#10151d", "line": "#222a35", "blue": "#4c90f0", "btn": "#2d72d2",
          "txt": "#dbe2ea", "dim": "#7d8896", "threat": "#e5484d", "gate": "#f2b84b"}
WOFF2 = ["IBMPlexSans-Regular.woff2", "IBMPlexSans-Medium.woff2", "IBMPlexSans-SemiBold.woff2",
         "IBMPlexSansCondensed-Medium.woff2", "IBMPlexSansCondensed-SemiBold.woff2",
         "IBMPlexMono-Regular.woff2", "IBMPlexMono-Medium.woff2"]
NVD = "This product uses the NVD API but is not endorsed or certified by the NVD."


# ---------------------------------------------------------------------------- 1. static contract
def test_page_files_exist_and_load_only_relative_assets():
    html = (STATIC / "cyber.html").read_text(encoding="utf-8")
    assert (STATIC / "cyber.css").is_file() and JS.is_file()
    assert 'href="fonts/plex.css"' in html and 'href="cyber.css"' in html and 'src="cyber.js"' in html
    assert "http://" not in html and "https://" not in html


def test_script_never_parses_markup_and_keeps_the_film_copy():
    js = JS.read_text(encoding="utf-8")
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert bad not in js, bad
    for good in ("CONTAINMENT · AWAITING APPROVAL", "Every target is in the evidence.", "Approve", "Reject",
                 "Approved · ", "Rejected · ", "__ready", "__render", NVD,
                 "Containment runs only after a person approves it."):
        assert good in js, good
    # the only absolute URL in the script is the SVG namespace identifier (never fetched)
    assert set(re.findall(r"https?://[^'\"\s)]+", js)) == {"http://www.w3.org/2000/svg"}


def test_stylesheet_tokens_exact_and_palette_closed():
    css = (STATIC / "cyber.css").read_text(encoding="utf-8")
    root = re.search(r":root\s*\{([^}]*)\}", css).group(1)
    for name, value in TOKENS.items():
        assert re.search(rf"--{name}\s*:\s*{re.escape(value)}\s*(;|$)", root, re.I), name
    # Only the nine hues: every hex colour is a token, every rgba() is a token's RGB (black only for shadows/masks).
    allowed = {tuple(int(v[i:i + 2], 16) for i in (1, 3, 5)) for v in TOKENS.values()} | {(0, 0, 0)}
    for hexc in re.findall(r"#[0-9a-fA-F]{6}\b", css):
        assert hexc.lower() in TOKENS.values(), hexc
    for r, g, b in re.findall(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,", css):
        assert (int(r), int(g), int(b)) in allowed, (r, g, b)


def test_fonts_self_hosted_with_ofl_and_linked_from_theatre():
    for name in WOFF2:
        assert (FONTS / name).read_bytes()[:4] == b"wOF2", name
    assert "SIL OPEN FONT LICENSE Version 1.1" in (FONTS / "OFL.txt").read_text(encoding="utf-8")
    css = (FONTS / "plex.css").read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*\{([^}]*)\}", css)
    want = {("IBM Plex Sans", "400"), ("IBM Plex Sans", "500"), ("IBM Plex Sans", "600"),
            ("IBM Plex Sans Condensed", "500"), ("IBM Plex Sans Condensed", "600"),
            ("IBM Plex Mono", "400"), ("IBM Plex Mono", "500")}
    got = set()
    for f in faces:
        fam = re.search(r"font-family:\s*'([^']+)'", f).group(1)
        wt = re.search(r"font-weight:\s*(\d+)", f).group(1)
        url = re.search(r"url\(([^)]+)\)", f).group(1).strip("'\"")
        assert re.search(r"font-display:\s*block", f) and "/" not in url and (FONTS / url).is_file()
        got.add((fam, wt))
    assert got == want
    assert "http" not in re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    theatre = (STATIC / "theatre.html").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/desk/static/fonts/plex.css">' in theatre
    assert re.search(r"body\{[^}]*font:[^;}]*?'IBM Plex Sans',", theatre)


# ---------------------------------------------------------------------------- 2. pure core under Node
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


def run_node(tmp_path, body: str):
    script = tmp_path / "probe.js"
    script.write_text("const C = require(%s);\nconst out = (() => {\n%s\n})();\nprocess.stdout.write(JSON.stringify(out));\n"
                      % (json.dumps(str(JS)), body), encoding="utf-8")
    r = subprocess.run([NODE, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_mask_text_masks_public_addresses_only(tmp_path):
    kept = ["10.1.2.3", "172.16.5.4", "172.31.255.1", "192.168.202.140", "127.0.0.1", "169.254.1.1", "100.64.0.1",
            "0.0.0.0", "224.0.0.251", "255.255.255.255", "fe80::65ca:c6cd:7ae0:ac8c", "fd00::5", "::1", "::",
            "Scan::Port_Scan", "SSH::Password_Guessing", "18:25:25", "v1.2.3.4.5", "ip-172-31-27-153", "00:11:22:33:44:55"]
    masked = {"61.197.203.243": "61.197.x.x", "172.32.0.1": "172.32.x.x", "100.128.0.1": "100.128.x.x",
              "8.8.8.8": "8.8.x.x", "2001:db8::1": "2001:db8:x:x::", "2a00:1450:4009:81f::200e": "2a00:1450:x:x::",
              "Invalid user admin from 220.99.93.50 port 22": "Invalid user admin from 220.99.x.x port 22",
              "61.197.203.243, 192.168.25.103 and 188.87.35.25.": "61.197.x.x, 192.168.25.103 and 188.87.x.x.",
              "[2001:db8::7]:443": "[2001:db8:x:x::]:443"}
    out = run_node(tmp_path, "return {kept: %s.map(s => C.maskText(s, true)), masked: %s.map(s => C.maskText(s, true)),"
                             " off: C.maskText('61.197.203.243', false), nul: C.maskText(null, true)};"
                   % (json.dumps(kept), json.dumps(list(masked))))
    assert out["kept"] == kept
    assert out["masked"] == list(masked.values())
    assert out["off"] == "61.197.203.243" and out["nul"] == ""


@needs_node
def test_split_citations(tmp_path):
    out = run_node(tmp_path, "return C.splitCitations('A [#12] b [cam #7][cam#8] [#x] [#003]');")
    assert out == [{"text": "A "}, {"cite": "sec", "id": 12, "label": "[#12]"}, {"text": " b "},
                   {"cite": "cam", "id": 7, "label": "[cam #7]"}, {"cite": "cam", "id": 8, "label": "[cam #8]"},
                   {"text": " [#x] "}, {"cite": "sec", "id": 3, "label": "[#3]"}]


@needs_node
def test_decision_line_card_copy_and_order(tmp_path):
    ts = 1790900000.0
    utc = datetime.fromtimestamp(ts, timezone.utc)
    tokyo = (utc + timedelta(hours=9)).strftime("%H:%M:%S")                # Asia/Tokyo: fixed +09:00
    out = run_node(tmp_path, """
      const a = s => ({id: 17, status: s, decided_at: %r, decided_by: 'Pierre', connector: 'lab-firewall'});
      return {
        sent: C.decisionLine(a('sent'), 'Asia/Tokyo'), approved: C.decisionLine(a('approved'), 'UTC'),
        rejected: C.decisionLine(a('rejected'), 'UTC'), failed: C.decisionLine(a('failed'), 'UTC'),
        pending: C.decisionLine(a('pending'), 'UTC'), badtz: C.decisionLine(a('sent'), 'Not/AZone'),
        heads: ['pending', 'sent', 'approved', 'rejected', 'failed'].map(C.cardHeader),
        conn: ['ready', 'none', 'missing', 'auto_on', 'not_http'].map(s => C.connectorLine({connector: 'lab-firewall', connector_status: s})),
        order: C.orderCards([{id: 1, status: 'sent', created: 1, decided_at: 50}, {id: 2, status: 'pending', created: 2},
                             {id: 3, status: 'pending', created: 3}, {id: 4, status: 'rejected', created: 4, decided_at: 60}]).map(x => x.id),
        list: [C.actionsOf({actions: [1]}), C.actionsOf({containment: [2]}), C.actionsOf([3]), C.actionsOf(null)],
      };""" % ts)
    hms = utc.strftime("%H:%M:%S")
    assert out["sent"] == f"Approved · {tokyo} · Pierre"
    assert out["approved"] == f"Approved · {hms} · Pierre" and out["failed"] == f"Approved · {hms} · Pierre"
    assert out["rejected"] == f"Rejected · {hms} · Pierre"
    assert out["pending"] == "" and out["badtz"] == f"Approved · {hms} · Pierre"
    assert out["heads"] == ["CONTAINMENT · AWAITING APPROVAL", "CONTAINMENT · APPROVED", "CONTAINMENT · APPROVED",
                            "CONTAINMENT · REJECTED", "CONTAINMENT · FAILED"]
    assert out["conn"] == ["Executes via lab-firewall after approval",
                           "No containment connector · approval records a simulated action",
                           "Connector lab-firewall not found · approval records a simulated action",
                           "Connector lab-firewall runs without approval · not used · simulated",
                           "Connector lab-firewall is not HTTP · simulated"]
    assert out["order"] == [3, 2, 4]
    assert out["list"] == [[1], [2], [3], []]


@needs_node
def test_time_formats_match_the_python_side(tmp_path):
    out = run_node(tmp_path, "return [C.fmtTs(1331994600), C.fmtTs(1331994600.9, false), C.fmtTs(null), C.fmtTs('x'),"
                             " C.fmtClock(1331994600, false), C.fmtRange(1331994600, 1332015300),"
                             " C.fmtRange(1322722800, 1323836400, false), C.fmtInt(1234567), C.fmtDur(172.5)];")
    assert out == ["2012-03-17 14:30:00Z", "Mar 17 14:30:00Z", "", "", "Mar 17 14:30:00",
                   "2012-03-17 14:30 → 20:15", "Dec 01 07:00 → Dec 14 04:20", "1,234,567", "2m 53s"]


@needs_node
def test_state_at_picks_frames_and_interpolates_the_clock(tmp_path):
    out = run_node(tmp_path, """
      const b = {kind: 'atlas-cyber-recording', version: 1, duration: 10, frames: [
        {t: 3.0, kind: 'timeline', data: {last_ts: 300, n: 'B'}},
        {t: 0.0, kind: 'config', data: {n: 'cfg'}},
        {t: 1.0, kind: 'timeline', data: {last_ts: 100, n: 'A'}},
        {t: 2.0, kind: 'graph', data: {nodes: [{id: 'ip:10.0.0.1'}], edges: []}},
        {t: 2.5, kind: 'ui', data: {action: 'approve', id: 17}},
        {t: 2.0, kind: 'graph', data: {nodes: [{id: 'ip:10.0.0.1'}, {id: 'ip:10.0.0.2'}], edges: []}},
        {t: 4.0, kind: 'bogus', data: {}}]};
      const s = t => { const x = C.stateAt(b, t); return {tl: x.timeline && x.timeline.n, cfg: x.config && x.config.n,
        g: x.graph ? x.graph.nodes.length : null, clock: x.clock, ui: x.ui.length, at: x.at.timeline}; };
      const p = C.prepare(b);
      return {s0: s(0), s05: s(0.5), s1: s(1), s2: s(2), s24: s(2.4), s25: s(2.5), s3: s(3), s9: s(9),
              first1: p.first.get('n:ip:10.0.0.1'), first2: p.first.get('n:ip:10.0.0.2'), frames: p.frames.length,
              again: JSON.stringify(C.stateAt(b, 2.2).graph) === JSON.stringify(C.stateAt(b, 2.2).graph),
              fade: [C.appear(p.first, 'n:ip:10.0.0.2', 2.0), C.appear(p.first, 'n:ip:10.0.0.2', 2.0 + C.ANIM_S), C.appear(p.first, 'zz', 0)]};""")
    assert out["s0"] == {"tl": None, "cfg": "cfg", "g": None, "clock": None, "ui": 0, "at": None}
    assert out["s05"]["tl"] is None and out["s1"]["tl"] == "A" and out["s1"]["clock"] == 100
    assert out["s2"]["clock"] == 200 and out["s2"]["g"] == 2          # same-t frames keep input order: the later wins
    assert out["s24"]["ui"] == 0 and out["s25"]["ui"] == 1
    assert out["s3"]["tl"] == "B" and out["s3"]["clock"] == 300 and out["s9"]["clock"] == 300
    assert out["first1"] == 2.0 and out["first2"] == 2.0 and out["frames"] == 6 and out["again"]
    assert out["fade"] == [0, 1, 1]


@needs_node
def test_layout_graph_is_deterministic_and_layered(tmp_path):
    out = run_node(tmp_path, """
      const n = (id, type, props) => ({id, type, label: id.split(':').slice(1).join(':'), props});
      const nodes = [
        n('ip:192.168.202.140', 'ip', {role: 'source', events: 5000, score: 30, threat: true, internal: true}),
        n('ip:192.168.202.79', 'ip', {role: 'source', events: 3000, score: 18}),
        n('sig:ET TROJAN Meterpreter', 'sig', {count: 860, score: 2.9, severity: 'critical'}),
        n('user:root', 'user', {attempts: 40, score: 1.6}),
        n('ip:192.168.25.103', 'ip', {role: 'target', events: 700, score: 9}),
        n('host:lab-01', 'host', {role: 'both', events: 12, score: 9, sensor: true}),
        n('camera:office', 'camera', {events: 4})];
      const g = {nodes, edges: []};
      const shuffled = {nodes: [nodes[6], nodes[3], nodes[0], nodes[5], nodes[2], nodes[4], nodes[1]], edges: []};
      const a = C.layoutGraph(g, 880, 520), b = C.layoutGraph(g, 880, 520), c = C.layoutGraph(shuffled, 880, 520);
      return {same: JSON.stringify(a) === JSON.stringify(b), order: JSON.stringify(a) === JSON.stringify(c), a,
              empty: C.layoutGraph({nodes: [], edges: []}, 100, 100)};""")
    a = out["a"]
    assert out["same"] and out["order"] and out["empty"] == {}
    x = {k: v["x"] for k, v in a.items()}
    assert x["ip:192.168.202.140"] == x["ip:192.168.202.79"] < x["sig:ET TROJAN Meterpreter"] == x["user:root"]
    assert x["user:root"] < x["ip:192.168.25.103"] == x["host:lab-01"] < x["camera:office"]
    # inside a column: score desc, then id; evenly spread; radius grows with log10(1 + events)
    assert a["ip:192.168.202.140"]["y"] < a["ip:192.168.202.79"]["y"]
    assert a["host:lab-01"]["y"] < a["ip:192.168.25.103"]["y"]            # tie on score: id order
    assert a["ip:192.168.202.140"]["r"] > a["ip:192.168.25.103"]["r"] > a["host:lab-01"]["r"]
    assert a["ip:192.168.202.140"]["side"] == "l" and a["camera:office"]["side"] == "r"


# ---------------------------------------------------------------------------- 3. the page in headless Chromium
S0 = 1331994600.0          # 2012-03-17 14:30:00 UTC
DECIDED = 1790946131.0


def _timeline(last_ts, marks=()):
    counts = [0, 3, 9, 20, 41, 30, 12, 0, 0, 0, 0, 0]
    return {"since": S0, "until": S0 + 3600, "bins": 12, "bin_s": 300.0, "year_assumed": False,
            "lanes": [{"sensor": "snort", "source": "snort_fast", "total": sum(counts), "counts": counts,
                       "alerts": [0, 0, 2, 5, 9, 4, 1, 0, 0, 0, 0, 0]},
                      {"sensor": "zeek", "source": "zeek", "total": 6, "counts": [1, 2, 3] + [0] * 9, "alerts": [0] * 12}],
            "marks": list(marks),
            "spans": [{"id": "ids_high:c2:192.168.202.140:192.168.25.103", "rule": "ids_high", "severity": "critical",
                       "first_ts": S0 + 900, "last_ts": S0 + 1900, "title": "synthetic span"}],
            "cameras": [{"camera": "office", "total": 2, "counts": [0, 1, 1] + [0] * 9,
                         "marks": [{"id": 7, "ts": S0 + 400, "reason": "person at the desk (synthetic)"}]}],
            "replays": [{"id": 1, "name": "Replay of a synthetic UI test · 60×", "speed": 60, "enabled": True,
                         "done": False, "inserted": 116, "last_ts": last_ts, "files": ["synthetic.log"]}],
            "last_ts": last_ts, "now": 1790911021.5}


MARK = {"id": 101, "t": "2012-03-17 14:45:00Z", "ts": S0 + 900, "sensor": "snort", "kind": "ids_alert",
        "src": "192.168.202.140", "dst": "192.168.25.103", "sig": "synthetic signature", "sev": "critical", "msg": "synthetic"}
GRAPH = {"nodes": [
    {"id": "ip:192.168.202.140", "type": "ip", "label": "192.168.202.140",
     "props": {"internal": True, "role": "source", "events": 90, "out": 90, "in": 0, "sensors": ["snort", "zeek"],
               "rules": ["ids_high"], "score": 12.0, "threat": True}},
    {"id": "ip:61.197.203.243", "type": "ip", "label": "61.197.203.243",
     "props": {"internal": False, "role": "source", "events": 9, "out": 9, "in": 0, "sensors": ["zeek"], "rules": [],
               "score": 1.0, "threat": False}},
    {"id": "sig:synthetic signature", "type": "sig", "label": "synthetic signature <b>x</b>",
     "props": {"source": "snort_fast", "severity": "critical", "count": 40, "score": 1.6}},
    {"id": "ip:192.168.25.103", "type": "ip", "label": "192.168.25.103",
     "props": {"internal": True, "role": "target", "events": 60, "out": 0, "in": 60, "sensors": ["snort"],
               "rules": ["ids_high"], "score": 9.0, "threat": False}}],
    "edges": [
    {"src": "ip:192.168.202.140", "dst": "ip:192.168.25.103", "kind": "alert", "evidence": [101, 102], "first": S0,
     "last": S0 + 1900, "count": 40, "severity": "critical"},
    {"src": "ip:192.168.202.140", "dst": "sig:synthetic signature", "kind": "triggered", "evidence": [101], "first": S0,
     "last": S0 + 1900, "count": 40, "severity": "critical"},
    {"src": "ip:61.197.203.243", "dst": "ip:192.168.25.103", "kind": "failed_login", "evidence": [55], "first": S0,
     "last": S0 + 60, "count": 9, "severity": "low"}],
    "truncated": False, "totals": {"nodes": 4, "edges": 3}, "window": {"since": S0, "until": S0 + 3600, "events": 116}}
DETS = {"detections": [
    {"id": "ids_high:c2:192.168.202.140:192.168.25.103", "rule": "ids_high", "severity": "critical",
     "title": "C2 traffic (synthetic) between 192.168.202.140 and 192.168.25.103: 40 alerts", "src": [], "dst": [],
     "users": [], "sources": ["snort_fast"], "sensors": ["snort"], "evidence": [101, 102], "evidence_total": 40,
     "counts": {}, "details": {"year_assumed": False}, "first_ts": S0 + 900, "last_ts": S0 + 1900, "row_id": 1,
     "run_id": "", "created": 1.0, "updated": 1.0},
    {"id": "scan:61.197.203.243", "rule": "scan", "severity": "low", "title": "<b>bold</b> synthetic scan title",
     "src": [], "dst": [], "users": [], "sources": ["zeek"], "sensors": ["zeek"], "evidence": [55], "evidence_total": 9,
     "counts": {}, "details": {"year_assumed": True}, "first_ts": S0, "last_ts": S0 + 60, "row_id": 2, "run_id": "",
     "created": 1.0, "updated": 1.0}],
    "total": 2, "by_severity": {"critical": 1, "high": 0, "medium": 0, "low": 1, "info": 0}, "trigger_status": None}
CONFIG = {"site_map": {}, "mask_public_ips": False, "trigger_min": "high", "params": {},
          "hook_url": "http://atlas.test/hook/tok-SYNTHETIC-1234/logs", "formats": [],
          "connectors": [], "jobs": [], "notices": {"nvd": NVD, "dbip": ""}}


def _action(status="pending", target="61.197.203.243"):
    a = {"id": 17, "status": status, "created": 1790911150.0, "decided_at": None, "decided_by": "", "note": "",
         "agent": "responder", "run_id": "r1", "question": f"Block {target}?",
         "targets": [{"kind": "ip", "value": target, "action": "block"}], "evidence": [55, 101],
         "connector": "lab-firewall", "connector_status": "ready", "justification": "Synthetic justification.",
         "policy": {"ok": True, "line": "Every target is in the evidence.", "violations": []}, "evidence_events": []}
    if status != "pending":
        a.update(decided_at=DECIDED, decided_by="Pierre",
                 note=f"[containment via lab-firewall: block {target} → HTTP 200]")
    return a


INCIDENT = {"run": {"id": "20261002-test", "status": "done", "active": False, "created": 1790911100.0,
                    "ended": 1790911190.0, "mode": "alert_triage", "title": "SECURITY DETECTION — 1 new detection(s) on Test desk"},
            "summary": ("Synthetic UI test summary.\n- 192.168.202.140 talked to 192.168.25.103 [#101] [#999] [cam #7].\n"
                        "- Markup stays text: <img src=x onerror=\"window.__pwned=1\"> **bold run** [#101]"),
            "verified": "Verified by the desk (not model-claimed): synthetic footer.",
            "citations": [{"kind": "sec", "id": 101, "found": True, "event": {"id": 101, "t": "2012-03-17 14:45:00Z",
                                                                             "sensor": "snort", "kind": "ids_alert", "src": "192.168.202.140",
                                                                             "dst": "192.168.25.103", "sev": "critical", "msg": "synthetic"}},
                          {"kind": "sec", "id": 999, "found": False, "event": None},
                          {"kind": "cam", "id": 7, "found": True, "event": {"id": 7, "camera": "office", "ts": S0 + 400,
                                                                            "reason": "synthetic", "answer": ""}}],
            "activity": [], "actions": [17], "detections": ["ids_high:c2:192.168.202.140:192.168.25.103"]}


def _bundle(mask=False):
    """SYNTHETIC recording for the REPLAY tests."""
    frames = [
        {"t": 0.0, "kind": "config", "data": dict(CONFIG, mask_public_ips=mask)},
        {"t": 0.5, "kind": "timeline", "data": _timeline(S0 + 900, [MARK])},
        {"t": 0.8, "kind": "graph", "data": GRAPH},
        {"t": 1.0, "kind": "detections", "data": DETS},
        {"t": 1.2, "kind": "incident", "data": {"run": None}},
        {"t": 1.4, "kind": "containment", "data": {"actions": [_action()], "other_pending": 2}},
        {"t": 4.5, "kind": "timeline", "data": _timeline(S0 + 1800, [MARK])},
        {"t": 5.0, "kind": "incident", "data": INCIDENT},
        {"t": 6.0, "kind": "ui", "data": {"action": "approve", "id": 17}},
        {"t": 6.5, "kind": "containment", "data": {"actions": [_action("sent")], "other_pending": 2}},
    ]
    return {"kind": "atlas-cyber-recording", "version": 1, "name": "synthetic-ui-test", "created": 1790911021.5,
            "desk": {"id": 3, "name": "Test desk"}, "tz": "Asia/Tokyo", "viewport": [1920, 1080],
            "mask_public_ips": mask, "query": {}, "duration": 8.0, "frames": frames}


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        b = pw.chromium.launch()
    except Exception as exc:                                   # Playwright present, browser not installed
        pw.stop()
        pytest.skip(f"chromium is not available: {exc}")
    yield b
    b.close()
    pw.stop()


def _replay_page(browser, bundle, query="?replay=__test", size=(1920, 1080)):
    page = browser.new_page(viewport={"width": size[0], "height": size[1]})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(PAGE_URI + query)
    assert page.evaluate("b => window.__loadBundle(b)", bundle) is True
    page.evaluate("() => window.__ready")
    return page, errors


def _text(page, sel):
    return page.evaluate("s => { const n = document.querySelector(s); return n ? n.textContent : null; }", sel)


def test_replay_render_is_a_pure_function_of_t(browser):
    page, errors = _replay_page(browser, _bundle())
    # From here on nothing may read the clock, randomness or the network.
    page.evaluate("""() => { window.__calls = [];
        const spy = (o, k) => { const f = o[k]; o[k] = function () { window.__calls.push(k); return f.apply(this, arguments); }; };
        spy(Date, 'now'); spy(Math, 'random'); spy(window, 'fetch'); spy(performance, 'now'); }""")
    snap = "t => { if (window.__render(t) !== true) throw new Error('render'); return document.body.outerHTML; }"
    first = page.evaluate(snap, 5.2)
    page.evaluate(snap, 0.0)
    page.evaluate(snap, 7.9)
    again = page.evaluate(snap, 5.2)
    page.evaluate(snap, 1.3)
    assert first == again == page.evaluate(snap, 5.2)
    assert page.evaluate(snap, 0.0) != first
    assert page.evaluate("() => window.__calls") == []
    assert page.evaluate("() => window.__duration") == 8.0
    fonts = page.evaluate("() => [...document.fonts].filter(f => f.status === 'loaded').length")
    assert fonts == 7
    assert errors == []
    page.close()


def test_replay_shows_the_contract_copy_and_motion(browser):
    page, errors = _replay_page(browser, _bundle())
    r = lambda t: page.evaluate("t => window.__render(t)", t)
    r(2.5)                                                     # clock interpolates between timeline frames (0.5, 4.5)
    assert _text(page, "#clock") == "2012-03-17 14:52:30"
    assert _text(page, "#mode-text") == "REPLAY" and _text(page, "#desk-name") == "Test desk"
    assert _text(page, "#replay-label") == "Replay of a synthetic UI test · 60×"
    assert _text(page, "#c-events") == "121" and _text(page, "#c-high") == "21" and _text(page, "#c-dets") == "2"
    assert _text(page, ".card .state") == "CONTAINMENT · AWAITING APPROVAL"
    assert _text(page, ".card-q") == "Block 61.197.203.243?"
    assert _text(page, ".card-policy") == "Every target is in the evidence."
    assert _text(page, ".card-conn") == "Executes via lab-firewall after approval"
    assert _text(page, ".card .approve") == "Approve" and _text(page, ".card .reject") == "Reject"
    assert page.evaluate("() => [...document.querySelectorAll('.card-ev .cite')].map(n => n.textContent)") == ["[#55]", "[#101]"]
    assert _text(page, "#f-other") == "2 other approvals waiting" and _text(page, "#f-nvd") == NVD
    assert "Containment runs only after a person approves it." in _text(page, "#rail-foot")
    assert _text(page, "#inc-empty .e1") is not None and page.evaluate("() => document.querySelector('#inc-wrap').hidden")
    r(1.45)                                                    # a card fades in from its first frame (1.4)
    op = float(page.evaluate("() => getComputedStyle(document.querySelector('.card')).opacity"))
    assert 0 < op < 1
    r(6.1)                                                     # the recorded click shows pressed for 250 ms
    assert page.evaluate("() => document.querySelector('.card .approve').classList.contains('pressed')")
    r(6.3)
    assert not page.evaluate("() => document.querySelector('.card .approve').classList.contains('pressed')")
    r(7.5)
    assert _text(page, ".card .state") == "CONTAINMENT · APPROVED"
    tokyo = (datetime.fromtimestamp(DECIDED, timezone.utc) + timedelta(hours=9)).strftime("%H:%M:%S")
    assert _text(page, ".card-log") == f"Approved · {tokyo} · Pierre"
    assert _text(page, ".card-result") == "[containment via lab-firewall: block 61.197.203.243 → HTTP 200]"
    assert page.evaluate("() => document.querySelectorAll('.card .approve').length") == 0
    assert errors == []
    page.close()


def test_replay_escapes_data_and_resolves_citations(browser):
    page, errors = _replay_page(browser, _bundle())
    page.evaluate("() => window.__render(7.0)")
    assert page.evaluate("() => document.querySelectorAll('#inc-body img, #d-list b, #gr-svg b').length") == 0
    assert page.evaluate("() => window.__pwned") is None
    body = _text(page, "#inc-body")
    assert '<img src=x onerror="window.__pwned=1">' in body and "**" not in body
    assert _text(page, "#inc-body strong") == "bold run"
    chips = page.evaluate("() => [...document.querySelectorAll('#inc-body .cite')].map(n => [n.textContent, n.classList.contains('off')])")
    assert chips == [["[#101]", False], ["[#999]", True], ["[cam #7]", False], ["[#101]", False]]
    assert "<b>bold</b> synthetic scan title" in _text(page, "#d-list")
    assert "Mar 17 14:30" in _text(page, "#d-list") and "2012-03-17 14:45 → 15:01" in _text(page, "#d-list")
    labels = page.evaluate("() => [...document.querySelectorAll('#gr-svg text.glab')].map(n => n.textContent)")
    assert "synthetic signature <b>x</b>" in labels
    assert _text(page, "#inc-title") == "SECURITY DETECTION — 1 new detection(s) on Test desk"
    assert _text(page, "#inc-verified") == "Verified by the desk (not model-claimed): synthetic footer."
    # a mark for every high+ event in its lane and a span for the detection
    assert page.evaluate("() => document.querySelectorAll('#tl-svg .mark').length") == 2      # snort mark + camera mark
    assert page.evaluate("() => document.querySelectorAll('#tl-svg .span.sev-critical').length") == 1
    assert errors == []
    page.close()


def test_replay_survives_odd_shapes_and_drops_the_year_when_unknown(browser):
    b = _bundle()
    tl = _timeline(S0 + 1800, [MARK])
    tl["year_assumed"] = True
    long_label = "L" * 60
    graph = json.loads(json.dumps(GRAPH))
    graph["nodes"][2]["label"] = long_label
    dets = json.loads(json.dumps(DETS))
    dets["detections"][1]["severity"] = "high onmouseover"            # class names never come raw from data
    odd = dict(_action(), id=31, status="sent now")
    b["frames"] += [{"t": 7.0, "kind": "timeline", "data": tl}, {"t": 7.0, "kind": "graph", "data": graph},
                    {"t": 7.0, "kind": "detections", "data": dets},
                    {"t": 7.0, "kind": "containment", "data": {"actions": [odd], "other_pending": 1}}]
    page, errors = _replay_page(browser, b)
    page.evaluate("() => window.__render(7.9)")
    assert _text(page, "#clock") == "Mar 17 15:00:00"
    assert "YEAR NOT IN LOG" in _text(page, "#tl-window") and "2012" not in _text(page, "#tl-window")
    labels = page.evaluate("() => [...document.querySelectorAll('#gr-svg text.glab')].map(n => n.textContent)")
    assert "L" * 27 + "…" in labels and long_label not in labels
    assert page.evaluate("() => document.querySelector('#d-list li:nth-child(2) .sev').className") == "sev info"
    assert _text(page, ".card .state") == "CONTAINMENT · SENT NOW"
    assert _text(page, "#f-other") == "1 other approval waiting"
    assert errors == []
    page.close()


def test_replay_masking_follows_the_bundle_and_the_query(browser):
    page, _ = _replay_page(browser, _bundle(mask=True))
    page.evaluate("() => window.__render(2.0)")
    assert _text(page, ".card-q") == "Block 61.197.x.x?"
    labels = page.evaluate("() => [...document.querySelectorAll('#gr-svg text.glab')].map(n => n.textContent)")
    assert "61.197.x.x" in labels and "192.168.202.140" in labels
    page.close()
    page, _ = _replay_page(browser, _bundle(mask=True), "?replay=__test&mask=0")
    page.evaluate("() => window.__render(2.0)")
    assert _text(page, ".card-q") == "Block 61.197.203.243?"
    page.close()
    page, _ = _replay_page(browser, _bundle(mask=False), "?replay=__test&mask=1")
    page.evaluate("() => window.__render(2.0)")
    assert _text(page, ".card-q") == "Block 61.197.x.x?"
    page.close()


@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720)])
def test_layout_fits_without_scrolling(browser, size):
    page, _ = _replay_page(browser, _bundle(), size=size)
    page.evaluate("() => window.__render(7.0)")
    dims = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
    assert dims == [size[0], size[1]]
    page.close()


def test_three_cards_still_fit_the_rail(browser):
    b = _bundle()
    many = [dict(_action(), id=21, created=1790911300.0, question="Block 61.197.203.243 and 220.99.93.50?",
                 evidence=list(range(1, 31))),
            dict(_action(), id=20, created=1790911200.0), dict(_action("rejected"), id=19)]
    b["frames"].append({"t": 7.0, "kind": "containment", "data": {"actions": many, "other_pending": 0}})
    page, errors = _replay_page(browser, b)
    page.evaluate("() => window.__render(7.9)")
    assert page.evaluate("() => [...document.querySelectorAll('.card .card-id')].map(n => n.textContent)") == ["#21", "#20", "#19"]
    first = page.evaluate("() => [...document.querySelectorAll('#cards .card:nth-child(1) .card-ev > *')].map(n => n.textContent)")
    assert first == ["[#1]", "[#2]", "[#3]", "[#4]", "+26"]                    # three cards: one row of chips, then +N
    visible = page.evaluate("""() => { const box = document.querySelector('#cards .card:nth-child(1) .card-ev').getBoundingClientRect();
        const more = document.querySelector('#cards .card:nth-child(1) .card-ev .more').getBoundingClientRect();
        return more.bottom <= box.bottom + 0.5 && more.right <= box.right + 0.5; }""")
    assert visible
    fit = page.evaluate("""() => { const rail = document.querySelector('#rail').getBoundingClientRect();
        const foot = document.querySelector('#rail-foot').getBoundingClientRect();
        return [foot.bottom <= rail.bottom + 0.5, foot.bottom <= innerHeight, document.documentElement.scrollHeight === innerHeight]; }""")
    assert fit == [True, True, True]
    assert errors == []
    page.close()


# ---------------------------------------------------------------------------- LIVE and record mode, intercepted
CTYPE = {".html": "text/html", ".css": "text/css", ".js": "application/javascript", ".woff2": "font/woff2"}


class FakeDesk:
    """Answers the page's requests on http://atlas.test; aborts anything else (so nothing reaches the network)."""

    def __init__(self, status=None):
        self.status = status or {}                 # path -> forced HTTP status
        self.action = _action()
        self.requests = []
        self.foreign = []

    def handle(self, route):
        req = route.request
        u = urlparse(req.url)
        if u.scheme + "://" + u.netloc != ORIGIN:
            self.foreign.append(req.url)
            return route.abort()
        self.requests.append((req.method, u.path, parse_qs(u.query), req.post_data, dict(req.headers)))
        if u.path.startswith("/desk/static/"):
            f = STATIC / u.path[len("/desk/static/"):]
            if f.is_file():
                return route.fulfill(status=200, body=f.read_bytes(), headers={"Content-Type": CTYPE.get(f.suffix, "application/octet-stream")})
            return route.fulfill(status=404, body="")
        if u.path == "/login":
            return route.fulfill(status=200, body="<!doctype html><title>login</title>", headers={"Content-Type": "text/html"})
        if u.path in self.status:
            return route.fulfill(status=self.status[u.path], body='{"error": "x"}', headers={"Content-Type": "application/json"})
        data = {
            "/api/config": {"desk": {"id": 3, "name": "desk-3"}, "business": {"name": "Security operations"}},
            "/api/cyber/config": CONFIG,
            "/api/cyber/timeline": _timeline(S0 + 1800, [MARK]),
            "/api/cyber/graph": GRAPH,
            "/api/cyber/detections": DETS,
            "/api/cyber/incident": INCIDENT,
            "/api/cyber/containment": {"actions": [self.action], "other_pending": 0},
        }.get(u.path)
        if req.method == "POST" and re.fullmatch(r"/api/actions/\d+/decide", u.path):
            self.action = _action("sent")
            return route.fulfill(status=200, body=json.dumps(self.action), headers={"Content-Type": "application/json"})
        if req.method == "POST" and u.path == "/api/cyber/recordings":
            body = json.loads(req.post_data)
            return route.fulfill(status=200, body=json.dumps({"ok": True, "name": body["name"], "size": len(req.post_data)}),
                                 headers={"Content-Type": "application/json"})
        if data is None:
            return route.fulfill(status=404, body='{"error": "not found"}', headers={"Content-Type": "application/json"})
        return route.fulfill(status=200, body=json.dumps(data), headers={"Content-Type": "application/json"})

    def calls(self, method, path):
        return [r for r in self.requests if r[0] == method and r[1] == path]


def _live_page(browser, desk, query=""):
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("**/*", desk.handle)
    page.goto(ORIGIN + "/desk/static/cyber.html" + query)
    return page, errors


def test_live_polls_the_routes_and_approves_through_the_gate(browser):
    desk = FakeDesk()
    page, errors = _live_page(browser, desk, "?since=2012-03-17T14:30:00Z&until=2012-03-17T15:30:00Z")
    page.evaluate("() => window.__ready")
    assert _text(page, "#mode-text") == "LIVE" and _text(page, "#desk-name") == "Security operations"
    assert _text(page, "#clock") == "2012-03-17 15:00:00"
    assert _text(page, ".card .state") == "CONTAINMENT · AWAITING APPROVAL"
    for kind in ("timeline", "graph", "detections"):           # the page's window goes to the windowed routes only
        q = desk.calls("GET", f"/api/cyber/{kind}")[0][2]
        assert q == {"since": ["2012-03-17T14:30:00Z"], "until": ["2012-03-17T15:30:00Z"]}, kind
    for kind in ("containment", "incident", "config"):
        assert desk.calls("GET", f"/api/cyber/{kind}")[0][2] == {}, kind
    page.click(".card .approve")
    page.wait_for_function("() => document.querySelector('.card .state').textContent === 'CONTAINMENT · APPROVED'")
    (method, path, _q, body, headers), = desk.calls("POST", "/api/actions/17/decide")
    assert json.loads(body) == {"status": "approved"} and headers.get("content-type") == "application/json"
    assert _text(page, ".card-result") == "[containment via lab-firewall: block 61.197.203.243 → HTTP 200]"
    # a found citation chip highlights its timeline mark and the graph edges that carry it as evidence
    page.click("#inc-body .cite:not(.off)")
    assert page.evaluate("() => document.querySelectorAll('#tl-svg .mark-hl').length") == 1
    assert page.evaluate("() => document.querySelectorAll('#gr-svg path.edge.lit').length") == 2
    assert desk.foreign == [] and errors == []
    page.close()


def test_live_reject_sends_only_the_status(browser):
    desk = FakeDesk()
    page, _ = _live_page(browser, desk)
    page.evaluate("() => window.__ready")
    page.click(".card .reject")
    page.wait_for_function("() => document.querySelectorAll('.card .reject').length === 0 || window.__rejected")
    (_m, _p, _q, body, _h), = desk.calls("POST", "/api/actions/17/decide")
    assert json.loads(body) == {"status": "rejected"}
    page.close()


def test_live_401_goes_to_login_and_409_says_no_desk(browser):
    desk = FakeDesk(status={"/api/cyber/timeline": 401})
    page, _ = _live_page(browser, desk)
    page.wait_for_url(ORIGIN + "/login?next=/desk/cyber")
    page.close()
    paths = ["/api/cyber/" + k for k in ("timeline", "graph", "detections", "incident", "containment", "config")]
    desk = FakeDesk(status={p: 409 for p in paths})
    page, _ = _live_page(browser, desk)
    page.wait_for_function("() => !document.querySelector('#overlay').hidden")
    assert _text(page, "#ov-text") == "No desk selected"
    page.close()


def test_record_mode_saves_a_bundle_that_replays(browser):
    desk = FakeDesk()
    page, errors = _live_page(browser, desk, "?record=demo-1&since=2012-03-17T14:30:00Z")
    page.evaluate("() => window.__ready")
    assert _text(page, "#rec-name") == "demo-1" and not page.evaluate("() => document.querySelector('#rec-wrap').hidden")
    page.click(".card .approve")
    page.wait_for_function("() => document.querySelector('.card .state').textContent === 'CONTAINMENT · APPROVED'")
    saved = page.evaluate("() => window.__saveRecording()")
    assert saved["ok"] is True and saved["name"] == "demo-1"
    (_m, _p, _q, body, _h), = desk.calls("POST", "/api/cyber/recordings")
    req = json.loads(body)
    b = req["bundle"]
    assert req["name"] == "demo-1" and b["kind"] == "atlas-cyber-recording" and b["version"] == 1
    assert b["name"] == "demo-1" and b["desk"] == {"id": 3, "name": "Security operations"}
    assert b["query"] == {"since": "2012-03-17T14:30:00Z"} and b["viewport"] == [1920, 1080] and b["tz"]
    kinds = [f["kind"] for f in b["frames"]]
    assert {"config", "timeline", "graph", "detections", "incident", "containment", "ui"} <= set(kinds)
    assert kinds.count("timeline") == 1                         # an unchanged poll is not a new frame
    ts = [f["t"] for f in b["frames"]]
    assert ts == sorted(ts) and all(round(t, 3) == t for t in ts) and b["duration"] >= ts[-1]
    ui = [f for f in b["frames"] if f["kind"] == "ui"]
    assert [f["data"] for f in ui] == [{"action": "approve", "id": 17}]
    assert [f["data"] for f in b["frames"] if f["kind"] == "timeline"][0] == _timeline(S0 + 1800, [MARK])
    cfg = [f["data"] for f in b["frames"] if f["kind"] == "config"][0]          # the hook token never enters a recording
    assert cfg["hook_url"] == "http://atlas.test/hook/<token>/logs" and "tok-SYNTHETIC-1234" not in body
    page.close()
    # the saved bundle replays from disk: the end shows the approved card
    page, errors2 = _replay_page(browser, b)
    page.evaluate("d => window.__render(d)", b["duration"])
    assert _text(page, ".card .state") == "CONTAINMENT · APPROVED"
    assert _text(page, "#rec-wrap") is not None and page.evaluate("() => document.querySelector('#rec-wrap').hidden")
    assert errors == [] and errors2 == []
    page.close()


class ClockDesk(FakeDesk):
    """Like the real route, the timeline answer carries the server clock `now`, so no two polls are equal; from the
    third poll on the data itself changes (a newer last_ts)."""

    def __init__(self):
        super().__init__()
        self.polls = 0

    def handle(self, route):
        u = urlparse(route.request.url)
        if u.scheme + "://" + u.netloc == ORIGIN and u.path == "/api/cyber/timeline":
            self.polls += 1
            data = dict(_timeline(S0 + (1800 if self.polls < 3 else 2400), [MARK]), now=1790911000.0 + self.polls)
            return route.fulfill(status=200, body=json.dumps(data), headers={"Content-Type": "application/json"})
        return super().handle(route)


def test_record_mode_keeps_timeline_changes_not_clock_ticks(browser):
    """A quiet stretch must not add a frame per poll (recordings are capped), yet the poll just before a change is
    kept, so REPLAY's clock moves only between two real observations."""
    desk = ClockDesk()
    page, errors = _live_page(browser, desk, "?record=clock-1")
    page.evaluate("() => window.__ready")
    for _ in range(60):                                      # timeline polls every 2 s
        if desk.polls >= 5:
            break
        page.wait_for_timeout(250)
    assert desk.polls >= 5
    page.evaluate("() => window.__saveRecording()")
    b = json.loads(desk.calls("POST", "/api/cyber/recordings")[-1][3])["bundle"]
    tl = [f for f in b["frames"] if f["kind"] == "timeline"]
    assert [f["data"]["now"] - 1790911000.0 for f in tl] == [1, 2, 3]      # polls 4 and 5 equal poll 3: no frames
    assert [f["data"]["last_ts"] for f in tl] == [S0 + 1800, S0 + 1800, S0 + 2400]
    assert tl[0]["t"] < tl[1]["t"] < tl[2]["t"]
    clock = page.evaluate("([b, t]) => CyberCore.stateAt(b, t).clock", [b, (tl[0]["t"] + tl[1]["t"]) / 2])
    assert clock == S0 + 1800                               # quiet until the poll before the change
    assert errors == []
    page.close()
