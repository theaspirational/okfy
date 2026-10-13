"""Streamable-HTTP transport with one shared bearer token.

stdio is one process per agent session on the same machine. This is the other
shape: one long-running process on a host, reached over HTTP by every client
that holds the token — the same seven tools, the same bundle, every client
seeing every other's writes on its next call.

The token is the whole access model. It is read from a FILE, never from a
flag (a flag is visible in `ps` and shell history), it must be long enough
that guessing is hopeless, and a request without it is refused before the MCP
layer sees a byte. There are no users, scopes or sessions: one token, one
bundle, every holder equal. Anything finer belongs in front of this server
(a reverse proxy with its own users), not in it. Encryption is also the
proxy's job: this serves plain HTTP, so bind it to loopback or a private
interface and terminate TLS in front.
"""
import hmac
import json
import os
from pathlib import Path

E_TOKEN = "E_MCP_HTTP_TOKEN"
E_UNAUTHORIZED = "E_MCP_UNAUTHORIZED"
MIN_TOKEN_CHARS = 32


def load_token(path: Path | None) -> str:
    """The bearer token from `path` (or `$OKFY_MCP_TOKEN_FILE`), stripped.
    Refuses (`E_MCP_HTTP_TOKEN`) when no file was named, it cannot be read,
    or the token is shorter than MIN_TOKEN_CHARS: HTTP without a token would
    hand the bundle to anyone who can reach the port, so there is no
    unauthenticated mode to fall into."""
    if path is None and os.environ.get("OKFY_MCP_TOKEN_FILE"):
        path = Path(os.environ["OKFY_MCP_TOKEN_FILE"])
    if path is None:
        raise ValueError(
            f"{E_TOKEN}: --http needs a bearer token — pass --token-file <file> "
            "(or set OKFY_MCP_TOKEN_FILE); make one with "
            "`openssl rand -hex 32 > <file>; chmod 600 <file>`")
    try:
        token = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ValueError(f"{E_TOKEN}: cannot read token file {path}: "
                         f"{e.strerror or e}") from e
    if len(token) < MIN_TOKEN_CHARS or any(ch.isspace() for ch in token):
        raise ValueError(
            f"{E_TOKEN}: the token in {path} must be one line of at least "
            f"{MIN_TOKEN_CHARS} characters with no whitespace — "
            "`openssl rand -hex 32 > <file>` makes a fitting one")
    return token


class BearerToken:
    """ASGI middleware: every HTTP request must carry
    `Authorization: Bearer <token>`, compared in constant time. Anything
    else is answered 401 with the documented `{error, message, way_out}`
    envelope and never reaches `app`. Non-HTTP scopes (lifespan) pass
    through untouched so the MCP session manager still starts and stops."""

    def __init__(self, app, token: str):
        self.app = app
        self._expected = token.encode("utf-8")

    def _authorized(self, scope) -> bool:
        for name, value in scope.get("headers", ()):
            if name == b"authorization":
                scheme, _, presented = value.partition(b" ")
                return (scheme.lower() == b"bearer"
                        and hmac.compare_digest(presented.strip(), self._expected))
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        body = json.dumps({
            "error": E_UNAUTHORIZED,
            "message": f"{E_UNAUTHORIZED}: this request carries no valid bearer token",
            "way_out": "send the server's token as `Authorization: Bearer <token>` "
                       "on every request",
        }).encode("utf-8")
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                (b"www-authenticate", b"Bearer")]})
        await send({"type": "http.response.body", "body": body})
