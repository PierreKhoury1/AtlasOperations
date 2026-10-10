"""`python -m atlas usage`: the operator's tools for usage limits (usage spec §7.2, §3.6).

    python -m atlas usage show [--email a@b.c | --owner ID] [--json]
    python -m atlas usage set (--email a@b.c | --owner ID) --limit ai_requests_day --value 500 [--note "pilot until 31 Oct"]
    python -m atlas usage clear (--email a@b.c | --owner ID) --limit ai_requests_day|all
    python -m atlas usage prune
    python -m atlas usage --env-file /etc/atlas/atlas.env ...      read the service's env file first, as systemd does

`show` with an account prints its limits (each with any override and the basis its Usage page gives), where today's
requests went, the last 7 UTC days and its overrides. Without one it prints the operator's overview: the shared pool,
the website's share, Atlas's paid cap, the disk, every account with use today and every override on record. `set`
writes an operator override for one account: a number, or `none` for no Atlas limit (still counted). There is no web
UI for it, so an owner cannot raise their own share. `clear` removes overrides. `prune` runs the retention once; the
server runs it every hour.

The command opens the portal's own store (DATABASE_URL, else desk.db in ATLAS_DATA_DIR) and reads what the server
stored there: the provider key snapshot, the ledger, the overrides. It never calls a provider. What only the running
server holds in memory (the runs going now, the per-minute window) is marked as such, never printed as a number.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NONE_WORDS = ("none", "unlimited")          # --value: no Atlas limit for this account (still counted)
# limits that count only an owner's own scope: while accounts are on, owner 0's desks (the demo desk, the showroom)
# count in the website's share instead (spec §7.3), so an override of these for owner 0 would change nothing
_OWNER_SCOPE_ONLY = ("ai_requests_day", "ai_requests_auto_day", "paid_usd_month", "runs_at_once")
_WINDOW_WORDS = {"D": "a UTC day", "M": "a UTC month", "T": "in total", "run": "per run or link study",
                 "now": "at once"}


def load_env_file(path: str) -> list[str]:
    """KEY=VALUE lines the way systemd's EnvironmentFile reads them: blank lines and lines starting with # or ; are
    skipped, one pair of matching quotes around a value is removed, nothing is expanded. Each is set in os.environ,
    over whatever the shell has, so the command sees what the service sees. Returns the names set (never the
    values: the file holds keys)."""
    names: list[str] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if not sep or not _KEY_RE.fullmatch(key):
                continue
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            os.environ[key] = val
            names.append(key)
    return names


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m atlas usage",
        description="Usage limits, for the operator: an account's limits and use, operator overrides, the ledger's "
                    "retention. Reads the portal's store (DATABASE_URL, else desk.db in ATLAS_DATA_DIR); never calls "
                    "a provider.")
    p.add_argument("--env-file", default=None, metavar="PATH",
                   help="read KEY=VALUE lines first, as systemd does for the service (e.g. /etc/atlas/atlas.env)")
    again = argparse.ArgumentParser(add_help=False)          # --env-file after the command works too
    again.add_argument("--env-file", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{show,set,clear,prune}")

    def who(sp: argparse.ArgumentParser, required: bool) -> None:
        g = sp.add_mutually_exclusive_group(required=required)
        g.add_argument("--email", help="the account's sign-in email")
        g.add_argument("--owner", type=int, metavar="ID",
                       help="the account's id; 0 = open test mode (DESK_OPEN=1), else the website and demo desks")

    s = sub.add_parser("show", parents=[again], help="an account's limits, use and overrides; without one, the "
                                                      "operator's overview")
    who(s, False)
    s.add_argument("--json", action="store_true", help="print JSON instead of text")
    st = sub.add_parser("set", parents=[again], help="set an operator override for one account")
    who(st, True)
    st.add_argument("--limit", required=True, metavar="ID", help="one of: " + ", ".join(_owner_limit_ids()))
    st.add_argument("--value", required=True,
                    help="the limit in its own unit (requests, runs, segments, messages, USD, MB), or 'none' for no "
                         "Atlas limit (still counted)")
    st.add_argument("--note", default="", help="why, up to 200 characters. The owner sees it on their Usage page")
    c = sub.add_parser("clear", parents=[again], help="remove an override (the default applies again)")
    who(c, True)
    c.add_argument("--limit", required=True, metavar="ID", help="the limit id, or 'all' for every override of the "
                                                                "account")
    sub.add_parser("prune", parents=[again], help="run the ledger's retention once (the server runs it hourly)")
    return p


def _owner_limit_ids() -> tuple[str, ...]:
    # the help text is built before atlas.usage may be imported (the env file comes first): the ids are stable
    return ("ai_requests_day", "ai_requests_auto_day", "paid_usd_month", "run_requests", "runs_at_once",
            "sms_segments_day", "auto_sends_day", "storage_mb")


def _fail(msg: str) -> int:
    print(f"atlas usage: {msg}", file=sys.stderr)
    return 2


def store_label(store) -> str:
    """Which store the command opened: the SQLite file, or the Postgres host and database (never the user or
    password)."""
    if getattr(store, "backend", "") == "postgres":
        from urllib.parse import urlsplit
        try:
            u = urlsplit(str(getattr(store._conn, "_url", "") or ""))
            port = f":{u.port}" if u.port else ""
            return f"postgres {u.hostname or 'localhost'}{port}/{u.path.lstrip('/') or '?'}"
        except ValueError:
            return "postgres"
    try:
        rows = store._conn.execute("PRAGMA database_list").fetchall()
        return "sqlite " + (next((str(r[2]) for r in rows if r[1] == "main"), "") or "(in memory)")
    except Exception:
        return "sqlite"


def _utc(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _num(v: Any, unit: str) -> str:
    if v is None:
        return "-"
    f = float(v)
    if unit == "usd":
        return f"${f:,.2f}"
    if unit == "MB":
        return f"{f:,.1f} MB"
    return f"{int(f):,}" if f.is_integer() else f"{f:,.1f}"


class _Cli:
    """One command's context: the usage module bound to the store, the time, and how to name the account."""

    def __init__(self, U, store, env_note: str):
        self.U, self.store, self.env_note = U, store, env_note
        self.now = U._now()

    # ------------------------------------------------------------------ naming
    def amount(self, lid: str, v: Any) -> str:
        """A limit's value in words: '500 requests a UTC day', '$2.50 a UTC month', 'no Atlas limit (still counted)'."""
        if v is None:
            return "no Atlas limit (still counted)"
        meta = self.U.LIMITS[lid]
        unit, words = meta["unit"], _WINDOW_WORDS.get(meta["window"], "")
        n = _num(v, unit)
        if unit not in ("usd", "MB"):
            n += f" {unit}"
        return f"{n} {words}".strip()

    def who(self, owner_id: int, email: str) -> str:
        if owner_id:
            return f"#{owner_id} {email}".strip()
        return "#0 (open test mode)" if self.U.OPEN_MODE() else "#0 (the website and the demo desks)"

    def resolve(self, email: str | None, owner: int | None) -> tuple[int, str] | str:
        """The account by email or id, in this store; an error message when there is none."""
        if email is not None:
            u = self.store.user_by_email(email)
            if not u:
                hint = " Open test mode has no accounts: use --owner 0." if self.U.OPEN_MODE() else ""
                return f"no account with the email {email.strip().lower()!r} in {store_label(self.store)}.{hint}"
            return int(u["id"]), str(u.get("email") or "")
        if owner is None or owner < 0:
            return "--owner takes an account id (0 or more)"
        if owner == 0:
            return 0, ""
        u = self.store.user(owner)
        if not u:
            return f"no account #{owner} in {store_label(self.store)}"
        return owner, str(u.get("email") or "")

    def header(self, title: str) -> list[str]:
        U = self.U
        mode = "limits enforced" if U.CFG.enforce else "meter only (USAGE_ENFORCE=0): nothing is refused but by the provider"
        mode += " · accounts " + ("off (DESK_OPEN=1)" if U.OPEN_MODE() else "on")
        if os.environ.get("DESK_MODE", "").strip().lower() == "demo":
            mode += " · DESK_MODE=demo: scripted agents " + ("limited (USAGE_DEMO=enforce)" if U.CFG.demo == "enforce"
                                                             else "counted, not limited")
        lines = [title, f"  Store   {store_label(self.store)}"]
        if self.env_note:
            lines.append(f"  Env     {self.env_note}")
        lines += [f"  Mode    {mode}", f"  As of   {_utc(self.now)}",
                  f"  Pool    {U.pool_size():,} free-model requests a UTC day ({U._pool_source(self.now)})"]
        return lines

    def counting_since(self) -> float | None:
        """The ledger's oldest row (retention keeps 35 days), or None when nothing was counted in this store."""
        r = self.store._conn.execute("SELECT MIN(ts) FROM usage_events").fetchone()
        return float(r[0]) if r and r[0] else None

    def since_line(self) -> str:
        ts = self.counting_since()
        return f"Counting since {_utc(ts)}" if ts else "Nothing counted yet in this store"


