"""Call the isolated trial through its authenticated MCP endpoint."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from dotenv import dotenv_values


async def main(args):
    os.chdir("/opt/ru-marketplace-mcp")
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from mcp_core.redact import redact_error_text

    environment = dotenv_values("/etc/marketplace-trial/server.env")
    transport = StreamableHttpTransport(
        "http://127.0.0.1:8790/mcp",
        headers={
            "Authorization": "Bearer " + environment["MCP_HTTP_AUTH_TOKEN"],
            "X-MCP-Tenant": environment["MCP_HTTP_TENANT_ID"],
        },
    )
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "checks": []}
    async with Client(transport, timeout=160) as client:
        if args.inventory:
            tools = await client.list_tools()
            report["tools"] = [tool.model_dump(mode="json", exclude_none=True) for tool in tools]
            cases = [("marketplace_sources", {}), ("marketplace_health", {})]
            print("tools", len(tools), [tool.name for tool in tools], flush=True)
        elif args.cases:
            cases = [(case["tool"], case.get("arguments", {})) for case in json.loads(args.cases.read_text(encoding="utf-8"))]
        else:
            cases = [(args.tool, json.loads(args.arguments))]
        for name, arguments in cases:
            started = time.monotonic()
            try:
                result = await client.call_tool(name, arguments, raise_on_error=False, timeout=150)
                payload = result.structured_content
                if payload is None:
                    payload = [block.model_dump(mode="json") for block in result.content]
                entry = {"tool": name, "arguments": arguments, "is_error": result.is_error, "result": payload}
            except Exception as error:
                entry = {"tool": name, "arguments": arguments, "is_error": True, "error": redact_error_text(str(error))}
            entry["elapsed"] = round(time.monotonic() - started, 2)
            report["checks"].append(entry)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(name, "error" if entry["is_error"] else "ok", entry["elapsed"], flush=True)


parser = argparse.ArgumentParser()
parser.add_argument("--inventory", action="store_true")
parser.add_argument("--tool", default="marketplace_sources")
parser.add_argument("--cases", type=Path)
parser.add_argument("--arguments", default="{}")
parser.add_argument("--output", type=Path, default=Path("/var/lib/marketplace-trial/last-probe.json"))
asyncio.run(main(parser.parse_args()))
