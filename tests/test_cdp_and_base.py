import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import public_resolver
from research_v2 import camoufox_capture, worker
from research_v2.browser_capture import capture_with_camoufox
from research_v2.config import Settings
from research_v2.connectors.base import (
    ConnectorCardResult,
    ConnectorSearchResult,
    ConnectorTransport,
    MarketplaceConnector,
    SourceOutcome,
    infer_condition,
    make_product_and_offer,
    parse_price_rub,
)
from research_v2.network import AccessError
from test_ozon_capture import Process, enable


SEARCH = "https://www.ozon.ru/search/?text=wanbo"
API_URL = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2"
JSON_PATTERN = "/api/entrypoint-api.bx/"


@pytest.mark.parametrize(
    ("flag", "concurrency", "enabled", "maximum"),
    [("1", "100", True, 6), ("true", "3", True, 3), ("yes", "2", True, 2),
     ("0", "0", False, 1), ("false", "4", False, 4), ("no", "5", False, 5)],
)
def test_settings_cdp_env(monkeypatch, flag, concurrency, enabled, maximum):
    monkeypatch.setenv("RESEARCH_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setenv("RESEARCH_NATIVE_CONNECTORS", flag)
    monkeypatch.setenv("RESEARCH_CDP_MAX_CONCURRENT", concurrency)
    settings = Settings.load()
    assert settings.cdp_url == "http://127.0.0.1:9222"
    assert settings.native_connectors is enabled and settings.cdp_max_concurrent == maximum


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:9222", "http://192.168.0.1:9222", "http://example.org:9222",
    "http://user:password@127.0.0.1:9222", "http://127.0.0.1:9222?url=x", "http://127.0.0.1:99999",
])
def test_settings_cdp_rejects_unsafe_endpoints(url):
    with pytest.raises(ValueError):
        Settings(cdp_url=url, allow_internal_hosts=True)


def test_settings_cdp_allows_loopback_and_opt_in_internal_host():
    assert Settings(cdp_url="http://[::1]:9222").cdp_url
    assert Settings(cdp_url="http://localhost:9222").cdp_url
    assert Settings(cdp_url="http://browser-cdp:9222", allow_internal_hosts=True).cdp_url
    with pytest.raises(ValueError):
        Settings(cdp_url="http://browser-cdp:9222")


@pytest.fixture
async def client(settings):
    settings.worker_token = "worker-token"
    settings.cdp_per_host_interval = 0
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=worker.create_app(settings)),
        base_url="http://worker.invalid",
        headers={"Authorization": "Bearer worker-token"},
    ) as client:
        yield client


async def test_cdp_health_unconfigured_and_authenticated(client):
    assert (await client.get("/cdp-health")).json() == {"reachable": False, "reason": "cdp_not_configured"}
    assert (await client.get("/cdp-health", headers={"Authorization": "Bearer wrong"})).status_code == 401


@pytest.mark.parametrize("reachable", [True, False])
async def test_cdp_health_endpoint(client, settings, monkeypatch, reachable):
    settings.cdp_url = "http://127.0.0.1:9222/"
    async_client = httpx.AsyncClient

    def handler(request):
        assert str(request.url) == "http://127.0.0.1:9222/json/version"
        if not reachable:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"Browser": "Chrome/131.0"})

    def internal_client(**kwargs):
        assert kwargs == {"timeout": 3, "trust_env": False}
        return async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(worker.httpx, "AsyncClient", internal_client)
    result = (await client.get("/cdp-health")).json()
    assert result == (
        {"reachable": True, "browser": "Chrome/131.0", "cdp_url": settings.cdp_url}
        if reachable else {"reachable": False, "reason": "cdp_unreachable"}
    )


@pytest.mark.parametrize("url", ["http://127.0.0.1:80", "file:///etc/passwd"])
async def test_cdp_rejects_unsafe_initial_url(client, url):
    response = await client.post("/cdp-fetch", json={"url": url})
    assert response.status_code == 422
    assert response.json() == {"error": "ssrf_blocked", "http_status": None}


async def test_cdp_payload_is_bounded(client):
    response = await client.post("/cdp-fetch", json={"url": SEARCH, "extra": "x" * 17000})
    assert response.status_code == 422 and response.json()["error"] == "request_too_large"
    response = await client.post("/cdp-fetch", json={"url": SEARCH, "wait_ms": True})
    assert response.status_code == 422 and response.json()["error"] == "invalid_wait_ms"


