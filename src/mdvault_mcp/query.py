"""A tiny query language over frontmatter: `status=draft AND tags contains project`.

    expr    := term (OR term)*
    term    := clause (AND clause)*
    clause  := [NOT] key (= | != | > | >= | < | <= | contains) value
             | [NOT] key (exists | missing)

Keys may be dotted for nested maps (`meta.owner`). Values with spaces go in quotes.
String comparison ignores case. `=` on a list means "any element equals", `contains` on
a list means the same and on a string means substring. For the `tags` key, `contains
project` also matches the nested tag `project/alpha`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .safeio import key


class QueryError(ValueError):
    pass


_TOKEN = re.compile(r"""\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|(!=|>=|<=|=|>|<)|([^\s=!<>"']+))""")
_OPS = {"=", "!=", ">", ">=", "<", "<="}
GRAMMAR = (
    "Query syntax: clauses like `status=draft`, `due < 2026-12-01`, `tags contains project`, "
    "`owner exists`, `owner missing`, joined with AND / OR (AND binds tighter), optional NOT in "
    "front. Quote values with spaces: title = \"My Plan\"."
)


@dataclass
class Clause:
    key: str
    op: str
    value: str
    negate: bool = False


def _tokenize(expr: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    expr = expr.rstrip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if m is None or m.end() == pos:
            raise QueryError(f"cannot parse the query near {expr[pos:pos + 20]!r}. {GRAMMAR}")
        pos = m.end()
        if m.group(1) is not None:
            out.append(("str", re.sub(r"\\(.)", r"\1", m.group(1))))
        elif m.group(2) is not None:
            out.append(("str", m.group(2)))
        elif m.group(3) is not None:
            out.append(("op", m.group(3)))
        else:
            out.append(("word", m.group(4)))
    return out


def parse_query(expr: str) -> list[list[Clause]]:
    """Return OR-of-ANDs: a list of groups, each group a list of clauses."""
    toks = _tokenize(expr)
    if not toks:
        raise QueryError(f"empty query. {GRAMMAR}")
    groups: list[list[Clause]] = [[]]
    i = 0

    def kw(t: tuple[str, str], word: str) -> bool:
        return t[0] == "word" and t[1].casefold() == word

    while i < len(toks):
        negate = False
        if kw(toks[i], "not"):
            negate = True
            i += 1
        if i >= len(toks) or toks[i][0] == "op":
            raise QueryError(f"expected a property name. {GRAMMAR}")
        k = toks[i][1]
        i += 1
        if i >= len(toks):
            raise QueryError(f"property {k!r} needs an operator. {GRAMMAR}")
        t = toks[i]
        if t[0] == "op":
            op = t[1]
            i += 1
        elif kw(t, "contains") or kw(t, "exists") or kw(t, "missing"):
            op = t[1].casefold()
            i += 1
        else:
            raise QueryError(f"unknown operator {t[1]!r} after {k!r}. {GRAMMAR}")
        val = ""
        if op not in ("exists", "missing"):
            if i >= len(toks) or toks[i][0] == "op":
                raise QueryError(f"operator {op!r} after {k!r} needs a value. {GRAMMAR}")
            val = toks[i][1]
            i += 1
        groups[-1].append(Clause(k, op, val, negate))
        if i < len(toks):
            if kw(toks[i], "and"):
                i += 1
            elif kw(toks[i], "or"):
                i += 1
                groups.append([])
            else:
                raise QueryError(f"expected AND or OR before {toks[i][1]!r}. {GRAMMAR}")
            if i >= len(toks):
                raise QueryError(f"the query ends after AND/OR. {GRAMMAR}")
    return groups


_MISSING = object()


def _lookup(fm: dict[str, Any], dotted: str) -> Any:
    cur: Any = fm
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return _MISSING
        if part in cur:
            cur = cur[part]
            continue
        folded = {k.casefold(): v for k, v in cur.items()}
        if part.casefold() not in folded:
            return _MISSING
        cur = folded[part.casefold()]
    return cur


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x)
        except ValueError:
            return None
    return None


def _scalar_eq(v: Any, expected: str) -> bool:
    if v is None:
        return key(expected) in ("null", "none", "~", "")
    if isinstance(v, bool):
        return key(expected) == ("true" if v else "false")
    if isinstance(v, (int, float)):
        n = _num(expected)
        return n is not None and n == float(v)
    return key(str(v)) == key(expected)


def _cmp(v: Any, op: str, expected: str) -> bool:
    a, b = _num(v), _num(expected)
    if a is not None and b is not None:
        x: Any = a
        y: Any = b
    elif isinstance(v, str):
        x, y = key(v), key(expected)
    else:
        return False
    return bool({">": x > y, ">=": x >= y, "<": x < y, "<=": x <= y}[op])


def _eval(c: Clause, fm: dict[str, Any], tags: list[str]) -> bool:
    v: Any = tags if c.key.casefold() == "tags" and _lookup(fm, "tags") is not _MISSING else _lookup(fm, c.key)
    if c.op == "exists":
        return v is not _MISSING
    if c.op == "missing":
        return v is _MISSING
    if v is _MISSING:
        return c.op == "!="
    items = v if isinstance(v, list) else None
    if c.op == "=":
        return any(_scalar_eq(x, c.value) for x in items) if items is not None else _scalar_eq(v, c.value)
    if c.op == "!=":
        return not (any(_scalar_eq(x, c.value) for x in items) if items is not None else _scalar_eq(v, c.value))
    if c.op == "contains":
        if items is not None:
            if c.key.casefold() == "tags":
                want = key(c.value.lstrip("#"))
                return any(key(str(x)) == want or key(str(x)).startswith(want + "/") for x in items)
            return any(_scalar_eq(x, c.value) for x in items)
        if isinstance(v, dict):
            return key(c.value) in {key(k) for k in v}
        return v is not None and key(c.value) in key(str(v))
    # ordering operators
    if items is not None:
        return any(_cmp(x, c.op, c.value) for x in items)
    return _cmp(v, c.op, c.value)


def matches(groups: list[list[Clause]], fm: dict[str, Any], tags: list[str]) -> bool:
    return any(all(_eval(c, fm, tags) != c.negate for c in g) for g in groups)
