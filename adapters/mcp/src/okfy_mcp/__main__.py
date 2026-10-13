import argparse
import os
import sys
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="okfy-mcp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the stdio MCP server for a bundle/workspace")
    s.add_argument("path", type=Path)
    # v0.24 (d): opt-in usage journal — off unless a path is given, here or
    # via OKFY_MCP_JOURNAL (the flag wins when both are set). No query TEXT
    # is ever journaled unless --journal-text is ALSO given.
    s.add_argument("--journal", type=Path, default=None,
                   help="append a JSONL row per tool call to this file "
                        "(refused if it resolves inside the served bundle/"
                        "workspace); also read from OKFY_MCP_JOURNAL")
    s.add_argument("--journal-text", dest="journal_text", action="store_true",
                   help="also journal the raw query text (off by default: "
                        "the journal never carries query text otherwise)")
    # streamable HTTP for many clients at once, behind one bearer token; the
    # token comes from a FILE (never a flag: flags show in `ps`), and HTTP
    # without one is refused (E_MCP_HTTP_TOKEN) — there is no open mode
    s.add_argument("--http", action="store_true",
                   help="serve streamable HTTP at http://HOST:PORT/mcp instead "
                        "of stdio; requires --token-file (or OKFY_MCP_TOKEN_FILE)")
    s.add_argument("--host", default="127.0.0.1",
                   help="--http bind address (default 127.0.0.1; plain HTTP, "
                        "so put TLS in front before binding wider)")
    s.add_argument("--port", type=int, default=8765, help="--http port (default 8765)")
    s.add_argument("--token-file", dest="token_file", type=Path, default=None,
                   help="file holding the one bearer token every --http client "
                        "must send (one line, 32+ characters, no whitespace)")
    # the other --http access model: JWTs from one OpenID issuer; who wrote
    # a concept is then the token's subject, not a string the client typed
    s.add_argument("--oidc-issuer", dest="oidc_issuer", default=None,
                   help="--http: accept JWT access tokens from this OpenID issuer "
                        "URL (its .well-known/openid-configuration is read at "
                        "start) instead of --token-file; tokens need scope "
                        "okfy:read, writes also okfy:write")
    s.add_argument("--oidc-audience", dest="oidc_audiences", action="append",
                   default=[], metavar="CLIENT_ID",
                   help="with --oidc-issuer: an `aud` the token must carry "
                        "(repeatable; at least one)")
    s.add_argument("--public-url", dest="public_url", default=None,
                   help="with --oidc-issuer: the URL clients reach this server "
                        "at, e.g. https://memory.example/mcp (default "
                        "http://HOST:PORT/mcp); published as the protected-"
                        "resource metadata so an MCP client can find the issuer")
    c = sub.add_parser("config", help="print an MCP client config snippet")
    c.add_argument("path", type=Path)
    c.add_argument("--client", choices=["claude-code", "claude-desktop", "cursor"],
                   default="claude-code")
    c.add_argument("--name", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        from okfy_mcp.server import serve, serve_http
        journal = a.journal or (Path(os.environ["OKFY_MCP_JOURNAL"])
                                if os.environ.get("OKFY_MCP_JOURNAL") else None)
        try:
            if a.http:
                serve_http(a.path, a.host, a.port, a.token_file,
                           journal=journal, journal_text=a.journal_text,
                           oidc_issuer=a.oidc_issuer, oidc_audiences=a.oidc_audiences,
                           public_url=a.public_url)
            else:
                serve(a.path, journal=journal, journal_text=a.journal_text)
        except ValueError as e:        # a coded startup refusal, not a traceback
            print(e, file=sys.stderr)
            return 2
        return 0
    if a.cmd == "config":
        from okfy_mcp.config import snippet
        print(snippet(a.path, client=a.client, name=a.name))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
