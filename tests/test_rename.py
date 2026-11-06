"""Rename correctness: re-parse every link in a fresh index afterwards and compare with before."""

import os
import random

import pytest
from vaultgen import make_big_vault, read, write

from mdvault_mcp import ops
from mdvault_mcp.index import Vault
from mdvault_mcp.safeio import VaultError

from test_ops import no_temp_files, snapshot  # noqa: E402


def graph_state(root, cache_dir):
    """{(src, link index): (status, target-or-candidates)} from a brand-new index."""
    v = Vault(root, cache_dir)
    v.refresh()
    out = {}
    for src, rs in v.graph.out.items():
        for i, r in enumerate(rs):
            out[(src, i)] = (r.status, r.target if r.status != "ambiguous" else tuple(r.candidates))
    return out


def assert_preserved(before, after, old, new):
    """Every link still resolves to the same note (renamed), nothing newly broken or ambiguous."""
    mp = lambda p: new if p == old else p  # noqa: E731
    assert len(before) == len(after)
    for (src, i), (status, target) in before.items():
        a_status, a_target = after[(mp(src), i)]
        if status == "ambiguous":
            if old in target:
                continue  # candidates changed on purpose
            assert (a_status, a_target) == (status, tuple(mp(t) for t in target)), (src, i)
        else:
            assert a_status == status, (src, i, status, a_status)
            assert a_target == (mp(target) if target else target), (src, i, target, a_target)


def do_rename(vault, cache_dir, note, new_name, old_path):
    before = graph_state(vault.root, cache_dir + "-before")
    out = ops.rename_note(vault, note, new_name, False)
    new_path = vault.resolve_ref(new_name if "/" in new_name else (old_path.rsplit("/", 1)[0] + "/" + new_name if "/" in old_path else new_name))
    after = graph_state(vault.root, cache_dir + "-after")
    assert_preserved(before, after, old_path, new_path.replace("\\", "/"))
    assert no_temp_files(vault.root)
    return out, new_path


def test_dry_run_is_the_default_and_writes_nothing(vault):
    before = snapshot(vault.root)
    out = ops.rename_note(vault, "Beta Plan", "Beta Plan v2", True)
    assert out.startswith("DRY RUN")
    assert "Home.md" in out and "Projects/Alpha/Alpha Plan.md" in out and "Projects/Beta/Beta Plan.md (the renamed note itself)" not in out
    assert "Call again with dry_run=false" in out
    assert snapshot(vault.root) == before  # not a single byte, not a single new file


def test_simple_rename_updates_every_link_form_byte_exactly(vault, cache_dir):
    before = snapshot(vault.root)
    out, new = do_rename(vault, cache_dir, "Beta Plan", "Beta Plan v2", "Projects/Beta/Beta Plan.md")
    assert new == "Projects/Beta/Beta Plan v2.md"
    after = snapshot(vault.root)
    assert "Projects/Beta/Beta Plan.md" not in after
    assert after["Projects/Beta/Beta Plan v2.md"] == before["Projects/Beta/Beta Plan.md"].replace(b"", b"")  # no inbound edits in itself
    # Home: alias kept
    assert after["Home.md"] == before["Home.md"].replace(b"[[Beta Plan|the beta]]", b"[[Beta Plan v2|the beta]]")
    a = before["Projects/Alpha/Alpha Plan.md"]
    expect = a.replace(b"[[Beta Plan#^b1|beta block]]", b"[[Beta Plan v2#^b1|beta block]]").replace(
        b"../Beta/Beta%20Plan.md#top", b"../Beta/Beta%20Plan%20v2.md#top"
    )
    assert after["Projects/Alpha/Alpha Plan.md"] == expect
    changed = {p for p in after if p in before and after[p] != before[p]}
    assert changed == {"Home.md", "Projects/Alpha/Alpha Plan.md"}
    assert "2 link(s) in 2 file(s)" in out or "3 link(s) in 2 file(s)" in out
    assert "before, " in out and "after" in out


