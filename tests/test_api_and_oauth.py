import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from research_v2 import api, auth
from research_v2.auth import BearerGate, TokenRegistry, gateway_app
from research_v2.config import Settings
import server

BASE = "https://search.example"
REDIRECT = "https://client.example/callback?source=research"
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


async def echo(request):
    return JSONResponse({"ok": True})


def test_registry_uses_constant_time_comparison_for_all_configured_tokens(monkeypatch):
    compare_digest = auth.secrets.compare_digest
    comparisons = []

    def spy(left, right):
        comparisons.append((left, right))
        return compare_digest(left, right)

    monkeypatch.setattr(auth.secrets, "compare_digest", spy)
    registry = TokenRegistry("single:raw", tokens="user:tok1,friend:tok2,unlabeled",
                             extra_verifier=lambda value: "oauth:user" if value == "oauth" else None)
    for token, label in [
        ("single:raw", "default"), ("tok1", "user"), ("tok2", "friend"), ("unlabeled", "default"),
        ("wrong", None), ("токен", None), ("oauth", "oauth:user"),
    ]:
        comparisons.clear()
        assert registry.verify(token) == label
        assert len(comparisons) == 4
        assert all(len(left) == len(right) == 32 for left, right in comparisons)


async def test_bearer_gate_static_keys_challenge_and_public_paths():
    endpoint = Starlette(routes=[Route(path, echo) for path in ("/mcp", "/health", "/", "/oauth/info")])
    metadata = BASE + METADATA_PATH
    app = BearerGate(endpoint, {"user:tok1", "friend:tok2"}, public_paths={"/"},
                     public_prefixes=("/oauth/",), oauth_metadata_url=metadata)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        for path in ("/health", "/", "/oauth/info"):
            assert (await client.get(path)).status_code == 200
        for headers in ({"Authorization": "Bearer wrong"}, {"X-API-Key": "wrong"}, {}):
            response = await client.get("/mcp", headers=headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == f'Bearer resource_metadata="{metadata}"'
        assert (await client.get("/mcp", headers={"Authorization": "bearer tok1"})).status_code == 200
        assert (await client.get("/mcp", headers={"X-API-Key": "tok2"})).status_code == 200


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_MCP_TOKEN", "legacy")
    monkeypatch.setenv("RESEARCH_API_TOKENS", "friend:tok2")
    return Settings(
        database=str(tmp_path / "research.sqlite3"), public_base_url=BASE + "/",
        marketplaces={"ozon": "ozon.ru", "avito": "avito.ru"}, enabled_marketplaces=["ozon"],
        api_tokens=["user:tok1"],
    )


def client_for(app, base=BASE):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base)


