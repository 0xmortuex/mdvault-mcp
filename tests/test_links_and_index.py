import os

import pytest

from mdvault_mcp.index import Vault, tokenize
from mdvault_mcp.links import extract_inline_tags, mask_code, parse_links
from mdvault_mcp.safeio import VaultError


def L(text):
    return parse_links(text)


def test_wikilink_forms():
    text = "a [[Note]] b [[Note#Head|alias]] c ![[img.png]] d [[Folder/Note^blk]] e [[#Local]] f [[ Spaced ]]"
    lks = L(text)
    assert [(lk.target, lk.fragment, lk.text, lk.embed) for lk in lks] == [
        ("Note", "", "", False),
        ("Note", "#Head", "alias", False),
        ("img.png", "", "", True),
        ("Folder/Note", "^blk", "", False),
        ("", "#Local", "", False),
        ("Spaced", "", "", False),
    ]
    for lk in lks:  # the target span points exactly at the target text
        assert text[lk.tstart : lk.tend] == lk.target


def test_table_escaped_pipe():
    lk = L(r"| [[Note\|alias]] |")[0]
    assert lk.target == "Note" and lk.text == "alias"


def test_markdown_links():
    text = "[a](x/y.md) [b](<with space.md>) [c](n%20s.md#h) ![i](pic.png) [web](https://x.org) [m](mailto:a@b) [anchor](#top) [q](f.md?x=1)"
    lks = L(text)
    assert [(lk.target, lk.fragment, lk.embed, lk.angle) for lk in lks] == [
        ("x/y.md", "", False, False),
        ("with space.md", "", False, True),
        ("n s.md", "#h", False, False),
        ("pic.png", "", True, False),
        ("f.md", "?x=1", False, False),
    ]


def test_code_is_ignored():
    text = "```\n[[in fence]]\n```\n~~~py\n[[tilde]]\n~~~\n`[[inline]]` ``[[double`tick]]`` [[real]]\n"
    assert [lk.target for lk in L(text)] == ["real"]
    m = mask_code(text)
    assert len(m) == len(text) and m.count("\n") == text.count("\n")


def test_unclosed_fence_hides_rest():
    assert L("[[a]]\n```\n[[b]]\n") and [lk.target for lk in L("[[a]]\n```\n[[b]]\n")] == ["a"]


def test_line_numbers_with_crlf():
    lks = L("one\r\ntwo [[x]]\r\n\r\n[[y]]")
    assert [lk.line for lk in lks] == [2, 4]


def test_tags():
    body = "# Heading\n#tag1 #nested/tag-2 text#notag [[a#b]] [x](y.md#frag) #123 #2024 `#code` http://x.org/#anchor #Şeker\n"
    assert extract_inline_tags(body) == ["tag1", "nested/tag-2", "Şeker"]


def test_tokenizer_folds_turkish():
    assert tokenize("Şeker ÇİÇEK Işık İstanbul ağaç") == ["seker", "cicek", "isik", "istanbul", "agac"]


# ------------------------------------------------------------------ resolution


def resolve(vault, src, ref):
    lk = parse_links(ref)[0]
    return vault.graph.resolve(lk, src)


def test_scan_ignores_hidden_and_counts(vault):
    assert ".obsidian/app.json" not in vault.attachments
    assert all(not p.startswith(".") for p in vault.notes)
    assert "diagram.png" in vault.attachments
    assert len(vault.notes) == 8


