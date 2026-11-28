"""DNS and the shared structured-data/DOM parsing used by this connector group."""

from dataclasses import dataclass, field, fields
import json
import re
from time import monotonic
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, NavigableString

from ..fetching import blocked_reason
from ..network import AccessError, host_matches, validate_url
from ..shopping import has_type, normalize, product_matches
from .base import (
    Availability,
    ConnectorCardResult,
    ConnectorSearchResult,
    MarketplaceConnector,
    OutcomeStatus,
    PriceValue,
    SourceOutcome,
    make_product_and_offer,
    parse_price_rub,
)


@dataclass
class _Listing:
    url: str
    title: str
    price: PriceValue = None
    original_price: PriceValue = None
    conditional_price: PriceValue = None
    availability: Availability = "unknown"
    brand: str | None = None
    seller: str | None = None
    condition: str | None = None
    rating: float | None = None
    reviews_count: int | None = None
    image_url: str | None = None
    sku: str | None = None
    specifications: dict[str, str] = field(default_factory=dict)
    sizes: list[str] = field(default_factory=list)
    price_conditions: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.price = _price(self.price)
        self.original_price = _price(self.original_price)
        self.conditional_price = _price(self.conditional_price)

    def fill_missing(self, other):
        same_seller = not self.seller or not other.seller or self.seller == other.seller
        for item in fields(self):
            if not same_seller and item.name in {
                "price", "original_price", "conditional_price", "availability", "condition", "price_conditions",
            }:
                continue
            current = getattr(self, item.name)
            if current is None or current == "unknown" or current == []:
                setattr(self, item.name, getattr(other, item.name))
        self.specifications = other.specifications | self.specifications


def _text(value):
    if isinstance(value, str):
        return " ".join(value.split()) or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _named(value):
    if isinstance(value, dict):
        value = value.get("name") or value.get("title")
    return _text(value)


def _price(value):
    if isinstance(value, dict):
        currency = _text(value.get("currency") or value.get("currencyCode"))
        if not _rub(currency):
            return None
        value = next((value[k] for k in ("amount", "value", "price") if value.get(k) is not None), None)
    return parse_price_rub(value)


def _rub(currency):
    return currency is None or currency.upper() in {"RUB", "RUR", "₽", "643"}


def _stock(value) -> Availability:
    if value is True:
        return "in_stock"
    if value is False:
        return "out_of_stock"
    label = (_text(value) or "").casefold()
    if label.startswith(("https://schema.org/", "http://schema.org/")):
        label = label.rstrip("/").rsplit("/", 1)[-1]
    if label in {"outofstock", "out_of_stock", "soldout", "discontinued", "unavailable", "false"} or re.search(
        r"нет в наличии|нет в магазинах|не в наличии|нет на складе|товар закончился|нет размеров|распродан|недоступен", label,
    ):
        return "out_of_stock"
    if label in {"preorder", "pre_order", "presale", "backorder"} or "предзаказ" in label:
        return "preorder"
    if label in {"instock", "in_stock", "limitedavailability", "available", "true"} or re.search(
        r"\bв наличии\b|\bв магазинах\b|\bна складе\b", label,
    ):
        return "in_stock"
    return "unknown"


def _rating(value):
    match = re.match(r"(?:рейтинг\s*:?\s*|оценка\s*:?\s*)?(\d+(?:[.,]\d+)?)(?![\d.,])", _text(value) or "", re.I)
    if match:
        score = float(match[1].replace(",", "."))
        if 0 <= score <= 5:
            return score
    return None


def _count(value):
    label = (_text(value) or "").strip("()").strip()
    match = re.fullmatch(r"(\d{1,3}(?: \d{3})+|\d+)(?:\s+[а-яёa-z]+)?", label, re.I)
    if match and len(digits := re.sub(r"\s", "", match[1])) <= 12:
        return int(digits)
    return None


def _image(value):
    if isinstance(value, list):
        return next((url for item in value if (url := _image(item))), None)
    if isinstance(value, dict):
        return _image(value.get("original") or value.get("url") or value.get("contentUrl"))
    if isinstance(value, str) and not value.startswith("data:"):
        return _text(value)
    return None


def _objects(data):
    """Iterate JSON objects, including arbitrarily nested lists and state wrappers."""
    pending = [data]
    seen = set()
    while pending:
        node = pending.pop()
        if not isinstance(node, (dict, list)) or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, dict):
            yield node
            pending.extend(reversed(node.values()))
        else:
            pending.extend(reversed(node))


