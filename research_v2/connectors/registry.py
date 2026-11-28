"""Fan out marketplace requests to the in-process connector registry."""

import asyncio
from itertools import zip_longest
import json
from time import monotonic

from .aliexpress import AliExpressConnector
from .avito import AvitoConnector
from .base import ConnectorSearchResult, ConnectorTransport, MarketplaceConnector, SourceOutcome
from .citilink import CitilinkConnector
from .detmir import DetmirConnector
from .dns import DnsConnector
from .lamoda import LamodaConnector
from .megamarket import MegamarketConnector
from .ozon import OzonConnector
from .wildberries import WildberriesConnector
from .yandex_market import YandexMarketConnector
from ..config import Settings
from ..discovery import canonical_url
from ..models import Offer, Product
from ..network import AccessError
from ..shopping import (
    available_priced_offer, dedupe_reviews, merge_product, normalize_conditional_price, offer_key,
    offer_matches_model,
)
from ..store import Store


CONNECTOR_TYPES = (
    WildberriesConnector, OzonConnector, YandexMarketConnector, AvitoConnector, AliExpressConnector,
    DnsConnector, CitilinkConnector, MegamarketConnector, LamodaConnector, DetmirConnector,
)
BREAKER_FAILURES = {"blocked", "timeout", "error"}
SEARCH_TIMEOUT = 45
CARD_TIMEOUT = 45
REVIEW_TIMEOUT = 35
BREAKER_COOLDOWN = 90


