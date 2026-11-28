"""Yandex Market public offers, Pay prices, and buyer reviews."""

import json
import re
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..shopping import YandexMarketAdapter, has_type
from .base import ConnectorCardResult, MarketplaceConnector, make_product_and_offer
from .wildberries import (
    attr_text,
    availability,
    card_result,
    count,
    fetch_capture,
    id_text,
    image_url,
    json_objects,
    json_value,
    label,
    mapping,
    market_url,
    merge_listings,
    observed_stock,
    rating,
    review_record,
    rows,
    rub,
    schema_listing,
    schema_products,
    search_result,
    source_error,
    specifications,
    unique_reviews,
)

_ADAPTER = YandexMarketAdapter()
_ORIGIN = "https://market.yandex.ru"
_PATH = r"^/(?:product(?:--[^/]+)?/\d+|card/[^/]+/\d+|offer/[^/]+)/?$"
_PAY_LABEL = re.compile(r"яндекс\s*(?:пэй|пей|плюс)|yandex\s*(?:pay|plus)|(?:с|по)\s+(?:(?:картой|карте)\s+)?п[эе]й", re.I)
_REGULAR_LABEL = re.compile(r"без\s+(?:карты\s+)?(?:яндекс\s+)?(?:п[эе]й|плюса)|обычная цена", re.I)
_PAY_SELECTOR = (
    '[data-auto="snippet-price-green"], [data-auto="snippet-price-with-plus"], '
    '[data-auto="snippet-price-with-pay"], [data-auto="green-price"], [data-auto="ya-bank-price"], '
    '[data-auto="price-with-plus"], [data-auto="price-with-pay"], [data-auto="price-with-card"]'
)


def _url(value):
    if sku := id_text(value):
        return f"{_ORIGIN}/card/x/{sku}"
    target = market_url(value, _ORIGIN, _ADAPTER.domain, _PATH, keep_query=True)
    if not target:
        return None
    parsed = urlsplit(target)
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query) if k not in {"cpc", "sponsored", "ogV"}])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _sku(url):
    path = urlsplit(url).path.rstrip("/")
    return id_text(path.rsplit("/", 1)[-1])


def _state_payloads(capture):
    payloads = list(capture.payloads)
    for tag in capture.soup.select('noframes[data-apiary="patch"], script[type="application/json"], script[data-apiary]'):
        text = (tag.string or tag.get_text()).strip()
        if (payload := json_value(text)) is not None:
            payloads.append(payload)
            continue
        # Some patches wrap a JSON object in an assignment or a callback.
        if match := re.match(r"^[\w.$\[\]'\"]+[\s=.(]*(?=[{\[])", text):
            try:
                payload, _ = json.JSONDecoder().raw_decode(text[match.end():])
                payloads.append(payload)
            except (ValueError, RecursionError):
                pass
    return payloads


def _prices(row):
    price = mapping(row.get("price"))
    conditional = rub(
        row.get("greenPrice") or row.get("yaBankPrice") or row.get("priceWithPlus")
        or row.get("price_with_plus") or row.get("yandexPayPrice") or price.get("greenPrice") or price.get("yaBankPrice")
    )
    public = rub(row.get("regularPrice") or row.get("priceWithoutPlus") or price.get("regular")
                 or price.get("value") or row.get("price"))
    if conditional is not None and public == conditional:
        public = None
    return public, conditional, rub(row.get("originalPrice") or row.get("oldPrice") or price.get("oldPrice"))


def _dom_prices(tile, default=None):
    current = tile.select_one('[data-auto="snippet-price-current"], [data-auto="price-value"], [data-auto="price"]')
    old = tile.select_one('[data-auto="snippet-price-old"], [data-auto="old-price"], del, s')
    current_price = rub(current.get_text(" ", strip=True)) if current else default
    old_price = rub(old.get_text(" ", strip=True)) if old else None
    regular = rub(attr_text(tile, '[data-auto="snippet-price-regular"], [data-auto="price-without-card"], [data-auto="regular-price"]'))
    conditional = rub(attr_text(tile, _PAY_SELECTOR))
    context = current.get_text(" ", strip=True) if current else ""
    if current:
        context += " " + current.get("aria-label", "") + " " + current.get("title", "")
    current_is_conditional = bool(_PAY_LABEL.search(context) and not _REGULAR_LABEL.search(context))
    if not current_is_conditional and not conditional:
        price_box = current.parent if current else None
        nearby = price_box.get_text(" ", strip=True) if price_box else ""
        current_is_conditional = bool(current_price and _PAY_LABEL.search(nearby) and not _REGULAR_LABEL.search(nearby))
    if current_is_conditional:
        conditional = conditional or current_price
        if old and old.get("data-auto") == "snippet-price-old" and regular is None:
            # The paired price in a Pay-labelled snippet is what the user pays without Pay.
            old_context = old.get("aria-label", "") + " " + old.get_text(" ", strip=True)
            if _REGULAR_LABEL.search(old_context) or (old.name not in {"s", "del"} and not old.select_one("s, del")):
                regular, old_price = old_price, None
    elif current_price != conditional:
        regular = regular or current_price
    return regular, conditional, old_price