class FakeBrowserError(Exception):
    pass


class FakeBrowserTimeout(FakeBrowserError):
    pass


def response_for(page, url, status=200, *, navigation=False, body=b'{"tiles":[1]}'):
    return SimpleNamespace(
        url=url, status=status, frame=page.main_frame,
        request=SimpleNamespace(is_navigation_request=lambda: navigation),
        headers={"content-type": "application/json", "content-length": str(len(body))},
        body=AsyncMock(return_value=body),
    )


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.main_frame = object()
        self.listeners = {}
        self.closed = False
        self.challenge = False
        self.challenge_clears = True
        self.waits = []
        self.navigation_url = SEARCH
        self.html = "<title>Search</title><p>Products</p>"
        self.fail_content = False
        self.route_web_socket = AsyncMock()

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    async def emit(self, response):
        for listener in self.listeners.get("response", []):
            result = listener(response)
            if asyncio.iscoroutine(result):
                await result

    async def goto(self, url, **kwargs):
        self.url = self.navigation_url
        await self.emit(response_for(self, url, 439 if self.challenge else 200, navigation=True))
        await self.emit(response_for(self, API_URL))
        await self.emit(response_for(self, API_URL + "?too-large", body=b" " * (512 * 1024 + 1)))
        await self.emit(response_for(self, API_URL + "?not-json", body=b"not JSON"))

    async def evaluate(self, script):
        if "document.title" in script:
            return ["Проверка браузера", "Qrator"] if self.challenge else ["Search", "Products"]

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        if self.challenge and self.challenge_clears:
            self.challenge = False
            await self.emit(response_for(self, self.url, navigation=True))

    async def title(self):
        return "Search"

    async def content(self):
        if self.fail_content:
            raise FakeBrowserError("browser disconnected")
        return self.html

    async def close(self):
        self.closed = True


def install_fake_playwright(monkeypatch):
    playwright = ModuleType("playwright")
    async_api = ModuleType("playwright.async_api")
    async_api.Error = FakeBrowserError
    async_api.TimeoutError = FakeBrowserTimeout
    playwright.async_api = async_api
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    return async_api


@pytest.fixture
def cdp_browser(settings, monkeypatch):
    settings.cdp_url = "http://127.0.0.1:9222"
    monkeypatch.setattr(worker, "resolve_public", public_resolver)
    page = FakePage()
    listeners = {}

    async def send(command, params=None):
        if command == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "main"}}}

    session = SimpleNamespace(send=AsyncMock(side_effect=send), on=listeners.__setitem__)
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), new_cdp_session=AsyncMock(return_value=session))
    browser = SimpleNamespace(
        contexts=[context], close=AsyncMock(), new_context=AsyncMock(), version="131.0.0.0",
    )
    connect = AsyncMock(return_value=browser)

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))

    install_fake_playwright(monkeypatch).async_playwright = playwright
    return SimpleNamespace(page=page, browser=browser, connect=connect, session=session, listeners=listeners)


async def test_cdp_fetch_captures_json_and_waits_for_challenge(client, settings, cdp_browser):
    cdp_browser.page.challenge = True
    response = await client.post("/cdp-fetch", json={
        "url": SEARCH, "capture_json_patterns": [JSON_PATTERN], "wait_ms": 0, "scroll_steps": 0,
        "user_agent_compat": "windows",
    })
    assert response.status_code == 200
    assert response.json() == {
        "html": cdp_browser.page.html, "final_url": SEARCH, "http_status": 200, "title": "Search",
        "captured_json": [{"url": API_URL, "status": 200, "data": {"tiles": [1]}}], "extractor": "cdp-chromium",
    }
    assert cdp_browser.page.waits == [500]
    cdp_browser.connect.assert_awaited_once_with(settings.cdp_url, timeout=5000)
    cdp_browser.browser.new_context.assert_not_awaited()
    cdp_browser.browser.close.assert_awaited_once()
    assert cdp_browser.page.closed


async def test_cdp_fetch_challenge_timeout_keeps_status_and_cleans_up(client, settings, cdp_browser):
    cdp_browser.page.challenge = True
    cdp_browser.page.challenge_clears = False
    settings.challenge_wait_s = 0
    response = await client.post("/cdp-fetch", json={"url": SEARCH})
    assert response.status_code == 422
    assert response.json() == {"error": "challenge_unresolved", "http_status": 439}
    assert cdp_browser.page.closed
    cdp_browser.browser.close.assert_awaited_once()


