"""Security helpers: secrets encryption at rest, request rate limiting, SSRF guard.

Encryption: connector configs (SMTP passwords, API keys, tokens) are encrypted with Fernet using a key derived
from the desk secret (DESK_SECRET env or data/secret.key). Ciphertext is prefixed "enc1:"; legacy plaintext JSON
rows decrypt transparently and are re-encrypted by the boot migration. Rotating DESK_SECRET invalidates stored
connector secrets (owners re-enter them) - document before rotating.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import socket
import threading
import time
from typing import Any
from urllib.parse import urlparse

_PREFIX = "enc1:"
_fernet = None
_fernet_lock = threading.Lock()


def _secret_material() -> bytes:
    env = os.environ.get("DESK_SECRET", "").strip()
    if env:
        return env.encode()
    from .config import DATA_DIR
    f = DATA_DIR / "secret.key"
    if f.exists():
        return f.read_text(encoding="utf-8").strip().encode()
    import secrets as _s
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    f.write_text(_s.token_hex(32), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip().encode()


def fernet():
    global _fernet
    with _fernet_lock:
        if _fernet is None:
            from cryptography.fernet import Fernet
            key = base64.urlsafe_b64encode(hashlib.sha256(b"atlas-connector-secrets:" + _secret_material()).digest())
            _fernet = Fernet(key)
        return _fernet


def encrypt_config(config: dict[str, Any]) -> str:
    raw = json.dumps(config or {}, ensure_ascii=False).encode()
    return _PREFIX + fernet().encrypt(raw).decode()


def decrypt_config(stored: str | None) -> dict[str, Any]:
    s = stored or "{}"
    if s.startswith(_PREFIX):
        try:
            return json.loads(fernet().decrypt(s[len(_PREFIX):].encode()))
        except Exception:
            return {"_decrypt_error": "stored secrets cannot be decrypted (DESK_SECRET changed?) - re-enter this connector's credentials"}
    try:
        return json.loads(s)                     # legacy plaintext row
    except json.JSONDecodeError:
        return {}


def is_encrypted(stored: str | None) -> bool:
    return bool(stored) and stored.startswith(_PREFIX)


# ---------------------------------------------------------------------------- rate limiting
class RateLimiter:
    """Sliding-window in-memory limiter: allow(key) -> True if under `limit` events per `window_s`."""

    def __init__(self, limit: int, window_s: float):
        self.limit = int(limit)
        self.window = float(window_s)
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            q = self._hits.setdefault(key, [])
            cutoff = now - self.window
            while q and q[0] < cutoff:
                q.pop(0)
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 10000:              # bound memory under scanning noise
                for k in [k for k, v in self._hits.items() if not v or v[-1] < cutoff][:5000]:
                    self._hits.pop(k, None)
            return True


# ---------------------------------------------------------------------------- SSRF guard
_ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_FETCH", "").strip() in ("1", "true", "yes")
# Cloud instance-metadata endpoints: Hetzner, AWS/GCP/Azure, Alibaba. Anything that can read these can read the box's identity.
_METADATA_IPS = {ipaddress.ip_address(a) for a in ("169.254.169.254", "100.100.100.200", "fd00:ec2::254")}
_INTERNAL_NAMES = ("localhost", ".localhost", ".local", ".internal")


class UnsafeTarget(ValueError):
    """A URL the server will not fetch: an internal address, a metadata endpoint, or a redirect chain that leads there."""


def _ip(text: str):
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:      # ::ffff:127.0.0.1 is 127.0.0.1
        ip = ip.ipv4_mapped
    return ip


def _targets(url: str):
    """(host, [resolved ips]) for a URL, or (reason, None) when it cannot be checked at all."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return "unparseable URL", None
    if not host:
        return "no host in URL", None
    if host.lower() == "localhost" or host.lower().endswith(_INTERNAL_NAMES[1:]):
        return f"internal hostname '{host}'", None
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None, []                              # unresolvable: let the fetch fail naturally
    ips = [_ip(i[4][0].split("%")[0]) for i in infos]
    return None, [ip for ip in ips if ip is not None]


def private_url_reason(url: str) -> str | None:
    """Reason string when a URL reaches anything that is not public internet space, else None.
    Used for agent-driven fetches (web_fetch, link study, the browser agent): a model that reads an attacker's page
    must not be able to point the next request at the LAN, loopback or the metadata service."""
    if _ALLOW_PRIVATE:
        return None
    reason, ips = _targets(url)
    if reason:
        return reason
    for ip in ips or []:
        # is_global is False for private, loopback, link-local, CGNAT (100.64/10), reserved and documentation ranges
        if not ip.is_global or ip.is_multicast or ip in _METADATA_IPS:
            return f"resolves to non-public address {ip}"
    return None


def internal_target_reason(url: str) -> str | None:
    """Narrower guard for endpoints an owner configured (HTTP connectors, camera snapshots). These may legitimately sit
    on the LAN (a camera on the office network), so only what a server reaches from the inside is refused: loopback
    (Caddy's admin API, Postgres' HTTP neighbours, the Hermes gateway), link-local and the cloud metadata service."""
    if _ALLOW_PRIVATE:
        return None
    reason, ips = _targets(url)
    if reason:
        return reason
    for ip in ips or []:
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip in _METADATA_IPS:
            return f"refused - {ip} is a server-internal address"
    return None


def send_checked(client, method: str, url: str, *, guard=private_url_reason, max_hops: int = 5, **kw):
    """client.request() with every hop checked. Redirects are followed by hand so each new Location goes through
    `guard` again: a public URL that 302s to 127.0.0.1 or to the metadata address is refused, not fetched.
    Use with an httpx.Client(follow_redirects=False). A 307/308 keeps the method and body; any other redirect is a GET."""
    for _ in range(max_hops + 1):
        reason = guard(url)
        if reason:
            raise UnsafeTarget(reason)
        r = client.request(method, url, **kw)
        if not (r.is_redirect and r.headers.get("location")):
            return r
        url = str(r.url.join(r.headers["location"]))
        if r.status_code in (307, 308):
            kw = {k: v for k, v in kw.items() if k != "params"}
        else:
            method = "GET"
            kw = {k: v for k, v in kw.items() if k in ("headers", "auth", "timeout")}
    raise UnsafeTarget("too many redirects")