def test_move_to_other_folder_fixes_relative_markdown_links_and_keeps_crlf(vault, cache_dir):
    before = snapshot(vault.root)
    out, new = do_rename(vault, cache_dir, "Beta Plan", "Archive/Beta Plan.md", "Projects/Beta/Beta Plan.md")
    assert new == "Archive/Beta Plan.md"
    after = snapshot(vault.root)
    # bare wikilinks need no change when the name stays unique
    assert after["Home.md"] == before["Home.md"]
    alpha = after["Projects/Alpha/Alpha Plan.md"]
    assert b"../../Archive/Beta%20Plan.md#top" in alpha and b"[[Beta Plan#^b1|beta block]]" in alpha
    moved = after["Archive/Beta Plan.md"]
    assert b"../Projects/Alpha/Alpha%20Plan.md" in moved  # the moved note's own relative link
    assert moved.count(b"\n") == moved.count(b"\r\n") > 0  # still CRLF throughout
    assert os.path.isdir(os.path.join(vault.root, "Archive"))


def test_rename_note_with_self_links_and_embeds(vault, cache_dir):
    before = snapshot(vault.root)
    do_rename(vault, cache_dir, "Projects/Alpha/Alpha Plan.md", "Alpha Roadmap", "Projects/Alpha/Alpha Plan.md")
    after = snapshot(vault.root)
    new = after["Projects/Alpha/Alpha Roadmap.md"]
    assert b"[[Alpha Roadmap#Goal]]" in new  # self link
    assert b"# still thinking" in new and b"# a comment line that must survive" in new  # comments intact
    assert after["Home.md"] == before["Home.md"].replace(b"![[Alpha Plan]]", b"![[Alpha Roadmap]]")
    assert b"Alpha%20Roadmap.md" in after["Projects/Beta/Beta Plan.md"] and b"[[Alpha Roadmap]]" in after["Projects/Beta/Beta Plan.md"]
    assert after["Projects/Beta/Beta Plan.md"].count(b"\n") == after["Projects/Beta/Beta Plan.md"].count(b"\r\n")


def test_turkish_rename_and_case_insensitive_links(vault, cache_dir):
    before = snapshot(vault.root)
    do_rename(vault, cache_dir, "şeker notu", "Şeker Tarifi", "Turkce/Şeker Notu.md")
    home = read(vault.root, "Home.md")
    assert home == before["Home.md"].replace(b"[[\xc5\x9eeker Notu]]", "[[Şeker Tarifi]]".encode()).replace(
        "[[şeker notu#Tarif]]".encode(), "[[Şeker Tarifi#Tarif]]".encode()
    )


def test_case_only_rename(vault, cache_dir):
    ops.create_note(vault, "Lower.md", "[[Home]]", None, False)
    do_rename(vault, cache_dir, "Lower", "LOWER", "Lower.md")
    assert "LOWER.md" in os.listdir(vault.root) and "Lower.md" not in os.listdir(vault.root)


def test_ambiguous_links_are_reported_not_rewritten(vault, cache_dir):
    before = snapshot(vault.root)
    out = ops.rename_note(vault, "Areas/Meeting Notes.md", "Areas Standup", True)
    assert "NOT updated" in out and "Home.md" in out and "Meeting Notes" in out
    ops.rename_note(vault, "Areas/Meeting Notes.md", "Areas Standup", False)
    after = snapshot(vault.root)
    assert after["Home.md"] == before["Home.md"]  # ambiguous link untouched


def test_unambiguous_path_links_to_one_twin_are_rewritten(vault, cache_dir):
    write(vault.root, "Ref.md", "[[Projects/Meeting Notes]] and [[Meeting Notes]]\n")
    vault.refresh()
    ops.rename_note(vault, "Projects/Meeting Notes.md", "Standup", False)
    ref = read(vault.root, "Ref.md").decode()
    assert ref.startswith("[[Projects/Standup]] and [[Meeting Notes]]")  # path form kept, ambiguous one untouched


def test_refuses_when_rename_would_create_ambiguity(vault):
    before = snapshot(vault.root)
    with pytest.raises(VaultError, match="would make .* ambiguous"):
        ops.rename_note(vault, "Orphan", "Beta Plan", False)  # [[Beta Plan]] links exist
    assert snapshot(vault.root) == before