def _json_script(node):
    try:
        return json.loads(node.string or node.get_text())
    except (ValueError, RecursionError):
        return None


def _captured(capture, url_contains=None):
    responses = capture.get("captured_json")
    for entry in responses if isinstance(responses, list) else []:
        if not isinstance(entry, dict):
            continue
        if url_contains and url_contains not in (_text(entry.get("url")) or ""):
            continue
        status = entry.get("status")
        if status is None or isinstance(status, int) and 200 <= status < 300:
            yield entry.get("data", entry)


def _value(node, selectors, *attributes):
    for selector in selectors:
        for el in node.select(selector):
            for attribute in attributes:
                if value := _text(el.get(attribute)):
                    return value
            if value := el.get_text(" ", strip=True):
                return value
    return None


def _dom_price(node, selectors):
    for selector in selectors:
        for el in node.select(selector):
            for value in (
                el.get("content"), el.get("data-meta-price"), el.get("data-price"),
                " ".join(el.find_all(string=True, recursive=False)), el.get_text(" ", strip=True),
            ):
                if (price := _price(value)) is not None:
                    return price
    return None


def _rub_text_price(node, old_selectors):
    excluded = {id(el) for el in node.select(
        ", ".join(old_selectors + ('[class*="bonus"]', '[class*="cashback"]', '[class*="installment"]')),
    )}
    prices = set()
    for string in node.find_all(string=True):
        if type(string) is not NavigableString or not re.fullmatch(r"\d[\d\s]*(?:[.,]\d{1,2})?\s*₽", string.strip()):
            continue
        if any(id(parent) in excluded for parent in string.parents):
            continue
        if re.search(r"в месяц|/\s*мес\.|рассрочк|бонус|кэшб[еэ]к", string.parent.get_text(" ", strip=True), re.I):
            continue
        if (price := _price(string)) is not None:
            prices.add(price)
    return prices.pop() if len(prices) == 1 else None


def _dom_image(node):
    return _image(_value(node, ('[itemprop="image"]', 'meta[property="og:image"]', "img"),
                         "content", "data-src", "data-original", "data-lazy-src", "src"))


def _dom_stock(node, extra_buy_selector):
    explicit = _stock(node.get("data-available") or node.get("data-in-stock") or node.get("data-availability") or _value(
        node, ('[itemprop="availability"]', '[data-test="availability"]', '[class*="availability"]'),
        "content", "href", "data-availability",
    ))
    if explicit != "unknown":
        return explicit
    visible = _stock(node.get_text(" ", strip=True))
    if visible != "unknown":
        return visible
    for button in node.select("button, [role=button], " + extra_buy_selector):
        if button.has_attr("disabled") or button.get("aria-disabled") == "true":
            continue
        label = button.get("aria-label") or button.get_text(" ", strip=True)
        if button.css.match(extra_buy_selector) or re.match(r"^(?:купить|в корзину|добавить в корзину)$", label, re.I):
            return "in_stock"
    return "unknown"


def _rows(value):
    return value if isinstance(value, list) else [value] if isinstance(value, dict) else []


def _specifications(value):
    result = {}
    for row in _rows(value):
        name = _named(row) if isinstance(row, dict) else None
        if name and (entry := _named(row.get("value"))):
            result[name] = entry
    return result


def _jsonld(soup, card_url=None):
    for script in soup.select('script[type="application/ld+json"]'):
        data = _json_script(script)
        # ItemLists can supply the URL on the ListItem wrapper.
        item_urls = {
            id(node["item"]): node.get("url") for node in _objects(data)
            if has_type(node, "ListItem") and isinstance(node.get("item"), dict)
        }
        for node in _objects(data):
            if not has_type(node, "Product") or not (title := _text(node.get("name"))):
                continue
            aggregate = node.get("aggregateRating")
            aggregate = aggregate if isinstance(aggregate, dict) else {}
            offers = [offer for offer in _rows(node.get("offers")) if isinstance(offer, dict)
                      and not has_type(offer, "AggregateOffer")]
            for offer in offers or [{}]:
                url = _text(offer.get("url") or node.get("url") or item_urls.get(id(node))) or card_url
                if not url:
                    continue
                yield _Listing(
                    url, title,
                    price=offer.get("price") if _rub(_text(offer.get("priceCurrency"))) else None,
                    availability=_stock(offer.get("availability")),
                    brand=_named(node.get("brand")), seller=_named(offer.get("seller")),
                    condition=_text(offer.get("itemCondition") or node.get("itemCondition")),
                    rating=_rating(aggregate.get("ratingValue")),
                    reviews_count=_count(aggregate.get("reviewCount")),
                    image_url=_image(node.get("image")), sku=_text(node.get("sku") or offer.get("sku")),
                    specifications=_specifications(node.get("additionalProperty")),
                    sizes=list(dict.fromkeys(
                        entry for variant in _rows(node.get("hasVariant")) if isinstance(variant, dict)
                        if (entry := _text(variant.get("size")))
                    )),
                )