@pytest.mark.parametrize("method", ["none", "client_secret_post"])
async def test_oauth_authorization_code_and_refresh_flow(settings, method):
    app = gateway_app(server.mcp, settings)
    async with client_for(app) as client:
        challenge_response = await client.post("/mcp")
        assert challenge_response.status_code == 401
        assert challenge_response.headers["WWW-Authenticate"] == f'Bearer resource_metadata="{BASE}{METADATA_PATH}"'
        for path in ("/.well-known/oauth-protected-resource", METADATA_PATH):
            resource = await client.get(path)
            assert resource.json() == {
                "resource": BASE + "/mcp", "authorization_servers": [BASE],
                "bearer_methods_supported": ["header"], "scopes_supported": ["research:read"],
            }
        metadata = (await client.get("/.well-known/oauth-authorization-server")).json()
        assert metadata["issuer"] == BASE
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        assert metadata["registration_endpoint"] == BASE + "/oauth/register"

        for bad_uri in ["http://insecure.example/callback", "https://example.com/callback#",
                        "http://localhost@insecure.example/callback"]:
            invalid = await client.post("/oauth/register", json={"redirect_uris": [bad_uri]})
            assert invalid.status_code == 400
            assert invalid.json()["error"] == "invalid_redirect_uri"

        registration = await client.post("/oauth/register", json={
            "redirect_uris": [REDIRECT, "http://localhost:5000/mcp"],
            "client_name": "<MCP Client>", "token_endpoint_auth_method": method,
        })
        assert registration.status_code == 201
        registered = registration.json()
        assert registered["token_endpoint_auth_method"] == method
        client_id = registered["client_id"]
        client_secret = registered.get("client_secret")
        assert bool(client_secret) == (method == "client_secret_post")

        verifier = "a" * 64
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        authorize_params = {
            "client_id": client_id, "redirect_uri": REDIRECT, "resource": BASE + "/mcp",
            "response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256",
            "state": "client state&test",
        }
        invalid_redirect = await client.get(
            "/oauth/authorize", params={**authorize_params, "redirect_uri": REDIRECT + "suffix"}
        )
        assert invalid_redirect.status_code == 400
        page = await client.get("/oauth/authorize", params=authorize_params)
        assert page.status_code == 200
        assert "&lt;MCP Client&gt;" in page.text and "<MCP Client>" not in page.text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        form = {"csrf_token": csrf, "client_id": client_id, "redirect_uri": REDIRECT}
        async with client_for(app) as another_browser:
            missing_cookie = await another_browser.post("/oauth/authorize", data={**form, "api_token": "tok1"})
            assert missing_cookie.status_code == 400
        invalid = await client.post("/oauth/authorize", data={**form, "api_token": "wrong"})
        assert invalid.status_code == 400
        assert "Invalid API token" in invalid.text
        approval = await client.post("/oauth/authorize", data={**form, "api_token": "tok1"})
        assert approval.status_code == 302
        callback = parse_qs(urlsplit(approval.headers["location"]).query)
        assert callback["source"] == ["research"]
        assert callback["state"] == ["client state&test"]
        assert callback["iss"] == [BASE]
        assert (await client.post("/oauth/authorize", data={**form, "api_token": "tok1"})).status_code == 400

        code = callback["code"][0]
        exchange = {
            "grant_type": "authorization_code", "client_id": client_id, "redirect_uri": REDIRECT,
            "code": code, "code_verifier": verifier, "resource": BASE + "/mcp",
        }
        if client_secret:
            wrong_secret = await client.post("/oauth/token", data={**exchange, "client_secret": "wrong"})
            assert wrong_secret.json()["error"] == "invalid_client"
            exchange["client_secret"] = client_secret
        wrong_pkce = await client.post("/oauth/token", data={**exchange, "code_verifier": "b" * 64})
        assert wrong_pkce.status_code == 400
        assert wrong_pkce.json()["error"] == "invalid_grant"
        wrong_resource = await client.post("/oauth/token", data={**exchange, "resource": BASE + "/elsewhere"})
        assert wrong_resource.status_code == 400
        issued = await client.post("/oauth/token", data=exchange)
        assert issued.status_code == 200
        assert issued.headers["cache-control"] == "no-store"
        first = issued.json()
        assert first["scope"] == "research:read"
        assert first["expires_in"] == 30 * 24 * 3600
        assert (await client.post("/oauth/token", data=exchange)).status_code == 400

        def bearer(token):
            return {"Authorization": f"Bearer {token}"}

        assert (await client.get("/api/v1/status?quick=1", headers=bearer(first["access_token"]))).status_code == 200
        # A restarted gateway reads the existing credentials from SQLite.
        async with client_for(gateway_app(server.mcp, settings)) as restarted:
            response = await restarted.get("/api/v1/status?quick=1", headers=bearer(first["access_token"]))
            assert response.status_code == 200

        refresh = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": first["refresh_token"]}
        if client_secret:
            refresh["client_secret"] = client_secret
        results = await asyncio.gather(*(client.post("/oauth/token", json=refresh) for _ in range(2)))
        assert sorted(result.status_code for result in results) == [200, 400]
        second = next(result.json() for result in results if result.status_code == 200)
        assert second["access_token"] != first["access_token"]
        assert second["refresh_token"] != first["refresh_token"]
        assert (await client.get("/api/v1/status?quick=1", headers=bearer(first["access_token"]))).status_code == 401
        assert (await client.get("/api/v1/status?quick=1", headers=bearer(second["access_token"]))).status_code == 200

        with sqlite3.connect(settings.database) as db:
            assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            assert db.execute("SELECT count(*) FROM oauth_codes").fetchone() == (0,)
            stored = db.execute("SELECT access_hash,refresh_hash,token_label FROM oauth_tokens").fetchone()
            assert stored == (
                hashlib.sha256(second["access_token"].encode()).digest(),
                hashlib.sha256(second["refresh_token"].encode()).digest(), "user",
            )
            if client_secret:
                assert db.execute("SELECT secret_hash FROM oauth_clients").fetchone() == (
                    hashlib.sha256(client_secret.encode()).digest(),
                )


