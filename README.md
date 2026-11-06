# mdvault-mcp

**Let your AI work with a folder of markdown notes the way you do: search it, follow the links, query the properties, and refactor it without breaking anything.**

<!-- mcp-name: io.github.0xmortuex/mdvault-mcp -->

mdvault-mcp is an [MCP](https://modelcontextprotocol.io) server for an Obsidian-style vault, a Logseq pages folder, or any directory of `.md` files. It does not need Obsidian running and talks to the files directly.

**Why another notes server?** Several Obsidian MCP servers exist already (for example [StevenStavrakis/obsidian-mcp](https://github.com/StevenStavrakis/obsidian-mcp), [laurentiudanielgi-coder/obsidian-mcp](https://github.com/laurentiudanielgi-coder/obsidian-mcp), [farmerajf/obsidian-mcp-server](https://github.com/farmerajf/obsidian-mcp-server)), and some of them already update links on rename. This one is built around *not damaging your notes*, and says so with tests:

- **Rename is a reviewable refactor.** `rename_note` defaults to a dry run that lists every file and every link line it would change, then reports broken links before and after. Its tests re-parse the whole vault in a fresh index after each rename and check that every link still resolves to the same note.
- **Ambiguity is reported, never guessed.** Two notes called `Meeting Notes` in different folders make `[[Meeting Notes]]` ambiguous. You get both candidate paths, and ambiguous links are left alone by renames.
- **Byte-exact edits.** Line endings (CRLF stays CRLF), BOM, frontmatter comments and formatting are preserved. Edits are text splices, never a re-serialised file.
- **Confined, atomic, no surprise overwrites.** Symlink and Windows junction escapes are blocked (tested), dot-folders like `.git` and `.obsidian` are never touched, files are written via temp file + `os.replace`, and nothing is overwritten without `overwrite=true`.
- **Fast on big vaults.** In-process BM25 search; parsed notes are cached in SQLite (outside your vault) keyed by mtime. Tested on a generated 5,000-note vault.
- **Turkish-friendly.** `seker` finds `Şeker`; links are case-insensitive and Unicode-normalised.

Python, one dependency (`mcp`), runs with `uvx`.

## What it looks like

Real output from the test vault:

```
> rename_note "Beta Plan" -> "Beta Roadmap"          (dry_run defaults to true)
DRY RUN, nothing written.
Rename: Projects/Beta/Beta Plan.md -> Projects/Beta/Beta Roadmap.md
3 link(s) in 2 file(s) would be updated:
  Home.md  (1 link)
      L8: Welcome. See [[Projects/Alpha/Plan]] and [[Beta Plan|the beta]].
       -> Welcome. See [[Projects/Alpha/Plan]] and [[Beta Roadmap|the beta]].
  Projects/Alpha/Alpha Plan.md  (2 links)
      L14: Links: [[Home]], [[Alpha Plan#Goal]], [[Beta Plan#^b1|beta block]], [[Meeting Notes]].
       -> Links: [[Home]], [[Alpha Plan#Goal]], [[Beta Roadmap#^b1|beta block]], [[Meeting Notes]].
      L15: Markdown: [home](../../Home.md), [beta](../Beta/Beta%20Plan.md#top), ...
       -> Markdown: [home](../../Home.md), [beta](../Beta/Beta%20Roadmap.md#top), ...
Call again with dry_run=false to apply.

> read_note "Meeting Notes"
'Meeting Notes' is ambiguous: 2 notes match. Pass the full path of one of them:
  - Areas/Meeting Notes.md
  - Projects/Meeting Notes.md

> note_links "Alpha Plan" direction=in
Backlinks to Projects/Alpha/Alpha Plan.md: 3 links from 2 notes
  Home.md:10  (embed) Embed: ![[diagram.png]] and ![[Alpha Plan]].
  Projects/Beta/Beta Plan.md:8 [2 links]  Back to [[Alpha Plan]] and [alpha](../Alpha/Alpha%20Plan.md).
```

## Install

You need [uv](https://docs.astral.sh/uv/). Point the server at your notes folder.

**Claude Code**

```
claude mcp add notes -- uvx mdvault-mcp --vault "C:\Users\you\Documents\Notes"
```

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "notes": {
      "command": "uvx",
      "args": ["mdvault-mcp", "--vault", "C:\\Users\\you\\Documents\\Notes"]
    }
  }
}
```

Add `"--read-only"` to the args to disable every writing tool. `MDVAULT_PATH` and `MDVAULT_READ_ONLY=1` work instead of the flags. `MDVAULT_CACHE_DIR` moves the index cache (default: `%LOCALAPPDATA%\mdvault-mcp`, `~/Library/Caches/mdvault-mcp` or `~/.cache/mdvault-mcp`).

## Tools

| Tool | What it does |
|------|--------------|
| `vault_overview` | Note count, top tags, most linked notes, orphans, broken and ambiguous links, notes with bad frontmatter. |
| `search_notes` | BM25-ranked full text with a snippet and line number. Optional `folder`, `tag`, `all_terms`. |
| `read_note` | By path, name or `[[wikilink]]`: parsed frontmatter, tags, link counts, body (or `raw=true`). |
| `note_links` | Backlinks (with the surrounding line) and/or outgoing links with where each resolves. |
| `query_notes` | Tag (nested tags included) and frontmatter queries: `status=draft AND tags contains project`. |
| `broken_links` | Broken links with did-you-mean, and ambiguous links with all candidates. |
| `create_note` | New note with optional frontmatter; refuses to overwrite; warns about duplicate names. |
| `append_to_note` | Append to a note, or to today's daily note (`daily=true`, created if missing). |
| `edit_note` | Exact-text replace that must match once; `dry_run` shows a diff. |
| `rename_note` | Rename or move and update every link; dry run by default. |

Links understood: `[[Note]]`, `[[Note#Heading]]`, `[[Note#^block]]`, `[[Note|alias]]`, `![[embed]]`, `[[Folder/Note]]`, `[text](path/Note.md#h)` (relative, `/rooted`, `%20`-encoded, `<angle>`). Links inside code fences and inline code are ignored.

## Try asking

- "Give me an overview of my vault. What is broken or orphaned?"
- "Search my notes for kernel scheduler and show me the best match."
- "Which notes link to my Roadmap note, and what do they say around the link?"
- "List every note with status=draft and the tag project, due before December."
- "Rename 'Q3 Plan' to 'Q4 Plan'. Show me the dry run first."
- "Add '- call the dentist' to today's daily note."
- "In the 'Alpha Plan' note, change the owner line to 'owner: fadi'."
- "Are there two notes with the same name? Which links are ambiguous?"

## Security

The server reads and writes files, so the limits are deliberate:

- **One folder.** Every path is resolved against the vault root. `..`, absolute paths, drive letters, and any component starting with `.` (`.git`, `.obsidian`, `.trash`) are refused. Symlinks and Windows junctions are never followed when scanning, and any write path that passes through one is refused, even when it points back inside the vault.
- **No silent overwrite.** `create_note` needs `overwrite=true`; `rename_note` never overwrites; `edit_note` needs the old text to match exactly once.
- **Atomic.** Each file is written to a temp file in its folder, fsynced, then moved into place with `os.replace`. A rename stages every changed file first, re-checks that none changed while planning, and only then moves anything.
- **Not UTF-8? Untouched.** A note that is not valid UTF-8 is reported and never rewritten.
- **File names stay portable.** New names with characters Windows rejects (`: ? * | " < >`), reserved device names or trailing dots are refused.
- `--read-only` removes the ability to write at all.

The index cache lives outside the vault and holds parsed note text (words and link positions), so treat it like your notes.

## Limitations

- Reference-style links (`[x]: path`), raw HTML links, Logseq `key:: value` properties and `((block-uuid))` references are not parsed.
- Frontmatter is a practical YAML subset: maps, lists, flow collections, quoted strings, numbers, booleans, `|` and `>` blocks. Anchors, tags and multi-line plain scalars are reported as errors on that note (it is still searchable and linkable).
- Frontmatter `aliases` are not used to resolve links (Obsidian does not resolve them either).
- A rename rewrites the link target only. If a link's visible text was the old name (`[[Old]]`), it now shows the new name, as in Obsidian; use `[[Old|text]]` to keep the display.
- Renames apply to notes, not attachments.
- If the vault changes on disk between tool calls (Obsidian sync, your editor), the next call re-scans it. There is no file watcher.
- Cold indexing is CPU-cheap (about 0.6 ms per note to parse) but reads every file once; on a Windows machine with real-time antivirus scanning, 5,000 freshly written notes took about 25 s on the first run. A restart reads the cache in about 0.2 s.

## Development

```
uv sync --extra test
uv run pytest -m "not perf"     # fast suite
uv run pytest tests/test_perf.py -s   # 5,000-note vault
uv run ruff check src tests && uv run mypy --strict src
```

Companion tutorial that builds a minimal version from scratch: [mdvault-mcp-the-hard-way](https://github.com/0xmortuex/mdvault-mcp-the-hard-way).

## License

MIT
