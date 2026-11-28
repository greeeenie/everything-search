"""Transport, normalization, and result contracts for native marketplace sources."""

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from decimal import Decimal
import json
import re
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..browser_capture import CAPTURE_JSON_OUTPUT_LIMIT, capture_timeout
from ..config import Settings
from ..models import Evidence, Offer, Product, Region, now
from ..network import AccessError, EgressRouter, host_matches, resolve_public, validate_url
from ..shopping import available_priced_offer, identity, money, offer_matches_model, title_color

Condition = Literal["new", "used", "refurbished", "unknown"]
Availability = Literal["in_stock", "out_of_stock", "preorder", "unknown"]
OutcomeStatus = Literal["ok", "blocked", "timeout", "error", "unhealthy", "skipped"]
PriceValue = Decimal | int | float | str | None
MARKETPLACE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}


@dataclass
class SourceOutcome:
    """Serializable status of a marketplace search attempt."""

    source: str
    status: OutcomeStatus
    offers_returned: int = 0
    elapsed_ms: int = 0
    detail: str | None = None
    error_code: str | None = None
    fallback_used: bool = False
    warnings: list[str] = field(default_factory=list)
    observed_at: str = field(default_factory=now)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ConnectorSearchResult:
    products: list[Product] = field(default_factory=list)
    offers: list[Offer] = field(default_factory=list)
    outcome: dict = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)


@dataclass
class ConnectorCardResult:
    products: list[Product] = field(default_factory=list)
    offers: list[Offer] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    error: str | None = None