async def test_cdp_can_return_challenge_without_waiting(client, cdp_browser):
    cdp_browser.page.challenge = True
    response = await client.post("/cdp-fetch", json={
        "url": SEARCH, "wait_for_challenge": False, "wait_ms": 0, "scroll_steps": 0,
    })
    assert response.status_code == 200 and response.json()["http_status"] == 439
    assert cdp_browser.page.waits == [] and cdp_browser.page.closed


async def test_cdp_uses_separate_pool_and_serializes_each_host(client, settings, monkeypatch):
    worker_instance = worker.Worker(settings)
    settings.cdp_url = "http://127.0.0.1:9222"
    tasks = []
    avito_started, ozon_started, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    started = []

    async def page(options, host, navigation):
        started.append(host)
        if host == "www.ozon.ru":
            ozon_started.set()
        else:
            avito_started.set()
        await finish.wait()
        return {"html": "<p>Results</p>"}

    class Request:
        def __init__(self, url):
            self.url = url

        async def stream(self):
            yield json.dumps({"url": self.url}).encode()

    monkeypatch.setattr(worker_instance, "cdp_page", page)
    async with worker_instance.semaphore:
        try:
            tasks.append(asyncio.create_task(worker_instance.cdp_fetch(Request(SEARCH))))
            await asyncio.wait_for(ozon_started.wait(), 1)
            tasks.append(asyncio.create_task(worker_instance.cdp_fetch(Request(SEARCH + "2"))))
            tasks.append(asyncio.create_task(worker_instance.cdp_fetch(Request("https://www.avito.ru/all?q=x"))))
            await asyncio.wait_for(avito_started.wait(), 1)
            assert started == ["www.ozon.ru", "www.avito.ru"]
        finally:
            finish.set()
            results = await asyncio.gather(*tasks)
    assert all(result.status_code == 200 for result in results)
    assert started == ["www.ozon.ru", "www.avito.ru", "www.ozon.ru"]


@pytest.mark.parametrize("final_url,reason", [
    ("http://127.0.0.1/", "ssrf_blocked"), ("https://example.org/", "source_policy_denied"),
])
async def test_cdp_checks_final_url_and_cleans_up(client, cdp_browser, final_url, reason):
    cdp_browser.page.navigation_url = final_url
    response = await client.post("/cdp-fetch", json={"url": SEARCH, "wait_ms": 0, "scroll_steps": 0})
    assert response.status_code == 422 and response.json() == {"error": reason, "http_status": 200}
    assert cdp_browser.page.closed
    cdp_browser.browser.close.assert_awaited_once()


async def test_cdp_intercepts_redirect_before_navigation_and_leaves_script_requests(client, cdp_browser):
    async def goto(url, **kwargs):
        callback = cdp_browser.listeners["Fetch.requestPaused"]
        for request_id, resource_type, target in [
            ("script", "Script", "https://www.ozon.ru/static/app.js"),
            ("image", "Image", "https://www.ozon.ru/image.png"),
            ("redirect", "Document", "http://127.0.0.1/admin"),
        ]:
            await callback({
                "requestId": request_id, "resourceType": resource_type, "frameId": "main",
                "request": {"url": target},
            })
        raise FakeBrowserError("blocked navigation")

    cdp_browser.page.goto = goto
    response = await client.post("/cdp-fetch", json={"url": SEARCH})
    assert response.status_code == 422 and response.json()["error"] == "ssrf_blocked"
    calls = [
        (call.args[0], call.args[1]["requestId"])
        for call in cdp_browser.session.send.await_args_list if call.args[0].startswith("Fetch.")
        and call.args[0] != "Fetch.enable"
    ]
    assert calls == [
        ("Fetch.continueRequest", "script"), ("Fetch.failRequest", "image"), ("Fetch.failRequest", "redirect"),
    ]


async def test_capture_endpoint_forwards_options(client, settings, monkeypatch):
    enable(settings)
    capture = AsyncMock(return_value={"html": "search"})
    monkeypatch.setattr("research_v2.browser_capture.capture_with_camoufox", capture)
    response = await client.post("/capture", json={
        "url": SEARCH, "region": "RU", "allow_search": True, "capture_json_patterns": [JSON_PATTERN],
    })
    assert response.status_code == 200
    capture.assert_awaited_once_with(
        settings, SEARCH, "RU", allow_search=True, capture_json_patterns=[JSON_PATTERN],
    )


