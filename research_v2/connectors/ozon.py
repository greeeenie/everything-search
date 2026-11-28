"""Ozon catalog and product data from public widgets and rendered pages."""

import re
from time import monotonic
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..fetching import extract
from ..models import Page
from ..shopping import OzonAdapter, has_type, product_matches
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
    mapping,
    market_url,
    merge_listings,
    observed_stock,
    priced_tiles,
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

_ORIGIN = "https://www.ozon.ru"
_PATH = r"^/product/[^/]*[0-9]/?$"
_CAPTURE_PATTERNS = ["entrypoint-api.bx/page/json/v2", "composer-api.bx"]
_CARD_PRICE = re.compile(r"с\s+(?:ozon\s+)?карт|ozon\s*карт|озон\s*карт|cardprice", re.I)
_REGULAR_PRICE = re.compile(r"без\s+(?:ozon\s+)?карт|обычная цена|без озон", re.I)
_DOM_BADGE = re.compile(
    r"ч[её]рная пятница|распродажа|хит|акция|ozon|в корзину|нет в наличии|раскупили"
    r"|\d+\s*(?:ед\.?\s*осталось|отзыв\w*|оцен\w*|%|[а-я]+)",
    re.I,
)
_DOM_SCORE = re.compile(r"[0-5][.,]\d")
_PRODUCT_LINK = 'a[href*="/product/"]'


def _url(value, *, keep_query=False):
    if sku := id_text(value):
        return f"{_ORIGIN}/product/{sku}/"
    return market_url(value, _ORIGIN, "ozon.ru", _PATH, keep_query=keep_query)


def _sku(url):
    match = re.search(r"(\d+)/?$", urlsplit(url).path)
    return match[1] if match else None


def _price_fields(node):
    public = rub(node.get("priceWithoutCard") or node.get("regularPrice") or node.get("price"))
    conditional = rub(node.get("cardPrice") or node.get("priceWithCard") or node.get("ozonCardPrice"))
    original = rub(node.get("originalPrice") or node.get("oldPrice"))
    entries = node.get("prices") or node.get("price")
    for entry in rows(entries):
        caption = " ".join(filter(None, (label(entry.get(k)) for k in ("label", "description", "subtitle", "type", "style", "textStyle"))))
        role = caption.upper().removeprefix("PRICE_")
        amount = rub(entry.get("text") or entry.get("price") or entry.get("value"))
        if amount is None:
            continue
        if _REGULAR_PRICE.search(caption) or role in {"SECONDARY", "REGULAR", "WITHOUT_CARD"}:
            public = amount
        elif _CARD_PRICE.search(caption) or role in {"PRIMARY", "CARD", "OZON_CARD"} or entry.get("isCardPrice") is True:
            conditional = amount
        elif re.search(r"original|old|crossed|strikethrough", caption, re.I):
            original = amount
        elif public is None:
            # An unlabelled atom can only be used when it isn't one of several prices.
            if isinstance(entries, dict) or (isinstance(entries, list) and len(entries) == 1):
                public = amount
    return public, conditional, original


def _rating_fields(atoms):
    stars, reviews = None, None
    for node in json_objects(atoms):
        text = label(node.get("text"))
        if not text:
            continue
        if re.search(r"отзыв|оцен", text, re.I):
            reviews = count(text) if reviews is None else reviews
        elif stars is None:
            stars = rating(text)
    return stars, reviews


def _widgets(payloads, prefix):
    for node in json_objects(payloads):
        for key, value in node.items():
            if key.casefold().startswith(prefix.casefold()):
                yield from json_objects(value)


