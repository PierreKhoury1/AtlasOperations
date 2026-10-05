"""Security desk core (atlas/cyber.py): parsers, detection rules, evidence sampling, merge, graph, correlate summary,
timeline, enrichment (fake fetch_json only, never the network), containment helpers and text/time helpers.

Fixtures in tests/fixtures/cyber are unmodified excerpts of public SecRepo data (Security Repo by Mike Sconzo,
CC BY 4.0; MACCDC 2012 via SecRepo). Lines for formats those files do not contain are written here and marked
"# synthetic"."""
import gzip
import json
import math
import random
from pathlib import Path

import pytest

from atlas import cyber as CY

FX = Path(__file__).parent / "fixtures" / "cyber"
NOW = CY.parse_time("2026-10-02T12:00:00Z")           # fixed "now" for year inference


def _load(name: str, fmt: str, start: int = 1, **opts) -> list[dict]:
    """Parse a fixture and give the events store-like ids (line order)."""
    evs, _ = CY.parse(fmt, CY.open_lines(FX / name), now=NOW, **opts)
    for i, e in enumerate(evs):
        e["id"] = start + i
    return evs


def _with_ids(evs: list[dict], start: int = 1) -> list[dict]:
    for i, e in enumerate(evs):
        e["id"] = start + i
    return evs


def _maccdc() -> list[dict]:
    notice = _load("zeek_notice_sample.log", "zeek", 1)
    ssh = _load("zeek_ssh_sample.log", "zeek", 1001)
    fast = _load("snort_fast_sample.log", "snort_fast", 2001, year=2012, tz="-05:00")
    return notice + ssh + fast


def _one(dets: list[dict], did: str) -> dict:
    found = [d for d in dets if d["id"] == did]
    assert len(found) == 1, [d["id"] for d in dets]
    return found[0]


# ---------------------------------------------------------------------------- text and time helpers
def test_safe_text_controls_bidi_whitespace_and_truncation():
    assert CY.safe_text("a\tb\x00c\x1b[31m\u202eevil\u2066x\u200f  y\n z", 100) == "a bc[31mevilx y z"
    assert CY.safe_text("\x85\x9fok\x7f", 10) == "ok"                       # C1 and DEL removed
    assert CY.safe_text(None, 10) == "" and CY.safe_text("", 10) == ""
    assert CY.safe_text("abcdefghij", 10) == "abcdefghij"
    assert CY.safe_text("abcdefghijk", 10) == "abcdefghi…"
    assert len(CY.safe_text("x" * 500, 300)) == 300
    assert CY.safe_text("  many   spaces\u3000here ", 50) == "many spaces here"


def test_parse_time_and_fmt_ts():
    assert CY.parse_time(None) is None and CY.parse_time("") is None and CY.parse_time("junk") is None
    assert CY.parse_time(True) is None and CY.parse_time("nan") is None
    assert CY.parse_time(1331995287.96) == 1331995287.96
    assert CY.parse_time("1331995287") == 1331995287.0
    assert CY.parse_time(1331995287960) == 1331995287.96                    # milliseconds
    assert CY.parse_time("2012-03-17T14:41:27Z") == 1331995287.0
    assert CY.parse_time("2012-03-17 14:41:27") == 1331995287.0              # no offset means UTC
    assert CY.parse_time("2012-03-17T14:41") == 1331995260.0
    assert CY.parse_time("2012-03-17T14:41:27.5+01:00") == 1331991687.5
    assert CY.parse_time("2012-03-17T09:41:27-0500") == 1331995287.0
    assert CY.parse_time("2012-03-17") == 1331942400.0                      # date alone: midnight UTC
    assert CY.parse_time("2012-02-30") is None
    assert CY.fmt_ts(1331995287.96) == "2012-03-17 14:41:27Z"
    assert CY.fmt_ts(1331995287.96, year=False) == "Mar 17 14:41:27Z"
    assert CY.fmt_ts(CY.parse_time("2025-12-06T08:23:28Z"), year=False) == "Dec 06 08:23:28Z"
    assert CY.fmt_ts(None) == "" and CY.fmt_ts("x") == "" and CY.fmt_ts(float("inf")) == ""


def test_citations_and_split_summary():
    text = "Scan [#12] then C2 [#7], camera [cam #12] and [cam#3]; again [#12]. Not [#x] or [Your name]."
    assert CY.citations(text) == [{"kind": "sec", "id": 12}, {"kind": "sec", "id": 7}, {"kind": "cam", "id": 12},
                                  {"kind": "cam", "id": 3}]
    assert len(CY.citations(" ".join(f"[#{i}]" for i in range(1, 300)))) == 100
    assert CY.citations(None) == []
    body = "Line one [#1].\nLine two.  \n\n---\nVerified by the desk (not model-claimed): 1 action queued."
    assert CY.split_summary(body) == ("Line one [#1].\nLine two.",
                                      "Verified by the desk (not model-claimed): 1 action queued.")
    assert CY.split_summary("  plain summary \n") == ("plain summary", "")
    # a model that writes its own fake footer cannot hide the real one (the LAST marker is the footer)
    spoof = "A\n\n---\nVerified by the desk: all clear\n\n---\nVerified by the desk (not model-claimed): real"
    summary, verified = CY.split_summary(spoof)
    assert verified == "Verified by the desk (not model-claimed): real" and "all clear" in summary


def test_compact_event_and_normalize_event():
    ev = {"id": 9, "ts": CY.parse_time("2025-11-30T08:42:04Z"), "sensor": "ip-172-31-27-153", "kind": "invalid_user",
          "src": "187.12.249.74", "dst": "", "user": "admin", "sig": "", "severity": "low",
          "message": "Invalid user admin from 187.12.249.74", "attrs": {"year_assumed": True}}
    c = CY.compact_event(ev, msg_len=20)
    assert c == {"id": 9, "t": "Nov 30 08:42:04Z", "sensor": "ip-172-31-27-153", "kind": "invalid_user",
                 "src": "187.12.249.74", "user": "admin", "sev": "low", "msg": "Invalid user admin …"}
    c2 = CY.compact_event({"id": 1, "ts": 1331995287.0, "kind": "notice", "sig": "Scan::Port_Scan", "attrs": "{}"})
    assert c2 == {"id": 1, "t": "2012-03-17 14:41:27Z", "kind": "notice", "sig": "Scan::Port_Scan"}
    assert CY.compact_event({"id": None, "kind": ""}) == {"id": None, "t": "", "kind": ""}

    # synthetic: structured events posted to the logs hook
    n = CY.normalize_event({"ts": "2026-10-01T22:14:05Z", "kind": "Weird", "severity": "LOUD", "source": "evil",
                            "src": "10.0.0.5", "dst": "UNKNOWN", "user": "deploy", "message": "x\u202ey",
                            "attrs": {"host": "lab-01", "nested": {"a": 1}, "n": 3}}, sensor="lab-01", now=5.0)
    assert n["ts"] == CY.parse_time("2026-10-01T22:14:05Z") and n["kind"] == "custom" and n["severity"] == "info"
    assert n["source"] == "custom" and n["sensor"] == "lab-01" and n["src"] == "10.0.0.5" and n["dst"] == ""
    assert n["message"] == "xy" and n["attrs"] == {"host": "lab-01", "n": 3} and json.loads(n["raw"])["user"] == "deploy"
    n2 = CY.normalize_event({"sig": "custom rule", "kind": "ids_alert", "severity": "high", "source": "zeek",
                             "attrs": "not a dict", "sensor": "edge"}, now=1234.5)
    assert n2["ts"] == 1234.5 and n2["kind"] == "ids_alert" and n2["source"] == "zeek" and n2["sensor"] == "edge"
    assert n2["attrs"] == {} and n2["severity"] == "high"
    assert CY.normalize_event(["not", "a", "dict"]) is None
    assert CY.normalize_event({"kind": "notice", "user": "x"}) is None        # no message, src or sig
    assert set(n) == {"ts", "source", "sensor", "kind", "src", "dst", "user", "sig", "severity", "message", "raw",
                      "attrs"}


# ---------------------------------------------------------------------------- sshd
def test_sshd_fixture_kinds_counts_and_skips():
    evs, st = CY.parse("sshd", CY.open_lines(FX / "auth_sample.log"), now=NOW)
    assert st["lines"] == 54 and st["events"] == 29 and st["skipped"] == 25 and st["errors"] == [] and st["fmt"] == "sshd"
    kinds = {}
    for e in evs:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    assert kinds == {"disconnect": 15, "invalid_user": 2, "probe": 10, "auth_failure": 2}
    assert not any("input_userauth_request" in e["raw"] or "CRON" in e["raw"] for e in evs)   # duplicates skipped
    inv = [e for e in evs if e["kind"] == "invalid_user"][0]
    assert (inv["src"], inv["user"], inv["severity"], inv["dst"]) == ("187.12.249.74", "admin", "low", "")
    assert inv["message"] == "Invalid user admin from 187.12.249.74" and inv["sensor"] == "ip-172-31-27-153"
    assert inv["attrs"] == {"host": "ip-172-31-27-153", "pid": 22182, "invalid_user": True, "year_assumed": True}
    assert inv["raw"] == "Nov 30 08:42:04 ip-172-31-27-153 sshd[22182]: Invalid user admin from 187.12.249.74"
    rd = [e for e in evs if e["message"].startswith("Disconnect from")]
    assert rd[0]["message"] == "Disconnect from 187.12.249.74 (11: Bye Bye)" and rd[0]["attrs"]["code"] == 11
    assert rd[1]["message"] == "Disconnect from 62.210.172.145 (11)" and rd[1]["severity"] == "info"
    closed = [e for e in evs if e["message"].startswith("Connection closed")]
    assert len(closed) == 13 and closed[0]["src"] == "122.225.103.87"
    noid = [e for e in evs if e["message"].startswith("No SSH identification")]
    assert len(noid) == 8 and noid[0]["kind"] == "probe" and noid[0]["severity"] == "low"
    bad = [e for e in evs if e["message"].startswith("Bad SSH protocol")]
    assert bad[1]["message"] == "Bad SSH protocol identification from 171.13.14.45"
    assert bad[1]["attrs"]["ident"] == "GET / HTTP/1.0" and "GET" not in bad[1]["message"]
    too_many = [e for e in evs if e["message"].startswith("Too many")]
    assert len(too_many) == 2 and too_many[0]["src"] == "" and too_many[0]["user"] == "root"
    assert too_many[0]["kind"] == "auth_failure" and too_many[0]["message"] == "Too many authentication failures for root"