# ---------------------------------------------------------------------------- show (one account)
def _pool(U, now: float) -> dict[str, Any]:
    """The shared pool with its counts, as the operator view has it, without the last poll's error: only the running
    server knows that one."""
    return {k: v for k, v in U.pool_view(now, operator=True).items() if k != "error"}


def _desks(store, owner_id: int) -> list[dict[str, Any]]:
    """The account's desks the way the Usage page names them, and whether each can send SMS (a Twilio connector)."""
    out = []
    for d in store.desks_for(owner_id):
        name = ((d.get("config") or {}).get("business") or {}).get("name") or d["name"]
        out.append({"id": int(d["id"]), "name": str(name),
                    "sms": any(c["kind"] == "twilio" for c in store.connectors(d["id"]))})
    return out


def owner_report(cli: _Cli, owner_id: int, email: str) -> dict[str, Any]:
    """An account's usage as the operator sees it, from the store only."""
    U, now, o = cli.U, cli.now, owner_id
    desks = _desks(cli.store, o)
    limits = [e for e in U.limits_view(o, desks=desks, now=now, operator=True)
              if e["id"] not in ("disk_floor", "pool_free_day")]          # the server's, in the overview
    for e in limits:
        if e["id"] == "runs_at_once":                 # the runs going now are in the running server's memory only
            e.update(used=None, remaining=None, state="off" if e["limit"] is None else "unknown",
                     note="runs going now: only the running server knows (its Usage page shows them)")
    return {"store": store_label(cli.store), "as_of": int(now), "enforced": U.CFG.enforce,
            "owner": {"id": o, "email": email, "unlimited": U.is_unlimited(o), "open_mode": U.OPEN_MODE()},
            "pool": _pool(U, now), "limits": limits, "overrides": U.overrides(o),
            "today": U.today_view(o, now=now), "history": U.history(o, now), "sends": U.sends_view(o, now),
            "deferred_waiting": len(U.deferred(o)), "desks": [{"id": d["id"], "name": d["name"]} for d in desks],
            "counting_since": cli.counting_since()}


