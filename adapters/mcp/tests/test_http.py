"""The HTTP transport: one process, many clients, one token.

What is checked: no token file / short token → the server refuses to start;
no header or a wrong token → 401 with the envelope, nothing reaches MCP; the
right token → the seven tools, and a write from one client is found by the
next client's search without a restart — the cross-app case the transport
exists for.
"""
import asyncio
import json
import socket
import subprocess
import threading
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from okfy import frontmatter
from okfy.init import init_bundle
from okfy_mcp.http import E_TOKEN, E_UNAUTHORIZED, load_token
from okfy_mcp.server import http_app

TOKEN = "a" * 40
NOTE = "---\ntype: note\ntitle: A\ndescription: first\n---\n\n# A\n\nFirst line.\n"
PLAN = ("---\ntype: ExtractionPlan\ntitle: t\narchetype: decision-support\n"
        "archetype_version: 1\ntypes:\n  note: notes\nsegments: []\n---\n\nPlan.\n")


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "okfy test")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "test@example.invalid")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("# A\n", encoding="utf-8")
    root = init_bundle(tmp_path / "b", corpus)
    purpose = root / "meta/purpose.md"
    meta, body = frontmatter.parse(purpose.read_text(encoding="utf-8"))
    meta["direct"] = {"types": ["note"]}
    purpose.write_text(frontmatter.serialize(meta, body), encoding="utf-8")
    (root / "meta/extraction-plan.md").write_text(PLAN, encoding="utf-8")
    (root / "notes").mkdir()
    (root / "notes/a.md").write_text(NOTE, encoding="utf-8")
    for args in (("add", "-A"), ("commit", "-q", "-m", "fixture")):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def test_no_token_no_server(tmp_path, monkeypatch):
    monkeypatch.delenv("OKFY_MCP_TOKEN_FILE", raising=False)
    with pytest.raises(ValueError, match=f"^{E_TOKEN}: "):
        load_token(None)
    short = tmp_path / "t"
    short.write_text("too-short\n", encoding="utf-8")
    with pytest.raises(ValueError, match=f"^{E_TOKEN}: .*at least 32"):
        load_token(short)
    with pytest.raises(ValueError, match=f"^{E_TOKEN}: cannot read"):
        load_token(tmp_path / "missing")
    good = tmp_path / "g"
    good.write_text(TOKEN + "\n", encoding="utf-8")
    assert load_token(good) == TOKEN
    monkeypatch.setenv("OKFY_MCP_TOKEN_FILE", str(good))
    assert load_token(None) == TOKEN                      # the env var names the file


class _Server:
    """uvicorn on a free loopback port, in a thread, for one test."""

    def __init__(self, app):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return f"http://127.0.0.1:{self.port}/mcp"
            self.thread.join(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)


def _client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)


async def _call(url: str, token: str, tool: str, args: dict) -> dict:
    async with streamable_http_client(url, http_client=_client(token)) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            return json.loads(res.content[0].text)


async def _tools(url: str, token: str) -> list[str]:
    async with streamable_http_client(url, http_client=_client(token)) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return sorted(t.name for t in (await s.list_tools()).tools)


def test_one_server_many_clients_one_token(root):
    with _Server(http_app(root, TOKEN)) as url:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"}}}
        accept = {"Accept": "application/json, text/event-stream"}
        # no header, wrong token, wrong scheme: 401 with the envelope, and the
        # MCP layer never answered
        for headers in ({}, {"Authorization": f"Bearer {TOKEN[:-1]}b"},
                        {"Authorization": f"Basic {TOKEN}"}):
            r = httpx.post(url, json=init, headers={**accept, **headers})
            assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
            assert set(r.json()) == {"error", "message", "way_out"}
            assert r.json()["error"] == E_UNAUTHORIZED

        assert _tools_sync(url) == ["okfy_fresh", "okfy_links", "okfy_overview",
                                    "okfy_propose", "okfy_query", "okfy_show",
                                    "okfy_write"]
        # client 1 writes; client 2 (a fresh connection, no shared session)
        # finds it on its next search — no restart, no approval in between
        shown = asyncio.run(_call(url, TOKEN, "okfy_show", {"concept_id": "notes/a"}))
        out = asyncio.run(_call(url, TOKEN, "okfy_write", {
            "concept_id": "notes/a", "actor": "phone-app/1.0",
            "expect_sha256": shown["sha256"],
            "patch": [{"old": "First line.", "new": "Remember zebra-quartz.", "count": 1}]}))
        assert (out["channel"], out["persisted"]) == ("direct", True)
        assert "observed" not in out
        found = asyncio.run(_call(url, TOKEN, "okfy_query", {"text": "zebra-quartz"}))
        assert [h["id"] for h in found["results"]] == ["notes/a"]
        # and the stale client is refused with the current sha, as over stdio
        again = asyncio.run(_call(url, TOKEN, "okfy_write", {
            "concept_id": "notes/a", "actor": "laptop/1.0",
            "expect_sha256": shown["sha256"],
            "patch": [{"old": "Remember", "new": "Forget", "count": 1}]}))
        assert again["error"] == "E_WRITE_BASE_MOVED"
        assert again["current_sha256"] == out["sha256"]


def _tools_sync(url: str) -> list[str]:
    return asyncio.run(_tools(url, TOKEN))