def test_twin_without_bare_links_allows_rename_and_uses_path_links(vault, cache_dir):
    ops.create_note(vault, "Twin/Zed.md", "z", None, False)  # nothing links to it
    ops.create_note(vault, "Sub/Lonely2.md", "x", None, False)
    ops.create_note(vault, "Back.md", "[[Lonely2]] and [[Lonely2#h|a]]", None, False)
    before = graph_state(vault.root, cache_dir + "-b")
    out = ops.rename_note(vault, "Lonely2", "Sub/Zed", False)
    assert "another note is already named" in out
    assert read(vault.root, "Back.md") == b"[[Sub/Zed]] and [[Sub/Zed#h|a]]\n"  # path form: bare [[Zed]] would be ambiguous
    assert_preserved(before, graph_state(vault.root, cache_dir + "-a"), "Sub/Lonely2.md", "Sub/Zed.md")


def test_root_level_name_clash_is_refused_when_it_would_break_links(vault):
    ops.create_note(vault, "Twin/Zed.md", "z", None, False)
    ops.create_note(vault, "Lonely3.md", "x", None, False)
    ops.create_note(vault, "Back.md", "[[Lonely3]]", None, False)
    before = snapshot(vault.root)
    with pytest.raises(VaultError, match="vault root can only be linked by its bare name"):
        ops.rename_note(vault, "Lonely3", "Zed", False)
    assert snapshot(vault.root) == before


def test_rename_refused_when_it_would_make_existing_links_ambiguous(vault):
    ops.create_note(vault, "Twin/Zed.md", "z", None, False)
    ops.create_note(vault, "Uses.md", "[[Zed]]", None, False)  # resolves to Twin/Zed.md today
    before = snapshot(vault.root)
    with pytest.raises(VaultError, match="would make 1 existing link"):
        ops.rename_note(vault, "Orphan", "Other/Zed", False)
    assert snapshot(vault.root) == before


def test_ambiguity_reported_when_twin_names_exist(vault):
    ops.create_note(vault, "Sub/Thing.md", "t", None, False)
    ops.create_note(vault, "Sub3/Thing.md", "twin", None, False)
    ops.create_note(vault, "Backlinker.md", "[[Thing]] and [[Sub/Thing]]", None, False)
    out = ops.rename_note(vault, "Sub/Thing.md", "Sub/Thing Renamed", True)
    assert "NOT updated" in out and "Backlinker.md:1" in out
    ops.rename_note(vault, "Sub/Thing.md", "Sub/Thing Renamed", False)
    assert read(vault.root, "Backlinker.md") == b"[[Thing]] and [[Sub/Thing Renamed]]\n"


def test_errors(vault):
    before = snapshot(vault.root)
    for args, pat in [
        (("Home", "Orphan"), "already exists"),
        (("Home", "Home"), "nothing to do"),
        (("Home", ""), "empty"),
        (("Home", "../outside"), "inside the vault"),
        (("Home", ".obsidian/x"), "off limits"),
        (("Home", "bad:name"), "not allowed"),
        (("Nonexistent", "x"), "no note matches"),
        (("Meeting Notes", "x"), "ambiguous"),
    ]:
        with pytest.raises(VaultError, match=pat):
            ops.rename_note(vault, args[0], args[1], False)
    assert snapshot(vault.root) == before


def test_rename_detects_concurrent_modification(vault, monkeypatch):
    real = ops._plan_file

    def sneaky(v, src, *a):
        plan = real(v, src, *a)
        if src == "Home.md":
            with open(os.path.join(v.root, "Home.md"), "ab") as f:
                f.write(b"\nsomeone else edited this\n")
        return plan

    monkeypatch.setattr(ops, "_plan_file", sneaky)
    with pytest.raises(VaultError, match="changed while the rename was being planned"):
        ops.rename_note(vault, "Beta Plan", "Beta Renamed", False)
    assert os.path.exists(os.path.join(vault.root, "Projects", "Beta", "Beta Plan.md"))
    assert read(vault.root, "Projects/Alpha/Alpha Plan.md").count(b"Beta Plan") > 0  # nothing was half-applied


def test_staging_failure_applies_nothing(vault, monkeypatch):
    before = snapshot(vault.root)
    real = ops.StagedWrite
    calls = {"n": 0}

    class Flaky(real):
        def __init__(self, path, data):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("no space left on device")
            super().__init__(path, data)

    monkeypatch.setattr(ops, "StagedWrite", Flaky)
    with pytest.raises(OSError, match="no space"):
        ops.rename_note(vault, "Beta Plan", "Beta Renamed", False)
    monkeypatch.undo()
    assert snapshot(vault.root) == before  # no note moved, no link rewritten, no temp left


