"""mdvault-mcp: an MCP server for a folder of markdown notes."""

from __future__ import annotations

import argparse
import functools
import os
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import ops
from .frontmatter import FrontmatterError
from .index import Vault
from .query import QueryError
from .safeio import VaultError

mcp = MCPServer(
    "mdvault",
    instructions=(
        "Work with a folder of markdown notes (Obsidian, Logseq or plain) as a knowledge base. "
        "Start with vault_overview. Find notes with search_notes (ranked full text) or query_notes "
        "(tags and frontmatter). Read with read_note; follow the graph with note_links; audit with "
        "broken_links. Notes can be named by vault path ('Projects/Plan.md'), by name ('Plan') or as "
        "a [[wikilink]]. If several notes share a name you get an ambiguity error listing them: "
        "retry with the full path. Writes (create_note, append_to_note, edit_note, rename_note) never "
        "overwrite without overwrite=true, stay inside the vault, and rename_note defaults to a dry "
        "run that lists every file it would change."
    ),
)

F = TypeVar("F", bound=Callable[..., Any])

_LOCK = threading.RLock()
_vault: Vault | None = None
_read_only = False

# Failures an agent can act on. MCPServer only forwards a ToolError's message; anything
# else becomes a bare "Error executing tool X". Everything listed here is re-raised as a
# ToolError with its text intact; anything not listed is a genuine bug and stays visible
# in the server log.
_EXPECTED = (VaultError, QueryError, FrontmatterError, OSError)


def configure(vault_path: str, read_only: bool = False, cache_dir: str | None = None) -> None:
    global _vault, _read_only
    _vault = Vault(vault_path, cache_dir)
    _read_only = read_only


def _v() -> Vault:
    if _vault is None:
        raise ToolError("no vault is configured: start the server with --vault PATH or set MDVAULT_PATH")
    return _vault


def _writable() -> Vault:
    if _read_only:
        raise ToolError("the server was started with --read-only; writing tools are disabled")
    return _v()


