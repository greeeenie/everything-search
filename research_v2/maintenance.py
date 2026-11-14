import argparse
import asyncio
import json

from .engine import Engine


async def main(clear=False):
    engine = Engine()
    if clear:
        engine.store.clear()
        print("History cleared")
        return
    engine.store.prune()
    result = await engine.diagnose_egress()
    # Report countries and readiness. Keep home IP out of routine logs.
    print(
        json.dumps(
            {
                "checked_at": result["checked_at"],
                "distinct_ips": result["distinct_ips"],
                "egress": [{k: v for k, v in row.items() if k != "ip"} for row in result["egress"]],
            }
        )
    )
    if any(row["status"] != "verified" for row in result["egress"]):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--clear", action="store_true")
    asyncio.run(main(parser.parse_args().clear))
