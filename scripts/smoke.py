"""Live public-source smoke run. No fixed expected prices.

Run on the selected host as the Research service user. Reports failures and
access restrictions explicitly, writes a dated JSON artifact when requested.
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from research_v2.models import now


async def main(url, output):
    report = {"checked_at": now(), "endpoint": url, "checks": []}
    # systemd Type=simple can return before Python opens the listening socket.
    async with httpx.AsyncClient(timeout=2, trust_env=False) as readiness:
        async with asyncio.timeout(20):
            while True:
                try:
                    if (await readiness.get(url.rsplit("/mcp", 1)[0] + "/health")).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(1)
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            report["tools"] = [tool.name for tool in (await session.list_tools()).tools]
            cases = [
                ("diagnose_egress", {}),
                (
                    "search_web",
                    {"query": "Python documentation asyncio", "site": "docs.python.org", "max_results": 2},
                ),
                ("read_page", {"url": "https://docs.python.org/3/library/asyncio.html"}),
                ("read_page", {"url": "http://127.0.0.1/"}),
                (
                    "search_products",
                    {
                        "query": "Wanbo Cube 2 Pro",
                        "model": "Wanbo Cube 2 Pro",
                        "city": "Омск",
                        "max_results": 6,
                    },
                ),
                ("compare_prices", {"model": "Wanbo Cube 2 Pro", "city": "Омск", "max_results": 6}),
                (
                    "get_product_details",
                    {
                        "url": "https://www.ozon.ru/product/proektor-dlya-filmov-portativnyy-wanbo-cube-2-pro-android-11-golosovoy-pomoshchnik-1gb-8gb-500-ansi-3071004273/"
                    },
                ),
                ("get_product_reviews", {"model": "Wanbo Cube 2 Pro", "max_results": 4}),
                ("research", {"query": "Steam Deck OLED battery life", "max_threads": 1}),
                ("search_hacker_news", {"query": "Playwright", "max_results": 1}),
                (
                    "search_github_issues",
                    {"query": "chromium", "repository": "microsoft/playwright", "max_results": 1},
                ),
                ("research", {"query": "Python asyncio cancellation", "mode": "quick"}),
            ]
            for name, arguments in cases:
                try:
                    async with asyncio.timeout(100 if name != "research" else 190):
                        result = await session.call_tool(name, arguments)
                    data = result.structuredContent
                    row = {"tool": name, "arguments": arguments, "is_error": result.isError, "result": data}
                    if data is None:
                        row["message"] = str(result.content)[:500]
                    if name == "diagnose_egress" and data:
                        for egress in data["egress"]:
                            egress.pop("ip", None)
                    report["checks"].append(row)
                    print(name, "ERROR" if result.isError else "returned", flush=True)
                except TimeoutError:
                    report["checks"].append({"tool": name, "error": "smoke_timeout"})
                    print(name, "smoke_timeout", flush=True)
    if output:
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--output")
    args = parser.parse_args()
    asyncio.run(main(args.url, args.output))