async def test_camoufox_forwards_search_and_patterns_to_subprocess(settings, monkeypatch):
    enable(settings)
    settings.camoufox_python = sys.executable
    process = Process(b'{"captured_json":[]}')
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert await capture_with_camoufox(
        settings, SEARCH, "RU", allow_search=True, capture_json_patterns=[JSON_PATTERN],
    ) == {"captured_json": []}
    assert json.loads(process.stdin.data) == {
        "url": SEARCH, "region": "RU", "allow_search": True, "capture_json_patterns": [JSON_PATTERN],
    }


@pytest.mark.parametrize("patterns", [None, [JSON_PATTERN]])
async def test_camoufox_explicit_search_skips_product_requirement(settings, monkeypatch, tmp_path, patterns):
    enable(settings)
    settings.database = str(tmp_path / "test.sqlite3")
    install_fake_playwright(monkeypatch)
    page = FakePage()
    if patterns is None:
        page.html = "<title>Search</title><p>" + ("Search results " * 50) + "</p>"
    context = SimpleNamespace(
        pages=[], new_page=AsyncMock(return_value=page), route=AsyncMock(), route_web_socket=AsyncMock(),
    )

    @asynccontextmanager
    async def browser(**kwargs):
        assert kwargs["proxy"] == {"server": "http://127.0.0.1:1"}
        yield context

    @asynccontextmanager
    async def network(settings, selected, domains):
        assert selected == "RU" and domains == ["ozon.ru", "ozone.ru"]
        yield SimpleNamespace(url="http://127.0.0.1:1")

    firefox = ModuleType("camoufox")
    firefox.DefaultAddons = SimpleNamespace(UBO="ubo")
    firefox.async_api = ModuleType("camoufox.async_api")
    firefox.async_api.AsyncCamoufox = browser
    monkeypatch.setitem(sys.modules, "camoufox", firefox)
    monkeypatch.setitem(sys.modules, "camoufox.async_api", firefox.async_api)
    monkeypatch.setattr(camoufox_capture, "BrowserNetwork", network)
    allowed = AsyncMock(side_effect=AccessError("robots_denied"))
    monkeypatch.setattr(camoufox_capture.Fetcher, "allowed", allowed)
    with pytest.raises(AccessError, match="robots_denied"):
        await camoufox_capture.capture(settings, SEARCH, "RU")
    with pytest.raises(AccessError, match="robots_denied"):
        await camoufox_capture.capture(settings, "https://ozon.ru/account/", "RU", allow_search=True)
    result = await camoufox_capture.capture(
        settings, SEARCH, "RU", allow_search=True, capture_json_patterns=patterns,
    )
    assert result["html"] == page.html and result["http_status"] == 200
    assert len(result["captured_json"]) == bool(patterns)
    allowed.assert_any_await(SEARCH, "RU")


@pytest.mark.parametrize(("value", "expected"), [
    ("19 608 ₽", "19608"), ("19\u2009608\u00a0₽", "19608"), (123, "123"), (2.5, "2.5"),
    ("10,25 руб.", "10.25"), (0, None), (-1, None), ("1e12", None), ("NaN", None),
    ("Infinity", None), (True, None), ("12 USD", None),
])
def test_parse_price_rub(value, expected):
    assert parse_price_rub(value) == (Decimal(expected) if expected is not None else None)


@pytest.mark.parametrize(("title", "explicit", "default", "expected"), [
    ("Wanbo уцененный", None, "new", "refurbished"),
    ("Wanbo б/у", None, "new", "used"),
    ("Wanbo б/у", "NewCondition", "unknown", "new"),
    ("Wanbo", None, "unknown", "unknown"),
    ("Wanbo", "Восстановленный", "new", "refurbished"),
])
def test_infer_condition(title, explicit, default, expected):
    assert infer_condition(title, explicit, default) == expected


def test_normalize_product_and_offer():
    product, offer = make_product_and_offer(
        "ozon", SEARCH, "Wanbo Cube 2 Pro синий", "19 608 ₽",
        model="Wanbo Cube 2 Pro", brand="Wanbo", conditional_price=18500, original_price=22000,
        rating=4.8, reviews_count=12, image_url="https://cdn.ozone.ru/item.jpg", sku=12345, seller="Shop",
        specifications={"ANSI": "500"}, variants={"plug": "EU"},
    )
    assert offer.product_id == product.id and product.sources == [offer.evidence]
    assert product.name == "Wanbo Cube 2 Pro" and product.specifications == {"ANSI": "500"}
    assert offer.evidence.source == "native:ozon" and offer.evidence.region == "RU"
    assert offer.variants == {"color": "синий", "plug": "EU"} and offer.sku == "12345"
    assert (offer.price, offer.conditional_price, offer.original_price) == (19608, 18500, 22000)
    with pytest.raises(AccessError, match="ssrf_blocked"):
        make_product_and_offer("ozon", "http://127.0.0.1/product", "Wanbo", 12000)


