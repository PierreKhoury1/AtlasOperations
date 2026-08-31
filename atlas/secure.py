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


def private_url_reason(url: str) -> str | None:
    """Return a reason string when a URL points at private/internal address space, else None.
    Applied to agent-driven fetches (web_fetch, link study) - owner-configured connector base_urls are exempt."""
    if _ALLOW_PRIVATE:
        return None
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return "unparseable URL"
    if not host:
        return "no host in URL"
    if host.lower() in ("localhost",) or host.lower().endswith(".localhost") or host.lower().endswith(".local") or host.lower().endswith(".internal"):
        return f"internal hostname '{host}'"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None                                  # unresolvable: let the fetch fail naturally
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return f"resolves to non-public address {ip}"
    return None
