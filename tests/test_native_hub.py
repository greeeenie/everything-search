import asyncio
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from conftest import product_page
from research_v2.config import Settings
from research_v2.connectors import ConnectorCardResult, ConnectorSearchResult, NativeMarketplaceHub, SourceOutcome
from research_v2.connectors import registry
from research_v2.connectors.base import make_product_and_offer
from research_v2.engine import Engine
from research_v2.models import Page
from test_engine import FakeDiscovery, FakeFetcher


MODEL = "Wanbo Cube 2 Pro"
URLS = {
    "wildberries": "https://www.wildberries.ru/catalog/123/detail.aspx",
    "ozon": "https://www.ozon.ru/product/wanbo-cube-2-pro-123456/",
    "yandex_market": "https://market.yandex.ru/product--wanbo/123456",
    "avito": "https://www.avito.ru/omsk/elektronika/wanbo_12345678",
    "aliexpress": "https://aliexpress.ru/item/12345.html",
    "dns": "https://www.dns-shop.ru/product/abc/wanbo/",
    "citilink": "https://www.citilink.ru/product/wanbo-123/",
    "megamarket": "https://megamarket.ru/catalog/details/wanbo-123/",
    "lamoda": "https://www.lamoda.ru/p/mp002xw12345/",
    "detsky_mir": "https://www.detmir.ru/product/index/id/123/",
}


def listing(source, price=None, *, title=MODEL, model=MODEL, url=None, **kwargs):
    return make_product_and_offer(source, url or URLS[source], title, price, model=model, **kwargs)


def search_result(source, *listings, status="ok", error=None, fallback=False):
    return ConnectorSearchResult(
        [product for product, _ in listings],
        [offer for _, offer in listings],
        SourceOutcome(source, status, error_code=error, fallback_used=fallback).as_dict(),
    )


@pytest.fixture
def hub(settings, store):
    transport = Mock(cdp_health=AsyncMock(return_value={"reachable": True, "browser": "Chromium"}))
    return NativeMarketplaceHub(settings, store, transport=transport)


def test_registry_routes_all_ten_sources(hub):
    assert hub.known_marketplaces == set(URLS)
    for source, url in URLS.items():
        assert hub.connector_for_url(url) is hub.connectors[source]
        assert hub.connectors[source].transport is hub.transport
    assert hub.connector_for_url("https://ozon.ru.example.com/product/123") is None
    assert hub.connector_for_url("https://example.com@ozon.ru/product/123") is None
    assert {name for name, connector in hub.connectors.items() if connector.supports_reviews} == {
        "wildberries", "ozon", "yandex_market",
    }
    empty = NativeMarketplaceHub(hub.settings, hub.store, connectors={})
    assert empty.known_marketplaces == set()


def test_engine_enables_native_connectors_for_configured_runtime(settings, store, monkeypatch):
    monkeypatch.delenv("RESEARCH_CONFIG", raising=False)
    monkeypatch.delenv("RESEARCH_NATIVE_CONNECTORS", raising=False)
    assert Settings.load().native_connectors is True
    assert Engine(settings, store).marketplace_hub is None
    configured = replace(settings, native_connectors=True)
    assert isinstance(Engine(configured, store).marketplace_hub, NativeMarketplaceHub)
    assert Engine(configured, store, FakeFetcher([]), FakeDiscovery([])).marketplace_hub is None


async def test_compare_concurrency_exact_model_merge_dedup_and_store_cache(hub, monkeypatch):
    sources = ["ozon", "wildberries"]
    both_started = asyncio.Event()
    started = set()

    async def search(source):
        started.add(source)
        if started == set(sources):
            both_started.set()
        await both_started.wait()
        good = listing(source, 10000, specifications={"ANSI": "500" if source == "ozon" else "450"})
        duplicate = (good[0], good[1].model_copy(update={"url": URLS[source] + "#duplicate"}))
        wrong = listing(source, 100, title="Wanbo Cube 2 Pro Max", model=None, url=URLS[source] + "?adjacent=1")
        return search_result(source, good, duplicate, wrong)

    for source in sources:
        async def run(*args, name=source, **kwargs):
            return await search(name)

        monkeypatch.setattr(hub.connectors[source], "search", AsyncMock(side_effect=run))

    products, offers, outcomes, errors = await asyncio.wait_for(
        hub.compare(MODEL, sources, model=MODEL, limit=5, city="Omsk"), timeout=2,
    )
    assert len(products) == 1 and len(products[0].sources) == 2
    assert set(products[0].specification_conflicts["ANSI"]) == {"450", "500"}
    assert [(offer.marketplace, offer.price) for offer in offers] == [("ozon", 10000), ("wildberries", 10000)]
    assert [(row["source"], row["offers_returned"]) for row in outcomes] == [("ozon", 1), ("wildberries", 1)]
    assert errors == []

    other_hub = NativeMarketplaceHub(hub.settings, hub.store, connectors=hub.connectors)
    assert await other_hub.compare(MODEL, sources[::-1], model=MODEL, limit=5, city="Omsk") == (
        products, offers, outcomes, errors,
    )
    for options in ({"city": "Moscow"}, {"limit": 6}, {"model": None}, {"query": "projector"}):
        await other_hub.compare(marketplaces=sources, **({"query": MODEL, "model": MODEL, "limit": 5,
                                                         "city": "Omsk"} | options))
    for source in sources:
        connector = hub.connectors[source]
        assert connector.search.await_count == 5
        assert connector.search.await_args_list[0].kwargs == {"model": MODEL, "limit": 5, "city": "Omsk"}
    hub.store.db.execute("UPDATE cache SET expires = 0")
    await hub.compare(MODEL, sources, model=MODEL, limit=5, city="Omsk")
    assert hub.connectors["ozon"].search.await_count == 6


