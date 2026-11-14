import asyncio

import httpx

from conftest import fetcher_for, product_page
from research_v2.engine import Engine
from research_v2.shopping import adapter_for
from test_engine import make_engine


def test_capacity_variants_are_explicit_and_distinct():
    first = product_page(name="Raspberry Pi 5 - 8 GB RAM")
    second = product_page(name="Raspberry Pi 5 - 16GB RAM")
    _, a = adapter_for(first.url).parse(first, "Raspberry Pi 5")
    _, b = adapter_for(second.url).parse(second, "Raspberry Pi 5")
    assert a[0].product_id == b[0].product_id
    assert a[0].variants["capacity_labels"] == "8 gb"
    assert b[0].variants["capacity_labels"] == "16 gb"


def test_unmapped_store_variants_are_kept_distinct():
    page = product_page(
        host="example.com", extra_offer={"url": "https://example.com/product/123456?variant=100"}
    )
    _, offers = adapter_for(page.url).parse(page)
    assert offers[0].variants["source_variant"] == "example.com:100"


def test_store_variant_option_supplies_explicit_capacity():
    page = product_page(
        host="example.com", extra_offer={"url": "https://example.com/product/123456?variant=100"}
    )
    page.variant_labels = {"100": "8GB"}
    _, offers = adapter_for(page.url).parse(page)
    assert offers[0].variants["capacity_labels"] == "8 gb"


async def test_conflicting_specifications_are_reported(settings, store):
    first, second = product_page(), product_page(host="avito.ru")
    second.structured_data[0]["additionalProperty"][0]["value"] = "450"
    result = await make_engine(settings, store, [first, second]).search_products(
        "Wanbo Cube 2 Pro", model="Wanbo Cube 2 Pro"
    )
    product = result["products"][0]
    assert "ANSI" not in product["specifications"]
    assert product["specification_conflicts"]["ANSI"] == ["500", "450"]


async def test_unrelated_index_results_are_not_fetched(settings, store):
    unrelated = product_page(name="Psychologist service", host="avito.ru")
    unrelated.title = "Psychologist service"
    unrelated.url = "https://avito.ru/omsk/uslugi/psychologist_12345678"
    result = await make_engine(settings, store, [unrelated]).search_products(
        "Wanbo Cube 2 Pro", model="Wanbo Cube 2 Pro"
    )
    assert not result["offers"] and not result["candidates"]


async def test_redirect_limit_is_bounded(settings, store):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return (
            httpx.Response(404)
            if request.url.path == "/robots.txt"
            else httpx.Response(302, headers={"location": "/loop"})
        )

    page = await fetcher_for(settings, store, handler).read("https://example.com/loop")
    assert page.error == "redirect_limit"
    assert count == settings.max_redirects + 2


async def test_finished_pages_survive_research_timeout(settings, store, monkeypatch):
    from test_engine import FakeDiscovery

    pages = [product_page(), product_page(host="avito.ru")]

    class SlowFetcher:
        async def read(self, url, *args, **kwargs):
            if "avito.ru" in url:
                await asyncio.sleep(1)
            return pages[0]

    engine = Engine(settings, store, SlowFetcher(), FakeDiscovery(pages))
    original_timeout = asyncio.timeout
    monkeypatch.setattr("research_v2.engine.asyncio.timeout", lambda duration: original_timeout(0.03))
    result = await engine.research("Wanbo", mode="quick")
    assert result["timed_out"]
    assert result["results"]["web"]["pages_read"] == 1
    assert result["errors"][0]["error"] == "time_budget_exceeded"


def test_malformed_schema_type_is_ignored():
    page = product_page()
    page.structured_data[0]["@type"] = None
    assert adapter_for(page.url).parse(page) == ([], [])


def test_private_offer_link_is_rejected():
    page = product_page(host="example.com", extra_offer={"url": "http://127.0.0.1/secret"})
    products, offers = adapter_for(page.url).parse(page)
    assert products and not offers
