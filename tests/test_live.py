"""Opt-in real public-source checks. Never assert a preselected current price."""

import os

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


async def test_live_public_product_has_evidenced_price():
    result = await call(
        "get_product_details", {"url": "https://www.adafruit.com/product/5813", "model": "Raspberry Pi 5"}
    )
    assert result["page"]["status"] == "verified", result["page"].get("error")
    assert result["products"] and result["offers"]
    assert any(offer["price"] is not None and offer["currency"] for offer in result["offers"])
    assert all(
        offer["evidence"]["checked_at"] and offer["evidence"]["status"] == "verified"
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
