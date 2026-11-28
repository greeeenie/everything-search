"""Detsky Mir's public JSON API with a rendered-page fallback."""

import re
from time import monotonic
from urllib.parse import urlencode, urlsplit

from ..network import AccessError
from .dns import (
    _CdpMarketplaceConnector,
    _Listing,
    _captured,
    _count,
    _image,
    _named,
    _rating,
    _specifications,
    _stock,
    _text,
)


class DetmirConnector(_CdpMarketplaceConnector):
    name = "detsky_mir"
    domain = "detmir.ru"
    origin = "https://www.detmir.ru"
    api_url = "https://api.detmir.ru/v2/products"
    product_path = r"/product/index/id/[0-9]+/?"
    search_path = "/search/results/"
    search_parameter = "qt"
    search_options = {
        "wait_selector": 'a[href*="/product/index/id/"]', "wait_ms": 3000,
        "capture_json_patterns": ["api.detmir.ru/v2/products"], "timeout_s": 35,
    }
    card_options = {
        "wait_selector": 'h1, [itemprop="price"]', "wait_ms": 3000,
        "capture_json_patterns": ["api.detmir.ru/v2/products"], "timeout_s": 35,
    }
    tile_selector = '[data-testid="product-card"], [data-test="product-card"], [data-product-id], article'
    link_selector = 'a[href*="/product/index/id/"]'
    title_selectors = ('[data-test="product-title"]', '[class*="product-card__title"]') + (
        _CdpMarketplaceConnector.title_selectors
    )
    price_selectors = ('[data-testid="product-price"]', '[data-test="price"]', '[class*="product-card__price"]') + (
        _CdpMarketplaceConnector.price_selectors
    )
    old_price_selectors = ('[data-test="old-price"]', '[class*="product-card__old-price"]') + (
        _CdpMarketplaceConnector.old_price_selectors
    )

    def _card_url(self, value):
        value = value.strip()
        if re.fullmatch(r"[0-9]+", value):
            value = f"/product/index/id/{value}/"
        return self._product_url(value)

    @staticmethod
    def _stock(item):
        available = item.get("available")
        if not isinstance(available, dict):
            return _stock(available)
        codes = []
        for channel, key in (("online", "warehouse_codes"), ("offline", "region_iso_codes")):
            group = available.get(channel)
            if isinstance(group, dict) and isinstance(group.get(key), list):
                codes.append(group[key])
        if any(_text(code) for group in codes for code in group):
            return "in_stock"
        return "out_of_stock" if codes and all(not group for group in codes) else "unknown"

    def _api_listing(self, item):
        if not isinstance(item, dict):
            return None
        sku = _text(item.get("id"))
        title = _text(item.get("title"))
        if not sku or not title or not (expected := self._card_url(sku)):
            return None
        link = item.get("link")
        url = self._product_url(_text(link.get("web_url"))) if isinstance(link, dict) else None
        if not url or not self._same_card(url, expected):
            url = expected
        return _Listing(
            url, title, sku=sku, brand=_named(item.get("brand")), seller=_named(item.get("vendor") or item.get("seller")),
            price=item.get("price"), original_price=item.get("old_price"), availability=self._stock(item),
            rating=_rating(item.get("rating")), reviews_count=_count(item.get("review_count")),
            image_url=_image(item.get("pictures")), specifications=_specifications(item.get("characteristics")),
        )

    def _api_listings(self, data, *, is_card=False):
        if isinstance(data, dict):
            data = data.get("items", data.get("data", data))
        if is_card and isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise AccessError("invalid_api_payload")
        rows = [row for item in data if (row := self._api_listing(item))]
        if data and not rows:
            raise AccessError("invalid_api_payload")
        return rows

    async def _api(self, url, params, *, card_url=None):
        response = await self.transport.http_get(url, params=params, headers={"Accept": "application/json"})
        if not response.is_success:
            raise AccessError(f"http_{response.status_code}")
        try:
            data = response.json()
        except (ValueError, RecursionError):
            raise AccessError("invalid_api_json") from None
        rows = self._api_listings(data, is_card=bool(card_url))
        if card_url:
            rows = [row for row in rows if self._same_card(row.url, card_url)]
            if not rows:
                raise AccessError("product_not_found")
        return rows, response.status_code

    def _structured(self, soup, capture, card_url):
        for data in _captured(capture):
            try:
                yield from self._api_listings(data, is_card=bool(card_url))
            except AccessError:
                continue
        yield from super()._structured(soup, capture, card_url)

    def _dom_details(self, node, listing, *, is_card):
        listing.sku = urlsplit(listing.url).path.rstrip("/").rsplit("/", 1)[-1]

    async def search(self, query, *, model=None, limit=12, city=None):
        started = monotonic()
        params = {
            "filter": f"site:detmir;search:{query};region.iso:RU-MOW",
            "limit": min(30, max(limit * 2, 12)), "offset": 0, "meta": "*",
        }
        try:
            listings, status = await self._api(self.api_url, params)
        except (AccessError, TimeoutError) as exc:
            url = self.origin + self.search_path + "?" + urlencode({self.search_parameter: query})
            listings, status, error = await self._capture(url)
            return self._search_result(
                started, model=model, limit=limit, listings=listings, status=status, error=error,
                fallback_used=True, warnings=["api_" + (str(exc) or "timeout")],
            )
        return self._search_result(started, model=model, limit=limit, listings=listings, status=status)

    async def get_card(self, url_or_sku, *, model=None):
        card_url = self._card_url(url_or_sku)
        if card_url:
            sku = urlsplit(card_url).path.rstrip("/").rsplit("/", 1)[-1]
            try:
                listings, status = await self._api(f"{self.api_url}/{sku}", {"meta": "*"}, card_url=card_url)
            except (AccessError, TimeoutError):
                pass
            else:
                return self._card_result(listings, model, status)
        return await super().get_card(url_or_sku, model=model)
