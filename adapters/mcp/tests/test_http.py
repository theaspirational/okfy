"""The HTTP transport: one process, many clients, one token — or one issuer.

What is checked: no token file / short token → the server refuses to start;
no header or a wrong token → 401 with the envelope, nothing reaches MCP; the
right token → the seven tools, and a write from one client is found by the
next client's search without a restart — the cross-app case the transport
exists for. Then the issuer mode against a stub OpenID issuer that mints
real RS256 tokens: who signed the token is who wrote the concept, a token
without `okfy:write` reads but cannot write, and foreign or expired tokens
never reach a tool.
"""
import asyncio
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from okfy import frontmatter
from okfy.init import init_bundle
from okfy_mcp.http import (E_FORBIDDEN, E_ISSUER, E_TOKEN, E_UNAUTHORIZED,
                           OidcVerifier, load_token)
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

    def __init__(self, app, path="/mcp"):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.path = path

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return f"http://127.0.0.1:{self.port}{self.path}"
            self.thread.join(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)


def _client(token: str, **headers: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"Authorization": f"Bearer {token}", **headers},
                             timeout=30)


async def _call(url: str, token: str, tool: str, args: dict, **headers: str) -> dict:
    async with streamable_http_client(url, http_client=_client(token, **headers)) as (r, w, _):
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


