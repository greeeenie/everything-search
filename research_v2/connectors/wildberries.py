"""Wildberries public catalog, card metadata, and buyer feedback."""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
import json
import re
from time import monotonic
from urllib.parse import urlencode, urljoin, urlunsplit

from bs4 import BeautifulSoup
import httpx

from ..fetching import blocked_reason
from ..models import Offer, Product, now
from ..network import AccessError, host_matches, validate_url
from ..shopping import has_type, normalize, product_matches
from .base import (
    Availability,
    ConnectorCardResult,
    ConnectorSearchResult,
    MarketplaceConnector,
    OutcomeStatus,
    SourceOutcome,
    make_product_and_offer,
    parse_price_rub,
)


# Shared parsing and transport helpers for the five native connectors.
Listing = tuple[Product, Offer]


def mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def rows(value) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
    elif isinstance(value, list):
        yield from (row for row in value if isinstance(row, dict))


def json_value(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return None


def json_objects(value) -> Iterable[dict]:
    """Walk decoded state, including JSON strings nested in widget state."""
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
        elif isinstance(node, str) and node.lstrip().startswith(("{", "[")):
            decoded = json_value(node)
            if isinstance(decoded, (dict, list)):
                stack.append(decoded)


def label(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("title") or value.get("text")
    return value.strip() if isinstance(value, str) and value.strip() else None


def attr_text(node, selector: str, attr: str = "content") -> str | None:
    tag = node.select_one(selector)
    return (label(tag.get(attr)) or label(tag.get("content")) or label(tag.get_text(" ", strip=True))) if tag else None


def id_text(value) -> str | None:
    if isinstance(value, (int, str)) and re.fullmatch(r"[0-9]{1,20}", str(value).strip()):
        return str(value).strip()
    return None


def count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    match = re.fullmatch(r"\s*(\d(?:[\d\s]*\d)?)(?:\s+(?:отзыв\w*|оцен\w*|покуп\w*|заказ\w*))?\s*", str(value))
    digits = re.sub(r"\s+", "", match[1]) if match else ""
    return int(digits) if 0 < len(digits) <= 12 else None


def rating(value) -> float | None:
    if isinstance(value, dict):
        value = value.get("ratingValue")
    amount = parse_price_rub(value)
    return float(amount) if amount is not None and amount <= 5 else None


_RUB = r"(?:₽|руб\.?|RUB|RUR)"
_PRICE = r"\d+(?:[^\S\r\n]\d{3})*(?:[.,]\d{1,2})?"
TILE_RUB_PRICE = re.compile(rf"(?<![\w.,])({_PRICE})\s*₽")


def rub(value, *, require_currency: bool = False) -> Decimal | None:
    if isinstance(value, dict):
        currency = value.get("currency") or value.get("currencyCode") or value.get("priceCurrency")
        if currency and str(currency).upper() not in {"RUB", "RUR", "₽", "643"}:
            return None
        value = next((value[k] for k in ("value", "amount", "price", "priceValue", "text", "formatted")
                      if value.get(k) is not None), None)
    if not require_currency and (amount := parse_price_rub(value)) is not None:
        return amount
    if isinstance(value, str) and (match := re.search(rf"({_PRICE})\s*{_RUB}", value, re.I)):
        return parse_price_rub(match[1])
    return None


def dom_segments(tile):
    return [part.strip() for text in tile.stripped_strings for part in text.split("|") if part.strip()]


def priced_tiles(soup, selector, sku_for_link, *, max_parents):
    """Find the nearest priced ancestor for each link without crossing into another product."""
    links = soup.select(selector)
    identifiers = {id(link): sku_for_link(link.get("href")) for link in links}
    seen = set()
    for link in links:
        if not (sku := identifiers[id(link)]):
            continue
        for depth, tile in enumerate((link, *link.parents)):
            if depth > max_parents or tile.name in {"main", "body", "html", "[document]"}:
                break
            tile_links = [link] if tile is link else tile.select(selector)
            if any(identifiers[id(candidate)] not in {None, sku} for candidate in tile_links):
                break
            if TILE_RUB_PRICE.search(tile.get_text(" ", strip=True)):
                key = (sku, id(tile))
                if key not in seen:
                    seen.add(key)
                    yield sku, tile, [candidate for candidate in tile_links if identifiers[id(candidate)] == sku]
                break


def availability(value) -> Availability:
    if value is True:
        return "in_stock"
    if value is False:
        return "out_of_stock"
    if isinstance(value, str):
        value = re.sub(r"[\s_-]", "", value.rsplit("/", 1)[-1].casefold())
        if value in {"instock", "limitedavailability", "available", "вналичии", "active"}:
            return "in_stock"
        if value in {"outofstock", "soldout", "discontinued", "unavailable", "нетвналичии", "inactive"}:
            return "out_of_stock"
        if value == "preorder":
            return "preorder"
    return "unknown"


def observed_stock(node) -> Availability:
    state = availability(attr_text(node, '[itemprop="availability"]', "href"))
    if state == "unknown":
        state = availability(node.get("data-availability"))
    if state == "unknown" and re.search(
        r"\b(?:нет в наличии|товар закончился|товар распродан|снято с публикации|объявление закрыто|sold out)\b",
        node.get_text(" ", strip=True), re.I,
    ):
        state = "out_of_stock"
    if state == "unknown":
        for button in node.select('[data-auto="cartButton"], [data-marker*="buy"], .product-card__add-basket, button'):
            if button.has_attr("disabled") or button.get("aria-disabled") == "true":
                continue
            button_text = button.get("aria-label") or button.get("title") or button.get_text(" ", strip=True)
            if "product-card__add-basket" in button.get("class", []) or re.search(
                r"\b(?:в корзину|купить|add to cart|buy now)\b", button_text, re.I,
            ):
                return "in_stock"
    return state


def market_url(value, base: str, domain: str, pattern: str, *, keep_query: bool = False) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed, host = validate_url(urljoin(base, value.strip()))
    except (AccessError, ValueError):
        return None
    if not host_matches(host, domain) or not re.search(pattern, parsed.path):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query if keep_query else "", ""))