async def test_connector_worker_transport(settings):
    settings.browser_url = "http://127.0.0.1:8770/"
    settings.worker_token = "worker-token"
    requests = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer worker-token"
        requests.append((request.url.path, request.method, json.loads(request.content or b"null")))
        if request.url.path == "/cdp-health":
            return httpx.Response(200, json={"reachable": True})
        if request.url.path == "/capture":
            return httpx.Response(422, json={"error": "robots_denied"})
        return httpx.Response(200, json={"html": "search"})

    transport = ConnectorTransport(settings, transport=httpx.MockTransport(handler))
    assert await transport.cdp_health() == {"reachable": True}
    assert await transport.cdp_fetch(SEARCH, capture_json_patterns=[JSON_PATTERN]) == {"html": "search"}
    assert await transport.camoufox_fetch(
        SEARCH, allow_search=True, capture_json_patterns=[JSON_PATTERN],
    ) == {"error": "robots_denied"}
    assert requests[0] == ("/cdp-health", "GET", None)
    assert requests[1][0:2] == ("/cdp-fetch", "POST")
    assert requests[1][2]["capture_json_patterns"] == [JSON_PATTERN]
    assert requests[2] == ("/capture", "POST", {
        "url": SEARCH, "region": "RU", "allow_search": True, "capture_json_patterns": [JSON_PATTERN],
    })


async def test_connector_http_get_pins_address_and_keeps_marketplace_headers(settings):
    def handler(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "www.ozon.ru"
        assert request.extensions["sni_hostname"] == "www.ozon.ru"
        assert request.headers["accept-language"] == "ru-RU,ru;q=0.9"
        assert "Chrome/" in request.headers["user-agent"]
        assert request.url.params == httpx.QueryParams({"text": "wanbo", "page": "2"})
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/"})

    transport = ConnectorTransport(settings, resolver=public_resolver, transport=httpx.MockTransport(handler))
    response = await transport.http_get(SEARCH, params={"page": "2"}, headers={"Host": "host.invalid"})
    assert response.status_code == 302 and response.url.host == "www.ozon.ru"
    with pytest.raises(AccessError, match="ssrf_blocked"):
        await transport.http_get("http://127.0.0.1/")


async def test_connector_filters_offers_and_exposes_outcome(settings):
    class Connector(MarketplaceConnector):
        name, domain = "ozon", "ozon.ru"

        async def search(self, query, **kwargs):
            return ConnectorSearchResult()

        async def get_card(self, url_or_sku, **kwargs):
            return ConnectorCardResult()

    connector = Connector(ConnectorTransport(settings))
    assert connector.matches_url(SEARCH) and not connector.matches_url("https://ozon.ru.example.org/")
    rows = [
        make_product_and_offer("ozon", f"https://www.ozon.ru/{path}", title, price, availability=stock)
        for path, title, price, stock in [
            ("1", "Чехол Wanbo Cube 2 Pro", 100, "in_stock"),
            ("2", "Wanbo Cube 2 Pro", 12000, "in_stock"),
            ("2#duplicate", "Wanbo Cube 2 Pro", 12000, "in_stock"),
            ("3", "Wanbo Cube 2 Pro", 12000, "out_of_stock"),
            ("4", "Wanbo Cube 2 Pro", None, "in_stock"),
            ("5", "Wanbo Cube 2 Pro Max", 19000, "in_stock"),
        ]
    ]
    products, offers = connector._filter_and_dedupe(
        [row[0] for row in rows], [row[1] for row in rows], model="Wanbo Cube 2 Pro", limit=5,
    )
    assert [offer.url for offer in offers] == ["https://www.ozon.ru/2"]
    assert [product.id for product in products] == [offers[0].product_id]
    assert await connector.get_reviews("Wanbo Cube 2 Pro") == ([], [])
    assert SourceOutcome("ozon", "ok", offers_returned=len(offers)).as_dict()["offers_returned"] == 1