def test_sshd_synthetic_patterns_rfc3339_and_zones():
    lines = [
        # synthetic: OpenSSH 9 shapes the SecRepo file does not contain
        "Oct  1 22:14:01 lab-01 sshd[4242]: Failed password for root from 203.0.113.9 port 50522 ssh2",
        "Oct  1 22:14:02 lab-01 sshd[4242]: Failed password for invalid user oracle from 203.0.113.9 port 50523 ssh2",
        "Oct  1 22:14:03 lab-01 sshd[4242]: Accepted publickey for deploy from 203.0.113.9 port 50524 ssh2",
        "Oct  1 22:14:04 lab-01 sshd[4242]: error: maximum authentication attempts exceeded for invalid user test from 203.0.113.9 port 50525 ssh2 [preauth]",
        "Oct  1 22:14:05 lab-01 sshd[4242]: Disconnecting invalid user test 203.0.113.9 port 50525: Too many authentication failures [preauth]",
        "Oct  1 22:14:06 lab-01 sshd-session[4243]: Connection closed by authenticating user root 203.0.113.9 port 50526 [preauth]",
        "Oct  1 22:14:07 lab-01 sshd[4244]: Disconnected from invalid user guest 2001:db8::7 port 50527 [preauth]",
        "Oct  1 22:14:08 lab-01 sshd[4245]: Received disconnect from 2001:db8::7: 11: Bye [preauth]",
        "Oct  1 22:14:09 lab-01 sshd[4246]: Invalid user  from 203.0.113.9 port 50528",
        "Oct  1 22:14:10 lab-01 sshd[4247]: Did not receive identification string from UNKNOWN port 65535",
        "Oct  1 22:14:11 lab-01 sudo[4248]: deploy : TTY=pts/0 ; COMMAND=/bin/ls",
        "not a syslog line at all",
        "",
    ]
    st = {}
    evs = list(CY.iter_parse("sshd", lines, now=NOW, stats=st))
    got = [(e["kind"], e["src"], e["user"], e["severity"]) for e in evs]
    assert got == [("auth_failure", "203.0.113.9", "root", "low"),
                   ("auth_failure", "203.0.113.9", "oracle", "low"),
                   ("auth_success", "203.0.113.9", "deploy", "info"),
                   ("auth_failure", "203.0.113.9", "test", "low"),
                   ("auth_failure", "203.0.113.9", "test", "low"),
                   ("disconnect", "203.0.113.9", "root", "info"),
                   ("disconnect", "2001:db8::7", "guest", "info"),
                   ("disconnect", "2001:db8::7", "", "info"),
                   ("invalid_user", "203.0.113.9", "", "low"),
                   ("probe", "", "", "low")]
    assert evs[0]["message"] == "Failed password for root from 203.0.113.9" and evs[0]["attrs"]["invalid_user"] is False
    assert evs[1]["message"] == "Failed password for invalid user oracle from 203.0.113.9" and evs[1]["attrs"]["port"] == 50523
    assert evs[2]["message"] == "Accepted publickey for deploy from 203.0.113.9" and evs[2]["attrs"]["method"] == "publickey"
    assert evs[3]["message"] == "Maximum authentication attempts exceeded for test from 203.0.113.9"
    assert evs[4]["message"] == "Too many authentication failures for test from 203.0.113.9"
    assert evs[7]["attrs"]["reason"] == "Bye"                                   # IPv6 without a port
    assert all(e["sensor"] == "lab-01" and e["dst"] == "" and e["attrs"]["host"] == "lab-01" for e in evs)
    assert st == {"lines": 13, "events": 10, "skipped": 3, "errors": ["line 12: not a syslog line"],
                  "first_ts": evs[0]["ts"], "last_ts": evs[-1]["ts"], "zeek_path": ""}

    # year inference: Oct 1 is before the fixed now (2026-10-02) -> 2026; Nov 30 would be in the future -> 2025
    assert CY.fmt_ts(evs[0]["ts"]) == "2026-10-01 22:14:01Z" and evs[0]["attrs"]["year_assumed"] is True
    nov = CY.parse("sshd", ["Nov 30 08:42:04 h sshd[1]: Invalid user a from 1.2.3.4"], now=NOW)[0][0]
    assert CY.fmt_ts(nov["ts"]) == "2025-11-30 08:42:04Z"
    soon = CY.parse("sshd", ["Oct  3 08:00:00 h sshd[1]: Invalid user a from 1.2.3.4"], now=NOW)[0][0]
    assert CY.fmt_ts(soon["ts"]) == "2026-10-03 08:00:00Z"                     # within 2 days: same year
    given = CY.parse("sshd", ["Mar 17 13:23:37 h sshd[1]: Invalid user a from 1.2.3.4"], year=2012, tz="-05:00")[0][0]
    assert CY.fmt_ts(given["ts"]) == "2012-03-17 18:23:37Z" and "year_assumed" not in given["attrs"]
    plus = CY.parse("sshd", ["Mar 17 13:23:37 h sshd[1]: Invalid user a from 1.2.3.4"], year=2012, tz="+0100")[0][0]
    assert CY.fmt_ts(plus["ts"]) == "2012-03-17 12:23:37Z"
    # synthetic: Ubuntu 24.04 rsyslog default (RFC 3339) carries its own year and zone
    rfc = CY.parse("sshd", ["2026-10-01T23:14:05.250000+01:00 lab-01 sshd[4242]: Accepted password for deploy "
                            "from 192.168.1.50 port 50522 ssh2"], tz="-05:00", now=NOW)[0][0]
    assert rfc["ts"] == CY.parse_time("2026-10-01T22:14:05.25Z") and "year_assumed" not in rfc["attrs"]
    assert rfc["kind"] == "auth_success" and rfc["user"] == "deploy" and rfc["sensor"] == "lab-01"
    assert CY.parse("sshd", ["Nov 30 08:42:04 h sshd[1]: Invalid user a from 1.2.3.4"], sensor="edge-01")[0][0]["sensor"] == "edge-01"


def test_iter_parse_rejects_bad_options_at_call_time():
    with pytest.raises(ValueError):
        CY.iter_parse("auto", [])
    with pytest.raises(ValueError):
        CY.iter_parse("sshd", [], tz="Europe/London")
    with pytest.raises(ValueError):
        CY.iter_parse("snort_fast", [], tz="+25:00")
    with pytest.raises(ValueError):
        CY.iter_parse("sshd", [], year="soon")
    assert list(CY.iter_parse("combined", [], tz="local")) == []


# ---------------------------------------------------------------------------- Zeek
def test_zeek_headerless_notice_and_ssh():
    notice, st = CY.parse("zeek", CY.open_lines(FX / "zeek_notice_sample.log"))
    assert st["zeek_path"] == "notice" and st["events"] == 46 and st["skipped"] == 0
    ps = [e for e in notice if e["sig"] == "Scan::Port_Scan" and e["src"] == "192.168.202.140"][0]
    assert (ps["kind"], ps["dst"], ps["severity"], ps["ts"]) == ("notice", "192.168.25.100", "medium", 1331995287.96)
    assert ps["message"] == "192.168.202.140 scanned at least 15 unique ports of host 192.168.25.100 in 0m0s"
    assert ps["attrs"] == {"zeek_path": "notice", "note": "Scan::Port_Scan", "sub": "remote"}
    addr = [e for e in notice if e["sig"] == "Scan::Address_Scan" and e["src"] == "192.168.202.140"][0]
    assert addr["dst"] == "" and addr["attrs"]["dport"] == 443 and addr["attrs"]["proto"] == "tcp"
    pg = [e for e in notice if e["sig"] == "SSH::Password_Guessing"]
    assert {e["src"] for e in pg} == {"192.168.202.110", "192.168.202.140"} and all(e["severity"] == "high" for e in pg)
    ssl = notice[0]
    assert (ssl["sig"], ssl["severity"], ssl["src"], ssl["dst"]) == ("SSL::Invalid_Server_Cert", "info",
                                                                     "192.168.202.79", "192.168.229.254")
    assert ssl["attrs"]["sport"] == 46119 and ssl["attrs"]["dport"] == 443 and ssl["attrs"]["uid"] == "CGUBcoXKxBE8gTNl"
    sqli = [e for e in notice if e["sig"] == "HTTP::SQL_Injection_Attacker"][0]
    assert sqli["severity"] == "high" and sqli["src"] == "192.168.202.110"

    ssh, st = CY.parse("zeek", CY.open_lines(FX / "zeek_ssh_sample.log"))
    assert st["zeek_path"] == "ssh" and st["events"] == 136
    ok = [e for e in ssh if e["kind"] == "auth_success"]
    assert len(ok) == 1 and ok[0]["ts"] == 1331910012.66 and ok[0]["severity"] == "medium"
    assert (ok[0]["src"], ok[0]["dst"], ok[0]["user"], ok[0]["sig"]) == ("192.168.202.110", "192.168.28.253", "", "")
    assert ok[0]["message"] == "SSH success 192.168.202.110 -> 192.168.28.253 (Zeek heuristic)"
    assert ok[0]["attrs"]["status"] == "success" and ok[0]["attrs"]["direction"] == "INBOUND"
    fail = [e for e in ssh if e["kind"] == "auth_failure"]
    assert len(fail) == 63 + 30 and fail[0]["severity"] == "low" and fail[0]["message"].endswith("(Zeek)")
    und = [e for e in ssh if e["kind"] == "ssh_session"]
    assert len(und) == 42 and und[0]["severity"] == "info"


