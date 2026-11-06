# Changelog

## 0.1.0 - 2026-11-06

First release.

- Ten tools: `vault_overview`, `search_notes`, `read_note`, `note_links`, `query_notes`,
  `broken_links`, `create_note`, `append_to_note`, `edit_note`, `rename_note`.
- Indexing: wikilinks (aliases, headings, blocks, embeds, path-qualified), markdown links
  (relative, rooted, percent-encoded, angle-bracketed), tags (frontmatter and inline, nested),
  frontmatter (hand-written YAML subset parser, errors name the line). Parsed notes are cached
  in SQLite outside the vault, keyed by mtime and size.
- Search: in-process BM25 with title and tag boosts, case- and diacritic-insensitive (Turkish).
- Same-name notes in different folders are reported as ambiguous, never guessed.
- Writes stay inside the vault (traversal, symlinks, junctions and dot-folders refused), never
  overwrite without `overwrite=true`, are atomic (temp file + `os.replace`), and preserve line
  endings, BOM and frontmatter formatting and comments byte for byte.
- `rename_note` defaults to a dry run listing every file and link it would change; it updates
  wikilinks, embeds and markdown links, fixes the moved note's own relative links, and reports
  broken links before and after.
- `--read-only` / `MDVAULT_READ_ONLY=1` disables the four writing tools.
