"""Verify prospect lists produced by researcher agents.

For every prospect: fetch home + contact page ourselves, collect every email that literally appears in the HTML,
confirm the researcher's email is one of them (or adopt one we found), detect signals with study.SIGNALS,
re-score fit deterministically, and write a merged CSV/JSON. `--load` inserts verified rows into the Growth desk CRM.
"""
from __future__ import annotations
import csv, glob, json, re, sys, time
from pathlib import Path
from urllib.parse import urljoin
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from atlas import study

SCRATCH = Path(r"C:\Users\pierr\AppData\Local\Temp\claude\C--Users-pierr\779a2ca6-8429-4d77-b5f0-4648b50ef124\scratchpad")
OUT = Path(__file__).resolve().parent
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.I)
BAD_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
GENERIC_SKIP = ("sentry", "example.", "wixpress", "domain.com", "email.com", "yourdomain", "@2x", "godaddy", "wordpress")
CONTACT_PATHS = ("/contact", "/contact-us", "/contact-us/", "/contact/", "/about", "/about-us")

def fetch(url: str) -> str:
    try:
        code, _final, html = study._fetch(url, 15.0)
        return html if code < 400 else ""
    except Exception:
        return ""

def _cf_decode(hexs: str) -> str:
    try:
        b = bytes.fromhex(hexs); return "".join(chr(x ^ b[0]) for x in b[1:])
    except Exception:
        return ""

def emails_in(html: str) -> set[str]:
    out = set()
    decoded = " ".join(_cf_decode(h) for h in re.findall(r'data-cfemail="([0-9a-fA-F]+)"', html or ""))
    for m in EMAIL_RX.findall((html or "") + " " + decoded):
        e = m.strip(".").lower()
        if e.endswith(BAD_EXT) or any(b in e for b in GENERIC_SKIP):
            continue
        out.add(e)
    return out

def signals_in(html: str) -> set[str]:
    return {k for k, _l, rx in study.SIGNALS if re.search(rx, html or "", re.I)}

def verify(p: dict) -> dict:
    url = (p.get("website") or p.get("url") or "").strip()
    p["website"] = url
    if not url:
        p.update(verified=False, verify_note="no website"); return p
    if not url.startswith("http"):
        url = "https://" + url; p["website"] = url
    home = fetch(url)
    htmls = [home]
    found = emails_in(home)
    if home:
        # follow the page the site itself calls contact, else guess paths
        links = [u for u, lbl in study._links(home, url) if re.search(r"contact|get-in-touch|enquir", u + " " + lbl, re.I)]
        for cu in (links[:2] or [urljoin(url, cp) for cp in CONTACT_PATHS[:3]]):
            h = fetch(cu); htmls.append(h); found |= emails_in(h)
    sig = set().union(*(signals_in(h) for h in htmls))
    claimed = (p.get("email") or "").strip().lower()
    if not home:
        p.update(verified=False, verify_note="site unreachable (may be bot-walled)", found_emails=sorted(found))
    elif claimed and claimed in found:
        p.update(verified=True, verify_note="email seen on site")
    elif claimed and not found:
        p.update(verified=False, verify_note="claimed email NOT on fetched pages; no emails found (JS-rendered?)")
    elif claimed:
        p.update(verified=False, verify_note=f"claimed email not on site; site shows {sorted(found)[:3]}")
    elif found:
        own = [e for e in sorted(found) if e.split("@")[1].split(".")[0] in url] or sorted(found)
        p.update(email=own[0], verified=True, verify_note="email found by crawler (researcher had none)")
    else:
        p.update(verified=False, verify_note="no email on site")
    p["found_emails"] = sorted(found)
    p["site_signals"] = sorted(sig)
    # deterministic fit: base from researcher, adjust for what we actually saw
    fit = int(p.get("fit_score") or 3)
    if "livechat" in sig: fit -= 2
    if "booking" in sig: fit -= 1
    if "form" in sig and "livechat" not in sig: fit += 1
    if "whatsapp" in sig: fit += 0  # neutral: shows they take async enquiries
    p["fit_final"] = max(1, min(5, fit))
    return p

def main():
    load = "--load" in sys.argv
    rows = []
    if "--retry" in sys.argv:
        prev = json.loads(Path(sys.argv[sys.argv.index("--retry") + 1]).read_text(encoding="utf-8"))
        keep = [r for r in prev if r.get("verified")]
        redo = [r for r in prev if not r.get("verified")]
        print(f"retrying {len(redo)} unverified; keeping {len(keep)}")
        out = keep + [verify(r) for r in redo]
        for r in out[len(keep):]: print(f"  {r['name'][:38]:38} {'OK' if r['verified'] else 'NO'} {r['verify_note'][:70]}")
        return finish(out, load)
    for f in sorted(SCRATCH.glob("prospects_*.json")):
        seg = f.stem.replace("prospects_", "")
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            print("skip", f.name, exc); continue
        for p in data:
            p["segment"] = seg; rows.append(p)
    print(f"{len(rows)} prospects from {len(list(SCRATCH.glob('prospects_*.json')))} files")
    seen = set(); out = []
    for i, p in enumerate(rows, 1):
        key = (p.get("website") or p.get("name") or "").lower().rstrip("/")
        if key in seen: continue
        seen.add(key)
        t = time.time(); v = verify(p)
        print(f"[{i}/{len(rows)}] {v.get('name','?')[:38]:38} {('OK ' if v['verified'] else 'NO ')} fit={v['fit_final']} {v['verify_note'][:60]} ({time.time()-t:.0f}s)")
        out.append(v)
    finish(out, load)

def finish(out, load):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = OUT / stamp; d.mkdir(exist_ok=True)
    (d / "prospects.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    cols = ["segment", "name", "website", "city", "email", "verified", "verify_note", "fit_final", "fit_score", "owner_name", "phone", "review_signal", "site_signals", "signals", "why_now", "email_source"]
    with open(d / "prospects.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore"); w.writeheader()
        for r in out:
            r2 = dict(r); r2["site_signals"] = ",".join(r.get("site_signals", [])); r2["signals"] = ",".join(r.get("signals", []) or [])
            w.writerow(r2)
    ok = [r for r in out if r["verified"]]
    print(f"\nverified {len(ok)}/{len(out)} -> {d}")
    if load:
        import sqlite3
        c = sqlite3.connect(str(OUT.parents[1] / "data" / "desk.db"))
        # flag the earlier model-guessed contacts
        c.execute("update contacts set notes='[UNVERIFIED EMAIL - model-guessed, do not send] ' || notes where desk_id=8 and email like 'owner@%' and notes not like '[UNVERIFIED%'")
        n = 0
        for r in ok:
            if c.execute("select 1 from contacts where desk_id=8 and (lower(email)=? or lower(company)=?)", (r["email"].lower(), r["name"].lower())).fetchone():
                continue
            notes = (f"Website: {r['website']} | {r.get('city','')} | {r.get('type') or r.get('trade','')} | reviews: {r.get('review_signal','')} | "
                     f"site signals: {','.join(r['site_signals'])} | fit {r['fit_final']}/5 | email verified ({r['verify_note']}) | hook: {r.get('why_now','')}")
            c.execute("insert into contacts(name,company,email,phone,stage,notes,next_action,updated,desk_id) values(?,?,?,?,?,?,?,?,8)",
                      (r.get("owner_name") or r["name"], r["name"], r["email"], r.get("phone", ""), "New", notes, "send blueprint offer", time.time()))
            n += 1
        c.commit(); print(f"loaded {n} new contacts into Growth desk (id 8)")

if __name__ == "__main__":
    main()
