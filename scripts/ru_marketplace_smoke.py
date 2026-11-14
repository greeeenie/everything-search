"""Exercise only Ozon, Avito and Yandex Market through the normal MCP tools."""

import asyncio
import json
from pathlib import Path
import time
import httpx

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    report = {"checks": []}
    destination = Path("/var/lib/research-v2/ru-marketplaces-smoke.json")
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        async with asyncio.timeout(20):
            while True:
                try:
                    if (await client.get("http://127.0.0.1:8765/health")).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(1)
    cases = [
        (
            "read_page",
            {"url": "https://www.avito.ru/all?q=Wanbo+Cube+2+Pro", "region": "RU", "refresh": True},
        ),
        (
            "get_product_details",
            {
                "url": "https://www.ozon.ru/product/proektor-dlya-filmov-portativnyy-wanbo-cube-2-pro-s-golosovym-upravleniem-android-11-yarkostyu-500-3070977459/",
                "model": "Wanbo Cube 2 Pro",
                "region": "RU",
            },
        ),
        (
            "get_product_details",
            {
                "url": "https://market.yandex.ru/card/proyektor-wanbo-cube-2-pro-belyy/4756644609",
                "model": "Wanbo Cube 2 Pro",
                "region": "RU",
            },
        ),
        (
            "search_products",
            {
                "query": "Wanbo Cube 2 Pro",
                "model": "Wanbo Cube 2 Pro",
                "marketplaces": ["avito"],
                "max_results": 5,
                "city": "Омск",
            },
        ),
        (
            "search_products",
            {
                "query": "Wanbo Cube 2 Pro",
                "model": "Wanbo Cube 2 Pro",
                "marketplaces": ["ozon", "avito", "yandex_market"],
                "max_results": 9,
                "city": "Омск",
            },
        ),
    ]
    async with streamable_http_client("http://127.0.0.1:8765/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for name, arguments in cases:
                started = time.monotonic()
                result = await session.call_tool(name, arguments)
                report["checks"].append(
                    {
                        "tool": name,
                        "arguments": arguments,
                        "is_error": result.isError,
                        "elapsed": time.monotonic() - started,
                        "result": result.structuredContent,
                    }
                )
                print(name, arguments.get("url") or arguments.get("marketplaces"), result.isError, flush=True)
                destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            offers = report["checks"][-1]["result"]["offers"]
            avito = next((offer for offer in offers if offer["marketplace"] == "avito"), None)
            if avito:
                started = time.monotonic()
                result = await session.call_tool(
                    "get_product_details", {"url": avito["url"], "model": "Wanbo Cube 2 Pro", "region": "RU"}
                )
                report["checks"].append(
                    {
                        "tool": "get_product_details",
                        "arguments": {"url": avito["url"]},
                        "is_error": result.isError,
                        "elapsed": time.monotonic() - started,
                        "result": result.structuredContent,
                    }
                )
                print("avito offer details", result.isError, flush=True)
            result = await session.call_tool(
                "compare_prices",
                {"model": "Wanbo Cube 2 Pro", "marketplaces": ["avito", "yandex_market"], "city": "Омск"},
            )
            report["checks"].append(
                {"tool": "compare_prices", "is_error": result.isError, "result": result.structuredContent}
            )
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            started = time.monotonic()
            result = await session.call_tool(
                "search_products",
                {
                    "query": "Wanbo Cube 2 Pro",
                    "model": "Wanbo Cube 2 Pro",
                    "marketplaces": ["avito"],
                    "condition": "used",
                    "max_results": 4,
                },
            )
            report["checks"].append(
                {
                    "tool": "search_products",
                    "arguments": {"condition": "used"},
                    "is_error": result.isError,
                    "elapsed": time.monotonic() - started,
                    "result": result.structuredContent,
                }
            )
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


asyncio.run(main())