def image_url(value, base: str) -> str | None:
    if isinstance(value, list):
        value = next(iter(value), None)
    if isinstance(value, dict):
        value = value.get("url") or value.get("src") or value.get("contentUrl") or value.get("640x480")
        if not value:
            return None
    try:
        return urljoin(base, value) if isinstance(value, str) and value.strip() else None
    except ValueError:
        return None


def specifications(value) -> dict[str, str]:
    if isinstance(value, dict):
        return {key[:150]: str(val)[:500] for key, val in value.items()
                if isinstance(val, (str, int, float)) and str(val).strip()}
    result = {}
    for row in rows(value):
        key = label(row.get("name") or row.get("title") or row.get("label") or row.get("propertyName"))
        val = row.get("value", row.get("text", row.get("propertyValue")))
        if isinstance(val, dict):
            val = label(val)
        if isinstance(val, list):
            val = ", ".join(filter(None, (label(x) for x in val)))
        if key and isinstance(val, (str, int, float)) and str(val).strip():
            result[key[:150]] = str(val)[:500]
    return result


def schema_products(payload) -> Iterable[dict]:
    linked = set()
    for node in json_objects(payload):
        if has_type(node, "ListItem") and (item := mapping(node.get("item"))) and has_type(item, "Product"):
            if node.get("url") and not item.get("url"):
                linked.add(id(item))
                yield {**item, "url": node["url"]}
        if has_type(node, "Product") and id(node) not in linked:
            yield node


