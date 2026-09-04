"""Browser hand — a Claude-in-Chrome-class browser agent that runs on the server.

How it works
  * Playwright drives a real Chromium with a persistent profile per desk (logins survive between runs).
  * Every step the page is SNAPSHOTTED: each visible interactive element gets a number ([12] button "Send"),
    plus the page text. The model acts by number, never by guessing coordinates. When it asks to `look` (or is
    stuck) it also gets a screenshot with the same numbers drawn on it (set-of-marks).
  * Actions are deterministic Playwright calls. Anything that could send/pay/delete/post is a SUBMIT: unless the
    task was started with allow_submit=True the agent stops there with status `needs_approval` and hands back
    exactly what it was about to do, so the desk can queue it for the owner.
  * CAPTCHAs and "verify you are human" walls stop the agent (status `blocked_captcha`); it never tries to solve them.
  * Every successful action is RECORDED into a macro (robust selectors: id / aria-label / role+name / placeholder /
    label / text / css). Macros replay without a model, cost nothing, and hand back to the agent only when a step breaks.
  * Budgets: max steps, max input tokens, per-action timeouts. Domains can be restricted with allow_domains.

Runs in the caller's thread (Playwright sync API). Use one BrowserHand per task.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config as cfg

BROWSER_DIR = cfg.DATA_DIR / "browser"
MACRO_DIR = BROWSER_DIR / "macros"
DEFAULT_MODEL = os.environ.get("BROWSER_MODEL", "anthropic/claude-sonnet-4.5")
DEFAULT_PROVIDER = os.environ.get("BROWSER_PROVIDER", "")
MAX_STEPS = int(os.environ.get("BROWSER_MAX_STEPS", "40"))
MAX_INPUT_TOKENS = int(os.environ.get("BROWSER_MAX_INPUT_TOKENS", "250000"))
ACTION_TIMEOUT_MS = 10000
SUBMIT_RE = re.compile(r"\b(send|submit|pay|purchase|buy|confirm|delete|remove|post|publish|order|checkout|book|apply|register|sign ?up|accept|agree|transfer|approve|unsubscribe|cancel subscription|place order|complete)\b", re.I)
SEARCH_RE = re.compile(r"\b(search|find|filter|look ?up|query)\b", re.I)
CAPTCHA_RE = re.compile(r"(recaptcha|hcaptcha|captcha|verify you are human|are you a robot|unusual traffic|cf-challenge|challenge-platform|press and hold)", re.I)

SNAPSHOT_JS = r"""
(() => {
  const SEL = 'a[href], button, input, textarea, select, summary, [role="button"], [role="link"], [role="textbox"], [role="checkbox"], [role="radio"], [role="tab"], [role="menuitem"], [role="option"], [role="combobox"], [role="switch"], [role="searchbox"], [contenteditable="true"], [onclick], [tabindex]:not([tabindex="-1"])';
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return null;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || s.opacity === '0' || s.pointerEvents === 'none' && el.tagName !== 'INPUT') return null;
    return r;
  };
  const labelFor = (el) => {
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return norm(l.innerText); }
    const p = el.closest('label'); if (p) return norm(p.innerText).slice(0, 80);
    return '';
  };
  const nameOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const aria = el.getAttribute('aria-label'); if (aria) return norm(aria);
    const lb = el.getAttribute('aria-labelledby'); if (lb) { const t = lb.split(/\s+/).map(i => document.getElementById(i)).filter(Boolean).map(n => norm(n.innerText)).join(' '); if (t) return t; }
    if (tag === 'input' && (el.type === 'submit' || el.type === 'button') && el.value) return norm(el.value);
    if (tag === 'input' || tag === 'textarea' || tag === 'select') { const l = labelFor(el); if (l) return l; if (el.placeholder) return norm(el.placeholder); if (el.name) return el.name; if (el.title) return norm(el.title); return ''; }
    if (tag === 'img') return norm(el.alt || el.title);
    const t = norm(el.innerText || el.textContent); if (t) return t.slice(0, 90);
    const img = el.querySelector('img[alt]'); if (img) return norm(img.alt);
    if (el.title) return norm(el.title);
    const svgT = el.querySelector('svg title'); if (svgT) return norm(svgT.textContent);
    return '';
  };
  const cssPath = (el) => {
    const parts = [];
    let n = el, depth = 0;
    while (n && n.nodeType === 1 && depth < 6) {
      let p = n.tagName.toLowerCase();
      if (n.id && !/^\d/.test(n.id) && !/\d{4,}/.test(n.id)) { parts.unshift('#' + CSS.escape(n.id)); break; }
      const par = n.parentElement;
      if (par) { const sib = Array.from(par.children).filter(c => c.tagName === n.tagName); if (sib.length > 1) p += ':nth-of-type(' + (sib.indexOf(n) + 1) + ')'; }
      parts.unshift(p); n = par; depth++;
    }
    return parts.join(' > ');
  };
  const roleOf = (el) => {
    const r = el.getAttribute('role'); if (r) return r;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || (tag === 'input' && (el.type === 'submit' || el.type === 'button'))) return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea' || el.isContentEditable) return 'textbox';
    if (tag === 'input') { if (el.type === 'checkbox') return 'checkbox'; if (el.type === 'radio') return 'radio'; if (el.type === 'search') return 'searchbox'; return 'textbox'; }
    if (tag === 'summary') return 'button';
    return 'generic';
  };
  document.querySelectorAll('[data-atlas-id]').forEach(e => e.removeAttribute('data-atlas-id'));
  const out = []; let id = 0;
  const seen = new Set();
  for (const el of document.querySelectorAll(SEL)) {
    const r = vis(el); if (!r) continue;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && (el.type === 'hidden')) continue;
    const inside = el.parentElement && el.parentElement.closest('a[href], button, [role="button"]');
    if (inside && tag !== 'input' && tag !== 'select' && tag !== 'textarea' && seen.has(inside)) continue;
    seen.add(el);
    id++; el.setAttribute('data-atlas-id', String(id));
    const role = roleOf(el);
    const name = nameOf(el);
    const rec = { id, role, tag, type: el.type || '', name: name.slice(0, 100), placeholder: norm(el.placeholder || ''), label: labelFor(el).slice(0, 80),
      value: (tag === 'input' && el.type !== 'submit' && el.type !== 'button' && el.type !== 'password') || tag === 'textarea' || tag === 'select' ? String(el.value || '').slice(0, 80) : (el.isContentEditable ? norm(el.innerText).slice(0, 80) : ''),
      href: el.href ? String(el.href).slice(0, 160) : '', checked: el.checked === true, disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
      inView: r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth,
      aria: norm(el.getAttribute('aria-label') || ''), domId: (el.id && !/\d{4,}/.test(el.id)) ? el.id : '', nameAttr: el.getAttribute('name') || '', testid: el.getAttribute('data-testid') || '', css: cssPath(el), editable: !!el.isContentEditable,
      inForm: !!el.closest('form') };
    if (el.tagName.toLowerCase() === 'select') rec.options = Array.from(el.options).slice(0, 40).map(o => norm(o.textContent)).join(' | ');
    out.push(rec);
  }
  const body = document.body ? document.body.innerText : '';
  return { url: location.href, title: document.title, elements: out, text: body.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n'), scrollY: Math.round(scrollY), scrollH: Math.round(document.documentElement.scrollHeight), viewH: innerHeight, viewW: innerWidth, frames: document.querySelectorAll('iframe').length };
})()
"""
MARKS_JS = r"""
(ids) => {
  document.querySelectorAll('.__atlas_mark').forEach(e => e.remove());
  for (const id of ids) {
    const el = document.querySelector('[data-atlas-id="' + id + '"]'); if (!el) continue;
    const r = el.getBoundingClientRect();
    const b = document.createElement('div'); b.className = '__atlas_mark';
    b.style.cssText = 'position:fixed;left:' + r.x + 'px;top:' + r.y + 'px;width:' + r.width + 'px;height:' + r.height + 'px;border:2px solid #e11d48;box-sizing:border-box;z-index:2147483646;pointer-events:none;';
    const t = document.createElement('div'); t.className = '__atlas_mark';
    t.textContent = id; t.style.cssText = 'position:fixed;left:' + Math.max(0, r.x - 2) + 'px;top:' + Math.max(0, r.y - 16) + 'px;background:#e11d48;color:#fff;font:bold 11px/14px monospace;padding:0 3px;z-index:2147483647;pointer-events:none;border-radius:2px;';
    document.body.appendChild(b); document.body.appendChild(t);
  }
}
"""
UNMARK_JS = "() => document.querySelectorAll('.__atlas_mark').forEach(e => e.remove())"

TOOLS: list[dict[str, Any]] = [
    {"name": "navigate", "description": "Open a URL in the current tab.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "click", "description": "Click the element with this number from the element list.", "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "type", "description": "Type text into the numbered textbox/editor (replaces existing text unless append=true). Set press_enter=true to submit the field afterwards.", "parameters": {"type": "object", "properties": {"id": {"type": "integer"}, "text": {"type": "string"}, "press_enter": {"type": "boolean"}, "append": {"type": "boolean"}}, "required": ["id", "text"]}},
    {"name": "select", "description": "Choose an option in the numbered dropdown, by visible label or value.", "parameters": {"type": "object", "properties": {"id": {"type": "integer"}, "option": {"type": "string"}}, "required": ["id", "option"]}},
    {"name": "press", "description": "Press a keyboard key on the focused element (Enter, Tab, Escape, ArrowDown, PageDown...).", "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "scroll", "description": "Scroll the page down or up by one screen, or scroll a numbered element into view.", "parameters": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["down", "up"]}, "id": {"type": "integer"}}}},
    {"name": "back", "description": "Go back one page.", "parameters": {"type": "object", "properties": {}}},
    {"name": "wait", "description": "Wait for the page to settle (max 10 s).", "parameters": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "look", "description": "Get a screenshot of the current view with the element numbers drawn on it. Use when the text list is ambiguous or the layout matters.", "parameters": {"type": "object", "properties": {}}},
    {"name": "extract", "description": "Return the FULL visible text of the page (the observation only shows an excerpt). Use before answering questions about page content.", "parameters": {"type": "object", "properties": {"note": {"type": "string", "description": "What you are looking for."}}}},
    {"name": "done", "description": "Finish: the task is complete or the question is answered. `answer` must quote the evidence you saw on the page.", "parameters": {"type": "object", "properties": {"answer": {"type": "string"}, "evidence": {"type": "string", "description": "Exact text from the page that proves it."}}, "required": ["answer"]}},
    {"name": "fail", "description": "Give up with a precise reason (what you tried, what blocked you).", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "ask_owner", "description": "You need information only the owner has (a password, a choice, a missing detail). Stops the task with the question.", "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
]

SYSTEM = """You are the browser hand of a business operations desk. You operate a real web browser to complete ONE task precisely.

