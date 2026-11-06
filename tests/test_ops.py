import os
import subprocess
import sys

import pytest
from vaultgen import read, write

from mdvault_mcp import ops
from mdvault_mcp.safeio import VaultError, clean_rel, confine


def snapshot(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root).replace("\\", "/")] = fh.read()
    return out


def no_temp_files(root):
    return [f for dp, _, fn in os.walk(root) for f in fn if f.startswith(".mdvault-")] == []


# ------------------------------------------------------------------ path safety


@pytest.mark.parametrize(
    "bad",
    ["../x.md", "a/../../x.md", "/etc/passwd", "\\\\server\\share\\x.md", "C:\\Windows\\x.md", "c:/x.md", ".git/config", ".obsidian/app.json",
     "ok/.hidden/x.md", "CON.md", "dir/nul.txt", "bad?.md", "a:b.md", "trail.", "sp ace /x.md", "", "   ", "a/./../b"],
)
def test_clean_rel_rejects(bad):
    with pytest.raises(VaultError):
        clean_rel(bad)


def test_clean_rel_normalises_backslashes():
    assert clean_rel("Projects\\Sub\\Note.md") == "Projects/Sub/Note.md"


@pytest.mark.parametrize("bad", ["../escape.md", "sub/../../escape.md", ".obsidian/evil.md", ".git/hooks/pre-commit", "/abs.md"])
def test_writes_outside_or_hidden_are_refused_and_nothing_is_created(vault, bad, tmp_path):
    before = snapshot(vault.root)
    outside = snapshot(str(tmp_path))
    with pytest.raises(VaultError):
        ops.create_note(vault, bad, "x", None, True)
    assert snapshot(vault.root) == before
    assert snapshot(str(tmp_path)) == outside  # nothing leaked next to the vault either
    assert not os.path.exists(os.path.join(os.path.dirname(vault.root), "escape.md"))


def _link_dir(link, target):
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":  # junctions need no privilege
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
        return r.returncode == 0
    return False


