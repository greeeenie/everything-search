from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Region = Literal["EU", "RU"]
Status = Literal["verified", "indexed_only", "unavailable"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Evidence(BaseModel):
    url: str
    source: str
    checked_at: str = Field(default_factory=now)
    status: Status
    region: Region
    error: str | None = None
    http_status: int | None = None
    excerpts: list[str] = Field(default_factory=list)


class Page(Evidence):
    final_url: str | None = None
    title: str = ""
    text: str = ""
    html: str = Field(default="", exclude=True)
    structured_data: list[dict] = Field(default_factory=list)
    paragraphs: list[str] = Field(default_factory=list)
    variant_labels: dict[str, str] = Field(default_factory=dict)
    cached: bool = False
    extractor: str = "httpx+beautifulsoup"


class Product(BaseModel):
    id: str
    name: str
    brand: str | None = None
    model: str | None = None
    identity_basis: str = "exact normalized model"
    specifications: dict[str, str] = Field(default_factory=dict)
    specification_conflicts: dict[str, list[str]] = Field(default_factory=dict)
    variants: list[str] = Field(default_factory=list)
    sources: list[Evidence] = Field(default_factory=list)


class Offer(BaseModel):
    url: str = ""
    product_id: str
    title: str
    marketplace: str
    seller: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    price_conditions: list[str] = Field(default_factory=list)
    price_scope: Literal["public", "conditional", "unknown"] = "unknown"
    condition: Literal["new", "used", "refurbished", "unknown"] = "unknown"
    availability: Literal["in_stock", "out_of_stock", "preorder", "unknown"] = "unknown"
    delivery: dict = Field(default_factory=dict)
    delivery_city: str | None = None
    delivery_status: Literal["confirmed", "unconfirmed"] = "unconfirmed"
    shipping_cost: Decimal | None = None
    total_price: Decimal | None = None
    variants: dict[str, str] = Field(default_factory=dict)
    evidence: Evidence


class SearchHit(Evidence):
    title: str
    search_excerpt: str = ""