Each turn you receive an OBSERVATION: the page URL and title, a numbered list of the interactive elements you can act on, and an excerpt of the page text. Act by calling exactly ONE tool per turn, always by element number.

Rules
- Read the observation before acting. If an element you need is not listed, scroll, or use `look` for a screenshot.
- Prefer the most specific element (a labelled textbox over a generic one). Never invent numbers.
- Typing into a field replaces its content. Check the `value=` shown afterwards to confirm it took.
- After an action, check that the page actually changed the way you expected before moving on. If nothing changed twice in a row, try a different element or `look`.
- Use `extract` to read the full page before answering any question about its content. Quote what you saw in `done`.
- Anything that sends, pays, posts, deletes, books or confirms is a submit. The desk will stop you at the submit if the owner has not pre-approved it; that is expected - just attempt it once you have everything filled in and verified.
- Never try to solve a CAPTCHA or "verify you are human" check; call `fail` and say so.
- Never enter a password or payment details unless they were given to you in the task text.
- Stay on the task. Do not browse elsewhere, do not accept cookie banners unless they block the page (then choose the most restrictive option).
- Finish with `done` (with evidence) or `fail` (with the exact blocker). Be concise."""


# ------------------------------------------------------------------------------------------------ data
@dataclass
class Observation:
    url: str
    title: str
    elements: list[dict[str, Any]]
    text: str
    scroll_y: int
    scroll_h: int
    view_h: int
    frames: int = 0

    def digest(self) -> str:
        key = self.url + "|" + "|".join(f"{e['role']}:{e['name']}:{e['value']}:{int(e['checked'])}" for e in self.elements[:200]) + "|" + self.text[:3000]
        return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]

    def render(self, max_elements: int = 140, text_chars: int = 1800) -> str:
        els = [e for e in self.elements if e["inView"]] + [e for e in self.elements if not e["inView"]]
        lines = []
        for e in els[:max_elements]:
            bits = [f"[{e['id']}] {e['role']}"]
            if e["type"] and e["role"] in ("textbox", "generic") and e["type"] not in ("text",):
                bits[0] += f"({e['type']})"
            if e["name"]:
                bits.append(json.dumps(e["name"], ensure_ascii=False))
            if e["placeholder"] and e["placeholder"] != e["name"]:
                bits.append(f"placeholder={json.dumps(e['placeholder'], ensure_ascii=False)}")
            if e["value"] and e["role"] not in ("checkbox", "radio"):
                bits.append(f"value={json.dumps(e['value'], ensure_ascii=False)}")
            if e.get("options"):
                bits.append(f"options=[{e['options'][:160]}]")
            if e["checked"]:
                bits.append("checked")
            if e["disabled"]:
                bits.append("disabled")
            if e["href"] and e["role"] == "link":
                bits.append("-> " + e["href"][:90])
            if not e["inView"]:
                bits.append("(off-screen)")
            lines.append(" ".join(bits))
        more = len(els) - min(len(els), max_elements)
        pos = f"scroll {self.scroll_y}/{max(0, self.scroll_h - self.view_h)}px" + (" (bottom)" if self.scroll_y + self.view_h >= self.scroll_h - 4 else "")
        txt = self.text[:text_chars] + (" …[use extract for the full text]" if len(self.text) > text_chars else "")
        return (f"URL: {self.url}\nTITLE: {self.title}\n{pos}" + (f" · {self.frames} iframe(s) not inspected" if self.frames else "") +
                f"\n\nELEMENTS ({len(els)}):\n" + "\n".join(lines) + (f"\n… {more} more (scroll to reveal)" if more > 0 else "") +
                f"\n\nTEXT EXCERPT:\n{txt}")


@dataclass
class Step:
    n: int
    action: str
    args: dict[str, Any]
    result: str
    url: str
    ok: bool
    changed: bool
    selector: dict[str, Any] | None = None
    ms: int = 0


@dataclass
class Result:
    status: str                              # done | failed | needs_approval | blocked_captcha | needs_input | budget
    answer: str = ""
    steps: list[Step] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    pending: dict[str, Any] | None = None    # what was about to happen when we stopped for approval
    macro: dict[str, Any] | None = None
    final_url: str = ""
    screenshots: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = {"done": "DONE", "failed": "FAILED", "needs_approval": "STOPPED BEFORE SUBMIT (needs owner approval)",
                "blocked_captcha": "BLOCKED BY CAPTCHA", "needs_input": "NEEDS OWNER INPUT", "budget": "STOPPED: budget reached"}.get(self.status, self.status)
        body = self.answer.strip()
        if self.pending:
            body += f"\nPending action: {self.pending.get('description', '')}"
        return f"{head}. {body}\n({len(self.steps)} steps, {self.tokens_in:,} in / {self.tokens_out:,} out tokens, last URL {self.final_url})"


class SubmitBlocked(Exception):
    def __init__(self, description: str, action: dict[str, Any]):
        super().__init__(description)
        self.description = description
        self.action = action


class CaptchaWall(Exception):
    pass


# ------------------------------------------------------------------------------------------------ the hand
class BrowserHand:
    """One Chromium tab with a persistent profile. All methods run in the creating thread."""

    def __init__(self, profile: str = "default", headless: bool = True, allow_domains: list[str] | None = None,
                 viewport: tuple[int, int] = (1280, 900), record: bool = True):
        from playwright.sync_api import sync_playwright
        self.profile_dir = BROWSER_DIR / "profiles" / re.sub(r"[^\w.-]+", "_", profile)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.allow_domains = [d.lower().lstrip(".") for d in (allow_domains or []) if d]
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), headless=headless, viewport={"width": viewport[0], "height": viewport[1]},
            locale="en-GB", timezone_id="Europe/London",
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            args=["--disable-blink-features=AutomationControlled"], ignore_https_errors=False)
        self.ctx.set_default_timeout(ACTION_TIMEOUT_MS)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.on("dialog", lambda d: d.dismiss())
        self.ctx.on("page", self._on_new_page)
        self.record = record
        self.macro_steps: list[dict[str, Any]] = []
        self.last_obs: Observation | None = None
        self.allow_submit = False

    def _on_new_page(self, page) -> None:                      # links that open a new tab: follow them
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        self.page = page

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            self.ctx.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass

    # ---------------------------------------------------------------- observation
    def _check_domain(self, url: str) -> None:
        if not self.allow_domains:
            return
        if url.startswith(("file:", "about:")):
            return
        host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()
        if not any(host == d or host.endswith("." + d) for d in self.allow_domains):
            raise ValueError(f"navigation to {host} is outside the allowed domains {self.allow_domains}")

    def settle(self, ms: int = 600) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=2500)
        except Exception:
            pass
        self.page.wait_for_timeout(ms)

    def observe(self) -> Observation:
        for _ in range(3):
            try:
                raw = self.page.evaluate(SNAPSHOT_JS)
                break
            except Exception as exc:                                # page navigating mid-snapshot
                if "Execution context was destroyed" in str(exc) or "navigation" in str(exc).lower():
                    self.settle(400)
                    continue
                raise
        else:
            raw = {"url": self.page.url, "title": "", "elements": [], "text": "", "scrollY": 0, "scrollH": 0, "viewH": 0, "frames": 0}
        obs = Observation(raw["url"], raw.get("title", ""), raw.get("elements", []), raw.get("text", ""), raw.get("scrollY", 0), raw.get("scrollH", 0), raw.get("viewH", 0), raw.get("frames", 0))
        self.last_obs = obs
        if CAPTCHA_RE.search(obs.text[:4000]) or CAPTCHA_RE.search(obs.title) or self.page.locator("iframe[src*='captcha'], iframe[src*='challenge']").count() > 0:
            raise CaptchaWall(f"captcha / human-verification wall at {obs.url}")
        return obs

    def screenshot(self, marks: bool = True, path: Path | None = None) -> bytes:
        ids = [e["id"] for e in (self.last_obs.elements if self.last_obs else []) if e["inView"]][:120]
        if marks and ids:
            try:
                self.page.evaluate(MARKS_JS, ids)
            except Exception:
                pass
        try:
            data = self.page.screenshot(type="jpeg", quality=62, full_page=False)
        finally:
            if marks:
                try:
                    self.page.evaluate(UNMARK_JS)
                except Exception:
                    pass
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return data

    # ---------------------------------------------------------------- elements & selectors
    def _el(self, id_: int) -> dict[str, Any]:
        if not self.last_obs:
            self.observe()
        for e in self.last_obs.elements:                        # type: ignore[union-attr]
            if e["id"] == int(id_):
                return e
        raise ValueError(f"no element [{id_}] in the current observation - take a fresh look and use a listed number")

    def _loc(self, e: dict[str, Any]):
        return self.page.locator(f'[data-atlas-id="{e["id"]}"]').first

    @staticmethod
    def selector_bundle(e: dict[str, Any]) -> dict[str, Any]:
        return {k: e.get(k, "") for k in ("role", "name", "placeholder", "label", "aria", "domId", "nameAttr", "testid", "css", "tag", "type")}

    def resolve(self, sel: dict[str, Any]):
        """Find an element again from a recorded selector bundle: most stable strategy first."""
        p = self.page
        cands = []
        if sel.get("testid"):
            cands.append(p.get_by_test_id(sel["testid"]))
        if sel.get("domId"):
            cands.append(p.locator("#" + re.sub(r"([^\w-])", r"\\\1", sel["domId"])))
        if sel.get("aria"):
            cands.append(p.get_by_label(sel["aria"], exact=True))
        if sel.get("role") in ("button", "link", "textbox", "checkbox", "radio", "combobox", "tab", "menuitem", "option", "searchbox", "switch") and sel.get("name"):
            cands.append(p.get_by_role(sel["role"], name=sel["name"], exact=True))
            cands.append(p.get_by_role(sel["role"], name=sel["name"]))
        if sel.get("placeholder"):
            cands.append(p.get_by_placeholder(sel["placeholder"], exact=True))
        if sel.get("label"):
            cands.append(p.get_by_label(sel["label"], exact=True))
        if sel.get("nameAttr") and sel.get("tag") in ("input", "textarea", "select"):
            cands.append(p.locator(f'{sel["tag"]}[name="{sel["nameAttr"]}"]'))
        if sel.get("name") and sel.get("role") in ("button", "link"):
            cands.append(p.get_by_text(sel["name"], exact=True))
        if sel.get("css"):
            cands.append(p.locator(sel["css"]))
        for c in cands:
            try:
                if c.count() >= 1 and c.first.is_visible():
                    return c.first
            except Exception:
                continue
        raise LookupError(f"element not found: {sel.get('role')} {sel.get('name')!r}")

    # ---------------------------------------------------------------- actions
    def _is_submit(self, e: dict[str, Any]) -> bool:
        if e["role"] == "button" and (SUBMIT_RE.search(e["name"] or "") or e["type"] == "submit"):
            return True
        if e["role"] == "link" and SUBMIT_RE.search(e["name"] or "") and not SEARCH_RE.search(e["name"] or ""):
            return True
        return False

    def _guard_submit(self, description: str, action: dict[str, Any]) -> None:
        if not self.allow_submit:
            raise SubmitBlocked(description, action)

    def _record(self, action: dict[str, Any], e: dict[str, Any] | None) -> None:
        if not self.record:
            return
        rec = dict(action)
        if e is not None:
            rec["selector"] = self.selector_bundle(e)
        self.macro_steps.append(rec)

    def act(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """Execute one action. Returns (result text, element used). Raises SubmitBlocked / CaptchaWall / ValueError."""
        p = self.page
        if name == "navigate":
            url = str(args.get("url", "")).strip()
            if not re.match(r"^(https?|file|about):", url):
                url = "https://" + url
            self._check_domain(url)
            p.goto(url, wait_until="domcontentloaded")
            self.settle()
            self._record({"action": "navigate", "url": url}, None)
            return f"opened {p.url}", None
        if name == "click":
            e = self._el(args.get("id"))
            if e["disabled"]:
                return f"[{e['id']}] is disabled - nothing happened", e
            if self._is_submit(e):
                self._guard_submit(f"click {e['role']} \"{e['name']}\" on {p.url}", {"action": "click", "selector": self.selector_bundle(e)})
            loc = self._loc(e)
            try:
                loc.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            with p.expect_navigation(timeout=1500) if False else _nullcontext():
                loc.click(timeout=ACTION_TIMEOUT_MS)
            self.settle()
            self._record({"action": "click"}, e)
            return f"clicked [{e['id']}] {e['role']} \"{e['name']}\"", e
        if name == "type":
            e = self._el(args.get("id"))
            text = str(args.get("text", ""))
            append = bool(args.get("append"))
            enter = bool(args.get("press_enter"))
            if enter and e["role"] not in ("searchbox",) and not SEARCH_RE.search((e["name"] or "") + " " + (e["placeholder"] or "")):
                self._guard_submit(f"type {text[:80]!r} into \"{e['name'] or e['placeholder']}\" and press Enter on {p.url}",
                                   {"action": "type", "selector": self.selector_bundle(e), "text": text, "press_enter": True})
            loc = self._loc(e)
            loc.scroll_into_view_if_needed(timeout=3000)
            if e.get("editable"):
                loc.click()
                if not append:
                    p.keyboard.press("Control+A")
                    p.keyboard.press("Delete")
                p.keyboard.type(text, delay=8)
            else:
                if append:
                    loc.click()
                    p.keyboard.press("End")
                    p.keyboard.type(text, delay=8)
                else:
                    loc.fill(text)
            if enter:
                p.keyboard.press("Enter")
            self.settle(400)
            self._record({"action": "type", "text": text, "press_enter": enter, "append": append}, e)
            try:
                val = loc.input_value(timeout=1000) if not e.get("editable") else loc.inner_text(timeout=1000)
            except Exception:
                val = ""
            return f"typed into [{e['id']}] \"{e['name'] or e['placeholder']}\"" + (f" (value now {val[:60]!r})" if val else "") + (" and pressed Enter" if enter else ""), e
        if name == "select":
            e = self._el(args.get("id"))
            opt = str(args.get("option", ""))
            loc = self._loc(e)
            try:
                loc.select_option(label=opt)
            except Exception:
                loc.select_option(value=opt)
            self.settle(300)
            self._record({"action": "select", "option": opt}, e)
            return f"selected {opt!r} in [{e['id']}] \"{e['name']}\"", e
        if name == "press":
            key = str(args.get("key", "Enter"))
            if key.lower() == "enter":
                try:
                    focused = p.evaluate("() => { const a = document.activeElement; return a ? {tag: a.tagName.toLowerCase(), id: a.getAttribute('data-atlas-id'), type: a.type || '', name: a.getAttribute('aria-label') || a.placeholder || a.name || ''} : null }")
                except Exception:
                    focused = None
                if focused and focused.get("tag") in ("input", "textarea", "div") and not SEARCH_RE.search(str(focused.get("name", "")) + " " + str(focused.get("type", ""))):
                    self._guard_submit(f"press Enter in \"{focused.get('name')}\" on {p.url}", {"action": "press", "key": "Enter"})
            p.keyboard.press(key)
            self.settle(400)
            self._record({"action": "press", "key": key}, None)
            return f"pressed {key}", None
        if name == "scroll":
            if args.get("id"):
                e = self._el(args["id"])
                self._loc(e).scroll_into_view_if_needed()
                p.wait_for_timeout(300)
                self._record({"action": "scroll"}, e)
                return f"scrolled [{e['id']}] into view", e
            direction = str(args.get("direction", "down"))
            p.mouse.wheel(0, (p.viewport_size or {"height": 800})["height"] * (0.85 if direction == "down" else -0.85))
            p.wait_for_timeout(350)
            self._record({"action": "scroll", "direction": direction}, None)
            return f"scrolled {direction}", None
        if name == "back":
            p.go_back(wait_until="domcontentloaded")
            self.settle()
            self._record({"action": "back"}, None)
            return "went back", None
        if name == "wait":
            s = min(10.0, max(0.2, float(args.get("seconds", 2))))
            p.wait_for_timeout(int(s * 1000))
            self.settle(200)
            return f"waited {s:g}s", None
        raise ValueError(f"unknown action {name}")

    # ---------------------------------------------------------------- macros
    def replay(self, macro: dict[str, Any], vars_: dict[str, Any] | None = None, on_step: Callable[[int, str], None] | None = None) -> int:
        """Run a recorded macro without a model. Returns the number of steps executed; raises on the failing step
        (LookupError / SubmitBlocked / CaptchaWall) so the caller can hand over to the agent from there."""
        v = {k: str(val) for k, val in (vars_ or {}).items()}
        sub = lambda s: re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: v.get(m.group(1), m.group(0)), s or "")
        done = 0
        for i, st in enumerate(macro.get("steps", [])):
            a = st.get("action")
            if a == "navigate":
                url = sub(st["url"])
                self._check_domain(url)
                self.page.goto(url, wait_until="domcontentloaded")
                self.settle()
            elif a in ("click", "type", "select", "scroll") and st.get("selector"):
                loc = self.resolve(st["selector"])
                sel = st["selector"]
                if a == "click":
                    if sel.get("role") == "button" and (SUBMIT_RE.search(sel.get("name") or "") or sel.get("type") == "submit"):
                        self._guard_submit(f"click \"{sel.get('name')}\" on {self.page.url}", {"action": "click", "selector": sel})
                    loc.click()
                    self.settle()
                elif a == "type":
                    text = sub(st.get("text", ""))
                    if st.get("press_enter") and not SEARCH_RE.search((sel.get("name") or "") + " " + (sel.get("placeholder") or "")):
                        self._guard_submit(f"type {text[:80]!r} into \"{sel.get('name')}\" and press Enter", {"action": "type", "selector": sel, "text": text, "press_enter": True})
                    if sel.get("tag") not in ("input", "textarea"):
                        loc.click()
                        self.page.keyboard.press("Control+A")
                        self.page.keyboard.type(text, delay=8)
                    elif st.get("append"):
                        loc.click()
                        self.page.keyboard.press("End")
                        self.page.keyboard.type(text, delay=8)
                    else:
                        loc.fill(text)
                    if st.get("press_enter"):
                        self.page.keyboard.press("Enter")
                    self.settle(400)
                elif a == "select":
                    try:
                        loc.select_option(label=sub(st.get("option", "")))
                    except Exception:
                        loc.select_option(value=sub(st.get("option", "")))
                elif a == "scroll":
                    loc.scroll_into_view_if_needed()
            elif a == "scroll":
                self.page.mouse.wheel(0, 700 if st.get("direction", "down") == "down" else -700)
                self.page.wait_for_timeout(300)
            elif a == "press":
                self.page.keyboard.press(st.get("key", "Enter"))
                self.settle(300)
            elif a == "back":
                self.page.go_back(wait_until="domcontentloaded")
                self.settle()
            done = i + 1
            if on_step:
                on_step(i, a)
        return done

    def macro(self, name: str, task: str, start_url: str) -> dict[str, Any]:
        return {"name": name, "task": task, "start_url": start_url, "created": time.time(), "steps": list(self.macro_steps)}


class _nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def save_macro(m: dict[str, Any]) -> Path:
    MACRO_DIR.mkdir(parents=True, exist_ok=True)
    p = MACRO_DIR / (re.sub(r"[^\w.-]+", "_", m["name"]) + ".json")
    p.write_text(json.dumps(m, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def load_macro(name: str) -> dict[str, Any] | None:
    p = MACRO_DIR / (re.sub(r"[^\w.-]+", "_", name) + ".json")
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


# ------------------------------------------------------------------------------------------------ the agent loop
def _image_message(provider, text: str, jpeg: bytes) -> Any:
    b64 = base64.b64encode(jpeg).decode()
    if provider.__class__.__name__.startswith("Anthropic"):
        return {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}, {"type": "text", "text": text}]}
    return {"role": "user", "content": [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}


def run_task(task: str, url: str = "", provider=None, model: str = "", allow_submit: bool = False, profile: str = "default",
             headless: bool = True, max_steps: int = MAX_STEPS, max_input_tokens: int = MAX_INPUT_TOKENS,
             allow_domains: list[str] | None = None, macro: dict[str, Any] | None = None, vars_: dict[str, Any] | None = None,
             record_name: str = "", on_event: Callable[[str, str], None] | None = None, shots_dir: Path | None = None,
             hand: BrowserHand | None = None) -> Result:
    """Run one browser task with a model. `macro` (recorded earlier) is replayed first and the model only takes over
    where it stops. Returns a Result; never raises for task-level problems."""
    from .providers import ToolCall  # noqa: F401  (type only)

    ev = on_event or (lambda kind, text: None)
    own = hand is None
    hand = hand or BrowserHand(profile=profile, headless=headless, allow_domains=allow_domains)
    hand.allow_submit = allow_submit
    res = Result(status="failed")
    model = model or DEFAULT_MODEL
    shots_dir = shots_dir or (BROWSER_DIR / "shots" / time.strftime("%Y%m%d-%H%M%S"))
    t_start = time.time()
    try:
        # ---- 1. replay what we already know --------------------------------------------------------------------
        replayed = 0
        replay_note = ""
        if macro and macro.get("steps"):
            try:
                replayed = hand.replay(macro, vars_, on_step=lambda i, a: ev("tool", f"macro step {i + 1}: {a}"))
                replay_note = f"A recorded macro already ran {replayed} step(s) successfully; continue from the current page."
            except SubmitBlocked as sb:
                res.status, res.answer = "needs_approval", f"Macro reached the submit step: {sb.description}"
                res.pending = {"description": sb.description, "action": sb.action, "macro_steps": macro["steps"], "url": hand.page.url}
                res.final_url = hand.page.url
                return res
            except CaptchaWall as cw:
                res.status, res.answer, res.final_url = "blocked_captcha", str(cw), hand.page.url
                return res
            except Exception as exc:
                replay_note = f"A recorded macro ran {replayed} step(s) then failed at step {replayed + 1} ({type(exc).__name__}: {str(exc)[:160]}). Continue the task from the current page and do not repeat what is already done."
                ev("log", replay_note)
        elif url:
            try:
                hand.act("navigate", {"url": url})
            except CaptchaWall as cw:
                res.status, res.answer, res.final_url = "blocked_captcha", str(cw), hand.page.url
                return res
            except Exception as exc:
                res.answer = f"could not open {url}: {type(exc).__name__}: {str(exc)[:200]}"
                res.final_url = hand.page.url
                return res

        # ---- 2. model loop ----------------------------------------------------------------------------------------
        if provider is None:
            from . import config as C, providers as P
            pool = P.ProviderPool(C.load("providers", C.DEFAULT_PROVIDERS))
            provider = pool.get(DEFAULT_PROVIDER)
        try:
            obs = hand.observe()
        except CaptchaWall as cw:
            res.status, res.answer, res.final_url = "blocked_captcha", str(cw), hand.page.url
            return res
        intro = f"TASK: {task}\n" + (f"START URL: {url}\n" if url else "") + (f"NOTE: {replay_note}\n" if replay_note else "") + \
                ("SUBMITS ARE PRE-APPROVED for this task.\n" if allow_submit else "SUBMITS ARE NOT PRE-APPROVED: fill and verify everything, then attempt the submit once; the desk will queue it.\n") + \
                "\nOBSERVATION:\n" + obs.render()
        messages: list[Any] = [provider.user_message(intro)]
        last_digest = obs.digest()
        same_count = 0
        nudges = 0
        want_shot = False
        for n in range(1, max_steps + 1):
            try:
                resp = provider.chat(SYSTEM, messages, TOOLS, model)
            except Exception as exc:
                res.answer = f"model error: {type(exc).__name__}: {str(exc)[:200]}"
                break
            res.tokens_in += resp.input_tokens
            res.tokens_out += resp.output_tokens
            if res.tokens_in > max_input_tokens:
                res.status, res.answer = "budget", f"input-token budget {max_input_tokens:,} reached after {n - 1} steps"
                break
            if not resp.tool_calls:
                nudges += 1
                if nudges > 2:
                    res.answer = "the model stopped calling tools: " + (resp.text or "")[:300]
                    break
                messages.append(resp.assistant_message if resp.assistant_message else {"role": "assistant", "content": resp.text or ""})
                messages.append(provider.user_message("Reply with exactly one tool call (done/fail if finished)."))
                continue
            nudges = 0
            call = resp.tool_calls[0]
            extra = [(c.id, c.name, "skipped: one action per turn - re-issue after seeing the result", True) for c in resp.tool_calls[1:]]
            name, args = call.name, call.args or {}
            ev("tool", f"browser {name} {json.dumps({k: v for k, v in args.items() if k != 'text'} | ({'text': str(args.get('text'))[:60]} if 'text' in args else {}), ensure_ascii=False)}")
            t0 = time.time()
            out, el, ok, changed = "", None, True, False
            if name == "done":
                res.status = "done"
                res.answer = str(args.get("answer", "")).strip() + (f"\nEvidence: {args.get('evidence')}" if args.get("evidence") else "")
                res.steps.append(Step(n, name, args, "done", hand.page.url, True, False))
                break
            if name == "fail":
                res.status, res.answer = "failed", str(args.get("reason", "")).strip()
                res.steps.append(Step(n, name, args, "fail", hand.page.url, True, False))
                break
            if name == "ask_owner":
                res.status, res.answer = "needs_input", str(args.get("question", "")).strip()
                res.steps.append(Step(n, name, args, "ask_owner", hand.page.url, True, False))
                break
            try:
                if name == "look":
                    want_shot, out = True, "screenshot attached to the next observation"
                elif name == "extract":
                    full = hand.observe().text
                    out = "FULL PAGE TEXT (" + str(len(full)) + " chars):\n" + full[:14000] + ("\n…[truncated]" if len(full) > 14000 else "")
                else:
                    out, el = hand.act(name, args)
            except SubmitBlocked as sb:
                res.status, res.answer = "needs_approval", f"Ready to submit; stopped for approval: {sb.description}"
                res.pending = {"description": sb.description, "action": sb.action, "macro_steps": list(hand.macro_steps), "url": hand.page.url}
                res.steps.append(Step(n, name, args, "blocked: " + sb.description, hand.page.url, False, False))
                try:
                    res.screenshots.append(str(shots_dir / f"{n:02d}-pending.jpg"))
                    hand.screenshot(marks=False, path=shots_dir / f"{n:02d}-pending.jpg")
                except Exception:
                    pass
                break
            except CaptchaWall as cw:
                res.status, res.answer = "blocked_captcha", str(cw)
                res.steps.append(Step(n, name, args, str(cw), hand.page.url, False, False))
                break
            except Exception as exc:
                ok, out = False, f"ERROR: {type(exc).__name__}: {str(exc)[:220]}"
            # observe after the action
            try:
                obs = hand.observe()
            except CaptchaWall as cw:
                res.status, res.answer = "blocked_captcha", str(cw)
                break
            d = obs.digest()
            changed = d != last_digest
            last_digest = d
            same_count = 0 if changed else same_count + 1
            res.steps.append(Step(n, name, args, out[:300], obs.url, ok, changed, hand.selector_bundle(el) if el else None, int((time.time() - t0) * 1000)))
            if name == "extract":
                fb = out
            else:
                fb = ("RESULT: " + out + ("" if changed or name in ("look", "wait") else "  (page did NOT change)") +
                      (f"\nNOTE: nothing has changed for {same_count} actions - try another element, scroll, or `look`." if same_count >= 2 else "") +
                      "\n\nOBSERVATION:\n" + obs.render())
            messages.append(resp.assistant_message)
            messages.extend(provider.tool_results([(call.id, name, fb, not ok)] + extra))
            if want_shot or same_count >= 2:
                want_shot = False
                try:
                    jpeg = hand.screenshot(marks=True, path=shots_dir / f"{n:02d}.jpg")
                    res.screenshots.append(str(shots_dir / f"{n:02d}.jpg"))
                    messages.append(_image_message(provider, "Screenshot of the current view; the red numbers are the element ids from the list.", jpeg))
                except Exception as exc:
                    messages.append(provider.user_message(f"(screenshot unavailable: {str(exc)[:100]})"))
        else:
            res.status, res.answer = "budget", f"stopped after {max_steps} steps without finishing"
        res.final_url = hand.page.url
        if record_name and res.status in ("done", "needs_approval"):
            res.macro = hand.macro(record_name, task, url)
            save_macro(res.macro)
        elif res.status in ("done", "needs_approval"):
            res.macro = hand.macro(record_name or "last", task, url)
        return res
    finally:
        ev("log", f"browser task {res.status} in {time.time() - t_start:.1f}s, {len(res.steps)} steps")
        if own:
            hand.close()


def perform_pending(pending: dict[str, Any], profile: str = "default", headless: bool = True) -> str:
    """Owner approved: rebuild the page state by replaying the recorded steps, then do the blocked submit."""
    hand = BrowserHand(profile=profile, headless=headless, record=False)
    hand.allow_submit = True
    try:
        steps = list(pending.get("macro_steps") or [])
        if steps:
            hand.replay({"steps": steps})
        act = pending.get("action") or {}
        a = act.get("action")
        try:
            before = set(hand.observe().text.splitlines())
        except CaptchaWall as cw:
            return f"could not rebuild the page - captcha wall: {cw}"
        if a in ("click", "type") and act.get("selector"):
            loc = hand.resolve(act["selector"])
            if a == "click":
                loc.click()
            else:
                if act["selector"].get("tag") in ("input", "textarea"):
                    loc.fill(str(act.get("text", "")))
                else:
                    loc.click()
                    hand.page.keyboard.press("Control+A")
                    hand.page.keyboard.type(str(act.get("text", "")), delay=8)
                if act.get("press_enter"):
                    hand.page.keyboard.press("Enter")
            hand.settle()
        elif a == "press":
            hand.page.keyboard.press(str(act.get("key", "Enter")))
            hand.settle()
        else:
            return f"nothing to perform for action {a!r}"
        try:
            after = hand.observe().text
        except CaptchaWall as cw:
            return f"submitted, then hit a captcha wall: {cw}"
        new_lines = [ln.strip() for ln in after.splitlines() if ln.strip() and ln.strip() not in before]
        change = " | ".join(new_lines)[:300] if new_lines else after[:200].replace("\n", " ")
        return f"performed {pending.get('description', a)} - now at {hand.page.url} - page now shows: {change}"
    finally:
        hand.close()


# ------------------------------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="atlas.browser", description="Browser hand: run a task, replay a macro, or log in to a site interactively.")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="run a task with the model")
    r.add_argument("task")
    r.add_argument("--url", default="")
    r.add_argument("--allow-submit", action="store_true")
    r.add_argument("--model", default="")
    r.add_argument("--provider", default="")
    r.add_argument("--profile", default="default")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--record", default="", help="save the successful run as a macro with this name")
    r.add_argument("--macro", default="", help="replay this macro first, model continues from where it stops")
    r.add_argument("--var", action="append", default=[], help="k=v substitutions for macro text")
    r.add_argument("--domains", default="", help="comma-separated allowed domains")
    r.add_argument("--max-steps", type=int, default=MAX_STEPS)
    lg = sub.add_parser("login", help="open a headed browser on the profile so you can log in; press Enter here when done")
    lg.add_argument("url")
    lg.add_argument("--profile", default="default")
    rp = sub.add_parser("replay", help="replay a macro without a model")
    rp.add_argument("name")
    rp.add_argument("--profile", default="default")
    rp.add_argument("--allow-submit", action="store_true")
    rp.add_argument("--var", action="append", default=[])
    rp.add_argument("--headed", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "login":
        hand = BrowserHand(profile=a.profile, headless=False)
        hand.page.goto(a.url)
        print(f"Browser open on {a.url}. Log in there, then press Enter here to save the session.")
        try:
            sys.stdin.readline()
        finally:
            hand.close()
        print("session saved in profile", a.profile)
        return 0
    if a.cmd == "replay":
        m = load_macro(a.name)
        if not m:
            print("no such macro", a.name)
            return 2
        hand = BrowserHand(profile=a.profile, headless=not a.headed, record=False)
        hand.allow_submit = a.allow_submit
        try:
            n = hand.replay(m, dict(kv.split("=", 1) for kv in a.var), on_step=lambda i, act: print(f"  step {i + 1}: {act}"))
            print(f"replayed {n} steps · now at {hand.page.url}")
        except SubmitBlocked as sb:
            print("stopped before submit (use --allow-submit):", sb.description)
        finally:
            hand.close()
        return 0
    if a.cmd == "run":
        from . import config as C, providers as P
        pool = P.ProviderPool(C.load("providers", C.DEFAULT_PROVIDERS))
        provider = pool.get(a.provider or DEFAULT_PROVIDER)
        res = run_task(a.task, a.url, provider, a.model, allow_submit=a.allow_submit, profile=a.profile, headless=not a.headed,
                       max_steps=a.max_steps, allow_domains=[d for d in a.domains.split(",") if d], macro=load_macro(a.macro) if a.macro else None,
                       vars_=dict(kv.split("=", 1) for kv in a.var), record_name=a.record,
                       on_event=lambda k, t: print(f"  [{k}] {t}"))
        print()
        print(res.summary())
        for s in res.steps:
            print(f"  {s.n:02d} {s.action:8s} {'ok ' if s.ok else 'ERR'} {'~' if s.changed else '='} {s.result[:110]}")
        return 0 if res.status in ("done", "needs_approval") else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