def test_directory_link_escape_is_blocked(vault, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("TOP SECRET [[Home]]\n", encoding="utf-8")
    if not _link_dir(os.path.join(vault.root, "Linked"), str(outside)):
        pytest.skip("cannot create symlinks or junctions on this machine")
    vault.refresh()
    assert not any(p.startswith("Linked") for p in vault.notes)  # the scanner does not follow it
    for attempt in (
        lambda: ops.create_note(vault, "Linked/new.md", "pwn", None, False),
        lambda: ops.create_note(vault, "Linked/secret.md", "pwn", None, True),
        lambda: ops.append_to_note(vault, "pwn", "Linked/secret.md", False, None, "Daily", "%Y"),
        lambda: ops.edit_note(vault, "Linked/secret.md", "SECRET", "X", False),
        lambda: ops.read_note(vault, "Linked/secret.md", False, 1000),
        lambda: ops.create_note(vault, "Linked/sub/deep.md", "x", None, False),
    ):
        with pytest.raises(VaultError):
            attempt()
    assert [p.name for p in outside.iterdir()] == ["secret.md"]
    assert (outside / "secret.md").read_text(encoding="utf-8") == "TOP SECRET [[Home]]\n"
    assert "TOP SECRET" not in ops.search_notes(vault, "secret", 10, None, None, False)


def test_file_symlink_escape_is_blocked(vault, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("outside\n", encoding="utf-8")
    try:
        os.symlink(str(secret), os.path.join(vault.root, "Evil.md"))
    except (OSError, NotImplementedError):
        pytest.skip("cannot create file symlinks on this machine")
    vault.refresh()
    assert "Evil.md" not in vault.notes
    with pytest.raises(VaultError):
        ops.create_note(vault, "Evil.md", "overwrite!", None, True)
    with pytest.raises(VaultError):
        ops.append_to_note(vault, "x", "Evil.md", False, None, "Daily", "%Y")
    assert secret.read_text(encoding="utf-8") == "outside\n"


def test_confine_returns_path_inside(vault):
    p = confine(vault.root, "New/Sub/x.md")
    assert os.path.normcase(p).startswith(os.path.normcase(vault.root))


# ------------------------------------------------------------------ create


def test_create_note_basic_and_never_overwrites(vault):
    msg = ops.create_note(vault, "Ideas/New Idea", "hello\nworld", {"status": "draft", "tags": ["idea", "x y"]}, False)
    assert "Created Ideas/New Idea.md" in msg
    raw = read(vault.root, "Ideas/New Idea.md").decode()
    assert raw == '---\nstatus: draft\ntags:\n  - idea\n  - x y\n---\nhello\nworld\n'
    assert vault.notes["Ideas/New Idea.md"].fm == {"status": "draft", "tags": ["idea", "x y"]}
    with pytest.raises(VaultError, match="already exists.*overwrite=true"):
        ops.create_note(vault, "Ideas/New Idea.md", "other", None, False)
    assert read(vault.root, "Ideas/New Idea.md").decode() == raw
    ops.create_note(vault, "Ideas/New Idea.md", "replaced", None, True)
    assert read(vault.root, "Ideas/New Idea.md") == b"replaced\n"
    assert no_temp_files(vault.root)


def test_create_overwrite_keeps_crlf(vault):
    ops.create_note(vault, "Projects/Beta/Beta Plan.md", "a\nb", None, True)
    assert read(vault.root, "Projects/Beta/Beta Plan.md") == b"a\r\nb\r\n"


def test_create_warns_about_duplicate_name(vault):
    msg = ops.create_note(vault, "Elsewhere/Home.md", "dup", None, False)
    assert "Warning" in msg and "Home.md" in msg and "ambiguous" in msg


def test_create_rejects_case_variant_and_bad_frontmatter(vault):
    with pytest.raises(VaultError, match="already exists|different letter case"):
        ops.create_note(vault, "home.md", "x", None, False)
    with pytest.raises(VaultError, match="not valid"):
        ops.create_note(vault, "bad.md", "---\nk: [oops\n---\nbody", None, False)
    with pytest.raises(VaultError, match="not both"):
        ops.create_note(vault, "bad2.md", "---\na: 1\n---\n", {"b": 2}, False)
    with pytest.raises(VaultError, match="cannot write frontmatter"):
        ops.create_note(vault, "bad3.md", "x", {"a": [[1]]}, False)
    assert "bad.md" not in snapshot(vault.root)


def test_create_turkish_name(vault):
    ops.create_note(vault, "Turkce/Çiçek Bahçesi.md", "# Çiçek\n[[şeker notu]]\n", None, False)
    assert "Turkce/Çiçek Bahçesi.md" in vault.notes
    assert any(r.src == "Turkce/Çiçek Bahçesi.md" and r.target == "Turkce/Şeker Notu.md" for r in vault.graph.inc["Turkce/Şeker Notu.md"])


def test_atomic_write_failure_leaves_original_and_no_temp(vault, monkeypatch):
    before = snapshot(vault.root)

    def boom(*a, **k):
        raise OSError("disk exploded")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk exploded"):
        ops.edit_note(vault, "Orphan", "No links at all.", "changed", False)
    monkeypatch.undo()
    assert snapshot(vault.root) == before  # original untouched, temp file cleaned up


# ------------------------------------------------------------------ append / daily


def test_append_preserves_crlf_and_adds_missing_newline(vault):
    before = read(vault.root, "Projects/Beta/Beta Plan.md")
    ops.append_to_note(vault, "- new item\n- another", "Beta Plan", False, None, "Daily", "%Y")
    after = read(vault.root, "Projects/Beta/Beta Plan.md")
    assert after.startswith(before)
    assert after[len(before):] == b"- new item\r\n- another\r\n"
    assert after.count(b"\n") == after.count(b"\r\n")  # not one bare LF
    write(vault.root, "NoEol.md", "last line without newline")
    with open(os.path.join(vault.root, "NoEol.md"), "wb") as f:
        f.write(b"last line without newline")
    ops.append_to_note(vault, "added", "NoEol", False, None, "Daily", "%Y")
    assert read(vault.root, "NoEol.md") == b"last line without newline\nadded\n"


def test_daily_note_created_then_appended(vault):
    m1 = ops.append_to_note(vault, "- 09:00 standup", None, True, "2026-11-06", "Daily", "%Y-%m-%d")
    assert "Created" in m1 and "Daily/2026-11-06.md" in vault.notes
    m2 = ops.append_to_note(vault, "- 10:00 review", None, True, "2026-11-06", "Daily", "%Y-%m-%d")
    assert "Created" not in m2
    assert read(vault.root, "Daily/2026-11-06.md") == b"- 09:00 standup\n- 10:00 review\n"
    ops.append_to_note(vault, "x", None, True, "2026-11-07", "Journal/Days", "%Y/%m/%d")
    assert "Journal/Days/2026/11/07.md" in vault.notes


def test_append_argument_errors(vault):
    with pytest.raises(VaultError, match="exactly one"):
        ops.append_to_note(vault, "x", None, False, None, "Daily", "%Y")
    with pytest.raises(VaultError, match="exactly one"):
        ops.append_to_note(vault, "x", "Home", True, None, "Daily", "%Y")
    with pytest.raises(VaultError, match="YYYY-MM-DD"):
        ops.append_to_note(vault, "x", None, True, "11/06/2026", "Daily", "%Y")
    with pytest.raises(VaultError, match="no note matches"):
        ops.append_to_note(vault, "x", "Nonexistent Note", False, None, "Daily", "%Y")
    with pytest.raises(VaultError, match="empty"):
        ops.append_to_note(vault, "", "Home", False, None, "Daily", "%Y")


# ------------------------------------------------------------------ edit


def test_edit_exact_once_and_byte_exact_everywhere_else(vault):
    p = "Projects/Alpha/Alpha Plan.md"
    before = read(vault.root, p)
    ops.edit_note(vault, p, "Goal text with a block.", "Revised goal.", False)
    after = read(vault.root, p)
    assert after == before.replace(b"Goal text with a block.", b"Revised goal.")
    assert b"# still thinking" in after and b"# a comment line that must survive" in after


def test_edit_crlf_file_with_lf_old_text(vault):
    p = "Projects/Beta/Beta Plan.md"
    ops.edit_note(vault, p, "# Beta Plan\n\n^b1 paragraph with id.", "# Beta Plan\n\n^b1 edited.\nsecond line", False)
    after = read(vault.root, p)
    assert b"^b1 edited.\r\nsecond line\r\n" in after
    assert after.count(b"\n") == after.count(b"\r\n")


def test_edit_errors(vault):
    with pytest.raises(VaultError, match=r"matches \d+ places.*lines"):
        ops.edit_note(vault, "Home", "[[", "((", False)
    with pytest.raises(VaultError, match="not found"):
        ops.edit_note(vault, "Home", "this text is not there", "x", False)
    with pytest.raises(VaultError, match="first line of old_text does appear at line"):
        ops.edit_note(vault, "Home", "# Home\nNOT the next line", "x", False)
    with pytest.raises(VaultError, match="empty"):
        ops.edit_note(vault, "Home", "", "x", False)


def test_edit_dry_run_writes_nothing_and_shows_diff(vault):
    before = snapshot(vault.root)
    out = ops.edit_note(vault, "Orphan", "No links at all.", "Now different.", True)
    assert "DRY RUN" in out and "-No links at all." in out and "+Now different." in out
    assert snapshot(vault.root) == before


def test_edit_cannot_break_frontmatter(vault):
    before = snapshot(vault.root)
    with pytest.raises(VaultError, match="break the frontmatter"):
        ops.edit_note(vault, "Projects/Alpha/Alpha Plan.md", "owner: fadi", "owner: [unclosed", False)
    assert snapshot(vault.root) == before
    # an already-broken note can still be fixed
    ops.edit_note(vault, "Broken FM", "key: [unclosed", "key: [closed]", False)
    assert read(vault.root, "Broken FM.md") == b"---\nkey: [closed]\n---\n# Broken FM\n"


def test_edit_preserves_bom(vault):
    with open(os.path.join(vault.root, "Bom.md"), "wb") as f:
        f.write(b"\xef\xbb\xbf# Bom note\r\nbody\r\n")
    ops.edit_note(vault, "Bom", "body", "changed", False)
    assert read(vault.root, "Bom.md") == b"\xef\xbb\xbf# Bom note\r\nchanged\r\n"


def test_non_utf8_note_is_never_touched(vault):
    with open(os.path.join(vault.root, "latin.md"), "wb") as f:
        f.write(b"caf\xe9 \xff\n")
    for attempt in (
        lambda: ops.edit_note(vault, "latin", "caf", "x", False),
        lambda: ops.append_to_note(vault, "x", "latin", False, None, "Daily", "%Y"),
        lambda: ops.read_note(vault, "latin", False, 1000),
    ):
        with pytest.raises(VaultError, match="not valid UTF-8"):
            attempt()
    assert read(vault.root, "latin.md") == b"caf\xe9 \xff\n"


# ------------------------------------------------------------------ read / links / query / overview


def test_read_note_output(vault):
    out = ops.read_note(vault, "Alpha Plan", False, 20000)
    assert "path: Projects/Alpha/Alpha Plan.md" in out
    assert '"status": "draft"' in out and '"owner": "fadi"' in out
    assert "# a comment" not in out.split("--- content ---")[1]
    raw = ops.read_note(vault, "Alpha Plan", True, 20000)
    assert "# a comment line that must survive" in raw
    crlf = ops.read_note(vault, "Beta Plan", False, 20000)
    assert "line endings: CRLF" in crlf and "\r" not in crlf
    assert "[truncated" in ops.read_note(vault, "Alpha Plan", False, 20)
    assert "UNPARSEABLE" in ops.read_note(vault, "Broken FM", False, 2000)


def test_note_links_report(vault):
    out = ops.note_links(vault, "Alpha Plan", "in", 50)
    assert "Home.md:" in out and "Projects/Beta/Beta Plan.md:" in out and "(embed)" in out
    assert out.count("Projects/Beta/Beta Plan.md:8") == 1 and "[2 links]" in out  # two links on one line: one row
    out = ops.note_links(vault, "Home", "out", 50)
    assert "-> Projects/Beta/Beta Plan.md" in out
    assert "AMBIGUOUS, could be: Areas/Meeting Notes.md, Projects/Meeting Notes.md" in out
    assert "BROKEN" in out and "-> diagram.png" in out
    with pytest.raises(VaultError, match="ambiguous"):
        ops.note_links(vault, "Meeting Notes", "both", 50)
    amb = ops.note_links(vault, "Areas/Meeting Notes.md", "in", 50)
    assert "Ambiguous links that might point here" in amb and "Home.md" in amb
    with pytest.raises(VaultError, match="direction"):
        ops.note_links(vault, "Home", "sideways", 5)


def test_broken_links_report(vault):
    out = ops.broken_links(vault, None, 100)
    assert "BROKEN    Home.md:" in out and "[[Does Not Exist]]" in out
    assert "AMBIGUOUS Home.md:" in out
    assert "Not A Link In Code" not in out
    assert "No broken" in ops.broken_links(vault, "Turkce", 10)


def test_query_notes(vault):
    def paths(**kw):
        out = ops.query_notes(vault, kw.get("where"), kw.get("tag"), kw.get("folder"), 50)
        return sorted(line.split("  ")[1] for line in out.splitlines()[1:] if line.startswith("  "))

    assert paths(where="status=draft") == ["Projects/Alpha/Alpha Plan.md"]
    assert paths(where="status = DRAFT AND tags contains project") == ["Projects/Alpha/Alpha Plan.md"]
    assert paths(where="tags contains project") == ["Home.md", "Projects/Alpha/Alpha Plan.md", "Projects/Beta/Beta Plan.md"]
    assert paths(where="status=draft OR status=published") == ["Projects/Alpha/Alpha Plan.md", "Projects/Beta/Beta Plan.md"]
    assert paths(where="due < 2027-01-01") == ["Projects/Alpha/Alpha Plan.md"]
    assert paths(where="due >= 2027-01-01") == []
    assert paths(where="owner exists") == ["Projects/Alpha/Alpha Plan.md"]
    assert paths(where="NOT owner exists AND status exists") == ["Home.md", "Projects/Beta/Beta Plan.md"]
    assert paths(where="status != draft AND status exists") == ["Home.md", "Projects/Beta/Beta Plan.md"]
    assert paths(tag="project/alpha") == ["Projects/Alpha/Alpha Plan.md"]
    assert paths(tag="#beta") == ["Projects/Beta/Beta Plan.md"]  # inline nested tag beta/launch
    assert paths(tag="project", where="status=active") == ["Home.md"]
    assert paths(where='tags contains "project"', folder="Projects") == ["Projects/Alpha/Alpha Plan.md", "Projects/Beta/Beta Plan.md"]
    assert "No notes match" in ops.query_notes(vault, "status=nonsense", None, None, 5)


@pytest.mark.parametrize("bad", ["status", "status ~ x", "= draft", "a=1 b=2", "a=1 AND", "a >", ""])
def test_query_syntax_errors_explain_the_grammar(vault, bad):
    with pytest.raises(VaultError, match="Query syntax|empty query"):
        ops.query_notes(vault, bad, None, None, 5)


def test_query_needs_something(vault):
    with pytest.raises(VaultError, match="where"):
        ops.query_notes(vault, None, None, None, 5)


def test_search_output_has_snippets(vault):
    out = ops.search_notes(vault, "seker cicek", 5, None, None, False)
    assert "Turkce/Şeker Notu.md" in out and "L" in out and "Çiçek şeker" in out
    assert "No notes match" in ops.search_notes(vault, "qqqqzzz", 5, None, None, False)
    with pytest.raises(VaultError, match="no searchable words"):
        ops.search_notes(vault, "!!! ???", 5, None, None, False)


def test_overview(vault):
    out = ops.vault_overview(vault)
    assert "8 notes" in out and "Orphan.md" in out
    assert "Areas/Meeting Notes.md" not in out.split("Orphans")[1].split("Broken links")[0]  # ambiguous links may point at it
    assert "Broken links: 2" in out and "Ambiguous links" in out
    assert "Broken FM.md" in out and "unparseable" in out.lower()
    assert "#project" in out
    assert "Projects/Alpha/Alpha Plan.md" in out.split("Most linked-to")[1]


def test_vault_tools_use_fresh_state_after_outside_edit(vault):
    write(vault.root, "Fresh.md", "# Fresh\nbrand new content about zebras\n")
    assert "Fresh.md" in ops.search_notes(vault, "zebras", 5, None, None, False)
    os.remove(os.path.join(vault.root, "Fresh.md"))
    assert "No notes match" in ops.search_notes(vault, "zebras", 5, None, None, False)


def test_python_m_help_runs():
    r = subprocess.run([sys.executable, "-m", "mdvault_mcp", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--vault" in r.stdout
    r = subprocess.run([sys.executable, "-m", "mdvault_mcp"], capture_output=True, text=True, env={**os.environ, "MDVAULT_PATH": ""})
    assert r.returncode != 0 and "no vault given" in r.stderr
    r = subprocess.run([sys.executable, "-m", "mdvault_mcp", "--vault", "/definitely/not/here"], capture_output=True, text=True)
    assert r.returncode != 0 and "does not exist" in r.stderr