def _failure_status(code) -> OutcomeStatus:
    if "timeout" in code or "timed_out" in code or code == "http_408":
        return "timeout"
    if code in {"http_401", "http_403", "http_407", "http_429", "http_451", "authentication_required"} or any(
        marker in code for marker in ("challenge", "captcha", "blocked", "qrator", "policy_denied")
    ):
        return "blocked"
    return "error"


def _capture_error(capture, soup):
    if code := _text(capture.get("error")):
        return code
    status = capture.get("http_status")
    status = status if isinstance(status, int) else 200
    title = _text(capture.get("title")) or _value(soup, ("title",)) or ""
    if "qrator" in title.casefold():
        return "captcha_or_access_challenge"
    return blocked_reason(status, title, soup.get_text(" ", strip=True))


class _CdpMarketplaceConnector(MarketplaceConnector):
    origin: str
    product_path: str
    search_path: str
    search_parameter: str = "q"
    search_options: dict
    card_options: dict
    tile_selector: str
    link_selector: str
    title_selectors = ('[itemprop="name"]',)
    price_selectors = ('[itemprop="price"]', '[data-price]')
    old_price_selectors = ("del", "s")
    brand_selectors = ('[itemprop="brand"]',)
    seller_selectors = ('[itemprop="seller"]',)
    rating_selectors = ('[itemprop="ratingValue"]', '[data-rating]')
    review_selectors = ('[itemprop="reviewCount"]', '[data-review-count]')
    buy_selector = '[data-test="add-to-cart"]'

    def _product_url(self, value):
        if not value:
            return None
        try:
            parsed, host = validate_url(urljoin(self.origin, value))
        except (AccessError, ValueError):
            return None
        if host_matches(host, self.domain) and re.fullmatch(self.product_path, parsed.path):
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/", parsed.query, ""))
        return None

    def _card_url(self, value):
        return self._product_url(value.strip())

    def _same_card(self, url, card_url):
        return urlsplit(url).path == urlsplit(card_url).path

    def _listing_key(self, url):
        return url

    def _structured(self, soup, capture, card_url):
        yield from _jsonld(soup, card_url)

    def _tile_nodes(self, soup):
        sentinel = set()
        for card in soup.select(self.tile_selector):
            anchor = card if card.css.match(self.link_selector) else card.select_one(self.link_selector)
            if anchor and self._product_url(anchor.get("href")):
                sentinel.add(id(anchor))
                yield card, anchor
        for anchor in soup.select(self.link_selector):
            if id(anchor) in sentinel:
                continue
            target = self._product_url(anchor.get("href"))
            if not target:
                continue
            for parent in anchor.parents:
                if parent.name in {"main", "body", "html", "[document]"}:
                    break
                links = [self._product_url(link.get("href")) for link in parent.select(self.link_selector)]
                if any(url and url != target for url in links):
                    break
                if parent.name in {"article", "li"} or self._current_dom_price(parent) is not None:
                    yield parent, anchor
                    break

    def _dom_details(self, node, listing, *, is_card):
        """Populate fields specific to one marketplace's DOM."""

    def _current_dom_price(self, node):
        return _dom_price(node, self.price_selectors) or _rub_text_price(node, self.old_price_selectors)

    def _dom_listing(self, node, url, title, *, is_card=False):
        rating = _value(node, self.rating_selectors, "content", "data-rating", "aria-label", "title")
        reviews = _value(node, self.review_selectors, "content", "data-review-count")
        listing = _Listing(
            url, title, price=self._current_dom_price(node),
            original_price=_dom_price(node, self.old_price_selectors),
            availability=_dom_stock(node, self.buy_selector),
            brand=_value(node, self.brand_selectors, "content"), seller=_value(node, self.seller_selectors, "content"),
            rating=_rating(rating), reviews_count=_count(reviews), image_url=_dom_image(node),
        )
        self._dom_details(node, listing, is_card=is_card)
        return listing

    def _tile_title(self, card, anchor):
        return (
            _value(card, self.title_selectors, "content") or anchor.get_text(" ", strip=True)
            or _text(anchor.get("title") or anchor.get("aria-label"))
            or _value(card, (self.link_selector,), "title", "aria-label")
        )

    def _search_dom(self, soup, capture):
        for card, anchor in self._tile_nodes(soup):
            if title := self._tile_title(card, anchor):
                yield self._dom_listing(card, anchor["href"], title)

    def _dom(self, soup, capture, card_url):
        if card_url:
            heading = soup.select_one("h1")
            title = heading.get_text(" ", strip=True) if heading else None
            if title:
                root = next((heading.find_parent(selector) for selector in ("main", "article")
                             if heading.find_parent(selector)), soup)
                related = [tile for tile, anchor in self._tile_nodes(root)
                           if heading not in tile.descendants and not self._same_card(
                               self._product_url(anchor["href"]), card_url,
                           )]
                for tile in related:
                    if tile.parent:
                        tile.decompose()
                yield self._dom_listing(root, card_url, title, is_card=True)
            return
        yield from self._search_dom(soup, capture)

    def _parse(self, capture, soup, card_url):
        listings = {}
        for rows in (self._structured(soup, capture, card_url), self._dom(soup, capture, card_url)):
            for row in rows:
                url = self._product_url(row.url)
                if not url or (card_url and not self._same_card(url, card_url)):
                    continue
                if row.image_url:
                    try:
                        row.image_url = urljoin(url, row.image_url)
                    except ValueError:
                        row.image_url = None
                key = card_url or self._listing_key(url)
                if key in listings:
                    listings[key].fill_missing(row)
                else:
                    row.url = url
                    listings[key] = row
        return list(listings.values())

    def _normalize(self, listings, model, status):
        products, offers = [], []
        for row in listings:
            match_name = row.title
            if row.brand and not normalize(row.title).startswith(normalize(row.brand)):
                match_name = f"{row.brand} {row.title}"
            if model and not product_matches(model, match_name):
                continue
            product, offer = make_product_and_offer(
                self.name, row.url, row.title, row.price, model=model, brand=row.brand, seller=row.seller,
                condition=row.condition, availability=row.availability, original_price=row.original_price,
                conditional_price=row.conditional_price, rating=row.rating, reviews_count=row.reviews_count,
                image_url=row.image_url, sku=row.sku, specifications=row.specifications,
                price_conditions=row.price_conditions, variants={"sizes": " | ".join(row.sizes)} if row.sizes else None,
                http_status=status if isinstance(status, int) else None,
            )
            product.variants = row.sizes
            products.append(product)
            offers.append(offer)
        return products, offers

    def _search_result(
        self, started, *, model, limit, listings=(), status=None, error=None, fallback_used=False, warnings=(),
    ):
        products, offers = self._normalize(listings, model, status)
        products, offers = self._filter_and_dedupe(products, offers, model=model, limit=limit)
        outcome = SourceOutcome(
            source=self.name, status=_failure_status(error) if error else "ok", offers_returned=len(offers),
            elapsed_ms=int((monotonic() - started) * 1000), error_code=error, fallback_used=fallback_used,
            detail=error or ("no_matching_available_priced_offers" if not offers else None), warnings=list(warnings),
        )
        errors = [{"source": self.name, "error": error}] if error else []
        return ConnectorSearchResult(products, offers, outcome.as_dict(), errors)

    async def _capture(self, url, *, is_card=False):
        try:
            capture = await self.transport.cdp_fetch(url, **(self.card_options if is_card else self.search_options))
        except AccessError as error:
            return [], None, str(error) or "browser_error"
        except TimeoutError:
            return [], None, "browser_timeout"
        soup = BeautifulSoup(capture.get("html") or "", "html.parser")
        status = capture.get("http_status")
        if error := _capture_error(capture, soup):
            return [], status, error
        if not (capture.get("html") or capture.get("captured_json")):
            return [], status, "empty_capture"
        final_url = capture.get("final_url") or url
        if not self.matches_url(final_url):
            return [], status, "unexpected_marketplace_redirect"
        card_url = self._product_url(final_url)
        if is_card and (not card_url or not self._same_card(card_url, url)):
            return [], status, "unexpected_product_redirect"
        return self._parse(capture, soup, card_url), status, None

    async def search(self, query, *, model=None, limit=12, city=None):
        started = monotonic()
        url = self.origin + self.search_path + "?" + urlencode({self.search_parameter: query})
        listings, status, error = await self._capture(url)
        return self._search_result(started, model=model, limit=limit, listings=listings, status=status, error=error)

    def _card_result(self, listings, model, status, error=None):
        products, offers = self._normalize(listings, model, status)
        return ConnectorCardResult(
            products=products, offers=offers,
            error=error or (None if products else "model_mismatch" if listings else "product_not_found"),
        )

    async def get_card(self, url_or_sku, *, model=None):
        url = self._card_url(url_or_sku)
        if not url:
            return ConnectorCardResult(error="invalid_product_url")
        listings, status, error = await self._capture(url, is_card=True)
        return self._card_result(listings, model, status, error)


