"""Security desk core: parse security logs, run deterministic detection rules, enrich indicators from public reference
data, build the entity graph and normalise containment proposals.

Everything here is a pure function except the enrichment cache under DATA_DIR/intel (or ATLAS_INTEL_DIR):

  parse        sshd syslog, Zeek TSV (notice / ssh / conn), Snort or Suricata fast.log, Apache/Nginx combined
  detect       ssh_bruteforce, success_after_failures, wordlist_fingerprint, scan, ids_high, web_probe
  graph        IPs, hosts, users, signatures and cameras; every edge carries the event ids that justify it
  enrich       RIPEstat (registry holder, ASN, country), optional DB-IP Lite CSV, CISA KEV, NVD (rate-limited); cached
  containment  normalise a proposal and phrase the approval question. Nothing in this module blocks anything: a
               proposal becomes a queued action that a person approves (atlas.desk.app._dispatch carries it out).

Log fields (usernames, URLs, user agents, messages, signature text) are attacker-controlled data. They are cleaned with
safe_text, never used in detection titles (except addresses and IDS/Zeek signature names), and reach agents only as
JSON fields. fetch_json is the only function that touches the network and it only talks to ALLOWED_HOSTS.

CLI (re-check the dataset facts without the portal):
  py -m atlas.cyber parse FILE [--fmt F] [--sensor S] [--year Y] [--tz TZ] [--zeek-path P] [--limit N]
  py -m atlas.cyber detect FILE [same options] [--json]
"""
from __future__ import annotations

import bisect
import collections
import copy
import csv
import gzip
import hashlib
import ipaddress
import itertools
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urlsplit

from . import config

# ---------------------------------------------------------------------------- constants (contract: exact names/values)
FORMATS = ("sshd", "zeek", "snort_fast", "combined")
SOURCES = ("sshd", "zeek", "snort_fast", "combined", "custom")          # event["source"]
KINDS = ("invalid_user", "auth_failure", "auth_success", "disconnect", "probe", "notice",
         "ssh_session", "ids_alert", "http_request", "conn", "custom")
FAIL_KINDS = ("invalid_user", "auth_failure")
SEVERITIES = ("info", "low", "medium", "high", "critical")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
RULES = ("success_after_failures", "ids_high", "wordlist_fingerprint", "ssh_bruteforce", "scan", "web_probe")
#        ^ also the tie-break order when two detections share a severity
FIELD_LIMITS = {"source": 20, "sensor": 60, "kind": 24, "src": 64, "dst": 64, "user": 128,
                "sig": 200, "message": 300, "raw": 4000, "origin": 80}
ATTRS_MAX_JSON = 2000
MAX_DETECT_EVENTS = int(os.environ.get("CYBER_MAX_DETECT_EVENTS", "200000"))
DEFAULT_PARAMS = {"bf_failures": 10, "bf_window_s": 600, "saf_failures": 5, "saf_window_s": 3600,
                  "wl_min_len": 20, "wl_min_sources": 2, "scan_ports": 15, "scan_hosts": 25,
                  "scan_window_s": 300, "ids_min_alerts": 5, "probe_min": 5, "evidence_cap": 50}
C2_RE = re.compile(r"(?i)\b(meterpreter|cobalt\s?strike|sliver|reverse[ -]shell|command and control)\b")
C2_FAMILY = {"meterpreter": "Metasploit Meterpreter", "cobalt strike": "Cobalt Strike", "cobaltstrike": "Cobalt Strike",
             "sliver": "Sliver", "reverse shell": "reverse shell", "command and control": "command and control"}
             # key = match lower-cased, '-' replaced by ' ', whitespace collapsed
NOTE_SEVERITY = {"SSH::Password_Guessing": "high", "HTTP::SQL_Injection_Attacker": "high",
                 "HTTP::SQL_Injection_Victim": "high", "Signatures::Sensitive_Signature": "high",
                 "Scan::Port_Scan": "medium", "Scan::Address_Scan": "medium",
                 "SSL::Invalid_Server_Cert": "info"}                      # any other note: "low"
PROBE_FAMILIES = (                                                       # (id, label, path prefixes)
    ("wordpress", "WordPress login/admin", ("/wp-login.php", "/wp-admin", "/xmlrpc.php", "/wp-content/plugins/", "/wp-includes/")),
    ("phpmyadmin", "phpMyAdmin", ("/phpmyadmin", "/pma/", "/myadmin", "/mysqladmin")),
    ("env_file", ".env file", ("/.env",)),
    ("git_repo", ".git repository", ("/.git/",)),
    ("cgi_bin", "CGI", ("/cgi-bin/",)),
    ("router", "router admin", ("/hnap1", "/boaform", "/gponform")),
    ("java_admin", "Java admin console", ("/manager/html", "/actuator", "/jmx-console")),
    ("php_rce", "PHP exploit", ("/vendor/phpunit",)),
    ("traversal", "path traversal", ("../", "/etc/passwd")),             # these two match anywhere in the path
)
SSHD_PROGS = ("sshd", "sshd-session", "sshd-auth")
CITE_RE = re.compile(r"\[(cam ?)?#(\d+)\]")                              # [#12] = log event, [cam #12] = camera event
POLICY_LINE = "Every target is in the evidence."
CONTAINMENT_ACTIONS = ("block", "isolate", "disable")
TARGET_KINDS = ("ip", "host", "user")
ALLOWED_PAIRS = frozenset({("ip", "block"), ("ip", "isolate"), ("host", "isolate"), ("host", "block"), ("user", "disable")})
DEFAULT_ACTION = {"ip": "block", "host": "isolate", "user": "disable"}
NVD_NOTICE = "This product uses the NVD API but is not endorsed or certified by the NVD."
DBIP_ATTRIBUTION = "IP Geolocation by DB-IP (https://db-ip.com)"
REGISTRY_NOTE = ("Registry data as of today: it names the current holder of the address block, "
                 "not who used the address in the past.")
SECREPO_ATTRIBUTION = "Security Repo by Mike Sconzo (secrepo.com), CC BY 4.0"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
RIPESTAT_URL = "https://stat.ripe.net/data/{call}/data.json"
ALLOWED_HOSTS = frozenset({"stat.ripe.net", "www.cisa.gov", "services.nvd.nist.gov"})

# Headerless Zeek (Bro 2.x) column lists, as SecRepo publishes the MACCDC 2012 logs.
ZEEK_FIELDS = {
    "notice": ["ts", "uid", "id.orig_h", "id.orig_p", "id.resp_h", "id.resp_p", "fuid", "file_mime_type", "file_desc",
               "proto", "note", "msg", "sub", "src", "dst", "p", "n", "peer_descr", "actions", "suppress_for", "dropped",
               "remote_location.country_code", "remote_location.region", "remote_location.city",
               "remote_location.latitude", "remote_location.longitude"],
    "ssh": ["ts", "uid", "id.orig_h", "id.orig_p", "id.resp_h", "id.resp_p", "status", "direction", "client", "server",
            "remote_location.country_code", "remote_location.region", "remote_location.city",
            "remote_location.latitude", "remote_location.longitude"],
    "conn": ["ts", "uid", "id.orig_h", "id.orig_p", "id.resp_h", "id.resp_p", "proto", "service", "duration",
             "orig_bytes", "resp_bytes", "conn_state", "local_orig", "missed_bytes", "history", "orig_pkts",
             "orig_ip_bytes", "resp_pkts", "resp_ip_bytes", "tunnel_parents"],
}   # 21-column conn: insert "local_resp" after "local_orig"
_ZEEK_BY_COLUMNS = {26: "notice", 15: "ssh", 20: "conn", 21: "conn"}

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_NO = {m: i + 1 for i, m in enumerate(_MONTHS)}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Injectable clock/sleep for the NVD rate limiter (tests swap them for a fake clock).
_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep


# ---------------------------------------------------------------------------- text and time helpers
# C0/C1 controls (tab handled first) and the bidi marks, embeddings, overrides and isolates that can reorder text
_BIDI = "".join(map(chr, (0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))))
_CONTROL = re.compile("[\x00-\x08\x0a-\x1f\x7f-\x9f" + _BIDI + "]")
_SPACES = re.compile(r"\s+")


def safe_text(value: Any, limit: int) -> str:
    """One clean line of at most `limit` characters: control and bidi characters removed, whitespace collapsed."""
    s = str(value or "").replace("\t", " ")
    s = _SPACES.sub(" ", _CONTROL.sub("", s)).strip()
    if limit <= 0:
        return ""
    return s if len(s) <= limit else s[: limit - 1] + "…"


_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?)?\s*([Zz]|[+-]\d{2}:?\d{2})?$")


def _offset(text: str | None) -> timezone:
    if not text or text in ("Z", "z"):
        return timezone.utc
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", text)
    if not m or int(m[2]) > 23 or int(m[3]) > 59:
        raise ValueError(f"bad UTC offset {text!r}")
    off = timedelta(hours=int(m[2]), minutes=int(m[3]))
    return timezone(-off if m[1] == "-" else off)


def parse_time(value: Any) -> float | None:
    """Epoch seconds (UTC) from epoch numbers (milliseconds above 1e12) or ISO 8601 text; None for junk."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            v = float(s)
        except ValueError:
            m = _ISO.match(s)
            if not m:
                return None
            y, mo, d, hh, mi, ss, frac, off = m.groups()
            try:
                dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0), tzinfo=_offset(off))
            except ValueError:
                return None
            return dt.timestamp() + (float(frac) if frac else 0.0)
    if not math.isfinite(v):
        return None
    return v / 1000.0 if v > 1e12 else v


def _utc(ts: Any) -> datetime | None:
    if ts is None or isinstance(ts, bool):
        return None
    try:
        v = float(ts)
        return _EPOCH + timedelta(seconds=v) if math.isfinite(v) else None
    except (TypeError, ValueError, OverflowError):
        return None


def fmt_ts(ts: Any, year: bool = True) -> str:
    """UTC: '2012-03-17 14:41:27Z', or 'Mar 17 14:41:27Z' when the source log carried no year."""
    dt = _utc(ts)
    if dt is None:
        return ""
    if year:
        return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"
    return f"{_MONTHS[dt.month - 1]} {dt.day:02d} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


# ---------------------------------------------------------------------------- the event dict
_HOSTNAME = re.compile(r"[\w.-]{1,64}", re.ASCII)
_ATTR_LIMITS = {"reason": 120, "ident": 80, "client": 80, "server": 80}


def _is_ip(value: Any) -> bool:
    try:
        ipaddress.ip_address(str(value).strip())
        return True
    except ValueError:
        return False


def _addr(value: Any) -> str:
    """An IP in compressed form, else a plain hostname, else '' (so 'UNKNOWN', '-' and junk become '')."""
    s = str(value or "").strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    if not s:
        return ""
    try:
        return ipaddress.ip_address(s).compressed[:64]
    except ValueError:
        pass
    if s.upper() == "UNKNOWN" or not _HOSTNAME.fullmatch(s) or not re.search(r"[A-Za-z0-9]", s):
        return ""
    return s


def _clean_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown/empty values, clean strings, keep ints; cap the JSON at ATTRS_MAX_JSON by dropping trailing keys."""
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if v is None or v == "":
            continue
        if isinstance(v, bool) or isinstance(v, int):
            out[k] = v
        elif isinstance(v, float):
            if math.isfinite(v):
                out[k] = v
        elif isinstance(v, str):
            t = safe_text(v, _ATTR_LIMITS.get(k, 200))
            if t:
                out[k] = t
    while out and len(json.dumps(out)) > ATTRS_MAX_JSON:
        out.pop(next(reversed(out)))
    return out


def _event(ts: float, source: str, sensor: str, kind: str, *, src: Any = "", dst: Any = "", user: Any = "",
           sig: Any = "", severity: str = "info", message: Any = "", raw: Any = "",
           attrs: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ts": float(ts), "source": safe_text(source, FIELD_LIMITS["source"]),
            "sensor": safe_text(sensor, FIELD_LIMITS["sensor"]), "kind": safe_text(kind, FIELD_LIMITS["kind"]),
            "src": _addr(src), "dst": _addr(dst), "user": safe_text(user, FIELD_LIMITS["user"]),
            "sig": safe_text(sig, FIELD_LIMITS["sig"]),
            "severity": severity if severity in SEV_RANK else "info",
            "message": safe_text(message, FIELD_LIMITS["message"]),
            "raw": str(raw or "").replace("\x00", "")[: FIELD_LIMITS["raw"]],
            "attrs": _clean_attrs(attrs or {})}


def _attrs_of(ev: dict[str, Any]) -> dict[str, Any]:
    a = ev.get("attrs")
    if isinstance(a, str):
        try:
            a = json.loads(a or "{}")
        except ValueError:
            a = {}
    return a if isinstance(a, dict) else {}


def _int(value: Any) -> int | None:
    s = str(value if value is not None else "").strip()
    return int(s) if re.fullmatch(r"-?[0-9]{1,18}", s) else None


def _hostport(addr: str, port: int | None) -> str:
    if port is None:
        return addr
    return f"[{addr}]:{port}" if ":" in addr else f"{addr}:{port}"


# ---------------------------------------------------------------------------- parsers: shared machinery
def _tzinfo(tz: Any) -> timezone | None:
    """'UTC'/'Z'/'' -> UTC, '+HH:MM'/'-HH:MM'/'+HHMM' -> fixed offset, 'local' -> None (server zone per date)."""
    s = str(tz if tz is not None else "").strip()
    if s.upper() in ("", "UTC", "Z"):
        return timezone.utc
    if s.lower() == "local":
        return None
    try:
        return _offset(s)
    except ValueError:
        raise ValueError(f"unknown tz {safe_text(s, 24)!r}: use UTC, +HH:MM, -HH:MM or local") from None