class ConnectorTransport:
    """Fetch public marketplace resources or call the authenticated browser worker."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str, int], Awaitable[str]] = resolve_public,
    ):
        self.settings = settings
        self.router = EgressRouter(settings)
        self.transport = transport
        self.resolver = resolver

    async def http_get(
        self,
        url: str,
        *,
        region: Region = "RU",
        headers: Mapping[str, str] | None = None,
        params: httpx.QueryParams | Mapping | None = None,
        timeout: float = 12,
    ) -> httpx.Response:
        """Perform one bounded, regional GET pinned to the validated DNS address."""
        parsed, host = validate_url(url)
        if any(host_matches(host, domain) for domain in self.settings.blocked_domains):
            raise AccessError("source_policy_denied")
        selected = self.router.choose(url, region)
        request_headers = httpx.Headers(MARKETPLACE_HEADERS)
        request_headers.update(headers or {})
        original = httpx.Request("GET", url)
        request_headers["Host"] = original.url.netloc.decode("ascii")
        if params is not None:
            original.url = original.url.copy_merge_params(params)
        try:
            async with asyncio.timeout(timeout):
                ip = await self.resolver(host, parsed.port or (443 if parsed.scheme == "https" else 80))
                pinned = original.url.copy_with(host=ip)
                async with httpx.AsyncClient(
                    proxy=self.router.proxy(selected) if self.transport is None else None,
                    transport=self.transport,
                    trust_env=False,
                    timeout=timeout,
                    follow_redirects=False,
                ) as client:
                    async with client.stream(
                        "GET", pinned, headers=request_headers, extensions={"sni_hostname": host},
                    ) as response:
                        content = await self._read(response, self.settings.max_bytes)
                        response_headers = response.headers.copy()
                        for key in ("content-encoding", "content-length", "transfer-encoding"):
                            response_headers.pop(key, None)
                        return httpx.Response(
                            response.status_code, headers=response_headers, content=content, request=original,
                        )
        except (httpx.TimeoutException, TimeoutError):
            raise AccessError("timeout") from None
        except httpx.RequestError:
            raise AccessError("ru_egress_unavailable" if selected == "RU" else "network_error") from None

    @staticmethod
    async def _read(response: httpx.Response, limit: int) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > limit:
                raise AccessError("response_too_large")
            body.extend(chunk)
        return bytes(body)

    async def _worker(self, method: str, path: str, timeout: float, payload: dict | None = None) -> dict:
        if not self.settings.browser_url:
            raise AccessError("browser_worker_not_configured")
        if not self.settings.worker_token:
            raise AccessError("worker_auth_not_configured")
        limit = (
            65536 if payload is None else self.settings.max_bytes * 6 + CAPTURE_JSON_OUTPUT_LIMIT
        )
        try:
            async with asyncio.timeout(timeout), httpx.AsyncClient(
                transport=self.transport, trust_env=False, timeout=timeout,
                headers={"Authorization": "Bearer " + self.settings.worker_token},
            ) as client:
                async with client.stream(
                    method, self.settings.browser_url.rstrip("/") + path, json=payload,
                ) as response:
                    if response.status_code not in {200, 422}:
                        response.raise_for_status()
                    result = json.loads(await self._read(response, limit))
                    if not isinstance(result, dict) or (response.status_code == 422 and not result.get("error")):
                        raise ValueError("invalid worker response")
                    return result
        except (httpx.TimeoutException, TimeoutError):
            raise AccessError("browser_timeout") from None
        except (httpx.HTTPError, ValueError, RecursionError):
            raise AccessError("worker_unavailable") from None

    async def cdp_fetch(
        self,
        url: str,
        *,
        region: Region = "RU",
        wait_selector: str | None = None,
        wait_ms: int = 2500,
        wait_for_challenge: bool = True,
        scroll_steps: int = 1,
        capture_json_patterns: list[str] | None = None,
        timeout_s: float = 35,
        block_media: bool = True,
        user_agent_compat: Literal["windows"] | None = None,
    ) -> dict:
        """Return HTML and matching JSON, or the worker's structured fetch error."""
        validate_url(url)
        return await self._worker("POST", "/cdp-fetch", timeout_s + 3, {
            "url": url, "region": region, "wait_selector": wait_selector, "wait_ms": wait_ms,
            "wait_for_challenge": wait_for_challenge, "scroll_steps": scroll_steps,
            "capture_json_patterns": capture_json_patterns, "timeout_s": timeout_s,
            "block_media": block_media, "user_agent_compat": user_agent_compat,
        })

    async def camoufox_fetch(
        self,
        url: str,
        *,
        region: Region = "RU",
        allow_search: bool = False,
        capture_json_patterns: list[str] | None = None,
    ) -> dict:
        """Return the guarded capture or the worker's structured capture error."""
        validate_url(url)
        return await self._worker("POST", "/capture", capture_timeout(self.settings, url) + 3, {
            "url": url, "region": region, "allow_search": allow_search,
            "capture_json_patterns": capture_json_patterns,
        })

    async def cdp_health(self) -> dict:
        try:
            return await self._worker("GET", "/cdp-health", 5)
        except AccessError as error:
            return {"reachable": False, "reason": str(error)}


def parse_price_rub(value: PriceValue) -> Decimal | None:
    """Normalize a public RUB price while rejecting invalid and implausible values."""
    if isinstance(value, str):
        value = re.sub(r"\s+", "", value)
        value = re.sub(r"(?i)(?:₽|руб\.?|rub|rur)$", "", value)
    return money(value)


_CONDITION_PATTERNS: tuple[tuple[Condition, str], ...] = (
    ("refurbished", r"\b(?:refurbished(?:condition)?|renewed|восстановлен\w*|уцен\w*|витринн\w*)\b"),
    ("used", r"\b(?:used(?:condition)?|pre-owned|б\s*[/.]\s*у\.?|бу|бывш\w+ в употреблении)\b"),
    ("new", r"\b(?:new(?:condition)?|новый|новое|новая|новые)\b"),
    ("unknown", r"^unknown$"),
)


def infer_condition(title: str, explicit: str | None = None, default: str = "new") -> Condition:
    """Prefer explicit marketplace condition labels and then inspect the title."""
    for text in (explicit, title):
        if text:
            for condition, pattern in _CONDITION_PATTERNS:
                if re.search(pattern, text.strip().casefold()):
                    return condition
    for condition, _ in _CONDITION_PATTERNS:
        if condition == default:
            return condition
    return "unknown"