def test_markdown_link_styles(tmp_path, cache_dir):
    root = str(tmp_path / "md")
    os.makedirs(root)
    write(root, "docs/Target Note.md", "# T\n")
    write(root, "docs/a.md", "[x](Target%20Note.md) [y](./Target%20Note.md) [z](<Target Note.md>) [r](/docs/Target%20Note.md#sec \"title\") [n](Target%20Note)\n")
    write(root, "top.md", "[x](docs/Target%20Note.md) [ext](https://example.com/Target%20Note.md)\n")
    v = Vault(root, cache_dir)
    v.refresh()
    before = graph_state(root, cache_dir + "-b")
    ops.rename_note(v, "docs/Target Note.md", "sub/New (draft).md", False)
    after = graph_state(root, cache_dir + "-a")
    assert_preserved(before, after, "docs/Target Note.md", "sub/New (draft).md")
    a = read(root, "docs/a.md").decode()
    assert a == "[x](../sub/New%20%28draft%29.md) [y](../sub/New%20%28draft%29.md) [z](<../sub/New (draft).md>) [r](/sub/New%20%28draft%29.md#sec \"title\") [n](../sub/New%20%28draft%29)\n"
    assert "https://example.com/Target%20Note.md" in read(root, "top.md").decode()


def test_links_in_code_blocks_are_not_rewritten(tmp_path, cache_dir):
    root = str(tmp_path / "c")
    os.makedirs(root)
    write(root, "Old.md", "# Old\n")
    write(root, "n.md", "real [[Old]]\n```\n[[Old]]\n```\n`[[Old]]`\n")
    v = Vault(root, cache_dir)
    v.refresh()
    ops.rename_note(v, "Old", "New", False)
    assert read(root, "n.md").decode() == "real [[New]]\n```\n[[Old]]\n```\n`[[Old]]`\n"


def test_frontmatter_property_links_are_rewritten_with_formatting_intact(tmp_path, cache_dir):
    root = str(tmp_path / "f")
    os.makedirs(root)
    write(root, "Old.md", "# Old\n")
    src = '---\n# my comment\nrelated:   "[[Old]]"   # trailing\nlist: ["[[Old|x]]", plain]\n---\nbody\n'
    write(root, "n.md", src, newline="\r\n")
    v = Vault(root, cache_dir)
    v.refresh()
    ops.rename_note(v, "Old", "Newer", False)
    assert read(root, "n.md") == src.replace("[[Old", "[[Newer").replace("\n", "\r\n").encode()


def test_rename_report_lists_every_file(tmp_path, cache_dir):
    root = str(tmp_path / "many")
    os.makedirs(root)
    write(root, "Target.md", "# t\n")
    for i in range(30):
        write(root, f"src{i:02d}.md", "[[Target]] " * 5 + "\n")
    v = Vault(root, cache_dir)
    v.refresh()
    out = ops.rename_note(v, "Target", "Goal", True)
    assert "150 link(s) in 30 file(s)" in out
    assert all(f"src{i:02d}.md" in out for i in range(30))
    assert "and 2 more in this file" in out


def test_random_renames_on_generated_vault_never_break_links(tmp_path, cache_dir):
    root = str(tmp_path / "gen")
    os.makedirs(root)
    paths = make_big_vault(root, n=250, seed=11)
    v = Vault(root, cache_dir)
    v.refresh()
    rng = random.Random(5)
    done = 0
    for step in range(25):
        old = rng.choice(sorted(v.notes))
        folder = rng.choice(["", "Moved/", "Projects/", "Deep/er/path/", "Turkce/"])
        stem = rng.choice(["Renamed ", "Şeker ", "Yeni "]) + str(step)
        new_name = (folder + stem) if (folder or rng.random() < 0.5) else stem
        if not folder:
            new_name = stem  # same folder
        before = graph_state(root, cache_dir + f"-b{step}")
        try:
            ops.rename_note(v, old, new_name, False)
        except VaultError as e:
            assert "ambiguous" in str(e) or "already exists" in str(e)
            continue
        new_rel = (new_name if "/" in new_name else (old.rsplit("/", 1)[0] + "/" if "/" in old else "") + new_name) + ".md"
        after = graph_state(root, cache_dir + f"-a{step}")
        assert_preserved(before, after, old, new_rel)
        done += 1
    assert done >= 15
    assert no_temp_files(root)
    assert len(paths) == 250
