"""Data never goes raw into an inline event handler. A contact key, camera or memory name with a quote in it used to
close the JS string inside onclick="fn('${esc(x)}')" and run as code with the owner's session (esc() leaves ' alone,
and the browser decodes &quot; / &#39; before the handler runs); a JSON literal from jstr() inside a double-quoted
attribute ended the attribute, so the camera Feed button never parsed.

The rule these checks hold every page to: a value reaches a handler only as ${jsa(v)} (= esc(jstr(v)), one JS string
literal after the browser's attribute decoding) or as esc(JSON.stringify(v)), never spliced into a '...' string,
and a bare ${x} only for numeric ids and indexes. The same goes for handler code built ahead of time (the `on:`
strings of Home's feed, the onclick strings handed to the workspace's row())."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "atlas" / "desk" / "static"
PAGES = sorted(p for p in STATIC.iterdir() if p.suffix in (".html", ".js"))
HANDLER = re.compile(r' on[a-z]+="([^"]*)"')
SINGLE_QUOTED_HANDLER = re.compile(r" on[a-z]+='")
TEMPLATE = re.compile(r"`([^`]*)`")
CALL_START = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$.]*[(=]")      # handler code: fn(...) or x=...
SAFE_ESC = ("esc(JSON.stringify(", "esc(jstr(")
HELPER_CALL = re.compile(r"\$\{\s*([A-Za-z_$][A-Za-z0-9_$.]*)\s*\(")   # ${fn(...)} inside handler code
HANDLER_HELPERS = {"jsa", "esc", "Number"}                       # esc only as esc(JSON.stringify(..)) / esc(jstr(..))


def _handlers(page: Path) -> list[str]:
    return HANDLER.findall(page.read_text(encoding="utf-8"))


def _prebuilt(page: Path) -> list[str]:
    """Template literals that are handler code built ahead of time (they start like a call or an assignment)."""
    return [t for t in TEMPLATE.findall(page.read_text(encoding="utf-8")) if CALL_START.match(t.strip())]


def quoted_interpolations(code: str) -> list[str]:
    """Every ${...} that sits inside a '...' string of handler code: there the value is spliced into a JS string
    literal raw, and a quote in it ends the string."""
    out, quote, i = [], False, 0
    while i < len(code):
        ch = code[i]
        if quote and ch == chr(92):                    # a backslash escape inside the string
            i += 2
            continue
        if ch == "'":
            quote = not quote
        elif quote and code.startswith("${", i):
            out.append(code[max(0, i - 30):i + 40])
        i += 1
    return out


def test_scanner_finds_spliced_values():
    assert quoted_interpolations("homeOpenAgent('${aid}')")
    assert quoted_interpolations("api('/runs/${r.id}/cancel',{method:'POST'})")
    assert quoted_interpolations("x=1;go('a');fn('${v}')")
    assert not quoted_interpolations("homeOpenAgent(${jsa(aid)})")
    assert not quoted_interpolations("api(${jsa('/runs/' + r.id + '/cancel')},{method:'POST',body:{}})")
    assert not quoted_interpolations("${le.snapshot_url?`zoom(${jsa(le.snapshot_url)})`:''}")
    assert not quoted_interpolations("decide(${a.id},'approved')")


def test_every_page_is_scanned():
    names = {p.name for p in PAGES}
    assert {"index.html", "objects.html", "workspace.js", "studio.js", "ops.html", "review.html", "shell.js", "cyber.js",
            "theatre.html", "workspace.html"} <= names


def test_no_values_spliced_into_handler_strings():
    for page in PAGES:
        bad = [q for h in _handlers(page) for q in quoted_interpolations(h)]
        bad += [q for t in _prebuilt(page) for q in quoted_interpolations(t)]
        assert not bad, (page.name, bad[:3])


def test_no_html_escaped_strings_inside_handlers():
    for page in PAGES:
        bad = [h for h in _handlers(page)
               if "${esc(" in h and h.count("${esc(") != sum(h.count("${" + s) for s in SAFE_ESC)]
        assert not bad, (page.name, bad[:3])


def test_handler_values_use_only_the_known_helpers():
    """A value in handler code goes through jsa() (or esc(JSON.stringify()), or Number() for an id), never a home-made
    escaper. The workspace had jsq() = JSON.stringify(v) with only " turned into &quot;: it leaves &, so a value
    holding "&quot;" decodes back into a quote and closes the string (see test_jsa_round_trips_hostile_values)."""
    assert HELPER_CALL.findall('send(${jsq(s)})') == ["jsq"]
    assert HELPER_CALL.findall("inspect(${jsa(id)})") == ["jsa"]
    for page in PAGES:
        used = {m for code in _handlers(page) + _prebuilt(page) for m in HELPER_CALL.findall(code)}
        assert not used - HANDLER_HELPERS, (page.name, sorted(used - HANDLER_HELPERS))


def test_no_raw_json_literals_inside_double_quoted_handlers():
    for page in PAGES:
        bad = [h for h in _handlers(page) if "${jstr(" in h or "${JSON.stringify(" in h]
        assert not bad, (page.name, bad[:3])


def test_handlers_are_double_quoted():
    """The rules above read double-quoted handler attributes; a single-quoted one would slip past them."""
    for page in PAGES:
        assert not SINGLE_QUOTED_HANDLER.search(page.read_text(encoding="utf-8")), page.name


def test_jsa_is_defined_where_it_is_used():
    pages = {p.name: p.read_text(encoding="utf-8") for p in PAGES}
    for name, text in pages.items():
        if "${jsa(" not in text:
            continue
        own = "const jsa = " in text
        via_index = name == "studio.js" and "const jsa = " in pages["index.html"]     # studio.js runs inside index.html
        assert own or via_index, name
    helper = [line for line in pages["index.html"].splitlines() if line.startswith("const jstr = ")]
    for name in ("objects.html", "review.html", "workspace.js"):
        assert [line for line in pages[name].splitlines() if line.startswith("const jstr = ")] == helper, name
    for name, text in pages.items():                  # every jsa is esc(jstr(v)), over an esc() that escapes & and "
        defs = [line for line in text.splitlines() if line.startswith("const jsa = ")]
        assert all(line.startswith("const jsa = v => esc(jstr(v));") for line in defs), name
        if defs:
            esc = next(line for line in text.splitlines() if line.startswith("const esc = "))
            assert "&amp;" in esc and "&quot;" in esc, name


def test_jsa_round_trips_hostile_values():
    """jsa() = esc(jstr(v)): after the browser's attribute decoding the handler sees one JS string literal."""
    import html
    import json

    def jstr(v):
        return json.dumps(str(v), ensure_ascii=False).replace("'", "\\u0027").replace("<", "\\u003c")

    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    for v in ("a'-alert(document.domain)-'@x.example", 'x" onmouseover="alert(1)', "&quot;);alert(1);//",
              "</script><script>alert(1)</script>", "o'brien@example.ie", "rtsp://cam/1?a=1&b=2"):
        attr = esc(jstr(v))
        assert '"' not in attr and "'" not in attr and "<" not in attr
        assert json.loads(html.unescape(attr)) == v
    # the escaper jsa() replaced in the workspace: only " -> &quot;, so an entity in the value decodes into a quote
    jsq = json.dumps("&quot;);alert(1);//").replace('"', "&quot;")
    assert html.unescape(jsq) == '"");alert(1);//"'          # send("");alert(1);//") - the value ran as code