def _override_text(cli: _Cli, r: dict[str, Any]) -> str:
    val = cli.amount(r["limit_id"], r["value"]) if r["limit_id"] in cli.U.LIMITS else str(r["value"])
    note = f"   note: {r['note']}" if r["note"] else ""
    return f"{val}   set {_utc(r['updated'])} by {r['set_by'] or '?'}{note}"


def _cost_text(row: dict[str, Any]) -> str:
    parts = []
    if row.get("cost_usd") is not None:
        parts.append(f"${row['cost_usd']:,.4f}")
    if row.get("est_usd") is not None:
        parts.append(f"${row['est_usd']:,.4f} estimated")
    if row.get("cost_unreported"):
        parts.append(f"{row['cost_unreported']} not reported")
    return " + ".join(parts) or "-"


def _print_owner(cli: _Cli, rep: dict[str, Any]) -> None:
    U, now = cli.U, cli.now
    o, email = rep["owner"]["id"], rep["owner"]["email"]
    out = cli.header(f"Atlas usage · account {cli.who(o, email)}")
    pool = rep["pool"]
    out.append(f"          today: {pool['state']}, resets {U._hm(pool['resets_at'], now)}")
    if rep["owner"]["unlimited"]:
        out.append("  Note    in USAGE_UNLIMITED_EMAILS: no Atlas limit on AI requests, paid models and runs at once "
                   "(still counted; the shared pool and Atlas's paid cap still apply)")
    if o == 0 and not rep["owner"]["open_mode"]:
        out.append("  Note    accounts are on: owner 0's desks (the demo desk, the showroom) count in the website's "
                   "share, public_requests_day (`show` without an account)")
    out += ["", "Limits", f"  {'ID':<22} {'USED':>12} {'LIMIT':>16}  {'STATE':<8} {'REFUSED':>7}  RESETS"]
    for e in rep["limits"]:
        lid, unit = e["id"], e["unit"]
        used = "?" if lid == "runs_at_once" else _num(e["used"], unit)
        lim = "none" if e["limit"] is None else _num(e["limit"], unit)
        resets = U._hm(e["resets_at"], now) if e.get("resets_at") else _WINDOW_WORDS.get(U.LIMITS[lid]["window"], "")
        out.append(f"  {lid:<22} {used:>12} {lim:>16}  {e['state']:<8} {e.get('refused', 0):>7}  {resets}")
        where = f" · desk {e['desk_id']} {e['desk']}" if e.get("desk_id") else ""
        out.append(f"      {e['label']}{where}")
        if (e.get("basis") or {}).get("text"):
            out.append(f"      page says: {e['basis']['text']}")
        if e.get("binding"):
            out.append(f"      stopped first by: {e['binding']['label']}")
        if e.get("note"):
            out.append(f"      {e['note']}")
    out += ["", "Overrides"]
    for r in rep["overrides"]:
        out.append(f"  {r['limit_id']:<22} {_override_text(cli, r)}")
    if not rep["overrides"]:
        out.append("  none")
    out += ["", "Today (UTC): where the AI requests went (embedding requests apart, as +N embed)",
            f"  {'WHAT':<44} {'ACTIONS':>7} {'REQUESTS':>8} {'TOKENS IN / OUT':>21}  COST"]
    for t in rep["today"]:
        reqs = f"{t['requests']:,}" + (f" +{t['embed_requests']:,} embed" if t["embed_requests"] else "")
        acts = "-" if t["actions"] is None else f"{t['actions']:,}"
        out.append(f"  {t['label'][:44]:<44} {acts:>7} {reqs:>8} {t['tokens_in']:>10,} / {t['tokens_out']:<8,}  "
                   f"{_cost_text(t)}")
    if not rep["today"]:
        out.append("  nothing yet today")
    out += ["", "Last 7 UTC days: AI requests on the shared pool (yours + Atlas's own / that day's limit)"]
    for h in rep["history"][-7:]:
        d = datetime.fromtimestamp(h["day"], timezone.utc).strftime("%a %d %b")
        lim = "-" if h["limit"] is None else f"{h['limit']:,}"
        out.append(f"  {d}   {h['you']:>6,} + {h['auto']:<6,} / {lim}")
    s = rep["sends"]
    by = ", ".join(f"{k} {v:,}" for k, v in s["by_channel"].items())
    out += ["", f"Sent today: {s['today']:,}" + (f" ({by})" if by else "") +
            f"; held by the 24-hour reply window: {s['held_today']:,}",
            f"Run starts waiting for a limit to reset: {rep['deferred_waiting']:,}", cli.since_line()]
    print("\n".join(out))