def _wall(y: int, mo: int, d: int, hh: int, mi: int, ss: int, frac: float, tzi: timezone | None) -> float:
    if tzi is None:                                    # naive datetime -> the server's local zone for that date
        return datetime(y, mo, d, hh, mi, ss).timestamp() + frac
    return datetime(y, mo, d, hh, mi, ss, tzinfo=tzi).timestamp() + frac


def _yearless(mo: int, d: int, hh: int, mi: int, ss: int, frac: float, tzi: timezone | None, year: int | None,
              now: float) -> tuple[float, bool]:
    """Timestamp for a line without a year: the given year, else the year of `now` (the previous year when that would
    put the event more than two days in the future). The bool says whether the year was assumed."""
    if year:
        return _wall(year, mo, d, hh, mi, ss, frac, tzi), False
    y = (_utc(now) or _EPOCH).year
    ts = _wall(y, mo, d, hh, mi, ss, frac, tzi)
    if ts > now + 2 * 86400:
        ts = _wall(y - 1, mo, d, hh, mi, ss, frac, tzi)
    return ts, True


def _note_error(stats: dict[str, Any], text: str) -> None:
    errs = stats.setdefault("errors", [])
    text = safe_text(text, 120)
    if len(errs) < 5 and text not in errs:
        errs.append(text)


