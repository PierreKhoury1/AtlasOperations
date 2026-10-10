"""Server-written text (round 2 designer spec sections 6.13 and 8).

plain_md: a model-written string the server embeds in a line of its own (an agent's name, a step label, a proposal's
reason, a question) is shown as written, never as markup. The workspace's mdInline turns only ` (code), * (bold and
italics) and [text](url) (a link) into markup, so those are taken out: a name like "[x](https://…)" would otherwise
render as a link inside a server line."""
from __future__ import annotations

from typing import Any


def plain_md(text: Any) -> str:
    """`text` with ` and * removed and "](" split to "] (" ("" for anything that is not text)."""
    if not isinstance(text, str):
        return ""
    return text.replace("`", "").replace("*", "").replace("](", "] (")
