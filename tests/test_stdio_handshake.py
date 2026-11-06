"""MCP stdio smoke test: spawns the real installed server over stdio, checks the tool list,
a realistic session, and that instructive errors survive the trip to the agent
(MCPServer hides any exception that isn't a ToolError)."""

import os
import sys

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

EXPECTED_TOOLS = {
    "vault_overview",
    "search_notes",
    "read_note",
    "note_links",
    "query_notes",
    "broken_links",
    "create_note",
    "append_to_note",
    "edit_note",
    "rename_note",
}


def params(vault_dir, cache_dir, *extra):
    env = {**os.environ, "MDVAULT_CACHE_DIR": cache_dir, "MDVAULT_PATH": "", "MDVAULT_READ_ONLY": ""}
    return StdioServerParameters(command=sys.executable, args=["-m", "mdvault_mcp", "--vault", vault_dir, *extra], env=env)


def text(res):
    return res.content[0].text


@pytest.mark.anyio
async def test_tools_and_a_session_over_stdio(vault_dir, cache_dir):
    async with stdio_client(params(vault_dir, cache_dir)) as (r, w), ClientSession(r, w) as s:
        init = await s.initialize()
        assert init.server_info.name == "mdvault"
        tools = (await s.list_tools()).tools
        assert {t.name for t in tools} == EXPECTED_TOOLS
        assert all(len(t.description or "") > 80 for t in tools)  # every tool is documented for the agent

        res = await s.call_tool("vault_overview", {})
        assert not res.is_error and "8 notes" in text(res)
        res = await s.call_tool("search_notes", {"query": "seker cicek"})
        assert "Turkce/Şeker Notu.md" in text(res)
        res = await s.call_tool("query_notes", {"where": "status=draft AND tags contains project"})
        assert "Alpha Plan.md" in text(res)
        res = await s.call_tool("note_links", {"note": "[[Alpha Plan]]", "direction": "in"})
        assert "Home.md" in text(res)
        res = await s.call_tool("create_note", {"path": "Inbox/Idea", "content": "hi [[Home]]", "frontmatter": {"status": "new"}})
        assert not res.is_error and "Created Inbox/Idea.md" in text(res)
        res = await s.call_tool("append_to_note", {"text": "- logged", "daily": True, "date": "2026-11-06"})
        assert "Created and appended" in text(res)
        res = await s.call_tool("edit_note", {"note": "Inbox/Idea", "old_text": "hi", "new_text": "hello"})
        assert "Edited" in text(res)
        res = await s.call_tool("rename_note", {"note": "Inbox/Idea", "new_name": "Inbox/Better Idea"})
        assert "DRY RUN" in text(res) and "Home.md" not in text(res).split("updated:")[0]
        res = await s.call_tool("rename_note", {"note": "Inbox/Idea", "new_name": "Inbox/Better Idea", "dry_run": False})
        assert "Renamed" in text(res)
        res = await s.call_tool("read_note", {"note": "Better Idea"})
        assert "hello [[Home]]" in text(res) and '"status": "new"' in text(res)
        res = await s.call_tool("broken_links", {})
        assert "Does Not Exist" in text(res)


@pytest.mark.anyio
async def test_instructive_errors_reach_the_agent(vault_dir, cache_dir):
    async with stdio_client(params(vault_dir, cache_dir)) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool("read_note", {"note": "Meeting Notes"})
        assert res.is_error and "ambiguous" in text(res) and "Areas/Meeting Notes.md" in text(res)
        res = await s.call_tool("read_note", {"note": "Beta Plna"})
        assert res.is_error and "Did you mean" in text(res)
        res = await s.call_tool("create_note", {"path": "../escape.md", "content": "x"})
        assert res.is_error and "'..'" in text(res)
        res = await s.call_tool("create_note", {"path": "Home.md", "content": "x"})
        assert res.is_error and "overwrite=true" in text(res)
        res = await s.call_tool("query_notes", {"where": "status ~~ x"})
        assert res.is_error and "Query syntax" in text(res)
        res = await s.call_tool("edit_note", {"note": "Home", "old_text": "[[", "new_text": "(("})
        assert res.is_error and "places" in text(res)
        res = await s.call_tool("search_notes", {"query": "x", "limit": 0})
        assert res.is_error and "limit" in text(res)


@pytest.mark.anyio
async def test_read_only_mode(vault_dir, cache_dir):
    before = sorted(os.listdir(vault_dir))
    async with stdio_client(params(vault_dir, cache_dir, "--read-only")) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool("search_notes", {"query": "beta"})
        assert not res.is_error
        for name, args in [
            ("create_note", {"path": "x.md"}),
            ("append_to_note", {"text": "x", "note": "Home"}),
            ("edit_note", {"note": "Home", "old_text": "Welcome", "new_text": "Hi"}),
            ("rename_note", {"note": "Orphan", "new_name": "Other", "dry_run": False}),
        ]:
            res = await s.call_tool(name, args)
            assert res.is_error and "--read-only" in text(res)
    assert sorted(os.listdir(vault_dir)) == before