async def test_rest_operations_dispatch_to_engine_and_community_tools(settings, monkeypatch):
    engine = AsyncMock()
    engine.diagnose_egress.return_value = {"egress": [{"region": "RU"}]}
    monkeypatch.setattr(api, "get_engine", lambda: engine)
    monkeypatch.setattr("research_v2.engine.get_engine", lambda: engine)
    legacy = AsyncMock(return_value={"legacy": True})
    monkeypatch.setattr(server, "legacy_research", legacy)
    hn = AsyncMock()
    github = AsyncMock()
    monkeypatch.setattr(api.community_sources, "search_hacker_news", hn)
    monkeypatch.setattr(api.community_sources, "search_github_issues", github)
    operations = {
        "/products/search": ({"query": "headphones", "specifications": {"color": "black"}}, engine.search_products),
        "/products/compare": ({"model": "XM5", "city": "Omsk"}, engine.compare_prices),
        "/products/details": ({"url": "https://example.com/item"}, engine.get_product_details),
        "/products/reviews": ({"model": "XM5"}, engine.get_product_reviews),
        "/web/search": ({"query": "headphones", "site": "Example.COM"}, engine.search_web),
        "/web/read": ({"url": "https://example.com/item", "dynamic": True}, engine.read_page),
        "/community/hn": ({"query": "headphones"}, hn),
        "/community/github": ({"query": "headphones"}, github),
    }
    async with client_for(gateway_app(server.mcp, settings)) as client:
        assert (await client.get("/api/v1/status?quick=1")).status_code == 401
        assert (await client.post("/api/v1/products/compare", json={"model": "XM5"})).status_code == 401
        headers = {"Authorization": "Bearer legacy"}
        for path, (payload, target) in operations.items():
            target.return_value = {"evidence": [path]}
            response = await client.post("/api/v1" + path, json=payload, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json() == target.return_value
            target.assert_awaited_once()
            for key, value in payload.items():
                assert target.await_args.kwargs[key] == (value.lower() if key == "site" else value)
        for mode in ("quick", "deep"):
            engine.research.return_value = {"mode": mode}
            response = await client.post("/api/v1/research", json={"query": "topic", "mode": mode}, headers=headers)
            assert response.status_code == 200
            assert response.json() == {"mode": mode}
            engine.research.assert_awaited_with("topic", mode, None, None, None)
        response = await client.post("/api/v1/research", json={"query": "topic"}, headers=headers)
        assert response.json() == {"legacy": True}
        legacy.assert_awaited_once_with("topic", 4)

        for path, payload in (
            ("/products/search", {}), ("/products/search", {"query": "       "}),
            ("/products/search", {"query": "x", "min_price": 3, "max_price": 1}),
            ("/products/reviews", {"model": "XM5", "max_results": 30}),
            ("/products/details", {}), ("/web/read", {"url": "file:///etc/hosts"}),
            ("/research", {"query": "topic", "mode": "unknown"}),
        ):
            invalid = await client.post("/api/v1" + path, json=payload, headers=headers)
            assert invalid.status_code == 422, invalid.text
            assert invalid.json()["detail"]
        invalid = await client.post("/api/v1/web/search", content="{oops", headers=headers)
        assert invalid.status_code == 422

        quick = await client.get("/api/v1/status?quick=1", headers={"X-API-Key": "tok2"})
        assert quick.status_code == 200
        assert quick.json()["marketplaces"] == ["ozon"]
        assert quick.json()["version"]
        engine.diagnose_egress.assert_not_awaited()
        status = await client.get("/api/v1/status", headers=headers)
        assert status.json()["egress"] == engine.diagnose_egress.return_value
        engine.diagnose_egress.assert_awaited_once()


async def test_discovery_routes_use_configured_or_forwarded_origin(settings):
    settings.public_base_url = ""
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "search.example"}
    async with client_for(gateway_app(server.mcp, settings), "http://backend:8765") as client:
        spec = (await client.get("/openapi.json", headers=headers)).json()
        assert spec["openapi"] == "3.1.0"
        assert spec["servers"] == [{"url": BASE}]
        assert spec["components"]["securitySchemes"]["BearerAuth"] == {"type": "http", "scheme": "bearer"}
        assert spec["security"] == [{"BearerAuth": []}]
        assert len(spec["paths"]) == 10
        assert "model" in spec["components"]["schemas"]["ProductCompare"]["required"]
        operation = spec["paths"]["/api/v1/products/compare"]["post"]
        assert operation["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ProductCompare"
        }
        skill = await client.get("/skill.md", headers=headers)
        assert skill.status_code == 200
        assert skill.headers["content-type"] == "text/markdown; charset=utf-8"
        assert skill.text.startswith("---\nname: everything-search")
        assert BASE + "/api/v1/products/compare" in skill.text
        assert "{{BASE_URL}}" not in skill.text
        llms = await client.get("/llms.txt", headers=headers)
        assert llms.status_code == 200
        assert BASE + "/mcp" in llms.text and BASE + "/api/v1/research" in llms.text
        landing = await client.get("/")
        assert landing.status_code == 200
        assert "<title>Everything Search</title>" in landing.text
        landing_json = await client.get("/", headers={**headers, "Accept": "application/json"})
        assert BASE + "/mcp" in landing_json.json()["endpoints"]
        assert (await client.get("/health")).json() == {"status": "ok", "service": "research-v2"}
        protected = await client.get("/mcp", headers=headers)
        assert protected.headers["WWW-Authenticate"] == f'Bearer resource_metadata="{BASE}{METADATA_PATH}"'
        assert (await client.get("/openapi.json")).json()["servers"] == [{"url": "http://backend:8765"}]


