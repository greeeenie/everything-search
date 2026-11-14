import asyncio
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    async with streamable_http_client("http://127.0.0.1:8765/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("TOOLS", [tool.name for tool in (await session.list_tools()).tools])
    path = Path("/run/reddit-research-tunnel/health-url")
    if path.exists():
        base = path.read_text().strip()
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            for suffix in ("/", "/healthz", "/readyz", "/livez"):
                response = await client.get(base + suffix)
                print(suffix, response.status_code, response.text[:2000])


asyncio.run(main())
