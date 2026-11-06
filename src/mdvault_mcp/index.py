"""The vault index: scan, parse, cache, resolve links, rank search results.

Every tool call starts with `Vault.refresh()`: one directory walk plus a stat per file.
Only files whose (mtime, size) changed are re-read and re-parsed, and the parsed form
is kept in a small SQLite file outside the vault so a restart does not re-read 5,000
notes. The link graph and the BM25 postings are rebuilt lazily, only after a change.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from .frontmatter import FrontmatterError, parse_yaml, split_frontmatter
from .links import Link, extract_inline_tags, mask_code, parse_links
from .safeio import VaultError, decode, key

SCHEMA_VERSION = 3
_WORD = re.compile(r"\w+", re.UNICODE)
_H1 = re.compile(r"^#[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_REPARSE_TAGS = (0xA0000003, 0xA000000C)  # Windows junction, symlink


@functools.lru_cache(maxsize=200_000)
def fold_word(w: str) -> str:
    """Search form of a word: casefolded, diacritics stripped (Turkish: sekER finds Seker, ISPARTA finds Isparta)."""
    s = unicodedata.normalize("NFKD", w.casefold())
    return "".join(c for c in s if not unicodedata.combining(c)).replace("ı", "i")


def tokenize(text: str) -> list[str]:
    return [fold_word(w) for w in _WORD.findall(text)]


def norm_tag(t: str) -> str:
    return key(t.strip().lstrip("#").strip("/"))


def tags_from_frontmatter(fm: dict[str, Any] | None) -> list[str]:
    if not fm:
        return []
    raw = None
    for k, v in fm.items():
        if k.casefold() in ("tags", "tag"):
            raw = v
            break
    items: list[str] = []
    if isinstance(raw, list):
        items = [str(x) for x in raw if x is not None]
    elif isinstance(raw, str):
        items = [x for x in re.split(r"[,\s]+", raw) if x]
    return [t for t in (norm_tag(x) for x in items) if t]


@dataclass
class NoteRecord:
    path: str
    mtime_ns: int = 0
    size: int = 0
    title: str = ""
    fm: dict[str, Any] | None = None
    fm_error: str | None = None
    fm_tags: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    tf: dict[str, int] = field(default_factory=dict)
    length: int = 0
    words: int = 0
    error: str | None = None  # e.g. not valid UTF-8

    @property
    def stem(self) -> str:
        return self.path.rsplit("/", 1)[-1][:-3]

    def to_json(self) -> str:
        return json.dumps(
            {
                "t": self.title, "fm": self.fm, "fe": self.fm_error, "ft": self.fm_tags, "tg": self.tags,
                "l": [
                    [lk.kind, lk.embed, lk.start, lk.end, lk.tstart, lk.tend, lk.target, lk.fragment, lk.text, lk.line, lk.angle, lk.raw]
                    for lk in self.links
                ],
                "c": self.contexts, "tf": self.tf, "n": self.length, "w": self.words, "e": self.error,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, path: str, mtime_ns: int, size: int, data: str) -> NoteRecord:
        d = json.loads(data)
        return cls(
            path, mtime_ns, size, d["t"], d["fm"], d["fe"], d["ft"], d["tg"],
            [Link(*x) for x in d["l"]], d["c"], d["tf"], d["n"], d["w"], d["e"],
        )


def _context(line_no: int, lines: list[str]) -> str:
    line = lines[line_no - 1].strip() if 0 < line_no <= len(lines) else ""
    return line if len(line) <= 220 else line[:217] + "..."


def parse_note(path: str, raw: bytes, mtime_ns: int, size: int) -> NoteRecord:
    rec = NoteRecord(path, mtime_ns, size)
    try:
        ft = decode(raw, path)
    except VaultError as e:
        rec.error = str(e)
        rec.title = rec.stem
        return rec
    text = ft.text
    fm_raw, body_off = split_frontmatter(text)
    if fm_raw is not None:
        try:
            rec.fm = parse_yaml(fm_raw)
        except FrontmatterError as e:
            rec.fm_error = str(e)
    body = text[body_off:]
    h1 = _H1.search(mask_code(body))
    rec.title = h1.group(1).strip() if h1 else rec.stem
    rec.fm_tags = tags_from_frontmatter(rec.fm)
    inline = [norm_tag(t) for t in extract_inline_tags(body)]
    rec.tags = sorted({*rec.fm_tags, *inline})
    rec.links = parse_links(text)
    if rec.links:
        lines = text.split("\n")
        rec.contexts = [_context(lk.line, lines) for lk in rec.links]
    words = _WORD.findall(body)
    rec.words = len(words)
    tf: Counter[str] = Counter(fold_word(w) for w in words)
    for t in tokenize(rec.title):
        tf[t] += 3  # a title match counts more than a body match
    for t in rec.tags:
        for part in tokenize(t):
            tf[part] += 2
    rec.tf = dict(tf)
    rec.length = sum(tf.values())
    return rec


# ---------------------------------------------------------------- link resolution


@dataclass
class Resolved:
    link: Link
    src: str
    status: str  # "note" | "attachment" | "broken" | "ambiguous"
    target: str | None = None
    candidates: list[str] = field(default_factory=list)
    context: str = ""


def strip_md(p: str) -> str:
    return p[:-3] if p.lower().endswith(".md") else p


class Graph:
    """Resolved links for the whole vault. Immutable snapshot built from the records."""

    def __init__(self, notes: dict[str, NoteRecord], attachments: set[str]) -> None:
        self.notes = notes
        self.pk = {p: key(strip_md(p)) for p in notes}
        self.stem_map: dict[str, list[str]] = {}
        for p, k in self.pk.items():
            self.stem_map.setdefault(k.rsplit("/", 1)[-1], []).append(p)
        self.att_map: dict[str, list[str]] = {}
        for a in attachments:
            self.att_map.setdefault(key(a.rsplit("/", 1)[-1]), []).append(a)
        self.full: dict[str, list[str]] = {}
        for p in notes:
            self.full.setdefault(key(p), []).append(p)
        for a in attachments:
            self.full.setdefault(key(a), []).append(a)
        self.attachments = attachments
        self.out: dict[str, list[Resolved]] = {}
        self.inc: dict[str, list[Resolved]] = {}
        self.amb_inc: dict[str, list[Resolved]] = {}
        for src, rec in sorted(notes.items()):
            lst: list[Resolved] = []
            for i, lk in enumerate(rec.links):
                r = self.resolve(lk, src)
                r.context = rec.contexts[i] if i < len(rec.contexts) else ""
                lst.append(r)
                if r.status == "note" and r.target is not None:
                    self.inc.setdefault(r.target, []).append(r)
                elif r.status == "ambiguous":
                    for c in r.candidates:
                        self.amb_inc.setdefault(c, []).append(r)
            self.out[src] = lst

    def note_candidates(self, ref: str) -> list[str]:
        """Notes a wikilink-style name or vault path could mean (may be 0, 1 or several)."""
        k = key(ref.replace("\\", "/")).strip("/")
        if k.endswith(".md"):
            k = k[:-3]
        last = k.rsplit("/", 1)[-1]
        lst = self.stem_map.get(last, [])
        if "/" not in k:
            return sorted(lst)
        exact = [p for p in lst if self.pk[p] == k]
        if exact:
            return sorted(exact)
        return sorted(p for p in lst if self.pk[p].endswith("/" + k))

    def attachment_candidates(self, ref: str) -> list[str]:
        k = key(ref.replace("\\", "/")).strip("/")
        lst = self.att_map.get(k.rsplit("/", 1)[-1], [])
        if "/" not in k:
            return sorted(lst)
        exact = [p for p in lst if key(p) == k]
        if exact:
            return sorted(exact)
        return sorted(p for p in lst if key(p).endswith("/" + k))

    def resolve(self, lk: Link, src: str) -> Resolved:
        if lk.kind == "wiki":
            return self._resolve_wiki(lk, src)
        return self._resolve_md(lk, src)

    def _resolve_wiki(self, lk: Link, src: str) -> Resolved:
        t = lk.target
        if t == "":
            return Resolved(lk, src, "note", src)
        cands = self.note_candidates(t)
        if not cands and not t.lower().endswith(".md"):
            acands = self.attachment_candidates(t)
            if len(acands) == 1:
                return Resolved(lk, src, "attachment", acands[0])
            if len(acands) > 1:
                return Resolved(lk, src, "ambiguous", None, acands)
        if len(cands) == 1:
            return Resolved(lk, src, "note", cands[0])
        if len(cands) > 1:
            return Resolved(lk, src, "ambiguous", None, cands)
        return Resolved(lk, src, "broken")

    def resolve_md_path(self, dest: str, src: str) -> str | None:
        """Vault-relative normalised path for a markdown link destination, or None if it leaves the vault."""
        d = dest.replace("\\", "/")
        if d.startswith("/"):
            joined = d.lstrip("/")
        else:
            base = src.rsplit("/", 1)[0] if "/" in src else ""
            joined = f"{base}/{d}" if base else d
        parts: list[str] = []
        for seg in joined.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if not parts:
                    return None
                parts.pop()
            else:
                parts.append(seg)
        return "/".join(parts)

    def _resolve_md(self, lk: Link, src: str) -> Resolved:
        np = self.resolve_md_path(lk.target, src)
        if np is None or np == "":
            return Resolved(lk, src, "broken")
        hits = self.full.get(key(np), [])
        if not hits and "." not in np.rsplit("/", 1)[-1]:
            hits = self.full.get(key(np + ".md"), [])
        if len(hits) == 1:
            return Resolved(lk, src, "note" if hits[0] in self.notes else "attachment", hits[0])
        if len(hits) > 1:
            return Resolved(lk, src, "ambiguous", None, sorted(hits))
        return Resolved(lk, src, "broken")


# ---------------------------------------------------------------- disk cache


def default_cache_dir() -> str:
    env = os.environ.get("MDVAULT_CACHE_DIR")
    if env:
        return env
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "mdvault-mcp")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Caches", "mdvault-mcp")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "mdvault-mcp")


class Cache:
    """Parsed notes keyed by (mtime_ns, size), stored in SQLite outside the vault."""

    def __init__(self, cache_dir: str, root: str) -> None:
        os.makedirs(cache_dir, exist_ok=True)
        digest = hashlib.sha1(os.path.normcase(root).encode("utf-8")).hexdigest()[:16]
        self.path = os.path.join(cache_dir, f"{digest}.sqlite")

    def _open(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS notes (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, data TEXT)")
        row = con.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
        if row is None or row[0] != str(SCHEMA_VERSION):
            con.execute("DELETE FROM notes")  # the parsed format changed: old rows are not valid
            con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            con.commit()
        return con

    def load(self) -> dict[str, tuple[int, int, str]]:
        try:
            with closing(self._open()) as con:
                return {p: (m, s, d) for p, m, s, d in con.execute("SELECT path, mtime_ns, size, data FROM notes")}
        except sqlite3.DatabaseError as e:
            raise VaultError(f"the index cache {self.path} is unreadable ({e}); delete that file and retry") from e

    def save(self, upserts: list[tuple[str, int, int, str]], deletes: list[str]) -> None:
        if not upserts and not deletes:
            return
        try:
            with closing(self._open()) as con:
                con.executemany("INSERT OR REPLACE INTO notes VALUES (?,?,?,?)", upserts)
                con.executemany("DELETE FROM notes WHERE path=?", [(d,) for d in deletes])
                con.commit()
        except sqlite3.DatabaseError as e:
            raise VaultError(f"cannot update the index cache {self.path} ({e}); delete that file and retry") from e


# ---------------------------------------------------------------- the vault


@dataclass
class RefreshStats:
    scanned: int = 0
    parsed: int = 0
    from_cache: int = 0
    removed: int = 0
    changed: bool = False


class Vault:
    K1 = 1.2
    B = 0.75

    def __init__(self, root: str, cache_dir: str | None = None) -> None:
        if not os.path.isdir(root):
            raise VaultError(f"vault directory {root!r} does not exist or is not a directory")
        self.root = os.path.realpath(root)
        self.cache = Cache(cache_dir or default_cache_dir(), self.root)
        self.notes: dict[str, NoteRecord] = {}
        self.attachments: set[str] = set()
        self._loaded = False
        self._graph: Graph | None = None
        self._post: dict[str, list[tuple[str, int]]] | None = None
        self._avgdl = 1.0
        self.last: RefreshStats = RefreshStats()

    # -- scanning

    @staticmethod
    def _is_link(e: os.DirEntry[str]) -> bool:
        if e.is_symlink():
            return True
        return getattr(e.stat(follow_symlinks=False), "st_reparse_tag", 0) in _REPARSE_TAGS

    def _scan(self) -> tuple[dict[str, tuple[int, int]], set[str]]:
        files: dict[str, tuple[int, int]] = {}
        atts: set[str] = set()
        stack = [("", self.root)]
        while stack:
            rel_dir, abs_dir = stack.pop()
            with os.scandir(abs_dir) as it:
                for e in it:
                    name = e.name
                    if name.startswith(".") or self._is_link(e):
                        continue
                    rel = f"{rel_dir}/{name}" if rel_dir else name
                    if e.is_dir(follow_symlinks=False):
                        stack.append((rel, e.path))
                    elif e.is_file(follow_symlinks=False):
                        if name.lower().endswith(".md"):
                            st = e.stat(follow_symlinks=False)
                            files[rel] = (st.st_mtime_ns, st.st_size)
                        else:
                            atts.add(rel)
        return files, atts

    def refresh(self) -> RefreshStats:
        files, atts = self._scan()
        stats = RefreshStats(scanned=len(files))
        first = not self._loaded
        cached = self.cache.load() if first else {}
        upserts: list[tuple[str, int, int, str]] = []
        for rel, (m, s) in files.items():
            rec = self.notes.get(rel)
            if rec is not None and rec.mtime_ns == m and rec.size == s:
                continue
            hit = cached.get(rel)
            if hit is not None and hit[0] == m and hit[1] == s:
                self.notes[rel] = NoteRecord.from_json(rel, m, s, hit[2])
                stats.from_cache += 1
            else:
                with open(os.path.join(self.root, *rel.split("/")), "rb") as f:
                    raw = f.read()
                rec = parse_note(rel, raw, m, s)
                self.notes[rel] = rec
                upserts.append((rel, m, s, rec.to_json()))
                stats.parsed += 1
            stats.changed = True
        gone = [p for p in self.notes if p not in files]
        deletes = gone + [p for p in cached if p not in files and p not in gone]
        for p in gone:
            del self.notes[p]
        stats.removed = len(gone)
        if gone:
            stats.changed = True
        if atts != self.attachments:
            self.attachments = atts
            stats.changed = True
        self.cache.save(upserts, deletes)
        self._loaded = True
        if stats.changed:
            self._graph = None
            self._post = None
        self.last = stats
        return stats

    # -- graph

    @property
    def graph(self) -> Graph:
        if self._graph is None:
            self._graph = Graph(self.notes, self.attachments)
        return self._graph

    def resolve_ref(self, ref: str) -> str:
        """Turn a path, a note name or a [[wikilink]] into exactly one note path, or raise."""
        s = ref.strip()
        if s.startswith("[[") and s.endswith("]]"):
            s = s[2:-2]
        s = s.split("|", 1)[0]
        s = re.split(r"[#^]", s, 1)[0].strip()
        if not s:
            raise VaultError("empty note reference; pass a path like 'Projects/Plan.md' or a name like 'Plan'")
        cands = self.graph.note_candidates(s)
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            listing = "\n".join(f"  - {c}" for c in cands)
            raise VaultError(f"{ref!r} is ambiguous: {len(cands)} notes match. Pass the full path of one of them:\n{listing}")
        import difflib

        stems = {key(r.stem): r.path for r in self.notes.values()}
        near = difflib.get_close_matches(key(s.rsplit("/", 1)[-1]).removesuffix(".md"), list(stems), n=5, cutoff=0.6)
        hint = f" Did you mean: {', '.join(stems[n] for n in near)}?" if near else ""
        raise VaultError(f"no note matches {ref!r}.{hint} Use search_notes to find it, or create_note to make it.")

    def abs_path(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/"))

    # -- search

    def _postings(self) -> dict[str, list[tuple[str, int]]]:
        if self._post is None:
            post: dict[str, list[tuple[str, int]]] = {}
            total = 0
            n = 0
            for p, rec in self.notes.items():
                if rec.error:
                    continue
                n += 1
                total += rec.length
                for term, tf in rec.tf.items():
                    post.setdefault(term, []).append((p, tf))
            self._avgdl = (total / n) if n else 1.0
            self._post = post
        return self._post

    def search(
        self, query: str, *, folder: str | None = None, tag: str | None = None, all_terms: bool = False
    ) -> tuple[list[tuple[str, float]], list[str]]:
        """BM25 over title-boosted text. Returns ([(path, score)] best first, terms found nowhere)."""
        post = self._postings()
        terms = list(dict.fromkeys(tokenize(query)))
        n_docs = sum(1 for r in self.notes.values() if not r.error)
        scores: dict[str, float] = {}
        matched: Counter[str] = Counter()
        missing: list[str] = []
        for t in terms:
            plist = post.get(t)
            if not plist:
                missing.append(t)
                continue
            df = len(plist)
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            for p, tf in plist:
                dl = self.notes[p].length
                scores[p] = scores.get(p, 0.0) + idf * tf * (self.K1 + 1) / (tf + self.K1 * (1 - self.B + self.B * dl / self._avgdl))
                matched[p] += 1
        fk = key(folder.replace("\\", "/").strip("/")) + "/" if folder else None
        tk = norm_tag(tag) if tag else None
        out: list[tuple[str, float]] = []
        for p, sc in scores.items():
            if all_terms and matched[p] < len(terms):
                continue
            if fk and not key(p).startswith(fk):
                continue
            if tk and not any(t == tk or t.startswith(tk + "/") for t in self.notes[p].tags):
                continue
            out.append((p, sc))
        out.sort(key=lambda x: (-x[1], x[0]))
        return out, missing