def test_resolve_wikilinks(vault):
    r = resolve(vault, "Home.md", "[[Beta Plan]]")
    assert (r.status, r.target) == ("note", "Projects/Beta/Beta Plan.md")
    r = resolve(vault, "Home.md", "[[BETA plan]]")  # case-insensitive
    assert r.status == "note"
    r = resolve(vault, "Home.md", "[[Projects/Alpha/Alpha Plan]]")
    assert r.target == "Projects/Alpha/Alpha Plan.md"
    r = resolve(vault, "Home.md", "[[Alpha/Alpha Plan.md]]")  # path suffix + extension
    assert r.target == "Projects/Alpha/Alpha Plan.md"
    r = resolve(vault, "Home.md", "[[şeker notu]]")  # Turkish, case-insensitive
    assert r.target == "Turkce/Şeker Notu.md"
    r = resolve(vault, "Home.md", "[[diagram.png]]")
    assert (r.status, r.target) == ("attachment", "diagram.png")
    assert resolve(vault, "Home.md", "[[Nope]]").status == "broken"


def test_ambiguity_is_reported_not_guessed(vault):
    r = resolve(vault, "Home.md", "[[Meeting Notes]]")
    assert r.status == "ambiguous" and r.target is None
    assert r.candidates == ["Areas/Meeting Notes.md", "Projects/Meeting Notes.md"]
    assert resolve(vault, "Home.md", "[[Projects/Meeting Notes]]").status == "note"
    with pytest.raises(VaultError, match="ambiguous.*\n.*Areas/Meeting Notes.md"):
        vault.resolve_ref("Meeting Notes")


def test_resolve_markdown_links(vault):
    r = resolve(vault, "Projects/Alpha/Alpha Plan.md", "[b](../Beta/Beta%20Plan.md#top)")
    assert r.target == "Projects/Beta/Beta Plan.md"
    r = resolve(vault, "Projects/Alpha/Alpha Plan.md", "[b](/Home.md)")
    assert r.target == "Home.md"
    assert resolve(vault, "Home.md", "[b](../../escape.md)").status == "broken"
    assert resolve(vault, "Home.md", "[b](nothing.md)").status == "broken"


def test_resolve_ref_forms_and_hints(vault):
    assert vault.resolve_ref("[[Beta Plan|x]]") == "Projects/Beta/Beta Plan.md"
    assert vault.resolve_ref("Beta Plan#^b1") == "Projects/Beta/Beta Plan.md"
    assert vault.resolve_ref("projects\\beta\\beta plan.md") == "Projects/Beta/Beta Plan.md"
    with pytest.raises(VaultError, match="Did you mean: .*Beta Plan.md"):
        vault.resolve_ref("Beta Plna")


def test_graph_backlinks_and_embeds(vault):
    inc = vault.graph.inc["Projects/Alpha/Alpha Plan.md"]
    srcs = {(r.src, r.link.embed) for r in inc}
    assert ("Home.md", True) in srcs  # ![[Alpha Plan]]
    assert ("Projects/Beta/Beta Plan.md", False) in srcs  # markdown link with %20
    assert ("Projects/Alpha/Alpha Plan.md", False) in srcs  # [[Alpha Plan#Goal]] self link
    assert vault.graph.amb_inc["Areas/Meeting Notes.md"]  # ambiguous links might point here


def test_code_links_not_in_graph(vault):
    targets = [r.link.target for r in vault.graph.out["Projects/Alpha/Alpha Plan.md"]]
    assert "Not A Link In Code" not in targets and "also not" not in targets and "nothing.md" not in targets


def test_tags_and_frontmatter(vault):
    a = vault.notes["Projects/Alpha/Alpha Plan.md"]
    assert a.fm["status"] == "draft" and a.fm["owner"] == "fadi"
    assert a.tags == ["project", "project/alpha"]
    b = vault.notes["Projects/Beta/Beta Plan.md"]
    assert b.tags == ["beta/launch", "project"]  # fm string tag + inline; #2024 excluded
    assert vault.notes["Broken FM.md"].fm_error and "line" in vault.notes["Broken FM.md"].fm_error
    assert vault.notes["Turkce/Şeker Notu.md"].title == "Şeker Notu"


# ------------------------------------------------------------------ search


