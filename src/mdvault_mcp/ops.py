"""The tools' logic. Each function takes a Vault, does the work, returns text for the agent.

Writes follow one rule set: the target must be confined to the vault, nothing is
overwritten without being asked, content is spliced as text (never re-serialised) so
line endings, comments and formatting survive, and files land via temp + os.replace.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import posixpath
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime
from typing import Any
from urllib.parse import quote

from .frontmatter import FrontmatterError, dump_yaml, parse_yaml, split_frontmatter
from .index import Resolved, Vault, norm_tag, strip_md, tokenize
from .links import Link, parse_links
from .query import GRAMMAR, QueryError, matches, parse_query
from .safeio import (
    StagedWrite,
    VaultError,
    atomic_write,
    clean_rel,
    confine,
    detect_eol,
    key,
    make_parent_dirs,
    read_text,
    to_eol,
)


def _more(shown: int, total: int, what: str = "results") -> str:
    return f"\n... {total - shown} more {what} not shown (raise `limit` to see them)." if total > shown else ""


def _clip(s: str, n: int = 200) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def _short_ref(r: Resolved) -> str:
    return r.link.raw if len(r.link.raw) <= 80 else r.link.raw[:77] + "..."


# ------------------------------------------------------------------ overview


def vault_overview(v: Vault) -> str:
    st = v.refresh()
    g = v.graph
    notes = v.notes
    folders = {p.rsplit("/", 1)[0] for p in notes if "/" in p}
    tag_counts: Counter[str] = Counter()
    for r in notes.values():
        tag_counts.update(r.tags)
    bad_broken = [r for rs in g.out.values() for r in rs if r.status == "broken"]
    bad_amb = [r for rs in g.out.values() for r in rs if r.status == "ambiguous"]
    orphans = sorted(
        p
        for p in notes
        if not any(r.src != p for r in g.inc.get(p, []))
        and p not in g.amb_inc  # an ambiguous link may point here
        and not any(r.status in ("note", "attachment") and r.target != p for r in g.out.get(p, []))
    )
    linked = sorted(
        ((len({r.src for r in rs if r.src != p}), p) for p, rs in g.inc.items()),
        key=lambda x: (-x[0], x[1]),
    )
    linked = [x for x in linked if x[0] > 0][:10]
    lines = [
        f"Vault: {v.root}",
        f"{len(notes)} notes in {len(folders) + 1} folders, {sum(r.words for r in notes.values())} words, {len(v.attachments)} other files (images, PDFs, ...)",
        f"Index: {st.parsed} parsed, {st.from_cache} loaded from cache, {st.removed} removed this call",
        "",
    ]
    if tag_counts:
        lines.append("Top tags: " + ", ".join(f"#{t} ({n})" for t, n in tag_counts.most_common(15)))
    else:
        lines.append("Tags: none")
    lines += ["", f"Most linked-to notes ({len(linked)} shown):"]
    lines += [f"  {n:>4} backlinks  {p}" for n, p in linked] or ["  none"]
    lines += ["", f"Orphans (no links in or out; {len(orphans)}):"]
    lines += [f"  {p}" for p in orphans[:10]] or ["  none"]
    if len(orphans) > 10:
        lines.append(_more(10, len(orphans), "orphans").strip())
    lines += ["", f"Broken links: {len(bad_broken)}" + (" (use broken_links for the full list)" if bad_broken else "")]
    lines += [f"  {r.src}:{r.link.line}  {_short_ref(r)}" for r in bad_broken[:5]]
    lines.append(f"Ambiguous links (several notes share the name): {len(bad_amb)}" + (" (use broken_links)" if bad_amb else ""))
    lines += [f"  {r.src}:{r.link.line}  {_short_ref(r)}  -> {', '.join(r.candidates[:3])}" for r in bad_amb[:5]]
    fm_err = [(p, r.fm_error) for p, r in sorted(notes.items()) if r.fm_error]
    if fm_err:
        lines += ["", f"Notes with unparseable frontmatter ({len(fm_err)}):"]
        lines += [f"  {p}: {e}" for p, e in fm_err[:10]]
    bad_files = [(p, r.error) for p, r in sorted(notes.items()) if r.error]
    if bad_files:
        lines += ["", f"Unreadable notes ({len(bad_files)}):"]
        lines += [f"  {p}: {e}" for p, e in bad_files[:10]]
    return "\n".join(lines)


# ------------------------------------------------------------------ search


def _snippet(v: Vault, path: str, terms: set[str]) -> str:
    text = read_text(v.abs_path(path)).text
    best = ("", 0, 0)  # line, hits, line number
    for i, line in enumerate(text.split("\n"), 1):
        hits = len({t for t in tokenize(line) if t in terms})
        if hits > best[1]:
            best = (line.strip(), hits, i)
    line, hits, no = best
    if not hits:
        return ""
    first = next((m.start() for m in re.finditer(r"\w+", line) if tokenize(m.group())[0] in terms), 0)
    start = max(0, first - 60)
    clip = line[start : start + 180]
    return f"L{no}: {'...' if start else ''}{clip}{'...' if start + 180 < len(line) else ''}"


def search_notes(v: Vault, query: str, limit: int, folder: str | None, tag: str | None, all_terms: bool) -> str:
    if not tokenize(query):
        raise VaultError("the query has no searchable words")
    v.refresh()
    hits, missing = v.search(query, folder=folder, tag=tag, all_terms=all_terms)
    terms = set(tokenize(query))
    if not hits:
        extra = f" Words found in no note: {', '.join(missing)}." if missing else ""
        return f"No notes match {query!r}.{extra}"
    lines = [f"{len(hits)} notes match {query!r} (best first):"]
    for p, sc in hits[:limit]:
        r = v.notes[p]
        lines.append(f"{sc:6.2f}  {p}  --  {_clip(r.title, 80)}")
        s = _snippet(v, p, terms)
        if s:
            lines.append(f"        {s}")
    out = "\n".join(lines) + _more(min(limit, len(hits)), len(hits), "notes")
    if missing:
        out += f"\nNote: no note contains {', '.join(missing)}."
    return out


# ------------------------------------------------------------------ read


def read_note(v: Vault, note: str, raw: bool, max_chars: int) -> str:
    v.refresh()
    p = v.resolve_ref(note)
    rec = v.notes[p]
    ft = read_text(v.abs_path(p), p)
    text = ft.text.replace("\r\n", "\n")
    g = v.graph
    outs = g.out.get(p, [])
    n_back = len({r.src for r in g.inc.get(p, []) if r.src != p})
    head = [
        f"path: {p}",
        f"title: {rec.title}",
        f"tags: {', '.join('#' + t for t in rec.tags) or 'none'}",
        f"links out: {len(outs)} ({sum(1 for r in outs if r.status == 'broken')} broken, {sum(1 for r in outs if r.status == 'ambiguous')} ambiguous); backlinks: {n_back} notes",
        f"line endings: {'CRLF' if detect_eol(ft.text) == chr(13) + chr(10) else 'LF'}",
    ]
    if raw:
        body = text
    else:
        fm_raw, off = split_frontmatter(ft.text)
        body = ft.text[off:].replace("\r\n", "\n")
        if rec.fm_error:
            head.append(f"frontmatter: UNPARSEABLE ({rec.fm_error}); pass raw=true to see it")
        elif fm_raw is not None:
            head.append("frontmatter: " + json.dumps(rec.fm, ensure_ascii=False, default=str))
        else:
            head.append("frontmatter: none")
    trunc = ""
    if len(body) > max_chars:
        trunc = f"\n\n[truncated: showing {max_chars} of {len(body)} characters; raise max_chars to see more]"
        body = body[:max_chars]
    return "\n".join(head) + "\n--- " + ("raw file" if raw else "content") + " ---\n" + body + trunc


# ------------------------------------------------------------------ links


def note_links(v: Vault, note: str, direction: str, limit: int) -> str:
    if direction not in ("in", "out", "both"):
        raise VaultError("direction must be 'in' (backlinks), 'out' (outgoing links) or 'both'")
    v.refresh()
    p = v.resolve_ref(note)
    g = v.graph
    lines: list[str] = []
    if direction in ("in", "both"):
        inc = sorted((r for r in g.inc.get(p, []) if r.src != p), key=lambda r: (r.src, r.link.line))
        amb = sorted(g.amb_inc.get(p, []), key=lambda r: (r.src, r.link.line))
        srcs = len({r.src for r in inc})
        lines.append(f"Backlinks to {p}: {len(inc)} links from {srcs} notes")
        per_line: dict[tuple[str, int], list[Resolved]] = {}
        for r in inc:
            per_line.setdefault((r.src, r.link.line), []).append(r)
        for (src, ln), group in list(per_line.items())[:limit]:
            tag = " [" + str(len(group)) + " links]" if len(group) > 1 else ""
            embed = "(embed) " if group[0].link.embed else ""
            lines.append(f"  {src}:{ln}{tag}  {embed}{_clip(group[0].context)}")
        if len(per_line) > limit:
            lines.append(_more(limit, len(per_line), "lines").strip())
        if amb:
            lines.append(f"  Ambiguous links that might point here ({len(amb)}; several notes share the name):")
            for r in amb[:limit]:
                lines.append(f"    {r.src}:{r.link.line}  {_short_ref(r)}  candidates: {', '.join(r.candidates)}")
    if direction in ("out", "both"):
        outs = g.out.get(p, [])
        lines.append(f"Outgoing links from {p}: {len(outs)}")
        for r in outs[:limit]:
            if r.status in ("note", "attachment"):
                dest = f"-> {r.target}" + (f" {r.link.fragment}" if r.link.fragment else "")
            elif r.status == "ambiguous":
                dest = f"-> AMBIGUOUS, could be: {', '.join(r.candidates)}"
            else:
                dest = "-> BROKEN (no such note or file)"
            lines.append(f"  L{r.link.line}  {'(embed) ' if r.link.embed else ''}{_short_ref(r)}  {dest}")
        if len(outs) > limit:
            lines.append(_more(limit, len(outs), "links").strip())
    return "\n".join(x for x in lines if x)


def broken_links(v: Vault, folder: str | None, limit: int) -> str:
    v.refresh()
    g = v.graph
    fk = key(folder.replace("\\", "/").strip("/")) + "/" if folder else None
    bad = [
        r
        for src, rs in sorted(g.out.items())
        if not fk or key(src).startswith(fk)
        for r in rs
        if r.status in ("broken", "ambiguous")
    ]
    if not bad:
        return "No broken or ambiguous links." + (f" (in {folder})" if folder else "")
    import difflib as dl

    stems = {key(r.stem): r.path for r in v.notes.values()}
    nb = sum(1 for r in bad if r.status == "broken")
    lines = [f"{nb} broken and {len(bad) - nb} ambiguous links:"]
    for r in bad[:limit]:
        if r.status == "ambiguous":
            lines.append(f"  AMBIGUOUS {r.src}:{r.link.line}  {_short_ref(r)}  could be: {', '.join(r.candidates)}")
        else:
            near = dl.get_close_matches(key(strip_md(r.link.target.rsplit("/", 1)[-1])), list(stems), n=2, cutoff=0.7)
            hint = f"  did you mean {', '.join(stems[n] for n in near)}?" if near else ""
            lines.append(f"  BROKEN    {r.src}:{r.link.line}  {_short_ref(r)}{hint}")
    return "\n".join(lines) + _more(min(limit, len(bad)), len(bad), "links")


# ------------------------------------------------------------------ query


def query_notes(v: Vault, where: str | None, tag: str | None, folder: str | None, limit: int) -> str:
    if not where and not tag:
        raise VaultError("pass `where` (a frontmatter query) and/or `tag`. " + GRAMMAR)
    try:
        groups = parse_query(where) if where else None
    except QueryError as e:
        raise VaultError(str(e)) from e
    v.refresh()
    fk = key(folder.replace("\\", "/").strip("/")) + "/" if folder else None
    tk = norm_tag(tag) if tag else None
    hits: list[str] = []
    for p, r in sorted(v.notes.items()):
        if fk and not key(p).startswith(fk):
            continue
        if tk and not any(t == tk or t.startswith(tk + "/") for t in r.tags):
            continue
        if groups is not None and not matches(groups, r.fm or {}, r.fm_tags):
            continue
        hits.append(p)
    desc = " AND ".join(x for x in (f"tag #{tk} (nested tags included)" if tk else "", f"where {where}" if where else "", f"in {folder}" if folder else "") if x)
    if not hits:
        bad = sum(1 for r in v.notes.values() if r.fm_error)
        warn = f" ({bad} notes have unparseable frontmatter and are skipped; see vault_overview)" if bad and where else ""
        return f"No notes match {desc}.{warn}"
    lines = [f"{len(hits)} notes match {desc}:"]
    for p in hits[:limit]:
        r = v.notes[p]
        lines.append(f"  {p}  --  {_clip(r.title, 60)}  [{', '.join('#' + t for t in r.tags[:6])}]")
    return "\n".join(lines) + _more(min(limit, len(hits)), len(hits), "notes")


# ------------------------------------------------------------------ create / append


def _note_rel(name: str) -> str:
    rel = clean_rel(name, what="note path")
    return rel if rel.lower().endswith(".md") else rel + ".md"


def _same_name_warning(v: Vault, rel: str, exclude: str | None = None) -> str:
    stem = key(strip_md(rel).rsplit("/", 1)[-1])
    twins = [p for p in v.graph.stem_map.get(stem, []) if p != rel and p != exclude]
    if twins:
        return f"\nWarning: other notes share this name ({', '.join(twins)}); a plain [[{strip_md(rel).rsplit('/', 1)[-1]}]] link is now ambiguous, link with the folder path instead."
    return ""


def create_note(v: Vault, path: str, content: str, frontmatter: dict[str, Any] | None, overwrite: bool) -> str:
    rel = _note_rel(path)
    target = confine(v.root, rel)
    v.refresh()
    existed = os.path.isfile(target)
    if os.path.exists(target) and not existed:
        raise VaultError(f"{rel} exists and is not a regular file")
    if existed and not overwrite:
        raise VaultError(f"{rel} already exists. Pass overwrite=true to replace it, or use append_to_note / edit_note to change it.")
    if not existed and key(rel) in v.graph.full:
        raise VaultError(f"a file with the same name but different letter case already exists ({v.graph.full[key(rel)][0]}); choose another name")
    eol = detect_eol(read_text(target).text) if existed else "\n"
    body = content
    if frontmatter is not None:
        if content.lstrip().startswith("---"):
            raise VaultError("content already starts with a frontmatter block; pass either `frontmatter` or put it in `content`, not both")
        try:
            fm_text = dump_yaml(frontmatter)
        except FrontmatterError as e:
            raise VaultError(f"cannot write frontmatter: {e}") from e
        body = "---\n" + (fm_text + "\n" if fm_text else "") + "---\n" + content
    elif content.startswith("---"):
        fm_raw, _ = split_frontmatter(content.replace("\r\n", "\n"))
        if fm_raw is not None:
            try:
                parse_yaml(fm_raw)
            except FrontmatterError as e:
                raise VaultError(f"the frontmatter in `content` is not valid: {e}") from e
    if body and not body.endswith("\n"):
        body += "\n"
    make_parent_dirs(v.root, rel)
    atomic_write(target, to_eol(body, eol).encode("utf-8"))
    v.refresh()
    return f"{'Replaced' if existed else 'Created'} {rel} ({len(body)} characters)." + _same_name_warning(v, rel)


def daily_path(daily_folder: str, date_format: str, day: str | None) -> str:
    try:
        d = _date.fromisoformat(day) if day else datetime.now().date()
    except ValueError as e:
        raise VaultError(f"date must be YYYY-MM-DD, got {day!r}") from e
    try:
        name = d.strftime(date_format)
    except ValueError as e:
        raise VaultError(f"bad date_format {date_format!r}: {e}") from e
    folder = daily_folder.strip().strip("/\\")
    return _note_rel(f"{folder}/{name}" if folder else name)


def append_to_note(
    v: Vault, text: str, note: str | None, daily: bool, day: str | None, daily_folder: str, date_format: str
) -> str:
    if daily == (note is not None):
        raise VaultError("pass exactly one of `note` (an existing note) or daily=true (today's daily note)")
    if not text:
        raise VaultError("`text` is empty; nothing to append")
    v.refresh()
    created = False
    if daily:
        rel = daily_path(daily_folder, date_format, day)
        target = confine(v.root, rel)
        if not os.path.exists(target):
            make_parent_dirs(v.root, rel)
            atomic_write(target, b"")
            created = True
        elif key(rel) not in v.graph.full:
            raise VaultError(f"{rel} exists but is not a note")
        else:
            rel = v.graph.full[key(rel)][0]
            target = v.abs_path(rel)
    else:
        assert note is not None
        rel = v.resolve_ref(note)
        target = confine(v.root, rel)
    ft = read_text(target, rel)
    eol = detect_eol(ft.text)
    cur = ft.text
    add = to_eol(text, eol)
    if cur and not cur.endswith("\n"):
        add = eol + add
    if not add.endswith("\n"):
        add += eol
    ft.text = cur + add
    atomic_write(target, ft.encode())
    v.refresh()
    n = ft.text.count("\n")
    return f"{'Created and appended to' if created else 'Appended to'} {rel}: {text.count(chr(10)) + 1} line(s) added, file now has {n} lines."


# ------------------------------------------------------------------ edit


def edit_note(v: Vault, note: str, old_text: str, new_text: str, dry_run: bool) -> str:
    if old_text == "":
        raise VaultError("old_text is empty; give the exact text to replace (it must occur exactly once)")
    v.refresh()
    p = v.resolve_ref(note)
    target = confine(v.root, p)
    ft = read_text(target, p)
    eol = detect_eol(ft.text)
    old = to_eol(old_text, eol)
    new = to_eol(new_text, eol)
    n = ft.text.count(old)
    if n == 0:
        first = old.split(eol)[0].strip()
        hint = ""
        if first and first in ft.text:
            ln = ft.text[: ft.text.index(first)].count("\n") + 1
            hint = f" The first line of old_text does appear at line {ln}, so the rest differs (whitespace, line breaks or a stale read); call read_note again."
        raise VaultError(f"old_text was not found in {p}.{hint}")
    if n > 1:
        lines = sorted({ft.text[: m.start()].count("\n") + 1 for m in re.finditer(re.escape(old), ft.text)})
        raise VaultError(f"old_text matches {n} places in {p} (lines {', '.join(map(str, lines[:10]))}); include more surrounding text so it matches exactly once")
    idx = ft.text.index(old)
    updated = ft.text[:idx] + new + ft.text[idx + len(old) :]
    # An edit must not turn readable frontmatter into broken frontmatter.
    before_rec = v.notes[p]
    fm_raw, _ = split_frontmatter(updated)
    if fm_raw is not None and before_rec.fm_error is None:
        try:
            parse_yaml(fm_raw)
        except FrontmatterError as e:
            raise VaultError(f"this edit would break the frontmatter of {p} ({e}); not applied") from e
    line = ft.text[:idx].count("\n") + 1
    if dry_run:
        diff = "".join(
            difflib.unified_diff(
                ft.text.splitlines(True), updated.splitlines(True), f"a/{p}", f"b/{p}", n=2
            )
        )
        return f"DRY RUN, nothing written. Replacing 1 occurrence at line {line} of {p}:\n{diff.replace(chr(13), '')}"
    ft.text = updated
    atomic_write(target, ft.encode())
    v.refresh()
    return f"Edited {p}: replaced 1 occurrence at line {line} ({len(old)} -> {len(new)} characters)."


# ------------------------------------------------------------------ rename


@dataclass
class _Edit:
    start: int
    end: int
    new: str
    line: int
    old_line: str
    new_line: str


@dataclass
class _FilePlan:
    src: str
    original: bytes
    text: Any  # FileText
    edits: list[_Edit] = field(default_factory=list)


def _encode_dest(dest: str, original_raw: str, angle: bool) -> str:
    if angle:
        return dest
    if "%" in original_raw:
        return quote(dest, safe="/.-_~")
    return dest.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _new_wiki_target(lk: Link, new_rel: str, unique_stem: bool) -> str:
    stem = new_rel.rsplit("/", 1)[-1][:-3]
    nt = stem if ("/" not in lk.target and unique_stem) else strip_md(new_rel)
    return nt + ".md" if lk.target.lower().endswith(".md") else nt


def _new_md_dest(lk: Link, raw: str, target_new: str, src_new: str) -> str:
    if lk.target.startswith("/"):
        nd = "/" + target_new
    else:
        nd = posixpath.relpath(target_new, start=posixpath.dirname(src_new) or ".")
        if lk.target.startswith("./") and not nd.startswith("../"):
            nd = "./" + nd
    if not lk.target.lower().endswith(".md") and nd.lower().endswith(".md"):
        nd = nd[:-3]
    return _encode_dest(nd, raw, lk.angle)


def _line_bounds(text: str, pos: int) -> tuple[int, int]:
    s = text.rfind("\n", 0, pos) + 1
    e = text.find("\n", pos)
    return s, len(text) if e == -1 else e


def _plan_file(v: Vault, src: str, old: str, new_rel: str, unique_stem: bool) -> _FilePlan:
    g = v.graph
    path = v.abs_path(src)
    with open(path, "rb") as f:
        raw = f.read()
    ft = read_text(path, src)
    plan = _FilePlan(src, raw, ft)
    text = ft.text
    src_new = new_rel if src == old else src
    for lk in parse_links(text):
        r = g.resolve(lk, src)
        if r.status not in ("note", "attachment") or r.target is None:
            continue
        orig = text[lk.tstart : lk.tend]
        new_val: str | None = None
        if r.target == old and r.status == "note":
            if lk.kind == "wiki":
                if lk.target == "":
                    continue
                new_val = _new_wiki_target(lk, new_rel, unique_stem)
            else:
                new_val = _new_md_dest(lk, orig, new_rel, src_new)
        elif src == old and lk.kind == "md" and posixpath.dirname(new_rel) != posixpath.dirname(old):
            new_val = _new_md_dest(lk, orig, r.target, src_new)  # the moved note's own relative links
        if new_val is None or new_val == orig:
            continue
        s, e = _line_bounds(text, lk.tstart)
        plan.edits.append(
            _Edit(lk.tstart, lk.tend, new_val, lk.line, text[s:e].strip(), (text[s : lk.tstart] + new_val + text[lk.tend : e]).strip())
        )
    return plan


def _count_bad(v: Vault) -> int:
    return sum(1 for rs in v.graph.out.values() for r in rs if r.status in ("broken", "ambiguous"))


def rename_note(v: Vault, note: str, new_name: str, dry_run: bool) -> str:
    v.refresh()
    old = v.resolve_ref(note)
    g = v.graph
    nn = new_name.replace("\\", "/").strip()
    if not nn:
        raise VaultError("new_name is empty")
    new_rel = _note_rel(nn if "/" in nn else (old.rsplit("/", 1)[0] + "/" + nn if "/" in old else nn))
    if new_rel == old:
        raise VaultError(f"{old} already has that name; nothing to do")
    confine(v.root, new_rel)
    old_abs, new_abs = v.abs_path(old), v.abs_path(new_rel)
    case_only = key(new_rel) == key(old)
    if case_only:
        if os.path.lexists(new_abs) and not os.path.samefile(old_abs, new_abs):
            raise VaultError(f"{new_rel} already exists as a different file")
    elif key(new_rel) in g.full or os.path.lexists(new_abs):
        raise VaultError(f"{new_rel} already exists; rename never overwrites. Pick another name or move/delete that note first.")
    new_stem = new_rel.rsplit("/", 1)[-1][:-3]
    others = [p for p in g.stem_map.get(key(new_stem), []) if p != old]
    warnings: list[str] = []
    if others:
        clash = [r for o in others for r in g.inc.get(o, []) if "/" not in r.link.target]
        if clash:
            sample = "; ".join(f"{r.src}:{r.link.line} {_short_ref(r)}" for r in clash[:3])
            raise VaultError(
                f"renaming to {new_rel} would make {len(clash)} existing link(s) to {others[0]} ambiguous (two notes would share the name {new_stem!r}), e.g. {sample}. Choose a different name."
            )
        if "/" not in new_rel and g.inc.get(old):
            raise VaultError(
                f"another note is already named {new_stem!r} ({', '.join(others)}), and a note in the vault root can only be linked by its bare name, which would be ambiguous. Move it into a folder (new_name='Folder/{new_stem}') or pick a different name."
            )
        warnings.append(f"another note is already named {new_stem!r} ({', '.join(others)}); plain [[{new_stem}]] links to either are ambiguous, so links to this note are written with the folder path.")
    unique_stem = not others

    srcs = {r.src for r in g.inc.get(old, [])} | {old}
    plans = [_plan_file(v, s, old, new_rel, unique_stem) for s in sorted(srcs)]
    changed = [p for p in plans if p.edits]
    skipped = sorted(g.amb_inc.get(old, []), key=lambda r: (r.src, r.link.line))
    n_links = sum(len(p.edits) for p in changed)
    bad_before = _count_bad(v)

    def render(prefix: str) -> str:
        out = [f"{prefix}{old} -> {new_rel}"]
        out.append(f"{n_links} link(s) in {len(changed)} file(s) {'would be' if dry_run else 'were'} updated:" if changed else "No links need updating.")
        for p in changed:
            out.append(f"  {p.src}{' (the renamed note itself)' if p.src == old else ''}  ({len(p.edits)} link{'s' if len(p.edits) != 1 else ''})")
            for ed in p.edits[:3]:
                out.append(f"      L{ed.line}: {_clip(ed.old_line, 110)}")
                out.append(f"       -> {_clip(ed.new_line, 110)}")
            if len(p.edits) > 3:
                out.append(f"      ... and {len(p.edits) - 3} more in this file")
        if skipped:
            out.append(f"NOT updated: {len(skipped)} ambiguous link(s) that might refer to this note (several notes share the name); fix them by hand:")
            out += [f"  {r.src}:{r.link.line}  {_short_ref(r)}  candidates: {', '.join(r.candidates)}" for r in skipped[:20]]
        out += [f"Warning: {w}" for w in warnings]
        return "\n".join(out)

    if dry_run:
        return render("DRY RUN, nothing written.\nRename: ") + "\nCall again with dry_run=false to apply."

    # Apply: re-verify every file is untouched since planning, stage all temp files (so a
    # disk-full/permission error happens before anything moves), then replace, then rename.
    for p in plans:
        with open(v.abs_path(p.src), "rb") as f:
            if hashlib.sha256(f.read()).digest() != hashlib.sha256(p.original).digest():
                raise VaultError(f"{p.src} changed while the rename was being planned; nothing was written. Try again.")
    staged: list[StagedWrite] = []
    try:
        for p in changed:
            text = p.text.text
            for ed in sorted(p.edits, key=lambda e: -e.start):
                text = text[: ed.start] + ed.new + text[ed.end :]
            p.text.text = text
            staged.append(StagedWrite(v.abs_path(p.src), p.text.encode()))
        make_parent_dirs(v.root, new_rel)
    except BaseException:
        for s in staged:
            s.discard()
        raise
    done: list[str] = []
    try:
        for s in staged:
            s.commit()
            done.append(os.path.relpath(s.path, v.root))
        os.rename(old_abs, new_abs)
    except OSError as e:
        for s in staged:
            s.discard()
        raise VaultError(
            f"rename failed part-way ({e}). Files already updated to point at the new name: {', '.join(done) or 'none'}. The note is still at {old}. Retry the rename, or run broken_links to see what is affected."
        ) from e
    v.refresh()
    bad_after = _count_bad(v)
    tail = f"\nBroken+ambiguous links in the vault: {bad_before} before, {bad_after} after."
    if bad_after > bad_before:
        tail += " WARNING: the rename left more unresolved links than before; run broken_links."
    return render("Renamed: ") + tail