# ---------------------------------------------------------------------------- show (the operator's overview)
def house_report(cli: _Cli) -> dict[str, Any]:
    U, now = cli.U, cli.now
    hv = U.house_view(now)
    hermes = hv["hermes"]
    ovs = U.all_overrides()
    emails: dict[int, str] = {}
    for oid in {r["owner_id"] for r in ovs} | {r["owner_id"] for r in hv["owners"]}:
        u = cli.store.user(oid) if oid else None
        emails[oid] = str((u or {}).get("email") or "")
    for r in ovs:
        r["email"] = emails.get(r["owner_id"], "")
    for r in hv["owners"]:
        r["email"] = emails.get(r["owner_id"], "")
    return {"store": store_label(cli.store), "as_of": int(now), "enforced": U.CFG.enforce, "open_mode": U.OPEN_MODE(),
            "snapshot": {k: v for k, v in hv["snapshot"].items() if k != "error"}, "pool": _pool(U, now),
            "pool_basis": hv["pool_basis"], "limits": hv["limits"], "disk": U.disk_entry(now),
            "hermes": {"mode": hermes["mode"], "proxy_ready": hermes["proxy_ready"], "today": hermes["today"]},
            "owners": hv["owners"], "overrides": ovs, "unlimited_emails": sorted(U.CFG.unlimited_emails),
            "counting_since": cli.counting_since()}