def schema_listing(connector, node, target, model, *, http_status=None, buyer_rating=True) -> Listing:
    offer_nodes = [raw for raw in rows(node.get("offers")) if not has_type(raw, "AggregateOffer")]
    matched = next((raw for raw in offer_nodes if availability(raw.get("availability")) == "in_stock"), None)
    raw = matched or next(iter(offer_nodes), {})
    aggregate = mapping(node.get("aggregateRating"))
    currency = label(raw.get("priceCurrency"))
    return make_product_and_offer(
        connector.name, target, label(node.get("name")) or "",
        rub(raw.get("price")) if currency and currency.upper() in {"RUB", "RUR"} else None,
        model=model, brand=label(node.get("brand")), seller=label(raw.get("seller")),
        condition=label(raw.get("itemCondition") or node.get("itemCondition")), default_condition="unknown",
        availability=availability(raw.get("availability")),
        rating=rating(aggregate.get("ratingValue")) if buyer_rating else None,
        reviews_count=count(aggregate.get("reviewCount", aggregate.get("ratingCount"))) if buyer_rating else None,
        image_url=image_url(node.get("image"), target), sku=id_text(node.get("sku") or node.get("productID")),
        specifications=specifications(node.get("additionalProperty")), http_status=http_status,
    )


def merge_listings(primary: list[Listing], secondary: list[Listing], *, key=None, clarify_price=False) -> list[Listing]:
    """Fill missing fields for the same listing while keeping structured prices authoritative."""
    def url_key(offer):
        return offer.url.split("?", 1)[0].rstrip("/")

    key = key or url_key
    merged = {}
    for product, offer in [*primary, *secondary]:
        listing_key = key(offer)
        if listing_key not in merged:
            merged[listing_key] = (product, offer)
            continue
        original_product, original_offer = merged[listing_key]
        original_product.brand = original_product.brand or product.brand
        original_product.specifications = {**product.specifications, **original_product.specifications}
        for field in ("seller", "rating", "reviews_count", "image_url", "sku", "original_price"):
            if getattr(original_offer, field) is None:
                setattr(original_offer, field, getattr(offer, field))
        if original_offer.availability == "unknown" or offer.availability == "out_of_stock":
            original_offer.availability = offer.availability
        if original_offer.condition == "unknown":
            original_offer.condition = offer.condition
        original_offer.delivery.update({k: v for k, v in offer.delivery.items() if k not in original_offer.delivery})
        if original_offer.price_scope == "unknown":
            original_offer.price = offer.price
            original_offer.price_scope = offer.price_scope
            original_offer.currency = offer.currency
        if original_offer.conditional_price is None and offer.conditional_price is not None:
            original_offer.conditional_price = offer.conditional_price
            original_offer.price_conditions = offer.price_conditions
        if clarify_price and original_offer.price_scope == "public" and original_offer.price == offer.conditional_price:
            original_offer.price_scope = offer.price_scope
            original_offer.price = offer.price
            original_offer.conditional_price = offer.conditional_price
            original_offer.price_conditions = offer.price_conditions
        if original_offer.price_scope == "conditional" and offer.price_scope == "public":
            if original_offer.conditional_price != offer.price:
                original_offer.price = offer.price
                original_offer.price_scope = "public"
        for excerpt in offer.evidence.excerpts:
            if excerpt not in original_offer.evidence.excerpts:
                original_offer.evidence.excerpts.append(excerpt)
    return list(merged.values())


def script_data(soup, selector: str) -> list:
    return [data for tag in soup.select(selector)
            if (data := json_value(tag.string or tag.get_text())) is not None]


@dataclass
class Capture:
    soup: BeautifulSoup
    payloads: list
    url: str
    http_status: int | None = None
    error: str | None = None

    @property
    def schema(self) -> list:
        return script_data(self.soup, 'script[type="application/ld+json"]')


async def fetch_capture(fetch, url, **kwargs) -> Capture:
    try:
        result = await fetch(url, **kwargs)
    except (AccessError, TimeoutError, httpx.HTTPError) as exc:
        result = {"error": fetch_error(exc)}
    if isinstance(result, httpx.Response):
        try:
            final_url = str(result.url)
        except RuntimeError:
            final_url = url
        result = {"html": result.text, "http_status": result.status_code, "final_url": final_url}
    if not isinstance(result, dict):
        result = {"error": "invalid_capture"}
    html = result.get("html")
    soup = BeautifulSoup(html if isinstance(html, str) else "", "html.parser")
    status = result.get("http_status")
    status = status if isinstance(status, int) else None
    final_url = label(result.get("final_url")) or url
    error = label(result.get("error"))
    if not error:
        error = blocked_reason(status or 200, soup.title.get_text() if soup.title else "", soup.get_text(" ", strip=True))
    if not error and status and not 200 <= status < 300:
        error = f"http_{status}"
    captured = []
    for row in rows(result.get("captured_json")):
        if "data" in row and ("url" in row or "status" in row):
            row_status = row.get("status", 200)
            if isinstance(row_status, int) and 200 <= row_status < 300:
                captured.append(row["data"])
        else:
            captured.append(row)
    return Capture(soup, captured, final_url, status, error)