@asynccontextmanager
async def lifespan(app):
    incoming = asyncio.Queue()
    outgoing = asyncio.Queue()
    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}, "state": {}}
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    try:
        await incoming.put({"type": "lifespan.startup"})
        message = await asyncio.wait_for(outgoing.get(), 3)
        assert message["type"] == "lifespan.startup.complete", message
        yield
    finally:
        await incoming.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(task, 3)


async def test_mcp_lifespan_with_public_hostname_and_cors(settings):
    app = gateway_app(server.mcp, settings)
    origin = "https://agent.example"
    async with lifespan(app), client_for(app) as client:
        for path, method in (("/mcp", "POST"), ("/api/v1/products/search", "POST"),
                             (METADATA_PATH, "GET"), ("/oauth/token", "POST"), ("/openapi.json", "GET")):
            preflight = await client.options(path, headers={
                "Origin": origin, "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "authorization, x-api-key, mcp-session-id, mcp-protocol-version",
            })
            assert preflight.status_code == 200
            assert preflight.headers["Access-Control-Allow-Origin"] == "*"

        headers = {**MCP_HEADERS, "Authorization": "Bearer tok1", "Origin": origin}
        result = await client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}},
        })
        assert result.status_code == 200, result.text
        assert "Mcp-Session-Id" in result.headers["Access-Control-Expose-Headers"]
        protocol_version = result.json()["result"]["protocolVersion"]

        tools = await client.post("/mcp", headers={**headers, "MCP-Protocol-Version": protocol_version},
                                  json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert tools.status_code == 200, tools.text
        assert len(tools.json()["result"]["tools"]) == 10


@pytest.mark.parametrize("no_auth", [True, False], ids=["flag", "environment"])
async def test_local_gateway_without_tokens(settings, monkeypatch, no_auth):
    for name in ("RESEARCH_MCP_TOKEN", "RESEARCH_API_TOKENS", "RESEARCH_TRUSTED_TUNNEL", "RESEARCH_NO_AUTH"):
        monkeypatch.delenv(name, raising=False)
    if not no_auth:
        monkeypatch.setenv("RESEARCH_NO_AUTH", "1")
    settings.api_tokens = []
    settings.public_base_url = "http://127.0.0.1:8765"

    app = gateway_app(server.mcp, settings, no_auth=no_auth)
    async with lifespan(app), client_for(app, settings.public_base_url) as client:
        status = await client.get("/api/v1/status?quick=1")
        assert status.status_code == 200
        assert status.json()["auth"] == "none"
        assert (await client.get("/", headers={"Accept": "application/json"})).json()["auth"] == "none"
        assert (await client.get("/openapi.json")).json()["security"] == []
        for path in ("/health", "/skill.md", "/llms.txt"):
            assert (await client.get(path)).status_code == 200
        assert (await client.get(METADATA_PATH)).status_code == 404
        assert (await client.post("/oauth/register")).status_code == 404

        result = await client.post("/mcp", headers=MCP_HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}},
        })
        assert result.status_code == 200, result.text
        assert result.json()["result"]["serverInfo"]["name"] == "Research"


