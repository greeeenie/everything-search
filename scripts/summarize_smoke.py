import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
for row in report["checks"]:
    data = row.get("result") or {}
    print(
        json.dumps(
            {
                "tool": row["tool"],
                "error": row.get("error") or row.get("message"),
                "status": data.get("status"),
                "pages": data.get("pages_read"),
                "offers": len(data.get("offers", [])),
                "products": len(data.get("products", [])),
                "results": len(data.get("results", [])) if isinstance(data.get("results"), list) else None,
                "errors": data.get("errors"),
                "fetch_error": data.get("error"),
                "candidates": [
                    {"url": x["url"], "error": x.get("fetch_error"), "status": x.get("fetch_status")}
                    for x in data.get("candidates", [])
                ],
                "source_errors": [x.get("fetch_error") for x in data.get("sources", [])],
                "threads_read": data.get("threads_read"),
                "egress": data.get("egress"),
            },
            ensure_ascii=False,
        )
    )
