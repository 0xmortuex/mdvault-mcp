"""YAML frontmatter: split it off a note, parse a practical YAML subset, dump it.

Why a hand-written subset instead of pyyaml: no dependency, no type surprises
(pyyaml turns `date: 2024-01-01` into a date object and `no` into False), and the
errors can name the line. The subset covers what note apps write: `key: value`
maps (nested by indentation), block and flow lists, quoted strings, numbers,
booleans, null, and `|` / `>` block scalars. Anything else (anchors, tags,
multi-line plain scalars) raises FrontmatterError instead of guessing.

Nothing here ever rewrites a note's frontmatter: edits are plain text splices, so
comments and formatting survive byte for byte. `dump_yaml` is only used for the
frontmatter of brand-new notes.
"""

from __future__ import annotations

import re
from typing import Any


class FrontmatterError(ValueError):
    """The frontmatter is not valid (or not supported) YAML."""


def split_frontmatter(text: str) -> tuple[str | None, int]:
    """Return (raw yaml between the fences, offset where the body starts).

    `text` must not start with a BOM. A note has frontmatter only if its very
    first line is `---` and a later line is `---` (or `...`). An opening fence
    with no closing fence is not frontmatter, same as Obsidian.
    """
    first_nl = text.find("\n")
    if first_nl == -1 or text[:first_nl].rstrip() != "---":
        return None, 0
    pos = first_nl + 1
    n = len(text)
    while pos <= n:
        nl = text.find("\n", pos)
        end = n if nl == -1 else nl
        if text[pos:end].rstrip() in ("---", "..."):
            return text[first_nl + 1 : pos], (n if nl == -1 else nl + 1)
        if nl == -1:
            break
        pos = nl + 1
    return None, 0


_INT = re.compile(r"^[-+]?(0|[1-9][0-9]*)$")
_FLOAT = re.compile(r"^[-+]?([0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)([eE][-+]?[0-9]+)?$")
_KEY_END = re.compile(r":(\s|$)")
_QUOTED_KEY = re.compile(r"""^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*')\s*:(\s|$)""")


def parse_plain(s: str) -> Any:
    s = s.strip()
    if s in ("", "~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if _INT.match(s):
        return int(s)
    if _FLOAT.match(s) and ("." in s or "e" in s or "E" in s):
        return float(s)
    return s


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "/": "/", " ": " "}


def _read_double(s: str, i: int, ln: int) -> tuple[str, int]:
    """s[i] is the opening quote. Return (value, index after the closing quote)."""
    out: list[str] = []
    i += 1
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 1
            if i >= len(s):
                break
            e = s[i]
            if e in _ESCAPES:
                out.append(_ESCAPES[e])
            elif e in "xuU":
                width = {"x": 2, "u": 4, "U": 8}[e]
                hexs = s[i + 1 : i + 1 + width]
                if len(hexs) != width or not re.fullmatch(r"[0-9a-fA-F]+", hexs):
                    raise FrontmatterError(f"line {ln}: bad \\{e} escape")
                out.append(chr(int(hexs, 16)))
                i += width
            else:
                raise FrontmatterError(f"line {ln}: unknown escape \\{e}")
        elif c == '"':
            return "".join(out), i + 1
        else:
            out.append(c)
        i += 1
    raise FrontmatterError(f"line {ln}: unterminated double-quoted string")