def _error_code(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "connector_timeout"
    if isinstance(error, AccessError):
        return str(error)
    return "connector_error"


class NativeMarketplaceHub:
    def __init__(
        self, settings: Settings, store: Store, transport: ConnectorTransport | None = None,
        connectors: dict[str, MarketplaceConnector] | None = None,
    ):
        self.settings = settings
        self.store = store
        self.transport = transport if transport is not None else ConnectorTransport(settings)
        self.connectors = dict(connectors) if connectors is not None else {
            connector.name: connector(self.transport) for connector in CONNECTOR_TYPES
        }
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    @property
    def known_marketplaces(self) -> set[str]:
        return set(self.connectors)

    def connector_for_url(self, url: str) -> MarketplaceConnector | None:
        return next((connector for connector in self.connectors.values() if connector.matches_url(url)), None)

    def _is_open(self, source: str) -> bool:
        return self._open_until.get(source, 0) > monotonic()

    async def health(self) -> dict:
        cdp = await self.transport.cdp_health()
        checked = monotonic()
        return {
            "status": "ok" if cdp.get("reachable") else "degraded",
            "engine": "native",
            "connectors": sorted(self.connectors),
            "cdp": cdp,
            "breakers": {name: until - checked for name, until in self._open_until.items() if until > checked},
        }

    async def _search(self, source, query, *, model, limit, city) -> ConnectorSearchResult:
        started = monotonic()
        if self._is_open(source):
            return ConnectorSearchResult(outcome=SourceOutcome(
                source, "blocked", error_code="circuit_open",
            ).as_dict())
        connector = self.connectors.get(source)
        if connector is None:
            return ConnectorSearchResult(outcome=SourceOutcome(
                source, "skipped", error_code="unknown_marketplace",
            ).as_dict())
        try:
            async with asyncio.timeout(SEARCH_TIMEOUT):
                result = await connector.search(query, model=model, limit=limit, city=city)
                result = self._filter_search(result, model, limit)
        except Exception as error:
            result = self._failed_search(source, error, started)
        self._update_breaker(source, result.outcome.get("status", "ok"))
        return result

    @staticmethod
    def _filter_search(result: ConnectorSearchResult, model: str | None, limit: int) -> ConnectorSearchResult:
        if limit <= 0:
            return ConnectorSearchResult(outcome=result.outcome, errors=result.errors)
        products = {product.id: product for product in result.products}
        offers = {}
        for raw in result.offers:
            offer = normalize_conditional_price(raw)
            product = products.get(offer.product_id)
            if product is None or not available_priced_offer(offer):
                continue
            if model and not offer_matches_model(model, product, offer):
                continue
            offers.setdefault(offer_key(offer), offer)
            if len(offers) == limit:
                break
        selected = dict.fromkeys(offer.product_id for offer in offers.values())
        return ConnectorSearchResult(
            [products[product_id] for product_id in selected], list(offers.values()), result.outcome, result.errors,
        )

    @staticmethod
    def _failed_search(source: str, error: BaseException, started: float) -> ConnectorSearchResult:
        return ConnectorSearchResult(outcome=SourceOutcome(
            source, "timeout" if isinstance(error, TimeoutError) else "error",
            elapsed_ms=int((monotonic() - started) * 1000), error_code=_error_code(error),
        ).as_dict())

    def _update_breaker(self, source: str, status: str) -> None:
        if status == "ok":
            self._failures.pop(source, None)
            self._open_until.pop(source, None)
        elif status in BREAKER_FAILURES and not self._is_open(source):
            self._failures[source] = self._failures.get(source, 0) + 1
            if self._failures[source] >= 3:
                self._open_until[source] = monotonic() + BREAKER_COOLDOWN

    async def compare(
        self, query: str, marketplaces: list[str] | None = None, *,
        model: str | None = None, limit: int = 12, city: str | None = None,
    ) -> tuple[list[Product], list[Offer], list[dict], list[dict]]:
        sources = sorted(set(self.connectors if marketplaces is None else marketplaces))
        key = "native:search:" + json.dumps([query, sources, model, limit, city], ensure_ascii=False)
        cached = self.store.get(key)
        if cached and all(
            (outcome["error_code"] == "circuit_open") == self._is_open(outcome["source"])
            for outcome in cached[2] if outcome["status"] in BREAKER_FAILURES
        ):
            products, offers, outcomes, errors = cached
            return [Product.model_validate(row) for row in products], [
                Offer.model_validate(row) for row in offers
            ], outcomes, errors

        started = monotonic()
        results = await asyncio.gather(
            *(self._search(source, query, model=model, limit=limit, city=city) for source in sources),
            return_exceptions=True,
        )
        products, offers, outcomes, errors = {}, {}, [], []
        for source, result in zip(sources, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                result = self._failed_search(source, result, started)
                self._update_breaker(source, result.outcome["status"])
            for product in result.products:
                merge_product(products, product)
            for offer in result.offers:
                offers.setdefault(offer_key(offer), offer)

            outcome = {**SourceOutcome(source, "ok").as_dict(), **result.outcome}
            outcome.update(source=source, offers_returned=len(result.offers))
            outcomes.append(outcome)
            errors.extend(result.errors)
            if outcome["error_code"] and not result.errors:
                errors.append({"source": source, "error": outcome["error_code"]})

        product_ids = {offer.product_id for offer in offers.values()}
        merged_products = [product for product in products.values() if product.id in product_ids]
        merged_offers = list(offers.values())
        if merged_offers:
            self.store.put(key, [
                [product.model_dump(mode="json") for product in merged_products],
                [offer.model_dump(mode="json") for offer in merged_offers],
                outcomes, errors,
            ], self.settings.search_ttl)
        return merged_products, merged_offers, outcomes, errors

    async def get_card(
        self, url: str, *, model: str | None = None,
    ) -> tuple[list[Product], list[Offer], list[dict], str | None]:
        connector = self.connector_for_url(url)
        if connector is None:
            return [], [], [], "unsupported_marketplace_url"
        key = "native:card:" + json.dumps([canonical_url(url), model], ensure_ascii=False)
        if cached := self.store.get(key):
            products, offers, reviews = cached
            return [Product.model_validate(row) for row in products], [
                Offer.model_validate(row) for row in offers
            ], reviews, None
        try:
            async with asyncio.timeout(CARD_TIMEOUT):
                result = await connector.get_card(url, model=model)
        except Exception as error:
            return [], [], [], _error_code(error)
        offers = [normalize_conditional_price(offer) for offer in result.offers]
        if not result.error and (result.products or offers or result.reviews):
            self.store.put(key, [
                [product.model_dump(mode="json") for product in result.products],
                [offer.model_dump(mode="json") for offer in offers],
                result.reviews,
            ], self.settings.page_ttl)
        return result.products, offers, result.reviews, result.error

    async def get_reviews(self, model: str, *, limit: int = 10) -> tuple[list[dict], list[dict]]:
        if limit <= 0:
            return [], []
        sources = {
            name: connector for name, connector in self.connectors.items() if connector.supports_reviews
        }

        async def read(connector):
            async with asyncio.timeout(REVIEW_TIMEOUT):
                return await connector.get_reviews(model, limit=limit)

        results = await asyncio.gather(*(read(connector) for connector in sources.values()), return_exceptions=True)
        batches, errors = [], []
        for source, result in zip(sources, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                errors.append({"source": source, "stage": "reviews", "error": _error_code(result)})
            else:
                reviews, source_errors = result
                batches.append(reviews)
                errors.extend(source_errors)
        reviews = dedupe_reviews(review for batch in zip_longest(*batches) for review in batch if review is not None)
        return reviews[:limit], errors
