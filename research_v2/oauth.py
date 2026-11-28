import base64
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from html import escape
from ipaddress import IPv6Address
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from .auth import RESOURCE_METADATA_PATH, token_digest

SCOPE = "research:read"
CODE_TTL = 5 * 60
ACCESS_TTL = 30 * 24 * 60 * 60
REFRESH_TTL = 90 * 24 * 60 * 60
GRANT_TYPES = ["authorization_code", "refresh_token"]
AUTH_METHODS = ["none", "client_secret_post"]
CSRF_COOKIE = "research_oauth_browser"
SENSITIVE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


def oauth_error(error, description=None):
    data = {"error": error}
    if description:
        data["error_description"] = description
    return JSONResponse(data, status_code=400, headers=SENSITIVE_HEADERS)


async def read_parameters(request, *, allow_json=False):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 32_768:
            raise ValueError("OAuth request body exceeds 32 KiB.")
        body.extend(chunk)
    if allow_json and content_type == "application/json":
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        # Reject escaped lone surrogates before passing strings to SQLite or HTML.
        json.dumps(data, ensure_ascii=False).encode("utf-8")
        return data
    if content_type == "application/x-www-form-urlencoded":
        values = parse_qs(body.decode(), keep_blank_values=True, max_num_fields=32, errors="strict")
        if any(len(items) != 1 for items in values.values()):
            raise ValueError("Repeated OAuth parameter.")
        return {key: items[0] for key, items in values.items()}
    raise ValueError("Unsupported OAuth request content type.")


def valid_redirect_uri(value):
    if not isinstance(value, str) or re.search(r"[\\#\s\x00-\x1f\x7f]", value):
        return False
    try:
        uri = urlsplit(value)
        host = uri.hostname
        if uri.port == 0 or not host or uri.username is not None or uri.password is not None:
            return False
        if uri.netloc.startswith("["):
            IPv6Address(host)
        elif not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                     for label in host.encode("idna").decode("ascii").removesuffix(".").split(".")):
            return False
        return uri.scheme == "https" or (
            uri.scheme == "http" and host in {"localhost", "127.0.0.1"}
        )
    except ValueError:
        return False


@dataclass(frozen=True)
class Authorization:
    client_id: str
    client_name: str
    redirect_uri: str
    challenge: str
    resource: str
    state: str | None
    browser: str
    expires: float


