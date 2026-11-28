"""Avito public classified listings and item detail pages."""

import re
from time import monotonic
from urllib.parse import urlsplit

from ..shopping import AvitoAdapter
from .base import ConnectorCardResult, MarketplaceConnector, make_product_and_offer
from .wildberries import (
    attr_text,
    availability,
    card_result,
    fetch_capture,
    id_text,
    image_url,
    json_objects,
    label,
    mapping,
    market_url,
    merge_listings,
    observed_stock,
    rows,
    rub,
    schema_listing,
    schema_products,
    search_result,
    specifications,
)

_ORIGIN = "https://www.avito.ru"
_PATH = r"(?:^/|_)[0-9]{1,20}/?$"
_PATTERNS = ["/web/1/js/items"]
_SELECTOR = '[data-marker="item"], [itemscope][itemtype$="/Product"]'
_ADAPTER = AvitoAdapter()
_INACTIVE = {"inactive", "archived", "closed", "removed", "sold", "expired", "reserved", "blocked"}


def _url(value):
    if sku := id_text(value):
        return f"{_ORIGIN}/{sku}"
    return market_url(value, _ORIGIN, "avito.ru", _PATH)


def _sku(url):
    match = re.search(r"(\d+)/?$", urlsplit(url).path)
    return match[1] if match else None


def _inactive(item):
    state = label(item.get("status"))
    return (
        item.get("isReserved") in (True, "true") or item.get("isActive") in (False, "false")
        or item.get("isArchived") is True or item.get("isSold") is True
        or bool(state and state.casefold() in _INACTIVE)
        or availability(item.get("availability")) == "out_of_stock"
    )


def _captured(capture, model):
    listings = []
    excluded = set()
    for node in json_objects(capture.payloads):
        if not isinstance(node.get("items"), list):
            continue
        for item in rows(node["items"]):
            identifier = id_text(item.get("id"))
            url = _url(item.get("urlPath") or item.get("url") or identifier)
            if not url:
                continue
            sku = identifier or _sku(url)
            if _inactive(item):
                excluded.add(sku)
                continue
            if listing := _item_listing(item, url, model, capture.http_status, search=True):
                listings.append(listing)
    return listings, excluded


def _item_listing(item, url, model, status, *, search=False):
    title = label(item.get("title") or item.get("name"))
    if not title:
        return None
    detailed = mapping(item.get("priceDetailed"))
    currency = label(item.get("currency") or item.get("priceCurrency"))
    price = rub(detailed or item.get("price")) if not currency or currency.upper() in {"RUB", "RUR", "₽"} else None
    location = label(item.get("location") or item.get("address"))
    seller = label(item.get("seller") or item.get("sellerName") or item.get("user"))
    stock = availability(item.get("isActive", item.get("availability")))
    if _inactive(item):
        stock = "out_of_stock"
    elif stock == "unknown" and search:
        stock = "in_stock"
    product, offer = make_product_and_offer(
        "avito", url, title, price, model=model, sku=_sku(url), seller=seller,
        condition=label(item.get("condition") or item.get("itemCondition")),
        default_condition="unknown", availability=stock,
        image_url=image_url(item.get("images") or item.get("image"), url),
        specifications=specifications(item.get("params") or item.get("properties")), http_status=status,
    )
    if location:
        offer.delivery["listing_location"] = location
    if search and stock == "in_stock":
        offer.evidence.excerpts.append("Active classified listing returned by the items endpoint")
    return product, offer


def _dom_listing(tile, url, title, model, status, *, raw=None, search=False):
    raw = raw or {}
    stock = availability(raw.get("availability"))
    if stock == "unknown":
        stock = observed_stock(tile)
    if tile.select_one('[data-marker*="reserved"], [data-marker*="closed"]') or re.search(
        r"\b(?:зарезервировано|в резерве|объявление снято)\b", tile.get_text(" ", strip=True), re.I,
    ):
        stock = "out_of_stock"
    currency = label(raw.get("priceCurrency")) or attr_text(tile, '[itemprop="priceCurrency"]')
    if currency and currency.upper() not in {"RUB", "RUR", "₽"}:
        price = None
    elif currency:
        price = rub(raw.get("price") or attr_text(tile, '[itemprop="price"]'))
    else:
        price = None
    if price is None and not currency:
        price = rub(attr_text(tile, '[data-marker="item-price"], [data-marker="item-view/item-price"], [itemprop="price"]'),
                    require_currency=True)
    inferred_stock = stock == "unknown" and bool(price) and search
    props = {}
    for value in tile.select('[data-marker="item-view/item-params"] li, [data-marker="item-view/item-params"] [data-marker*="params-item"]'):
        name, separator, data = value.get_text(" ", strip=True).partition(":")
        if separator and name.strip() and data.strip():
            props[name.strip()] = data.strip()
    condition = label(raw.get("itemCondition")) or attr_text(tile, '[itemprop="itemCondition"]', "href") or props.get("Состояние")
    product, offer = make_product_and_offer(
        "avito", url, title, price, model=model, sku=_sku(url), default_condition="unknown",
        availability="in_stock" if inferred_stock else stock, condition=condition, specifications=props,
        seller=label(raw.get("seller")) or attr_text(tile, '[data-marker="seller-info/name"], [data-marker="item-view/seller-name"], a[href^="/brands/"]'),
        image_url=image_url(attr_text(tile, "img", "src"), url), http_status=status,
    )
    if inferred_stock:
        offer.evidence.excerpts.append("Active classified listing with a displayed price")
    if location := attr_text(tile, '[data-marker="item-location"], [data-marker="item-view/item-address"], [itemprop="address"]'):
        offer.delivery["listing_location"] = location
    if delivery := attr_text(tile, '[data-marker*="delivery-badge"], [data-marker="item-view/delivery"], [data-marker="item-delivery"]'):
        offer.delivery["badge"] = delivery
    return product, offer