def _print_house(cli: _Cli, rep: dict[str, Any]) -> None:
    U, now = cli.U, cli.now
    out = cli.header("Atlas usage · all accounts (the operator's overview)")
    p, s = rep["pool"], rep["snapshot"]
    out.append(f"          today: {p['state']}, resets {U._hm(p['resets_at'], now)}")
    if p.get("used_provider") is not None:
        out.append(f"          OpenRouter's count at its last read ({U._hm(p['read_at'], now)}): {p['used_provider']:,}; "
                   f"elsewhere on the key: {p['elsewhere']:,}")
    else:
        out.append("          OpenRouter's count today: not read yet today")
    out.append(f"          this server today: {p['used_here']:,}")
    if p.get("exhausted_at"):
        out.append(f"          OpenRouter answered free-models-per-day at {U._hm(p['exhausted_at'], now)}")
    if s.get("ts"):
        key = "none set" if s.get("limit") is None else f"${s['limit']:,.2f}" + (
            f" (remaining ${s['limit_remaining']:,.2f})" if s.get("limit_remaining") is not None else "")
        month = "-" if s.get("usage_monthly") is None else f"${s['usage_monthly']:,.2f}"
        out.append(f"  Key     read {U._hm(s['ts'], now)}: credit limit on OpenRouter: {key}; usage this month: {month}")
    else:
        out.append("  Key     not read yet: the server stores OpenRouter's figures for the key each time it reads them")
    out += ["", "Server-wide limits", f"  {'ID':<22} {'USED':>12} {'LIMIT':>16}  {'STATE':<8} {'REFUSED':>7}"]
    for e in rep["limits"] + [rep["disk"]]:
        lim = "none" if e["limit"] is None else _num(e["limit"], e["unit"])
        out.append(f"  {e['id']:<22} {_num(e['used'], e['unit']):>12} {lim:>16}  {e['state']:<8} {e.get('refused', 0):>7}")
        out.append(f"      {e['label']}: {(e.get('basis') or {}).get('text', '')}")
    h = rep["hermes"]
    t = h["today"]
    out += ["", f"Hermes  {h['mode']}" + (" (proxy token set)" if h["proxy_ready"] else "") +
            f"; inner requests today {t['inner']:,} (attributed by overlap {t['overlap']:,}, not attributed "
            f"{t['unattributed']:,}, refused {t['refused']:,})",
            "Unlimited accounts (USAGE_UNLIMITED_EMAILS): " + (", ".join(rep["unlimited_emails"]) or "none")]
    out += ["", "Accounts with use today",
            f"  {'OWNER':<7} {'EMAIL':<30} {'AI REQUESTS':>15} {'ATLAS OWN':>13} {'PAID (MONTH)':>17} {'REFUSED':>8}"]
    for r in rep["owners"]:
        lims = r["limits"]
        ai = f"{r['ai_requests_day']:,}/" + ("none" if lims["ai_requests_day"] is None else f"{lims['ai_requests_day']:,}")
        auto = f"{r['ai_requests_auto_day']:,}/" + ("none" if lims["ai_requests_auto_day"] is None
                                                    else f"{lims['ai_requests_auto_day']:,}")
        paid = f"${r['paid_usd_month']:,.2f}/" + ("none" if lims["paid_usd_month"] is None
                                                 else f"${lims['paid_usd_month']:,.2f}")
        out.append(f"  #{r['owner_id']:<6} {(r['email'] or '-')[:30]:<30} {ai:>15} {auto:>13} {paid:>17} "
                   f"{r['refused_today']:>8,}")
    if not rep["owners"]:
        out.append("  none")
    out += ["", "Overrides on record"]
    for r in rep["overrides"]:
        out.append(f"  #{r['owner_id']:<6} {(r['email'] or '-')[:30]:<30} {r['limit_id']:<22} {_override_text(cli, r)}")
    if not rep["overrides"]:
        out.append("  none")
    out += ["", cli.since_line()]
    print("\n".join(out))


