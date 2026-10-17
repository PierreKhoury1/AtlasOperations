"""The session pool (atlas/desk/pool.py): a Claude session per account on Atlas's box, metered through the proxy."""
import json
import time

import httpx
import pytest

import atlas.desk.app as A
import atlas.desk.pool as P
import atlas.usage as U


def test_cost_table_and_sse_usage(monkeypatch):
    monkeypatch.delenv("POOL_PRICES", raising=False)
    u = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 2_000_000, "cache_creation_input_tokens": 400_000}
    assert P.cost_of("claude-sonnet-5-5", u) == pytest.approx(3 + 1.5 + 0.6 + 1.5)
    assert P.cost_of("claude-haiku-5-5", {"input_tokens": 1_000_000}) == 1.0
    assert P.cost_of("mystery-model", u) is None
    monkeypatch.setenv("POOL_PRICES", "mystery-model=2:4")
    assert P.cost_of("Mystery-Model-2", {"input_tokens": 500_000, "output_tokens": 250_000}) == pytest.approx(1 + 1)
    usage = {}
    P._usage_from_sse(b'data: {"type":"message_start","message":{"usage":{"input_tokens":120,"cache_read_input_tokens":30,"output_tokens":1}}}', usage)
    P._usage_from_sse(b'data: {"type":"content_block_delta","delta":{"text":"hi"}}', usage)
    P._usage_from_sse(b'data: {"type":"message_delta","usage":{"output_tokens":77}}', usage)
    assert usage == {"input_tokens": 120, "cache_read_input_tokens": 30, "cache_creation_input_tokens": 0, "output_tokens": 77}


class _Resp:
    def __init__(self, status, chunks, headers=None):
        self.status_code, self._chunks, self.headers = status, chunks, headers or {"content-type": "text/event-stream"}
    def iter_raw(self):
        for c in self._chunks:
            yield c
    def close(self): pass


class _Client:
    sent = []
    resp = None
    def __init__(self, *a, **k): pass
    def build_request(self, method, url, headers=None, content=b""):
        return {"method": method, "url": url, "headers": headers, "content": content}
    def send(self, req, stream=True):
        _Client.sent.append(req)
        return _Client.resp
    def close(self): pass


@pytest.fixture()
def pooled(app_client, monkeypatch):
    """A signed-in account with a session whose runner is a harmless sleeper, the pool on, the proxy's upstream faked."""
    monkeypatch.setenv("TWO_FACTOR", "off")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
    monkeypatch.setenv("SESSION_POOL", "1")
    monkeypatch.setattr(A, "OPEN", False)
    monkeypatch.setattr(P.httpx, "Client", _Client)
    import subprocess, sys
    real_popen = subprocess.Popen
    monkeypatch.setattr(P.subprocess, "Popen", lambda args, **kw: real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **{k: v for k, v in kw.items() if k in ("cwd", "env", "stdin", "stdout", "stderr")}))
    c = A.app.test_client()
    if c.post("/signup", json={"name": "Pool", "company": "Co", "email": "pool@example.com", "password": "password1"}).status_code != 200:
        c.post("/login", json={"email": "pool@example.com", "password": "password1"})
    uid = A.store.user_by_email("pool@example.com")["id"]
    A.POOL.start("http://127.0.0.1:1")
    yield c, uid
    A.POOL.stop(uid)


def test_sign_in_starts_a_session_and_the_proxy_meters_it(pooled, monkeypatch):
    c, uid = pooled
    time.sleep(0.3)                                                    # the warm thread from sign-in
    s = A.POOL.get(uid)
    assert s is not None and s.alive() and (s.home / ".claude").is_dir()
    st = c.get("/api/bridges").get_json()
    assert st["pool"]["enabled"] is True and st["pool"]["running"] is True
    assert any(b["name"] == "Atlas session" for b in st["bridges"])
    # the runner's env never holds the real key; its base url points at the proxy
    assert c.post("/api/pool/start", json={}).get_json()["running"] is True
    # a streamed /v1/messages through the proxy: forwarded with the real key, usage read from the stream, cost in the ledger
    _Client.sent.clear()
    _Client.resp = _Resp(200, [b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":1000000,"output_tokens":0}}}\n\n',
                               b'data: {"type":"content_block_delta","delta":{"text":"ok"}}\n\n',
                               b'data: {"type":"message_delta","usage":{"output_tokens":100000}}\n\ndata: {"type":"message_stop"}\n\n'])
    body = json.dumps({"model": "claude-sonnet-5-5", "stream": True, "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]})
    ms = U.month_start(time.time())
    before = U.counter_n(f"o:{uid}", "usd:paid", "M", ms)
    r = c.post(f"/pool/{uid}/{s.ptoken}/v1/messages", data=body, headers={"content-type": "application/json", "x-api-key": "atlas-pool-x", "anthropic-version": "2023-06-01"})
    assert r.status_code == 200 and b"message_stop" in r.get_data()
    req = _Client.sent[-1]
    assert req["url"] == "https://api.anthropic.com/v1/messages" and req["headers"]["x-api-key"] == "sk-ant-real-key"
    assert "authorization" not in {k.lower() for k in req["headers"]}
    after = U.counter_n(f"o:{uid}", "usd:paid", "M", ms)
    assert (after - before) / 1e6 == pytest.approx(3 + 1.5, abs=0.01)         # $3 in + $1.50 out
    assert s.requests == 1 and s.cost_usd == pytest.approx(4.5)
    # wrong session token: refused, nothing forwarded
    n = len(_Client.sent)
    assert c.post(f"/pool/{uid}/not-the-token/v1/messages", data=body).status_code == 401 and len(_Client.sent) == n
    # the account's paid cap refuses the next request before it is sent, in Anthropic's error shape
    U.set_override(uid, "paid_usd_month", 1.0, note="test")
    r = c.post(f"/pool/{uid}/{s.ptoken}/v1/messages", data=body, headers={"content-type": "application/json"})
    assert r.status_code == 429 and r.get_json()["error"]["type"] == "rate_limit_error" and len(_Client.sent) == n
    U.clear_override(uid, "paid_usd_month")
    # operator view + stop
    monkeypatch.setattr(U.CFG, "operator_emails", frozenset({"pool@example.com"}))
    lst = c.get("/api/pool").get_json()
    assert lst["enabled"] is True and any(x["owner_id"] == uid for x in lst["sessions"])
    assert c.delete(f"/api/pool/{uid}").get_json()["ok"] is True and A.POOL.get(uid) is None
    assert not any(b["name"] == "Atlas session" for b in c.get("/api/bridges").get_json()["bridges"])


def test_pool_off_without_a_key(app_client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert P.enabled() is False
    c = A.app.test_client()
    c.post("/login", json={"email": "pool@example.com", "password": "password1"})
    assert c.post("/api/pool/start", json={}).status_code == 404
    assert c.get("/api/bridges").get_json()["pool"]["enabled"] is False