def _search_listings(capture, model):
    if capture.error:
        return []
    structured, excluded = _captured(capture, model)
    adapter_rows = {}
    for node in _ADAPTER.public_products(capture.soup, capture.url):
        raw = mapping(node.get("offers"))
        if url := _url(raw.get("url")):
            adapter_rows[url] = raw
    dom = []
    for tile in capture.soup.select(_SELECTOR):
        anchor = tile.select_one('a[data-marker="item-title"], a[itemprop="url"], a[href*="_"]')
        if not anchor or not (url := _url(anchor.get("href"))):
            continue
        title = attr_text(tile, '[itemprop="name"]') or label(anchor.get("title") or anchor.get_text(" ", strip=True))
        if not title:
            continue
        listing = _dom_listing(tile, url, title, model, capture.http_status, raw=adapter_rows.get(url), search=True)
        if listing[1].availability == "out_of_stock":
            excluded.add(listing[1].sku)
        dom.append(listing)
    return [(product, offer) for product, offer in merge_listings(structured, dom, key=lambda offer: offer.sku)
            if offer.sku not in excluded]


def _card_listings(connector, capture, url, model):
    if capture.error:
        return []
    sku = _sku(url)
    target = _url(capture.url)
    if target and _sku(target) == sku:
        url = target
    schema = next((node for node in schema_products(capture.schema) if label(node.get("name")) and (
        not (candidate := _url(node.get("url"))) or _sku(candidate) == sku
    )), {})
    structured = []
    for node in json_objects(capture.payloads):
        if id_text(node.get("id")) == sku and (
            node.get("priceDetailed") or node.get("price") or node.get("isActive") is not None or _inactive(node)
        ):
            if listing := _item_listing(node, url, model, capture.http_status):
                structured.append(listing)
                break
    title = label(schema.get("name")) or attr_text(capture.soup, 'h1, [data-marker="item-view/title-info"]')
    if not title and not structured:
        return []
    if schema:
        structured = merge_listings(structured, [
            schema_listing(connector, schema, url, model, http_status=capture.http_status, buyer_rating=False),
        ])
    if title:
        dom = _dom_listing(capture.soup, url, title, model, capture.http_status)
        structured = merge_listings(structured, [dom])
        if dom[1].availability == "out_of_stock":
            structured[0][1].availability = "out_of_stock"
    return structured


class AvitoConnector(MarketplaceConnector):
    name = "avito"
    domain = "avito.ru"
    supports_search = True
    supports_reviews = False

    async def search(self, query, *, model=None, limit=12, city=None):
        start = monotonic()
        url = _ADAPTER.search_url(query)
        capture = await fetch_capture(self.transport.cdp_fetch, url, wait_selector=_SELECTOR,
                                      wait_ms=3000, scroll_steps=1, capture_json_patterns=_PATTERNS, timeout_s=35)
        listings = _search_listings(capture, model)
        if listings:
            return search_result(self, listings, model, limit, start)
        primary = capture.error
        capture = await fetch_capture(self.transport.camoufox_fetch, url, region="RU", allow_search=True,
                                      capture_json_patterns=_PATTERNS)
        return search_result(self, _search_listings(capture, model), model, limit, start,
                             error=capture.error, previous=primary, fallback=True)

    async def get_card(self, url_or_sku, *, model=None):
        url = _url(url_or_sku)
        if not url:
            return ConnectorCardResult(error="invalid_card_url_or_sku")
        capture = await fetch_capture(self.transport.cdp_fetch, url, wait_selector="h1",
                                      capture_json_patterns=_PATTERNS, timeout_s=35)
        listings = _card_listings(self, capture, url, model)
        if listings:
            return card_result(listings, model)
        capture = await fetch_capture(self.transport.camoufox_fetch, url, region="RU", allow_search=False,
                                      capture_json_patterns=_PATTERNS)
        return card_result(_card_listings(self, capture, url, model), model, error=capture.error)