# ---------------------------------------------------------------------------- set / clear / prune
def _value(U, lid: str, raw: str) -> tuple[float | int | None, str]:
    """--value for one limit: (value, "") or (None, error). 'none' = no Atlas limit."""
    text = str(raw).strip()
    if text.lower() in NONE_WORDS:
        return None, ""
    unit = U.LIMITS[lid]["unit"]
    try:
        v = float(text)
    except ValueError:
        return None, f"--value {raw!r} is not a number (or 'none' for no Atlas limit)"
    if v != v or v in (float("inf"), float("-inf")) or v < 0:
        return None, f"--value {raw!r}: give 0 or more (or 'none' for no Atlas limit)"
    if unit in ("usd", "MB"):
        return v, ""
    if not v.is_integer():
        return None, f"{lid} counts whole {unit}: give a whole number"
    if lid == "run_requests" and v < 1:          # usage.run_cap reads 0 as "no cap"
        return None, "run_requests must be 1 or more (use --value none for no per-run cap)"
    return int(v), ""


def _set_by() -> str:
    import getpass
    try:
        return f"cli:{getpass.getuser()}"[:120]
    except Exception:
        return "cli"


def _applies_line(U) -> str:
    return f"A running server applies it within {int(U._OV_TTL)} s (it re-reads overrides that often)."


def cmd_set(cli: _Cli, who: tuple[int, str], lid: str, raw: str, note: str) -> int:
    U = cli.U
    o, email = who
    if lid not in U.LIMITS:
        return _fail(f"unknown limit {lid!r}; an account's limits are: {', '.join(U.OWNER_LIMITS)}")
    if lid not in U.OWNER_LIMITS:
        return _fail(f"{lid} is not a per-account limit: it is the whole server's, set by {U.HOUSE_SETTINGS.get(lid, 'the env')}. "
                     "Change it in the service's env file (/etc/atlas/atlas.env on atlas-1), then restart the portal.")
    if o == 0 and not U.OPEN_MODE() and lid in _OWNER_SCOPE_ONLY:
        return _fail(f"accounts are on, so owner 0's desks (the demo desk, the showroom) count in the website's share: "
                     f"{lid} does not apply to them. That share is USAGE_PUBLIC_SHARE.")
    value, err = _value(U, lid, raw)
    if err:
        return _fail(err)
    note = str(note or "").strip()
    if len(note) > 200:
        return _fail(f"--note is {len(note)} characters; the ledger keeps 200")
    old = {r["limit_id"]: r for r in U.overrides(o)}
    before = U.limit_value(o, lid)
    l2_follows = lid == "ai_requests_day" and "ai_requests_auto_day" not in old
    l2_before = U.limit_value(o, "ai_requests_auto_day") if l2_follows else None
    U.set_override(o, lid, value, note=note, set_by=_set_by())
    after = U.limit_value(o, lid)
    was = "an override" if lid in old else "the default"
    lines = [f"Store: {store_label(cli.store)}",
             f"Set {lid} ({U.LIMITS[lid]['label']}) for account {cli.who(o, email)}: {cli.amount(lid, after)}",
             f"  was {cli.amount(lid, before)} ({was})"]
    if l2_follows:
        l2 = U.limit_value(o, "ai_requests_auto_day")
        lines.append(f"  ai_requests_auto_day follows it: {cli.amount('ai_requests_auto_day', l2)} "
                     f"(was {cli.amount('ai_requests_auto_day', l2_before)})")
    if lid == "ai_requests_auto_day" and after is not None:
        l1 = U.limit_value(o, "ai_requests_day")
        if l1 is not None and after > l1:
            lines.append(f"  note: above this account's ai_requests_day ({l1:,}); Atlas's own requests count there too, "
                         "so that limit stops them first")
    if lid == "ai_requests_day" and after is not None and after > U.pool_size():
        lines.append(f"  note: above the whole shared pool ({U.pool_size():,} a UTC day), which stops this account first")
    if lid in U._UNLIMITED_IDS and U.is_unlimited(o):
        lines.append("  note: the account is in USAGE_UNLIMITED_EMAILS; this override takes precedence for this limit")
    lines += [f"  Their Usage page says: \"{U.basis(o, lid, cli.now)['text']}\"", _applies_line(U),
              f"Undo: python -m atlas usage clear {'--email ' + email if o else '--owner 0'} --limit {lid}"]
    print("\n".join(lines))
    return 0


