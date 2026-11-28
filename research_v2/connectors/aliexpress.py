"""AliExpress Russia catalog and listing details."""

import re
from time import monotonic
from urllib.parse import parse_qs, urlencode, urlsplit

from .base import ConnectorCardResult, MarketplaceConnector, make_product_and_offer
from .wildberries import (
    TILE_RUB_PRICE,
    attr_text,
    availability,
    card_result,
    count,
    dom_segments,
    fetch_capture,
    id_text,
    image_url,
    json_objects,
    label,
    market_url,
    merge_listings,
    observed_stock,
    priced_tiles,
    rating,
    rows,
    rub,
    schema_listing,
    schema_products,
    script_data,
    search_result,
    specifications,
)

_ORIGIN = "https://aliexpress.ru"
_PATH = r"^/item/([0-9]{1,20})(?:\.html)?/?$"
_PATTERNS = ["aer-jsonapi", "search"]
_SOLD_OUT = re.compile(r"товар закончился|нет в наличии|распродано|товар недоступен|sold out", re.I)
_FROM_PRICE = re.compile(r"\bот\s+$", re.I)
_ORDERS = re.compile(r"(?<!\w)(\d+(?:\s\d{3})*)\s+купили\b", re.I)
_PRODUCT_LINK = 'a[href*="/item/"]'
_DOM_BADGE = re.compile(r"в корзину|купить|распродажа|ч[её]рная пятница|\d+\s*(?:отзыв\w*|оцен\w*)", re.I)


def _sku(value):
    if sku := id_text(value):
        return sku
    target = market_url(value, _ORIGIN, "aliexpress.ru", _PATH)
    return re.fullmatch(_PATH, urlsplit(target).path)[1] if target else None


def _url(sku):
    return f"{_ORIGIN}/item/{sku}.html"


def _dom_prices(text):
    return [
        amount for match in TILE_RUB_PRICE.finditer(text)
        if not _FROM_PRICE.search(text, 0, match.start()) and (amount := rub(match[1])) is not None
    ]


def _state_items(capture):
    payloads = [*capture.payloads, *script_data(capture.soup, "script#__AER_DATA__")]
    for node in json_objects(payloads):
        sku = id_text(node.get("productId") or node.get("id"))
        link = node.get("productUrl") or node.get("url")
        if link:
            link_sku = _sku(link)
            if not link_sku or (sku and sku != link_sku):
                continue
            sku = sku or link_sku
        title = label(node.get("title") or node.get("productTitle") or node.get("name"))
        if sku and title and any(key in node for key in (
            "finalPrice", "price", "minPrice", "originalPrice", "orders", "averageStar",
            "availability", "specifications", "properties",
        )):
            yield sku, title, node


def _state_listing(sku, title, item, model, status, *, search=False):
    url = _url(sku)
    currency = item.get("priceCurrency") or item.get("currencyCode") or item.get("currency")
    if currency and str(currency).upper() not in {"RUB", "RUR", "₽", "643"}:
        price, original = None, None
    else:
        price = rub(item.get("finalPrice") or item.get("price") or item.get("minPrice"))
        original = rub(item.get("originalPrice") or item.get("oldPrice"))
    stock = availability(item.get("inStock", item.get("isAvailable", item.get("availability"))))
    quantity = count(item.get("quantity", item.get("stockQuantity")))
    if item.get("isSoldOut") is True or item.get("soldOut") is True or quantity == 0:
        stock = "out_of_stock"
    elif stock == "unknown" and quantity is not None:
        stock = "in_stock"
    inferred_stock = stock == "unknown" and search and price is not None
    product, offer = make_product_and_offer(
        "aliexpress", url, title, price, model=model, sku=sku,
        seller=label(item.get("seller") or item.get("storeName")), brand=label(item.get("brand")),
        availability="in_stock" if inferred_stock else stock, original_price=original,
        rating=rating(item.get("rating") or item.get("averageStar")),
        reviews_count=count(item.get("reviewsCount") or item.get("reviewCount")),
        image_url=image_url(item.get("image") or item.get("imageUrl") or item.get("images"), url),
        specifications=specifications(item.get("specifications") or item.get("properties")),
        http_status=status,
    )
    if inferred_stock:
        offer.evidence.excerpts.append("Active priced search tile with no sold-out indicator")
    if orders := label(item.get("tradeDesc")) or count(item.get("orders")):
        offer.evidence.excerpts.append(f"Orders={str(orders)[:150]}")
    return product, offer