def test_zeek_headered_synthetic_and_unknown_layout():
    # synthetic: a headered Zeek 6 ssh.log (auth_success T/F) and conn.log, as `zeek -r` writes them
    ssh_lines = ["#separator \\x09", "#set_separator\t,", "#empty_field\t(empty)", "#unset_field\t-", "#path\tssh",
                 "#open\t2026-10-01-22-00-00",
                 "#fields\tts\tuid\tid.orig_h\tid.orig_p\tid.resp_h\tid.resp_p\tversion\tauth_success\tauth_attempts"
                 "\tdirection\tclient\tserver",
                 "#types\ttime\tstring\taddr\tport\taddr\tport\tcount\tbool\tcount\tenum\tstring\tstring",
                 "1790900000.123456\tCAbc1\t10.0.0.5\t50000\t10.0.0.9\t22\t2\tT\t3\t-\tSSH-2.0-OpenSSH_9.6\t(empty)",
                 "1790900001.5\tCAbc2\t10.0.0.5\t50001\t10.0.0.9\t22\t2\tF\t6\t-\tSSH-2.0-libssh\t-",
                 "1790900002.5\tCAbc3\t10.0.0.5\t50002\t10.0.0.9\t22\t2\t-\t0\t-\t-\t-",
                 "#close\t2026-10-01-23-00-00"]
    assert CY.detect_format(ssh_lines) == ("zeek", {"zeek_path": "ssh"})
    st = {}
    evs = list(CY.iter_parse("zeek", ssh_lines, stats=st))
    assert [e["kind"] for e in evs] == ["auth_success", "auth_failure", "ssh_session"]
    assert evs[0]["ts"] == 1790900000.123456 and evs[0]["attrs"]["client"] == "SSH-2.0-OpenSSH_9.6"
    assert "server" not in evs[0]["attrs"] and evs[2]["attrs"]["status"] == "undetermined"
    assert st["zeek_path"] == "ssh" and st["events"] == 3 and st["skipped"] == 9
    conn_lines = ["#separator \\x09", "#path\tconn",
                  "#fields\tts\tuid\tid.orig_h\tid.orig_p\tid.resp_h\tid.resp_p\tproto\tservice\tduration\torig_bytes"
                  "\tresp_bytes\tconn_state",
                  "1790900003.0\tCx\t10.0.0.5\t40000\t10.0.0.9\t443\ttcp\tssl\t1.250000\t517\t4096\tSF"]
    c = CY.parse("zeek", conn_lines)[0][0]
    assert (c["kind"], c["severity"], c["message"]) == ("conn", "info", "tcp 10.0.0.5:40000 -> 10.0.0.9:443 SF")
    assert c["attrs"]["duration"] == 1.25 and c["attrs"]["orig_bytes"] == 517 and c["attrs"]["service"] == "ssl"
    # synthetic: an 18-column headerless line is not a layout we know
    odd = ["\t".join(["1331901000.01"] + ["x"] * 17)] * 3
    evs, st = CY.parse("zeek", odd)
    assert evs == [] and st["errors"] == ["unknown Zeek layout (18 columns): pass zeek_path"] and st["skipped"] == 3
    # an unsupported Zeek log is noted once
    evs, st = CY.parse("zeek", ["#path\thttp", "#fields\tts\tuid\thost", "1331901000.01\tC1\texample", "1331901000.02\tC2\tx"])
    assert evs == [] and st["errors"] == ["Zeek http log is not supported (notice, ssh, conn)"]
    # zeek_path given for a file with the wrong column count: skipped with an error, never mis-mapped
    evs, st = CY.parse("zeek", CY.open_lines(FX / "zeek_ssh_sample.log"), zeek_path="notice")
    assert evs == [] and st["errors"][0].endswith("15 Zeek columns, expected 26")


# ---------------------------------------------------------------------------- Snort / Suricata
def test_snort_fast_endpoints_priorities_and_c2():
    evs, st = CY.parse("snort_fast", CY.open_lines(FX / "snort_fast_sample.log"), year=2012, tz="-05:00")
    assert st["events"] == 69 and st["skipped"] == 0
    first = evs[0]
    assert (first["src"], first["dst"], first["severity"], first["kind"]) == ("192.168.21.253", "192.168.202.138",
                                                                              "high", "ids_alert")
    assert first["ts"] == CY.parse_time("2012-03-17T18:23:37.55Z") and "year_assumed" not in first["attrs"]
    assert first["attrs"] == {"gid": 1, "sid": 2013659, "rev": 3, "classification": "Potential Corporate Privacy Violation",
                              "priority": 1, "proto": "TCP", "sport": 443, "dport": 36510}
    assert first["sig"] == "ET POLICY Self Signed SSL Certificate (SomeOrganizationalUnit)"
    assert first["message"] == ("[1:2013659:3] ET POLICY Self Signed SSL Certificate (SomeOrganizationalUnit) · "
                                "Potential Corporate Privacy Violation · P1 · TCP 192.168.21.253:443 -> 192.168.202.138:36510")
    assert evs[1]["severity"] == "medium"                                      # priority 2
    icmp = evs[20]
    assert (icmp["src"], icmp["dst"], icmp["severity"]) == ("192.168.202.90", "192.168.26.253", "low")
    assert "sport" not in icmp["attrs"] and icmp["message"].endswith("ICMP 192.168.202.90 -> 192.168.26.253")
    v6icmp = evs[25]
    assert (v6icmp["src"], v6icmp["dst"]) == ("::", "ff02::1:ff8e:385a") and "dport" not in v6icmp["attrs"]
    udp6 = evs[36]                                                             # bracket-less IPv6 with a port
    assert (udp6["src"], udp6["attrs"]["sport"], udp6["dst"], udp6["attrs"]["dport"]) == (
        "fe80::65ca:c6cd:7ae0:ac8c", 63057, "ff02::c", 1900)
    udp4 = evs[23]
    assert (udp4["src"], udp4["attrs"]["sport"], udp4["dst"]) == ("192.168.1.254", 1900, "239.255.255.250")
    met = [e for e in evs if "Meterpreter" in e["sig"]]
    assert len(met) == 40 and all(e["severity"] == "critical" for e in met)
    # without a year the year is assumed and flagged
    e0 = CY.parse("snort_fast", CY.open_lines(FX / "snort_fast_sample.log"), now=NOW)[0][0]
    assert e0["attrs"]["year_assumed"] is True and CY.fmt_ts(e0["ts"]) == "2026-03-17 13:23:37Z"
    lines = [
        # synthetic: Suricata fast.log carries the year; bracketed IPv6 endpoint
        "10/01/2026-22:14:05.123456  [**] [1:2210054:1] SURICATA STREAM excessive retransmissions [**] "
        "[Classification: Generic Protocol Command Decode] [Priority: 3] {TCP} [2001:db8::1]:443 -> 192.168.1.10:51515",
        # synthetic: two-digit year, priority 4, and a line without a priority
        "10/01/26-22:14:06  [**] [1:9000001:1] LOCAL test info [**] [Priority: 4] {UDP} 10.0.0.1:53 -> 10.0.0.2:5353",
        "10/01/26-22:14:07  [**] [1:9000002:1] LOCAL reverse-shell banner seen [**] {TCP} 10.0.0.3:4444 -> 10.0.0.4:5555",
        "10/01/26-22:14:08  [**] [1:9000003:1] LOCAL no priority [**] {TCP} 10.0.0.3:4444 -> 10.0.0.4:5555",
        "garbage line",
    ]
    st = {}
    evs = list(CY.iter_parse("snort_fast", lines, year=1999, now=NOW, stats=st))
    assert evs[0]["ts"] == CY.parse_time("2026-10-01T22:14:05.123456Z") and "year_assumed" not in evs[0]["attrs"]
    assert (evs[0]["src"], evs[0]["attrs"]["sport"], evs[0]["severity"]) == ("2001:db8::1", 443, "low")
    assert "TCP [2001:db8::1]:443 -> 192.168.1.10:51515" in evs[0]["message"]
    assert evs[1]["severity"] == "info" and CY.fmt_ts(evs[1]["ts"]) == "2026-10-01 22:14:06Z"
    assert evs[2]["severity"] == "critical"                                    # C2_RE: reverse-shell
    assert evs[3]["severity"] == "medium" and "priority" not in evs[3]["attrs"]
    assert st["errors"] == ["line 5: not a fast.log alert"]


# ---------------------------------------------------------------------------- combined
def test_combined_fields_probes_escapes_and_malformed():
    evs, st = CY.parse("combined", CY.open_lines(FX / "web_sample.log"))
    assert st["events"] == 195 and st["skipped"] == 0
    e = evs[0]
    assert (e["src"], e["dst"], e["user"], e["kind"], e["severity"], e["sensor"]) == ("14.139.187.130", "", "",
                                                                                   "http_request", "info", "web")
    assert e["ts"] == CY.parse_time("2017-01-01T10:16:51Z") and e["message"] == "GET / -> 200"
    assert e["attrs"] == {"method": "GET", "path": "/", "status": 200, "bytes": 10267,
                          "referer": "https://www.google.co.in/",
                          "ua": "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/55.0.2883.87 Safari/537.36",
                          "proto": "HTTP/1.1"}
    probes = [x for x in evs if x["attrs"].get("probe")]
    assert len(probes) == 155 and {x["attrs"]["probe"] for x in probes} == {"wordpress"}
    assert all(x["severity"] == "low" for x in probes)
    rsd = evs[-1]
    assert rsd["attrs"]["path"] == "/xmlrpc.php" and rsd["attrs"]["query_len"] == 3 and rsd["message"] == "GET /xmlrpc.php?rsd -> 404"
    lines = [
        # synthetic: escaped quotes in the user agent, an auth user, a .env probe with a query string
        '203.0.113.7 - alice [01/Jan/2017:02:16:51 -0800] "GET /.env?x=1 HTTP/1.1" 404 - "-" "Mozilla \\"evil\\" agent"',
        # synthetic: traversal anywhere in the path; phpMyAdmin prefix, lower-cased
        '203.0.113.7 - - [01/Jan/2017:02:16:52 -0800] "GET /static/../../etc/passwd HTTP/1.1" 400 12',
        '203.0.113.7 - - [01/Jan/2017:02:16:53 -0800] "POST /PhpMyAdmin/index.php HTTP/1.1" 200 5 "-" "-"',
        # synthetic: a malformed request (TLS bytes sent to port 80) and a line that is not an access-log line
        '203.0.113.8 - - [01/Jan/2017:02:16:54 -0800] "\\x16\\x03\\x01\\x02" 400 0 "-" "-"',
        "this is not an access log line",
    ]
    st = {}
    evs = list(CY.iter_parse("combined", lines, stats=st))
    assert len(evs) == 4 and st["skipped"] == 1 and st["errors"] == ["line 5: not a combined access-log line"]
    assert evs[0]["attrs"]["ua"] == 'Mozilla "evil" agent' and evs[0]["user"] == "alice"
    assert evs[0]["attrs"]["probe"] == "env_file" and evs[0]["attrs"]["bytes"] == 0 and evs[0]["attrs"]["query_len"] == 3
    assert "referer" not in evs[0]["attrs"] and evs[0]["message"] == "GET /.env?x=1 -> 404"
    assert evs[1]["attrs"]["probe"] == "traversal" and evs[2]["attrs"]["probe"] == "phpmyadmin"
    assert "method" not in evs[3]["attrs"] and evs[3]["attrs"]["path"] == "\\x16\\x03\\x01\\x02"
    assert evs[3]["message"] == "\\x16\\x03\\x01\\x02 -> 400"
    assert CY.probe_family("/wp-login.php") == "wordpress" and CY.probe_family("/index.html") == ""