def test_search_ranking_and_diacritics(vault):
    hits, missing = vault.search("seker cicek")
    assert hits[0][0] == "Turkce/Şeker Notu.md" and missing == []
    hits, missing = vault.search("beta plan zzzz")
    assert hits[0][0] == "Projects/Beta/Beta Plan.md" and missing == ["zzzz"]


def test_search_filters(vault):
    hits, _ = vault.search("plan", folder="Projects/Alpha")
    assert [p for p, _ in hits] == ["Projects/Alpha/Alpha Plan.md"]
    hits, _ = vault.search("plan", tag="project/alpha")
    assert [p for p, _ in hits] == ["Projects/Alpha/Alpha Plan.md"]
    hits, _ = vault.search("plan", tag="project")  # nested tags count
    assert len(hits) == 3  # Home, Alpha, Beta all carry project or project/alpha
    hits, _ = vault.search("plan standup", all_terms=True)
    assert hits == []


def test_title_outranks_body(tmp_path, cache_dir):
    d = str(tmp_path / "v")
    os.makedirs(d)
    for name, text in {
        "a.md": "# Gamma\n" + "filler words " * 5 + "\n",
        "b.md": "# Other\nthe word gamma appears once in the body of this note with other filler words\n",
    }.items():
        with open(os.path.join(d, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)
    v = Vault(d, cache_dir)
    v.refresh()
    assert [p for p, _ in v.search("gamma")[0]] == ["a.md", "b.md"]


# ------------------------------------------------------------------ refresh + cache


def test_refresh_incremental_and_cache(vault_dir, cache_dir):
    v1 = Vault(vault_dir, cache_dir)
    st = v1.refresh()
    assert st.parsed == 8 and st.from_cache == 0
    v2 = Vault(vault_dir, cache_dir)  # new process: everything comes from the cache
    st = v2.refresh()
    assert st.parsed == 0 and st.from_cache == 8
    assert v2.notes["Home.md"].links == v1.notes["Home.md"].links
    assert v2.search("beta")[0] == v1.search("beta")[0]
    assert v2.refresh().changed is False

    p = os.path.join(vault_dir, "Orphan.md")
    with open(p, "a", encoding="utf-8") as f:
        f.write("now mentions [[Home]] and zebra\n")
    os.utime(p, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))
    os.remove(os.path.join(vault_dir, "Areas", "Meeting Notes.md"))
    st = v2.refresh()
    assert st.parsed == 1 and st.removed == 1 and st.changed
    assert v2.search("zebra")[0][0][0] == "Orphan.md"
    assert any(r.src == "Orphan.md" for r in v2.graph.inc["Home.md"])
    # the Meeting Notes link is no longer ambiguous once one of the twins is gone
    assert resolve(v2, "Home.md", "[[Meeting Notes]]").status == "note"
    v3 = Vault(vault_dir, cache_dir)  # cache reflects the edit and the deletion
    st = v3.refresh()
    assert st.parsed == 0 and "Areas/Meeting Notes.md" not in v3.notes


def test_non_utf8_note_is_reported(vault_dir, cache_dir):
    with open(os.path.join(vault_dir, "bad.md"), "wb") as f:
        f.write(b"\xff\xfe broken \x80")
    v = Vault(vault_dir, cache_dir)
    v.refresh()
    assert "not valid UTF-8" in (v.notes["bad.md"].error or "")
    assert "bad.md" not in [p for p, _ in v.search("broken")[0]]


def test_corrupt_cache_is_an_explicit_error(vault_dir, cache_dir):
    v = Vault(vault_dir, cache_dir)
    v.refresh()
    with open(v.cache.path, "wb") as f:
        f.write(b"this is not sqlite" * 100)
    with pytest.raises(VaultError, match="delete that file"):
        Vault(vault_dir, cache_dir).refresh()


def test_missing_vault_dir(tmp_path):
    with pytest.raises(VaultError, match="does not exist"):
        Vault(str(tmp_path / "nope"))