def fetch_error(exc) -> str:
    if isinstance(exc, AccessError):
        return str(exc)
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    return "network_error"


async def fetch_json(transport, url, **kwargs) -> tuple[object, int | None, str | None]:
    try:
        response = await transport.http_get(url, **kwargs)
    except (AccessError, TimeoutError, httpx.HTTPError) as exc:
        return None, None, fetch_error(exc)
    status = response.status_code
    if not 200 <= status < 300:
        return None, status, f"http_{status}"
    try:
        return response.json(), status, None
    except (ValueError, RecursionError):
        error = blocked_reason(status, "", response.text) or "invalid_json"
        return None, status, error


def _outcome_status(error: str | None) -> OutcomeStatus:
    if not error:
        return "ok"
    if "timeout" in error:
        return "timeout"
    if error in {"http_401", "http_403", "http_407", "http_429", "http_439", "http_451", "authentication_required"} or any(
        word in error for word in ("captcha", "challenge", "blocked", "denied")
    ):
        return "blocked"
    return "error"


def source_error(source, reason, stage=None) -> dict:
    return {"source": source, "error": reason, **({"stage": stage} if stage else {})}


def search_result(connector, listings, model, limit, start, *, error=None, previous=None, fallback=False, warnings=()):
    # A requested model gives every candidate the same canonical product ID. Match each
    # candidate against its own observed brand before recombining the selected products.
    candidates = {str(index): listing for index, listing in enumerate(listings)}
    _, selected = connector._filter_and_dedupe(
        [product.model_copy(update={"id": key}) for key, (product, _) in candidates.items()],
        [offer.model_copy(update={"product_id": key}) for key, (_, offer) in candidates.items()],
        model=model, limit=limit,
    )
    products_by_id = {}
    offers = []
    # The base helper uses an effective amount to retain conditional offers during filtering.
    for match in selected:
        product, offer = candidates[match.product_id]
        canonical = products_by_id.setdefault(product.id, product)
        canonical.brand = canonical.brand or product.brand
        if offer.price_scope == "conditional":
            offer.price = None
        offers.append(offer)
    detail = error or (None if offers else "No matching available priced offers" if listings else "No listings extracted")
    return ConnectorSearchResult(list(products_by_id.values()), offers, SourceOutcome(
        source=connector.name, status=_outcome_status(error), offers_returned=len(offers),
        elapsed_ms=int((monotonic() - start) * 1000), detail=detail, error_code=error,
        fallback_used=fallback, warnings=[*warnings, *([f"Primary transport: {previous}"] if previous else [])],
    ).as_dict(), [source_error(connector.name, error)] if error else [])


def card_result(listings, model, *, error=None, reviews=()) -> ConnectorCardResult:
    if error:
        return ConnectorCardResult(error=error)
    if model:
        listings = [(product, offer) for product, offer in listings
                    if product_matches(model, offer.title) or (
                        product.brand and not normalize(offer.title).startswith(normalize(product.brand))
                        and product_matches(model, f"{product.brand} {offer.title}")
                    )]
    if not listings:
        return ConnectorCardResult(error="model_mismatch_or_card_unavailable" if model else "card_unavailable")
    for _, offer in listings:
        if offer.price_scope == "conditional":
            offer.price = None
    products = {product.id: product for product, _ in listings}
    return ConnectorCardResult(list(products.values()), [offer for _, offer in listings], list(reviews))


