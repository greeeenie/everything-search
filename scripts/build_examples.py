"""Generate compact example responses from the local, private live-test captures."""

import json
from pathlib import Path

destination = Path("examples")
destination.mkdir(exist_ok=True)
source = Path("artifacts/private/research-public-product.json")
data = json.loads(source.read_text(encoding="utf-8"))["result"]
compact = {key: data[key] for key in ("checked_at", "products", "offers", "extraction_error")}
compact["page"] = {
    key: data["page"][key]
    for key in ("url", "final_url", "source", "checked_at", "status", "region", "http_status")
}
(destination / "verified-product-response.json").write_text(
    json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8"
)
smoke = json.loads(Path("artifacts/private/smoke-current.json").read_text(encoding="utf-8"))
shopping = next(row["result"] for row in smoke["checks"] if row["tool"] == "search_products")
indexed = {
    key: shopping[key] for key in ("query", "model", "city", "checked_at", "products", "offers", "errors")
}
indexed["candidates"] = [next(row for row in shopping["candidates"] if "ozon.ru/" in row["url"])]
(destination / "indexed-only-response.json").write_text(
    json.dumps(indexed, ensure_ascii=False, indent=2), encoding="utf-8"
)
