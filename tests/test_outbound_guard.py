"""URLs a desk owner typed in (BYO model endpoints, a Hermes Agent instance, a Slack webhook) are checked when saved
AND when used, and the connection goes to the address that was checked: a name that answers with a public address for
the check and with 127.0.0.1 for the connect (DNS rebinding) never reaches this server's loopback."""
import http.server
import json
import socket
import threading

import pytest

from atlas import integrations as I
from atlas import secure as SEC
from atlas.providers import make_provider


class _Hits(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def _ok(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        _Hits.seen.append({"path": self.path, "host": self.headers.get("Host")})
        body = json.dumps({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "model": "m"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _ok

    def log_message(self, *a):
        pass


@pytest.fixture()
def loopback_server():
    _Hits.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hits)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def _resolve_as(monkeypatch, answers):
    """getaddrinfo for *.test names: the next answer each call (the last one repeats)."""
    real = socket.getaddrinfo
    calls = []

    def fake(host, *a, **k):
        if str(host).endswith(".test"):
            calls.append(host)
            ip = answers[min(len(calls), len(answers)) - 1]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
        return real(host, *a, **k)
    monkeypatch.setattr(SEC.socket, "getaddrinfo", fake)
    return calls


def test_vetted_ips_rules():
    with pytest.raises(SEC.UnsafeTarget):
        SEC.vetted_ips("127.0.0.1", "internal")
    with pytest.raises(SEC.UnsafeTarget):
        SEC.vetted_ips("localhost", "internal")
    with pytest.raises(SEC.UnsafeTarget):
        SEC.vetted_ips("169.254.169.254", "internal")
    with pytest.raises(SEC.UnsafeTarget):
        SEC.vetted_ips("192.168.1.20", "public")                      # model endpoints: public internet only
    assert [str(i) for i in SEC.vetted_ips("192.168.1.20", "internal")] == ["192.168.1.20"]   # a LAN device is fine
    with pytest.raises(SEC.UnsafeTarget, match="cannot resolve"):
        SEC.vetted_ips("nonexistent-review1.invalid", "public")
    assert SEC.private_url_reason("https://nonexistent-review1.invalid/v1", unresolvable_ok=False) == "the host name does not resolve"


def test_rebinding_name_never_reaches_loopback(monkeypatch, loopback_server):
    """Public for the first look, 127.0.0.1 for every look after it: the request is refused before any connect."""
    calls = _resolve_as(monkeypatch, ["93.184.216.34", "127.0.0.1"])
    p = make_provider({"type": "openai_compatible", "base_url": f"http://rebind.test:{loopback_server}/v1",
                       "api_key": "k", "default_model": "m", "url_guard": True, "timeout": 5})
    with pytest.raises((RuntimeError, SEC.UnsafeTarget)):
        p.chat("", [{"role": "user", "content": "hi"}], [])
    assert len(calls) >= 2 and _Hits.seen == []                        # resolved again at connect time; nothing arrived


def test_connection_goes_to_the_checked_address_with_the_host_name_kept(monkeypatch, loopback_server):
    """With the address rules relaxed for the test (so loopback can stand in for a public host), a guarded call
    connects to exactly the vetted IP and still sends the host name in Host (TLS would use it for SNI + the cert)."""
    _resolve_as(monkeypatch, ["127.0.0.1"])
    monkeypatch.setitem(SEC._IP_RULES, "public", lambda ip: None)
    p = make_provider({"type": "openai_compatible", "base_url": f"http://pinned.test:{loopback_server}/v1",
                       "api_key": "k", "default_model": "m", "url_guard": True, "timeout": 5})
    r = p.chat("", [{"role": "user", "content": "hi"}], [])
    assert r.text == "OK"
    assert _Hits.seen and _Hits.seen[-1]["host"] == f"pinned.test:{loopback_server}" and _Hits.seen[-1]["path"] == "/v1/chat/completions"


def test_hermes_agent_connector_is_guarded_but_the_operators_own_instance_is_not(app_client, monkeypatch):
    import atlas.desk.app as A
    c = A.app.test_client()
    A._RL_AUTH._hits.clear()
    c.post("/signup", json={"name": "H", "company": "H", "email": "hermes-guard@example.com", "password": "password1"})
    did = c.post("/api/desks", json={"name": "H", "template": "sales_desk", "tier": "free"}).get_json()["id"]
    monkeypatch.setenv("DESK_MODE", "live")
    monkeypatch.setenv("DESK_LEAD_ENGINE", "hermes_agent")
    A.store.add_connector(did, "hermes_agent", "hermes", {"base_url": "http://127.0.0.1:8642", "api_key": "x"}, False)
    pc = A.desk_configs(A.store.desk(did))["providers"]["providers"]["hermes_agent_atlas"]
    assert pc["url_guard"] == "internal"
    with pytest.raises(RuntimeError, match="refused"):
        make_provider(pc).chat("", [{"role": "user", "content": "hi"}], [])
    with pytest.raises(SEC.UnsafeTarget):
        I.test_hermes_agent({"base_url": "http://127.0.0.1:8642", "api_key": "x"})
    # HERMES_AGENT_URL is the operator's own (trusted, may be local): no guard. It is used only while its model calls
    # can be counted (USAGE_HERMES_METER=proxy, usage spec §1.8; the default, off, keeps the built-in engine)
    for conn in A.store.connectors(did):
        A.store.delete_connector(conn["id"])
    monkeypatch.setenv("HERMES_AGENT_URL", "http://127.0.0.1:8642")
    from atlas import usage
    monkeypatch.setattr(usage.CFG, "hermes_meter", "proxy")
    pc = A.desk_configs(A.store.desk(did))["providers"]["providers"]["hermes_agent_atlas"]
    assert not pc["url_guard"]


def test_slack_webhook_is_checked_on_every_hop(monkeypatch):
    with pytest.raises(RuntimeError, match="refused"):
        I.slack_notify({"webhook_url": "http://127.0.0.1:9/hook"}, "hi")
    import httpx
    hits = []

    def handler(req):
        hits.append(str(req.url))
        if req.url.host == "hooks.slack.com":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text="ok")
    monkeypatch.setattr(I, "_TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="refused"):
        I.slack_notify({"webhook_url": "https://hooks.slack.com/services/T/B/X"}, "hi")
    assert hits == ["https://hooks.slack.com/services/T/B/X"]           # the redirect target was never requested


def test_slack_webhooks_are_public_internet_only(monkeypatch):
    """Slack is always on the public internet, so a webhook URL on the LAN is refused too (the stricter rule), and the
    connection goes to the checked address."""
    _resolve_as(monkeypatch, ["192.168.1.20"])
    for url in ("http://192.168.1.20/hook", "https://lan-hook.test/services/T/B/X", "http://10.0.0.5/x"):
        with pytest.raises(RuntimeError, match="refused"):
            I.slack_notify({"webhook_url": url}, "hi")


def test_rtsp_streams_go_to_the_checked_address(monkeypatch):
    """ffmpeg and OpenCV resolve a name again on their own, so an RTSP source is handed over with the address that was
    just checked in place of the name (credentials, port and path kept); a rebinding name is refused."""
    from atlas import vision as V
    _resolve_as(monkeypatch, ["192.168.1.20"])
    assert V.pinned_stream("rtsp://admin:p%40ss@cam1.test:554/Streaming/Channels/101?x=1") == \
        "rtsp://admin:p%40ss@192.168.1.20:554/Streaming/Channels/101?x=1"
    assert V.pinned_stream("rtsp://cam1.test/live") == "rtsp://192.168.1.20/live"
    assert V.pinned_stream("rtsp://192.168.1.21/live") == "rtsp://192.168.1.21/live"
    assert V.pinned_stream("https://cams.test/live.m3u8") == "https://cams.test/live.m3u8"   # TLS / virtual hosts need the name
    assert V.pinned_stream("samples/x.mp4") == "samples/x.mp4"
    for bad in ("rtsp://127.0.0.1/x", "rtsp://169.254.169.254/x", "rtsp://localhost/x", "http://127.0.0.1:2019/config/"):
        with pytest.raises(RuntimeError, match="refused"):
            V.pinned_stream(bad)
    _resolve_as(monkeypatch, ["192.168.1.20", "127.0.0.1"])                 # passes the first look, loopback after it
    with pytest.raises(RuntimeError, match="refused"):
        V.pinned_stream("rtsp://rebind.test/live")
    # the ffmpeg grab gets the pinned address and may open network protocols only (no file:/concat:/subfile:)
    _resolve_as(monkeypatch, ["192.168.1.20"])
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, b"\xff\xd8jpeg", b""
    monkeypatch.setattr(V.shutil, "which", lambda name: "ffmpeg")
    monkeypatch.setattr(V.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd) or Done())
    assert V._grab_rtsp("rtsp://u:p@cam2.test:8554/s") == b"\xff\xd8jpeg"
    cmd = seen["cmd"]
    assert cmd[cmd.index("-i") + 1] == "rtsp://u:p@192.168.1.20:8554/s"
    assert "file" not in cmd[cmd.index("-protocol_whitelist") + 1].split(",")
