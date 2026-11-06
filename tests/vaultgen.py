"""Generate realistic markdown vaults for tests (small hand-made one + a big random one)."""

from __future__ import annotations

import os
import random

WORDS = (
    "project plan idea review meeting budget research design network kernel memory scheduler "
    "draft summary roadmap sprint backlog release notes garden recipe travel reading book "
    "habit health workout journal finance invoice client server deploy testing bug feature "
    "graph vector parser index cache search query markdown vault obsidian logseq daily weekly"
).split()
TURKISH = "şeker çiçek ağaç ışık öğrenci üzüm İstanbul Ankara göl dağ yağmur güneş kitap defter".split()
FOLDERS = ["Projects", "Areas", "Resources", "People", "Archive", "Projects/Alpha", "Projects/Beta", "Areas/Health", "Turkce"]


def write(root: str, rel: str, text: str, newline: str = "\n") -> None:
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(text.replace("\n", newline).encode("utf-8"))


def read(root: str, rel: str) -> bytes:
    with open(os.path.join(root, *rel.split("/")), "rb") as f:
        return f.read()


def make_small_vault(root: str) -> None:
    """A hand-built vault that exercises every link form, ambiguity, Turkish names and CRLF."""
    write(root, "Home.md", """---
title: Home
tags: [index, project]
status: active
---
# Home

Welcome. See [[Projects/Alpha/Plan]] and [[Beta Plan|the beta]].
Also [[Meeting Notes]] (ambiguous!) and [[Şeker Notu]] plus [[şeker notu#Tarif]].
Embed: ![[diagram.png]] and ![[Alpha Plan]].
Broken: [[Does Not Exist]]. #inbox
""")
    write(root, "Projects/Alpha/Alpha Plan.md", """---
status: draft          # still thinking
tags:
  - project
  - project/alpha
owner: fadi
due: 2026-12-01
# a comment line that must survive
---
# Alpha Plan

Goal text with a block. ^goalblock

Links: [[Home]], [[Alpha Plan#Goal]], [[Beta Plan#^b1|beta block]], [[Meeting Notes]].
Markdown: [home](../../Home.md), [beta](../Beta/Beta%20Plan.md#top), ![pic](../../diagram.png).

```
[[Not A Link In Code]] and [x](nothing.md)
```
Inline `[[also not]]` code.
""")
    write(root, "Projects/Beta/Beta Plan.md", """---
status: published
tags: project
---
# Beta Plan

^b1 paragraph with id.
Back to [[Alpha Plan]] and [alpha](../Alpha/Alpha%20Plan.md).
Tag #beta/launch and #2024 (not a tag).
""", newline="\r\n")
    write(root, "Projects/Meeting Notes.md", "# Meeting Notes (projects)\n\nStandup with [[Home]].\n")
    write(root, "Areas/Meeting Notes.md", "# Meeting Notes (areas)\n\nNothing links here uniquely.\n")
    write(root, "Turkce/Şeker Notu.md", "---\ntags: [yemek]\n---\n# Şeker Notu\n\n## Tarif\n\nÇiçek şeker ve üzüm. [[Home]]\n")
    write(root, "Orphan.md", "# Lonely\n\nNo links at all.\n")
    write(root, "Broken FM.md", "---\nkey: [unclosed\n---\n# Broken FM\n")
    write(root, "diagram.png", "not really a png")
    write(root, ".obsidian/app.json", "{}")
    write(root, ".git/config", "[core]\n")


def make_big_vault(root: str, n: int = 5000, seed: int = 7) -> list[str]:
    """n random notes with realistic structure. Returns the list of note paths."""
    rng = random.Random(seed)
    paths: list[str] = []
    names: list[str] = []
    for i in range(n):
        folder = rng.choice(FOLDERS)
        w = rng.choice(WORDS).capitalize()
        name = f"{w} {rng.choice(WORDS)} {i}"
        if rng.random() < 0.02:
            name = f"{rng.choice(TURKISH)} {i}"
        paths.append(f"{folder}/{name}.md")
        names.append(name)
    for i, p in enumerate(paths):
        words = [rng.choice(WORDS + TURKISH) for _ in range(rng.randint(80, 260))]
        body_lines: list[str] = [f"# {names[i]}", ""]
        for _ in range(rng.randint(2, 6)):
            chunk = " ".join(rng.choice(WORDS + TURKISH) for _ in range(rng.randint(10, 40)))
            body_lines.append(chunk)
            if rng.random() < 0.6:
                t = rng.randrange(n)
                form = rng.choice(["[[{n}]]", "[[{n}|see this]]", "[[{n}#Heading]]", "![[{n}]]", "[[{f}/{n}]]"])
                folder = paths[t].rsplit("/", 1)[0]
                body_lines.append("See " + form.format(n=names[t], f=folder))
            body_lines.append("")
        if rng.random() < 0.05:
            body_lines.append("Broken [[Missing note " + str(i) + "]]")
        tags = rng.sample(["project", "idea", "area", "person", "log", "ref"], rng.randint(0, 3))
        fm = ["---", f"status: {rng.choice(['draft', 'active', 'done'])}", f"priority: {rng.randint(1, 5)}"]
        if tags:
            fm.append("tags: [" + ", ".join(tags) + "]")
        fm.append("---")
        text = "\n".join(fm + body_lines + [" ".join(words), ""])
        write(root, p, text, newline="\r\n" if rng.random() < 0.1 else "\n")
    return paths