def _run(parse_line: Callable[[str, int], dict[str, Any] | None], lines: Iterable[Any],
         stats: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for line in lines:
        stats["lines"] += 1
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        line = str(line).rstrip("\r\n")
        try:
            ev = parse_line(line, stats["lines"])
        except Exception as exc:                      # a hostile or broken line never stops the parse
            ev = None
            _note_error(stats, f"line {stats['lines']}: {type(exc).__name__}")
        if ev is None:
            stats["skipped"] += 1
            continue
        stats["events"] += 1
        ts = ev["ts"]
        if stats["first_ts"] is None or ts < stats["first_ts"]:
            stats["first_ts"] = ts
        if stats["last_ts"] is None or ts > stats["last_ts"]:
            stats["last_ts"] = ts
        yield ev


# ---------------------------------------------------------------------------- sshd (syslog)
SYSLOG_TRADITIONAL = re.compile(
    r"^(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) {1,2}(?P<day>\d{1,2}) (?P<time>\d{2}:\d{2}:\d{2}) "
    r"(?P<host>\S+) (?P<prog>[\w./-]+)(?:\[(?P<pid>\d+)\])?: (?P<msg>.*)$")
SYSLOG_RFC3339 = re.compile(
    r"^(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})) "
    r"(?P<host>\S+) (?P<prog>[\w./-]+)(?:\[(?P<pid>\d+)\])?: (?P<msg>.*)$")
_SSHD_PATTERNS = (                                     # first match wins (contract table 1.4, rows 1-11)
    ("invalid", re.compile(r"^Invalid user (?P<user>.*?) from (?P<src>\S+?)(?: port (?P<port>\d+))?\s*$")),
    ("skip", re.compile(r"^input_userauth_request: invalid user ")),
    ("failed", re.compile(r"^Failed (?P<method>\S+) for (?P<inv>invalid user )?(?P<user>.*?) from (?P<src>\S+) "
                          r"port (?P<port>\d+)")),
    ("accepted", re.compile(r"^Accepted (?P<method>\S+) for (?P<user>\S+) from (?P<src>\S+) port (?P<port>\d+)")),
    ("too_many", re.compile(r"^Disconnecting(?: (?:authenticating|invalid) user (?P<user2>\S+) (?P<src>\S+) "
                            r"port (?P<port>\d+))?: Too many authentication failures(?: for (?:invalid user )?"
                            r"(?P<user>\S+))?")),
    ("max_auth", re.compile(r"^(?:error: )?maximum authentication attempts exceeded for (?:invalid user )?"
                            r"(?P<user>\S+) from (?P<src>\S+) port (?P<port>\d+)")),
    ("received", re.compile(r"^Received disconnect from (?P<src>[0-9A-Fa-f:.]+?)(?: port (?P<port>\d+))?:\s*"
                            r"(?P<code>\d+):\s*(?P<reason>.*?)(?: \[preauth\])?\s*$")),
    ("closed", re.compile(r"^Connection closed by (?:(?:authenticating|invalid) user (?P<user>\S+) )?"
                          r"(?P<src>[0-9A-Fa-f:.]+)(?: port (?P<port>\d+))?")),
    ("disconnected", re.compile(r"^Disconnected from (?:(?:authenticating|invalid) user (?P<user>\S+) )?"
                                r"(?P<src>[0-9A-Fa-f:.]+)(?: port (?P<port>\d+))?")),
    ("no_ident", re.compile(r"^Did not receive identification string from (?P<src>\S+)")),
    ("bad_proto", re.compile(r"^Bad protocol version identification '(?P<ident>.*)' from (?P<src>\S+)")),
)
# Row 7 with a greedy address: only used when the lazy contract pattern split an IPv6 address without a port
# ("Received disconnect from 2001:db8::1: 11: Bye") at the wrong colon.
_RECEIVED_GREEDY = re.compile(r"^Received disconnect from (?P<src>[0-9A-Fa-f:.]+)(?: port (?P<port>\d+))?:\s*"
                              r"(?P<code>\d+):\s*(?P<reason>.*?)(?: \[preauth\])?\s*$")


def _sshd_parser(sensor: str, year: int | None, tzi: timezone | None, now: float,
                 stats: dict[str, Any]) -> Callable[[str, int], dict[str, Any] | None]:
    def parse_line(line: str, n: int) -> dict[str, Any] | None:
        if not line.strip():
            return None
        assumed = False
        m = SYSLOG_TRADITIONAL.match(line)
        if m:
            if m["prog"].rsplit("/", 1)[-1] not in SSHD_PROGS:
                return None
            hh, mi, ss = (int(x) for x in m["time"].split(":"))
            ts, assumed = _yearless(_MONTH_NO[m["mon"]], int(m["day"]), hh, mi, ss, 0.0, tzi, year, now)
        else:
            m = SYSLOG_RFC3339.match(line)
            if not m:
                _note_error(stats, f"line {n}: not a syslog line")
                return None
            if m["prog"].rsplit("/", 1)[-1] not in SSHD_PROGS:
                return None
            ts = parse_time(m["iso"])
            if ts is None:
                _note_error(stats, f"line {n}: bad timestamp")
                return None
        msg = m["msg"]
        for name, rx in _SSHD_PATTERNS:
            mm = rx.match(msg)
            if mm:
                break
        else:
            return None                                   # other sshd lines (reverse mapping, fatal, ...): skipped
        if name == "skip":
            return None
        g = mm.groupdict()
        if name == "received" and not _is_ip(g["src"]):
            m2 = _RECEIVED_GREEDY.match(msg)
            if m2 and _is_ip(m2["src"]):
                g = m2.groupdict()
        user = g.get("user") or ""
        if user.endswith(" [preauth]"):
            user = user[: -len(" [preauth]")]
        if name == "too_many":
            user = user or g.get("user2") or ""
        src = _addr(g.get("src"))
        attrs: dict[str, Any] = {"host": m["host"], "pid": _int(m["pid"]), "port": _int(g.get("port")),
                                 "method": g.get("method")}
        if name == "invalid":
            kind, sev, text = "invalid_user", "low", f"Invalid user {user} from {src}"
            attrs["invalid_user"] = True
        elif name == "failed":
            inv = "invalid user " if g.get("inv") else ""
            kind, sev, text = "auth_failure", "low", f"Failed {g['method']} for {inv}{user} from {src}"
            attrs["invalid_user"] = bool(inv)
        elif name == "accepted":
            kind, sev, text = "auth_success", "info", f"Accepted {g['method']} for {user} from {src}"
        elif name == "too_many":
            kind, sev = "auth_failure", "low"
            text = "Too many authentication failures" + (f" for {user}" if user else "") + (f" from {src}" if src else "")
        elif name == "max_auth":
            kind, sev, text = "auth_failure", "low", f"Maximum authentication attempts exceeded for {user} from {src}"
        elif name == "received":                          # "11:  [preauth]" leaves the tag in the lazy reason
            reason = safe_text(re.sub(r"\s*\[preauth\]$", "", g.get("reason") or ""), 120)
            kind, sev = "disconnect", "info"
            text = f"Disconnect from {src} ({g['code']}: {reason})" if reason else f"Disconnect from {src} ({g['code']})"
            attrs.update(code=_int(g.get("code")), reason=reason)
        elif name == "closed":
            kind, sev, text = "disconnect", "info", f"Connection closed by {src}"
        elif name == "disconnected":
            kind, sev, text = "disconnect", "info", f"Disconnected from {src}"
        elif name == "no_ident":
            kind, sev, text = "probe", "low", f"No SSH identification from {src}"
        else:                                             # bad_proto: the ident is data, never in the message
            kind, sev, text = "probe", "low", f"Bad SSH protocol identification from {src}"
            attrs["ident"] = g.get("ident")
        if name in ("too_many", "max_auth", "closed", "disconnected") and "invalid user " in msg:
            attrs["invalid_user"] = True
        if assumed:
            attrs["year_assumed"] = True
        return _event(ts, "sshd", sensor or m["host"], kind, src=src, user=user, severity=sev, message=text,
                      raw=line, attrs=attrs)
    return parse_line


# ---------------------------------------------------------------------------- Zeek TSV
def _zeek_path_from_fields(fields: list[str]) -> str:
    f = set(fields)
    if "note" in f:
        return "notice"
    if "conn_state" in f:
        return "conn"
    if f & {"client", "server", "auth_success"}:
        return "ssh"
    return ""


def _zeek_parser(sensor: str, zeek_path: str, stats: dict[str, Any]) -> Callable[[str, int], dict[str, Any] | None]:
    st: dict[str, Any] = {"sep": "\t", "unset": "-", "empty": "(empty)", "fields": None, "path": "", "auto": None}

    def header(line: str) -> None:
        if line.startswith("#separator"):
            val = line[len("#separator"):].strip(" ")
            st["sep"] = re.sub(r"\\x([0-9a-fA-F]{2})", lambda x: chr(int(x[1], 16)), val) or "\t"
            return
        parts = line.split(st["sep"])
        key, val = parts[0], (parts[1] if len(parts) > 1 else "")
        if key == "#fields":
            st["fields"] = parts[1:]
        elif key == "#path":
            st["path"] = val.strip()
        elif key == "#unset_field":
            st["unset"] = val
        elif key == "#empty_field":
            st["empty"] = val
        # #set_separator, #open, #types, #close: nothing to keep

    def parse_line(line: str, n: int) -> dict[str, Any] | None:
        if not line.strip():
            return None
        if line.startswith("#"):
            header(line)
            return None
        cols = line.split(st["sep"])
        if st["fields"]:
            fields = st["fields"]
            path = st["path"] or zeek_path or _zeek_path_from_fields(fields)
        else:
            path = zeek_path or st["path"]
            if not path:
                if st["auto"] is None:                    # headerless: the first data line decides the layout
                    st["auto"] = _ZEEK_BY_COLUMNS.get(len(cols), "")
                    if not st["auto"]:
                        _note_error(stats, f"unknown Zeek layout ({len(cols)} columns): pass zeek_path")
                path = st["auto"]
                if not path:
                    return None
            if path == "conn":
                fields = list(ZEEK_FIELDS["conn"])
                if len(cols) == 21:
                    fields.insert(fields.index("local_orig") + 1, "local_resp")
            else:
                fields = ZEEK_FIELDS.get(path) or []
        stats["zeek_path"] = path
        if path not in ZEEK_FIELDS:
            _note_error(stats, f"Zeek {safe_text(path, 20) or 'unknown'} log is not supported (notice, ssh, conn)")
            return None
        if len(cols) != len(fields):
            _note_error(stats, f"line {n}: {len(cols)} Zeek columns, expected {len(fields)}")
            return None
        r = {k: ("" if v in (st["unset"], st["empty"]) else v) for k, v in zip(fields, cols)}
        ts = parse_time(r.get("ts"))
        if ts is None:
            _note_error(stats, f"line {n}: bad Zeek timestamp")
            return None
        lane = sensor or "zeek"
        if path == "notice":
            note = r.get("note", "")
            attrs = {"zeek_path": "notice", "note": note, "uid": r.get("uid"), "sport": _int(r.get("id.orig_p")),
                     "dport": _int(r.get("p") or r.get("id.resp_p")), "proto": r.get("proto"), "sub": r.get("sub"),
                     "n": _int(r.get("n"))}
            return _event(ts, "zeek", lane, "notice", src=r.get("src") or r.get("id.orig_h"),
                          dst=r.get("dst") or r.get("id.resp_h"), sig=note,
                          severity=NOTE_SEVERITY.get(note, "low"), message=r.get("msg") or note, raw=line, attrs=attrs)
        src, dst = _addr(r.get("id.orig_h")), _addr(r.get("id.resp_h"))
        sport, dport = _int(r.get("id.orig_p")), _int(r.get("id.resp_p"))
        if path == "ssh":
            status = (r.get("status") or "").strip().lower()
            if not status and r.get("auth_success"):
                status = {"t": "success", "f": "failure"}.get(r["auth_success"].strip().lower(), "")
            status = safe_text(status, 24) or "undetermined"
            kind, sev = {"success": ("auth_success", "medium"), "failure": ("auth_failure", "low")}.get(
                status, ("ssh_session", "info"))
            text = f"SSH {status} {src} -> {dst} (Zeek{' heuristic' if status == 'success' else ''})"
            attrs = {"zeek_path": "ssh", "status": status, "direction": r.get("direction"), "client": r.get("client"),
                     "server": r.get("server"), "uid": r.get("uid"), "sport": sport, "dport": dport}
            return _event(ts, "zeek", lane, kind, src=src, dst=dst, severity=sev, message=text, raw=line, attrs=attrs)
        proto, state = r.get("proto", ""), r.get("conn_state", "")
        try:
            duration = round(float(r["duration"]), 6) if r.get("duration") else None
        except ValueError:
            duration = None
        attrs = {"zeek_path": "conn", "uid": r.get("uid"), "sport": sport, "dport": dport, "proto": proto,
                 "service": r.get("service"), "conn_state": state, "duration": duration,
                 "orig_bytes": _int(r.get("orig_bytes")), "resp_bytes": _int(r.get("resp_bytes"))}
        text = f"{proto} {_hostport(src, sport)} -> {_hostport(dst, dport)} {state}"
        return _event(ts, "zeek", lane, "conn", src=src, dst=dst, severity="info", message=text, raw=line, attrs=attrs)
    return parse_line


# ---------------------------------------------------------------------------- Snort / Suricata fast.log
SNORT_FAST = re.compile(
    r"^(?P<mon>\d{2})/(?P<day>\d{2})(?:/(?P<year>\d{2,4}))?-(?P<time>\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+\[\*\*\]\s+"
    r"\[(?P<gid>\d+):(?P<sid>\d+):(?P<rev>\d+)\]\s+(?P<msg>.*?)\s+\[\*\*\]"
    r"(?:\s+\[Classification:\s*(?P<cls>[^\]]*)\])?(?:\s+\[Priority:\s*(?P<prio>\d+)\])?"
    r"\s+\{(?P<proto>[^}]+)\}\s+(?P<a>\S+)\s+->\s+(?P<b>\S+)\s*$")
_PRIORITY_SEVERITY = {1: "high", 2: "medium", 3: "low"}


def _endpoint(token: str, proto: str) -> tuple[str, int | None]:
    """'[addr]:port' (Suricata IPv6), else for TCP/UDP split at the LAST colon when the tail is a port and the head an
    IP (Snort prints IPv6 ports without brackets), else the whole token is the address."""
    if token.startswith("["):
        m = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", token)
        if m:
            return m[1], (int(m[2]) if m[2] else None)
        return token, None
    if proto.upper() in ("TCP", "UDP"):
        head, sep, tail = token.rpartition(":")
        if sep and re.fullmatch(r"[0-9]{1,5}", tail) and _is_ip(head):
            return head, int(tail)
    return token, None


def _snort_parser(sensor: str, year: int | None, tzi: timezone | None, now: float,
                  stats: dict[str, Any]) -> Callable[[str, int], dict[str, Any] | None]:
    def parse_line(line: str, n: int) -> dict[str, Any] | None:
        if not line.strip():
            return None
        m = SNORT_FAST.match(line)
        if not m:
            _note_error(stats, f"line {n}: not a fast.log alert")
            return None
        hms, _, frac = m["time"].partition(".")
        hh, mi, ss = (int(x) for x in hms.split(":"))
        f = float("0." + frac) if frac else 0.0
        mo, d = int(m["mon"]), int(m["day"])
        if m["year"]:                                     # Suricata carries the year: no inference
            y = int(m["year"])
            ts, assumed = _wall(y + 2000 if y < 100 else y, mo, d, hh, mi, ss, f, tzi), False
        else:
            ts, assumed = _yearless(mo, d, hh, mi, ss, f, tzi, year, now)
        proto = m["proto"].strip()
        src, sport = _endpoint(m["a"], proto)
        dst, dport = _endpoint(m["b"], proto)
        src, dst = _addr(src), _addr(dst)
        msg = m["msg"]
        prio = int(m["prio"]) if m["prio"] else None
        sev = "info" if prio is not None and prio >= 4 else _PRIORITY_SEVERITY.get(prio, "medium")
        if C2_RE.search(msg):
            sev = "critical"
        cls = (m["cls"] or "").strip()
        parts = [f"[{m['gid']}:{m['sid']}:{m['rev']}] {msg}"]
        if cls:
            parts.append(cls)
        if prio is not None:
            parts.append(f"P{prio}")
        parts.append(f"{proto} {_hostport(src, sport)} -> {_hostport(dst, dport)}")
        attrs = {"gid": int(m["gid"]), "sid": int(m["sid"]), "rev": int(m["rev"]), "classification": cls,
                 "priority": prio, "proto": proto, "sport": sport, "dport": dport,
                 "year_assumed": True if assumed else None}
        return _event(ts, "snort_fast", sensor or "snort", "ids_alert", src=src, dst=dst, sig=msg, severity=sev,
                      message=" · ".join(parts), raw=line, attrs=attrs)
    return parse_line


# ---------------------------------------------------------------------------- Apache / Nginx combined
COMBINED = re.compile(
    r'^(?P<ip>\S+) \S+ (?P<authuser>\S+) \[(?P<time>[^\]]+)\] "(?P<req>(?:[^"\\]|\\.)*)" (?P<status>\d{3}) '
    r'(?P<bytes>\S+)(?: "(?P<ref>(?:[^"\\]|\\.)*)" "(?P<ua>(?:[^"\\]|\\.)*)")?')
_CLF_TIME = re.compile(r"^(\d{1,2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})(?:\s+([+-]\d{2}:?\d{2}))?$")
_REQUEST = re.compile(r"^(?P<method>[A-Za-z]{1,20}) (?P<target>\S+)(?: (?P<proto>[A-Za-z]{2,10}/[0-9.]{1,5}))?$")
_PROBE_ANYWHERE = ("../", "/etc/passwd")


def _unescape(s: str | None) -> str:
    return re.sub(r'\\(["\\])', r"\1", s or "")


def probe_family(path: str) -> str:
    """The PROBE_FAMILIES id whose prefix starts the (lower-cased) path; '../' and '/etc/passwd' match anywhere."""
    p = (path or "").lower()
    for fid, _label, prefixes in PROBE_FAMILIES:
        for x in prefixes:
            if (x in p) if x in _PROBE_ANYWHERE else p.startswith(x):
                return fid
    return ""


def _combined_parser(sensor: str, stats: dict[str, Any]) -> Callable[[str, int], dict[str, Any] | None]:
    def parse_line(line: str, n: int) -> dict[str, Any] | None:
        if not line.strip():
            return None
        m = COMBINED.match(line)
        if not m:
            _note_error(stats, f"line {n}: not a combined access-log line")
            return None
        t = _CLF_TIME.match(m["time"].strip())
        if not t or t[2].title() not in _MONTH_NO:
            _note_error(stats, f"line {n}: bad access-log time")
            return None
        dt = datetime(int(t[3]), _MONTH_NO[t[2].title()], int(t[1]), int(t[4]), int(t[5]), int(t[6]),
                      tzinfo=_offset(t[7]))
        req = _unescape(m["req"])
        rq = _REQUEST.match(req)
        if rq:
            method, target, proto = rq["method"].upper(), rq["target"], rq["proto"] or ""
            path, qmark, query = target.partition("?")
        else:                                             # malformed request ("-", TLS bytes on port 80, ...)
            method, target, proto, path, qmark, query = "", req[:200], "", req[:200], "", ""
        status = int(m["status"])
        fam = probe_family(path)
        attrs = {"method": method, "path": path, "query_len": len(query) if qmark else None, "status": status,
                 "bytes": int(m["bytes"]) if re.fullmatch(r"[0-9]{1,18}", m["bytes"]) else 0, "referer": _unescape(m["ref"]),
                 "ua": _unescape(m["ua"]), "proto": proto, "probe": fam}
        if attrs["referer"] == "-":
            attrs["referer"] = ""
        if attrs["ua"] == "-":
            attrs["ua"] = ""
        text = f"{method} {safe_text(target, 120)} -> {status}" if method else f"{safe_text(target, 120)} -> {status}"
        user = m["authuser"] if m["authuser"] != "-" else ""
        return _event(dt.timestamp(), "combined", sensor or "web", "http_request", src=m["ip"], user=user,
                      severity="low" if fam else "info", message=text, raw=line, attrs=attrs)
    return parse_line


# ---------------------------------------------------------------------------- parser entry points
def iter_parse(fmt: str, lines: Iterable[str], *, sensor: str = "", year: int | None = None, tz: str = "UTC",
               zeek_path: str = "", now: float | None = None, stats: dict | None = None) -> Iterator[dict]:
    """Events from log lines, one at a time. Unrecognised lines are skipped (counted in stats), never raised.
    The format and options are checked when this is called, so a bad fmt or tz raises ValueError immediately."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown log format {safe_text(fmt, 24)!r}: use one of {', '.join(FORMATS)}")
    tzi = _tzinfo(tz)
    if year in (None, ""):
        yr = None
    else:
        try:
            yr = int(year)
        except (TypeError, ValueError):
            raise ValueError("year must be a number such as 2012") from None
    now_ts = time.time() if now is None else float(now)
    st = stats if stats is not None else {}
    for k, v in (("lines", 0), ("events", 0), ("skipped", 0), ("first_ts", None), ("last_ts", None),
                 ("zeek_path", "")):
        st.setdefault(k, v)
    st.setdefault("errors", [])
    lane = safe_text(sensor, FIELD_LIMITS["sensor"])
    if fmt == "sshd":
        parse_line = _sshd_parser(lane, yr, tzi, now_ts, st)
    elif fmt == "zeek":
        parse_line = _zeek_parser(lane, str(zeek_path or "").strip().lower(), st)
    elif fmt == "snort_fast":
        parse_line = _snort_parser(lane, yr, tzi, now_ts, st)
    else:
        parse_line = _combined_parser(lane, st)
    return _run(parse_line, lines, st)


def parse(fmt: str, lines: Iterable[str], **opts: Any) -> tuple[list[dict], dict]:
    opts.pop("stats", None)
    st: dict[str, Any] = {}
    events = list(iter_parse(fmt, lines, stats=st, **opts))
    st["fmt"] = fmt
    return events, st


def detect_format(lines: list[str], filename: str = "") -> tuple[str, dict]:
    """Guess the format from the first 50 non-empty lines, then from the file name."""
    sample: list[str] = []
    for ln in lines:
        if isinstance(ln, bytes):
            ln = ln.decode("utf-8", "replace")
        ln = str(ln).rstrip("\r\n").lstrip("\ufeff")
        if ln.strip():
            sample.append(ln)
        if len(sample) >= 50:
            break
    name = os.path.basename(str(filename or "")).lower()

    def hinted_zeek() -> str:
        return "notice" if "notice" in name else "ssh" if "ssh.log" in name else "conn" if "conn" in name else ""

    if any(ln.startswith(("#fields", "#separator")) for ln in sample):
        path = next((re.split(r"[\t ]+", ln, maxsplit=1)[1].strip() for ln in sample
                     if ln.startswith("#path") and len(re.split(r"[\t ]+", ln, maxsplit=1)) > 1), "")
        if not path:
            fields = next((ln.split("\t")[1:] for ln in sample if ln.startswith("#fields")), [])
            path = _zeek_path_from_fields(fields) or hinted_zeek()
        return "zeek", {"zeek_path": path}
    data = [ln for ln in sample if not ln.startswith("#")]
    if data:
        cols = data[0].split("\t")
        if len(cols) >= 10 and re.fullmatch(r"\d+(?:\.\d+)?", cols[0].strip()):
            return "zeek", {"zeek_path": _ZEEK_BY_COLUMNS.get(len(cols), "") or hinted_zeek()}
        half = len(data) / 2
        if sum(1 for ln in data if SNORT_FAST.match(ln)) >= half:
            return "snort_fast", {}
        if sum(1 for ln in data if COMBINED.match(ln)) >= half:
            return "combined", {}
        syslog = [m for m in (SYSLOG_TRADITIONAL.match(ln) or SYSLOG_RFC3339.match(ln) for ln in data) if m]
        if len(syslog) >= half and any(m["prog"].rsplit("/", 1)[-1] in SSHD_PROGS for m in syslog):
            return "sshd", {}
    if "notice" in name:
        return "zeek", {"zeek_path": "notice"}
    if "ssh.log" in name:
        return "zeek", {"zeek_path": "ssh"}
    if "conn" in name:
        return "zeek", {"zeek_path": "conn"}
    if "alert" in name or "fast" in name:
        return "snort_fast", {}
    if "access" in name:
        return "combined", {}
    if "auth" in name or "secure" in name:
        return "sshd", {}
    return "", {}


def open_lines(path: Any) -> Iterator[str]:
    """Lines of a text or gzip file (UTF-8, undecodable bytes replaced), without line endings.
    Archives are refused at call time: ValueError('unpack the archive first')."""
    p = Path(path)
    name = p.name.lower()
    if name.endswith((".7z", ".zip")):
        raise ValueError("unpack the archive first")
    with open(p, "rb") as f:
        head = f.read(6)
    if head.startswith(b"7z\xbc\xaf\x27\x1c") or head.startswith(b"PK\x03\x04"):
        raise ValueError("unpack the archive first")
    # CONTRACT-DEVIATION: gzip is recognised by its 1f 8b magic only, not by the .gz suffix, so a mis-named plain-text
    # "x.log.gz" is still read instead of failing with BadGzipFile; every real gzip file has the magic.
    return _read_lines(p, head[:2] == b"\x1f\x8b")


def _read_lines(p: Path, gz: bool) -> Iterator[str]:
    opener: Any = gzip.open if gz else open
    with opener(p, "rb") as f:
        first = True
        try:
            for raw in f:
                line = raw.decode("utf-8", "replace")
                if line.endswith("\n"):
                    line = line[:-1]
                if line.endswith("\r"):
                    line = line[:-1]
                if first:
                    line, first = line.lstrip("\ufeff"), False
                yield line
        except EOFError:                                  # truncated gzip: keep what was readable
            return


def _hook_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in list(attrs.items())[:40]:
        key = safe_text(k, 40)
        if not key or v is None:
            continue
        if isinstance(v, (bool, int, float, str)):
            out[key] = v
    return out


def normalize_event(obj: dict, *, sensor: str = "hook", now: float | None = None) -> dict | None:
    """A structured event posted to the logs hook -> the event dict, or None when it carries nothing to show."""
    if not isinstance(obj, dict):
        return None
    if not any(str(obj.get(k) or "").strip() for k in ("message", "src", "sig")):
        return None
    ts = parse_time(obj.get("ts"))
    if ts is None:
        ts = time.time() if now is None else float(now)
    kind = str(obj.get("kind") or "").strip().lower()
    sev = str(obj.get("severity") or "").strip().lower()
    source = str(obj.get("source") or "").strip().lower()
    attrs = obj.get("attrs") if isinstance(obj.get("attrs"), dict) else {}
    try:
        raw = json.dumps(obj, ensure_ascii=True, default=str)
    except (TypeError, ValueError):
        raw = ""
    return _event(ts, source if source in SOURCES else "custom", obj.get("sensor") or sensor or "hook",
                  kind if kind in KINDS else "custom", src=obj.get("src"), dst=obj.get("dst"), user=obj.get("user"),
                  sig=obj.get("sig"), severity=sev if sev in SEV_RANK else "info", message=obj.get("message"),
                  raw=raw, attrs=_hook_attrs(attrs))


def compact_event(ev: dict, msg_len: int = 160) -> dict:
    """The row agents and timeline marks see: short keys, empty values dropped, time without a year when assumed."""
    a = _attrs_of(ev)
    out = {"id": ev.get("id"), "t": fmt_ts(ev.get("ts"), year=not a.get("year_assumed")),
           "sensor": safe_text(ev.get("sensor"), FIELD_LIMITS["sensor"]), "kind": safe_text(ev.get("kind"), 24),
           "src": safe_text(ev.get("src"), 64), "dst": safe_text(ev.get("dst"), 64),
           "user": safe_text(ev.get("user"), FIELD_LIMITS["user"]), "sig": safe_text(ev.get("sig"), FIELD_LIMITS["sig"]),
           "sev": safe_text(ev.get("severity"), 10), "msg": safe_text(ev.get("message"), msg_len)}
    return {k: v for k, v in out.items() if v != "" or k in ("id", "t", "kind")}


# ---------------------------------------------------------------------------- detection
def _ts(e: dict) -> float:
    return float(e.get("ts") or 0.0)


def _order(e: dict) -> tuple[float, int]:
    return (_ts(e), e.get("id") or 0)


def _target(e: dict) -> str:
    return e.get("dst") or e.get("sensor") or ""


def _evidence(events: list[dict], cap: int, group_key: Callable[[dict], Any] | None = None) -> list[int]:
    """Evidence sample: each group's first and last event, the overall first and last, then evenly spaced events;
    at most `cap` ids, in (ts, id) order."""
    evs = sorted(events, key=_order)
    n = len(evs)
    if cap <= 0 or not n:
        return []
    if n <= cap:
        return [e["id"] for e in evs]
    picked: list[int] = []
    seen: set[int] = set()

    def add(i: int) -> None:
        if i not in seen:
            seen.add(i)
            picked.append(i)

    if group_key is not None:
        first: dict[Any, int] = {}
        last: dict[Any, int] = {}
        for i, e in enumerate(evs):
            k = group_key(e)
            first.setdefault(k, i)
            last[k] = i
        for k in first:                                   # groups in order of first appearance
            add(first[k])
            add(last[k])
    add(0)
    add(n - 1)
    if len(picked) >= cap:
        picked = picked[:cap]
    elif cap > 1:
        for i in range(cap):
            add(round(i * (n - 1) / (cap - 1)))
            if len(picked) >= cap:
                break
    return [evs[i]["id"] for i in sorted(picked)]


def _params(params: dict | None) -> dict[str, Any]:
    p = dict(DEFAULT_PARAMS)
    for k, v in (params or {}).items():
        if k in p and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            p[k] = max(1, int(v)) if isinstance(DEFAULT_PARAMS[k], int) else float(v)
    return p


def _max_in_window(times: list[float], window: float) -> int:
    best = i = 0
    for j, t in enumerate(times):
        while t - times[i] > window:
            i += 1
        best = max(best, j - i + 1)
    return best


def _top(counter: collections.Counter, n: int) -> list[str]:
    return [k for k, _ in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def _uniq(items: Iterable[str], cap: int) -> list[str]:
    out: list[str] = []
    for x in items:
        if x and x not in out:
            out.append(x)
            if len(out) >= cap:
                break
    return out


# CONTRACT-DEVIATION: title wording only. Count words agree in number ('1 username', '1 host on one port'), and the
# brute-force title leaves out the username count when it is 0: Zeek ssh.log records no usernames, so '0 usernames'
# would state something the data does not show. The '(s)' forms are kept as the contract wrote them.
def _title(d: dict) -> str:
    """Detection title from structured fields only (addresses, counts, rule constants, note/signature names)."""
    r, c, det = d.get("rule"), d.get("counts") or {}, d.get("details") or {}
    src = d.get("src") or []
    s0 = src[0] if src else ""

    def num(k: str) -> int:
        try:
            return int(c.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    def n(k: str, one: str = "", many: str = "") -> str:
        v = num(k)
        return f"{v:,}" + (f" {one if v == 1 else many}" if one else "")

    if r == "ssh_bruteforce":
        if not num("failures"):
            t = f"SSH password guessing by {s0} (Zeek notice ×{n('zeek_notices')})"
        else:
            names = f", {n('usernames', 'username', 'usernames')}" if num("usernames") else ""
            t = f"SSH brute force from {s0}: {n('failures', 'failed login', 'failed logins')}{names}, {n('targets')} target(s)"
            if num("zeek_notices"):
                t += " + Zeek password-guessing notice"
    elif r == "success_after_failures":
        h = bool(det.get("heuristic"))
        tgt = det.get("target") or ((d.get("dst") or [""])[0])
        t = (f"Login {'likely ' if h else ''}succeeded from {s0} to {tgt} after "
             f"{n('failures', 'failed attempt', 'failed attempts')}{' (Zeek heuristic)' if h else ''}")
    elif r == "wordlist_fingerprint":
        t = (f"Same {n('length')}-username list from {n('sources', 'source', 'sources')}: one campaign "
             f"(fingerprint {det.get('fp', '')})")
    elif r == "scan":
        parts = [n(k, one, many) for k, one, many in (
            ("zeek_port_scans", "Zeek port scan(s)", "Zeek port scan(s)"),
            ("zeek_address_scans", "Zeek address scan(s)", "Zeek address scan(s)"),
            ("max_ports_one_host", "port on one host", "ports on one host"),
            ("max_hosts_one_port", "host on one port", "hosts on one port")) if num(k) > 0]
        t = f"{s0} scanning: " + ", ".join(parts)
    elif r == "ids_high":
        if det.get("c2"):
            pair = list(det.get("pair") or ["", ""]) + ["", ""]
            t = f"C2 traffic ({det.get('family', '')}) between {pair[0]} and {pair[1]}: {n('alerts', 'alert', 'alerts')}"
        else:
            t = (f"{n('alerts', 'priority-1 IDS alert', 'priority-1 IDS alerts')} from {s0} "
                 f"({n('signatures', 'signature', 'signatures')}, {n('targets', 'target', 'targets')})")
    elif r == "web_probe":
        t = (f"{n('requests', 'request', 'requests')} for {det.get('label', '')} paths from "
             f"{n('sources', 'source', 'sources')}")
        t += " (never served)" if not num("served") else f" ({n('served')} answered 2xx)"
    else:
        t = str(r or "detection")
    return safe_text(t, 200)


def _detection(rule: str, did: str, severity: str, matched: list[dict], evidence: list[int], *, src: list[str],
               dst: list[str], users: list[str], counts: dict[str, int], details: dict[str, Any]) -> dict[str, Any]:
    details = {**details, "year_assumed": any(bool(_attrs_of(e).get("year_assumed")) for e in matched)}
    times = [float(e.get("ts") or 0.0) for e in matched]
    d = {"id": did, "rule": rule, "severity": severity, "title": "", "src": _uniq(src, 50), "dst": _uniq(dst, 50),
         "users": _uniq(users, 20), "sources": sorted({e.get("source") or "" for e in matched} - {""}),
         "sensors": sorted({e.get("sensor") or "" for e in matched} - {""}), "evidence": evidence,
         "evidence_total": len(matched), "counts": counts, "details": details,
         "first_ts": min(times) if times else 0.0, "last_ts": max(times) if times else 0.0}
    d["title"] = _title(d)
    return d


def _rule_bruteforce(evs: list[dict], p: dict) -> list[dict]:
    fails: dict[str, list[dict]] = collections.defaultdict(list)
    notices: dict[str, list[dict]] = collections.defaultdict(list)
    for e in evs:
        s = e.get("src")
        if not s:
            continue
        if e.get("kind") in FAIL_KINDS:
            fails[s].append(e)
        elif e.get("kind") == "notice" and e.get("sig") == "SSH::Password_Guessing":
            notices[s].append(e)
    out = []
    for s in sorted(set(fails) | set(notices)):
        f, nt = fails.get(s, []), notices.get(s, [])
        m = _max_in_window([_ts(e) for e in f], p["bf_window_s"])
        if m < p["bf_failures"] and not nt:
            continue
        users = collections.Counter(e["user"] for e in f if e.get("user"))
        targets = collections.Counter(_target(e) for e in f if _target(e))
        matched = sorted(f + nt, key=_order)
        counts = {"failures": len(f), "max_in_window": m, "usernames": len(users), "targets": len(targets),
                  "zeek_notices": len(nt)}
        out.append(_detection("ssh_bruteforce", f"ssh_bruteforce:{s}", "medium", matched,
                              _evidence(matched, p["evidence_cap"]), src=[s], dst=_top(targets, 20),
                              users=_top(users, 5), counts=counts, details={}))
    return out


def _rule_success_after_failures(evs: list[dict], p: dict) -> list[dict]:
    fails: dict[str, list[dict]] = collections.defaultdict(list)
    for e in evs:
        if e.get("kind") in FAIL_KINDS and e.get("src"):
            fails[e["src"]].append(e)
    times = {s: [_ts(e) for e in fl] for s, fl in fails.items()}
    groups: dict[tuple[str, str], list[tuple[dict, int, int]]] = {}
    for e in evs:
        s = e.get("src")
        if e.get("kind") != "auth_success" or not s or s not in fails:
            continue
        t = _ts(e)
        lo = bisect.bisect_left(times[s], t - p["saf_window_s"])
        hi = bisect.bisect_right(times[s], t)
        if hi - lo >= p["saf_failures"]:
            groups.setdefault((s, _target(e)), []).append((e, lo, hi))
    out = []
    for (s, tgt), items in groups.items():
        successes = [e for e, _, _ in items]
        heuristic = all(e.get("source") == "zeek" for e in successes)
        window: dict[Any, dict] = {}
        for _, lo, hi in items:
            for f in fails[s][lo:hi]:
                window[f["id"]] = f
        win = sorted(window.values(), key=_order)
        shown = successes[:10]
        ids = {e["id"] for e in shown} | set(_evidence(win, max(0, p["evidence_cap"] - len(shown))))
        by_id = {e["id"]: e for e in shown + win}
        evidence = sorted(ids, key=lambda i: _order(by_id[i]))
        counts = {"failures": max(hi - lo for _, lo, hi in items), "successes": len(successes)}
        out.append(_detection("success_after_failures", f"success_after_failures:{s}:{tgt}",
                              "medium" if heuristic else "high", sorted(successes + win, key=_order), evidence,
                              src=[s], dst=[tgt], users=[e.get("user") or "" for e in successes], counts=counts,
                              details={"heuristic": heuristic, "target": tgt}))
    return out


def _rule_wordlist(evs: list[dict], p: dict) -> list[dict]:
    lists: dict[str, list[dict]] = collections.defaultdict(list)
    for e in evs:
        if (e.get("source") == "sshd" and e.get("kind") in ("invalid_user", "auth_failure") and e.get("user")
                and e.get("src")):
            lists[e["src"]].append(e)
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for s, el in lists.items():
        if len(el) >= p["wl_min_len"]:
            fp = hashlib.md5(("\n".join(e["user"] for e in el) + "\n").encode()).hexdigest()
            groups[fp].append(s)
    cap = p["evidence_cap"]
    out = []
    for fp, srcs in groups.items():
        if len(srcs) < p["wl_min_sources"]:
            continue
        srcs.sort(key=lambda s: _order(lists[s][0]))
        matched = sorted((e for s in srcs for e in lists[s]), key=_order)
        length = len(lists[srcs[0]])
        # per source its first 3 and last 3 attempts, taken round-robin so every source is shown before any cap bites
        ranked = []
        for s in srcs:
            el = lists[s]
            idx = list(dict.fromkeys(i for i in (0, len(el) - 1, 1, len(el) - 2, 2, len(el) - 3) if 0 <= i < len(el)))
            ranked.append([el[i] for i in idx])
        picked: dict[Any, dict] = {}
        for rank in range(6):
            for row in ranked:
                if rank < len(row) and len(picked) < cap:
                    picked.setdefault(row[rank]["id"], row[rank])
        evidence = [e["id"] for e in sorted(picked.values(), key=_order)]
        targets = collections.Counter(_target(e) for e in matched if _target(e))
        per_source = [{"src": s, "attempts": len(lists[s]), "first_ts": _ts(lists[s][0]),
                       "last_ts": _ts(lists[s][-1])} for s in srcs]
        out.append(_detection("wordlist_fingerprint", f"wordlist_fingerprint:{fp[:8]}", "medium", matched, evidence,
                              src=srcs, dst=_top(targets, 50), users=_uniq((e["user"] for e in lists[srcs[0]]), 5),
                              counts={"sources": len(srcs), "attempts": len(matched), "length": length},
                              details={"fingerprint": fp, "fp": fp[:8], "length": length, "per_source": per_source}))
    return out


def _rule_scan(evs: list[dict], p: dict) -> list[dict]:
    notices: dict[str, list[dict]] = collections.defaultdict(list)
    ports: dict[tuple, set] = collections.defaultdict(set)      # (src, bucket, dst) -> dports
    hosts: dict[tuple, set] = collections.defaultdict(set)      # (src, bucket, dport) -> dsts
    w = p["scan_window_s"]
    for e in evs:
        kind, s, d = e.get("kind"), e.get("src"), e.get("dst")
        if kind == "notice" and s and str(e.get("sig") or "").startswith("Scan::"):
            notices[s].append(e)
        elif kind in ("ids_alert", "conn") and s and d:
            dp = _attrs_of(e).get("dport")
            if isinstance(dp, int) and not isinstance(dp, bool):
                b = int(_ts(e) // w)
                ports[(s, b, d)].add(dp)
                hosts[(s, b, dp)].add(d)

    def best(table: dict[tuple, set]) -> dict[str, tuple[int, int, Any]]:
        out: dict[str, tuple[int, int, Any]] = {}
        for (s, b, k), vals in table.items():
            cur = out.get(s)
            cand = (len(vals), b, k)
            if cur is None or cand[0] > cur[0] or (cand[0] == cur[0] and (b, str(k)) < (cur[1], str(cur[2]))):
                out[s] = cand
        return out

    bp, bh = best(ports), best(hosts)
    firing = sorted(s for s in set(notices) | set(bp) | set(bh)
                    if notices.get(s) or bp.get(s, (0,))[0] >= p["scan_ports"] or bh.get(s, (0,))[0] >= p["scan_hosts"])
    if not firing:
        return []
    want_p = {(s, bp[s][1], bp[s][2]): s for s in firing if s in bp}
    want_h = {(s, bh[s][1], bh[s][2]): s for s in firing if s in bh}
    bucket_events: dict[str, list[dict]] = collections.defaultdict(list)
    for e in evs:
        if e.get("kind") not in ("ids_alert", "conn") or not e.get("src") or not e.get("dst"):
            continue
        dp = _attrs_of(e).get("dport")
        if not isinstance(dp, int) or isinstance(dp, bool):
            continue
        s, b = e["src"], int(_ts(e) // w)
        if (s, b, e["dst"]) in want_p or (s, b, dp) in want_h:
            bucket_events[s].append(e)
    out = []
    for s in firing:
        nt = notices.get(s, [])
        be = bucket_events.get(s, [])
        dsts = [e.get("dst") or "" for e in nt if e.get("sig") == "Scan::Port_Scan"]
        if s in bp:
            dsts.append(bp[s][2])
        if s in bh:
            dsts += sorted(hosts[(s, bh[s][1], bh[s][2])])
        matched = sorted(nt + be, key=_order)
        counts = {"zeek_port_scans": sum(1 for e in nt if e.get("sig") == "Scan::Port_Scan"),
                  "zeek_address_scans": sum(1 for e in nt if e.get("sig") == "Scan::Address_Scan"),
                  "max_ports_one_host": bp[s][0] if s in bp else 0, "max_hosts_one_port": bh[s][0] if s in bh else 0}
        evidence = _evidence(matched, p["evidence_cap"],
                             group_key=lambda e: e.get("sig") if e.get("kind") == "notice" else "")
        out.append(_detection("scan", f"scan:{s}", "low", matched, evidence, src=[s], dst=_uniq(dsts, 20), users=[],
                              counts=counts, details={}))
    return out


def _c2_family(sig: str) -> str:
    m = C2_RE.search(sig or "")
    if not m:
        return ""
    key = _SPACES.sub(" ", m.group(1).lower().replace("-", " ")).strip()
    return C2_FAMILY.get(key, key)


def _rule_ids(evs: list[dict], p: dict) -> list[dict]:
    c2: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    p1: dict[str, list[dict]] = collections.defaultdict(list)
    for e in evs:
        if e.get("kind") != "ids_alert":
            continue
        if C2_RE.search(e.get("sig") or ""):
            a, b = sorted((e.get("src") or "", e.get("dst") or ""))
            c2[(a, b)].append(e)
        elif _attrs_of(e).get("priority") == 1 and e.get("src"):
            p1[e["src"]].append(e)
    cap = p["evidence_cap"]
    out = []
    for (a, b), group in c2.items():
        sigs = collections.Counter(e.get("sig") or "" for e in group)
        counts = {"alerts": len(group), "signatures": len(sigs),
                  "a_to_b": sum(1 for e in group if e.get("src") == a and e.get("dst") == b),
                  "b_to_a": sum(1 for e in group if e.get("src") == b and e.get("dst") == a)}
        details = {"c2": True, "family": _c2_family(group[0].get("sig") or ""), "pair": [a, b],
                   "signatures": _top(sigs, 5)}
        out.append(_detection("ids_high", f"ids_high:c2:{a}:{b}", "critical", group,
                              _evidence(group, cap, group_key=lambda e: (e.get("src"), e.get("dst"))),
                              src=[a, b], dst=[], users=[], counts=counts, details=details))
    for s, group in p1.items():
        if len(group) < p["ids_min_alerts"]:
            continue
        sigs = collections.Counter(e.get("sig") or "" for e in group)
        targets = collections.Counter(_target(e) for e in group if _target(e))
        top = sorted(sigs.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        out.append(_detection("ids_high", f"ids_high:p1:{s}", "medium", group, _evidence(group, cap), src=[s],
                              dst=_top(targets, 20), users=[],
                              counts={"alerts": len(group), "signatures": len(sigs), "targets": len(targets)},
                              details={"c2": False, "top_signatures": [[k, v] for k, v in top]}))
    return out


def _served(e: dict) -> bool:
    st = _attrs_of(e).get("status")
    return isinstance(st, int) and 200 <= st <= 299


def _rule_web(evs: list[dict], p: dict) -> list[dict]:
    families = {fid: (label, prefixes) for fid, label, prefixes in PROBE_FAMILIES}
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for e in evs:
        if e.get("kind") == "http_request":
            fam = _attrs_of(e).get("probe")
            if fam in families:                           # only known families: titles stay constant text
                groups[fam].append(e)
    out = []
    for fid, group in groups.items():
        if len(group) < p["probe_min"]:
            continue
        label, prefixes = families[fid]
        sources = collections.Counter(e.get("src") or "" for e in group if e.get("src"))
        served = sum(1 for e in group if _served(e))
        counts = {"requests": len(group), "sources": len(sources), "served": served}
        out.append(_detection("web_probe", f"web_probe:{fid}", "medium" if served else "low", group,
                              _evidence(group, p["evidence_cap"], group_key=_served), src=_top(sources, 20),
                              dst=sorted({e.get("sensor") or "" for e in group} - {""}), users=[], counts=counts,
                              details={"family": fid, "label": label, "example_path": prefixes[0]}))
    return out


def detect(events: list[dict], params: dict | None = None) -> list[dict]:
    """Run every rule over stored event rows. Deterministic: same input, same output, no clock, no randomness."""
    p = _params(params)
    evs = []
    for i, e in enumerate(events):
        if e.get("id") is None or isinstance(e.get("attrs"), str):
            e = {**e, "id": e.get("id") if e.get("id") is not None else i + 1, "attrs": _attrs_of(e)}
        evs.append(e)
    evs.sort(key=_order)
    dets = (_rule_success_after_failures(evs, p) + _rule_ids(evs, p) + _rule_wordlist(evs, p)
            + _rule_bruteforce(evs, p) + _rule_scan(evs, p) + _rule_web(evs, p))
    dets.sort(key=lambda d: (-SEV_RANK[d["severity"]], RULES.index(d["rule"]), -d["evidence_total"], -d["last_ts"],
                             d["id"]))
    return dets


def _json_field(v: Any, default: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v or "null") or default
        except ValueError:
            return default
    return v if v is not None else default


def merge_detection(old: dict | None, new: dict) -> dict:
    """Fold a fresh detection into the stored one: widest time span, highest severity, largest counts, union of
    evidence and entities. Storage fields (run_id, created, row_id) are left as they are."""
    if old is None:
        return new
    m = copy.deepcopy(new)
    o_counts, o_det = _json_field(old.get("counts"), {}), _json_field(old.get("details"), {})
    times = [t for t in (old.get("first_ts"), new.get("first_ts")) if t is not None]
    m["first_ts"] = min(times) if times else None
    times = [t for t in (old.get("last_ts"), new.get("last_ts")) if t is not None]
    m["last_ts"] = max(times) if times else None
    if SEV_RANK.get(old.get("severity"), 0) > SEV_RANK.get(new.get("severity"), 0):
        m["severity"] = old["severity"]

    def number(x: Any) -> bool:
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    counts = dict(m.get("counts") or {})
    for k, v in o_counts.items():
        if k not in counts:
            counts[k] = v
        elif number(v) and number(counts[k]):
            counts[k] = max(v, counts[k])
    m["counts"] = counts
    m["evidence_total"] = max(int(old.get("evidence_total") or 0), int(new.get("evidence_total") or 0))
    o_ev, n_ev = _json_field(old.get("evidence"), []), list(new.get("evidence") or [])
    cap = max(DEFAULT_PARAMS["evidence_cap"], len(o_ev), len(n_ev))
    ev = sorted(set(o_ev) | set(n_ev))
    m["evidence"] = ev if len(ev) <= cap else ev[: cap // 2] + ev[len(ev) - (cap - cap // 2):]
    for key, limit in (("src", 50), ("dst", 50), ("users", 20)):
        merged = list(new.get(key) or [])
        merged += [x for x in _json_field(old.get(key), []) if x not in merged]
        m[key] = merged[:limit]
    for key in ("sources", "sensors"):
        m[key] = sorted(set(new.get(key) or []) | set(_json_field(old.get(key), [])))
    m["details"] = {**(new.get("details") or {}),
                    "year_assumed": bool(o_det.get("year_assumed")) or bool((new.get("details") or {}).get("year_assumed"))}
    m["title"] = _title(m)
    return m


# ---------------------------------------------------------------------------- entity graph
def _node_id(value: str) -> str:
    return f"ip:{value}" if _is_ip(value) else f"host:{value}"


def graph(events: list[dict], detections: list[dict] = (), *, site_map: dict | None = None,
          camera_events: list[dict] = (), site_window_s: int = 300, max_nodes: int = 40, max_edges: int = 80,
          focus: str = "") -> dict:
    """Entities (IPs, hosts, users, signatures, cameras) and the edges between them, each edge with its evidence ids.
    Cameras are linked only through the owner's explicit site_map, never guessed."""
    evs = sorted((e if e.get("id") is not None else {**e, "id": i + 1} for i, e in enumerate(events)), key=_order)
    dets = list(detections or [])
    nodes: dict[str, dict] = {}
    ent: dict[str, dict] = {}            # ip/host value -> {"events", "out", "in", "sensors"}
    users: dict[str, dict] = {}          # user -> {"attempts", "sources"}
    sigs: dict[str, dict] = {}           # sig -> {"count", "severity", "source"}
    edges: dict[tuple[str, str, str], dict] = {}
    sensor_names = {e.get("sensor") or "" for e in evs} - {""}

    def entity(value: str) -> dict:
        st = ent.get(value)
        if st is None:
            st = ent[value] = {"events": 0, "out": 0, "in": 0, "sensors": set()}
        return st

    def add_edge(a: str, b: str, kind: str, e: dict) -> None:
        t = _ts(e)
        ed = edges.get((a, b, kind))
        if ed is None:
            ed = edges[(a, b, kind)] = {"events": [], "count": 0, "first": t, "last": t, "sev": 0, "ya": False}
        ed["events"].append(e)
        ed["count"] += 1
        ed["first"] = min(ed["first"], t)
        ed["last"] = max(ed["last"], t)
        ed["sev"] = max(ed["sev"], SEV_RANK.get(e.get("severity"), 0))
        ed["ya"] = ed["ya"] or bool(_attrs_of(e).get("year_assumed"))

    for e in evs:
        kind, s, d, sensor = e.get("kind"), e.get("src") or "", e.get("dst") or "", e.get("sensor") or ""
        tgt = d or sensor
        if s:
            st = entity(s)
            st["events"] += 1
            st["out"] += 1
            if sensor:
                st["sensors"].add(sensor)
        if tgt and tgt != s:
            st = entity(tgt)
            st["events"] += 1
            st["in"] += 1
            if sensor:
                st["sensors"].add(sensor)
        u = e.get("user") or ""
        if kind in FAIL_KINDS + ("auth_success",) and s:
            failed = kind != "auth_success"
            if tgt:
                add_edge(_node_id(s), _node_id(tgt), "failed_login" if failed else "login", e)
            if u:
                add_edge(_node_id(s), f"user:{u}", "tried_user" if failed else "login_as", e)
        elif kind == "ids_alert":
            if s and d:
                add_edge(_node_id(s), _node_id(d), "alert", e)
            if s and e.get("sig") and SEV_RANK.get(e.get("severity"), 0) >= SEV_RANK["medium"]:
                add_edge(_node_id(s), f"sig:{e['sig']}", "triggered", e)
        elif kind == "notice":
            sig = e.get("sig") or ""
            if s and d:
                add_edge(_node_id(s), _node_id(d), "scan" if sig.startswith("Scan::") else "notice", e)
            if s and sig and SEV_RANK.get(e.get("severity"), 0) >= SEV_RANK["medium"]:
                add_edge(_node_id(s), f"sig:{sig}", "triggered", e)
        elif kind == "probe" and s and sensor:
            add_edge(_node_id(s), f"host:{sensor}", "probe", e)
        elif kind == "http_request" and s and sensor and _attrs_of(e).get("probe"):
            add_edge(_node_id(s), f"host:{sensor}", "http", e)
        elif kind == "custom" and s and d:
            add_edge(_node_id(s), _node_id(d), "event", e)
        if u and kind in FAIL_KINDS + ("auth_success",) and s:
            us = users.setdefault(u, {"attempts": 0, "sources": set()})
            us["attempts"] += 1
            us["sources"].add(s)
        if kind in ("ids_alert", "notice") and s and e.get("sig") and \
                SEV_RANK.get(e.get("severity"), 0) >= SEV_RANK["medium"]:
            sg = sigs.setdefault(e["sig"], {"count": 0, "severity": "info", "source": e.get("source") or ""})
            sg["count"] += 1
            if SEV_RANK.get(e.get("severity"), 0) > SEV_RANK[sg["severity"]]:
                sg["severity"] = e["severity"]

    # detection-derived: one node per shared wordlist, one edge from every source that used it
    by_id = {e["id"]: e for e in evs}
    wordlists: dict[str, dict] = {}
    for det in dets:
        if det.get("rule") != "wordlist_fingerprint":
            continue
        dd, cc = det.get("details") or {}, det.get("counts") or {}
        nid = f"sig:wordlist:{dd.get('fp', '')}"
        wordlists[nid] = {"label": f"wordlist {dd.get('fp', '')} · {dd.get('length', cc.get('length', 0))} usernames",
                          "count": int(cc.get("attempts") or 0), "severity": det.get("severity") or "medium"}
        for ps in dd.get("per_source") or []:
            s = ps.get("src") or ""
            if not s:
                continue
            sample = [i for i in det.get("evidence") or [] if (by_id.get(i) or {}).get("src") == s][:20]
            edges[(_node_id(s), nid, "used_wordlist")] = {
                "events": None, "evidence": sample, "count": int(ps.get("attempts") or 0),
                "first": ps.get("first_ts"), "last": ps.get("last_ts"),
                "sev": SEV_RANK.get(det.get("severity"), 0), "ya": bool(dd.get("year_assumed"))}

    # nodes for every edge end
    for (a, b, _k) in list(edges):
        for nid in (a, b):
            if nid in nodes:
                continue
            typ, _, value = nid.partition(":")
            if typ in ("ip", "host"):
                nodes[nid] = {"id": nid, "type": typ, "label": safe_text(value, 60), "props": {}}
            elif typ == "user":
                nodes[nid] = {"id": nid, "type": "user", "label": safe_text(value, 60), "props": {}}
            elif nid in wordlists:
                w = wordlists[nid]
                nodes[nid] = {"id": nid, "type": "sig", "label": safe_text(w["label"], 60),
                              "props": {"rule": "wordlist_fingerprint", "count": w["count"], "severity": w["severity"],
                                        "source": "sshd"}}
            else:
                nodes[nid] = {"id": nid, "type": "sig", "label": safe_text(value, 60), "props": {}}

    # site map: host -> camera, only for pairs the owner set, only with camera events near that host's log events
    camera_links: dict[tuple[str, str, str], dict] = {}
    if site_map and camera_events:
        by_value = {nid.partition(":")[2].lower(): nid for nid, n in nodes.items() if n["type"] in ("ip", "host")}
        cams: dict[str, list[dict]] = collections.defaultdict(list)
        for c in camera_events:
            if c.get("ts") is not None:
                cams[str(c.get("camera") or "").strip().lower()].append(c)
        keys = {str(h).strip().lower() for h in site_map}
        host_evs: dict[str, list[dict]] = collections.defaultdict(list)
        for e in evs:
            seen = set()
            for v in (e.get("src"), e.get("dst"), e.get("sensor")):
                lv = str(v or "").lower()
                if lv in keys and lv not in seen:
                    seen.add(lv)
                    host_evs[lv].append(e)
        for host, cam in site_map.items():
            key, name = str(host).strip().lower(), safe_text(cam, 60)
            nid = by_value.get(key)
            cev = sorted(cams.get(name.lower(), []), key=_order)
            hev = host_evs.get(key, [])
            if not nid or not cev or not hev:
                continue
            ht = [_ts(e) for e in hev]
            ct = [_ts(c) for c in cev]

            def near(arr: list[float], t: float) -> bool:
                return bisect.bisect_right(arr, t + site_window_s) > bisect.bisect_left(arr, t - site_window_s)

            mc = [c for c in cev if near(ht, _ts(c))]
            if not mc:
                continue
            ml = [e for e in hev if near(ct, _ts(e))]
            cid = f"camera:{name}"
            nodes[cid] = {"id": cid, "type": "camera", "label": name,
                          "props": {"events": len(mc), "triggered": sum(1 for c in mc if c.get("triggered"))}}
            camera_links[(nid, cid, "site_map")] = {
                "events": None, "evidence": _evidence(ml, 10), "camera_evidence": _evidence(mc, 10),
                "count": len(ml), "first": min(_ts(ml[0]), _ts(mc[0])),
                "last": max(_ts(ml[-1]), _ts(mc[-1])),
                "sev": max(SEV_RANK.get(e.get("severity"), 0) for e in ml),
                "ya": any(bool(_attrs_of(e).get("year_assumed")) for e in ml)}
    edges.update(camera_links)

    # props and scores
    involved: dict[str, list[dict]] = collections.defaultdict(list)
    det_sigs: set[str] = set()
    for det in dets:
        dd, cc = det.get("details") or {}, det.get("counts") or {}
        vals = set(det.get("src") or []) | set(det.get("dst") or []) | set(det.get("users") or []) | set(
            dd.get("pair") or [])
        for v in vals:
            if v:
                involved[v].append(det)
        det_sigs.update(dd.get("signatures") or [])
        det_sigs.update(s for s, _ in dd.get("top_signatures") or [])
        if det.get("rule") == "ssh_bruteforce" and cc.get("zeek_notices"):
            det_sigs.add("SSH::Password_Guessing")
        if det.get("rule") == "scan":
            if cc.get("zeek_port_scans"):
                det_sigs.add("Scan::Port_Scan")
            if cc.get("zeek_address_scans"):
                det_sigs.add("Scan::Address_Scan")

    def sev_score(ds: list[dict]) -> int:
        w = {"critical": 10, "high": 5, "medium": 2, "low": 1}
        return sum(w.get(x.get("severity"), 0) for x in ds)

    in_detection: set[str] = set(wordlists)
    for nid, n in nodes.items():
        typ, _, value = nid.partition(":")
        if typ in ("ip", "host"):
            st = ent.get(value) or {"events": 0, "out": 0, "in": 0, "sensors": set()}
            ds = involved.get(value, [])
            is_sensor = value in sensor_names
            internal = ip_scope(value)[0] if typ == "ip" else is_sensor
            role = "source" if st["out"] > st["in"] else "target" if st["in"] > st["out"] else "both"
            score = sev_score(ds) + 3 * max(0, len(st["sensors"]) - 1) + math.log10(1 + st["events"])
            n["props"] = {"internal": bool(internal), "role": role, "events": st["events"], "out": st["out"],
                          "in": st["in"], "sensors": sorted(st["sensors"]), "rules": sorted({x["rule"] for x in ds}),
                          "score": round(score, 2),
                          "threat": any(value in (x.get("src") or []) and SEV_RANK.get(x.get("severity"), 0)
                                        >= SEV_RANK["high"] for x in ds)}
            if is_sensor:
                n["props"]["sensor"] = True
            if ds:
                in_detection.add(nid)
        elif typ == "user":
            us = users.get(value) or {"attempts": 0, "sources": set()}
            ds = involved.get(value, [])
            n["props"] = {"attempts": us["attempts"], "sources": len(us["sources"]),
                          "rules": sorted({x["rule"] for x in ds}), "score": round(math.log10(1 + us["attempts"]), 2)}
            if ds:
                in_detection.add(nid)
        elif typ == "sig" and nid not in wordlists:
            sg = sigs.get(value) or {"count": 0, "severity": "info", "source": ""}
            n["props"] = {"source": sg["source"], "severity": sg["severity"], "count": sg["count"],
                          "score": round(math.log10(1 + sg["count"]), 2)}
            if value in det_sigs:
                in_detection.add(nid)
        elif typ == "sig":
            n["props"]["score"] = round(math.log10(1 + n["props"]["count"]), 2)
        elif typ == "camera":
            n["props"]["score"] = round(math.log10(1 + n["props"]["events"]), 2)

    edge_list = []
    for (a, b, k), ed in edges.items():
        out = {"src": a, "dst": b, "kind": k,
               "evidence": ed["evidence"] if ed.get("events") is None else _evidence(ed["events"], 20),
               "first": ed["first"], "last": ed["last"], "count": ed["count"], "severity": SEVERITIES[ed["sev"]],
               "year_assumed": ed["ya"]}
        if k == "site_map":
            out["camera_evidence"] = ed["camera_evidence"]
        edge_list.append(out)

    if focus:
        fv = str(focus).strip()
        fv = _addr(fv) or fv
        fid = next((c for c in (f"ip:{fv}", f"host:{fv}", f"user:{fv}") if c in nodes), "") or next(
            (nid for nid, n in nodes.items() if n["type"] == "host" and nid[5:].lower() == fv.lower()), "")
        keep = {fid} if fid else set()
        for ed in edge_list:
            if ed["src"] == fid:
                keep.add(ed["dst"])
            elif ed["dst"] == fid:
                keep.add(ed["src"])
        nodes = {nid: n for nid, n in nodes.items() if nid in keep}
        edge_list = [ed for ed in edge_list if ed["src"] in keep and ed["dst"] in keep]

    def events_of(n: dict) -> int:
        pr = n["props"]
        return int(pr.get("events", pr.get("attempts", pr.get("count", 0))) or 0)

    weight = {nid: (1000 if nid in in_detection else 0) + 10 * n["props"].get("score", 0) + math.log10(1 + events_of(n))
              for nid, n in nodes.items()}
    # CONTRACT-DEVIATION: a shared-wordlist node ranks right after its strongest source. By the plain formula a full
    # auth.log fills every slot with brute-force sources (score ~5.5 each) and drops the node (score log10(attempts))
    # that shows they ran one campaign; this way a campaign whose sources are kept also keeps its node.
    for (a, b, k) in edges:
        if k == "used_wordlist" and a in weight and b in weight:
            weight[b] = max(weight[b], weight[a] - 0.001)
    ranked = sorted(nodes.values(), key=lambda n: (-weight[n["id"]], n["id"]))
    kept = ranked[: max(0, int(max_nodes))]
    kept_ids = {n["id"] for n in kept}
    cand = [ed for ed in edge_list if ed["src"] in kept_ids and ed["dst"] in kept_ids]
    cand.sort(key=lambda ed: (-SEV_RANK[ed["severity"]], -ed["count"], ed["src"], ed["dst"], ed["kind"]))
    out_edges = cand[: max(0, int(max_edges))]
    return {"nodes": kept, "edges": out_edges,
            "truncated": len(kept) < len(nodes) or len(out_edges) < len(edge_list),
            "totals": {"nodes": len(nodes), "edges": len(edge_list)}}


def _involves(det: dict, entity: str) -> bool:
    vals = list(det.get("src") or []) + list(det.get("dst") or []) + list(det.get("users") or []) + list(
        (det.get("details") or {}).get("pair") or [])
    return entity in vals or entity.lower() in {str(v).lower() for v in list(det.get("src") or []) + list(det.get("dst") or [])}


def correlate_summary(detections: list[dict], g: dict, *, entity: str = "", limit: int = 10) -> dict:
    """What the agents' `correlate` tool returns: detections with evidence ids, top entities, strongest links."""
    ent = str(entity or "").strip()
    if ent:
        ent = _addr(ent) or ent
    dets = [d for d in detections or [] if not ent or _involves(d, ent)]
    rows = []
    for d in dets[: max(0, int(limit))]:
        ya = bool((d.get("details") or {}).get("year_assumed"))
        rows.append({"id": d.get("id"), "rule": d.get("rule"), "severity": d.get("severity"), "title": d.get("title"),
                     "src": list(d.get("src") or [])[:5], "dst": list(d.get("dst") or [])[:5],
                     "users": list(d.get("users") or [])[:5], "counts": d.get("counts") or {},
                     "first": fmt_ts(d.get("first_ts"), year=not ya), "last": fmt_ts(d.get("last_ts"), year=not ya),
                     "evidence": list(d.get("evidence") or [])[:12], "sources": d.get("sources") or [],
                     "sensors": d.get("sensors") or []})
    ents = [n for n in (g or {}).get("nodes") or [] if n.get("type") in ("ip", "host", "user")]
    ents.sort(key=lambda n: (-(n.get("props") or {}).get("score", 0), n.get("id")))
    entities = []
    for n in ents[:10]:
        pr = n.get("props") or {}
        entities.append({"id": n["id"], "type": n["type"], "label": n.get("label"), "internal": pr.get("internal", False),
                         "sensors": pr.get("sensors", []), "rules": pr.get("rules", []),
                         "events": pr.get("events", pr.get("attempts", 0)), "score": pr.get("score", 0),
                         "threat": pr.get("threat", False)})
    links = []
    for ed in ((g or {}).get("edges") or [])[:15]:
        ya = bool(ed.get("year_assumed"))
        links.append({"src": ed["src"], "dst": ed["dst"], "kind": ed["kind"], "count": ed["count"],
                      "first": fmt_ts(ed.get("first"), year=not ya), "last": fmt_ts(ed.get("last"), year=not ya),
                      "evidence": list(ed.get("evidence") or [])[:5]})
    return {"detections": rows, "entities": entities, "links": links,
            "note": "Rules are deterministic. Scores rank breadth and volume of evidence; they are not a verdict."}


def timeline(points: Iterable, since: float, until: float, bins: int = 120) -> dict:
    """Per-sensor event counts in equal bins over [since, until]; `alerts` counts severity high and above."""
    since, until = float(since), float(until)
    bins = max(10, min(600, int(bins or 120)))
    bin_s = (until - since) / bins
    lanes: dict[str, dict] = {}
    for pt in points:
        try:
            ts, sensor, sev, source = float(pt[0]), str(pt[1] or ""), pt[2], str(pt[3] or "")
        except (TypeError, ValueError, IndexError):
            continue
        if ts < since or ts > until:
            continue
        idx = min(bins - 1, int((ts - since) / bin_s)) if bin_s > 0 else 0
        lane = lanes.get(sensor)
        if lane is None:
            lane = lanes[sensor] = {"sensor": sensor, "source": source, "total": 0, "counts": [0] * bins,
                                    "alerts": [0] * bins, "_first": ts}
        lane["_first"] = min(lane["_first"], ts)
        lane["total"] += 1
        lane["counts"][idx] += 1
        if SEV_RANK.get(sev, 0) >= SEV_RANK["high"]:
            lane["alerts"][idx] += 1
    ordered = sorted(lanes.values(), key=lambda ln: (ln["_first"], ln["sensor"]))
    for ln in ordered:
        del ln["_first"]
    return {"since": since, "until": until, "bins": bins, "bin_s": bin_s, "lanes": ordered}


# ---------------------------------------------------------------------------- enrichment (reference data, cached)
def fetch_json(url: str, *, params: dict | None = None, headers: dict | None = None, timeout: float = 15.0) -> Any:
    """The ONLY network call in this module: https GET to stat.ripe.net, www.cisa.gov or services.nvd.nist.gov."""
    parts = urlsplit(str(url))
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = -1
    if parts.scheme != "https" or host not in ALLOWED_HOSTS or port not in (None, 443) or parts.username \
            or parts.password:
        raise ValueError(f"host not allowed for enrichment: {host or safe_text(url, 80)}")
    if os.environ.get("CYBER_OFFLINE", "").strip() == "1":
        raise RuntimeError("offline mode (CYBER_OFFLINE=1)")
    import httpx
    r = httpx.get(url, params=params, headers={"User-Agent": "Atlas/0.3 (+security desk enrichment)", **(headers or {})},
                  timeout=timeout, follow_redirects=False)
    if r.status_code >= 300:
        raise RuntimeError(f"HTTP {r.status_code} from {host}")
    return r.json()


def intel_dir() -> Path:
    env = os.environ.get("ATLAS_INTEL_DIR", "").strip()
    d = Path(env) if env else Path(config.DATA_DIR) / "intel"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, obj: Any) -> bool:
    """Atomic write (temp file + os.replace). A cache that cannot be written never fails the lookup."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, separators=(",", ":"))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
    except OSError:
        return False


_SHARED_NET = ipaddress.ip_network("100.64.0.0/10")
_RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_ULA = ipaddress.ip_network("fc00::/7")


def ip_scope(value: str) -> tuple[bool, str]:
    """(internal, scope). Internal = private, loopback, link_local or shared (carrier-grade NAT)."""
    try:
        ip = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return False, "invalid"
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_unspecified:
        scope = "unspecified"
    elif ip.is_loopback:
        scope = "loopback"
    elif ip.is_link_local:
        scope = "link_local"
    elif ip.version == 4 and ip in _SHARED_NET:
        scope = "shared"
    elif ip.is_multicast:
        scope = "multicast"
    elif (ip.version == 4 and any(ip in n for n in _RFC1918)) or (ip.version == 6 and ip in _ULA):
        scope = "private"
    elif ip.is_reserved or ip.is_private or not ip.is_global:
        scope = "reserved"
    else:
        scope = "public"
    return scope in ("private", "loopback", "link_local", "shared"), scope


_RIPE_LOCK = threading.Lock()
_RIPE_LAST = [0.0]
_RIPE_MIN_GAP_S = 0.1                     # be polite to RIPEstat: at most ten calls a second from this process


def _ripe_pace() -> None:
    with _RIPE_LOCK:
        wait = _RIPE_LAST[0] + _RIPE_MIN_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _RIPE_LAST[0] = time.monotonic()


def _ripestat(ip: str) -> tuple[dict[str, Any], bool, str, bool]:
    """(fields, cached, error, answered) for one public IP; 24 h cache, stale cache when the network fails."""
    path = intel_dir() / "ripestat" / (ip.replace(":", "_") + ".json")
    cache = _read_json(path)
    now = time.time()
    if isinstance(cache, dict) and now - float(cache.get("fetched_at") or 0) < 86400:
        return cache, True, "", True
    res: dict[str, Any] = {"prefix": "", "asn": None, "holder": "", "country": ""}
    errors, answered = [], False
    for call in ("prefix-overview", "rir-stats-country"):
        _ripe_pace()
        try:
            j = fetch_json(RIPESTAT_URL.format(call=call), params={"resource": ip, "sourceapp": "atlas-desk"})
            if not isinstance(j, dict) or j.get("status", "ok") != "ok":
                raise RuntimeError(f"status {safe_text((j or {}).get('status') if isinstance(j, dict) else 'bad', 20)}")
            data = j.get("data") or {}
            if call == "prefix-overview":
                res["prefix"] = safe_text(data.get("resource"), 64)
                asns = data.get("asns") or []
                if asns and isinstance(asns[0], dict):
                    res["asn"] = _int(asns[0].get("asn"))
                    res["holder"] = safe_text(asns[0].get("holder"), 200)
            else:
                located = data.get("located_resources") or []
                if located and isinstance(located[0], dict):
                    res["country"] = safe_text(located[0].get("location"), 8)
            answered = True
        except Exception as exc:
            errors.append(f"RIPEstat {call}: {safe_text(exc, 120)}")
    if not errors:
        res["fetched_at"] = now
        _write_json(path, res)
        return res, False, "", True
    if isinstance(cache, dict):                           # stale data beats none when the network fails
        return cache, True, "", True
    res["fetched_at"] = now
    return res, False, "; ".join(errors), answered


_DBIP_LOCK = threading.Lock()
_DBIP_TABLES: dict[str, tuple[tuple[int, int], tuple[list[int], list[int], list[list[str]]]]] = {}


def _dbip_newest(kind: str) -> Path | None:
    files = [p for p in intel_dir().glob(f"dbip-{kind}-lite-*.csv*") if p.name.endswith((".csv", ".csv.gz"))]
    return max(files, key=lambda p: p.name.replace(".gz", "")) if files else None


def _dbip_table(path: Path) -> tuple[list[int], list[int], list[list[str]]]:
    """IPv4 rows of a DB-IP Lite CSV as sorted start/end int arrays, loaded once per file version."""
    stat = path.stat()
    sig = (stat.st_mtime_ns, stat.st_size)
    with _DBIP_LOCK:
        hit = _DBIP_TABLES.get(str(path))
        if hit and hit[0] == sig:
            return hit[1]
    rows = []
    opener: Any = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 3 or ":" in row[0]:
                continue
            try:
                rows.append((int(ipaddress.IPv4Address(row[0].strip())), int(ipaddress.IPv4Address(row[1].strip())),
                             [c.strip() for c in row[2:]]))
            except ValueError:
                continue
    rows.sort(key=lambda r: r[0])
    table = ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])
    with _DBIP_LOCK:
        _DBIP_TABLES[str(path)] = (sig, table)
    return table


def _dbip_lookup(ip: str) -> dict[str, Any]:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return {}
    if addr.version != 4:
        return {}
    x = int(addr)
    out: dict[str, Any] = {}
    for kind in ("country", "asn"):
        path = _dbip_newest(kind)
        if path is None:
            continue
        starts, ends, vals = _dbip_table(path)
        i = bisect.bisect_right(starts, x) - 1
        if i >= 0 and x <= ends[i]:
            v = vals[i]
            if kind == "country" and v and v[0]:
                out["country"] = safe_text(v[0], 8)
            elif kind == "asn" and v:
                out["asn"] = _int(v[0])
                out["org"] = safe_text(v[1] if len(v) > 1 else "", 200)
    return out


def enrich_ip(ip: str) -> dict:
    """Public reference data for one address. Internal and special-purpose addresses are never looked up."""
    value = str(ip or "").strip()
    out = {"kind": "ip", "value": safe_text(value, 64), "internal": False, "scope": "", "asn": None, "holder": "",
           "prefix": "", "country": "", "country_source": "", "sources": [], "attribution": [], "note": "",
           "cached": False, "fetched_at": None, "error": ""}
    internal, scope = ip_scope(value)
    out["scope"] = scope
    if scope == "invalid":
        out["error"] = "not an IP address"
        return out
    out["value"] = ipaddress.ip_address(value).compressed
    if internal:
        out.update(internal=True, sources=["rfc1918"] if scope == "private" else ["local"],
                   note="Internal address: no external lookup.")
        return out
    if scope != "public":
        out.update(sources=["local"], note=f"Special-purpose address ({scope}): no external lookup.")
        return out
    v = out["value"]
    db = _dbip_lookup(v)
    rs, cached, err, answered = _ripestat(v)
    out.update(asn=rs.get("asn"), holder=rs.get("holder") or "", prefix=rs.get("prefix") or "",
               country=rs.get("country") or "", country_source="registry" if rs.get("country") else "",
               cached=cached, fetched_at=rs.get("fetched_at"), error=err, note=REGISTRY_NOTE,
               sources=["ripestat"] if answered else [])
    if db:
        if db.get("country"):
            out.update(country=db["country"], country_source="dbip-lite")
        if db.get("asn") is not None:
            out["asn"] = db["asn"]
        if not out["holder"] and db.get("org"):
            out["holder"] = db["org"]
        out["sources"] = ["dbip-lite"] + out["sources"]
        out["attribution"] = [DBIP_ATTRIBUTION]
    return out


_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.I)
_KEV_MEM: dict[str, Any] = {}             # {"key": (path, mtime_ns, size), "index": {cveID: entry}, "version": str}
_KEV_LOCK = threading.Lock()
_NVD_LOCK = threading.Lock()
_NVD_TIMES: collections.deque = collections.deque()


def _kev_catalog() -> tuple[dict[str, dict] | None, dict[str, Any]]:
    """(index by CVE id or None, {"version", "fetched_at", "cached", "stale", "error"}); 24 h cache in intel/kev.json."""
    path = intel_dir() / "kev.json"
    info: dict[str, Any] = {"version": "", "fetched_at": None, "cached": True, "stale": False, "error": ""}
    now = time.time()
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    with _KEV_LOCK:
        mem = dict(_KEV_MEM) if key and _KEV_MEM.get("key") == key else None
    if mem is None and key:
        cache = _read_json(path)
        if isinstance(cache, dict) and isinstance(cache.get("catalog"), dict):
            mem = {"key": key, "fetched_at": float(cache.get("fetched_at") or 0), **_kev_index(cache["catalog"])}
            with _KEV_LOCK:
                _KEV_MEM.clear()
                _KEV_MEM.update(mem)
    if mem and now - mem["fetched_at"] < 86400:
        info.update(version=mem["version"], fetched_at=mem["fetched_at"])
        return mem["index"], info
    try:
        catalog = fetch_json(KEV_URL)
        if not isinstance(catalog, dict) or not isinstance(catalog.get("vulnerabilities"), list):
            raise RuntimeError("unexpected KEV catalog format")
        _write_json(path, {"fetched_at": now, "catalog": catalog})
        idx = _kev_index(catalog)
        with _KEV_LOCK:
            _KEV_MEM.clear()
            try:
                st = path.stat()
                _KEV_MEM.update({"key": (str(path), st.st_mtime_ns, st.st_size), "fetched_at": now, **idx})
            except OSError:
                pass
        info.update(version=idx["version"], fetched_at=now, cached=False)
        return idx["index"], info
    except Exception as exc:
        if mem:                                           # stale catalog beats none
            info.update(version=mem["version"], fetched_at=mem["fetched_at"], stale=True)
            return mem["index"], info
        info.update(cached=False, error=f"CISA KEV unavailable: {safe_text(exc, 120)}")
        return None, info


def _kev_index(catalog: dict) -> dict[str, Any]:
    idx = {}
    for v in catalog.get("vulnerabilities") or []:
        if isinstance(v, dict) and v.get("cveID"):
            idx[str(v["cveID"]).strip().upper()] = v
    return {"index": idx, "version": safe_text(catalog.get("catalogVersion"), 40)}


def _nvd_slot() -> str:
    """Take a slot under NVD's published limit (5 requests per rolling 30 s, 50 with NVD_API_KEY), waiting at most
    NVD_MAX_WAIT_S. Returns '' or the rate-limit error."""
    has_key = bool(os.environ.get("NVD_API_KEY", "").strip())
    limit = 50 if has_key else 5
    try:
        max_wait = float(os.environ.get("NVD_MAX_WAIT_S", "7"))
    except ValueError:
        max_wait = 7.0
    with _NVD_LOCK:
        now = _clock()
        while _NVD_TIMES and now - _NVD_TIMES[0] >= 30:
            _NVD_TIMES.popleft()
        if len(_NVD_TIMES) >= limit:
            wait = 30 - (now - _NVD_TIMES[0])
            if wait > max_wait:
                return (f"NVD rate limit ({limit} requests per 30 s "
                        f"{'with' if has_key else 'without'} an API key)")
            _sleep(max(0.0, wait))
            now = _clock()
            while _NVD_TIMES and now - _NVD_TIMES[0] >= 30:
                _NVD_TIMES.popleft()
        _NVD_TIMES.append(now)
    return ""


def _nvd_record(cve: str) -> tuple[dict | None, dict[str, Any]]:
    """(raw NVD response or None, {"fetched_at", "cached", "error"}); 7-day cache in intel/nvd/<CVE>.json."""
    path = intel_dir() / "nvd" / f"{cve}.json"
    cache = _read_json(path)
    now = time.time()
    if isinstance(cache, dict) and isinstance(cache.get("response"), dict):
        if now - float(cache.get("fetched_at") or 0) < 7 * 86400:
            return cache["response"], {"fetched_at": float(cache["fetched_at"]), "cached": True, "error": ""}
    else:
        cache = None
    err = _nvd_slot()
    if not err:
        key = os.environ.get("NVD_API_KEY", "").strip()
        try:
            if key:
                resp = fetch_json(NVD_URL, params={"cveId": cve}, headers={"apiKey": key})
            else:
                resp = fetch_json(NVD_URL, params={"cveId": cve})
            if not isinstance(resp, dict):
                raise RuntimeError("unexpected NVD response")
            _write_json(path, {"fetched_at": now, "response": resp})
            return resp, {"fetched_at": now, "cached": False, "error": ""}
        except Exception as exc:
            err = f"NVD: {safe_text(exc, 120)}"
    if cache:
        return cache["response"], {"fetched_at": float(cache.get("fetched_at") or 0), "cached": True, "error": ""}
    return None, {"fetched_at": None, "cached": False, "error": err}


def _nvd_fields(resp: dict) -> dict[str, Any]:
    vulns = resp.get("vulnerabilities") or []
    c = (vulns[0] or {}).get("cve") if vulns and isinstance(vulns[0], dict) else None
    if not isinstance(c, dict):
        return {}
    desc = next((d.get("value") for d in c.get("descriptions") or [] if isinstance(d, dict) and d.get("lang") == "en"),
                "")
    cvss = None
    metrics = c.get("metrics") or {}
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        lst = [m for m in metrics.get(key) or [] if isinstance(m, dict)]
        if not lst:
            continue
        m = next((x for x in lst if x.get("type") == "Primary"), lst[0])
        cd = m.get("cvssData") or {}
        score = cd.get("baseScore")
        cvss = {"version": safe_text(cd.get("version"), 8),
                "score": float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
                "severity": safe_text(cd.get("baseSeverity") or m.get("baseSeverity"), 16),
                "vector": safe_text(cd.get("vectorString"), 200)}
        break
    return {"nvd_summary": safe_text(desc, 600), "cvss": cvss, "published": safe_text(c.get("published"), 40),
            "nvd_status": safe_text(c.get("vulnStatus"), 40)}


def enrich_cve(cve: str) -> dict:
    """CISA KEV status and the NVD summary/CVSS for one CVE id (both cached; NVD rate-limited)."""
    v = str(cve or "").strip().upper()
    if not _CVE_RE.match(v):
        return {"kind": "cve", "value": safe_text(cve, 64), "valid": False, "error": "not a CVE id (CVE-YYYY-NNNN)"}
    out: dict[str, Any] = {"kind": "cve", "value": v, "valid": True, "kev": None, "kev_date_added": "", "kev_name": "",
                           "kev_vendor": "", "kev_product": "", "kev_required_action": "", "kev_due_date": "",
                           "kev_ransomware": "", "kev_catalog_version": "", "kev_stale": False, "nvd_summary": "",
                           "cvss": None, "published": "", "nvd_status": "", "notice": "", "sources": [],
                           "cached": False, "fetched_at": None, "error": ""}
    errors, fetched, all_cached = [], [], True
    index, kinfo = _kev_catalog()
    if index is None:
        errors.append(kinfo["error"])
        all_cached = False
    else:
        entry = index.get(v)
        out.update(kev=entry is not None, kev_catalog_version=kinfo["version"], kev_stale=kinfo["stale"])
        if entry:
            out.update(kev_date_added=safe_text(entry.get("dateAdded"), 20),
                       kev_name=safe_text(entry.get("vulnerabilityName"), 200),
                       kev_vendor=safe_text(entry.get("vendorProject"), 100),
                       kev_product=safe_text(entry.get("product"), 100),
                       kev_required_action=safe_text(entry.get("requiredAction"), 300),
                       kev_due_date=safe_text(entry.get("dueDate"), 20),
                       kev_ransomware=safe_text(entry.get("knownRansomwareCampaignUse"), 40))
        out["sources"].append("cisa-kev")
        fetched.append(kinfo["fetched_at"])
        all_cached = all_cached and kinfo["cached"]
    resp, ninfo = _nvd_record(v)
    if resp is None:
        errors.append(ninfo["error"])
        all_cached = False
    else:
        fields = _nvd_fields(resp)
        out.update(fields or {"nvd_status": "not in NVD"})     # NVD answered: it has no record of this id
        out["notice"] = NVD_NOTICE
        out["sources"].append("nvd")
        fetched.append(ninfo["fetched_at"])
        all_cached = all_cached and ninfo["cached"]
    stamps = [t for t in fetched if t]
    out.update(cached=all_cached and bool(stamps), fetched_at=min(stamps) if stamps else time.time(),
               error="; ".join(e for e in errors if e))
    return out


def enrich(value: str, kind: str = "auto") -> dict:
    v = str(value or "").strip()
    k = str(kind or "auto").strip().lower()
    if k == "ip":
        return enrich_ip(v)
    if k == "cve":
        return enrich_cve(v)
    if _is_ip(v):
        return enrich_ip(v)
    if _CVE_RE.match(v):
        return enrich_cve(v)
    return {"kind": "unknown", "value": safe_text(v, 100), "error": "unsupported value: give an IP address or a CVE id"}


# ---------------------------------------------------------------------------- containment helpers and citations
_EVIDENCE_ID = re.compile(r"^\[?#?\s*([0-9]{1,15})\s*\]?$")


def containment_spec(args: dict) -> tuple[dict, list[str]]:
    """Normalise containment tool arguments or a stored action body (same shape, so this is idempotent).
    Returns (spec, errors); the spec is returned as far as it parsed."""
    args = args if isinstance(args, dict) else {}
    errors: list[str] = []
    top = str(args.get("action") or "").strip().lower()
    top = top if top in CONTAINMENT_ACTIONS else ""
    raw = args.get("targets")
    if isinstance(raw, (str, dict)):
        raw = [raw]
    elif not isinstance(raw, (list, tuple)):
        raw = []
    targets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for t in raw:
        if isinstance(t, str):
            value, own = t.strip()[:200], ""
            kind = "ip" if _is_ip(value) else "host"
        elif isinstance(t, dict):
            kind = str(t.get("kind") or "").strip().lower()
            value = str(t.get("value") if t.get("value") is not None else "").strip()[:200]
            own = str(t.get("action") or "").strip().lower()
        else:
            continue
        if kind not in TARGET_KINDS:
            errors.append(f"unknown target kind '{safe_text(kind, 24)}'")
            continue
        if not value:
            continue
        if kind == "ip":
            try:
                value = ipaddress.ip_address(value).compressed
            except ValueError:
                errors.append(f"'{safe_text(value, 200)}' is not an IP address")
                continue
        else:
            value = safe_text(value, 200)
        if (kind, value) in seen:
            continue
        seen.add((kind, value))
        action = safe_text(own or top or DEFAULT_ACTION[kind], 24)
        if (kind, action) not in ALLOWED_PAIRS:
            errors.append(f"cannot {action} a {kind}")
        targets.append({"kind": kind, "value": value, "action": action})
    if not targets:
        errors.append("no targets")
    elif len(targets) > 10:
        errors.append("too many targets (max 10)")
    raw_ev = args.get("evidence")
    if isinstance(raw_ev, str):
        raw_ev = [x for x in re.split(r"[\s,]+", raw_ev.strip()) if x]
    elif isinstance(raw_ev, (int, float)) and not isinstance(raw_ev, bool):
        raw_ev = [raw_ev]
    elif not isinstance(raw_ev, (list, tuple)):
        raw_ev = []
    evidence: list[int] = []
    bad = False
    for x in raw_ev:
        n = None
        if isinstance(x, bool):
            pass
        elif isinstance(x, int):
            n = x
        elif isinstance(x, float) and x.is_integer():
            n = int(x)
        elif isinstance(x, str):
            m = _EVIDENCE_ID.match(x.strip())
            n = int(m[1]) if m else None
        if n is None or n <= 0:
            bad = True
            continue
        if n not in evidence:
            evidence.append(n)
    if bad:
        errors.append("evidence ids must be numbers")
    if len(evidence) > 50:
        errors.append("too many evidence ids (max 50)")
    spec = {"targets": targets, "action": top, "evidence": evidence, "connector": safe_text(args.get("connector"), 60),
            "justification": safe_text(args.get("justification") or args.get("body") or args.get("reason") or "", 2000)}
    return spec, errors


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def containment_question(targets: list[dict]) -> str:
    """'Isolate 192.168.25.103 and block 192.168.202.140?' - grouped by action in order of first appearance."""
    groups: dict[str, list[str]] = {}
    for t in targets or []:
        kind = str(t.get("kind") or "")
        action = str(t.get("action") or DEFAULT_ACTION.get(kind, "")).strip().lower()
        value = safe_text(t.get("value"), 200)
        groups.setdefault(action, []).append(f"user {value}" if kind == "user" else value)
    if not groups:
        return ""
    parts = [f"{action.capitalize() if i == 0 else action} {_join(vals)}" for i, (action, vals) in enumerate(groups.items())]
    return _join(parts) + "?"


def citations(text: str) -> list[dict]:
    """[#12] -> {"kind": "sec", "id": 12}; [cam #12] -> {"kind": "cam", "id": 12}; de-duplicated, at most 100."""
    out, seen = [], set()
    for m in CITE_RE.finditer(str(text or "")):
        key = ("cam" if m.group(1) else "sec", int(m.group(2)))
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": key[0], "id": key[1]})
        if len(out) >= 100:
            break
    return out


_VERIFIED_MARK = "\n\n---\nVerified by the desk"


def split_summary(text: str) -> tuple[str, str]:
    """(summary, verified footer). The footer is the LAST marker, so text a model wrote cannot pose as the footer."""
    s = str(text or "")
    i = s.rfind(_VERIFIED_MARK)
    if i < 0:
        return s.strip(), ""
    return s[:i].rstrip(), s[i + len("\n\n---\n"):].strip()


# ---------------------------------------------------------------------------- CLI
def _cli(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="py -m atlas.cyber",
                                 description="Parse a security log and run the security desk's detection rules.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("parse", "detect"):
        sp = sub.add_parser(name, help=f"{name} one log file (.gz accepted)")
        sp.add_argument("file")
        sp.add_argument("--fmt", default="auto", help="auto, " + ", ".join(FORMATS))
        sp.add_argument("--sensor", default="")
        sp.add_argument("--year", type=int, default=None, help="year for logs that carry none (syslog, Snort)")
        sp.add_argument("--tz", default="UTC",
                        help="UTC, +HH:MM, -HH:MM or local (syslog and Snort only); write --tz=-05:00 for a negative offset")
        sp.add_argument("--zeek-path", default="", help="notice, ssh or conn for headerless Zeek files")
        if name == "parse":
            sp.add_argument("--limit", type=int, default=5)
        else:
            sp.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")       # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    fmt, opts = a.fmt, {}
    if fmt == "auto":
        fmt, opts = detect_format(list(itertools.islice(open_lines(a.file), 400)), a.file)
        if not fmt:
            print("could not tell the log format: pass --fmt", file=sys.stderr)
            return 2
    zeek_path = a.zeek_path or opts.get("zeek_path", "")
    st: dict[str, Any] = {}
    events = []
    for ev in iter_parse(fmt, open_lines(a.file), sensor=a.sensor, year=a.year, tz=a.tz, zeek_path=zeek_path, stats=st):
        ev["id"] = st["lines"]                            # id = line number in the file
        if a.cmd == "detect" or len(events) < max(0, a.limit):
            events.append(ev)
    st["fmt"] = fmt
    if a.cmd == "parse":
        print(json.dumps({"fmt": fmt, "options": opts, "stats": st}, ensure_ascii=True))
        for ev in events:
            print(json.dumps(ev, ensure_ascii=True))
        return 0
    dets = detect(events)
    if a.json:
        for d in dets:
            print(json.dumps(d, ensure_ascii=True))
    else:
        print(f"{fmt}: {st['events']:,} events from {st['lines']:,} lines; {len(dets)} detections")
        for d in dets:
            print(f"{d['severity']:<8} {d['rule']:<22} {d['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