def _widget_stock(item):
    stock = availability(item.get("isAvailable", item.get("availability")))
    if stock != "unknown":
        return stock
    if item.get("isOutOfStock") is True or item.get("soldOut") is True:
        return "out_of_stock"
    purchase_button = False
    for node in json_objects(item):
        text = label(node.get("text") or node.get("title") or node.get("label"))
        if text and re.search(r"нет в наличии|товар закончился", text, re.I):
            return "out_of_stock"
        action = label(node.get("actionType") or node.get("type"))
        if node.get("disabled") is not True and (
            (action and action.casefold() in {"addtocart", "add_to_cart", "buy"})
            or (text and re.fullmatch(r"в корзину|купить", text, re.I))
        ):
            purchase_button = True
    return "in_stock" if purchase_button else "unknown"


def _widget_tiles(capture, model):
    listings = []
    for item in json_objects(capture.payloads):
        state = item.get("mainState")
        if not isinstance(state, list):
            continue
        url = _url(mapping(item.get("action")).get("link") or item.get("link") or item.get("url"))
        if not url:
            continue
        title = label(item.get("title") or item.get("name"))
        public, conditional, original, stars, reviews = None, None, None, None, None
        for atom in rows(state):
            atom_data = mapping(atom.get("atom")) or atom
            text_atom = mapping(atom_data.get("textAtom"))
            if text_atom and (not title or atom.get("id") == "name" or text_atom.get("id") == "name"):
                title = label(text_atom.get("text")) or title
            if price_atom := mapping(atom_data.get("priceV2")):
                public, conditional, original = _price_fields(price_atom)
            if atom_data.get("labelList"):
                stars, reviews = _rating_fields(atom_data["labelList"])
        if not title:
            continue
        images = item.get("images") or item.get("image") or mapping(item.get("tileImage")).get("items")
        listings.append(make_product_and_offer(
            "ozon", url, title, public, model=model, sku=_sku(url), availability=_widget_stock(item),
            conditional_price=conditional, original_price=original,
            price_conditions=["bank_card"] if conditional else [], rating=stars, reviews_count=reviews,
            image_url=image_url(images, url), http_status=capture.http_status,
        ))
    return listings


def _dom_prices(tile):
    public = conditional = None
    for node in tile.select('[data-price-type], [data-widget="webPrice"] span, ins, [class*="price"]'):
        text = node.get_text(" ", strip=True)
        caption = " ".join((node.get("aria-label", ""), node.get("data-price-type", ""), text))
        if _REGULAR_PRICE.search(caption) or node.get("data-price-type") in {"regular", "public"}:
            public = public or rub(text)
        elif _CARD_PRICE.search(caption) or node.get("data-price-type") == "card":
            conditional = conditional or rub(text)
    text = tile.get_text(" ", strip=True)
    amount = rub(attr_text(tile, '[itemprop="price"]') or attr_text(tile, "ins"))
    if conditional is None and public is None and amount:
        if _CARD_PRICE.search(text) and not _REGULAR_PRICE.search(text):
            conditional = amount
        elif not _CARD_PRICE.search(text):
            public = amount
    if not (public or conditional) and not _CARD_PRICE.search(text):
        public = rub(text, require_currency=True)
    return public, conditional, rub(attr_text(tile, "del, s"))


def _dom_title(text):
    return bool(text and len(text) >= 8 and "₽" not in text and "|" not in text
                and not _DOM_BADGE.fullmatch(text) and re.search(r"[^\W\d_]", text))


def _search_dom_prices(tile, segments):
    public, conditional, original = _dom_prices(tile)
    amounts = [rub(match[1]) for match in TILE_RUB_PRICE.finditer(tile.get_text(" ", strip=True))]
    amounts = [amount for amount in amounts if amount is not None]
    if not (public or conditional):
        for index, segment in enumerate(segments):
            caption = _CARD_PRICE.search(segment)
            if not caption or _REGULAR_PRICE.search(segment):
                continue
            closest = min(
                TILE_RUB_PRICE.finditer(segment),
                key=lambda amount: max(caption.start() - amount.end(), amount.start() - caption.end(), 0),
                default=None,
            )
            conditional = rub(closest[1]) if closest else None
            if conditional is None and index:
                conditional = rub(segments[index - 1], require_currency=True)
            if conditional is None and index + 1 < len(segments):
                conditional = rub(segments[index + 1], require_currency=True)
            if conditional is not None:
                break
    if public is None:
        public = next((amount for amount in amounts if amount != conditional and amount != original), None)
    if original is None and public is not None:
        original = next((amount for amount in amounts if amount > public and amount != conditional), None)
    return public, conditional, original


