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


def _public_ip_reason(ip) -> str | None:
    # is_global is False for private, loopback, link-local, CGNAT (100.64/10), reserved and documentation ranges
    if not ip.is_global or ip.is_multicast or ip in _METADATA_IPS:
        return f"resolves to non-public address {ip}"
    return None


def _internal_ip_reason(ip) -> str | None:
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip in _METADATA_IPS:
        return f"refused - {ip} is a server-internal address"
    return None


_IP_RULES = {"public": _public_ip_reason, "internal": _internal_ip_reason}


def private_url_reason(url: str, unresolvable_ok: bool = True) -> str | None:
    """Reason string when a URL reaches anything that is not public internet space, else None.
    Used for agent-driven fetches (web_fetch, link study, the browser agent): a model that reads an attacker's page
    must not be able to point the next request at the LAN, loopback or the metadata service.
    unresolvable_ok=False for an endpoint being SAVED (a BYO model URL): a name that does not resolve now is refused
    rather than stored, so it cannot start resolving to somewhere internal later."""
    if _ALLOW_PRIVATE:
        return None
    reason, ips = _targets(url)
    if reason:
        return reason
    if not ips and not unresolvable_ok:
        return "the host name does not resolve"
    for ip in ips or []:
        why = _public_ip_reason(ip)
        if why:
            return why
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
        why = _internal_ip_reason(ip)
        if why:
            return why
    return None


def vetted_ips(host: str, scope: str = "public") -> list:
    """Every address `host` resolves to right now, IPv4 first, once ALL of them pass the `scope` rules ("public":
    internet only; "internal": the LAN too, never loopback / link-local / metadata). Raises UnsafeTarget otherwise,
    including when the name does not resolve. GuardedTransport connects to these exact addresses. ALLOW_PRIVATE_FETCH=1
    (a single-user install) turns the rules off: [] = connect as usual."""
    if _ALLOW_PRIVATE:
        return []
    rule = _IP_RULES[scope]
    h = (host or "").strip().strip("[]").rstrip(".").lower()
    if not h:
        raise UnsafeTarget("no host in URL")
    if h == "localhost" or h.endswith(_INTERNAL_NAMES[1:]):
        raise UnsafeTarget(f"internal hostname '{host}'")
    lit = _ip(h)
    if lit is not None:
        ips = [lit]
    else:
        try:
            infos = socket.getaddrinfo(h, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError, OSError):
            raise UnsafeTarget(f"cannot resolve '{host}'") from None
        ips = list(dict.fromkeys(ip for ip in (_ip(i[4][0].split("%")[0]) for i in infos) if ip is not None))
    if not ips:
        raise UnsafeTarget(f"cannot resolve '{host}'")
    for ip in ips:
        why = rule(ip)
        if why:
            raise UnsafeTarget(why)
    return sorted(ips, key=lambda ip: ip.version)


def guarded_transport(scope: str = "public", **kw):
    """An httpx transport for URLs an owner typed in (BYO model endpoints, Hermes Agent instances): see GuardedTransport."""
    return _guarded_cls()(scope, **kw)


_GT = None


def _guarded_cls():
    global _GT
    if _GT is not None:
        return _GT
    import httpx

    class GuardedTransport(httpx.HTTPTransport):
        """Connects only to an address the rules approved, and to exactly that address: the host is resolved once per
        request, every answer is checked (vetted_ips), and the connection goes to a checked IP while the Host header
        and the TLS name (SNI + certificate check) stay the host name. A name that answers with a public address for
        the check and with 127.0.0.1 a moment later for the connect (DNS rebinding, TTL 0) never reaches loopback, the
        LAN or the metadata service. Redirects are not followed by the clients that use it."""

        def __init__(self, scope: str = "public", **kw):
            super().__init__(**kw)
            self._scope = scope

        def handle_request(self, request):
            host = request.url.host
            ips = vetted_ips(host, self._scope)
            if not ips or (len(ips) == 1 and str(ips[0]) == host):
                return super().handle_request(request)
            url = request.url
            request.extensions = {**request.extensions, "sni_hostname": host}
            err = None
            for ip in ips:                              # every address passed; use the first that accepts a connection
                request.url = url.copy_with(host=str(ip))
                try:
                    return super().handle_request(request)
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    err = exc
            raise err  # type: ignore[misc]

    _GT = GuardedTransport
    return _GT


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
