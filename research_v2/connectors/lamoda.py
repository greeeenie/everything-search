"""Lamoda listings and product cards from public state and markup."""

import json
import re
from urllib.parse import urlsplit

from .dns import (
    _CdpMarketplaceConnector,
    _Listing,
    _captured,
    _count,
    _image,
    _json_script,
    _named,
    _objects,
    _rating,
    _stock,
    _text,
    _value,
)


def _nuxt_data(table):
    """Read Nuxt's flattened JSON table without executing its page scripts."""
    if not isinstance(table, list) or not table:
        return table
    cache = {}
    active = set()

    def hydrate(index):
        if type(index) is not int or not 0 <= index < len(table) or index in active:
            return None
        if index in cache:
            return cache[index]
        active.add(index)
        value = table[index]
        if isinstance(value, dict):
            value = {key: hydrate(ref) for key, ref in value.items()}
        elif isinstance(value, list):
            if len(value) == 2 and value[0] in ("Reactive", "ShallowReactive", "Ref", "ShallowRef"):
                value = hydrate(value[1])
            else:
                value = [hydrate(ref) for ref in value]
        active.remove(index)
        cache[index] = value
        return value

    try:
        return hydrate(0)
    except RecursionError:
        return None


def _payloads(soup):
    for script in soup.select("script:not([src])"):
        script_id = (script.get("id") or "").lower()
        if script_id in {"__nuxt__", "__nuxt_data__"} or "payload" in script_id or script.has_attr("data-nuxt-data"):
            data = _json_script(script)
            if data is not None:
                yield _nuxt_data(data) if script_id == "__nuxt_data__" or script.has_attr("data-nuxt-data") else data
                continue
        code = script.string or ""
        assignment = re.match(r"\s*(?:window\.)?__NUXT__\s*=\s*", code)
        if assignment:
            try:
                yield json.JSONDecoder().raw_decode(code, assignment.end())[0]
            except (ValueError, RecursionError):
                continue


class LamodaConnector(_CdpMarketplaceConnector):
    name = "lamoda"
    domain = "lamoda.ru"
    origin = "https://www.lamoda.ru"
    product_path = r"/p/[A-Za-z0-9_-]+(?:/[^/]+)*/?"
    search_path = "/catalogsearch/result/"
    search_options = {
        "wait_selector": 'a[href*="/p/"], .x-product-card__card',
        "wait_ms": 3500, "wait_for_challenge": True, "scroll_steps": 1,
        "capture_json_patterns": ["catalog", "search", "graphql"], "timeout_s": 40,
    }
    card_options = {
        "wait_selector": "h1, .x-product-card-description__price-single", "timeout_s": 40,
    }
    tile_selector = ".x-product-card__card"
    link_selector = 'a[href*="/p/"]'
    title_selectors = (".x-product-card-description__product-name",) + _CdpMarketplaceConnector.title_selectors
    brand_selectors = (
        ".x-product-card-description__brand-name", ".x-premium-product-title__brand-name", ".product-title__brand-name",
    ) + _CdpMarketplaceConnector.brand_selectors
    price_selectors = (
        ".x-product-card-description__price-new", ".x-product-card-description__price-single",
        ".x-premium-product-prices__price-new", ".x-premium-product-prices__price-single",
        ".product-price__price-new", ".product-price__price-single",
    ) + _CdpMarketplaceConnector.price_selectors
    old_price_selectors = (
        ".x-product-card-description__price-old", ".x-premium-product-prices__price-old", ".product-price__price-old",
    ) + _CdpMarketplaceConnector.old_price_selectors
    buy_selector = 'button[data-test="add-to-cart"], button[data-testid="add-to-cart"]'

    def _card_url(self, value):
        value = value.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", value):
            value = f"/p/{value}/"
        return self._product_url(value)

    @staticmethod
    def _path_sku(url):
        return urlsplit(url).path.split("/")[2]

    def _same_card(self, url, card_url):
        return self._path_sku(url).casefold() == self._path_sku(card_url).casefold()

    def _listing_key(self, url):
        return self._path_sku(url).casefold()

    def _state_listings(self, data):
        for item in _objects(data):
            sku = _text(item.get("sku"))
            title = _text(item.get("name"))
            if not sku or not title or not any(key in item for key in ("price_amount", "old_price_amount", "is_available")):
                continue
            source_url = _text(item.get("url") or item.get("seo_tail"))
            url = self._card_url(sku) or self._product_url(source_url)
            if not url:
                continue
            yield _Listing(
                url, title, sku=sku, brand=_named(item.get("brand")), price=item.get("price_amount"),
                original_price=item.get("old_price_amount"), availability=_stock(item.get("is_available")),
                rating=_rating(item.get("rating")), reviews_count=_count(item.get("review_count")),
                image_url=_image(item.get("image") or item.get("images")),
            )

    def _structured(self, soup, capture, card_url):
        if card_url:
            yield from super()._structured(soup, capture, card_url)
        for data in _payloads(soup):
            yield from self._state_listings(data)
        for data in _captured(capture):
            yield from self._state_listings(data)
        if not card_url:
            yield from super()._structured(soup, capture, card_url)

    def _parse(self, capture, soup, card_url):
        rows = super()._parse(capture, soup, card_url)
        for row in rows:
            sku = self._path_sku(row.url)
            row.url = self.origin + f"/p/{sku}/"
        return rows

    def _dom_details(self, node, listing, *, is_card):
        listing.sku = _text(node.get("data-sku")) or self._path_sku(listing.url)
        if is_card:
            listing.brand = listing.brand or _value(node, ('[data-test="product-brand"]',))
            listing.sizes = list(dict.fromkeys(
                value for size in node.select('[data-size], [data-test="size-option"], .x-size-list__item, [itemprop="size"]')
                if (value := _text(size.get("data-size") or size.get("content") or size.get_text(" ", strip=True)))
            ))