def _dom_rating(tile, segments):
    stars = rating(attr_text(tile, '[itemprop="ratingValue"]'))
    reviews = count(attr_text(tile, '[itemprop="reviewCount"]'))
    if reviews is None:
        reviews = next((count(text) for text in segments if re.search(r"отзыв|оцен", text, re.I) and count(text) is not None), None)
    for index, text in enumerate(segments):
        if _DOM_SCORE.fullmatch(text) and (score := rating(text)) is not None:
            stars = stars or score
            if reviews is None and index + 1 < len(segments):
                reviews = count(segments[index + 1])
            break
    return stars, reviews


def _parse_dom(capture, model):
    listings = {}
    tiles = priced_tiles(capture.soup, _PRODUCT_LINK, lambda href: _sku(_url(href) or ""), max_parents=6)
    for sku, tile, links in tiles:
        if sku in listings:
            continue
        segments = dom_segments(tile)
        title = max((text for link in links if _dom_title(text := link.get_text(" ", strip=True))), key=len, default=None)
        if not title:
            metadata = (attr_text(tile, 'h2, h3, [itemprop="name"], [data-testid*="title"]'),
                        *(label(link.get("aria-label") or link.get("title")) for link in links),
                        attr_text(tile, "img", "alt"))
            title = next((text for text in metadata if _dom_title(text)), None)
        if not title:
            title = max(filter(_dom_title, segments), key=len, default=None)
        if not title:
            continue
        url = _ORIGIN + urlsplit(_url(links[0]["href"])).path.rstrip("/") + "/"
        public, conditional, original = _search_dom_prices(tile, segments)
        stock = observed_stock(tile)
        if re.search(r"нет в наличии|раскупили", " ".join(segments), re.I):
            stock = "out_of_stock"
        elif stock == "unknown" and (public or conditional):
            stock = "in_stock"
        stars, reviews = _dom_rating(tile, segments)
        listings[sku] = make_product_and_offer(
            "ozon", url, title, public, model=model, sku=sku, availability=stock,
            conditional_price=conditional, original_price=original, price_conditions=["bank_card"] if conditional else [],
            rating=stars, reviews_count=reviews, image_url=image_url(attr_text(tile, "img", "src"), url),
            http_status=capture.http_status,
        )
    return list(listings.values())


def _search_listings(connector, capture, model):
    if capture.error:
        return []
    structured = []
    for node in schema_products(capture.schema):
        raw = next(iter(rows(node.get("offers"))), {})
        if (title := label(node.get("name"))) and (url := _url(node.get("url") or raw.get("url"))):
            structured.append(schema_listing(connector, {**node, "name": title}, url, model,
                                             http_status=capture.http_status))
    listings = merge_listings(_widget_tiles(capture, model), structured, key=lambda offer: _sku(offer.url))
    return merge_listings(listings, _parse_dom(capture, model), key=lambda offer: _sku(offer.url), clarify_price=True)