async def test_open_circuit_cooldown_and_success_reset(hub, monkeypatch):
    time = 1000.0
    monkeypatch.setattr(registry, "monotonic", lambda: time)
    failed = search_result("ozon", status="blocked", error="challenge_required")
    success = search_result("ozon", listing("ozon", 10000))
    search = AsyncMock(side_effect=[failed, failed, failed, success, failed, failed, success])
    monkeypatch.setattr(hub.connectors["ozon"], "search", search)

    for _ in range(3):
        assert (await hub.compare(MODEL, ["ozon"]))[2][0]["error_code"] == "challenge_required"
    assert (await hub.health())["breakers"] == {"ozon": 90}
    time += 89
    _, _, outcomes, errors = await hub.compare(MODEL, ["ozon"])
    assert outcomes[0]["status"] == "blocked" and outcomes[0]["error_code"] == "circuit_open"
    assert errors == [{"source": "ozon", "error": "circuit_open"}]
    assert search.await_count == 3
    time += 1
    assert (await hub.compare(MODEL, ["ozon"]))[2][0]["status"] == "ok"
    assert (await hub.health())["breakers"] == {}
    for query in ("one", "two", "three"):
        await hub.compare(query, ["ozon"])
    assert search.await_count == 7
    assert (await hub.health())["breakers"] == {}


