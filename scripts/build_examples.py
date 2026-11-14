"""Generate compact examples from private, real Russian-marketplace captures."""

import copy
import json
from pathlib import Path

destination = Path("examples")
destination.mkdir(exist_ok=True)
source = Path("artifacts/private/ru-marketplaces-automatic.json")
checks = json.loads(source.read_text(encoding="utf-8"))["checks"]
data = next(
    row["result"]
    for row in checks
    if row["tool"] == "get_product_details"
    and any(offer["marketplace"] == "avito" for offer in row["result"].get("offers", []))
)
compact = {key: data[key] for key in ("checked_at", "products", "offers", "extraction_error")}
compact["page"] = {
    key: data["page"][key]
    for key in ("url", "final_url", "source", "checked_at", "status", "region", "http_status")
}
(destination / "verified-product-response.json").write_text(
    json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8"
)
shopping = max(
    (row["result"] for row in checks if row["tool"] == "search_products"),
    key=lambda result: len(result["offers"]),
)
sample = copy.deepcopy(shopping)
sample["offers"] = [offer for offer in shopping["offers"] if offer["marketplace"] == "avito"][:2] + [
    offer for offer in shopping["offers"] if offer["marketplace"] == "yandex_market"
][:1]
for product in sample["products"]:
    product["sources"] = product["sources"][:2]
(destination / "marketplace-search-response.json").write_text(
    json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8"
)
indexed = {key: shopping[key] for key in ("query", "model", "city", "checked_at", "errors")}
indexed.update(products=[], offers=[])
indexed["candidates"] = [next(row for row in shopping["candidates"] if "ozon.ru/" in row["url"])]
(destination / "indexed-only-response.json").write_text(
    json.dumps(indexed, ensure_ascii=False, indent=2), encoding="utf-8"
)
