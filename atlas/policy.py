"""Outbound policy layer — deterministic checks that run before anything enters the approval queue.

Prompts *ask* the model to behave; this module *enforces* it. Each rule is a small function returning a
violation string or None. Rules are switched on per business via `business["policy"]` (a dict) with
sensible defaults, so a desk template can tighten or relax them without touching code.
"""
from __future__ import annotations

import re
from typing import Any

DEFAULTS: dict[str, Any] = {
    "no_money_figures": False,     # block £/€/$ amounts or "x%" fee figures in outbound text
    "no_placeholders": True,       # [Your name], {name}, <insert ...>
    "plain_text": True,            # no markdown (**bold**, # headings, - bullets)
    "max_words": 220,
    "require_signoff": "",         # e.g. "Sam Reid" — body must contain it
    "no_exact_times": True,        # "Tuesday 10am", "3:30pm" — offer morning/afternoon instead
    "banned_phrases": [],          # list of lowercase substrings
    "no_guarantees": True,         # "guarantee", "guaranteed", "we promise"
}

_MONEY = re.compile(r"(?:[£€$]\s?\d[\d,]*(?:\.\d+)?(?:\s?[kKmM])?)|(?:\d+(?:\.\d+)?\s?(?:%|percent|pcm|per month))", re.I)
_PLACEHOLDER = re.compile(r"\[[^\]\n]{2,40}\]|\{\{?[a-z_ ]{2,30}\}?\}|<insert[^>]*>", re.I)
_MARKDOWN = re.compile(r"(\*\*[^*\n]+\*\*)|(^\s*#{1,6}\s)|(^\s*[-*•]\s)|(`[^`\n]+`)", re.M)
_EXACT_TIME = re.compile(r"\b(?:\d{1,2}(?::\d{2})?\s?(?:am|pm))\b|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\s+(?:at\s+)?\d{1,2}(?::\d{2})?\b", re.I)
_GUARANTEE = re.compile(r"\b(guarantee[ds]?|we promise|promised|100% sure|no risk)\b", re.I)


def rules_for(business: dict[str, Any]) -> dict[str, Any]:
    r = dict(DEFAULTS)
    r.update(business.get("policy") or {})
    if not r.get("require_signoff") and business.get("sender_name"):
        r["require_signoff"] = str(business["sender_name"]).split(",")[0].strip()
    return r


def check_outbound(kind: str, subject: str, body: str, business: dict[str, Any]) -> list[str]:
    """Return a list of human-readable violations (empty = clean)."""
    r = rules_for(business)
    text = f"{subject}\n{body}"
    out: list[str] = []
    if r["no_money_figures"]:
        m = _MONEY.search(text)
        if m:
            out.append(f"money/fee figure in writing: '{m.group(0).strip()}' — remove; figures only after a visit/call")
    if r["no_placeholders"]:
        m = _PLACEHOLDER.search(text)
        if m:
            out.append(f"placeholder left in: '{m.group(0)}' — fill it in or remove it")
    if r["plain_text"] and kind in ("email", "whatsapp", "sms"):
        m = _MARKDOWN.search(body)
        if m:
            out.append(f"markdown formatting in a plain message: '{m.group(0).strip()[:30]}' — plain text only")
    words = len(re.findall(r"\S+", body))
    if r["max_words"] and words > int(r["max_words"]):
        out.append(f"too long: {words} words (max {r['max_words']})")
    if kind in ("whatsapp", "sms") and words > 60:
        out.append(f"{kind} too long: {words} words (max 60)")
    if r["require_signoff"] and kind == "email" and r["require_signoff"].lower() not in body.lower():
        out.append(f"missing sign-off '{r['require_signoff']}'")
    if r["no_exact_times"]:
        m = _EXACT_TIME.search(text)
        if m:
            out.append(f"specific time proposed: '{m.group(0)}' — offer a morning/afternoon, the owner books the slot")
    if r["no_guarantees"]:
        m = _GUARANTEE.search(text)
        if m:
            out.append(f"guarantee language: '{m.group(0)}'")
    for phrase in r.get("banned_phrases") or []:
        p = str(phrase or "").strip()
        if not p:
            continue
        # whole-word / whole-phrase match: 'AI' must not fire inside 'email' or 'detail'
        if re.search(r"(?<![\w-])" + re.escape(p) + r"(?![\w-])", text, re.I):
            out.append(f"banned phrase: '{p}'")
    return out