class DnsConnector(_CdpMarketplaceConnector):
    name = "dns"
    domain = "dns-shop.ru"
    origin = "https://www.dns-shop.ru"
    product_path = r"/product/[\w-]+(?:/[^/]+)?/?"
    search_path = "/search/"
    search_options = {
        "wait_selector": '.catalog-product, [data-id="product"], .product-buy__price',
        "wait_ms": 4000, "wait_for_challenge": True, "scroll_steps": 1,
        "capture_json_patterns": ["ajax-state/product-buy", "ajax-state/avails-container"],
        "timeout_s": 40,
    }
    card_options = {"wait_selector": "h1, .product-buy__price", "wait_for_challenge": True, "timeout_s": 40}
    tile_selector = '.catalog-product, [data-code], [data-product], [data-id="product"]'
    link_selector = 'a.catalog-product__name[href], a[href*="/product/"]'
    title_selectors = (".catalog-product__name",) + _CdpMarketplaceConnector.title_selectors
    price_selectors = (".product-buy__price_active", ".product-buy__price") + _CdpMarketplaceConnector.price_selectors
    old_price_selectors = (".product-buy__prev",) + _CdpMarketplaceConnector.old_price_selectors
    rating_selectors = (".catalog-product__rating", ".product-card-top__rating") + _CdpMarketplaceConnector.rating_selectors
    review_selectors = (
        ".catalog-product__reviews-count", ".catalog-product__reviews", ".product-card-top__rating-count",
    ) + _CdpMarketplaceConnector.review_selectors
    buy_selector = 'button.buy-btn, button.product-buy__buy, [data-role="buy"]'

    def _same_card(self, url, card_url):
        return urlsplit(url).path.split("/")[2] == urlsplit(card_url).path.split("/")[2]

    def _structured(self, soup, capture, card_url):
        yield from super()._structured(soup, capture, card_url)
        for data in _captured(capture):
            for item in _objects(data):
                url = _text(item.get("url") or item.get("productUrl"))
                title = _text(item.get("name") or item.get("title"))
                if not title or not self._product_url(url):
                    continue
                yield _Listing(
                    url, title, price=item.get("price"), original_price=item.get("oldPrice"),
                    availability=_stock(item.get("isAvailable", item.get("availability"))),
                    rating=_rating(item.get("rating")), reviews_count=_count(item.get("reviewsCount")),
                    sku=_text(item.get("code") or item.get("sku")), brand=_named(item.get("brand")),
                    image_url=_image(item.get("image")),
                )

    def _search_dom(self, soup, capture):
        prices_by_uuid: dict[str, dict] = {}
        for payload in _captured(capture, "product-buy"):
            data = payload.get("data") if isinstance(payload, dict) else None
            for state in _rows(data.get("states") if isinstance(data, dict) else None):
                item = state.get("data") if isinstance(state, dict) else None
                if isinstance(item, dict) and (uuid := _text(item.get("id"))):
                    prices_by_uuid[uuid] = item
        for card, anchor in self._tile_nodes(soup):
            item = prices_by_uuid.get(card.get("data-product"), {})
            title = self._tile_title(card, anchor) or _text(item.get("name"))
            if not title:
                continue
            listing = self._dom_listing(card, anchor["href"], title)
            prices = item.get("price")
            if isinstance(prices, dict):
                listing.price = _price(prices.get("current")) or listing.price
                listing.original_price = _price(prices.get("previous")) or _price(prices.get("old")) or listing.original_price
            yield listing

    def _dom_details(self, node, listing, *, is_card):
        listing.sku = _text(node.get("data-code")) or urlsplit(listing.url).path.split("/")[2]
        status = node.get("data-avail-status")
        if status in {"now", "today", "tomorrow", "later"}:
            listing.availability = "in_stock"
        elif status == "out_of_stock":
            listing.availability = "out_of_stock"
        if is_card:
            for spec in node.select(".product-characteristics__spec"):
                name = _value(spec, (".product-characteristics__spec-title", ".product-characteristics__spec-name", "dt"))
                value = _value(spec, (".product-characteristics__spec-value", "dd"))
                if name and value:
                    listing.specifications[name] = value