def _state_listing(node, url, title, model, status):
    price, conditional, original = _prices(node)
    score = mapping(node.get("rating"))
    reviews = node.get("opinions", node.get("ratingCount", node.get("reviewsCount")))
    return make_product_and_offer(
        "yandex_market", url, title, price, model=model, brand=label(node.get("brand") or node.get("vendor")),
        seller=label(node.get("seller") or node.get("shop")),
        availability=availability(node.get("isAvailable", node.get("available", node.get("availability")))),
        conditional_price=conditional, original_price=original,
        price_conditions=["yandex_pay"] if conditional else [], sku=_sku(url),
        rating=rating(node.get("rating")) or rating(score.get("value")),
        reviews_count=count(reviews) if not isinstance(reviews, dict) else count(reviews.get("count")),
        image_url=image_url(node.get("image") or node.get("pictures"), url),
        specifications=specifications(node.get("specifications") or node.get("characteristics")),
        http_status=status,
    )


def _dom_search(capture, model):
    # The adapter owns the canonical DOM selectors and excludes tracking-only parameters.
    tiles = []
    for tile in capture.soup.select('[data-auto="searchOrganic"]'):
        title = tile.select_one('[data-auto="snippet-title"]')
        if not title:
            continue
        anchor = title if title.name == "a" else title.find_parent("a") or title.find("a")
        if not anchor or not (url := _url(anchor.get("href"))):
            continue
        tiles.append((url, title.get_text(" ", strip=True), tile, _dom_prices(tile)))
    adapter_rows = {}
    for node in _ADAPTER.public_products(capture.soup, capture.url):
        raw = mapping(node.get("offers"))
        if url := _url(raw.get("url")):
            adapter_rows[url] = raw
    listings = []
    for url, title, tile, (regular, conditional, original) in tiles:
        if not title:
            continue
        raw = adapter_rows.get(url, {})
        if regular is None and conditional is None:
            regular = rub(raw.get("price"))
        stock = observed_stock(tile)
        if stock == "unknown":
            stock = availability(raw.get("availability"))
        listings.append(make_product_and_offer(
            "yandex_market", url, title, regular, model=model, sku=_sku(url), availability=stock,
            seller=label(raw.get("seller")) or attr_text(tile, '[data-zone-name="shopInShop"]'),
            conditional_price=conditional, original_price=original, price_conditions=["yandex_pay"] if conditional else [],
            rating=rating(attr_text(tile, '[data-auto="snippet-rating"], [data-auto="rating-value"], [itemprop="ratingValue"]')),
            reviews_count=count(attr_text(tile, '[data-auto="snippet-review-count"], [data-auto="rating-count"], [itemprop="reviewCount"]')),
            image_url=image_url(attr_text(tile, "img", "src"), url), http_status=capture.http_status,
        ))
    return listings


def _search_listings(connector, capture, model):
    if capture.error:
        return []
    states = list(json_objects(_state_payloads(capture)))
    structured = []
    dom = _dom_search(capture, model)
    dom_by_url = {offer.url: offer for _, offer in dom}
    dom_by_sku = {}
    for _, offer in dom:
        dom_by_sku.setdefault(offer.sku, []).append(offer)
    for node in states:
        title = label(node.get("title") or node.get("name"))
        if not any(_prices(node)) and node.get("rating") is None and node.get("opinions") is None:
            continue
        sku = id_text(node.get("productId") or node.get("marketSku") or node.get("id"))
        url = _url(node.get("productUrl") or node.get("url") or node.get("link"))
        if not url and sku:
            candidates = dom_by_sku.get(sku, [])
            seller = label(node.get("seller") or node.get("shop"))
            if seller:
                candidates = [candidate for candidate in candidates if candidate.seller == seller]
            if len(candidates) == 1:
                url = candidates[0].url
            elif not dom_by_sku.get(sku):
                url = _url(sku)
        if url and (title := title or (dom_by_url[url].title if url in dom_by_url else None)):
            structured.append(_state_listing(node, url, title, model, capture.http_status))
    schema = []
    for node in schema_products(capture.schema):
        raw = next(iter(rows(node.get("offers"))), {})
        if label(node.get("name")) and (url := _url(node.get("url") or raw.get("url"))):
            listing = schema_listing(connector, node, url, model, http_status=capture.http_status)
            schema.append(listing)
    # A displayed Pay label can qualify a bare JSON-LD price.
    listings = merge_listings(structured, schema, key=lambda offer: offer.url)
    return merge_listings(listings, dom, key=lambda offer: offer.url, clarify_price=True)