def review_record(marketplace, url, model, *, text=None, pros=None, cons=None, stars=None, published_at=None) -> dict | None:
    text, pros, cons = label(text), label(pros), label(cons)
    if not (text or pros or cons):
        return None
    return {
        "marketplace": marketplace, "url": url, "model": model, "status": "verified", "checked_at": now(),
        "text": (text or "\n".join(filter(None, (pros, cons))))[:1200],
        "pros": pros[:1200] if pros else None, "cons": cons[:1200] if cons else None,
        "rating": rating(stars), "published_at": published_at if isinstance(published_at, (str, int)) else None,
    }


def unique_reviews(reviews) -> list[dict]:
    unique = {}
    for review in filter(None, reviews):
        unique.setdefault((review["text"], review["pros"], review["cons"]), review)
    return list(unique.values())


_WB_PARAMS = {"appType": 1, "curr": "rub", "dest": -1257786, "spp": 30}
_WB_SEARCH = "https://search.wb.ru/exactmatch/ru/common/v9/search"
_WB_CARD = "https://card.wb.ru/cards/v4/detail"
_WB_ORIGIN = "https://www.wildberries.ru"
_WB_PATH = r"/catalog/([0-9]{1,20})/detail\.aspx/?$"
_WB_BASKET_MAX_VOLUMES = (
    143, 287, 431, 719, 1007, 1061, 1115, 1169, 1313, 1601, 1655, 1919, 2045,
    2189, 2405, 2621, 2837, 3053, 3269, 3485, 3701, 3917, 4133, 4349, 4565,
)


def _wb_url(sku: str) -> str:
    return f"{_WB_ORIGIN}/catalog/{sku}/detail.aspx"


def _wb_sku(value) -> str | None:
    if sku := id_text(value):
        return sku
    url = market_url(value, _WB_ORIGIN, "wildberries.ru", r"/catalog/[0-9]{1,20}/")
    return re.search(r"/catalog/(\d+)/", url)[1] if url else None


def _wb_products(payload):
    for node in json_objects(payload):
        if isinstance(node.get("products"), list):
            yield from rows(node["products"])


def _wb_has_products_array(payload) -> bool:
    return any(isinstance(node.get("products"), list) for node in json_objects(payload))


def _kopecks(value) -> Decimal | None:
    price = parse_price_rub(value)
    return price / 100 if price is not None else None


def _wb_prices(item: dict) -> tuple[Decimal | None, Decimal | None]:
    candidates = []
    for size in rows(item.get("sizes")):
        price = mapping(size.get("price"))
        amount = _kopecks(price.get("product"))
        if amount is not None:
            quantities = [qty for stock in rows(size.get("stocks")) if (qty := count(stock.get("qty"))) is not None]
            if not quantities or any(quantities):
                candidates.append((not bool(quantities), amount, _kopecks(price.get("basic"))))
    if candidates:
        _, amount, original = min(candidates, key=lambda candidate: candidate[:2])
        return amount, original
    return _kopecks(item.get("salePriceU")), _kopecks(item.get("priceU"))


def _wb_row(item, model, status, metadata=None) -> Listing | None:
    metadata = metadata or {}
    sku = id_text(item.get("id") or item.get("nmId") or item.get("nm_id") or metadata.get("nm_id"))
    title = label(item.get("name") or metadata.get("name"))
    if not sku or not title:
        return None
    quantity = count(item.get("totalQuantity"))
    stock = availability(quantity > 0) if quantity is not None else "unknown"
    price, original_price = _wb_prices(item)
    imt_id = id_text(item.get("root")) or id_text(metadata.get("imt_id"))
    return make_product_and_offer(
        "wildberries", _wb_url(sku), title, price, model=model, sku=sku,
        brand=label(item.get("brand") or mapping(metadata.get("selling")).get("brand_name")),
        seller=label(item.get("supplier")), availability=stock, original_price=original_price,
        rating=rating(item.get("reviewRating")) or rating(item.get("rating")), reviews_count=count(item.get("feedbacks")),
        specifications=specifications(metadata.get("options") or item.get("options")),
        variants={"imt_id": imt_id} if imt_id else None, http_status=status,
    )


