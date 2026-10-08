"""Hardening regressions: redirect-checked fetches, the client address behind the proxy, a scrubbed environment for
model-written scripts, the owner-endpoint guard, the portal CSP, and the browser agent's request guard."""
import pytest

from atlas import secure as SEC


def test_metadata_loopback_and_mapped_addresses_are_refused():
    assert SEC.internal_target_reason("http://169.254.169.254/latest/meta-data/")
    assert SEC.internal_target_reason("http://127.0.0.1:2019/config/")                 # Caddy's admin API
    assert SEC.internal_target_reason("http://[::ffff:127.0.0.1]:2019/")               # IPv4-mapped loopback
    assert SEC.internal_target_reason("http://[::ffff:169.254.169.254]/")              # IPv4-mapped metadata
    assert SEC.internal_target_reason("http://0.0.0.0:8094/")


def test_owner_endpoints_may_sit_on_the_lan():
    # a camera on the office network is the point of a camera, so the narrow guard leaves the LAN open
    assert SEC.internal_target_reason("http://192.168.1.31/capture") is None
    assert SEC.internal_target_reason("http://10.0.0.7/ISAPI/Streaming/channels/101/picture") is None


def test_agent_fetches_refuse_lan_cgnat_and_mapped_space():
    assert SEC.private_url_reason("http://192.168.1.1/router")
    assert SEC.private_url_reason("http://100.64.0.1/")                                # carrier-grade NAT
    assert SEC.private_url_reason("http://[::ffff:10.0.0.1]/")
    assert SEC.private_url_reason("http://8.8.8.8/") is None                           # public literal stays allowed


def test_redirect_from_public_to_metadata_is_not_followed():
    httpx = pytest.importorskip("httpx")
    hops = []

    def handler(request):
        hops.append(str(request.url))
        if request.url.host == "8.8.8.8":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text="metadata body")

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as c:
        with pytest.raises(SEC.UnsafeTarget):
            SEC.send_checked(c, "GET", "http://8.8.8.8/start", guard=SEC.internal_target_reason)
    assert hops == ["http://8.8.8.8/start"]                                            # the metadata address was never requested


def test_redirect_between_public_hosts_is_followed():
    httpx = pytest.importorskip("httpx")

    def handler(request):
        if request.url.path == "/a":
            return httpx.Response(301, headers={"location": "http://1.1.1.1/b"})
        return httpx.Response(200, text="ok")

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as c:
        r = SEC.send_checked(c, "GET", "http://8.8.8.8/a", guard=SEC.internal_target_reason)
    assert r.status_code == 200 and r.text == "ok"


def test_model_written_script_cannot_read_portal_secrets(tmp_path, monkeypatch):
    from atlas.tools import WorkspaceTools
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-DO-NOT-LEAK")
    monkeypatch.setenv("DESK_SECRET", "desk-secret-DO-NOT-LEAK")
    monkeypatch.setenv("DATABASE_URL", "postgresql://atlas:DO-NOT-LEAK@127.0.0.1/atlas")
    wt = WorkspaceTools(tmp_path / "run")
    out = wt.run_python(
        "import os\n"
        "print(sorted(os.environ))\n"
        "print(os.environ.get('OPENROUTER_API_KEY'), os.environ.get('DESK_SECRET'))\n"
    )
    assert "DO-NOT-LEAK" not in out
    assert "OPENROUTER_API_KEY" not in out and "DESK_SECRET" not in out and "DATABASE_URL" not in out
    assert "PYTHONIOENCODING" in out                                                    # the script still ran normally


def test_rate_limit_ignores_the_client_chosen_forwarded_entry(app_client):
    import atlas.desk.app as A
    old = A._RL_AUTH
    A._RL_AUTH = SEC.RateLimiter(2, 600)
    try:
        # the proxy (loopback peer) appends the real client last; a client that rewrites the first entry each time
        # is still one client
        codes = [app_client.post("/login", json={"email": "x@example.com", "password": "nope"},
                                 headers={"X-Forwarded-For": f"10.9.9.{i}, 203.0.113.50"}).status_code for i in range(3)]
        assert codes[-1] == 429
    finally:
        A._RL_AUTH = old


def test_forwarded_header_is_ignored_from_a_direct_peer(app_client):
    import atlas.desk.app as A
    old = A._RL_AUTH
    A._RL_AUTH = SEC.RateLimiter(2, 600)
    try:
        peer = {"REMOTE_ADDR": "198.51.100.7"}
        for i in range(2):
            app_client.post("/login", json={"email": "x@example.com", "password": "nope"},
                            headers={"X-Forwarded-For": f"10.0.0.{i}"}, environ_base=peer)
        r = app_client.post("/login", json={"email": "x@example.com", "password": "nope"},
                            headers={"X-Forwarded-For": "10.0.0.99"}, environ_base=peer)
        assert r.status_code == 429                                                    # a fresh forged header buys nothing
    finally:
        A._RL_AUTH = old


def test_portal_sends_a_csp_that_closes_exfiltration_routes(app_client):
    csp = app_client.get("/api/health").headers["Content-Security-Policy"]
    for directive in ("default-src 'self'", "connect-src 'self'", "form-action 'self'", "object-src 'none'",
                      "base-uri 'self'", "frame-ancestors 'none'"):
        assert directive in csp


def test_browser_refuses_internal_navigation(tmp_path, monkeypatch):
    pytest.importorskip("playwright")
    from atlas import browser as B
    monkeypatch.setattr(B, "BROWSER_DIR", tmp_path / "browser")
    monkeypatch.setattr(B, "MACRO_DIR", tmp_path / "browser" / "macros")
    hand = B.BrowserHand(profile="guard", headless=True)
    try:
        with pytest.raises(Exception) as exc:
            hand.act("navigate", {"url": "http://169.254.169.254/latest/meta-data/"})
        assert "BLOCKED" in str(exc.value).upper() or "ERR_" in str(exc.value).upper()
    finally:
        hand.close()