def _dom_listing(tile, url, sku, title, model, status, *, search=False):
    main_price = attr_text(tile, '[data-testid="product-price"], [class*="Price_Current"], [class*="currentPrice"], ins')
    currency = attr_text(tile, '[itemprop="priceCurrency"]')
    microdata = attr_text(tile, '[itemprop="price"]')
    if main_price:
        price = min(_dom_prices(main_price), default=None) if search else rub(main_price, require_currency=True)
    elif currency and currency.upper() in {"RUB", "RUR"}:
        price = rub(microdata)
    else:
        price = rub(microdata, require_currency=True)
    text = tile.get_text(" ", strip=True)
    original = rub(attr_text(tile, 'del, s, [class*="Price_Old"]'), require_currency=True)
    stars = rating(attr_text(tile, '[itemprop="ratingValue"], [data-testid="product-rating"], [class*="ratingValue"]'))
    orders = attr_text(tile, '[data-testid="product-orders"], [class*="tradeDesc"], [class*="ordersCount"]')
    if search and (not currency or currency.upper() in {"RUB", "RUR"}):
        visible = " ".join(
            value.strip() for value in tile.strings
            if value.strip() and not any(
                parent.name in {"s", "del"} or "Price_Old" in " ".join(parent.get("class", []))
                for parent in value.parents if parent is not tile
            )
        )
        prices = _dom_prices(visible)
        if prices:
            price = min(prices)
        if price is not None:
            original = max((amount for amount in _dom_prices(text) if amount > price), default=None)
        if stars is None:
            stars = next((rating(part) for part in dom_segments(tile) if re.fullmatch(r"[0-5][.,]\d", part)), None)
        if not orders and (match := _ORDERS.search(text)) and (quantity := count(match[1])) is not None:
            orders = str(quantity)
    stock = observed_stock(tile)
    if _SOLD_OUT.search(text):
        stock = "out_of_stock"
    inferred_stock = stock == "unknown" and price is not None and search
    props = {}
    if not search:
        for dt in tile.select("dl dt"):
            if dd := dt.find_next_sibling("dd"):
                props[dt.get_text(" ", strip=True)] = dd.get_text(" ", strip=True)
        for row in tile.select('[data-testid="product-specification"], [class*="Specification_Row"]'):
            parts = [child.get_text(" ", strip=True) for child in row.find_all(recursive=False)]
            if len(parts) == 2 and all(parts):
                props[parts[0]] = parts[1]
    product, offer = make_product_and_offer(
        "aliexpress", url, title, price, model=model, sku=sku,
        availability="in_stock" if inferred_stock else stock,
        original_price=original, rating=stars,
        reviews_count=count(attr_text(tile, '[itemprop="reviewCount"]')),
        image_url=image_url(attr_text(tile, 'img', "src"), url), specifications=props, http_status=status,
    )
    if inferred_stock:
        offer.evidence.excerpts.append("Active priced search tile with no sold-out indicator")
    if orders:
        offer.evidence.excerpts.append(f"Orders={orders[:150]}")
    return product, offer


def _dom_title(text):
    return bool(text and "₽" not in text and "|" not in text and not _ORDERS.fullmatch(text)
                and not _DOM_BADGE.fullmatch(text) and not _SOLD_OUT.search(text) and re.search(r"[^\W\d_]", text))


def _search_dom(capture, model):
    listings = {}
    for sku, tile, links in priced_tiles(capture.soup, _PRODUCT_LINK, _sku, max_parents=7):
        if sku in listings:
            continue
        candidates = (text for link in links
                      for text in (label(link.get("title")), label(link.get("aria-label")), link.get_text(" ", strip=True)))
        title = next(filter(_dom_title, candidates), None) or attr_text(
            tile, 'h2, h3, [itemprop="name"], [data-testid="product-title"], [class*="ProductTitle"]',
        )
        if not title:
            title = max(filter(_dom_title, dom_segments(tile)), key=len, default=None) or attr_text(tile, "img", "alt")
        if title:
            sku_id = next((value[0] for link in links
                           if (value := parse_qs(urlsplit(link["href"]).query).get("sku_id"))), None)
            url = _url(sku) + ("?" + urlencode({"sku_id": sku_id}) if sku_id else "")
            listings[sku] = _dom_listing(tile, url, sku, title, model, capture.http_status, search=True)
    return list(listings.values())


def _schema_listings(connector, capture, model, sku=None):
    listings = []
    for node in schema_products(capture.schema):
        raw = next(iter(rows(node.get("offers"))), {})
        candidate = _sku(node.get("url") or raw.get("url") or node.get("sku"))
        if sku and candidate and sku != candidate:
            continue
        if (identifier := candidate or sku) and label(node.get("name")):
            listing = schema_listing(connector, node, _url(identifier), model, http_status=capture.http_status)
            listing[1].sku = identifier
            listings.append(listing)
    return listings


class AliExpressConnector(MarketplaceConnector):
    name = "aliexpress"
    domain = "aliexpress.ru"
    supports_search = True
    supports_reviews = False

    async def search(self, query, *, model=None, limit=12, city=None):
        start = monotonic()
        url = _ORIGIN + "/wholesale?" + urlencode({"SearchText": query})
        capture = await fetch_capture(self.transport.cdp_fetch, url, wait_selector='a[href*="/item/"]',
                                      wait_ms=3500, scroll_steps=2, capture_json_patterns=_PATTERNS, timeout_s=40)
        listings = []
        if not capture.error:
            state = [_state_listing(sku, title, item, model, capture.http_status, search=True)
                     for sku, title, item in _state_items(capture)]
            listings = merge_listings(state, _schema_listings(self, capture, model))
            listings = merge_listings(listings, _search_dom(capture, model))
        return search_result(self, listings, model, limit, start, error=capture.error)

    async def get_card(self, url_or_sku, *, model=None):
        sku = _sku(url_or_sku)
        if not sku:
            return ConnectorCardResult(error="invalid_card_url_or_sku")
        url = _url(sku)
        capture = await fetch_capture(self.transport.cdp_fetch, url, wait_selector="h1",
                                      capture_json_patterns=["aer-jsonapi", "item"], timeout_s=40)
        if capture.error:
            return ConnectorCardResult(error=capture.error)
        listings = []
        for identifier, title, item in _state_items(capture):
            if identifier == sku:
                listing = _state_listing(sku, title, item, model, capture.http_status)
                listings = merge_listings(listings, [listing])
        listings = merge_listings(listings, _schema_listings(self, capture, model, sku))
        if title := attr_text(capture.soup, 'h1, [itemprop="name"]'):
            price_tile = capture.soup.select_one('[data-testid="product-details"], [itemscope][itemtype$="/Product"]')
            listings = merge_listings(listings, [
                _dom_listing(price_tile or capture.soup, url, sku, title, model, capture.http_status),
            ])
        return card_result(listings, model)