async def test_search_timeout_and_exception_preserve_other_sources(hub, monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(registry, "SEARCH_TIMEOUT", 0.01)
    monkeypatch.setattr(hub.connectors["avito"], "search", slow)
    monkeypatch.setattr(hub.connectors["ozon"], "search", AsyncMock(side_effect=ValueError("internal input")))
    monkeypatch.setattr(hub.connectors["dns"], "search", AsyncMock(
        return_value=search_result("dns", listing("dns", 9000)),
    ))
    _, offers, outcomes, errors = await hub.compare(MODEL, ["avito", "ozon", "dns", "unknown"])
    assert [offer.marketplace for offer in offers] == ["dns"]
    assert [(row["source"], row["status"]) for row in outcomes] == [
        ("avito", "timeout"), ("dns", "ok"), ("ozon", "error"), ("unknown", "skipped"),
    ]
    assert {row["error"] for row in errors} == {"connector_timeout", "connector_error", "unknown_marketplace"}


async def test_partial_cache_does_not_prolong_open_circuit(hub, monkeypatch):
    time = 1000.0
    monkeypatch.setattr(registry, "monotonic", lambda: time)
    failed = search_result("ozon", status="error", error="worker_unavailable")
    search = AsyncMock(side_effect=[failed, failed, failed, search_result("ozon", listing("ozon", 12000))])
    monkeypatch.setattr(hub.connectors["ozon"], "search", search)
    monkeypatch.setattr(hub.connectors["dns"], "search", AsyncMock(
        return_value=search_result("dns", listing("dns", 9000)),
    ))
    markets = ["dns", "ozon"]
    await hub.compare(MODEL, markets)
    await hub.compare("query two", ["ozon"])
    await hub.compare("query three", ["ozon"])
    _, offers, outcomes, _ = await hub.compare(MODEL, markets)
    assert len(offers) == 1 and outcomes[1]["error_code"] == "circuit_open"
    assert search.await_count == 3

    time += 90
    _, offers, outcomes, _ = await hub.compare(MODEL, markets)
    assert len(offers) == 2 and outcomes[1]["status"] == "ok"
    assert search.await_count == 4


@pytest.mark.parametrize("missing_price", [True, False])
async def test_engine_search_and_comparison_preserve_conditional_ranking(hub, monkeypatch, missing_price):
    product, wallet = listing("wildberries", 7500, conditional_price=7500, price_conditions=["wallet"])
    if missing_price:
        wallet.price = None
        wallet.currency = None
        wallet.price_scope = "conditional"
    public = listing("ozon", 12000)
    used = listing("avito", 8000, condition="used")
    public[1].delivery = {
        "shippingDestination": {"addressLocality": "Omsk"},
        "shippingRate": {"currency": "RUB", "value": 400},
    }
    for source, row in zip(("wildberries", "ozon", "avito"), ((product, wallet), public, used)):
        monkeypatch.setattr(hub.connectors[source], "search", AsyncMock(return_value=search_result(source, row)))
    discovery = Mock(search=AsyncMock())
    engine = Engine(hub.settings, hub.store, FakeFetcher([]), discovery, hub)
    args = {"marketplaces": ["wb", "ozon.ru", "avito"], "city": "Omsk", "max_results": 12}

    search = await engine.search_products(MODEL, model=MODEL, **args)
    wallet_result = next(offer for offer in search["offers"] if offer["marketplace"] == "wildberries")
    assert (wallet_result["price"], wallet_result["currency"], wallet_result["price_scope"]) == (
        "7500", "RUB", "conditional",
    )
    assert len(search["products"][0]["sources"]) == 3
    comparison = await engine.compare_prices(MODEL, **args)
    recommendations = comparison["recommendations"]
    assert recommendations["best_new_public"]["marketplace"] == "ozon"
    assert recommendations["best_new_conditional"]["effective_price"] == "7500"
    assert recommendations["best_used"]["marketplace"] == "avito"
    assert recommendations["cheapest_overall_public"]["marketplace"] == "avito"
    assert [offer["marketplace"] for offer in recommendations["ranked_top_5"]] == ["avito", "ozon"]
    new = next(group for group in comparison["comparisons"] if group["condition"] == "new")
    assert new["best_offer"]["total_price"] == "12400"
    assert new["best_price_offer"]["marketplace"] == "ozon"
    assert new["best_conditional_offer"]["marketplace"] == "wildberries"
    assert {row["source"] for row in comparison["source_outcomes"]} == {"wildberries", "ozon", "avito"}
    discovery.search.assert_not_awaited()


async def test_engine_falls_back_to_discovery_for_custom_and_untried_sources(hub, monkeypatch):
    for source, used in (("ozon", True), ("avito", False)):
        monkeypatch.setattr(hub.connectors[source], "search", AsyncMock(return_value=search_result(
            source, status="blocked", error="challenge_required", fallback=used,
        )))
    avito = product_page(host="avito.ru")
    custom = product_page(host="shop.example.com")
    discovery = Mock(search=AsyncMock(side_effect=FakeDiscovery([avito, custom]).search))
    fetcher = Mock(read=AsyncMock(side_effect=FakeFetcher([avito, custom]).read))
    engine = Engine(hub.settings, hub.store, fetcher, discovery, hub)

    result = await engine.search_products(MODEL, model=MODEL, marketplaces=["ozon", "avito", "shop.example.com"])

    assert discovery.search.await_args.args[1] == ["avito.ru", "shop.example.com"]
    assert [(row["source"], row["fallback_used"]) for row in result["source_outcomes"]] == [
        ("avito", "research_v2"), ("ozon", True),
    ]
    assert {call.args[0] for call in fetcher.read.await_args_list} == {avito.url, custom.url}
    assert fetcher.read.await_args_list[0].kwargs["dynamic"] is True


async def test_details_prefers_native_card_caches_reviews_and_falls_back(hub, monkeypatch):
    product, wallet = listing("wildberries", conditional_price=6000)
    wallet.price = None
    reviews = [{"url": wallet.url, "text": "Native card review"}]
    native_card = AsyncMock(return_value=ConnectorCardResult([product], [wallet], reviews))
    monkeypatch.setattr(hub.connectors["wildberries"], "get_card", native_card)
    page = product_page()
    monkeypatch.setattr(hub.connectors["ozon"], "get_card", AsyncMock(
        return_value=ConnectorCardResult(error="card_unavailable"),
    ))
    fetcher = Mock(read=AsyncMock(return_value=page))
    engine = Engine(hub.settings, hub.store, fetcher, FakeDiscovery([]), hub)

    details = await engine.get_product_details(wallet.url, model=MODEL)
    assert details["page"]["source"] == "native:wildberries"
    assert details["reviews"] == reviews
    assert details["offers"][0]["price"] == "6000"
    fetcher.read.assert_not_awaited()
    native_card.assert_awaited_once_with(wallet.url, model=MODEL)
    _, offers, cached_reviews, error = await hub.get_card(wallet.url + "?utm_source=test", model=MODEL)
    assert offers[0].price == Decimal("6000") and cached_reviews == reviews and error is None
    assert native_card.await_count == 1
    assert hub.store.db.execute("SELECT COUNT(*) FROM evidence").fetchone() == (2,)

    fallback = await engine.get_product_details(page.url, model=MODEL, region="RU")
    assert fallback["offers"][0]["price"] == "12990"
    assert fallback["marketplace_hub_error"] == "card_unavailable"
    fetcher.read.assert_awaited_once_with(page.url, "RU", dynamic=True)


async def test_reviews_merge_three_marketplaces_with_web_and_dedupe(hub, monkeypatch):
    forum = Page(url="https://forum.ixbt.com/topic", title=MODEL, source="public_page",
                 status="verified", region="RU", paragraphs=[MODEL + " has been reliable for six months of use."])
    seen = set()
    ready = asyncio.Event()

    async def rendezvous(source):
        seen.add(source)
        if len(seen) == 4:
            ready.set()
        await ready.wait()

    async def discover(*args, **kwargs):
        await rendezvous("forum")
        return await FakeDiscovery([forum]).search(*args, **kwargs)

    for source in ("wildberries", "ozon", "yandex_market"):
        async def reviews(*args, name=source, **kwargs):
            await rendezvous(name)
            row = {"marketplace": name, "url": URLS[name], "text": name + " review"}
            return [row, row.copy()], []

        monkeypatch.setattr(hub.connectors[source], "get_reviews", AsyncMock(side_effect=reviews))
    unsupported = AsyncMock()
    monkeypatch.setattr(hub.connectors["avito"], "get_reviews", unsupported)
    engine = Engine(hub.settings, hub.store, FakeFetcher([forum]), Mock(search=discover), hub)
    result = await asyncio.wait_for(engine.get_product_reviews(MODEL), timeout=2)

    assert len(result["reviews"]) == 4
    assert {review["url"] for review in result["reviews"]} == {
        URLS["wildberries"], URLS["ozon"], URLS["yandex_market"], forum.url,
    }
    assert result["sources"][0]["fetch_status"] == "verified" and result["errors"] == []
    unsupported.assert_not_awaited()


async def test_review_timeout_keeps_other_reviews_and_source_errors(hub, monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(registry, "REVIEW_TIMEOUT", 0.01)
    monkeypatch.setattr(hub.connectors["wildberries"], "get_reviews", slow)
    ozon_review = {"url": URLS["ozon"], "text": "Native Ozon review"}
    monkeypatch.setattr(hub.connectors["ozon"], "get_reviews", AsyncMock(return_value=([ozon_review], [])))
    market_error = {"source": "yandex_market", "error": "matching_listing_unavailable"}
    monkeypatch.setattr(hub.connectors["yandex_market"], "get_reviews", AsyncMock(return_value=([], [market_error])))

    reviews, errors = await hub.get_reviews(MODEL, limit=1)
    assert reviews == [ozon_review]
    assert errors == [{"source": "wildberries", "stage": "reviews", "error": "connector_timeout"}, market_error]


async def test_diagnose_egress_includes_native_cdp_health(hub):
    diagnose = AsyncMock(side_effect=[{"region": "RU", "ip": "192.0.2.1"}, {"region": "EU", "ip": "192.0.2.2"}])
    engine = Engine(hub.settings, hub.store, Mock(http=Mock(diagnose=diagnose)), FakeDiscovery([]), hub)
    result = await engine.diagnose_egress()
    assert result["distinct_ips"]
    assert result["marketplace_hub"] == {
        "status": "ok", "engine": "native", "connectors": sorted(URLS),
        "cdp": {"reachable": True, "browser": "Chromium"}, "breakers": {},
    }
    hub.transport.cdp_health.return_value = {"reachable": False}
    assert (await hub.health())["status"] == "degraded"


async def test_quick_research_has_45_second_budget_and_core_sources(hub, monkeypatch):
    settings = replace(hub.settings, enabled_marketplaces=list(URLS))
    engine = Engine(settings, hub.store, FakeFetcher([]), FakeDiscovery([]), hub)
    compare = AsyncMock(return_value={})
    monkeypatch.setattr(engine, "compare_prices", compare)

    result = await engine.research(MODEL, model=MODEL)

    assert result["budget_seconds"] == 45
    assert compare.await_args.kwargs["marketplaces"] == ["yandex_market", "wildberries", "aliexpress", "avito", "ozon"]
