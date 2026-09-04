"""Pre-study for the Design Studio: read a business's public links before the first question is asked.

`study(links, on_status, ...)` fetches the home page plus the most informative internal pages (about, services,
pricing, contact, booking...), extracts text and *signals* (contact forms, phone, email, WhatsApp, booking widgets,
chat widgets, platform, socials, opening hours), then produces a company profile:

    {name, summary, sector, services, locations, customers, team_hint, channels, tech, tone,
     opportunities:[{title, why, effort}], opening_message, suggestions, blueprint}

Live mode asks the model for the profile (grounded in the fetched text); demo mode derives one heuristically so the
whole flow works offline. Everything fetched is public web content; nothing is stored beyond the design session.
"""
from __future__ import annotations

import html as _html
import json
import re
import time
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

MAX_LINKS = 4
MAX_PAGES = 8
PAGE_CHARS = 5000
UA = "AtlasDesks-Designer/1.0 (+business scoping; reads public pages only)"
_INTERESTING = re.compile(r"(about|service|what-we-do|treatment|pricing|price|fees|plans|contact|book|appointment|team|faq|"
                          r"product|shop|menu|locations?|areas|reviews?|testimonials|how-it-works|case|work|portfolio)", re.I)
_SKIP = re.compile(r"(privacy|cookie|terms|login|sign-?in|cart|wp-|\.pdf|\.jpg|\.png|mailto:|tel:|javascript:|#)", re.I)

SIGNALS: list[tuple[str, str, str]] = [   # (key, label, regex on raw html)
    ("form", "contact form", r"<form[^>]*>"),
    ("email", "email address", r"mailto:|[\w.+-]+@[\w-]+\.[a-z]{2,}"),
    ("phone", "phone number", r"tel:|\b0\d{3}\s?\d{3}\s?\d{3,4}\b|\+44\s?\d"),
    ("whatsapp", "WhatsApp", r"wa\.me|api\.whatsapp\.com|whatsapp"),
    ("livechat", "live chat widget", r"intercom|tawk\.to|crisp\.chat|drift\.com|tidio|livechat|hubspot.*conversations|zendesk"),
    ("booking", "online booking widget", r"calendly|acuityscheduling|setmore|fresha|treatwell|dentally|opencare|zocdoc|simplybook|"
                                         r"bookwhen|timely|squareup\.com/appointments|booksy|resdiary|opentable|checkatrade"),
    ("ecommerce", "online store", r"cdn\.shopify|woocommerce|add-to-cart|bigcommerce|snipcart|squarespace-commerce"),
    ("payments", "payments", r"stripe|paypal|gocardless|klarna"),
    ("crm_forms", "marketing/CRM forms", r"hsforms|hubspot|mailchimp|typeform|jotform|gravityforms|activecampaign|klaviyo"),
    ("reviews", "reviews widget", r"trustpilot|reviews\.io|feefo|google.*reviews|checkatrade|yelp"),
    ("social_ig", "Instagram", r"instagram\.com/"),
    ("social_fb", "Facebook", r"facebook\.com/"),
    ("social_li", "LinkedIn", r"linkedin\.com/"),
    ("hours", "opening hours", r"(mon|monday)[^<]{0,40}(fri|friday|sat|sun)|opening hours|open(ing)? times"),
]
PLATFORMS = [("wordpress", r"wp-content|wp-includes"), ("wix", r"wix\.com|wixstatic"), ("squarespace", r"squarespace"),
             ("shopify", r"cdn\.shopify|myshopify"), ("webflow", r"webflow"), ("hubspot", r"hs-scripts|hubspot"),
             ("framer", r"framer"), ("godaddy", r"godaddy|secureserver")]