def _read_single(s: str, i: int, ln: int) -> tuple[str, int]:
    out: list[str] = []
    i += 1
    while i < len(s):
        c = s[i]
        if c == "'":
            if s[i + 1 : i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise FrontmatterError(f"line {ln}: unterminated single-quoted string")


class _Flow:
    """Parser for `[a, b, [c]]` and `{k: v}` on a single line."""

    def __init__(self, s: str, ln: int) -> None:
        self.s = s
        self.i = 0
        self.ln = ln

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t":
            self.i += 1

    def value(self) -> Any:
        self.ws()
        if self.i >= len(self.s):
            raise FrontmatterError(f"line {self.ln}: unexpected end of flow collection")
        c = self.s[self.i]
        if c == "[":
            return self.seq()
        if c == "{":
            return self.map()
        if c == '"':
            v, self.i = _read_double(self.s, self.i, self.ln)
            return v
        if c == "'":
            v, self.i = _read_single(self.s, self.i, self.ln)
            return v
        if c in "&*!|>%@`":
            raise FrontmatterError(f"line {self.ln}: unsupported YAML syntax {c!r}")
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in ",]}":
            if self.s[self.i] == ":" and self.s[self.i + 1 : self.i + 2] in (" ", ""):
                break
            self.i += 1
        return parse_plain(self.s[start : self.i])

    def seq(self) -> list[Any]:
        self.i += 1
        out: list[Any] = []
        self.ws()
        if self.s[self.i : self.i + 1] == "]":
            self.i += 1
            return out
        while True:
            out.append(self.value())
            self.ws()
            c = self.s[self.i : self.i + 1]
            self.i += 1
            if c == "]":
                return out
            if c != ",":
                raise FrontmatterError(f"line {self.ln}: expected ',' or ']' in list")
            self.ws()
            if self.s[self.i : self.i + 1] == "]":  # trailing comma
                self.i += 1
                return out

    def map(self) -> dict[str, Any]:
        self.i += 1
        out: dict[str, Any] = {}
        self.ws()
        if self.s[self.i : self.i + 1] == "}":
            self.i += 1
            return out
        while True:
            k = self.value()
            self.ws()
            if self.s[self.i : self.i + 1] != ":":
                raise FrontmatterError(f"line {self.ln}: expected ':' in {{...}}")
            self.i += 1
            v = self.value()
            out[str(k)] = v
            self.ws()
            c = self.s[self.i : self.i + 1]
            self.i += 1
            if c == "}":
                return out
            if c != ",":
                raise FrontmatterError(f"line {self.ln}: expected ',' or '}}' in map")


def parse_value(s: str, ln: int) -> Any:
    s = s.strip()
    if not s:
        return None
    c = s[0]
    if c == '"':
        v, j = _read_double(s, 0, ln)
        if s[j:].strip():
            raise FrontmatterError(f"line {ln}: unexpected text after quoted string")
        return v
    if c == "'":
        v, j = _read_single(s, 0, ln)
        if s[j:].strip():
            raise FrontmatterError(f"line {ln}: unexpected text after quoted string")
        return v
    if c in "[{":
        f = _Flow(s, ln)
        v = f.value()
        f.ws()
        if f.i != len(s):
            raise FrontmatterError(f"line {ln}: unexpected text after flow collection")
        return v
    if c in "&*!|>%@`":
        raise FrontmatterError(f"line {ln}: unsupported YAML syntax {c!r} (anchors, tags and aliases are not supported)")
    return parse_plain(s)


def _strip_comment(s: str) -> str:
    """Cut a trailing `# comment`. `#` starts one only after whitespace and outside quotes;
    a quote char opens a string only at the start of a scalar."""
    quote = ""
    last = ""  # last non-space character seen outside quotes
    i = 0
    while i < len(s):
        c = s[i]
        if quote:
            if quote == '"' and c == "\\":
                i += 2
                continue
            if c == quote:
                if quote == "'" and s[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                quote = ""
                last = c
            i += 1
            continue
        if c in "\"'" and (last == "" or last in ":,[{-"):
            quote = c
        elif c == "#" and (i == 0 or s[i - 1] in " 	"):
            return s[:i].rstrip()
        elif c not in " 	":
            last = c
        i += 1
    return s.rstrip()


class _Line:
    __slots__ = ("indent", "content", "ln")

    def __init__(self, indent: int, content: str, ln: int) -> None:
        self.indent = indent
        self.content = content
        self.ln = ln


class _Parser:
    def __init__(self, raw: str, first_line: int) -> None:
        self.lines = [ln.rstrip("\r") for ln in raw.split("\n")]
        self.i = 0
        self.first = first_line

    def lineno(self, i: int) -> int:
        return self.first + i

    def peek(self) -> _Line | None:
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
                raise FrontmatterError(f"line {self.lineno(self.i)}: tabs are not allowed for indentation")
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                self.i += 1
                continue
            content = _strip_comment(raw.strip())
            if content == "---":
                raise FrontmatterError(f"line {self.lineno(self.i)}: unexpected document marker")
            return _Line(len(raw) - len(raw.lstrip(" ")), content, self.lineno(self.i))
        return None

    def parse_root(self) -> dict[str, Any]:
        first = self.peek()
        if first is None:
            return {}
        if first.content.startswith("- ") or first.content == "-":
            raise FrontmatterError(f"line {first.ln}: frontmatter must be a mapping of `key: value` pairs")
        if first.indent != 0:
            raise FrontmatterError(f"line {first.ln}: unexpected indentation")
        result = self.parse_map(first.indent)
        extra = self.peek()
        if extra is not None:
            raise FrontmatterError(f"line {extra.ln}: unexpected content {extra.content!r}")
        return result

    def parse_block(self, indent: int) -> Any:
        ln = self.peek()
        assert ln is not None
        if ln.content.startswith("- ") or ln.content == "-":
            return self.parse_seq(indent)
        return self.parse_map(indent)

    def split_key(self, content: str, ln: int) -> tuple[str, str]:
        q = _QUOTED_KEY.match(content)
        if q:
            quoted = q.group(1)
            key = _read_double(quoted, 0, ln)[0] if quoted[0] == '"' else _read_single(quoted, 0, ln)[0]
            return key, content[q.end() :].strip()
        m = _KEY_END.search(content)
        if m is None or m.start() == 0:
            raise FrontmatterError(f"line {ln}: expected `key: value`, got {content!r}")
        key = content[: m.start()].strip()
        if key[0] in "&*!|>%@`[{":
            raise FrontmatterError(f"line {ln}: unsupported YAML syntax in key {key!r}")
        return key, content[m.end() :].strip()

    def parse_map(self, ind: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while True:
            ln = self.peek()
            if ln is None or ln.indent < ind:
                break
            if ln.indent > ind:
                raise FrontmatterError(f"line {ln.ln}: unexpected indentation")
            if ln.content.startswith("- ") or ln.content == "-":
                raise FrontmatterError(f"line {ln.ln}: list item where a `key: value` pair was expected")
            key, rest = self.split_key(ln.content, ln.ln)
            if key in out:
                raise FrontmatterError(f"line {ln.ln}: duplicate key {key!r}")
            self.i += 1
            value: Any
            if rest == "":
                nxt = self.peek()
                if nxt is not None and (
                    nxt.indent > ind or (nxt.indent == ind and (nxt.content.startswith("- ") or nxt.content == "-"))
                ):
                    value = self.parse_block(nxt.indent)
                else:
                    value = None
            elif rest[0] in "|>":
                value = self.block_scalar(rest, ind, ln.ln)
            else:
                value = parse_value(rest, ln.ln)
            out[key] = value
        return out

    def parse_seq(self, ind: int) -> list[Any]:
        out: list[Any] = []
        while True:
            ln = self.peek()
            if ln is None or ln.indent != ind or not (ln.content.startswith("- ") or ln.content == "-"):
                if ln is not None and ln.indent > ind:
                    raise FrontmatterError(f"line {ln.ln}: unexpected indentation")
                break
            rest = ln.content[1:].lstrip(" ")
            offset = len(ln.content) - len(rest)  # chars from the dash to the item start
            if rest == "":
                self.i += 1
                nxt = self.peek()
                if nxt is not None and nxt.indent > ind:
                    out.append(self.parse_block(nxt.indent))
                else:
                    out.append(None)
            elif rest[0] in "|>":
                self.i += 1
                out.append(self.block_scalar(rest, ind, ln.ln))
            elif rest[0] not in "\"'[{" and (_KEY_END.search(rest) is not None) or _QUOTED_KEY.match(rest):
                # `- key: value` starts an inline map; re-indent the line and parse it as a map
                self.lines[self.i] = " " * (ind + offset) + rest
                out.append(self.parse_map(ind + offset))
            else:
                self.i += 1
                out.append(parse_value(rest, ln.ln))
        return out

    def block_scalar(self, header: str, ind: int, ln: int) -> str:
        m = re.fullmatch(r"([|>])([-+]?)([1-9]?)([-+]?)", header)
        if not m:
            raise FrontmatterError(f"line {ln}: bad block scalar header {header!r}")
        style = m.group(1)
        chomp = m.group(2) or m.group(4)
        explicit = int(m.group(3)) if m.group(3) else 0
        body: list[str] = []
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if raw.strip() and (len(raw) - len(raw.lstrip(" "))) <= ind:
                break
            body.append(raw)
            self.i += 1
        block_indent = ind + explicit if explicit else 0
        if not block_indent:
            for raw in body:
                if raw.strip():
                    block_indent = len(raw) - len(raw.lstrip(" "))
                    break
        lines = [raw[block_indent:] if raw.strip() else "" for raw in body]
        trailing = 0
        while lines and lines[-1] == "":
            lines.pop()
            trailing += 1
        if style == "|":
            text = "\n".join(lines)
        else:
            parts: list[str] = []
            for idx, line in enumerate(lines):
                if idx == 0:
                    parts.append(line)
                elif line == "":
                    parts.append("\n")
                elif parts[-1].endswith("\n") or line.startswith(" ") or lines[idx - 1].startswith(" "):
                    parts.append(line if parts[-1].endswith("\n") else "\n" + line)
                else:
                    parts.append(" " + line)
            text = "".join(parts)
        if not lines:
            return ""
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" * (trailing + 1)
        return text + "\n"


def parse_yaml(raw: str, first_line: int = 2) -> dict[str, Any]:
    """Parse frontmatter text. `first_line` is the file line number of its first line."""
    return _Parser(raw, first_line).parse_root()


_BARE_WORDS = {"true", "false", "null", "~", "yes", "no", "on", "off", "y", "n"}


def _dump_str(s: str) -> str:
    plain_ok = (
        s != ""
        and s == s.strip()
        and s.lower() not in _BARE_WORDS
        and parse_plain(s) == s
        and s[0] not in "-?:,[]{}#&*!|>'\"%@`"
        and ": " not in s
        and " #" not in s
        and not s.endswith(":")
        and all(c >= " " and c != "\x7f" for c in s)
    )
    if plain_ok:
        return s
    out = ['"']
    for c in s:
        if c == "\\":
            out.append("\\\\")
        elif c == '"':
            out.append('\\"')
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif c == "\r":
            out.append("\\r")
        elif c < " " or c == "\x7f":
            out.append(f"\\x{ord(c):02x}")
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


def _dump_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _dump_str(v)
    raise FrontmatterError(f"cannot write a {type(v).__name__} value in frontmatter")


def _dump_entry(key: str, value: Any, indent: int, out: list[str]) -> None:
    pad = " " * indent
    k = _dump_str(key)
    if isinstance(value, dict) and value:
        out.append(f"{pad}{k}:")
        for k2, v2 in value.items():
            _dump_entry(str(k2), v2, indent + 2, out)
    elif isinstance(value, list) and value:
        out.append(f"{pad}{k}:")
        for item in value:
            if isinstance(item, (dict, list)) and item:
                raise FrontmatterError("nested collections inside lists are not supported when writing frontmatter")
            if isinstance(item, dict):
                out.append(f"{pad}  - {{}}")
            elif isinstance(item, list):
                out.append(f"{pad}  - []")
            else:
                out.append(f"{pad}  - {_dump_scalar(item)}")
    elif isinstance(value, dict):
        out.append(f"{pad}{k}: {{}}")
    elif isinstance(value, list):
        out.append(f"{pad}{k}: []")
    else:
        out.append(f"{pad}{k}: {_dump_scalar(value)}")


def dump_yaml(data: dict[str, Any]) -> str:
    """Serialise a mapping to YAML (no fences, no trailing newline)."""
    out: list[str] = []
    for k, v in data.items():
        _dump_entry(str(k), v, 0, out)
    return "\n".join(out)
