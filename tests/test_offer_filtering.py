import copy

import pytest

from conftest import product_page
from research_v2.engine import Engine
from research_v2.models import Page
from test_engine import FakeDiscovery, FakeFetcher, make_engine


@pytest.mark.parametrize("entrypoint", ["search_products", "compare_prices", "get_product_details", "search_web"])
@pytest.mark.parametrize(
    "changes",
    [
        {"availability": "OutOfStock"},
        {"availability": "PreOrder"},
        {"availability": ""},
        {"price": None},
        {"price": "0"},
        {"price": "NaN"},
        {"currency": ""},
    ],
)
async def test_all_offer_entrypoints_exclude_unavailable_or_unpriced(settings, store, entrypoint, changes):
    page = product_page(**changes)
    engine = make_engine(settings, store, [page])
    arguments = {
        "search_products": {"query": "Wanbo Cube 2 Pro", "model": "Wanbo Cube 2 Pro"},
        "compare_prices": {"model": "Wanbo Cube 2 Pro"},
        "get_product_details": {"url": page.url},
        "search_web": {"query": "Wanbo Cube 2 Pro", "site": "ozon.ru"},
    }
    result = await getattr(engine, entrypoint)(**arguments[entrypoint])
    if entrypoint == "search_web":
        assert result["results"] == []
        assert result["excluded_product_pages"] == 1 and result["pages_read"] == 1
    else:
        assert result["offers"] == []
    if entrypoint in {"search_products", "compare_prices"}:
        assert result["products"] == []


async def test_filter_applies_to_each_seller_offer(settings, store):
    page = product_page()
    available = page.structured_data[0]["offers"]
    unavailable = copy.deepcopy(available)
    unavailable.update(availability="https://schema.org/OutOfStock", price="1", seller={"name": "Shop B"})
    page.structured_data[0]["offers"] = [available, unavailable]
    result = await make_engine(settings, store, [page]).compare_prices("Wanbo Cube 2 Pro")
    assert len(result["offers"]) == len(result["products"]) == 1
    assert result["offers"][0]["seller"] == "Shop A"
    assert result["comparisons"][0]["offers"] == result["offers"]


async def test_conditional_in_stock_price_retains_conditions(settings, store):
    page = product_page(extra_text="Цена по карте Ozon")
    result = await make_engine(settings, store, [page]).search_products("Wanbo Cube 2 Pro")
    assert len(result["offers"]) == 1
    assert result["offers"][0]["price_scope"] == "conditional"
    assert result["offers"][0]["price_conditions"] == ["bank_card"]


async def test_web_filter_preserves_articles_and_available_products(settings, store):
    available = product_page()
    unavailable = product_page(host="avito.ru", availability="OutOfStock")
    article = Page(url="https://example.com/review", source="public_page", status="verified", region="EU")
    result = await make_engine(settings, store, [available, unavailable, article]).search_web("projector")
    assert {row["url"] for row in result["results"]} == {available.url, article.url}
    assert result["excluded_product_pages"] == 1 and result["pages_read"] == 3


async def test_indexed_product_page_remains_diagnostic(settings, store):
    page = product_page()
    page.status, page.error = "unavailable", "http_403"
    result = await make_engine(settings, store, [page]).search_web("Wanbo Cube 2 Pro")
    assert result["results"] == [] and result["excluded_product_pages"] == 1
    assert result["errors"] == [{"url": page.url, "error": "http_403"}]


async def test_condition_detail_can_remove_previously_in_stock_offer(settings, store):
    listing = product_page(host="avito.ru", condition="")
    detail = product_page(host="avito.ru", condition="UsedCondition", availability="OutOfStock")

    class UpdatedFetcher(FakeFetcher):
        async def read(self, url, *args, **kwargs):
            return detail if kwargs.get("dynamic") else listing

    engine = Engine(settings, store, UpdatedFetcher([listing]), FakeDiscovery([listing]))
    result = await engine.search_products("Wanbo Cube 2 Pro", condition="used")
    assert result["offers"] == [] and result["condition_details_checked"] == 1
