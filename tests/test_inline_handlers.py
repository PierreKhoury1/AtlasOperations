"""Data never goes raw into an inline event handler. A contact key, camera or memory name with a quote in it used to
close the JS string inside onclick="fn('${esc(x)}')" and run as code with the owner's session (esc() leaves ' alone,
and the browser decodes &quot; / &#39; before the handler runs); a JSON literal from jstr() inside a double-quoted
attribute ended the attribute, so the camera Feed button never parsed. These checks keep both patterns out."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "atlas" / "desk" / "static"
HANDLER = re.compile(r'\son[a-z]+="([^"]*)"')


def _handlers(name: str) -> list[str]:
    return HANDLER.findall((STATIC / name).read_text(encoding="utf-8"))


def test_no_html_escaped_strings_inside_handlers():
    for page in ("index.html", "objects.html", "workspace.js", "studio.js", "ops.html"):
        bad = [h for h in _handlers(page) if re.search(r"'\$\{esc\(", h)]
        assert not bad, (page, bad[:3])


def test_no_raw_json_literals_inside_double_quoted_handlers():
    for page in ("index.html", "objects.html", "workspace.js"):
        bad = [h for h in _handlers(page)
               if re.search(r"\$\{(jstr|JSON\.stringify)\(", h) and not re.search(r"\$\{(esc|jsa)\((jstr|JSON\.stringify)?", h)]
        assert not bad, (page, bad[:3])


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