def _buyer_reviews(capture, payloads, url, model):
    reviews = []
    card_id = _sku(url)
    for node in json_objects([payloads, capture.schema]):
        if not has_type(node, "Review") and not (
            any(key in node for key in ("pros", "cons", "comment"))
            and any(key in node for key in ("grade", "created"))
        ):
            continue
        reviewed_id = id_text(node.get("productId") or node.get("marketSku"))
        if card_id and reviewed_id and reviewed_id != card_id:
            continue
        reviews.append(review_record(
            "yandex_market", url, model, text=node.get("comment") or node.get("reviewBody"), pros=node.get("pros"),
            cons=node.get("cons"), stars=node.get("grade", node.get("reviewRating")),
            published_at=node.get("created", node.get("datePublished")),
        ))
    for node in capture.soup.select('[data-auto="review-item"]'):
        reviews.append(review_record(
            "yandex_market", url, model,
            text=attr_text(node, '[data-auto="review-comment"], [itemprop="reviewBody"]') or node.get_text(" ", strip=True),
            pros=attr_text(node, '[data-auto="review-pros"]'), cons=attr_text(node, '[data-auto="review-cons"]'),
            stars=attr_text(node, '[data-auto="review-rating"], [itemprop="ratingValue"]'),
            published_at=attr_text(node, 'time, [itemprop="datePublished"]', "datetime"),
        ))
    return unique_reviews(reviews)


def _card_listings(connector, capture, url, model):
    if capture.error:
        return [], []
    payloads = _state_payloads(capture)
    schema = next((node for node in schema_products(capture.schema) if label(node.get("name")) and (
        not (node_url := _url(node.get("url"))) or node_url == url or (_sku(url) and _sku(node_url) == _sku(url))
    )), {})
    title = label(schema.get("name")) or attr_text(capture.soup, "h1")
    matching_states = []
    card_id = _sku(url)
    for node in json_objects(payloads):
        candidate = _url(node.get("productUrl") or node.get("url"))
        identifier = id_text(node.get("productId") or node.get("marketSku") or node.get("id"))
        if candidate == url or (card_id and (identifier == card_id or (candidate and _sku(candidate) == card_id))):
            matching_states.append(node)
            title = title or label(node.get("title") or node.get("name"))
    if not title:
        return [], []
    structured = [_state_listing(node, url, title, model, capture.http_status) for node in matching_states]
    # Fold related fragments for this exact product before adding less specific page data.
    listings = []
    for listing in structured:
        listings = merge_listings(listings, [listing])
    regular, conditional, original = _dom_prices(capture.soup)
    props = {}
    for row in capture.soup.select('[data-auto="product-specifications"] dl, [data-auto="specification"]'):
        for key in row.select("dt"):
            if value := key.find_next_sibling("dd"):
                props[key.get_text(" ", strip=True)] = value.get_text(" ", strip=True)
    dom = make_product_and_offer(
        "yandex_market", url, title, regular, model=model, sku=card_id,
        brand=attr_text(capture.soup, '[data-auto="brand"], [itemprop="brand"]'),
        seller=attr_text(capture.soup, '[data-zone-name="shopInShop"], [data-auto="seller-name"]'),
        availability=observed_stock(capture.soup), conditional_price=conditional, original_price=original,
        price_conditions=["yandex_pay"] if conditional else [], specifications=props,
        rating=rating(attr_text(capture.soup, '[data-auto="rating-value"], [itemprop="ratingValue"]')),
        reviews_count=count(attr_text(capture.soup, '[data-auto="rating-count"], [itemprop="reviewCount"]')),
        http_status=capture.http_status,
    )
    listings = merge_listings(listings, [dom], clarify_price=True)
    if schema:
        listings = merge_listings(listings, [schema_listing(connector, schema, url, model, http_status=capture.http_status)])
    return listings, _buyer_reviews(capture, payloads, url, model)


class YandexMarketConnector(MarketplaceConnector):
    name = "yandex_market"
    domain = "market.yandex.ru"
    supports_search = True
    supports_reviews = True

    async def search(self, query, *, model=None, limit=12, city=None):
        start = monotonic()
        url = _ADAPTER.search_url(query)
        capture = await fetch_capture(self.transport.http_get, url, region="RU")
        listings = _search_listings(self, capture, model)
        if listings:
            return search_result(self, listings, model, limit, start)
        primary = capture.error
        capture = await fetch_capture(self.transport.cdp_fetch, url, wait_selector='[data-auto="searchOrganic"], article',
                                      wait_ms=2500, scroll_steps=1, timeout_s=35)
        return search_result(self, _search_listings(self, capture, model), model, limit, start,
                             error=capture.error, previous=primary, fallback=True)

    async def get_card(self, url_or_sku, *, model=None):
        url = _url(url_or_sku)
        if not url:
            return ConnectorCardResult(error="invalid_card_url_or_sku")
        capture = await fetch_capture(self.transport.http_get, url, region="RU")
        listings, reviews = _card_listings(self, capture, url, model)
        if not listings:
            capture = await fetch_capture(self.transport.cdp_fetch, url, wait_selector="h1", timeout_s=35)
            listings, reviews = _card_listings(self, capture, url, model)
        return card_result(listings, model, error=capture.error, reviews=reviews)

    async def get_reviews(self, model, *, limit=10):
        if limit <= 0:
            return [], []
        result = await self.search(model, model=model, limit=2)
        if not result.offers:
            return [], result.errors or [source_error(self.name, "matching_listing_unavailable", "search")]
        card = await self.get_card(result.offers[0].url, model=model)
        return card.reviews[:limit], [source_error(self.name, card.error, "card")] if card.error else []