def _card_listings(capture, url, model):
    if capture.error:
        return []
    title, text, nodes = extract(str(capture.soup), url)
    page = Page(url=url, final_url=url, title=title, text=text, structured_data=nodes,
                status="verified", source="native:ozon", region="RU", http_status=capture.http_status)
    products, offers = OzonAdapter().parse(page)
    product_by_id = {product.id: product for product in products}
    schema = next((node for node in schema_products(nodes)
                   if label(node.get("name")) and _sku(_url(node.get("url")) or url) == _sku(url)), {})
    heading = attr_text(capture.soup, "h1") or label(schema.get("name"))
    if not heading:
        heading = next((label(node.get("title") or node.get("name")) for node in _widgets(capture.payloads, "webProductHeading")
                        if label(node.get("title") or node.get("name"))), None)
    if not heading:
        return []
    public = conditional = original = None
    stock = "unknown"
    for node in _widgets(capture.payloads, "webPrice"):
        public, conditional, original = _price_fields(node)
        stock = availability(node.get("isAvailable", node.get("availability")))
        if public or conditional or stock != "unknown":
            break
    seller = next((name for node in _widgets(capture.payloads, "webCurrentSeller")
                   if (name := label(node.get("sellerName") or node.get("seller") or node.get("name") or node.get("title")))), None)
    specs = {}
    for node in _widgets(capture.payloads, "webCharacteristics"):
        for key in ("characteristics", "shortCharacteristics", "items", "properties"):
            specs.update(specifications(node.get(key)))
    for group in capture.soup.select("[data-widget='webCharacteristics'] dl"):
        for name in group.select("dt"):
            if value := name.find_next_sibling("dd"):
                specs.setdefault(name.get_text(" ", strip=True), value.get_text(" ", strip=True))
    aggregate = mapping(schema.get("aggregateRating"))
    stars = rating(aggregate.get("ratingValue"))
    reviews_count = count(aggregate.get("reviewCount", aggregate.get("ratingCount")))
    for node in _widgets(capture.payloads, "webReview"):
        stars = stars or rating(node.get("rating") or node.get("score"))
        if reviews_count is None:
            reviews_count = count(node.get("reviewsCount", node.get("reviewCount")))
    listing = make_product_and_offer(
        "ozon", url, heading, public, model=model, brand=label(schema.get("brand")), sku=_sku(url),
        availability=stock, conditional_price=conditional, original_price=original,
        price_conditions=["bank_card"] if conditional else [], seller=seller, specifications=specs,
        rating=stars, reviews_count=reviews_count, image_url=image_url(schema.get("image"), url),
        http_status=capture.http_status,
    )
    structured = []
    for offer in offers:
        if _sku(offer.url) != _sku(url):
            continue
        if offer.currency not in {None, "RUB"}:
            offer.price, offer.currency, offer.price_scope = None, None, "unknown"
        if offer.price_scope == "conditional":
            if "bank_card" in offer.price_conditions:
                offer.conditional_price = offer.price
            else:
                offer.price, offer.currency, offer.price_scope = None, None, "unknown"
        if conditional is not None and offer.price == conditional and public != conditional:
            offer.conditional_price, offer.price_scope, offer.price_conditions = conditional, "conditional", ["bank_card"]
        structured.append((product_by_id[offer.product_id], offer))
    dom = capture.soup.select_one('[data-widget="webPrice"]')
    dom_stock = observed_stock(capture.soup)
    dom_public, dom_card, dom_old = _dom_prices(dom) if dom else (None, None, None)
    fallback = make_product_and_offer(
        "ozon", url, heading, dom_public, model=model, sku=_sku(url), availability=dom_stock,
        conditional_price=dom_card, original_price=dom_old,
        price_conditions=["bank_card"] if dom_card else [], http_status=capture.http_status,
    )
    return merge_listings(merge_listings([listing], structured, key=lambda offer: _sku(offer.url)),
                          [fallback], key=lambda offer: _sku(offer.url))