def _wb_dom(soup, model, status, *, sku=None) -> list[Listing]:
    listings = []
    tiles = [soup] if sku else soup.select("article[data-nm-id], .product-card")
    for tile in tiles:
        anchor = tile.select_one('a[href*="/catalog/"]') if not sku else None
        item_id = sku or id_text(tile.get("data-nm-id")) or (_wb_sku(anchor.get("href")) if anchor else None)
        title = attr_text(tile, "h1" if sku else ".product-card__name") or label(tile.get("aria-label"))
        if not title and anchor:
            title = label(anchor.get("aria-label")) or label(anchor.get_text(" ", strip=True))
        if not title or not item_id:
            continue
        regular = rub(attr_text(tile, ".price__regular-price, [data-price-type='regular'], [data-price-type='public']"))
        visible = rub(attr_text(tile, ".price__lower-price, .price-block__final-price, ins"))
        price_tag = tile.select_one(".price__lower-price, .price-block__final-price, ins")
        if regular is None and price_tag and re.search(
            r"без (?:wb\s*)?кошелька|обычная цена", price_tag.get("aria-label", "") + " " + price_tag.get_text(" ", strip=True), re.I,
        ):
            regular = visible
        conditional = visible if visible != regular else None
        stock = observed_stock(tile)
        product, offer = make_product_and_offer(
            "wildberries", _wb_url(item_id), title, regular, model=model, sku=item_id, availability=stock,
            conditional_price=conditional, price_conditions=["wallet"] if conditional else [],
            original_price=rub(attr_text(tile, "del")), brand=label((attr_text(tile, ".product-card__brand") or "").strip(" /")),
            rating=rating(attr_text(tile, ".address-rate-mini")), reviews_count=count(attr_text(tile, ".product-card__count")),
            image_url=image_url(attr_text(tile, "img", "src"), _WB_ORIGIN), http_status=status,
        )
        listings.append((product, offer))
    return listings


