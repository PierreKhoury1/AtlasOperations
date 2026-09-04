"""Browser hand: element indexing, scripted agent loop, submit gate + approval hand-off, macro replay, captcha stop.
Runs a real headless Chromium against the static site in tests/browser_site (file://). Skipped if Playwright is missing."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from atlas import browser as B                      # noqa: E402
from atlas.providers import LLMResponse, Provider, ToolCall  # noqa: E402

SITE = Path(__file__).parent / "browser_site"
URL = SITE.resolve().as_uri()


def idof(obs: str, pattern: str) -> int:
    """Element id whose line matches `pattern` in a rendered observation."""
    for line in obs.splitlines():
        m = re.match(r"\[(\d+)\] (.*)", line)
        if m and re.search(pattern, m.group(2)):
            return int(m.group(1))
    raise AssertionError(f"no element matching {pattern!r} in:\n{obs}")


class FakeProvider(Provider):
    """Replays a script of tool calls; each entry is (name, args) or (name, fn(last_observation_text) -> args)."""
    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.default_model = "fake"
        self.seen: list[str] = []

    def _last_obs(self, messages) -> str:
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") in ("user", "tool"):
                c = m.get("content")
                if isinstance(c, list):
                    c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
                if isinstance(c, str) and ("OBSERVATION:" in c or "FULL PAGE TEXT" in c or "TASK:" in c):
                    return c
        return ""

    def chat(self, system, messages, tools, model="", on_token=None):
        obs = self._last_obs(messages)
        self.seen.append(obs)
        if not self.script:
            name, args = "fail", {"reason": "script exhausted"}
        else:
            name, args = self.script.pop(0)
            if callable(args):
                args = args(obs)
        return LLMResponse(text="", tool_calls=[ToolCall(id=f"c{len(self.seen)}", name=name, args=args)],
                           assistant_message={"role": "assistant", "content": ""}, input_tokens=10, output_tokens=3, model="fake")

    def tool_results(self, results):
        return [{"role": "tool", "tool_call_id": cid, "name": n, "content": ("ERROR: " + out) if err else out} for cid, n, out, err in results]


@pytest.fixture
def hand(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "BROWSER_DIR", tmp_path / "browser")
    monkeypatch.setattr(B, "MACRO_DIR", tmp_path / "browser" / "macros")
    h = B.BrowserHand(profile="t", headless=True)
    yield h
    h.close()


def test_snapshot_indexes_elements_accurately(hand):
    hand.act("navigate", {"url": URL + "/index.html"})
    obs = hand.observe()
    roles = {(e["role"], e["name"]) for e in obs.elements}
    assert ("searchbox", "Search services") in roles
    assert ("button", "Search") in roles and ("button", "Book call-out") in roles
    assert ("textbox", "Your name") in roles and ("combobox", "Preferred slot") in roles
    assert ("checkbox", "Emergency (no hot water / leak)") in roles
    sel = next(e for e in obs.elements if e["role"] == "combobox")
    assert "Morning" in sel["options"]
    off = next(e for e in obs.elements if e["name"] == "Unsubscribe")
    assert not off["inView"]
    r = obs.render()
    assert 'placeholder="e.g. boiler"' in r and "(off-screen)" in r and "value=" not in r.split("checkbox")[1].split("\n")[0]


def test_scripted_search_task_records_macro(hand, tmp_path):
    prov = FakeProvider([
        ("type", lambda o: {"id": idof(o, r'searchbox "Search services"'), "text": "boiler", "press_enter": True}),
        ("extract", {"note": "price"}),
        ("done", lambda o: {"answer": "Boiler repair from £95", "evidence": re.search(r"Boiler repair[^\n]*", o).group(0)}),
    ])
    res = B.run_task("Find the boiler price", URL + "/index.html", prov, "fake", hand=hand, record_name="boiler_search")
    assert res.status == "done", res.summary()
    assert "£95" in res.answer and "Evidence: Boiler repair from £95" in res.answer
    assert [s.action for s in res.steps] == ["type", "extract", "done"]
    assert res.steps[0].changed is True                     # results appeared
    assert res.macro and [s["action"] for s in res.macro["steps"]] == ["navigate", "type"]
    assert (B.MACRO_DIR / "boiler_search.json").is_file()
    assert res.tokens_in == 30


def test_submit_is_gated_then_performed_after_approval(hand, tmp_path):
    prov = FakeProvider([
        ("type", lambda o: {"id": idof(o, r'textbox "Your name"'), "text": "Priya Nair"}),
        ("type", lambda o: {"id": idof(o, r'textbox\(tel\) "Phone"'), "text": "07700 900123"}),
        ("select", lambda o: {"id": idof(o, r'combobox "Preferred slot"'), "option": "Morning 08:00–12:00"}),
        ("click", lambda o: {"id": idof(o, r'checkbox "Emergency')}),
        ("click", lambda o: {"id": idof(o, r'button "Book call-out"')}),
        ("done", {"answer": "should not get here"}),
    ])
    res = B.run_task("Book a morning emergency call-out for Priya", URL + "/index.html", prov, "fake", hand=hand)
    assert res.status == "needs_approval", res.summary()
    assert res.pending and res.pending["action"]["action"] == "click" and "Book call-out" in res.pending["description"]
    assert [s["action"] for s in res.pending["macro_steps"]] == ["navigate", "type", "type", "select", "click"]
    obs = hand.observe()
    assert "Booked:" not in obs.text                         # nothing was submitted
    # the owner approves -> a fresh hand rebuilds the state and performs the click
    hand.close()
    out = B.perform_pending(res.pending, profile="t2")
    assert "Booked: Priya Nair (07700 900123) morning · EMERGENCY" in out, out


def test_allow_submit_completes(hand):
    hand.allow_submit = True
    prov = FakeProvider([
        ("type", lambda o: {"id": idof(o, r'textbox "Your name"'), "text": "Tom"}),
        ("type", lambda o: {"id": idof(o, r'"Phone"'), "text": "07000"}),
        ("select", lambda o: {"id": idof(o, r'combobox'), "option": "pm"}),
        ("click", lambda o: {"id": idof(o, r'button "Book call-out"')}),
        ("done", lambda o: {"answer": re.search(r"Booked:[^\n]*", o).group(0)}),
    ])
    res = B.run_task("Book", URL + "/index.html", prov, "fake", allow_submit=True, hand=hand)
    assert res.status == "done" and "Booked: Tom (07000) afternoon · ref NG-4812" in res.answer


def test_chat_composer_enter_is_gated(hand):
    prov = FakeProvider([("type", lambda o: {"id": idof(o, r'textbox "Type a message"'), "text": "hello", "press_enter": True}), ("done", {"answer": "x"})])
    res = B.run_task("Send hello in the chat", URL + "/contact.html", prov, "fake", hand=hand)
    assert res.status == "needs_approval" and "press Enter" in res.pending["description"]
    assert "You: hello" not in hand.observe().text


def test_macro_replay_with_vars(hand, tmp_path):
    macro = {"name": "search", "steps": [
        {"action": "navigate", "url": URL + "/index.html"},
        {"action": "type", "text": "{{q}}", "press_enter": True, "selector": {"role": "searchbox", "name": "Search services", "placeholder": "e.g. boiler", "tag": "input", "type": "search", "domId": "q", "css": "#q"}},
    ]}
    n = hand.replay(macro, {"q": "drain"})
    assert n == 2
    assert "Drain unblocking £85" in hand.observe().text
    # a selector that no longer matches raises so the agent can take over
    bad = {"steps": [{"action": "click", "selector": {"role": "button", "name": "Does not exist", "css": "#nope"}}]}
    with pytest.raises(LookupError):
        hand.replay(bad)


def test_captcha_wall_stops_the_agent(hand):
    prov = FakeProvider([("done", {"answer": "x"})])
    res = B.run_task("Read the page", URL + "/captcha.html", prov, "fake", hand=hand)
    assert res.status == "blocked_captcha" and "captcha" in res.answer.lower()


def test_stuck_detection_attaches_screenshot(hand):
    prov = FakeProvider([("scroll", {"direction": "up"}), ("scroll", {"direction": "up"}), ("scroll", {"direction": "up"}), ("done", {"answer": "x"})])
    res = B.run_task("noop", URL + "/prices.html", prov, "fake", hand=hand)
    assert res.status == "done"
    assert any("nothing has changed" in o for o in prov.seen)
    assert res.screenshots and Path(res.screenshots[0]).is_file()
