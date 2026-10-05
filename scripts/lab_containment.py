"""Lab stand-in for a firewall: records approved containment requests, changes NOTHING on any system.

    py scripts/lab_containment.py [--port 8170] [--log data/lab_containment.jsonl]

Listens on 127.0.0.1 only. POST /contain (the desk's default containment_path), /ban or /isolate with JSON
{action, kind, value, evidence, approval_id}: the request is appended to a JSON-lines log and printed. GET / answers
the portal's connector test. Register it on the desk as an `http` connector (base_url http://127.0.0.1:8170, auto OFF).
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Lab(BaseHTTPRequestHandler):
    log_path = Path("data/lab_containment.jsonl")

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        n = sum(1 for _ in self.log_path.open(encoding="utf-8")) if self.log_path.exists() else 0
        self._send(200, {"ok": True, "service": "lab containment stand-in (records only)", "recorded": n})

    def do_POST(self):
        if self.path.split("?")[0] not in ("/contain", "/ban", "/isolate"):
            return self._send(404, {"error": "POST /contain, /ban or /isolate"})
        try:
            req = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)) or b"{}")
            assert isinstance(req, dict) and req.get("value")
        except Exception:
            return self._send(400, {"error": "JSON {action, kind, value, evidence, approval_id} expected"})
        rec = {"ts": time.time(), "endpoint": self.path.split("?")[0], **{k: req.get(k) for k in
               ("action", "kind", "value", "evidence", "approval_id")}}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        print(time.strftime("%H:%M:%S"), "RECORDED (nothing changed)", json.dumps(rec), flush=True)   # JSON-escaped
        self._send(200, {"ok": True, "recorded": rec, "note": "lab stand-in: nothing was changed on any system"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8170)
    ap.add_argument("--log", default=str(Lab.log_path))
    a = ap.parse_args()
    Lab.log_path = Path(a.log)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Lab)          # --port 0: the OS picks a free port
    print(f"lab containment stand-in on http://127.0.0.1:{srv.server_address[1]} (log: {Lab.log_path})", flush=True)
    srv.serve_forever()
