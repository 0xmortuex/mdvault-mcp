import random

import pytest

from mdvault_mcp.frontmatter import FrontmatterError, dump_yaml, parse_yaml, split_frontmatter


def test_split_basic_and_offsets():
    text = "---\na: 1\n---\nbody\n"
    raw, off = split_frontmatter(text)
    assert raw == "a: 1\n" and text[off:] == "body\n"


def test_split_crlf_and_dots_close():
    text = "---\r\na: 1\r\n...\r\nbody"
    raw, off = split_frontmatter(text)
    assert raw == "a: 1\r\n" and text[off:] == "body"


@pytest.mark.parametrize("text", ["no frontmatter\n", "---\nunclosed: yes\nbody", " ---\na: 1\n---\n", ""])
def test_no_frontmatter(text):
    assert split_frontmatter(text) == (None, 0)


def test_empty_block():
    raw, off = split_frontmatter("---\n---\nbody")
    assert raw == "" and parse_yaml(raw) == {}


def test_scalars_and_types():
    d = parse_yaml(
        "title: My Note\nn: 42\nf: 3.5\nneg: -7\nzero: 007\nflag: true\nno: no\nnothing: ~\nempty:\n"
        "date: 2026-11-06\nurl: http://x.org/a#b\nwin: C:\\Users\\me\n"
    )
    assert d == {
        "title": "My Note", "n": 42, "f": 3.5, "neg": -7, "zero": "007", "flag": True,
        "no": "no", "nothing": None, "empty": None, "date": "2026-11-06",
        "url": "http://x.org/a#b", "win": "C:\\Users\\me",
    }


def test_quotes_and_escapes_and_comments():
    d = parse_yaml(
        "a: \"he said \\\"hi\\\"\\n\"  # comment\nb: 'it''s'  # c\nc: plain # trailing\nd: \"x # not a comment\"\n"
        "e: 'don''t # x'\n# full line comment\nf: 1\n"
    )
    assert d == {"a": 'he said "hi"\n', "b": "it's", "c": "plain", "d": "x # not a comment", "e": "don't # x", "f": 1}


def test_lists_and_nested():
    d = parse_yaml(
        "tags: [a, b, 'c d', [x, y]]\nempty: []\nm: {k: v, n: 2}\nblock:\n  - one\n  - two\nsame:\n- s1\n- s2\n"
        "nested:\n  inner:\n    deep: 1\n  other: [1, 2]\nitems:\n  - name: a\n    n: 1\n  - name: b\n"
    )
    assert d["tags"] == ["a", "b", "c d", ["x", "y"]]
    assert d["empty"] == [] and d["m"] == {"k": "v", "n": 2}
    assert d["block"] == ["one", "two"] and d["same"] == ["s1", "s2"]
    assert d["nested"] == {"inner": {"deep": 1}, "other": [1, 2]}
    assert d["items"] == [{"name": "a", "n": 1}, {"name": "b"}]


def test_block_scalars():
    d = parse_yaml("lit: |\n  line1\n  line2\nfold: >\n  a\n  b\n\n  c\nstrip: |-\n  x\nafter: 1\n")
    assert d["lit"] == "line1\nline2\n"
    assert d["fold"] == "a b\nc\n"
    assert d["strip"] == "x" and d["after"] == 1


def test_wikilink_in_property_quoted():
    assert parse_yaml('related: "[[Other Note]]"\n') == {"related": "[[Other Note]]"}


@pytest.mark.parametrize(
    "raw,needle",
    [
        ("key: [unclosed\n", "line 2"),
        ("a: 1\na: 2\n", "duplicate key"),
        ("a: &x 1\n", "unsupported"),
        ("a: *x\n", "unsupported"),
        ("a: !!str 1\n", "unsupported"),
        ("- a\n- b\n", "mapping"),
        ("just text\n", "key: value"),
        ("a: \"unterminated\n", "unterminated"),
        ("a: 1\n  b: 2\n", "indentation"),
        ("a:\n\tb: 1\n", "tabs"),
    ],
)
def test_errors_name_the_line(raw, needle):
    with pytest.raises(FrontmatterError) as e:
        parse_yaml(raw)
    assert needle in str(e.value)


def test_error_line_numbers_are_file_lines():
    with pytest.raises(FrontmatterError, match="line 4"):
        parse_yaml("a: 1\nb: 2\nc: [oops\n", first_line=2)


def test_dump_roundtrip_fixed():
    data = {
        "title": "Has: colon", "empty": "", "num_like": "123", "bool_like": "true", "yes": "yes",
        "tags": ["a", "b c", "#x", "- dash"], "n": 5, "f": 1.5, "flag": False, "nothing": None,
        "nested": {"k": "v", "deep": {"x": 1}}, "multi": "line1\nline2", "quote": 'say "hi"',
        "uni": "Şeker çiçek", "empty_list": [], "empty_map": {}, "lead": " space",
    }
    assert parse_yaml(dump_yaml(data)) == data


def test_dump_roundtrip_random():
    rng = random.Random(3)
    alphabet = list("abc XYZ:#-,[]{}'\"\\ \t\nşğü01.&*!|>%@`")

    def rand_str():
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))

    for _ in range(300):
        data = {f"k{i}": rng.choice([rand_str(), rng.randint(-5, 5), rng.random() > 0.5, None, [rand_str() for _ in range(rng.randint(0, 3))]]) for i in range(rng.randint(1, 5))}
        assert parse_yaml(dump_yaml(data)) == data, data


def test_dump_rejects_unsupported():
    with pytest.raises(FrontmatterError):
        dump_yaml({"a": [[1]]})
    with pytest.raises(FrontmatterError):
        dump_yaml({"a": {1, 2}})