async def test_local_opt_in_preserves_token_enforcement(settings):
    async with client_for(gateway_app(server.mcp, settings, no_auth=True)) as client:
        assert (await client.get("/api/v1/status?quick=1")).status_code == 401
        response = await client.get("/api/v1/status?quick=1", headers={"Authorization": "Bearer tok1"})
        assert response.status_code == 200
        assert response.json()["auth"] == "token"
        assert (await client.get(METADATA_PATH)).status_code == 200


async def test_disabled_oauth_and_trusted_tunnel(settings, monkeypatch):
    settings.oauth_enabled = False
    async with client_for(gateway_app(server.mcp, settings)) as client:
        response = await client.get("/mcp")
        assert response.headers["www-authenticate"] == "Bearer"
        missing = await client.get(METADATA_PATH, headers={"X-API-Key": "legacy"})
        assert missing.status_code == 404
    monkeypatch.delenv("RESEARCH_MCP_TOKEN")
    monkeypatch.delenv("RESEARCH_API_TOKENS")
    monkeypatch.delenv("RESEARCH_TRUSTED_TUNNEL", raising=False)
    monkeypatch.delenv("RESEARCH_NO_AUTH", raising=False)
    settings.api_tokens = []
    with pytest.raises(RuntimeError, match="RESEARCH_MCP_TOKEN"):
        gateway_app(server.mcp, settings)
    monkeypatch.setenv("RESEARCH_TRUSTED_TUNNEL", "1")
    async with client_for(gateway_app(server.mcp, settings)) as client:
        assert (await client.get("/api/v1/status?quick=1")).status_code == 200
        assert (await client.get(METADATA_PATH)).status_code == 404


def test_serve_http_bind_address_precedence(settings, monkeypatch):
    run = []
    monkeypatch.setattr(auth.uvicorn, "run", lambda app, **params: run.append(params))
    monkeypatch.setenv("RESEARCH_HTTP_HOST", "127.0.0.2")
    settings.http_host = "127.0.0.3"
    auth.serve_http(server.mcp, 9876, host="127.0.0.4", settings=settings)
    auth.serve_http(server.mcp, 9876, settings=settings)
    settings.http_host = ""
    auth.serve_http(server.mcp, 9876, settings=settings)
    monkeypatch.delenv("RESEARCH_HTTP_HOST")
    auth.serve_http(server.mcp, 9876, settings=settings)
    assert [params["host"] for params in run] == ["127.0.0.4", "127.0.0.3", "127.0.0.2", "127.0.0.1"]
    assert all(params["port"] == 9876 for params in run)