class _Issuer:
    """A stub OpenID issuer, the shape Authentik or Keycloak present: a
    discovery document, a JWKS with one RS256 key under a `kid`, and a
    `mint` that signs whatever claims a test wants."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        self.jwks = {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}
        self.url = ""

    def app(self):
        return Starlette(routes=[
            Route("/.well-known/openid-configuration", lambda r: JSONResponse(
                {"issuer": self.url, "jwks_uri": self.url + "/jwks"})),
            Route("/jwks", lambda r: JSONResponse(self.jwks)),
        ])

    def mint(self, sub: str, scope: str = "okfy:read okfy:write", aud="okfy-memory",
             exp: int | None = None, iss: str | None = None, kid="k1", **extra) -> str:
        claims = {"iss": iss or self.url, "sub": sub, "aud": aud, "scope": scope,
                  "exp": exp or int(time.time()) + 300, "iat": int(time.time()), **extra}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": kid})


class _Authentik(_Issuer):
    """An issuer that, like Authentik, puts every client under its own path:
    `<url>/o/<slug>` is an issuer of its own, all signed by one key."""

    def app(self):
        def meta(r):
            slug = r.path_params["slug"]
            return JSONResponse({"issuer": f"{self.url}/o/{slug}",
                                 "jwks_uri": f"{self.url}/o/{slug}/jwks"})
        return Starlette(routes=[
            Route("/o/{slug}/.well-known/openid-configuration", meta),
            Route("/o/{slug}/jwks", lambda r: JSONResponse(self.jwks)),
        ])

    def mint_for(self, slug: str, sub: str, **extra) -> str:
        return self.mint(sub, iss=f"{self.url}/o/{slug}", **extra)


def _post(url: str, token: str) -> int:
    return httpx.post(url, json={}, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
    }).status_code


@pytest.fixture
def issuer():
    iss = _Issuer()
    with _Server(iss.app(), path="") as url:
        iss.url = url
        yield iss


def test_issuer_mode_refuses_to_start_unchecked(issuer):
    with pytest.raises(ValueError, match=f"^{E_ISSUER}: .*at least one issuer"):
        OidcVerifier([], ["okfy-memory"])
    with pytest.raises(ValueError, match=f"^{E_ISSUER}: .*at least one --oidc-audience"):
        OidcVerifier([issuer.url], [])
    with pytest.raises(ValueError, match=f"^{E_ISSUER}: cannot read"):
        OidcVerifier(["http://127.0.0.1:9/nowhere"], ["okfy-memory"])
    v = OidcVerifier([issuer.url + "/"], ["okfy-memory"])   # trailing slash tolerated
    assert v.issuers == [issuer.url]                        # ... and the issuer's own word wins


def test_one_bundle_several_issuers(root, issuer):
    """An Authentik puts each client app under its own issuer path: one
    server trusts them all, the token's `iss` picks the keys, and a token
    naming an issuer it was not signed by is refused."""
    other = _Issuer()
    with _Server(other.app(), path="") as other_url:
        other.url = other_url
        oidc = OidcVerifier([issuer.url, other.url], ["okfy-memory"])
        assert oidc.issuers == [issuer.url, other.url]
        with _Server(http_app(root, oidc=oidc, public_url="http://memory.test/mcp")) as url:
            meta = httpx.get(url[:-len("/mcp")] + "/.well-known/oauth-protected-resource/mcp").json()
            assert [u.rstrip("/") for u in meta["authorization_servers"]] == [issuer.url, other.url]
            for tok in (issuer.mint("human:a"), other.mint("human:b")):
                assert "okfy_query" in asyncio.run(_tools(url, tok))
            for tok in (other.mint("human:b", iss=issuer.url),      # signed by one, claims the other
                        other.mint("human:b", iss="http://third.test")):
                r = httpx.post(url, json={}, headers={
                    "Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
                assert r.status_code == 401


def test_clients_the_issuer_registered_itself(root):
    """An MCP client that registered itself with the issuer gets its own
    issuer path and a client id nobody configured: trusted under the prefix,
    signature checked, audience not. Everything else is still refused."""
    ak = _Authentik()
    with _Server(ak.app(), path="") as url:
        ak.url = url
        oidc = OidcVerifier([url + "/o/mcp"], ["okfy-memory"], prefixes=[url + "/o/"])
        assert oidc.issuers == [url + "/o/mcp"]            # the metadata names the explicit one
        with _Server(http_app(root, oidc=oidc, public_url="http://memory.test/mcp")) as murl:
            fresh = ak.mint_for("claude-7f3a", "human:friend", aud="7f3a-client")
            assert "okfy_query" in asyncio.run(_tools(murl, fresh))
            assert _post(murl, ak.mint_for("mcp", "human:friend", aud="7f3a-client")) == 401  # explicit: aud checked
            assert _post(murl, ak.mint("human:x", iss=url + "/elsewhere/x")) == 401         # not under the prefix
            assert _post(murl, _Issuer().mint("human:x", iss=url + "/o/claude-7f3a")) == 401  # under it, wrong key


def test_write_group_outranks_the_scope(root, issuer):
    """The client may ask for okfy:write for anyone; only a user the issuer
    puts in the write group gets it."""
    oidc = OidcVerifier([issuer.url], ["okfy-memory"], write_group="owners")
    with _Server(http_app(root, oidc=oidc, public_url="http://memory.test/mcp")) as url:
        sha = asyncio.run(_call(url, issuer.mint("human:guest"), "okfy_show",
                                {"concept_id": "notes/a"}))["sha256"]
        args = {"concept_id": "notes/a", "expect_sha256": sha, "channel": "direct",
                "patch": [{"old": "First", "new": "Guest", "count": 1}]}
        for tok in (issuer.mint("human:guest", groups=["friends"]), issuer.mint("human:guest")):
            assert asyncio.run(_call(url, tok, "okfy_write", args))["error"] == E_FORBIDDEN
        out = asyncio.run(_call(url, issuer.mint("human:owner", groups=["owners"]),
                                "okfy_write", args))
        assert "error" not in out and "Guest" in (root / "notes/a.md").read_text(encoding="utf-8")


def test_read_group_closes_the_bundle(root, issuer):
    """One issuer, several bundles: outside the read group nothing is served,
    whatever scopes the client asked for; inside it, the write group still
    decides the writes."""
    oidc = OidcVerifier([issuer.url], ["okfy-memory"], write_group="victor",
                        read_group="victor")
    with _Server(http_app(root, oidc=oidc, public_url="http://memory.test/mcp")) as url:
        for tok in (issuer.mint("human:guest"), issuer.mint("human:guest", groups=["friends"])):
            assert _post(url, tok) == 403
        assert "notes/a" in asyncio.run(_call(url, issuer.mint("human:victor", groups=["victor"]),
                                              "okfy_show", {"concept_id": "notes/a"}))["id"]


def test_who_signed_the_token_wrote_the_concept(root, issuer):
    oidc = OidcVerifier([issuer.url], ["okfy-memory"])
    with _Server(http_app(root, oidc=oidc, public_url="http://memory.test/mcp")) as url:
        base = url[:-len("/mcp")]
        # an MCP client that gets 401 is told where the issuer is
        r = httpx.post(url, json={}, headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401 and "resource_metadata=" in r.headers["www-authenticate"]
        meta = httpx.get(base + "/.well-known/oauth-protected-resource/mcp").json()
        assert [u.rstrip("/") for u in meta["authorization_servers"]] == [issuer.url]
        # both scopes are advertised, though only okfy:read is required to call
        assert meta["scopes_supported"] == ["okfy:read", "okfy:write", "offline_access"]
        # tokens that never reach a tool: wrong issuer, wrong audience, expired,
        # unknown key, the old shared token, no okfy:read
        bad = [issuer.mint("human:x", iss="http://other.test"),
               issuer.mint("human:x", aud="ray-exomem"),
               issuer.mint("human:x", exp=int(time.time()) - 10),
               issuer.mint("human:x", kid="k9"), TOKEN]
        for tok in bad:
            r = httpx.post(url, json={}, headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {tok}"})
            assert r.status_code == 401, tok
        r = httpx.post(url, json={}, headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {issuer.mint('human:x', scope='profile')}"})
        assert r.status_code == 403

        # a friend with read only: the search works, the write is refused by
        # scope, before the lane, and nothing is written
        friend = issuer.mint("human:friend", scope="okfy:read")
        shown = asyncio.run(_call(url, friend, "okfy_show", {"concept_id": "notes/a"}))
        out = asyncio.run(_call(url, friend, "okfy_write", {
            "concept_id": "notes/a", "expect_sha256": shown["sha256"],
            "patch": [{"old": "First line.", "new": "Friend was here.", "count": 1}]}))
        assert out["error"] == E_FORBIDDEN and "human:friend" in out["message"]
        assert (root / "notes/a.md").read_text(encoding="utf-8") == NOTE

        # an agent with write: the ledger names the token's subject, not the
        # actor the client typed; a subject that is no OKF actor gets `oidc:`
        agent = issuer.mint("agent:claude-code")
        out = asyncio.run(_call(url, agent, "okfy_write", {
            "concept_id": "notes/a", "actor": "phone-app/1.0",
            "expect_sha256": shown["sha256"],
            "patch": [{"old": "First line.", "new": "Agent was here.", "count": 1}]}))
        assert (out["channel"], out["persisted"]) == ("direct", True)
        rows = [json.loads(ln) for ln in
                (root / "meta/memory.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["actor"] == "agent:claude-code"
        hashed = issuer.mint("3f2a9c")                      # Authentik's default subject shape
        out = asyncio.run(_call(url, hashed, "okfy_write", {
            "concept_id": "notes/a", "expect_sha256": out["sha256"],
            "patch": [{"old": "Agent", "new": "Hash", "count": 1}]}))
        assert out["persisted"] is True
        rows = [json.loads(ln) for ln in
                (root / "meta/memory.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["actor"] == "oidc:3f2a9c"
        # an issuer that names the actor outright (one human, several apps);
        # the request arrives as through the reverse proxy at public_url —
        # the SDK's host guard must accept that Host, not only localhost
        named = issuer.mint("vasyl", okfy_actor="claude:vasyl")
        out = asyncio.run(_call(url, named, "okfy_write", {
            "concept_id": "notes/a", "expect_sha256": out["sha256"],
            "patch": [{"old": "Hash", "new": "Named", "count": 1}]}, Host="memory.test"))
        assert out["persisted"] is True
        rows = [json.loads(ln) for ln in
                (root / "meta/memory.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["actor"] == "claude:vasyl"
