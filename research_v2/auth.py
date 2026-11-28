import hashlib
import os
import re
import secrets
from copy import copy
from urllib.parse import urlsplit

import uvicorn
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import Settings

RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
PUBLIC_PATHS = {"/", "/health", "/openapi.json", "/skill.md", "/llms.txt"}
OAUTH_PUBLIC_PATHS = {
    RESOURCE_METADATA_PATH, "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server", "/oauth/register", "/oauth/authorize", "/oauth/token",
}


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).digest()


class TokenRegistry:
    def __init__(self, token=None, tokens=None, *, extra_verifier=None):
        entries = [(token, "default")] if isinstance(token, str) and token else []
        for configured in (token if not isinstance(token, str) else None, tokens):
            if isinstance(configured, str):
                configured = configured.split(",")
            for entry in configured or ():
                label, separator, value = entry.strip().partition(":")
                if not separator:
                    label, value = "default", label
                if value := value.strip():
                    entries.append((value, label.strip() or "default"))
        self._tokens = tuple(dict.fromkeys((token_digest(value), label) for value, label in entries))
        self.extra_verifier = extra_verifier

    def __bool__(self):
        return bool(self._tokens)

    def verify(self, token: str) -> str | None:
        if not token:
            return None
        supplied = token_digest(token)
        matched = None
        for expected, label in self._tokens:
            if secrets.compare_digest(supplied, expected) and matched is None:
                matched = label
        if matched is None and self.extra_verifier is not None:
            return self.extra_verifier(token)
        return matched


class BearerGate:
    def __init__(
        self, app, token, *, public_paths=None, public_prefixes=None,
        oauth_metadata_url=None, extra_verifier=None,
    ):
        self.app = app
        self.registry = token if isinstance(token, TokenRegistry) else TokenRegistry(token)
        self.public_paths = {"/health", *(public_paths or ())}
        self.public_prefixes = tuple(public_prefixes or ())
        self.oauth_metadata_url = oauth_metadata_url
        self.extra_verifier = extra_verifier

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            if path not in self.public_paths and not path.startswith(self.public_prefixes):
                headers = Headers(scope=scope)
                authorization = headers.get("authorization")
                if authorization is None:
                    supplied = headers.get("x-api-key", "")
                else:
                    scheme, _, value = authorization.partition(" ")
                    supplied = value if scheme.lower() == "bearer" else ""
                label = self.registry.verify(supplied)
                if label is None and supplied and self.extra_verifier is not None:
                    label = self.extra_verifier(supplied)
                if label is None:
                    metadata_url = self.oauth_metadata_url
                    if callable(metadata_url):
                        metadata_url = metadata_url(scope)
                    challenge = f'Bearer resource_metadata="{metadata_url}"' if metadata_url else "Bearer"
                    response = JSONResponse(
                        {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": challenge}
                    )
                    return await response(scope, receive, send)
        await self.app(scope, receive, send)


def public_base_url(scope, configured=""):
    if configured:
        return configured.rstrip("/")
    headers = Headers(scope=scope)
    scheme = headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    if scheme not in {"http", "https"}:
        scheme = scope.get("scheme", "http")
    for header in ("x-forwarded-host", "host"):
        host = headers.get(header, "").split(",", 1)[0].strip()
        if re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[A-Fa-f0-9:.]+\])(?::[0-9]{1,5})?", host):
            try:
                if urlsplit(f"{scheme}://{host}").port != 0:
                    return f"{scheme}://{host}"
            except ValueError:
                continue
    return f"{scheme}://localhost"


def gateway_app(mcp, settings, *, no_auth=False):
    from .api import api_routes
    from .oauth import OAuthServer

    tokens = TokenRegistry(
        os.getenv("RESEARCH_MCP_TOKEN"), [
            *settings.api_tokens,
            *os.getenv("RESEARCH_API_TOKENS", "").split(","),
        ],
    )
    allow_no_auth = bool(no_auth) or os.getenv("RESEARCH_NO_AUTH", "").strip().lower() in {"1", "true", "yes"}
    if not tokens and not allow_no_auth and os.getenv("RESEARCH_TRUSTED_TUNNEL") != "1":
        raise RuntimeError(
            "Set RESEARCH_MCP_TOKEN, RESEARCH_API_TOKENS, or explicitly enable the authenticated Secure MCP Tunnel deployment"
        )

    def base_url(scope):
        return public_base_url(scope, settings.public_base_url)

    if tokens or allow_no_auth:
        # Accept the gateway hostname in production and through local port forwarding.
        # Keep the loopback transport settings on the original FastMCP instance.
        mcp = copy(mcp)
        mcp.settings = mcp.settings.model_copy(update={"transport_security": None})
        mcp._session_manager = None

    app = mcp.streamable_http_app()

    async def health(request):
        return JSONResponse({"status": "ok", "service": "research-v2"})

    app.routes.extend(api_routes(settings, base_url, auth_mode="token" if tokens else "none"))
    app.routes.append(Route("/health", health))
    if tokens:
        oauth = OAuthServer(settings.database, tokens, base_url) if settings.oauth_enabled else None
        if oauth is not None:
            app.routes.extend(oauth.routes())
        app = BearerGate(
            app, tokens, public_paths=PUBLIC_PATHS | (OAUTH_PUBLIC_PATHS if oauth else set()),
            oauth_metadata_url=(lambda scope: base_url(scope) + RESOURCE_METADATA_PATH) if oauth else None,
            extra_verifier=oauth.verify_access_token if oauth else None,
        )

    cors = CORSMiddleware(
        app, allow_origins=["*"], allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "Mcp-Session-Id", "MCP-Protocol-Version"],
        expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
    )

    async def dispatch(scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            if path in {"/mcp", "/openapi.json"} or path.startswith(("/api/v1/", "/.well-known/", "/oauth/")):
                return await cors(scope, receive, send)
        await app(scope, receive, send)

    return dispatch


def serve_http(mcp, port, host=None, settings=None, *, no_auth=False):
    settings = settings if settings is not None else Settings.load()
    host = host or settings.http_host or os.getenv("RESEARCH_HTTP_HOST", "127.0.0.1")
    uvicorn.run(
        gateway_app(mcp, settings, no_auth=no_auth), host=host, port=port, log_level="warning", access_log=False,
    )