class WildberriesConnector(MarketplaceConnector):
    name = "wildberries"
    domain = "wildberries.ru"
    supports_search = True
    supports_reviews = True

    async def search(self, query, *, model=None, limit=12, city=None):
        start = monotonic()
        data, status, error = await fetch_json(self.transport, _WB_SEARCH, params={
            **_WB_PARAMS, "ab_testing": "false", "query": query, "resultset": "catalog",
            "sort": "popular", "suppressSpellcheck": "false",
        })
        if not error and _wb_has_products_array(data):
            listings = [listing for item in _wb_products(data) if (listing := _wb_row(item, model, status))]
            return search_result(self, listings, model, limit, start)
        primary = error or "invalid_catalog_payload"
        url = _WB_ORIGIN + "/catalog/0/search.aspx?" + urlencode({"search": query})
        capture = await fetch_capture(
            self.transport.cdp_fetch, url, wait_selector="article[data-nm-id], .product-card", wait_ms=3000,
            scroll_steps=2, capture_json_patterns=["exactmatch", "u-search", "search.wb.ru", "search/v"], timeout_s=35,
        )
        listings = []
        warnings = []
        if not capture.error:
            listings = [listing for item in _wb_products(capture.payloads)
                        if (listing := _wb_row(item, model, capture.http_status))]
            if not listings:
                listings = _wb_dom(capture.soup, model, capture.http_status)
                if listings:
                    warnings.append("wb_dom_fallback: tile prices may reflect WB Wallet discount")
        return search_result(self, listings, model, limit, start, error=capture.error, previous=primary,
                             fallback=True, warnings=warnings)

    async def _fetch_basket_card(self, nm_id: str) -> dict:
        vol, part = int(nm_id) // 100000, int(nm_id) // 1000
        basket = next((index for index, maximum in enumerate(_WB_BASKET_MAX_VOLUMES, 1) if vol <= maximum),
                      len(_WB_BASKET_MAX_VOLUMES) + (vol - _WB_BASKET_MAX_VOLUMES[-1] + 215) // 216)
        for index in dict.fromkeys((basket, basket - 1, basket + 1)):
            if index < 1:
                continue
            url = f"https://basket-{index:02}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/info/ru/card.json"
            metadata, _, error = await fetch_json(self.transport, url, timeout=4)
            if not error and isinstance(metadata, dict) and (
                id_text(metadata.get("nm_id")) in {None, nm_id}
                and (metadata.get("imt_id") or metadata.get("options") or metadata.get("name"))
            ):
                return metadata
        return {}

    async def _card_sources(self, sku: str):
        (payload, status, error), metadata = await asyncio.gather(
            fetch_json(self.transport, _WB_CARD, params={**_WB_PARAMS, "nm": sku}),
            self._fetch_basket_card(sku),
        )
        item = next((row for row in _wb_products(payload) if id_text(row.get("id") or row.get("nmId")) == sku), {})
        return item, status, error, metadata

    async def get_card(self, url_or_sku, *, model=None):
        sku = _wb_sku(url_or_sku)
        if not sku:
            return ConnectorCardResult(error="invalid_card_url_or_sku")
        item, status, error, metadata = await self._card_sources(sku)
        if item and (listing := _wb_row(item, model, status, metadata)):
            return card_result([listing], model)
        capture = await fetch_capture(self.transport.cdp_fetch, _wb_url(sku), wait_selector="h1",
                                      capture_json_patterns=["cards/v", "detail", "card.json"])
        if not capture.error:
            for node in json_objects(capture.payloads):
                if id_text(node.get("nm_id")) == sku and (node.get("options") or node.get("imt_id")):
                    metadata = {**metadata, **node}
                    break
            item = next((row for row in _wb_products(capture.payloads) if id_text(row.get("id")) == sku), {})
            listings = [_wb_row({**item, "id": sku}, model, capture.http_status, metadata)]
            listings = list(filter(None, listings))
            structured = [schema_listing(self, node, _wb_url(sku), model, http_status=capture.http_status)
                          for node in schema_products(capture.schema) if label(node.get("name"))]
            listings = merge_listings(listings, structured)
            listings = merge_listings(listings, _wb_dom(capture.soup, model, capture.http_status, sku=sku))
            if listings:
                return card_result(listings, model)
        if listing := _wb_row({"id": sku}, model, None, metadata):
            return card_result([listing], model)
        return ConnectorCardResult(error=capture.error or error or "card_unavailable")

    async def get_reviews(self, model, *, limit=10):
        if limit <= 0:
            return [], []
        result = await self.search(model, model=model, limit=3)
        if not result.offers:
            return [], result.errors or [source_error(self.name, "matching_listing_unavailable", "search")]
        offer = result.offers[0]
        sku = offer.sku or _wb_sku(offer.url)
        if not sku:
            return [], [source_error(self.name, "sku_unavailable", "search")]
        imt_id = id_text(offer.variants.get("imt_id"))
        if not imt_id:
            item, _, card_error, metadata = await self._card_sources(sku)
            imt_id = id_text(item.get("root")) or id_text(metadata.get("imt_id"))
            if not imt_id:
                return [], [source_error(self.name, card_error or "imt_id_unavailable", "card")]
        reviews = []
        error = None
        for host in ("feedbacks2", "feedbacks1"):
            data, _, error = await fetch_json(self.transport, f"https://{host}.wb.ru/feedbacks/v1/{imt_id}")
            feedbacks = next((node["feedbacks"] for node in json_objects(data)
                              if isinstance(node.get("feedbacks"), list)), [])
            reviews = unique_reviews(review_record(
                self.name, offer.url, model, text=row.get("text"), pros=row.get("pros"), cons=row.get("cons"),
                stars=row.get("productValuation"), published_at=row.get("createdDate"),
            ) for row in rows(feedbacks))
            if reviews:
                break
            if not error and not any(isinstance(node.get("feedbacks"), list) for node in json_objects(data)):
                error = "invalid_feedback_payload"
        recent = sorted(reviews, key=lambda review: str(review["published_at"] or ""), reverse=True)
        complaints = sorted((review for review in recent if review["rating"] is not None and review["rating"] <= 3),
                            key=lambda review: review["rating"])
        chosen = complaints[:max(1, limit // 2)]
        positive = [review for review in recent if review["rating"] is not None and review["rating"] >= 4]
        chosen += positive[:limit - len(chosen)]
        chosen += [review for review in recent if review not in chosen][:limit - len(chosen)]
        return chosen, [source_error(self.name, error, "reviews")] if error and not chosen else []