def make_product_and_offer(
    marketplace: str,
    url: str,
    title: str,
    price: PriceValue = None,
    *,
    model: str | None = None,
    brand: str | None = None,
    seller: str | None = None,
    condition: str | None = None,
    default_condition: Condition = "new",
    availability: Availability = "in_stock",
    conditional_price: PriceValue = None,
    original_price: PriceValue = None,
    price_conditions: list[str] | None = None,
    rating: float | None = None,
    reviews_count: int | None = None,
    image_url: str | None = None,
    sku: str | int | None = None,
    specifications: Mapping[str, str] | None = None,
    variants: Mapping[str, str] | None = None,
    observed_at: str | None = None,
    http_status: int | None = None,
) -> tuple[Product, Offer]:
    """Build a canonical product and a verified offer from observed source fields."""
    validate_url(url)
    if image_url:
        try:
            validate_url(image_url)
        except AccessError:
            image_url = None
    public_price = parse_price_rub(price)
    parsed_conditional_price = parse_price_rub(conditional_price)
    offer_variants = dict(variants or {})
    if color := title_color(title):
        offer_variants.setdefault("color", color)
    evidence = Evidence(
        url=url, status="verified", source=f"native:{marketplace}", region="RU",
        checked_at=observed_at or now(), http_status=http_status,
        excerpts=[f"Title={title}; price={public_price}; availability={availability}"],
    )
    product = Product(
        id=identity(model or title), name=model or title, brand=brand, model=model,
        identity_basis="exact query in marketplace title" if model else "exact marketplace title",
        specifications=dict(specifications or {}), sources=[evidence],
    )
    effective_price = public_price if public_price is not None else parsed_conditional_price
    offer = Offer(
        url=url, product_id=product.id, title=title, marketplace=marketplace,
        seller=seller, price=effective_price, currency="RUB" if effective_price is not None else None,
        conditional_price=parsed_conditional_price, original_price=parse_price_rub(original_price),
        price_scope="public" if public_price is not None else "conditional" if parsed_conditional_price is not None else "unknown",
        price_conditions=price_conditions or [],
        condition=infer_condition(title, condition, default_condition), availability=availability,
        rating=rating, reviews_count=reviews_count, image_url=image_url, sku=str(sku) if sku is not None else None,
        variants=offer_variants, evidence=evidence,
    )
    return product, offer


class MarketplaceConnector(ABC):
    """One marketplace's native search, card lookup, and optional review lookup."""

    name: str
    domain: str
    supports_search: bool = True
    supports_reviews: bool = False

    def __init__(self, transport: ConnectorTransport):
        self.transport = transport

    def matches_url(self, url: str) -> bool:
        try:
            _, host = validate_url(url)
        except AccessError:
            return False
        return host_matches(host, self.domain)

    @abstractmethod
    async def search(
        self, query: str, *, model: str | None = None, limit: int = 12, city: str | None = None,
    ) -> ConnectorSearchResult:
        """Return available marketplace offers for a search query."""

    @abstractmethod
    async def get_card(self, url_or_sku: str, *, model: str | None = None) -> ConnectorCardResult:
        """Return observed product details and offers for one listing."""

    async def get_reviews(self, model: str, *, limit: int = 10) -> tuple[list[dict], list[dict]]:
        """Return reviews and source errors when the connector supports review lookup."""
        return [], []

    @staticmethod
    def _filter_and_dedupe(
        products: list[Product], offers: list[Offer], *, model: str | None = None, limit: int = 12,
    ) -> tuple[list[Product], list[Offer]]:
        if limit <= 0:
            return [], []
        selected = []
        seen = set()
        known_products = {product.id: product for product in products}
        for offer in offers:
            product = known_products.get(offer.product_id)
            if product is None or not available_priced_offer(offer):
                continue
            if model and not offer_matches_model(model, product, offer):
                continue
            url = urlsplit(offer.url)
            key = urlunsplit((url.scheme, url.netloc.lower(), url.path, url.query, ""))
            if key in seen:
                continue
            seen.add(key)
            selected.append(offer)
            if len(selected) == limit:
                break
        return [known_products[pid] for pid in dict.fromkeys(offer.product_id for offer in selected)], selected
