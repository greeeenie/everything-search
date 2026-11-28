"""Citilink search and product cards from Next.js state and public markup."""

from .dns import (
    _CdpMarketplaceConnector,
    _Listing,
    _captured,
    _count,
    _image,
    _json_script,
    _named,
    _objects,
    _price,
    _rating,
    _stock,
    _text,
    _value,
)


class CitilinkConnector(_CdpMarketplaceConnector):
    name = "citilink"
    domain = "citilink.ru"
    origin = "https://www.citilink.ru"
    product_path = r"/product/[^/]+/?"
    search_path = "/search/"
    search_parameter = "text"
    search_options = {
        "wait_selector": 'a[href*="/product/"], [data-meta-product-id]',
        "wait_ms": 3500, "wait_for_challenge": True, "scroll_steps": 1,
        "capture_json_patterns": ["graphql", "search"], "timeout_s": 40,
    }
    card_options = {
        "wait_selector": 'h1, [data-meta-price]', "capture_json_patterns": ["graphql"], "timeout_s": 40,
    }
    tile_selector = (
        '[data-meta-product-id], [data-meta-name="SnippetProductHorizontalLayout"], '
        '[data-meta-name="SnippetProductVerticalLayout"]'
    )
    link_selector = 'a[href*="/product/"]'
    title_selectors = (
        '[data-meta-name="Snippet__title"]', '[data-meta-name="SnippetProductTitle"]',
    ) + _CdpMarketplaceConnector.title_selectors
    price_selectors = (
        '[data-meta-price]', '[data-meta-name="Price"]', '[class*="current-price"]', '[class~="price"]',
    ) + _CdpMarketplaceConnector.price_selectors
    old_price_selectors = (
        '[data-meta-name="OldPrice"]', '[class*="old-price"]',
    ) + _CdpMarketplaceConnector.old_price_selectors
    rating_selectors = (
        '[data-meta-name="Rating"]', '[class~="rating"]',
    ) + _CdpMarketplaceConnector.rating_selectors
    review_selectors = (
        '[data-meta-name="ReviewsCount"]', '[class*="reviews-count"]',
    ) + _CdpMarketplaceConnector.review_selectors

    def _next_listings(self, data):
        for item in _objects(data):
            sku = _text(item.get("id") or item.get("productId"))
            title = _text(item.get("name") or item.get("title"))
            slug = _text(item.get("slug"))
            url = _text(item.get("url"))
            if not url and slug:
                url = slug if slug.startswith(("/", "https://", "http://")) else f"/product/{slug}/"
            price = _price(item.get("price"))
            club_price = _price(item.get("priceClub"))
            if not (sku and title and url) or (price is None and club_price is None):
                continue
            rating = item.get("rating")
            if isinstance(rating, dict):
                rating = rating.get("value")
            yield _Listing(
                url, title, sku=sku, price=price, conditional_price=club_price,
                price_conditions=["club_membership"] if club_price is not None else [],
                original_price=item.get("oldPrice"), brand=_named(item.get("brand")),
                availability=_stock(item.get("isAvailable", item.get("available"))),
                rating=_rating(rating), reviews_count=_count(item.get("reviewsCount")),
                image_url=_image(item.get("image") or item.get("images")),
            )

    def _structured(self, soup, capture, card_url):
        if card_url:
            yield from super()._structured(soup, capture, card_url)
        for script in soup.select('script[id="__NEXT_DATA__"]'):
            yield from self._next_listings(_json_script(script))
        for data in _captured(capture):
            yield from self._next_listings(data)
        if not card_url:
            yield from super()._structured(soup, capture, card_url)

    def _dom_details(self, node, listing, *, is_card):
        listing.sku = _text(node.get("data-meta-product-id")) or _value(
            node, ("[data-meta-product-id]", '[itemprop="sku"]'), "data-meta-product-id", "content",
        )
