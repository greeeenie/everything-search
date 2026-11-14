from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import product_page
from research_v2.auth import BearerGate
from research_v2.discovery import Discovery, canonical_url
from research_v2.engine import Engine
from research_v2.models import SearchHit
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route


class FakeDiscovery:
    def __init__(self, pages):
        self.pages = pages

    async def search(self, *args, **kwargs):
        return [
            SearchHit(
                url=p.url,
                title=p.title,
                source="test_index",
                status="indexed_only",
                region="RU",
                search_excerpt="Index price 1 RUB must never become an offer",
            )
            for p in self.pages
        ], []


class FakeFetcher:
    def __init__(self, pages):
        self.pages = {p.url: p for p in pages}

    async def read(self, url, *args, **kwargs):
        return self.pages[url]


def make_engine(settings, store, pages):
    return Engine(settings, store, FakeFetcher(pages), FakeDiscovery(pages))


async def test_cross_market_model_group_and_distinct_conditions(settings, store):
    pages = [
        product_page(),
        product_page(host="avito.ru", condition="UsedCondition", price="9000"),
        product_page(host="market.yandex.ru", currency="USD", price="150"),
    ]
    result = await make_engine(settings, store, pages).compare_prices("Wanbo Cube 2 Pro", city="Омск")
    assert len(result["products"]) == 1 and len(result["offers"]) == 3
    assert len(result["comparisons"]) == 3
    assert all(group["best_offer"] is None for group in result["comparisons"])


async def test_confirmed_city_delivery_ranks_total_price(settings, store):
    shipping = {
        "shippingDetails": {
            "shippingDestination": {"addressLocality": "Омск"},
            "shippingRate": {"value": "300", "currency": "RUB"},
        }
    }
    pages = [
        product_page(price="12000", extra_offer=shipping),
        product_page(host="avito.ru", price="11000", extra_offer=shipping),
    ]
    result = await make_engine(settings, store, pages).compare_prices("Wanbo Cube 2 Pro", city="Омск")
    best = result["comparisons"][0]["best_offer"]
    assert best["total_price"] == "11300" and best["marketplace"] == "avito"


async def test_city_mismatch_never_confirms_shipping(settings, store):
    shipping = {
        "shippingDetails": {
            "shippingDestination": {"addressLocality": "Москва"},
            "shippingRate": {"value": "0", "currency": "RUB"},
        }
    }
    result = await make_engine(settings, store, [product_page(extra_offer=shipping)]).compare_prices(
        "Wanbo Cube 2 Pro", city="Омск"
    )
    assert result["comparisons"][0]["best_offer"] is None


async def test_conditional_price_cannot_win(settings, store):
    shipping = {
        "shippingDetails": {
            "shippingDestination": {"addressLocality": "Омск"},
            "shippingRate": {"value": "0", "currency": "RUB"},
        }
    }
    page = product_page(extra_text="Цена по карте Ozon", extra_offer=shipping)
    result = await make_engine(settings, store, [page]).compare_prices("Wanbo Cube 2 Pro", city="Омск")
    assert result["comparisons"][0]["best_offer"] is None


async def test_unavailable_page_retains_indexed_candidate(settings, store):
    page = product_page()
    page.status, page.error = "unavailable", "http_403"
    result = await make_engine(settings, store, [page]).search_products(
        "Wanbo Cube 2 Pro", model="Wanbo Cube 2 Pro"
    )
    assert result["offers"] == [] and result["products"] == []
    assert result["candidates"][0]["status"] == "indexed_only"
    assert result["candidates"][0]["fetch_error"] == "http_403"


def test_url_deduplication_keeps_seller_identity():
    assert (
        canonical_url("https://ozon.ru/product/123?utm_source=x&seller=1#top")
        == "https://ozon.ru/product/123?seller=1"
    )
    assert canonical_url("https://ozon.ru/product/123?seller=2") != canonical_url(
        "https://ozon.ru/product/123?seller=1"
    )


def test_history_14_day_retention_and_clear(store):
    old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    store.record({"url": "https://example.com", "source": "test", "checked_at": old, "status": "verified"})
    assert store.db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    store.put("test", {"value": 1}, 30)
    store.clear()
    assert store.get("test") is None


async def test_http_bearer_gate():
    async def endpoint(request):
        return JSONResponse({"ok": True})

    app = BearerGate(Starlette(routes=[Route("/mcp", endpoint)]), "test-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/mcp")).status_code == 401
        assert (await client.get("/mcp", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await client.get("/mcp", headers={"Authorization": "Bearer test-token"})).status_code == 200


async def test_mcp_inventory_and_old_research_shape(monkeypatch):
    import server

    names = {tool.name for tool in await server.mcp.list_tools()}
    assert names == {
        "research",
        "search_web",
        "search_hacker_news",
        "search_github_issues",
        "search_products",
        "compare_prices",
        "get_product_details",
        "get_product_reviews",
        "read_page",
        "diagnose_egress",
    }
    assert all(tool.outputSchema is not None for tool in await server.mcp.list_tools())

    async def search_index(*args, **kwargs):
        return []

    monkeypatch.setattr(server, "search_index", search_index)
    result = await server.research("Steam Deck")
    assert {"query", "provider", "scope", "threads_read", "comments_seen", "threads"} <= result.keys()
    assert result["status"] == "indexed_only"


async def test_search_domain_filter_enforced(settings, store, monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args, **kwargs):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": "https://ozon.ru/product/123456", "title": "Good"},
                        {"url": "https://ozon.ru.evil.com/product/123456", "title": "Bad"},
                    ]
                },
            )

    monkeypatch.setattr("research_v2.discovery.httpx.AsyncClient", lambda **kwargs: Client())
    hits, errors = await Discovery(settings, store).search("test", ["ozon.ru"])
    assert len(hits) == 1 and hits[0].status == "indexed_only" and not errors


async def test_invalid_price_range(settings, store):
    with pytest.raises(ValueError):
        await make_engine(settings, store, []).search_products("test", min_price=100, max_price=10)


async def test_explicit_currency_filters_without_price_range(settings, store):
    pages = [product_page(), product_page(host="avito.ru", currency="USD", price="100")]
    result = await make_engine(settings, store, pages).search_products("Wanbo Cube 2 Pro", currency="USD")
    assert len(result["offers"]) == 1 and result["offers"][0]["currency"] == "USD"
