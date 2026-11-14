import asyncio
import json
import os
from pathlib import Path

import httpx

for line in Path("/etc/research-v2/research.env").read_text().splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        os.environ[key] = value


async def main():
    async with httpx.AsyncClient(
        timeout=45,
        trust_env=False,
        headers={"Authorization": "Bearer " + os.environ["RESEARCH_WORKER_TOKEN"]},
    ) as client:
        cases = [
            (
                "/extract",
                {
                    "html": "<html><body><article><h1>Research worker</h1><p>Public evidence extraction works with Crawl4AI.</p></article></body></html>"
                },
            ),
            ("/render", {"url": "file:///etc/passwd", "region": "EU"}),
            ("/render", {"url": "https://quotes.toscrape.com/js/", "region": "EU"}),
        ]
        for path, payload in cases:
            response = await client.post("http://127.0.0.1:8770" + path, json=payload)
            data = response.json()
            if "html" in data:
                data = {
                    "html_bytes": len(data["html"]),
                    "final_url": data.get("final_url"),
                    "has_quotes": "quote" in data["html"],
                }
            print(json.dumps({"path": path, "status": response.status_code, "result": data}))


asyncio.run(main())