def tool(fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            with _LOCK:
                return fn(*args, **kwargs)
        except _EXPECTED as e:
            raise ToolError(str(e)) from e

    mcp.tool()(wrapper)
    return fn


def _limit(n: int) -> int:
    if n < 1:
        raise VaultError("limit must be at least 1")
    return n


@tool
def vault_overview() -> str:
    """Summarise the vault: note and file counts, top tags, most linked-to notes, orphan notes
    (no links in or out), broken links, ambiguous links, and notes whose frontmatter does not
    parse. Call this first to learn what is in the vault. Re-scans changed files on every call."""
    return ops.vault_overview(_v())


@tool
def search_notes(
    query: str, limit: int = 10, folder: str | None = None, tag: str | None = None, all_terms: bool = False
) -> str:
    """Full-text search over note titles, body text and tags, ranked best first (BM25), with a
    snippet showing the best-matching line and its line number.

    Matching ignores case and diacritics ("seker" finds "Seker" and "Şeker") and uses whole
    words, no stemming. Words in the title count more than words in the body. By default a note
    matches if it contains any query word, and notes containing more of them rank higher;
    all_terms=true requires every word. folder="Projects" limits to that folder (and below);
    tag="project" limits to notes with that tag or a nested tag like project/alpha."""
    return ops.search_notes(_v(), query, _limit(limit), folder, tag, all_terms)


@tool
def read_note(note: str, raw: bool = False, max_chars: int = 20000) -> str:
    """Read one note. `note` is a vault path ('Projects/Plan.md'), a name ('Plan') or a
    [[wikilink]]; a name shared by several notes returns an error listing the full paths.

    Returns the path, title, tags, link counts, the parsed frontmatter as JSON, and the body.
    raw=true returns the exact file text including the frontmatter block (use it before edit_note
    when you need to change the frontmatter itself). Long notes are truncated at max_chars with
    a visible notice."""
    return ops.read_note(_v(), note, raw, _limit(max_chars))


@tool
def note_links(note: str, direction: str = "both", limit: int = 50) -> str:
    """Show a note's links. direction="in" lists backlinks: every note linking here, with the
    file:line and the line of text around the link, plus ambiguous links that might point here;
    direction="out" lists the note's own links and where each resolves (a note, a file, BROKEN,
    or AMBIGUOUS with the candidate paths); "both" does both. Covers [[wikilinks]], ![[embeds]]
    and [markdown](links.md); links inside code blocks are ignored."""
    return ops.note_links(_v(), note, direction, _limit(limit))


@tool
def query_notes(where: str | None = None, tag: str | None = None, folder: str | None = None, limit: int = 50) -> str:
    """Find notes by tag and/or frontmatter properties.

    tag="project" matches notes tagged #project (frontmatter or inline) and nested tags such as
    project/alpha. `where` is a small query language over the frontmatter: `status=draft`,
    `due < 2026-12-01`, `tags contains project`, `owner exists`, `owner missing`, joined with AND /
    OR (AND binds tighter), with an optional NOT in front of a clause. Example:
    `status=draft AND tags contains project`. Quote values with spaces. Comparison ignores case;
    on a list, `=` and `contains` mean "any element". Notes whose frontmatter fails to parse are
    skipped (vault_overview lists them)."""
    return ops.query_notes(_v(), where, tag, folder, _limit(limit))


@tool
def broken_links(folder: str | None = None, limit: int = 200) -> str:
    """List links that point nowhere (BROKEN, with a did-you-mean when a similar note exists) and
    links that match several notes (AMBIGUOUS, with every candidate path), as file:line. Optionally
    limited to a folder."""
    return ops.broken_links(_v(), folder, _limit(limit))


@tool
def create_note(
    path: str, content: str = "", frontmatter: dict[str, Any] | None = None, overwrite: bool = False
) -> str:
    """Create a note. `path` is relative to the vault root ('Projects/New Idea.md'; .md is added
    if missing; missing folders are created). Fails if the file exists unless overwrite=true.
    `frontmatter` is an optional object written as a YAML block at the top, e.g.
    {"status": "draft", "tags": ["project"]}. Warns when another note already has the same name,
    because plain [[links]] to that name become ambiguous. Paths that leave the vault, pass
    through symlinks, or touch dot-folders (.git, .obsidian) are refused."""
    return ops.create_note(_writable(), path, content, frontmatter, overwrite)


@tool
def append_to_note(
    text: str,
    note: str | None = None,
    daily: bool = False,
    date: str | None = None,
    daily_folder: str = "Daily",
    date_format: str = "%Y-%m-%d",
) -> str:
    """Append text to the end of a note, keeping the file's line endings. Either `note` names an
    existing note, or daily=true appends to today's daily note (created if missing) at
    '<daily_folder>/<date_format>.md', by default 'Daily/YYYY-MM-DD.md'. date="YYYY-MM-DD" picks
    another day. Good for logging ideas, tasks and meeting notes without rewriting anything."""
    return ops.append_to_note(_writable(), text, note, daily, date, daily_folder, date_format)


@tool
def edit_note(note: str, old_text: str, new_text: str, dry_run: bool = False) -> str:
    """Replace exact text in a note. old_text must match exactly once (whitespace included), or
    the edit is refused with the line numbers of the matches; add surrounding text to make it
    unique. new_text="" deletes. Line endings of the file are preserved and an edit that would
    make the frontmatter unparseable is refused. dry_run=true shows a diff and writes nothing."""
    return ops.edit_note(_writable(), note, old_text, new_text, dry_run)


@tool
def rename_note(note: str, new_name: str, dry_run: bool = True) -> str:
    """Rename or move a note and update every link to it across the vault: [[wikilinks]] (keeping
    #headings, ^blocks and |aliases), ![[embeds]] and relative [markdown](links.md). new_name is a
    bare name ('New Title', stays in the same folder) or a vault path ('Archive/New Title.md',
    moves it). A moved note's own relative links are fixed too.

    dry_run defaults to TRUE: it lists every file and link that would change and writes nothing;
    review it, then call again with dry_run=false. Never overwrites an existing note. Links that
    are ambiguous (several notes share the name) cannot be attributed safely and are reported
    rather than rewritten. The result reports broken links before and after."""
    return ops.rename_note(_writable(), note, new_name, dry_run)


def main() -> None:
    ap = argparse.ArgumentParser(prog="mdvault-mcp", description="MCP server for a folder of markdown notes.")
    ap.add_argument("--vault", default=os.environ.get("MDVAULT_PATH"), help="path to the notes folder (or set MDVAULT_PATH)")
    ap.add_argument(
        "--read-only",
        action="store_true",
        default=os.environ.get("MDVAULT_READ_ONLY", "") not in ("", "0"),
        help="disable create/append/edit/rename",
    )
    args = ap.parse_args()
    if not args.vault:
        ap.error("no vault given: pass --vault PATH or set MDVAULT_PATH")
    try:
        configure(args.vault, args.read_only)
    except VaultError as e:
        ap.error(str(e))
    mcp.run()


if __name__ == "__main__":
    main()