# ---------------------------------------------------------------------------- formats and files
def test_detect_format_content_and_filename_hints():
    def head(name):
        return list(CY.open_lines(FX / name))[:50]

    assert CY.detect_format(head("auth_sample.log")) == ("sshd", {})
    assert CY.detect_format(head("auth_campaign.log.gz")) == ("sshd", {})
    assert CY.detect_format(head("zeek_notice_sample.log")) == ("zeek", {"zeek_path": "notice"})
    assert CY.detect_format(head("zeek_ssh_sample.log")) == ("zeek", {"zeek_path": "ssh"})
    assert CY.detect_format(head("snort_fast_sample.log")) == ("snort_fast", {})
    assert CY.detect_format(head("web_sample.log")) == ("combined", {})
    # only CRON lines: content is inconclusive, the file name decides
    cron = head("auth_sample.log")[:3]
    assert CY.detect_format(cron) == ("", {})
    assert CY.detect_format(cron, "/var/log/auth.log") == ("sshd", {})
    assert CY.detect_format([], "secure") == ("sshd", {})
    assert CY.detect_format([], "notice.log.gz") == ("zeek", {"zeek_path": "notice"})
    assert CY.detect_format([], "ssh.log") == ("zeek", {"zeek_path": "ssh"})
    assert CY.detect_format([], "conn.log") == ("zeek", {"zeek_path": "conn"})
    assert CY.detect_format([], "alert.fast.maccdc2012_00016.pcap") == ("snort_fast", {})
    assert CY.detect_format([], "access.log.2017-01-01") == ("combined", {})
    assert CY.detect_format(["hello world"], "notes.txt") == ("", {})


def test_open_lines_gzip_text_and_archives(tmp_path):
    p = tmp_path / "mixed.log"
    p.write_bytes(b"\xef\xbb\xbfline one\r\nline \xff two\nlast")
    assert list(CY.open_lines(p)) == ["line one", "line \ufffd two", "last"]
    g = tmp_path / "noext"
    g.write_bytes(gzip.compress(b"a\nb\n"))                                   # gzip found by its magic bytes
    assert list(CY.open_lines(g)) == ["a", "b"]
    for name, data in (("x.7z", b"7z\xbc\xaf\x27\x1c..."), ("x.zip", b"PK\x03\x04..."), ("renamed.log", b"PK\x03\x04..")):
        q = tmp_path / name
        q.write_bytes(data)
        with pytest.raises(ValueError, match="unpack the archive first"):
            CY.open_lines(q)
    assert sum(1 for _ in CY.open_lines(FX / "auth_campaign.log.gz")) == 1666


# ---------------------------------------------------------------------------- detection rules
def test_wordlist_campaign_from_the_fixture():
    evs = _load("auth_campaign.log.gz", "sshd")
    dets = CY.detect(evs)
    wl = [d for d in dets if d["rule"] == "wordlist_fingerprint"]
    assert len(wl) == 1
    d = wl[0]
    assert d["id"] == "wordlist_fingerprint:e3503990" and d["severity"] == "medium"
    assert d["details"]["fp"] == "e3503990" and d["details"]["fingerprint"].startswith("e3503990")
    assert d["counts"] == {"sources": 4, "attempts": 1636, "length": 409} and d["evidence_total"] == 1636
    assert d["src"] == ["220.99.93.50", "218.25.17.234", "61.197.203.243", "188.87.35.25"]   # by first attempt
    assert d["dst"] == ["ip-172-31-27-153"] and d["users"] == ["zhangyan", "dff", "oracle", "test", "git"]
    assert d["title"] == "Same 409-username list from 4 sources: one campaign (fingerprint e3503990)"
    assert d["details"]["year_assumed"] is True and d["sources"] == ["sshd"] and d["sensors"] == ["ip-172-31-27-153"]
    assert [p["attempts"] for p in d["details"]["per_source"]] == [409] * 4
    # evidence: per source its first 3 and last 3 attempts, sorted, every source represented
    by_id = {e["id"]: e for e in evs}
    assert len(d["evidence"]) == 24 and d["evidence"] == sorted(d["evidence"], key=lambda i: (by_id[i]["ts"], i))
    assert {by_id[i]["src"] for i in d["evidence"]} == set(d["src"])
    for d2 in (x for x in dets if x["rule"] == "ssh_bruteforce"):
        assert d2["counts"]["failures"] == 409 and d2["counts"]["usernames"] == 47 and d2["severity"] == "medium"
    assert len([x for x in dets if x["rule"] == "ssh_bruteforce"]) == 4      # the 30 other sources stay quiet
    # evidence cap reached: every source still shows up
    small = [x for x in CY.detect(evs, {"evidence_cap": 4}) if x["rule"] == "wordlist_fingerprint"][0]
    assert len(small["evidence"]) == 4 and {by_id[i]["src"] for i in small["evidence"]} == set(d["src"])


