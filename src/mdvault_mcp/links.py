"""Find links and tags in note text.

Recognised: [[wikilinks]] (with #heading, ^block, |alias, and ![[embeds]]) and
[markdown](links.md) (relative or /rooted, %-encoded or <angle bracketed>, images
included). Links inside code fences and `inline code` are ignored. Every link records
the exact character span of its *target*, so a rename can splice a new target in
without disturbing a single other byte of the file.

Not handled (documented limitation): reference-style `[x]: path` definitions and raw
HTML `<a href>`.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from urllib.parse import unquote


@dataclass(frozen=True)
class Link:
    kind: str  # "wiki" or "md"
    embed: bool
    start: int  # span of the whole link in the file text
    end: int
    tstart: int  # span of the target path/name (what a rename rewrites)
    tend: int
    target: str  # decoded target, no heading/block part
    fragment: str  # "#heading", "#^block", "^block" or ""
    text: str  # alias (wiki) or link text (md)
    line: int  # 1-based
    angle: bool = False  # md link destination was written <like this>
    raw: str = ""  # the link exactly as written


_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_WIKI = re.compile(r"(!?)\[\[([^\[\]\n]*)\]\]")
_MD = re.compile(r"(!?)\[([^\]\n]*)\]\(\s*(<[^>\n]*>|[^)\s]*)(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_TAG = re.compile(r"(?<![\w/#&\[(])#(\w[\w/\-]*)")


def mask_code(text: str) -> str:
    """Return `text` with code fences and inline code blanked out (same length, same newlines)."""
    out: list[str] = []
    fence_char = ""
    fence_len = 0
    pos = 0
    for line in text.split("\n"):
        stripped = line.rstrip("\r")
        m = _FENCE.match(stripped)
        if fence_char:
            if m and m.group(1)[0] == fence_char and len(m.group(1)) >= fence_len and stripped.strip(" \t" + fence_char) == "":
                fence_char = ""
            out.append(re.sub(r"[^\r]", " ", line))
        elif m:
            fence_char = m.group(1)[0]
            fence_len = len(m.group(1))
            out.append(re.sub(r"[^\r]", " ", line))
        else:
            if "`" in line:
                line = _INLINE_CODE.sub(lambda mm: " " * (mm.end() - mm.start()), line)
            out.append(line)
        pos += len(line) + 1
    return "\n".join(out)


def _line_starts(text: str) -> list[int]:
    starts = [0]
    i = text.find("\n")
    while i != -1:
        starts.append(i + 1)
        i = text.find("\n", i + 1)
    return starts


def parse_links(text: str) -> list[Link]:
    """All links in `text`, in file order, ignoring code."""
    masked = mask_code(text)
    starts = _line_starts(text)
    found: list[Link] = []

    def line_of(pos: int) -> int:
        return bisect.bisect_right(starts, pos)

    for m in _WIKI.finditer(masked):
        embed = m.group(1) == "!"
        inner = m.group(2)
        base = m.start(2)
        pipe = inner.find("|")
        head = inner if pipe == -1 else inner[:pipe]
        alias = "" if pipe == -1 else inner[pipe + 1 :].strip()
        if head.endswith("\\"):  # `\|` is how a pipe is escaped inside a markdown table
            head = head[:-1]
        cut = len(head)
        for i, ch in enumerate(head):
            if ch in "#^":
                cut = i
                break
        name_raw = head[:cut]
        lead = len(name_raw) - len(name_raw.lstrip())
        name = name_raw.strip()
        frag = head[cut:].strip()
        tstart = base + lead
        found.append(
            Link("wiki", embed, m.start(), m.end(), tstart, tstart + len(name), name, frag, alias, line_of(m.start()), False, text[m.start() : m.end()])
        )

    wiki_spans = [(lk.start, lk.end) for lk in found]
    for m in _MD.finditer(masked):
        if any(a <= m.start() < b for a, b in wiki_spans):
            continue
        dest = m.group(3)
        dstart = m.start(3)
        angle = dest.startswith("<")
        if angle:
            dest = dest[1:-1]
            dstart += 1
        if dest == "" or dest.startswith("#") or _SCHEME.match(dest) or dest.startswith("//"):
            continue  # empty, same-note anchor, or external (http:, mailto:, ...)
        hash_i = dest.find("#")
        path_raw = dest if hash_i == -1 else dest[:hash_i]
        frag = "" if hash_i == -1 else dest[hash_i:]
        q = path_raw.find("?")
        if q != -1:  # query strings are not part of a file path
            frag = path_raw[q:] + frag
            path_raw = path_raw[:q]
        if path_raw == "":
            continue
        target = unquote(path_raw)
        found.append(
            Link("md", m.group(1) == "!", m.start(), m.end(), dstart, dstart + len(path_raw), target, unquote(frag), m.group(2), line_of(m.start()), angle, text[m.start() : m.end()])
        )
    found.sort(key=lambda lk: lk.start)
    return found


def extract_inline_tags(body: str) -> list[str]:
    """`#tag` and `#nested/tag` occurrences in note text (code ignored), lowercased, in order."""
    out: list[str] = []
    for m in _TAG.finditer(mask_code(body)):
        t = m.group(1).rstrip("/-")
        if t and not t.isdigit() and not re.fullmatch(r"[\d/]+", t):
            out.append(t)
    return out