def _reviews(capture, url, model):
    schema_reviews = []
    for node in schema_products(capture.schema):
        product_url = _url(node.get("url"))
        name = label(node.get("name"))
        brand = label(node.get("brand"))
        if product_url and _sku(product_url) != _sku(url):
            continue
        if name and (product_matches(model, name) or (brand and product_matches(model, f"{brand} {name}"))):
            schema_reviews.extend(rows(node.get("review")))
    review_path = urlsplit(url).path.rstrip("/") + "/reviews"
    for node in capture.schema:
        for review in rows(node):
            if not has_type(review, "Review"):
                continue
            subject = mapping(review.get("itemReviewed"))
            subject_url = _url(subject.get("url"))
            if (subject_url and _sku(subject_url) == _sku(url)) or (
                not subject and urlsplit(capture.url).path.rstrip("/") == review_path
            ):
                schema_reviews.append(review)
    reviews = []
    for node in json_objects([schema_reviews, capture.payloads]):
        if not has_type(node, "Review") and not (
            any(key in node for key in ("reviewText", "advantages", "disadvantages", "pros", "cons"))
            and any(key in node for key in ("rating", "score", "createdAt", "publishedAt"))
        ):
            continue
        product_id = id_text(node.get("productId") or node.get("sku"))
        if product_id and product_id != _sku(url):
            continue
        reviews.append(review_record(
            "ozon", url, model, text=node.get("reviewBody") or node.get("reviewText") or node.get("text") or node.get("comment"),
            pros=node.get("pros") or node.get("advantages"), cons=node.get("cons") or node.get("disadvantages"),
            stars=node.get("reviewRating") or node.get("rating") or node.get("score"),
            published_at=node.get("datePublished") or node.get("publishedAt") or node.get("createdAt"),
        ))
    return unique_reviews(reviews)


class OzonConnector(MarketplaceConnector):
    name = "ozon"
    domain = "ozon.ru"
    supports_search = True
    supports_reviews = True

    async def search(self, query, *, model=None, limit=12, city=None):
        start = monotonic()
        url = _ORIGIN + "/search/?" + urlencode({"text": query, "from_global": "true"})
        capture = await fetch_capture(
            self.transport.cdp_fetch, url, wait_selector="[data-widget*='searchResults'], a[href*='/product/']",
            wait_ms=3000, scroll_steps=2, capture_json_patterns=_CAPTURE_PATTERNS,
            user_agent_compat="windows", timeout_s=40,
        )
        listings = _search_listings(self, capture, model)
        if listings:
            return search_result(self, listings, model, limit, start)
        primary = capture.error
        capture = await fetch_capture(self.transport.camoufox_fetch, url, region="RU", allow_search=True,
                                      capture_json_patterns=_CAPTURE_PATTERNS)
        return search_result(self, _search_listings(self, capture, model), model, limit, start,
                             error=capture.error, previous=primary, fallback=True)

    async def get_card(self, url_or_sku, *, model=None):
        url = _url(url_or_sku, keep_query=True)
        if not url:
            return ConnectorCardResult(error="invalid_card_url_or_sku")
        patterns = _CAPTURE_PATTERNS[:1]
        capture = await fetch_capture(self.transport.cdp_fetch, url, wait_selector="h1",
                                      capture_json_patterns=patterns, user_agent_compat="windows")
        listings = _card_listings(capture, url, model)
        if listings:
            return card_result(listings, model)
        capture = await fetch_capture(self.transport.camoufox_fetch, url, region="RU", allow_search=False,
                                      capture_json_patterns=patterns)
        return card_result(_card_listings(capture, url, model), model, error=capture.error)

    async def get_reviews(self, model, *, limit=10):
        if limit <= 0:
            return [], []
        result = await self.search(model, model=model, limit=2)
        if not result.offers:
            return [], result.errors or [source_error(self.name, "matching_listing_unavailable", "search")]
        url = result.offers[0].url
        target = urlsplit(url)
        reviews_url = urlunsplit((target.scheme, target.netloc, target.path.rstrip("/") + "/reviews/", "", ""))
        for address in (reviews_url, url):
            capture = await fetch_capture(self.transport.cdp_fetch, address, user_agent_compat="windows",
                                          capture_json_patterns=_CAPTURE_PATTERNS)
            if not capture.error and (reviews := _reviews(capture, url, model)):
                return reviews[:limit], []
            capture = await fetch_capture(self.transport.camoufox_fetch, address, region="RU", allow_search=False,
                                          capture_json_patterns=_CAPTURE_PATTERNS)
            if not capture.error and (reviews := _reviews(capture, url, model)):
                return reviews[:limit], []
        return [], [source_error(self.name, capture.error or "reviews_unavailable", "reviews")]
