"""Megamarket listings from catalog responses and product markup."""

import re
from urllib.parse import urlsplit

from .dns import (
    _CdpMarketplaceConnector,
    _Listing,
    _captured,
    _count,
    _image,
    _named,
    _objects,
    _rating,
    _stock,
    _text,
    _value,
)


class MegamarketConnector(_CdpMarketplaceConnector):
    name = "megamarket"
    domain = "megamarket.ru"
    origin = "https://megamarket.ru"
    product_path = r"/catalog/details/[^/]+/?"
    search_path = "/catalog/"
    search_options = {
        "wait_selector": '[data-test="product-item"], a[href*="/catalog/details/"]',
        "wait_ms": 3500, "wait_for_challenge": True, "scroll_steps": 1,
        "capture_json_patterns": ["catalogService/catalog/search", "search"], "timeout_s": 40,
    }
    card_options = {
        "wait_selector": 'h1, [data-test="price"]',
        "capture_json_patterns": ["catalogService", "goodsService", "merchant"], "timeout_s": 40,
    }
    tile_selector = '[data-test="product-item"], .catalog-item'
    link_selector = 'a[href*="/catalog/details/"]'
    title_selectors = ('[data-test="product-name"]', ".catalog-item__name", ".catalog-item__title") + (
        _CdpMarketplaceConnector.title_selectors
    )
    price_selectors = ('[data-test="price"]', '[data-test="product-price"]', ".item-price", ".catalog-item__price") + (
        _CdpMarketplaceConnector.price_selectors
    )
    old_price_selectors = ('[data-test="old-price"]', ".item-price-old", ".catalog-item__old-price") + (
        _CdpMarketplaceConnector.old_price_selectors
    )
    rating_selectors = ('[data-test="rating"]', ".catalog-item__rating") + _CdpMarketplaceConnector.rating_selectors
    review_selectors = ('[data-test="review-count"]', ".catalog-item__reviews") + _CdpMarketplaceConnector.review_selectors
    seller_selectors = ('[data-test="seller"]', ".catalog-item__merchant", ".merchant-name") + (
        _CdpMarketplaceConnector.seller_selectors
    )

    def _card_url(self, value):
        value = value.strip()
        if re.fullmatch(r"[0-9]+", value):
            value = f"/catalog/details/{value}/"
        return self._product_url(value)

    def _same_card(self, url, card_url):
        path = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        other = urlsplit(card_url).path.rstrip("/").rsplit("/", 1)[-1]
        if path == other:
            return True
        id_left = re.search(r"(?:^|-)([0-9]+)$", path)
        id_right = re.search(r"(?:^|-)([0-9]+)$", other)
        return bool(id_left and id_right and id_left[1] == id_right[1])

    def _structured(self, soup, capture, card_url):
        for data in _captured(capture):
            for item in _objects(data):
                goods = item.get("goods")
                if not isinstance(goods, dict) or not (title := _text(goods.get("title"))):
                    continue
                sku = _text(goods.get("goodsId"))
                url = _text(goods.get("webUrl")) or (self._card_url(sku) if sku else None)
                if not url:
                    continue
                final_price = item.get("finalPrice")
                declared_price = item.get("price")
                yield _Listing(
                    url, title, sku=sku, brand=_named(goods.get("brand")), image_url=_image(goods.get("image")),
                    price=final_price if final_price is not None else declared_price,
                    original_price=declared_price if final_price is not None else item.get("oldPrice"),
                    seller=_named(item.get("merchantName")),
                    availability=_stock(item.get("isAvailable", goods.get("isAvailable"))),
                    rating=_rating(item.get("rating", goods.get("rating"))),
                    reviews_count=_count(item.get("reviewCount", goods.get("reviewCount"))),
                )
        yield from super()._structured(soup, capture, card_url)

    def _dom_details(self, node, listing, *, is_card):
        if bonus := _value(node, ('[data-test*="cashback"]', '[data-test*="bonus"]', '[class*="cashback"]',
                                  '[class*="bonus"]')):
            listing.price_conditions.append(bonus)
        listing.sku = _text(node.get("data-goods-id")) or _value(node, ('[itemprop="sku"]',), "content")
