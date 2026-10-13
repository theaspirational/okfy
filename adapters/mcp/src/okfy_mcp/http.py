"""Streamable-HTTP transport: one shared bearer token, or JWTs from an issuer.

stdio is one process per agent session on the same machine. This is the other
shape: one long-running process on a host, reached over HTTP by every client,
the same seven tools, the same bundle, every client seeing every other's
writes on its next call. Who may call is decided in one of two ways:

`--token-file`: one shared token (`BearerToken`). Read from a FILE, never a
flag (a flag is visible in `ps` and shell history), long enough that guessing
is hopeless, checked before the MCP layer sees a byte. No users, scopes or
sessions: one token, one bundle, every holder equal, and the `actor` on a
write is whatever the client claims.

`--oidc-issuer`: every request carries a JWT access token minted by one
OpenID issuer (Authentik, Keycloak, ...), checked by `OidcVerifier` against
the issuer's published keys. The token says WHO (`sub`, which becomes the
ledger `actor`, so a write is signed by its author's identity and not by a
self-declared string) and WHAT (`scope`: `okfy:read` to call anything,
`okfy:write` for `okfy_write`/`okfy_propose`). Users, revocation and the
login itself live in the issuer, where they are managed once for every
service that trusts it. The MCP SDK's own auth middleware does the HTTP side
(401/403 in the OAuth shape, protected-resource metadata so an MCP client can
find the issuer); this module only verifies the token and names the actor.

Encryption is the proxy's job either way: this serves plain HTTP, so bind it
to loopback or a private interface and terminate TLS in front.
"""
import hmac
import json
import logging
import os
from pathlib import Path

import anyio
import httpx
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

from okfy.actor import ACTOR_RE

E_TOKEN = "E_MCP_HTTP_TOKEN"
E_UNAUTHORIZED = "E_MCP_UNAUTHORIZED"
E_ISSUER = "E_MCP_OIDC_ISSUER"
E_FORBIDDEN = "E_MCP_FORBIDDEN"
MIN_TOKEN_CHARS = 32
READ_SCOPE = "okfy:read"
WRITE_SCOPE = "okfy:write"
log = logging.getLogger(__name__)


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


class OidcVerifier:
    """mcp `TokenVerifier`: a JWT access token from one of `issuers`, RS256,
    signed by a key that issuer publishes at its `jwks_uri`, not expired, and
    meant for one of `audiences` (the client ids the issuers registered for
    this bundle: an `aud` check is what keeps a token minted for another
    service out of this one). Several issuers because an Authentik puts each
    registered client app under its own issuer path, and one bundle serves
    Claude, Claude Code and Codex alike; the token's `iss` picks the key set.
    Every discovery document is read ONCE, at construction, so a wrong
    `--oidc-issuer` fails the start (`E_MCP_OIDC_ISSUER`) instead of failing
    every request later; keys are fetched and cached by `kid` as tokens
    arrive, so a key rotation needs no restart."""

    def __init__(self, issuers: list[str], audiences: list[str]):
        if not issuers:
            raise ValueError(f"{E_ISSUER}: --oidc-issuer needs at least one "
                             "issuer URL")
        if not audiences:
            raise ValueError(f"{E_ISSUER}: --oidc-issuer needs at least one "
                             "--oidc-audience (the client id the issuer minted "
                             "this bundle's tokens for)")
        self._keys: dict[str, jwt.PyJWKClient] = {}
        for issuer in issuers:
            url = issuer.rstrip("/") + "/.well-known/openid-configuration"
            try:
                r = httpx.get(url, timeout=10, follow_redirects=True)
                r.raise_for_status()
                meta = r.json()
                name, jwks_uri = str(meta["issuer"]), str(meta["jwks_uri"])
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                raise ValueError(f"{E_ISSUER}: cannot read the issuer's discovery "
                                 f"document at {url} ({e.__class__.__name__}: {e}) — "
                                 "pass the issuer URL exactly as its own "
                                 "`.well-known/openid-configuration` states `issuer`"
                                 ) from e
            # The key fetch goes through urllib with its stock User-Agent,
            # which Cloudflare (in front of most issuers) answers 403; name
            # the client.
            self._keys[name] = jwt.PyJWKClient(jwks_uri, cache_keys=True,
                                               headers={"User-Agent": "okfy-mcp"})
        self.issuers = list(self._keys)         # as the issuers state them
        self.audiences = list(audiences)

    def _claims(self, token: str) -> dict:
        iss = str(jwt.decode(token, options={"verify_signature": False}).get("iss") or "")
        keys = self._keys.get(iss)
        if keys is None:
            raise jwt.InvalidIssuerError(f"issuer {iss!r} is not one of {self.issuers}")
        key = keys.get_signing_key_from_jwt(token).key   # blocking fetch
        return jwt.decode(token, key, algorithms=["RS256"], audience=self.audiences,
                          issuer=iss, options={"require": ["exp", "sub", "aud", "iss"]})

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await anyio.to_thread.run_sync(self._claims, token)
        except (jwt.PyJWTError, ValueError) as e:   # bad signature, expired, wrong aud/iss, unknown kid
            # the reason, never the token: the operator's only clue when a
            # client that logged in fine is still answered 401
            log.warning("token refused: %s: %s", e.__class__.__name__, e)
            return None
        sub = str(claims["sub"])
        return AccessToken(token=token, client_id=str(claims.get("azp") or sub),
                           scopes=str(claims.get("scope") or "").split(),
                           expires_at=int(claims["exp"]), subject=sub, claims=claims)


ACTOR_CLAIM = "okfy_actor"


def actor_for(claimed: str | None, *, write: bool = False) -> str | None:
    """The actor of this tool call. Under `OidcVerifier` it is what the
    verified token says — the `okfy_actor` claim when the issuer sets one
    (an issuer that serves several agent apps can then tell them apart, e.g.
    `claude:vasyl` and `codex:vasyl` for one human), else `sub` — as itself
    when that already is an OKF actor string (`human:vasyl`, `agent:codex`),
    else `oidc:<sub>`. The client's own claim is dropped: an identity the
    issuer signed outranks a string the client typed. A write also needs
    WRITE_SCOPE on the token, else `E_MCP_FORBIDDEN`. Without a token (stdio,
    or the shared file token) the claim is returned as is, for `check_actor`
    to validate as before."""
    tok = get_access_token()
    if tok is None:
        return claimed
    sub = str((tok.claims or {}).get(ACTOR_CLAIM) or tok.subject or tok.client_id)
    if write and WRITE_SCOPE not in tok.scopes:
        raise ValueError(
            f"{E_FORBIDDEN}: the token for {sub!r} carries scopes {sorted(tok.scopes)}, "
            f"not {WRITE_SCOPE}: it may read this bundle but not write to it — "
            "the issuer's admin grants the scope")
    return sub if ACTOR_RE.match(sub) else f"oidc:{sub}"