def _sshd_lines(src: str, n: int, start: str = "Oct  1 22:00:00", step_s: int = 1, user: str = "root") -> list[str]:
    """synthetic: n 'Failed password' lines from one source, step_s seconds apart."""
    base = CY.parse_time("2026-" + {"Oct": "10"}[start[:3]] + f"-{int(start[4:6]):02d}T{start[7:]}Z")
    out = []
    for i in range(n):
        t = base + i * step_s
        hh, mm, ss = int(t // 3600 % 24), int(t // 60 % 60), int(t % 60)
        out.append(f"Oct  1 {hh:02d}:{mm:02d}:{ss:02d} lab-01 sshd[{100 + i}]: Failed password for {user} "
                   f"from {src} port {40000 + i} ssh2")
    return out


def test_ssh_bruteforce_thresholds():
    at = _with_ids(CY.parse("sshd", _sshd_lines("203.0.113.10", 10), year=2026)[0])
    d = _one(CY.detect(at), "ssh_bruteforce:203.0.113.10")
    assert d["counts"] == {"failures": 10, "max_in_window": 10, "usernames": 1, "targets": 1, "zeek_notices": 0}
    assert d["dst"] == ["lab-01"] and d["users"] == ["root"]
    assert d["title"] == "SSH brute force from 203.0.113.10: 10 failed logins, 1 username, 1 target(s)"
    below = _with_ids(CY.parse("sshd", _sshd_lines("203.0.113.10", 9), year=2026)[0])
    assert [x for x in CY.detect(below) if x["rule"] == "ssh_bruteforce"] == []
    spread = _with_ids(CY.parse("sshd", _sshd_lines("203.0.113.10", 10, step_s=120), year=2026)[0])
    assert [x for x in CY.detect(spread) if x["rule"] == "ssh_bruteforce"] == []   # 10 in 18 min, not in 10 min
    assert _one(CY.detect(spread, {"bf_window_s": 1200}), "ssh_bruteforce:203.0.113.10")["counts"]["max_in_window"] == 10
    # Zeek: notices alone fire, and the title never claims usernames Zeek does not record
    notice = _load("zeek_notice_sample.log", "zeek")
    d = _one(CY.detect(notice), "ssh_bruteforce:192.168.202.140")
    assert d["title"] == "SSH password guessing by 192.168.202.140 (Zeek notice ×2)" and d["counts"]["failures"] == 0
    both = _one(CY.detect(_maccdc()), "ssh_bruteforce:192.168.202.140")
    assert both["title"] == ("SSH brute force from 192.168.202.140: 30 failed logins, 15 target(s) "
                             "+ Zeek password-guessing notice")
    assert both["counts"]["zeek_notices"] == 2 and both["sources"] == ["zeek"]


def test_success_after_failures_sshd_and_zeek_heuristic():
    lines = _sshd_lines("203.0.113.9", 5) + [
        "Oct  1 22:00:30 lab-01 sshd[999]: Accepted password for root from 203.0.113.9 port 41000 ssh2",   # synthetic
        "Oct  1 22:00:31 lab-01 sshd[998]: Accepted password for root from 198.51.100.4 port 41001 ssh2"]  # synthetic
    evs = _with_ids(CY.parse("sshd", lines, year=2026)[0])
    dets = CY.detect(evs)
    d = _one(dets, "success_after_failures:203.0.113.9:lab-01")
    assert d["severity"] == "high" and d["details"] == {"heuristic": False, "target": "lab-01", "year_assumed": False}
    assert d["title"] == "Login succeeded from 203.0.113.9 to lab-01 after 5 failed attempts"
    assert d["counts"] == {"failures": 5, "successes": 1} and d["users"] == ["root"] and d["evidence"] == [1, 2, 3, 4, 5, 6]
    assert dets[0]["id"] == d["id"]                                            # high sorts first
    assert not any(x["rule"] == "success_after_failures" for x in CY.detect(evs[1:]))   # 4 failures: not enough
    # Zeek: 202.110's "success" to 192.168.28.253 after 63 failures to the 192.168.22.x hosts in the hour before
    ssh = _load("zeek_ssh_sample.log", "zeek")
    z = _one(CY.detect(ssh), "success_after_failures:192.168.202.110:192.168.28.253")
    assert z["severity"] == "medium" and z["details"]["heuristic"] is True and "likely" in z["title"]
    assert z["title"] == ("Login likely succeeded from 192.168.202.110 to 192.168.28.253 after 63 failed attempts "
                          "(Zeek heuristic)")
    by_id = {e["id"]: e for e in ssh}
    success_id = [e["id"] for e in ssh if e["kind"] == "auth_success"][0]
    assert success_id in z["evidence"] and len(z["evidence"]) == 50 and z["evidence_total"] == 64
    assert all(by_id[i]["dst"].startswith("192.168.22.") for i in z["evidence"] if i != success_id)


def test_scan_c2_and_web_probe_rules():
    dets = CY.detect(_load("zeek_notice_sample.log", "zeek"))
    s = _one(dets, "scan:192.168.202.140")
    assert s["severity"] == "low" and s["counts"] == {"zeek_port_scans": 2, "zeek_address_scans": 3,
                                                      "max_ports_one_host": 0, "max_hosts_one_port": 0}
    assert s["dst"] == ["192.168.25.100", "192.168.21.103"] and s["evidence_total"] == 5
    assert s["title"] == "192.168.202.140 scanning: 2 Zeek port scan(s), 3 Zeek address scan(s)"
    # synthetic: one source touching 20 ports of one host inside 5 minutes (conn events)
    conn = [{"id": i + 1, "ts": 1790900000.0 + i, "source": "zeek", "sensor": "zeek", "kind": "conn",
             "src": "10.9.9.9", "dst": "10.0.0.1", "severity": "info", "attrs": {"dport": 1000 + i}} for i in range(20)]
    d = _one(CY.detect(conn), "scan:10.9.9.9")
    assert d["counts"]["max_ports_one_host"] == 20 and d["title"] == "10.9.9.9 scanning: 20 ports on one host, 1 host on one port"
    assert [x for x in CY.detect(conn[:14]) if x["rule"] == "scan"] == []    # 14 ports < scan_ports

    fast = _load("snort_fast_sample.log", "snort_fast", year=2012, tz="-05:00")
    dets = CY.detect(fast)
    c2 = _one(dets, "ids_high:c2:192.168.202.140:192.168.25.103")
    assert c2["severity"] == "critical" and c2["src"] == ["192.168.202.140", "192.168.25.103"] and c2["dst"] == []
    assert c2["counts"] == {"alerts": 30, "signatures": 1, "a_to_b": 15, "b_to_a": 15}
    assert c2["details"]["family"] == "Metasploit Meterpreter" and c2["details"]["c2"] is True
    assert c2["details"]["pair"] == ["192.168.202.140", "192.168.25.103"]
    assert c2["title"] == "C2 traffic (Metasploit Meterpreter) between 192.168.202.140 and 192.168.25.103: 30 alerts"
    assert CY.fmt_ts(c2["first_ts"]) == "2012-03-17 18:25:25Z" and c2["sensors"] == ["snort"]
    assert _one(dets, "ids_high:c2:192.168.202.140:192.168.21.103")["counts"]["a_to_b"] == 5
    assert [d["severity"] for d in dets[:2]] == ["critical", "critical"]
    # synthetic: priority-1 alerts that are not C2, from one source
    p1 = [{"id": i + 1, "ts": 100.0 + i, "source": "snort_fast", "sensor": "snort", "kind": "ids_alert", "src": "10.1.1.1",
           "dst": f"10.2.2.{i % 2}", "sig": "WEB-ATTACKS /usr/bin/id command attempt" if i % 3 else "ET EXPLOIT x",
           "severity": "high", "attrs": {"priority": 1}} for i in range(6)]
    d = _one(CY.detect(p1), "ids_high:p1:10.1.1.1")
    assert d["severity"] == "medium" and d["counts"] == {"alerts": 6, "signatures": 2, "targets": 2}
    assert d["details"]["top_signatures"] == [["WEB-ATTACKS /usr/bin/id command attempt", 4], ["ET EXPLOIT x", 2]]
    assert d["title"] == "6 priority-1 IDS alerts from 10.1.1.1 (2 signatures, 2 targets)"
    assert not CY.detect(p1[:4])

    web = _load("web_sample.log", "combined")
    dets = CY.detect(web)
    assert [d["id"] for d in dets] == ["web_probe:wordpress"]
    w = dets[0]
    assert w["severity"] == "low" and w["counts"] == {"requests": 155, "sources": 47, "served": 0}
    assert w["details"] == {"family": "wordpress", "label": "WordPress login/admin", "example_path": "/wp-login.php",
                            "year_assumed": False}
    assert w["title"] == "155 requests for WordPress login/admin paths from 47 sources (never served)"
    assert w["dst"] == ["web"] and len(w["src"]) == 20
    for e in web:                                                              # a served probe raises it to medium
        if e["attrs"].get("probe") and e["attrs"]["path"] == "/wp-admin/":
            e["attrs"]["status"] = 200
    w2 = CY.detect(web)[0]
    assert w2["severity"] == "medium" and w2["title"].endswith("(1 answered 2xx)")


def test_detect_is_deterministic_sorted_and_evidence_capped():
    evs = _maccdc()
    a = CY.detect(evs)
    shuffled = list(evs)
    random.Random(7).shuffle(shuffled)
    assert CY.detect(shuffled) == a
    keys = [(-CY.SEV_RANK[d["severity"]], CY.RULES.index(d["rule"]), -d["evidence_total"], -d["last_ts"], d["id"]) for d in a]
    assert keys == sorted(keys)
    by_id = {e["id"]: e for e in evs}
    for d in a:
        assert len(d["evidence"]) <= 50 and set(d) == {"id", "rule", "severity", "title", "src", "dst", "users", "sources",
                                                      "sensors", "evidence", "evidence_total", "counts", "details",
                                                      "first_ts", "last_ts"}
        assert d["evidence"] == sorted(d["evidence"], key=lambda i: (by_id[i]["ts"], i))
        assert d["first_ts"] <= d["last_ts"] and "year_assumed" in d["details"] and len(d["title"]) <= 200
    # ids missing -> index + 1
    no_ids = [{k: v for k, v in e.items() if k != "id"} for e in _load("snort_fast_sample.log", "snort_fast", year=2012)]
    assert _one(CY.detect(no_ids), "ids_high:c2:192.168.202.140:192.168.25.103")["evidence"][0] == 29


def test_evidence_sampler():
    evs = [{"id": i, "ts": float(i // 2), "src": "a" if i < 80 else "b"} for i in range(1, 101)]
    ids = CY._evidence(evs, 10, group_key=lambda e: e["src"])
    assert len(ids) == 10 and ids == sorted(ids)
    assert {1, 79, 80, 100} <= set(ids)                                        # group firsts and lasts
    assert CY._evidence(evs, 200) == list(range(1, 101))
    assert CY._evidence(evs, 0) == [] and CY._evidence([], 5) == []
    groups = [{"id": i, "ts": float(i), "src": str(i)} for i in range(1, 21)]
    assert CY._evidence(groups, 5, group_key=lambda e: e["src"]) == [1, 2, 3, 4, 5]   # group picks over the cap


def test_merge_detection_widens_and_keeps_storage_fields():
    old = {"id": "scan:10.9.9.9", "rule": "scan", "severity": "medium", "title": "old", "src": ["10.9.9.9"],
           "dst": ["10.0.0.1", "10.0.0.2"], "users": [], "sources": ["zeek"], "sensors": ["zeek"],
           "evidence": [1, 2, 3], "evidence_total": 3, "counts": {"zeek_port_scans": 4, "zeek_address_scans": 0,
                                                                 "max_ports_one_host": 0, "max_hosts_one_port": 0},
           "details": {"year_assumed": True}, "first_ts": 100.0, "last_ts": 200.0,
           "row_id": 7, "run_id": "R1", "created": 1.0, "updated": 2.0}
    new = {"id": "scan:10.9.9.9", "rule": "scan", "severity": "low", "title": "new", "src": ["10.9.9.9"],
           "dst": ["10.0.0.3", "10.0.0.1"], "users": [], "sources": ["snort_fast"], "sensors": ["snort"],
           "evidence": [3, 4], "evidence_total": 2, "counts": {"zeek_port_scans": 1, "zeek_address_scans": 2,
                                                              "max_ports_one_host": 16, "max_hosts_one_port": 0},
           "details": {"year_assumed": False}, "first_ts": 150.0, "last_ts": 300.0}
    before = json.dumps(old, sort_keys=True)
    m = CY.merge_detection(old, new)
    assert json.dumps(old, sort_keys=True) == before                           # inputs untouched
    assert (m["first_ts"], m["last_ts"], m["severity"]) == (100.0, 300.0, "medium")
    assert m["counts"] == {"zeek_port_scans": 4, "zeek_address_scans": 2, "max_ports_one_host": 16, "max_hosts_one_port": 0}
    assert m["evidence"] == [1, 2, 3, 4] and m["evidence_total"] == 3
    assert m["dst"] == ["10.0.0.3", "10.0.0.1", "10.0.0.2"] and m["sources"] == ["snort_fast", "zeek"]
    assert m["sensors"] == ["snort", "zeek"] and m["details"]["year_assumed"] is True
    assert m["title"] == "10.9.9.9 scanning: 4 Zeek port scan(s), 2 Zeek address scan(s), 16 ports on one host"
    assert "run_id" not in m and "row_id" not in m and "created" not in m      # storage fields are not touched
    assert CY.merge_detection(None, new) is new
    big_old = dict(old, evidence=list(range(1, 61)))
    big = CY.merge_detection(big_old, dict(new, evidence=list(range(100, 140))))
    assert big["evidence"] == list(range(1, 31)) + list(range(110, 140))      # lowest cap//2 + highest
    # rows read back from storage may carry JSON strings
    stored = dict(old, counts=json.dumps(old["counts"]), details=json.dumps(old["details"]), evidence=json.dumps([9]))
    assert CY.merge_detection(stored, new)["evidence"] == [3, 4, 9]


# ---------------------------------------------------------------------------- graph and correlation
def test_graph_shapes_wordlist_caps_and_focus():
    evs = _load("auth_campaign.log.gz", "sshd")
    dets = CY.detect(evs)
    g = CY.graph(evs, dets, max_nodes=200, max_edges=1000)
    assert set(g) == {"nodes", "edges", "truncated", "totals"} and g["truncated"] is False
    ids = {n["id"] for n in g["nodes"]}
    assert g["totals"] == {"nodes": len(g["nodes"]), "edges": len(g["edges"])}
    wl = [n for n in g["nodes"] if n["id"] == "sig:wordlist:e3503990"][0]
    assert wl["type"] == "sig" and wl["label"] == "wordlist e3503990 · 409 usernames"
    assert wl["props"]["rule"] == "wordlist_fingerprint" and wl["props"]["count"] == 1636
    used = [e for e in g["edges"] if e["kind"] == "used_wordlist"]
    assert sorted(e["src"] for e in used) == sorted(f"ip:{s}" for s in dets[0]["src"]) and all(e["count"] == 409 for e in used)
    by_id = {e["id"]: e for e in evs}
    for e in used:
        assert e["evidence"] and all(f"ip:{by_id[i]['src']}" == e["src"] for i in e["evidence"])
    host = [n for n in g["nodes"] if n["id"] == "host:ip-172-31-27-153"][0]
    assert host["props"]["sensor"] is True and host["props"]["internal"] is True and host["props"]["role"] == "target"
    src = [n for n in g["nodes"] if n["id"] == "ip:61.197.203.243"][0]["props"]
    assert src["role"] == "source" and src["rules"] == ["ssh_bruteforce", "wordlist_fingerprint"] and src["out"] == 409
    assert src["internal"] is False and src["threat"] is False and src["sensors"] == ["ip-172-31-27-153"]
    assert src["score"] == round(2 + 2 + 0 + math.log10(410), 2)            # 2 medium detections + volume
    for e in g["edges"]:
        assert e["src"] in ids and e["dst"] in ids and len(e["evidence"]) <= 20 and e["severity"] in CY.SEVERITIES
        assert set(e) >= {"src", "dst", "kind", "evidence", "first", "last", "count", "severity"}
    kinds = {e["kind"] for e in g["edges"]}
    assert kinds == {"failed_login", "tried_user", "used_wordlist"}
    users = [n for n in g["nodes"] if n["type"] == "user"]
    assert users and all(n["id"].startswith("user:") and "attempts" in n["props"] for n in users)
    # caps
    small = CY.graph(evs, dets, max_nodes=6, max_edges=4)
    assert len(small["nodes"]) == 6 and len(small["edges"]) <= 4 and small["truncated"] is True
    assert small["totals"] == g["totals"]
    kept = {n["id"] for n in small["nodes"]}
    assert "sig:wordlist:e3503990" in kept and "ip:61.197.203.243" in kept      # detection entities first
    assert all(e["src"] in kept and e["dst"] in kept for e in small["edges"])
    # focus: the node, its neighbours, their edges
    f = CY.graph(evs, dets, focus="61.197.203.243", max_nodes=500, max_edges=500)
    fids = {n["id"] for n in f["nodes"]}
    assert "ip:61.197.203.243" in fids and "host:ip-172-31-27-153" in fids and "sig:wordlist:e3503990" in fids
    assert "ip:220.99.93.50" not in fids
    assert all("ip:61.197.203.243" in (e["src"], e["dst"]) or e["src"] in fids and e["dst"] in fids for e in f["edges"])
    assert CY.graph(evs, dets, focus="203.0.113.250")["nodes"] == []
    assert CY.graph([], [])["nodes"] == [] and CY.graph([], [])["truncated"] is False


def test_graph_cap_keeps_the_campaign_node_among_many_bruteforce_sources():
    """The full SecRepo auth.log has ~200 brute-force sources, each a detection entity; the capped graph must still
    keep the shared-wordlist node that ties the 4-source campaign together (it ranks right after its strongest source)."""
    evs = _load("auth_campaign.log.gz", "sshd")
    # synthetic: 60 more sources, 30 'Invalid user' lines each, every source with its own usernames (no shared list)
    lines = [f"Dec 20 03:{k:02d}:{i:02d} ip-172-31-27-153 sshd[{9000 + i}]: Invalid user s{k}u{i} from 198.51.100.{k + 1}"
             for k in range(60) for i in range(30)]
    evs += _with_ids(CY.parse("sshd", lines, now=NOW)[0], start=len(evs) + 1)
    dets = CY.detect(evs)
    assert [d["id"] for d in dets if d["rule"] == "wordlist_fingerprint"] == ["wordlist_fingerprint:e3503990"]
    assert sum(d["rule"] == "ssh_bruteforce" and d["src"][0].startswith("198.51.100.") for d in dets) == 60
    campaign = [f"ip:{s}" for s in ("220.99.93.50", "218.25.17.234", "61.197.203.243", "188.87.35.25")]
    g = CY.graph(evs, dets, max_nodes=40)
    kept = [n["id"] for n in g["nodes"]]
    assert len(kept) == 40 and set(campaign) <= set(kept) and "sig:wordlist:e3503990" in kept
    assert kept.index("sig:wordlist:e3503990") == max(kept.index(c) for c in campaign) + 1
    assert sorted(e["src"] for e in g["edges"] if e["kind"] == "used_wordlist") == sorted(campaign)
    assert sum(n["id"].startswith("ip:198.51.100.") for n in g["nodes"]) == 40 - 1 - 4 - 1     # host, campaign, node


def test_graph_site_map_links_only_mapped_hosts():
    # synthetic: lab drill on lab-01 (failures then a success) and a camera called office mapped to it
    lines = _sshd_lines("192.168.1.66", 6) + [
        "Oct  1 22:00:40 lab-01 sshd[999]: Accepted password for deploy from 192.168.1.66 port 41000 ssh2"]
    evs = _with_ids(CY.parse("sshd", lines, year=2026)[0])
    other = _with_ids(CY.parse("sshd", [l.replace("lab-01", "lab-02") for l in _sshd_lines("192.168.1.77", 3)],
                               year=2026)[0], start=100)
    t0 = evs[0]["ts"]
    cams = [{"id": 77, "camera": "office", "ts": t0 + 60, "triggered": 1, "reason": "person at the desk"},
            {"id": 78, "camera": "office", "ts": t0 + 4000, "triggered": 0},    # far away in time
            {"id": 79, "camera": "door", "ts": t0 + 10, "triggered": 1}]      # never mapped
    dets = CY.detect(evs + other)
    g = CY.graph(evs + other, dets, site_map={"LAB-01": "office"}, camera_events=cams, max_nodes=80)
    links = [e for e in g["edges"] if e["kind"] == "site_map"]
    assert len(links) == 1 and links[0]["src"] == "host:lab-01" and links[0]["dst"] == "camera:office"
    assert links[0]["camera_evidence"] == [77] and links[0]["evidence"] == [e["id"] for e in evs]
    cam = [n for n in g["nodes"] if n["type"] == "camera"]
    assert cam == [{"id": "camera:office", "type": "camera", "label": "office",
                    "props": {"events": 1, "triggered": 1, "score": 0.3}}]
    login = [e for e in g["edges"] if e["kind"] == "login"][0]
    assert login["src"] == "ip:192.168.1.66" and login["dst"] == "host:lab-01" and login["severity"] == "info"
    # the door camera and lab-02 sit inside the same minutes, but nobody mapped them: no link (checked above: 1 link)
    # no site map, a host with no node, a camera with no events, or no camera events near the host: never a link
    assert not [e for e in CY.graph(evs, dets, camera_events=cams)["edges"] if e["kind"] == "site_map"]
    g2 = CY.graph(evs + other, dets, site_map={"lab-03": "office", "lab-01": "garage"}, camera_events=cams)
    assert not [e for e in g2["edges"] if e["kind"] == "site_map"] and not [n for n in g2["nodes"] if n["type"] == "camera"]
    g3 = CY.graph(evs, dets, site_map={"lab-01": "office"}, camera_events=[cams[1]])
    assert not [e for e in g3["edges"] if e["kind"] == "site_map"]
    g4 = CY.graph(evs + other, dets, site_map={"lab-02": "door"}, camera_events=cams)   # mapped: linked
    assert [(e["src"], e["dst"], e["camera_evidence"]) for e in g4["edges"] if e["kind"] == "site_map"] == [
        ("host:lab-02", "camera:door", [79])]


def test_correlate_summary_ranks_the_cross_sensor_host_first():
    evs = _maccdc()
    dets = CY.detect(evs)
    g = CY.graph(evs, dets, max_nodes=40)
    out = CY.correlate_summary(dets, g)
    assert set(out) == {"detections", "entities", "links", "note"}
    assert out["note"] == "Rules are deterministic. Scores rank breadth and volume of evidence; they are not a verdict."
    top = out["entities"][0]
    assert top["label"] == "192.168.202.140" and top["sensors"] == ["snort", "zeek"] and top["threat"] is True
    assert top["rules"] == ["ids_high", "scan", "ssh_bruteforce"] and top["internal"] is True
    assert [e["score"] for e in out["entities"]] == sorted((e["score"] for e in out["entities"]), reverse=True)
    assert len(out["detections"]) == 10 and len(out["links"]) <= 15
    row = out["detections"][0]
    assert set(row) == {"id", "rule", "severity", "title", "src", "dst", "users", "counts", "first", "last", "evidence",
                        "sources", "sensors"}
    assert row["first"] == "2012-03-17 18:25:25Z" and len(row["evidence"]) <= 12
    focus = CY.correlate_summary(dets, g, entity="192.168.202.110", limit=25)
    assert focus["detections"] and all("192.168.202.110" in d["src"] + d["dst"] for d in focus["detections"])
    assert {d["rule"] for d in focus["detections"]} == {"success_after_failures", "ssh_bruteforce", "scan"}
    # year-less sources keep their times year-less
    camp = _load("auth_campaign.log.gz", "sshd")
    cd = CY.detect(camp)
    cs = CY.correlate_summary(cd, CY.graph(camp, cd), limit=1)
    assert cs["detections"][0]["first"] == "Dec 02 05:19:56Z" and cs["links"][0]["first"].startswith(("Nov", "Dec"))


def test_timeline_bins_and_lanes():
    pts = [(100.0, "snort", "high", "snort_fast"), (100.5, "snort", "low", "snort_fast"), (50.0, "zeek", "medium", "zeek"),
           (199.9, "zeek", "critical", "zeek"), (200.0, "zeek", "info", "zeek"), (10.0, "zeek", "info", "zeek"),
           (250.0, "snort", "info", "snort_fast")]
    t = CY.timeline(pts, 0.0, 200.0, bins=4)
    assert t["bins"] == 10 and t["bin_s"] == 20.0                               # bins clamp to 10..600
    assert [ln["sensor"] for ln in t["lanes"]] == ["zeek", "snort"]           # ordered by first point
    zeek, snort = t["lanes"]
    assert zeek["total"] == 4 and zeek["counts"][0] == 1 and zeek["counts"][2] == 1 and zeek["counts"][9] == 2
    assert zeek["alerts"][9] == 1 and sum(zeek["alerts"]) == 1 and zeek["source"] == "zeek"
    assert snort["total"] == 2 and snort["counts"][5] == 2 and snort["alerts"][5] == 1
    assert CY.timeline([], 0, 100, bins=1000)["bins"] == 600


# ---------------------------------------------------------------------------- enrichment (fake network only)
KEV = {  # synthetic API response in the shape of the CISA KEV feed
    "title": "CISA Catalog of Known Exploited Vulnerabilities", "catalogVersion": "2026.10.01", "count": 1,
    "vulnerabilities": [{"cveID": "CVE-2021-44228", "vendorProject": "Apache", "product": "Log4j2",
                         "vulnerabilityName": "Apache Log4j2 Remote Code Execution Vulnerability",
                         "dateAdded": "2021-12-10", "requiredAction": "For all affected software assets ... apply updates.",
                         "dueDate": "2021-12-24", "knownRansomwareCampaignUse": "Known", "notes": ""}]}


def _nvd(cve: str) -> dict:
    """synthetic API response in the shape of NVD CVE API 2.0"""
    return {"resultsPerPage": 1, "totalResults": 1, "vulnerabilities": [{"cve": {
        "id": cve, "published": "2021-12-10T10:15:09.143", "vulnStatus": "Analyzed",
        "descriptions": [{"lang": "es", "value": "no"}, {"lang": "en", "value": f"Test description for {cve}."}],
        "metrics": {"cvssMetricV31": [{"type": "Secondary", "cvssData": {"version": "3.1", "baseScore": 9.0,
                                                                           "baseSeverity": "CRITICAL", "vectorString": "x"}},
                                      {"type": "Primary", "cvssData": {"version": "3.1", "baseScore": 10.0,
                                                                         "baseSeverity": "CRITICAL",
                                                                         "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H"}}],
                    "cvssMetricV2": [{"type": "Primary", "baseSeverity": "HIGH",
                                      "cvssData": {"version": "2.0", "baseScore": 9.3, "vectorString": "AV:N"}}]}}}]}


class FakeNet:
    """Stands in for cyber.fetch_json: records calls, answers like the real services (synthetic data)."""
    def __init__(self):
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=15.0):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if url == CY.KEV_URL:
            return KEV
        if url == CY.NVD_URL:
            if params["cveId"] == "CVE-1999-99999":
                return {"resultsPerPage": 0, "totalResults": 0, "vulnerabilities": []}   # synthetic: unknown id
            return _nvd(params["cveId"])
        if url == CY.RIPESTAT_URL.format(call="prefix-overview"):
            return {"status": "ok", "data": {"resource": "1.1.1.0/24", "asns": [{"asn": 13335, "holder": "CLOUDFLARENET"}]}}
        if url == CY.RIPESTAT_URL.format(call="rir-stats-country"):
            return {"status": "ok", "data": {"located_resources": [{"resource": "1.1.1.0/24", "location": "AU"}]}}
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture()
def fake_net(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_INTEL_DIR", str(tmp_path / "intel"))
    monkeypatch.delenv("CYBER_OFFLINE", raising=False)
    monkeypatch.delenv("NVD_API_KEY", raising=False)
    monkeypatch.delenv("NVD_MAX_WAIT_S", raising=False)
    monkeypatch.setattr(CY, "_RIPE_MIN_GAP_S", 0.0)
    CY._NVD_TIMES.clear()
    net = FakeNet()
    monkeypatch.setattr(CY, "fetch_json", net)
    yield net
    CY._NVD_TIMES.clear()


def test_ip_scope_values():
    assert CY.ip_scope("192.168.202.140") == (True, "private")
    assert CY.ip_scope("10.0.0.5") == (True, "private") and CY.ip_scope("fd00::1") == (True, "private")
    assert CY.ip_scope("127.0.0.1") == (True, "loopback") and CY.ip_scope("::1") == (True, "loopback")
    assert CY.ip_scope("169.254.1.1") == (True, "link_local") and CY.ip_scope("fe80::1") == (True, "link_local")
    assert CY.ip_scope("100.64.1.1") == (True, "shared")
    assert CY.ip_scope("239.255.255.250") == (False, "multicast") and CY.ip_scope("ff02::c") == (False, "multicast")
    assert CY.ip_scope("0.0.0.0") == (False, "unspecified") and CY.ip_scope("::") == (False, "unspecified")
    assert CY.ip_scope("240.0.0.1") == (False, "reserved") and CY.ip_scope("192.0.2.1") == (False, "reserved")
    assert CY.ip_scope("61.197.203.243") == (False, "public") and CY.ip_scope("2606:4700::1111") == (False, "public")
    assert CY.ip_scope("::ffff:192.168.1.1") == (True, "private")
    assert CY.ip_scope("not-an-ip") == (False, "invalid") and CY.ip_scope("") == (False, "invalid")


def test_enrich_ip_internal_public_cache_and_errors(fake_net, tmp_path):
    r = CY.enrich("192.168.202.140")
    assert fake_net.calls == [] and r["internal"] is True and r["scope"] == "private" and r["sources"] == ["rfc1918"]
    assert r["note"] == "Internal address: no external lookup." and r["asn"] is None and r["fetched_at"] is None
    assert CY.enrich_ip("127.0.0.1")["sources"] == ["local"] and fake_net.calls == []
    assert CY.enrich_ip("239.255.255.250")["note"].startswith("Special-purpose address (multicast)") and fake_net.calls == []
    bad = CY.enrich_ip("999.1.1.1")
    assert bad["scope"] == "invalid" and bad["error"] == "not an IP address" and set(bad) == set(r)

    pub = CY.enrich_ip("1.1.1.1")
    assert pub == {"kind": "ip", "value": "1.1.1.1", "internal": False, "scope": "public", "asn": 13335,
                   "holder": "CLOUDFLARENET", "prefix": "1.1.1.0/24", "country": "AU", "country_source": "registry",
                   "sources": ["ripestat"], "attribution": [], "note": CY.REGISTRY_NOTE, "cached": False,
                   "fetched_at": pub["fetched_at"], "error": ""}
    assert [c[0] for c in fake_net.calls] == [CY.RIPESTAT_URL.format(call="prefix-overview"),
                                              CY.RIPESTAT_URL.format(call="rir-stats-country")]
    assert fake_net.calls[0][1] == {"resource": "1.1.1.1", "sourceapp": "atlas-desk"}
    assert (tmp_path / "intel" / "ripestat" / "1.1.1.1.json").exists()
    again = CY.enrich_ip("1.1.1.1")
    assert again["cached"] is True and again["holder"] == "CLOUDFLARENET" and len(fake_net.calls) == 2   # cache hit
    v6 = CY.enrich_ip("2606:4700:4700::1111")
    assert v6["value"] == "2606:4700:4700::1111" and (tmp_path / "intel" / "ripestat" / "2606_4700_4700__1111.json").exists()

    def down(url, **kw):
        raise RuntimeError("HTTP 503 from stat.ripe.net")
    CY.fetch_json = down                                                       # restored by monkeypatch
    fresh = CY.enrich_ip("8.8.4.4")
    assert fresh["holder"] == "" and fresh["sources"] == [] and "HTTP 503" in fresh["error"]
    stale = tmp_path / "intel" / "ripestat" / "1.1.1.1.json"
    data = json.loads(stale.read_text())
    data["fetched_at"] -= 3 * 86400
    stale.write_text(json.dumps(data))
    old = CY.enrich_ip("1.1.1.1")                                              # network down: stale cache is used
    assert old["holder"] == "CLOUDFLARENET" and old["cached"] is True and old["error"] == ""


def test_enrich_ip_with_dbip_lite_csv(fake_net, tmp_path):
    intel = tmp_path / "intel"
    intel.mkdir(parents=True)
    # synthetic: a tiny DB-IP Lite style CSV (rows start,end,country / start,end,asn,org), IPv6 rows ignored
    (intel / "dbip-country-lite-2026-09.csv").write_text("1.0.0.0,1.255.255.255,XX\n", encoding="utf-8")
    (intel / "dbip-country-lite-2026-10.csv").write_text(
        "1.0.0.0,1.0.0.255,AU\n1.1.1.0,1.1.1.255,AU\n8.8.8.0,8.8.8.255,US\n2001:db8::,2001:db8::ffff,ZZ\n", encoding="utf-8")
    with gzip.open(intel / "dbip-asn-lite-2026-10.csv.gz", "wt", encoding="utf-8") as f:
        f.write('8.8.8.0,8.8.8.255,15169,"Google, LLC"\n')
    r = CY.enrich_ip("8.8.8.8")
    assert r["country"] == "US" and r["country_source"] == "dbip-lite" and r["asn"] == 15169
    assert r["sources"] == ["dbip-lite", "ripestat"] and r["attribution"] == [CY.DBIP_ATTRIBUTION]
    assert r["holder"] == "CLOUDFLARENET" and r["prefix"] == "1.1.1.0/24"      # RIPEstat still fills holder/prefix
    r2 = CY.enrich_ip("9.9.9.9")                                               # not in the CSV: registry country
    assert r2["country"] == "AU" and r2["country_source"] == "registry" and r2["sources"] == ["ripestat"]
    assert r2["attribution"] == []


def test_enrich_cve_kev_nvd_notice_and_cache(fake_net, tmp_path):
    assert CY.enrich_cve("CVE-21-1") == {"kind": "cve", "value": "CVE-21-1", "valid": False,
                                         "error": "not a CVE id (CVE-YYYY-NNNN)"}
    r = CY.enrich("cve-2021-44228")
    assert r["value"] == "CVE-2021-44228" and r["valid"] is True and r["kev"] is True
    assert r["kev_date_added"] == "2021-12-10" and r["kev_vendor"] == "Apache" and r["kev_product"] == "Log4j2"
    assert r["kev_ransomware"] == "Known" and r["kev_catalog_version"] == "2026.10.01" and r["kev_stale"] is False
    assert r["nvd_summary"] == "Test description for CVE-2021-44228."
    assert r["cvss"] == {"version": "3.1", "score": 10.0, "severity": "CRITICAL",
                         "vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H"}
    assert r["published"] == "2021-12-10T10:15:09.143" and r["nvd_status"] == "Analyzed"
    assert r["notice"] == CY.NVD_NOTICE and r["sources"] == ["cisa-kev", "nvd"] and r["error"] == ""
    assert r["cached"] is False and fake_net.calls[1] == (CY.NVD_URL, {"cveId": "CVE-2021-44228"}, {})
    n = len(fake_net.calls)
    again = CY.enrich_cve("CVE-2021-44228")
    assert again["cached"] is True and len(fake_net.calls) == n and again["kev_date_added"] == "2021-12-10"
    other = CY.enrich_cve("CVE-2020-0001")
    assert other["kev"] is False and other["kev_date_added"] == "" and other["notice"] == CY.NVD_NOTICE
    unknown = CY.enrich_cve("CVE-1999-99999")
    assert unknown["nvd_status"] == "not in NVD" and unknown["nvd_summary"] == "" and unknown["cvss"] is None
    assert sum(1 for c in fake_net.calls if c[0] == CY.KEV_URL) == 1           # KEV catalog cached for 24 h
    assert json.loads((tmp_path / "intel" / "kev.json").read_text())["catalog"]["catalogVersion"] == "2026.10.01"
    assert (tmp_path / "intel" / "nvd" / "CVE-2021-44228.json").exists()
    assert CY.enrich("hello") == {"kind": "unknown", "value": "hello",
                                  "error": "unsupported value: give an IP address or a CVE id"}

    def down(url, **kw):
        raise RuntimeError("offline")
    CY.fetch_json = down
    kev = tmp_path / "intel" / "kev.json"
    data = json.loads(kev.read_text())
    data["fetched_at"] -= 2 * 86400
    kev.write_text(json.dumps(data))
    stale = CY.enrich_cve("CVE-2021-44228")
    assert stale["kev"] is True and stale["kev_stale"] is True and stale["nvd_summary"]
    missing = CY.enrich_cve("CVE-2019-0708")
    assert missing["nvd_summary"] == "" and missing["notice"] == "" and missing["sources"] == ["cisa-kev"]
    assert "NVD" in missing["error"]
    kev.unlink()
    nothing = CY.enrich_cve("CVE-2019-0708")
    assert nothing["kev"] is None and "CISA KEV unavailable" in nothing["error"]


def test_nvd_rate_limit_with_fake_clock(fake_net, monkeypatch):
    clock = [1000.0]
    slept = []

    def sleep(s):
        slept.append(s)
        clock[0] += s
    monkeypatch.setattr(CY, "_clock", lambda: clock[0])
    monkeypatch.setattr(CY, "_sleep", sleep)
    for i in range(5):
        assert CY.enrich_cve(f"CVE-2026-000{i}")["error"] == ""
    clock[0] = 1001.0
    sixth = CY.enrich_cve("CVE-2026-0005")
    assert sixth["error"] == "NVD rate limit (5 requests per 30 s without an API key)" and sixth["nvd_summary"] == ""
    assert sixth["kev"] is False and slept == []                               # KEV still answered from its cache
    assert sum(1 for c in fake_net.calls if c[0] == CY.NVD_URL) == 5
    clock[0] = 1025.0                                                          # the oldest slot frees in 5 s <= 7 s
    seventh = CY.enrich_cve("CVE-2026-0006")
    assert seventh["error"] == "" and slept == [5.0] and seventh["nvd_summary"]
    monkeypatch.setenv("NVD_API_KEY", "test-key")                              # with a key: 50 per 30 s, sent as apiKey
    assert CY.enrich_cve("CVE-2026-0007")["error"] == ""
    assert fake_net.calls[-1][2] == {"apiKey": "test-key"}


def test_fetch_json_allows_only_reference_hosts(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise AssertionError("no network in tests")
    monkeypatch.setattr(httpx, "get", boom)
    for url in ("https://ip-api.com/json/1.1.1.1", "http://stat.ripe.net/data/x/data.json",
                "https://stat.ripe.net.evil.example/x", "https://user@www.cisa.gov/x", "https://www.cisa.gov:8443/x",
                "ftp://services.nvd.nist.gov/x"):
        with pytest.raises(ValueError, match="host not allowed for enrichment"):
            CY.fetch_json(url)
    with pytest.raises(ValueError, match="host not allowed for enrichment: ip-api.com"):
        CY.fetch_json("https://ip-api.com/json/1.1.1.1")
    monkeypatch.setenv("CYBER_OFFLINE", "1")
    with pytest.raises(RuntimeError, match=r"offline mode \(CYBER_OFFLINE=1\)"):
        CY.fetch_json(CY.KEV_URL)

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True}
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=None):
        seen.update(url=url, params=params, headers=headers, timeout=timeout, follow_redirects=follow_redirects)
        return Resp()
    monkeypatch.delenv("CYBER_OFFLINE")
    monkeypatch.setattr(httpx, "get", fake_get)
    assert CY.fetch_json(CY.NVD_URL, params={"cveId": "CVE-2021-44228"}, headers={"apiKey": "k"}) == {"ok": True}
    assert seen["follow_redirects"] is False and seen["headers"]["apiKey"] == "k"
    assert seen["headers"]["User-Agent"] == "Atlas/0.3 (+security desk enrichment)" and seen["timeout"] == 15.0
    Resp.status_code = 302
    with pytest.raises(RuntimeError, match="HTTP 302 from services.nvd.nist.gov"):
        CY.fetch_json(CY.NVD_URL)


def test_cyber_module_has_one_network_call_and_no_ip_api():
    src = (Path(CY.__file__)).read_text(encoding="utf-8")
    assert "ip-api" not in src
    assert src.count("httpx.get(") == 1 and src.count("import httpx") == 1
    for banned in ("from . import store", "from .store", "orchestrator", "from . import policy", "from .desk"):
        assert banned not in src


# ---------------------------------------------------------------------------- containment helpers
def test_containment_question_vectors():
    q = CY.containment_question
    assert q([{"kind": "ip", "value": "192.168.25.103", "action": "isolate"},
              {"kind": "ip", "value": "192.168.202.140", "action": "block"}]) == "Isolate 192.168.25.103 and block 192.168.202.140?"
    assert q([{"kind": "ip", "value": v, "action": "block"} for v in
              ("61.197.203.243", "220.99.93.50", "218.25.17.234", "188.87.35.25")]) == \
        "Block 61.197.203.243, 220.99.93.50, 218.25.17.234 and 188.87.35.25?"
    assert q([{"kind": "user", "value": "deploy", "action": "disable"}, {"kind": "host", "value": "lab-01", "action": "isolate"},
              {"kind": "ip", "value": "10.0.0.5", "action": "block"}]) == "Disable user deploy, isolate lab-01 and block 10.0.0.5?"
    assert q([{"kind": "ip", "value": "10.0.0.5", "action": "block"}]) == "Block 10.0.0.5?"
    assert q([]) == ""


def test_containment_spec_normalises_and_reports_errors():
    spec, errs = CY.containment_spec({
        "targets": [{"kind": "IP", "value": " 192.168.25.103 ", "action": "isolate"},
                    "192.168.202.140", "lab-01", {"kind": "ip", "value": "192.168.25.103"},
                    {"kind": "user", "value": "deploy", "action": "Disable"}],
        "action": "block", "evidence": [9001, "9002", "#9003", "[#9004]", 9001], "connector": "  lab-firewall ",
        "body": "Meterpreter C2 between the two hosts [#9001]."})
    assert errs == []
    assert spec == {"targets": [{"kind": "ip", "value": "192.168.25.103", "action": "isolate"},
                                {"kind": "ip", "value": "192.168.202.140", "action": "block"},
                                {"kind": "host", "value": "lab-01", "action": "block"},
                                {"kind": "user", "value": "deploy", "action": "disable"}],
                    "action": "block", "evidence": [9001, 9002, 9003, 9004], "connector": "lab-firewall",
                    "justification": "Meterpreter C2 between the two hosts [#9001]."}
    assert CY.containment_spec(json.loads(json.dumps(spec))) == (spec, [])  # a stored body re-parses to itself
    spec, errs = CY.containment_spec({"targets": [{"kind": "ip", "value": "2001:0db8::0001"}], "action": "nuke"})
    assert spec["targets"] == [{"kind": "ip", "value": "2001:db8::1", "action": "block"}] and spec["action"] == ""
    assert errs == [] and spec["evidence"] == []                               # empty evidence is policy's call
    _, errs = CY.containment_spec({})
    assert errs == ["no targets"]
    _, errs = CY.containment_spec({"targets": [{"kind": "mac", "value": "aa:bb"}, {"kind": "ip", "value": "lab-01"}]})
    assert errs == ["unknown target kind 'mac'", "'lab-01' is not an IP address", "no targets"]
    spec, errs = CY.containment_spec({"targets": [{"kind": "user", "value": "root", "action": "block"},
                                                  {"kind": "host", "value": "h1", "action": "disable"}]})
    assert errs == ["cannot block a user", "cannot disable a host"] and len(spec["targets"]) == 2
    _, errs = CY.containment_spec({"targets": [{"kind": "user", "value": "root"}], "action": "block"})
    assert errs == ["cannot block a user"]                                     # a valid top-level action still applies
    spec, errs = CY.containment_spec({"targets": [{"kind": "user", "value": "root"}]})
    assert errs == [] and spec["targets"] == [{"kind": "user", "value": "root", "action": "disable"}]
    _, errs = CY.containment_spec({"targets": [f"10.0.0.{i}" for i in range(11)], "evidence": [1]})
    assert errs == ["too many targets (max 10)"]
    spec, errs = CY.containment_spec({"targets": ["10.0.0.1"], "evidence": list(range(1, 52))})
    assert errs == ["too many evidence ids (max 50)"] and len(spec["evidence"]) == 51
    spec, errs = CY.containment_spec({"targets": ["10.0.0.1"], "evidence": [1, "x", True, "[cam #3]", 2.0]})
    assert errs == ["evidence ids must be numbers"] and spec["evidence"] == [1, 2]
    spec, _ = CY.containment_spec({"targets": ["10.0.0.1"], "evidence": "12, #13 [#14]", "reason": "why"})
    assert spec["evidence"] == [12, 13, 14] and spec["justification"] == "why"
    assert CY.POLICY_LINE == "Every target is in the evidence."


# ---------------------------------------------------------------------------- CLI
def test_cli_detect_and_parse(capsys):
    assert CY._cli(["detect", str(FX / "auth_campaign.log.gz")]) == 0
    out = capsys.readouterr().out
    assert "sshd: 1,666 events from 1,666 lines; 5 detections" in out
    assert "Same 409-username list from 4 sources: one campaign (fingerprint e3503990)" in out
    assert CY._cli(["parse", str(FX / "zeek_ssh_sample.log"), "--limit", "1"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    head = json.loads(lines[0])
    assert head["fmt"] == "zeek" and head["options"] == {"zeek_path": "ssh"} and head["stats"]["events"] == 136
    assert len(lines) == 2 and json.loads(lines[1])["id"] == 1
    assert CY._cli(["detect", str(FX / "snort_fast_sample.log"), "--year", "2012", "--tz=-05:00", "--json"]) == 0
    rows = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    assert rows[0]["id"] == "ids_high:c2:192.168.202.140:192.168.25.103" and rows[0]["evidence"][0] == 29
