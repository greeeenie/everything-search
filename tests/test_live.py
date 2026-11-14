"""Opt-in real public-source checks. Never assert a preselected current price."""

import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RESEARCH_LIVE") != "1", reason="Set RESEARCH_LIVE=1 on the deployed host"),
]


async def call(tool, arguments):
    async with streamable_http_client(os.getenv("RESEARCH_MCP_URL", "http://127.0.0.1:8765/mcp")) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            assert not result.isError, result.content
            assert result.structuredContent is not None
            return result.structuredContent


async def test_live_distinct_regional_routes():
    result = await call("diagnose_egress", {})
    assert result["distinct_ips"]
    assert {x["region"]: x["country"] for x in result["egress"]} == {"RU": "RU", "EU": "LV"}
    assert all(x["status"] == "verified" for x in result["egress"])


async def test_live_browser_guard_uses_both_exits():
    from research_v2.browser_network import BrowserNetwork
    from research_v2.config import Settings

    settings = Settings.load()
    exits = {}
    for region, country in settings.expected_countries.items():
        async with BrowserNetwork(settings, region, ["www.cloudflare.com"]) as network:
            async with httpx.AsyncClient(proxy=network.url, timeout=15, trust_env=False) as client:
                response = await client.get("https://www.cloudflare.com/cdn-cgi/trace")
        assert response.status_code == 200
        values = dict(line.split("=", 1) for line in response.text.splitlines() if "=" in line)
        assert values.get("loc") == country
        exits[region] = values["ip"]
    assert exits["RU"] != exits["EU"]


@pytest.mark.parametrize("marketplace", ["yandex_market", "avito", "ozon"])
async def test_live_public_product_has_evidenced_price(marketplace):
    result = await call(
        "search_products",
        {
            "query": "Wanbo Cube 2 Pro",
            "model": "Wanbo Cube 2 Pro",
            "marketplaces": [marketplace],
            "max_results": 4,
        },
    )
    assert result["products"] and result["offers"], result["candidates"]
    assert any(offer["price"] is not None and offer["currency"] for offer in result["offers"])
    assert all(
        offer["evidence"]["checked_at"]
        and offer["evidence"]["status"] == "verified"
        and offer["evidence"]["region"] == "RU"
        and offer["marketplace"] == marketplace
        and offer["availability"] == "in_stock"
        and offer["price"] is not None
        and float(offer["price"]) > 0
        and offer["currency"]
        for offer in result["offers"]
    )


async def test_live_marketplace_restriction_is_explicit():
    result = await call(
        "get_product_details",
        {
            "url": "https://www.ozon.ru/product/proektor-dlya-filmov-portativnyy-wanbo-cube-2-pro-android-11-golosovoy-pomoshchnik-1gb-8gb-500-ansi-3071004273/",
            "region": "RU",
        },
    )
    page = result["page"]
    assert page["checked_at"] and page["region"] == "RU"
    if page["status"] == "unavailable":
        assert page["error"] and not result["offers"]
    else:
        assert page["status"] == "verified"


async def test_live_ozon_card_uses_automatic_browser():
    result = await call(
        "get_product_details",
        {
            "url": "https://www.ozon.ru/product/proektor-wanbo-projector-cube-2-pro-belyy-3456542658/",
            "model": "Wanbo Cube 2 Pro",
            "region": "RU",
        },
    )
    assert result["page"]["status"] == "verified", result["page"].get("error")
    assert result["page"]["extractor"].startswith("camoufox")
    assert result["offers"] and all(offer["evidence"]["status"] == "verified" for offer in result["offers"])
    assert any(offer["price"] is not None and offer["currency"] == "RUB" for offer in result["offers"])


async def test_live_web_product_rows_require_stock_and_price():
    result = await call("search_web", {"query": '"Wanbo Vali 1 Pro"', "site": "ozon.ru", "max_results": 3})
    assert isinstance(result["excluded_product_pages"], int)
    assert result["pages_read"] > 0, result["errors"]
    for row in result["results"]:
        assert row["offers"]
        assert all(
            offer["evidence"]["status"] == "verified"
            and offer["availability"] == "in_stock"
            and offer["price"] is not None
            and float(offer["price"]) > 0
            and offer["currency"]
            for offer in row["offers"]
        )
