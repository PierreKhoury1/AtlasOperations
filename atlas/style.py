"""Server-written text and the reply's style (round 2 designer spec sections 6.13, 7.4 and 8).

plain_md: a model-written string the server embeds in a line of its own (an agent's name, a step label, a proposal's
reason, a question) is shown as written, never as markup. The workspace's mdInline turns only ` (code), * (bold and
italics) and [text](url) (a link) into markup, so those are taken out: a name like "[x](https://…)" would otherwise
render as a link inside a server line.

strip_opener: the deterministic opener strip (8.3), "no active listening" as code. An acknowledgement ("Got it —",
"Understood, Leo.", "Hi!") never opens a reply, and on a design turn neither does a sentence that plays the owner's words
back or introduces Atlas ("It sounds like…", "You want…", "I'm Atlas…", "I can certainly help…"). On the 59 stored
replies these rules removed only filler, and never a whole reply. Scope: the model's prose (the streamed head and the
final text), never the server's own lines."""
from __future__ import annotations

import re
from typing import Any

# the acknowledgement prefix, as measured on the stored replies (round2 spec2-scripts/strip_probe2.py), with one change:
# a plain hyphen counts as its punctuation only when no letter follows it ("Got it - you" yes, "Sure-footed" no)
ACK = re.compile(r"^\s*(?:(?:got it|understood|i understand|i see|thanks(?: for (?:sharing|that|the \w+))?|"
                 r"thank you(?: for \w+(?: \w+)?)?|great(?: question)?|good(?: question)?|perfect|noted|sounds good|"
                 r"absolutely|certainly|of course|sure|okay|ok|excellent|hello|hi|hey)(?:,? [A-Z][a-z]+)?\s*"
                 r"(?:[,.!:;—–]|-(?![A-Za-z]))+\s*)+", re.I)
# a sentence that restates the owner or introduces Atlas; "you need" is not in the list: it never fired on the stored
# replies and would delete a real requirement ("You need a stream address…")
RESTATE = re.compile(r"^(?:it sounds like|it seems like|so you(?:'re| are)|you(?:'re| are) (?:looking|after)|you want|"
                     r"you(?:'d|’d) like|i understand|i hear|it(?:'s|’s) a pleasure|nice to meet|pleased to meet|"
                     r"i(?:'m|’m) atlas|i can (?:certainly )?help|that(?:'s|’s) a (?:clear|great|good|helpful)|that helps|"
                     r"that makes sense|makes sense)\b", re.I)
SENT = re.compile(r"(?<=[.!?])\s+")


def plain_md(text: Any) -> str:
    """`text` with ` and * removed and "](" split to "] (" ("" for anything that is not text)."""
    if not isinstance(text, str):
        return ""
    return text.replace("`", "").replace("*", "").replace("](", "] (")


def strip_opener(text: Any, answer: bool = False) -> str:
    """The reply without its opener (8.3), first character capitalised.
    1. Leading acknowledgements go ("Got it —", "Thanks for sharing.", "Hello Leo.", "Sure,").
    2. On a design turn (not `answer`), so do leading sentences that restate the owner or introduce Atlas, while at
       least one sentence remains after each.
    The two rules repeat until neither applies, so strip_opener(strip_opener(x)) == strip_opener(x), and what is left
    is never empty: an acknowledgement or a sentence with nothing after it stays."""
    if not isinstance(text, str):
        return ""
    p = text.lstrip()
    while p:
        before = p
        m = ACK.match(p)
        if m and p[m.end():].strip():
            p = p[m.end():]
        if not answer:
            parts = SENT.split(p, maxsplit=1)
            if len(parts) == 2 and parts[1].strip() and RESTATE.match(parts[0]):
                p = parts[1]
        if p == before:
            break
    return p[:1].upper() + p[1:]


def head_settled(text: Any) -> bool:
    """Whether more text after `text` can no longer change what strip_opener takes from its start: the streamed head
    may then be shown (7.4), and the final strip at the end of the turn removes nothing that was streamed. Not settled
    while what is left starts with an acknowledgement, or a restating sentence, that stays only because nothing
    follows it yet."""
    p = strip_opener(text)
    m = ACK.match(p)
    if m and not p[m.end():].strip():
        return False
    if RESTATE.match(p):
        parts = SENT.split(p, maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return False
    return True
