import argparse
import asyncio
import json
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(tool, arguments, url):
    started = time.monotonic()
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            print(
                json.dumps(
                    {
                        "tool": tool,
                        "elapsed": time.monotonic() - started,
                        "is_error": result.isError,
                        "result": result.structuredContent,
                    },
                    ensure_ascii=False,
                )
            )


parser = argparse.ArgumentParser()
parser.add_argument("tool")
parser.add_argument("arguments_file")
parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
args = parser.parse_args()
asyncio.run(main(args.tool, json.load(open(args.arguments_file, encoding="utf-8")), args.url))
