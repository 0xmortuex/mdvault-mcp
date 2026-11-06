"""A 5,000-note vault: index, cache, search, backlinks and rename at realistic scale."""

import os
import time

import pytest
from vaultgen import make_big_vault

from mdvault_mcp import ops
from mdvault_mcp.index import Vault

from test_rename import assert_preserved, graph_state  # noqa: E402

pytestmark = pytest.mark.perf


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("big") / "vault")
    os.makedirs(root)
    paths = make_big_vault(root, n=5000)
    cache = str(tmp_path_factory.mktemp("bigcache"))
    return root, cache, paths


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t


def test_5000_notes_cold_warm_search_rename(big, capsys):
    root, cache, paths = big
    v = Vault(root, cache)
    st, cold = timed(v.refresh)
    assert st.parsed == 5000 and len(v.notes) == 5000
    _, graph_t = timed(lambda: v.graph)
    _, noop = timed(v.refresh)  # nothing changed: just stat every file
    v2 = Vault(root, cache)
    st2, warm = timed(v2.refresh)  # a restarted server: parsed notes come from the SQLite cache
    assert st2.parsed == 0 and st2.from_cache == 5000
    (hits, _), search_t = timed(lambda: v2.search("kernel scheduler memory"))
    assert len(hits) > 100
    (hits2, _), search2_t = timed(lambda: v2.search("kernel scheduler memory"))
    assert hits2 == hits
    most = max(v2.graph.inc, key=lambda p: len(v2.graph.inc[p]))
    _, links_t = timed(lambda: ops.note_links(v2, most, "both", 50))
    _, overview_t = timed(lambda: ops.vault_overview(v2))
    plan, dry_t = timed(lambda: ops.rename_note(v2, most, "Zeta Renamed Hub", True))
    assert plan.startswith("DRY RUN")
    with capsys.disabled():
        print(
            f"\n[perf 5000 notes] cold index {cold:.2f}s, graph {graph_t:.2f}s, no-op refresh {noop:.2f}s, "
            f"restart from cache {warm:.2f}s, first search {search_t:.2f}s (incl. postings), second {search2_t * 1000:.0f}ms, "
            f"note_links {links_t * 1000:.0f}ms, overview {overview_t:.2f}s, rename dry-run {dry_t:.2f}s"
        )
    # Generous ceilings (CI runners are slow); real numbers are printed above.
    assert cold < 120 and warm < 30 and noop < 15 and search2_t < 2 and links_t < 3 and dry_t < 10


def test_5000_notes_apply_rename_and_reparse(big):
    root, cache, paths = big
    v = Vault(root, cache)
    v.refresh()
    hub = max(v.graph.inc, key=lambda p: len({r.src for r in v.graph.inc[p]}))
    n_src = len({r.src for r in v.graph.inc[hub]})
    before = graph_state(root, cache + "-b")
    out = ops.rename_note(v, hub, "Archive/Hub Renamed", False)
    assert f"in {n_src}" in out or n_src == 0 or "file(s)" in out
    after = graph_state(root, cache + "-a")
    assert_preserved(before, after, hub, "Archive/Hub Renamed.md")
    broken_before = sum(1 for s, _ in before.values() if s == "broken")
    broken_after = sum(1 for s, _ in after.values() if s == "broken")
    assert broken_after == broken_before
    leftovers = [f for dp, _, fn in os.walk(root) for f in fn if f.startswith(".mdvault-")]
    assert leftovers == []