def cmd_clear(cli: _Cli, who: tuple[int, str], lid: str) -> int:
    U = cli.U
    o, email = who
    old = {r["limit_id"]: r for r in U.overrides(o)}
    if lid == "all":
        ids = sorted(old)
    elif lid not in U.LIMITS:
        return _fail(f"unknown limit {lid!r}; an account's limits are: {', '.join(U.OWNER_LIMITS)}")
    else:
        ids = [lid] if lid in old else []
    lines = [f"Store: {store_label(cli.store)}"]
    if not ids:
        what = "no overrides" if lid == "all" else f"no override of {lid}"
        lines.append(f"Account {cli.who(o, email)} has {what}: nothing to clear.")
        if lid != "all":
            lines.append(f"  In force: {cli.amount(lid, U.limit_value(o, lid))}")
        print("\n".join(lines))
        return 0
    for i in ids:
        before = U.limit_value(o, i) if i in U.LIMITS else old[i]["value"]
        U.clear_override(o, i)
        now_v = cli.amount(i, U.limit_value(o, i)) if i in U.LIMITS else "-"
        was = cli.amount(i, before) if i in U.LIMITS else str(before)
        lines.append(f"Cleared {i} for account {cli.who(o, email)}: {now_v} (was {was}, the override)")
    lines.append(_applies_line(U))
    print("\n".join(lines))
    return 0


def cmd_prune(cli: _Cli) -> int:
    U = cli.U
    out = U.prune()
    rows = [("usage_events", out.get("events", 0), f"older than {U.CFG.events_keep_days} days (USAGE_EVENTS_KEEP_DAYS)"),
            ("usage_counters", out.get("counters", 0),
             f"day rows older than {U.CFG.counters_keep_days} days (USAGE_COUNTERS_KEEP_DAYS); month and total rows "
             "are kept"),
            ("usage_provider", out.get("provider", 0), f"key snapshots older than {U.PROVIDER_KEEP_DAYS} days"),
            ("usage_deferred", out.get("deferred", 0),
             f"run starts that started or were dropped more than {U.DEFERRED_KEEP_DAYS} days ago")]
    lines = [f"Store: {store_label(cli.store)}", "Retention, run once (the server also runs it every hour):"]
    lines += [f"  {t:<16} {n:>8,} removed   {why}" for t, n, why in rows]
    lines.append(f"  {'usage_overrides':<16} {'kept':>8}           until cleared")
    print("\n".join(lines))
    return 0


# ---------------------------------------------------------------------------- entry point
def main(argv: list[str] | None = None, *, store=None) -> int:
    """`python -m atlas usage ...`. `store`: an open Store to use instead of the portal's (tests)."""
    args = _parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):           # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    env_note = ""
    if args.env_file:                                 # before atlas.config and atlas.usage read the environment
        try:
            names = load_env_file(args.env_file)
        except OSError as exc:
            root = " (the file is root-only: run this as root)" if isinstance(exc, PermissionError) else ""
            return _fail(f"cannot read {args.env_file}: {exc.strerror or exc}{root}")
        env_note = f"{args.env_file} ({len(names)} variables)"
    from . import config as cfg
    from . import usage as U
    from .store import Store
    if store is None:
        url = os.environ.get("DATABASE_URL", "").strip() or None
        try:
            store = Store(cfg.DATA_DIR / "desk.db", url=url)      # the portal's database (atlas/desk/app.py DB_PATH)
        except Exception as exc:
            return _fail(f"cannot open the store: {type(exc).__name__}: {str(exc)[:200]}")
    if U._store is not store:
        U.init(store)
    U.restore_snapshot()
    cli = _Cli(U, store, env_note)
    if args.cmd == "prune":
        return cmd_prune(cli)
    if args.cmd == "show" and args.email is None and args.owner is None:
        rep = house_report(cli)
        if args.json:
            print(json.dumps(rep, indent=1, default=str))
        else:
            _print_house(cli, rep)
        return 0
    who = cli.resolve(args.email, args.owner)
    if isinstance(who, str):
        return _fail(who)
    if args.cmd == "show":
        rep = owner_report(cli, *who)
        if args.json:
            print(json.dumps(rep, indent=1, default=str))
        else:
            _print_owner(cli, rep)
        return 0
    if args.cmd == "set":
        return cmd_set(cli, who, args.limit, args.value, args.note)
    return cmd_clear(cli, who, args.limit)


if __name__ == "__main__":
    sys.exit(main())