SECTORS = [
    ("dental / clinic", r"dentist|dental|clinic|patient|treatment|physio|therapy|gp\b|surgery|aesthetic|orthodont"),
    ("trades", r"plumb|heating|boiler|electric|roof|builder|joiner|locksmith|gas safe|landscap|decorat"),
    ("estate / lettings", r"estate agent|lettings|landlord|tenant|valuation|property management|rightmove|zoopla"),
    ("agency", r"agency|branding|campaign|seo|ppc|social media|creative|design studio|marketing"),
    ("consultancy", r"consult|advisory|strategy|transformation|fractional|interim"),
    ("law / accounting", r"solicitor|law firm|legal|accountan|bookkeep|tax|payroll|audit"),
    ("e-commerce", r"shop|add to cart|checkout|free shipping|our products|collection"),
    ("hospitality", r"restaurant|menu|book a table|hotel|rooms|café|cafe|bar\b"),
    ("saas / software", r"software|platform|api|pricing plans|free trial|dashboard|integrations"),
    ("education / coaching", r"course|coach|tutor|training|academy|lesson|workshop"),
    ("recruitment", r"recruit|candidates|vacancies|staffing|talent"),
    ("fitness / wellness", r"gym|yoga|pilates|personal train|studio|membership|class"),
]


def _clean(url: str) -> str:
    url = url.strip().strip('<>"\'')
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def _text(html: str) -> str:
    t = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = _html.unescape(t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def _title(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    t = _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""
    return re.split(r"\s+[|\-–—:]\s+", t)[0][:80] if t else ""


def _links(html: str, base: str) -> list[tuple[str, str]]:
    out, seen = [], set()
    host = urlparse(base).netloc.lower().replace("www.", "")
    for m in re.finditer(r'(?is)<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', html):
        href, label = m.group(1), _text(m.group(2))[:60]
        if _SKIP.search(href):
            continue
        u = urljoin(base, href).split("#")[0].rstrip("/")
        if urlparse(u).netloc.lower().replace("www.", "") != host or u in seen:
            continue
        seen.add(u)
        out.append((u, label))
    return out


_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def _fetch(url: str, timeout: float = 15.0) -> tuple[int, str, str]:
    """Honest UA first; many small-business hosts (Cloudflare, Wix, WAFs) 403 unknown agents, so retry as a browser."""
    import httpx
    from . import secure as SEC
    reason = SEC.private_url_reason(url)
    if reason:
        return 403, url, ""                      # never study internal address space
    hdrs = {"User-Agent": UA, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en-GB,en;q=0.9"}
    r = httpx.get(url, follow_redirects=True, timeout=timeout, headers=hdrs)
    if r.status_code in (401, 403, 406, 429, 503):
        r = httpx.get(url, follow_redirects=True, timeout=timeout, headers={**hdrs, "User-Agent": _BROWSER_UA})
    ctype = r.headers.get("content-type", "")
    body = r.text if ("html" in ctype or "text" in ctype) else ""
    if r.status_code in (403, 503) or _is_challenge(body):
        # Cloudflare / bot-challenge page: fall back to a public reader service that renders the page for us
        txt = _reader_fallback(url, timeout)
        if txt:
            return 200, url, "<html><head><title>" + _html.escape(txt.splitlines()[0][:80]) + "</title></head><body>" + _html.escape(txt) + "</body></html>"
    return r.status_code, str(r.url), body


def _is_challenge(html: str) -> bool:
    h = (html or "")[:4000].lower()
    return ("just a moment" in h and "cloudflare" in h) or "cf-chl" in h or "_cf_chl_opt" in h or "checking your browser" in h


def _reader_fallback(url: str, timeout: float) -> str:
    """r.jina.ai renders a public page and returns its text. Only used when direct fetch is blocked; opt out with STUDY_READER=0."""
    import os
    import httpx
    if os.environ.get("STUDY_READER", "1") in ("0", "false", "no"):
        return ""
    try:
        r = httpx.get("https://r.jina.ai/" + url, timeout=max(timeout, 25), headers={"User-Agent": UA, "X-Return-Format": "text"})
        if r.status_code < 400 and len(r.text) > 200 and not _is_challenge(r.text):
            return r.text[:60000]
    except Exception:
        pass
    return ""


def crawl(links: list[str], on_status: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Fetch the home page + up to MAX_PAGES informative pages across the given links. Never raises."""
    say = on_status or (lambda s: None)
    pages: list[dict[str, Any]] = []
    signals: dict[str, int] = {}
    platform = ""
    nav_labels: list[str] = []
    errors: list[str] = []
    budget = MAX_PAGES
    for raw in [_clean(x) for x in links][:MAX_LINKS]:
        if not raw or budget <= 0:
            continue
        try:
            say(f"Reading {urlparse(raw).netloc}{urlparse(raw).path}")
            code, final, html = _fetch(raw)
        except Exception as exc:
            errors.append(f"{raw}: {type(exc).__name__}")
            say(f"Could not read {raw} ({type(exc).__name__})")
            continue
        if code >= 400 or not html:
            errors.append(f"{raw}: HTTP {code}")
            say(f"{urlparse(raw).netloc} returned HTTP {code}")
            continue
        budget -= 1
        pages.append({"url": final, "title": _title(html), "text": _text(html)[:PAGE_CHARS], "home": True})
        for key, _label, rx in SIGNALS:
            if re.search(rx, html, re.I):
                signals[key] = signals.get(key, 0) + 1
        for name, rx in PLATFORMS:
            if not platform and re.search(rx, html, re.I):
                platform = name
        cands = _links(html, final)
        nav_labels += [lbl for _u, lbl in cands[:40] if lbl and len(lbl) < 30]
        ranked = sorted(cands, key=lambda c: (0 if _INTERESTING.search(c[0] + " " + c[1]) else 1, len(c[0])))
        for u, lbl in ranked:
            if budget <= 0 or not _INTERESTING.search(u + " " + lbl):
                break
            try:
                say(f"Reading {urlparse(u).path or '/'}")
                code, final2, h2 = _fetch(u, 12.0)
            except Exception:
                continue
            if code >= 400 or not h2:
                continue
            budget -= 1
            pages.append({"url": final2, "title": _title(h2) or lbl, "text": _text(h2)[:PAGE_CHARS], "home": False})
            for key, _label, rx in SIGNALS:
                if re.search(rx, h2, re.I):
                    signals[key] = signals.get(key, 0) + 1
    return {"pages": pages, "signals": signals, "platform": platform, "nav": list(dict.fromkeys(nav_labels))[:30], "errors": errors}


def _hermes_browse(links: list[str], say: Callable[[str], None]) -> list[dict[str, Any]]:
    """Last-resort reader for bot-walled sites: a configured Hermes Agent opens the page with its own browser/tools
    and returns the text. Uses HERMES_AGENT_URL/KEY env (the same instance that powers the hermes_agent engine)."""
    import os
    url = os.environ.get("HERMES_AGENT_URL", "").strip()
    key = os.environ.get("HERMES_AGENT_KEY", "").strip()
    if not url or not key:
        return []
    import httpx
    pages = []
    for raw in [_clean(x) for x in links][:2]:
        if not raw:
            continue
        say(f"Site blocks crawlers — asking Hermes Agent to open {urlparse(raw).netloc}")
        try:
            r = httpx.post(url.rstrip("/") + "/v1/chat/completions",
                           headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                    "X-Hermes-Session-Key": "atlas:study"},
                           timeout=180,
                           json={"model": "hermes-agent", "messages": [{
                               "role": "user",
                               "content": (f"Open {raw} with your tools and return ONLY the page's visible text content "
                                           f"(first ~4000 characters), starting with the page title on the first line. "
                                           f"No commentary, no summary - the raw readable text.")}]})
            txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content", "") if r.status_code < 400 else ""
        except Exception:
            txt = ""
        if txt and len(txt) > 300:
            lines = txt.strip().splitlines()
            pages.append({"url": raw, "title": lines[0][:80], "text": txt[:PAGE_CHARS * 2], "home": True})
    return pages


def signal_labels(signals: dict[str, int]) -> list[str]:
    lab = {k: l for k, l, _ in SIGNALS}
    return [lab[k] for k in lab if k in signals]


def guess_sector(text: str) -> str:
    low = text.lower()
    best, score = "services business", 0
    for name, rx in SECTORS:
        n = len(re.findall(rx, low))
        if n > score:
            best, score = name, n
    return best


# ---------------------------------------------------------------------------- heuristic (demo / fallback) profile
def heuristic_profile(crawl_result: dict[str, Any], links: list[str]) -> dict[str, Any]:
    pages = crawl_result["pages"]
    home = next((p for p in pages if p.get("home")), pages[0] if pages else None)
    alltext = "\n".join(p["text"] for p in pages)
    name = (home or {}).get("title") or urlparse(_clean(links[0])).netloc.replace("www.", "").split(".")[0].title() if links else "Your business"
    sector = guess_sector(alltext)
    services = [l for l in crawl_result["nav"] if re.search(_INTERESTING, l) and not re.search(r"contact|about|home|book|faq|review|team", l, re.I)][:8]
    channels = signal_labels(crawl_result["signals"])
    locs = list(dict.fromkeys(re.findall(r"\b(London|Manchester|Leeds|Birmingham|Bristol|Liverpool|Glasgow|Edinburgh|Sheffield|Newcastle|Nottingham|Leicester|Cardiff|Brighton|Oxford|Cambridge|Reading|Salford|Trafford|Stockport)\b", alltext)))[:5]
    summary = (home or {}).get("text", "")[:220].replace("\n", " ")
    kind = ("proposal" if "consult" in sector or "agency" in sector else "orders" if "e-commerce" in sector else "inbox" if "law" in sector else "sales")
    opps = {
        "sales": [{"title": "Same-hour replies to new enquiries", "why": f"You take enquiries via {', '.join(channels[:3]) or 'your website'}; every hour of delay loses bookings.", "effort": "1 week"},
                  {"title": "Qualify and hand reception a ready-to-call lead", "why": "Agents collect the missing details before a human picks up the phone.", "effort": "1 week"},
                  {"title": "Follow-up chaser for quotes that went quiet", "why": "Most lost jobs are never-chased quotes.", "effort": "2 days"}],
        "proposal": [{"title": "Brief → proposal in an hour", "why": "Research, approach, pricing and a client-ready draft assembled for your review.", "effort": "1 week"},
                     {"title": "Inbound enquiry triage", "why": "Separate real prospects from tyre-kickers before you spend a call on them.", "effort": "3 days"}],
        "orders": [{"title": "Order-status replies", "why": "Answer 'where is my order' from your store data, instantly.", "effort": "1 week"},
                   {"title": "Product listing and launch copy", "why": "Consistent listings in your brand voice.", "effort": "3 days"}],
        "inbox": [{"title": "Inbox triage every morning", "why": "Classify, draft routine replies, escalate the rest.", "effort": "1 week"},
                  {"title": "Client onboarding and document chasing", "why": "The paperwork loop that eats admin time.", "effort": "2 weeks"}],
    }[kind]
    sector_phrase = sector if "business" in sector else f"{sector} business"
    opening = (f"I've read {name}'s site" + (f" — {len(pages)} page(s)" if pages else "") + f". You look like a {sector_phrase}"
               + (f" in {', '.join(locs[:2])}" if locs else "") + (f", offering {', '.join(services[:4])}" if services else "")
               + (f". I can see {', '.join(channels[:4])}" if channels else "")
               + ". The processes I'd look at first: " + "; ".join(o["title"].lower() for o in opps[:3])
               + ". Which of these costs you the most time each week — or is it something else?")
    return {"name": name[:60], "summary": summary, "sector": sector, "services": services, "locations": locs, "customers": "",
            "team_hint": "", "channels": channels, "tech": ([crawl_result["platform"]] if crawl_result["platform"] else []),
            "tone": "", "opportunities": opps, "opening_message": opening,
            "suggestions": [o["title"] for o in opps[:3]] + ["Something else"], "kind": kind, "pages_read": len(pages),
            "errors": crawl_result["errors"], "links": links}


STUDY_PROMPT = """You are the Atlas Desks solutions designer. Below is text scraped from a business's public web pages, plus
signals detected in the HTML. Produce a compact JSON profile for a scoping call. Ground every claim in the text; where
unknown, use "" or []. Then write the opening message you would say to the owner: 2-4 sentences showing you understood
their business (name, what they do, who for, where, how customers reach them), followed by the 2-3 processes you would
automate first, ending with one question about where their time goes. Professional, plain English, no hype, no markdown.

Return ONLY this JSON (no prose, no fences):
{"name": "", "summary": "one sentence", "sector": "", "services": [], "locations": [], "customers": "", "team_hint": "",
 "channels": ["how customers contact/book, from the signals + text"], "tech": ["platforms/tools detected"], "tone": "their brand voice in 5 words",
 "opportunities": [{"title": "", "why": "", "effort": "days|1 week|2 weeks"}],
 "opening_message": "", "suggestions": ["3-4 short reply chips, max 8 words"],
 "blueprint": {"business": {"name": "", "tagline": "", "description": "", "services": [], "target_clients": "", "tone": ""},
               "agents": [{"id": "", "name": "", "role": "", "goal": "", "tools": ["read_file"], "strong": false}],
               "workflows": [{"id": "", "name": "", "trigger": {"kind": "webhook|inbox|schedule|manual", "detail": ""}, "steps": []}],
               "connectors": [{"kind": "smtp|imap|http|mcp|webhook", "name": "", "purpose": "", "required": true}],
               "policy": {"no_money_figures": true, "max_words": 200, "banned_phrases": []}}}
The blueprint is a first draft for the top opportunity only: 2-4 specialist agents (Atlas orchestrates; do not include it),
one workflow, the connectors implied by the channels you found (a contact form => webhook, email => imap + smtp,
a booking widget => http connector named after it)."""


def study(links: list[str], on_status: Callable[[str], None] | None = None, provider=None, model: str = "") -> dict[str, Any]:
    """Crawl + profile. `provider` (a Provider instance) enables the model-written profile; None => heuristic."""
    say = on_status or (lambda s: None)
    t0 = time.time()
    cr = crawl(links, say)
    if not cr["pages"]:
        hp = _hermes_browse(links, say)                # bot-walled site: let the Hermes Agent's own browser read it
        if hp:
            cr["pages"] = hp
            cr["errors"].append("direct crawl blocked - pages read via Hermes Agent browser")
    base = heuristic_profile(cr, links)
    if not cr["pages"]:
        base["opening_message"] = ("I couldn't read those links (" + "; ".join(cr["errors"][:3]) + "). Tell me what the business does "
                                   "and how customers reach you, and I'll sketch the team from that.")
        base["study_seconds"] = round(time.time() - t0, 1)
        return base
    if provider is None:
        base["study_seconds"] = round(time.time() - t0, 1)
        return base
    say("Building your company profile")
    corpus = "\n\n".join(f"### {p['title'] or p['url']} ({p['url']})\n{p['text'][:2500]}" for p in cr["pages"])[:14000]
    sig = ", ".join(signal_labels(cr["signals"])) or "none detected"
    user = (f"LINKS: {', '.join(links)}\nSIGNALS: {sig}\nPLATFORM: {cr['platform'] or 'unknown'}\nNAV: {', '.join(cr['nav'][:25])}\n\nPAGES:\n{corpus}")
    data = None
    try:
        r = provider.chat(STUDY_PROMPT, [provider.user_message(user)], [], model or "")
        txt = (r.text or "").strip()
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            base["model_error"] = f"no JSON in profile reply ({len(txt)} chars): {txt[:120]!r}"
        else:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError as exc:
                from .designer import _loose_json
                data = _loose_json(m.group(0))
                if data is None:
                    base["model_error"] = f"profile JSON unparseable at char {exc.pos}: {m.group(0)[max(0, exc.pos - 60):exc.pos + 20]!r}"
    except Exception as exc:
        base["model_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    if isinstance(data, dict) and data.get("opening_message"):
        for k in ("name", "summary", "sector", "services", "locations", "customers", "team_hint", "channels", "tech", "tone",
                  "opportunities", "opening_message", "suggestions", "blueprint"):
            if data.get(k):
                base[k] = data[k]
        base["source"] = "model"
    else:
        base["source"] = "heuristic"
    base["study_seconds"] = round(time.time() - t0, 1)
    return base