class OAuthServer:
    def __init__(self, database, registry, base_url):
        self.registry = registry
        self.base_url = base_url
        self.pending = OrderedDict()
        self.database = database
        self.memory_db = sqlite3.connect(database) if database == ":memory:" else None
        if self.memory_db is None:
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    client_name TEXT NOT NULL,
                    redirect_uris TEXT NOT NULL,
                    auth_method TEXT NOT NULL,
                    secret_hash BLOB
                );
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code_hash BLOB PRIMARY KEY NOT NULL,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    token_label TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    access_hash BLOB PRIMARY KEY NOT NULL,
                    refresh_hash BLOB UNIQUE NOT NULL,
                    client_id TEXT NOT NULL,
                    token_label TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    access_expires REAL NOT NULL,
                    refresh_expires REAL NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = self.memory_db if self.memory_db is not None else sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            if db is not self.memory_db:
                db.close()

    def routes(self):
        return [
            Route("/.well-known/oauth-protected-resource", self.protected_resource),
            Route(RESOURCE_METADATA_PATH, self.protected_resource),
            Route("/.well-known/oauth-authorization-server", self.authorization_server),
            Route("/oauth/register", self.register, methods=["POST"]),
            Route("/oauth/authorize", self.authorize),
            Route("/oauth/authorize", self.approve, methods=["POST"]),
            Route("/oauth/token", self.token, methods=["POST"]),
        ]

    async def protected_resource(self, request):
        base = self.base_url(request.scope)
        return JSONResponse({
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [SCOPE],
        })

    async def authorization_server(self, request):
        base = self.base_url(request.scope)
        return JSONResponse({
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": GRANT_TYPES,
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": AUTH_METHODS,
            "scopes_supported": [SCOPE],
            "authorization_response_iss_parameter_supported": True,
        })

    async def register(self, request):
        try:
            data = await read_parameters(request, allow_json=True)
        except ValueError:
            return oauth_error("invalid_client_metadata", "Expected a JSON client registration object.")
        redirects = data.get("redirect_uris")
        if not isinstance(redirects, list) or not redirects or not all(map(valid_redirect_uri, redirects)):
            return oauth_error("invalid_redirect_uri", "Use HTTPS or HTTP on localhost or 127.0.0.1.")
        method = data.get("token_endpoint_auth_method", "none")
        name = data.get("client_name", "MCP client")
        if method not in AUTH_METHODS or not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            return oauth_error("invalid_client_metadata", "Invalid client name or authentication method.")
        client_id = secrets.token_urlsafe(24)
        secret = secrets.token_urlsafe(32) if method == "client_secret_post" else None
        with self.connect() as db:
            db.execute(
                "INSERT INTO oauth_clients VALUES (?, ?, ?, ?, ?)",
                (client_id, name, json.dumps(redirects), method, token_digest(secret) if secret else None),
            )
        response = {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "client_name": name,
            "redirect_uris": redirects,
            "token_endpoint_auth_method": method,
            "grant_types": GRANT_TYPES,
            "response_types": ["code"],
        }
        if secret:
            response.update(client_secret=secret, client_secret_expires_at=0)
        return JSONResponse(response, status_code=201, headers=SENSITIVE_HEADERS)

    async def authorize(self, request):
        params = request.query_params
        if len(params) != len(params.multi_items()):
            return oauth_error("invalid_request", "Repeated OAuth parameter.")
        client_id = params.get("client_id", "")
        redirect = params.get("redirect_uri")
        with self.connect() as db:
            client = db.execute("SELECT * FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
        if client is None or redirect not in json.loads(client["redirect_uris"]):
            return oauth_error("invalid_request", "Unregistered client or redirect URI.")

        state = params.get("state")
        issuer = self.base_url(request.scope)
        if params.get("response_type") != "code":
            return self.redirect(redirect, state, issuer, error="unsupported_response_type")
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
            return self.redirect(
                redirect, state, issuer, error="invalid_request", error_description="S256 PKCE is required."
            )
        if set(params.get("scope", SCOPE).split()) != {SCOPE}:
            return self.redirect(redirect, state, issuer, error="invalid_scope")
        resource = issuer + "/mcp"
        if params.get("resource", resource) != resource:
            return self.redirect(redirect, state, issuer, error="invalid_target")

        browser = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
        now = time.monotonic()
        while self.pending and (len(self.pending) >= 2048 or next(iter(self.pending.values())).expires <= now):
            self.pending.popitem(last=False)
        csrf = secrets.token_urlsafe(32)
        flow = Authorization(client_id, client["client_name"], redirect, challenge, resource, state, browser,
                             now + CODE_TTL)
        self.pending[csrf] = flow
        response = self.form(flow, csrf)
        response.set_cookie(
            CSRF_COOKIE, browser, max_age=CODE_TTL, secure=resource.startswith("https://"),
            httponly=True, samesite="lax", path="/oauth/authorize",
        )
        return response

    async def approve(self, request):
        try:
            data = await read_parameters(request)
        except ValueError:
            return oauth_error("invalid_request", "Expected a URL-encoded authorization form.")
        csrf = data.get("csrf_token", "")
        flow = self.pending.get(csrf)
        if (
            flow is None or flow.expires <= time.monotonic()
            or not secrets.compare_digest(
                token_digest(request.cookies.get(CSRF_COOKIE, "")), token_digest(flow.browser)
            )
            or flow.client_id != data.get("client_id", flow.client_id)
            or flow.redirect_uri != data.get("redirect_uri", flow.redirect_uri)
            or flow.resource != self.base_url(request.scope) + "/mcp"
        ):
            return oauth_error("invalid_request", "Expired or invalid authorization form.")
        token_label = self.registry.verify(data.get("api_token", ""))
        if token_label is None:
            return self.form(flow, csrf, invalid_token=True)
        self.pending.pop(csrf)
        code = secrets.token_urlsafe(32)
        now = time.time()
        with self.connect() as db:
            db.execute("DELETE FROM oauth_codes WHERE expires <= ?", (now,))
            db.execute(
                "INSERT INTO oauth_codes VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_digest(code), flow.client_id, flow.redirect_uri, flow.challenge, token_label,
                 flow.resource, now + CODE_TTL),
            )
        return self.redirect(flow.redirect_uri, flow.state, flow.resource.removesuffix("/mcp"), code=code)

    @staticmethod
    def redirect(uri, state, issuer, **params):
        target = urlsplit(uri)
        if state is not None:
            params["state"] = state
        params["iss"] = issuer
        query = "&".join(part for part in (target.query, urlencode(params)) if part)
        return RedirectResponse(urlunsplit(target._replace(query=query)), status_code=302, headers=SENSITIVE_HEADERS)

    @staticmethod
    def form(flow, csrf, *, invalid_token=False):
        error = '<p role="alert">Invalid API token. Неверный API-токен.</p>' if invalid_token else ""
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Connect Everything Search</title>
<style>
body {{ font: 1rem/1.55 system-ui, sans-serif; padding: 1.25rem; }}
main {{ max-width: 30rem; margin: clamp(1rem, 9vh, 5rem) auto; }}
h1, p {{ overflow-wrap: anywhere; }}
form {{ display: grid; gap: .65rem; }}
input, button {{ font: inherit; padding: .65rem; }}
[role="alert"] {{ font-weight: bold; }}
</style>
<main>
<h1>Connect Everything Search</h1>
<p>Allow <strong>{escape(flow.client_name)}</strong> to use your read-only research tools.</p>
<p lang="ru">Разрешите приложению доступ к поиску и исследованиям.</p>
<p>Redirect / Переход: <code>{escape(flow.redirect_uri)}</code></p>
{error}
<form method="post" action="/oauth/authorize">
<input type="hidden" name="csrf_token" value="{escape(csrf)}">
<input type="hidden" name="client_id" value="{escape(flow.client_id)}">
<input type="hidden" name="redirect_uri" value="{escape(flow.redirect_uri)}">
<label for="api_token">API token / API-токен</label>
<input id="api_token" name="api_token" type="password" autocomplete="off" autofocus required>
<button type="submit">Authorize / Разрешить</button>
</form>
</main>
</html>""",
            status_code=400 if invalid_token else 200,
            headers={
                **SENSITIVE_HEADERS,
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            },
        )

    async def token(self, request):
        try:
            data = await read_parameters(request, allow_json=True)
        except ValueError:
            return oauth_error("invalid_request", "Expected OAuth token parameters.")
        if not all(isinstance(value, str) for value in data.values()):
            return oauth_error("invalid_request", "OAuth token parameters must be strings.")
        grant = data.get("grant_type", "")
        if grant not in GRANT_TYPES:
            return oauth_error("unsupported_grant_type")

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            client = db.execute(
                "SELECT * FROM oauth_clients WHERE client_id=?", (data.get("client_id", ""),)
            ).fetchone()
            if client is None:
                return oauth_error("invalid_client")
            supplied_secret = data.get("client_secret", "")
            if client["auth_method"] == "client_secret_post" and not secrets.compare_digest(
                token_digest(supplied_secret), client["secret_hash"]
            ):
                return oauth_error("invalid_client")
            if client["auth_method"] == "none" and supplied_secret:
                return oauth_error("invalid_client")

            if grant == "authorization_code":
                row = self.consume_code(db, data, client["client_id"], now)
            else:
                row = self.consume_refresh(db, data, client["client_id"], now)
            if row is None:
                return oauth_error("invalid_grant")

            access = secrets.token_urlsafe(32)
            refresh = secrets.token_urlsafe(32)
            db.execute("DELETE FROM oauth_tokens WHERE refresh_expires <= ?", (now,))
            db.execute(
                "INSERT INTO oauth_tokens VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_digest(access), token_digest(refresh), client["client_id"], row["token_label"],
                 row["resource"], now + ACCESS_TTL, now + REFRESH_TTL),
            )
        return JSONResponse({
            "access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
            "refresh_token": refresh, "scope": SCOPE,
        }, headers=SENSITIVE_HEADERS)

    @staticmethod
    def consume_code(db, data, client_id, now):
        code_hash = token_digest(data.get("code", ""))
        row = db.execute(
            "SELECT * FROM oauth_codes WHERE code_hash=? AND expires>? AND client_id=? AND redirect_uri=?",
            (code_hash, now, client_id, data.get("redirect_uri")),
        ).fetchone()
        verifier = data.get("code_verifier", "")
        if row is None or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
            return None
        challenge = base64.urlsafe_b64encode(token_digest(verifier)).rstrip(b"=").decode("ascii")
        if not secrets.compare_digest(challenge, row["code_challenge"]):
            return None
        if data.get("resource", row["resource"]) != row["resource"]:
            return None
        db.execute("DELETE FROM oauth_codes WHERE code_hash=?", (code_hash,))
        return row

    @staticmethod
    def consume_refresh(db, data, client_id, now):
        refresh_hash = token_digest(data.get("refresh_token", ""))
        row = db.execute(
            "SELECT * FROM oauth_tokens WHERE refresh_hash=? AND refresh_expires>? AND client_id=?",
            (refresh_hash, now, client_id),
        ).fetchone()
        if row is None or data.get("resource", row["resource"]) != row["resource"]:
            return None
        if data.get("scope", SCOPE) != SCOPE:
            return None
        db.execute("DELETE FROM oauth_tokens WHERE refresh_hash=?", (refresh_hash,))
        return row

    def verify_access_token(self, token: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT token_label FROM oauth_tokens WHERE access_hash=? AND access_expires>?",
                (token_digest(token), time.time()),
            ).fetchone()
        return f"oauth:{row['token_label']}" if row else None
