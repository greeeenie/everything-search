"""Conservative public structured-data extraction, never guessed snippet prices."""

import hashlib
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .models import Evidence, Offer, Product
from .network import AccessError, host_matches, validate_url


def normalize(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])", " ", value)
    value = re.sub(r"(?<=\d),(?=\d)", ".", value)
    words = re.findall(r"[\w]+(?:\.[\d]+)?", value)
    return " ".join(word for word in words if word not in {"projector", "проектор"})


def identity(name, brand=None, model=None, sku=None):
    """Explicit model wins; otherwise require exact title (safe under-merging)."""
    base = normalize(" ".join(str(x) for x in (brand, model) if x)) if model else normalize(name)
    # SKU is shop-local, never a cross-market product identity.
    return hashlib.sha256(base.encode()).hexdigest()[:20]


def exact_model_in(model, text):
    needle, haystack = normalize(model), normalize(text)
    return bool(needle and re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack))


def model_matches(model, text):
    """Reject nearby model numbers and added Pro/Max/Plus/Ultra suffixes."""
    if not exact_model_in(model, text):
        return False
    needle, haystack = normalize(model), normalize(text)
    suffix = haystack.split(needle, 1)[1].strip().split()
    return not suffix or suffix[0] not in {"pro", "max", "plus", "ultra", "mini", "lite"}


def product_matches(model, name):
    if not model_matches(model, name):
        return False
    needle, text = normalize(model), normalize(name)
    prefix, suffix = text.split(needle, 1)
    accessories = {"case", "cover", "remote", "mount", "чехол", "пульт", "кабель", "крепление", "адаптер"}
    bundles = {"kit", "bundle", "комплект", "набор"}
    requested = set(needle.split())
    if (set(prefix.split()) & accessories) - requested:
        return False
    if (set(suffix.split()) & (accessories | bundles)) - requested:
        return False
    return True


def objects(nodes):
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield node
        graph = node.get("@graph")
        if isinstance(graph, list):
            yield from objects(graph)
        if isinstance(node.get("mainEntity"), dict):
            yield from objects([node["mainEntity"]])
        variants = node.get("hasVariant", [])
        if isinstance(variants, dict):
            variants = [variants]
        if isinstance(variants, list):
            yield from objects(variants)


def has_type(node, kind):
    kinds = node.get("@type") or []
    if not isinstance(kinds, (str, list)):
        return False
    return kind in ([kinds] if isinstance(kinds, str) else kinds)


def money(value):
    if isinstance(value, (dict, list, bool)) or value is None:
        return None
    try:
        parsed = Decimal(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
        return parsed if parsed.is_finite() and 0 < parsed < Decimal("1e12") else None
    except InvalidOperation:
        return None


def title_color(title):
    text = title.casefold().replace("ё", "е")
    match = re.search(r"(?<!\w)(?:бежево[-\s]зеленый|темно[-\s]синий|белый|черный|синий|зеленый)(?!\w)", text)
    return match[0].replace(" ", "-") if match else None


class MarketplaceAdapter:
    name = "public_web"
    domain = ""

    def search_url(self, query):
        return None

    def public_products(self, soup, url):
        return []

    def is_offer_url(self, url):
        return True

    def is_product_page(self, url):
        return self.is_offer_url(url)

    def parse(self, page, requested_model=None):
        if page.status != "verified" or not self.is_product_page(page.final_url or page.url):
            return [], []
        products, offers = [], []
        for node in objects(page.structured_data):
            if not has_type(node, "Product") or not node.get("name"):
                continue
            name = str(node["name"])
            brand = node.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            if not isinstance(brand, str):
                brand = None
            match_name = name
            if brand and not normalize(name).startswith(normalize(brand)):
                match_name = brand + " " + name
            if requested_model and not product_matches(requested_model, match_name):
                continue
            model = node.get("model")
            if isinstance(model, dict):
                model = model.get("name")
            if not isinstance(model, str):
                model = None
            # Exact-query matching is not fuzzy: Cube 2 Pro cannot become Cube 1/X5 Pro.
            normalized_model = requested_model or model
            pid = identity(requested_model or name) if requested_model else identity(name, brand, model)
            evidence = Evidence(**{k: getattr(page, k) for k in Evidence.model_fields})
            evidence.excerpts = [f"Product.name={name}; model={model or 'not supplied'}"]
            if node.get("_extraction"):
                evidence.excerpts.append("Extraction=" + str(node["_extraction"]))
            product = Product(
                id=pid,
                name=requested_model or name,
                brand=brand,
                model=normalized_model,
                identity_basis="exact query in structured Product.name"
                if requested_model
                else "explicit model or exact title",
                sources=[evidence],
            )
            props = node.get("additionalProperty", [])
            if isinstance(props, dict):
                props = [props]
            for prop in props if isinstance(props, list) else []:
                if isinstance(prop, dict) and prop.get("name") and prop.get("value") is not None:
                    product.specifications[str(prop["name"])[:150]] = str(prop["value"])[:500]
            variants = node.get("hasVariant", [])
            if isinstance(variants, dict):
                variants = [variants]
            product.variants = [
                str(x["name"])[:300] for x in variants if isinstance(x, dict) and x.get("name")
            ]
            products.append(product)
            raw_offers = node.get("offers", [])
            if isinstance(raw_offers, dict):
                raw_offers = [raw_offers]
            for raw in raw_offers if isinstance(raw_offers, list) else []:
                if not isinstance(raw, dict) or has_type(raw, "AggregateOffer"):
                    continue  # lowPrice is not a specific seller offer.
                source_url = urljoin(
                    page.final_url or page.url, str(raw.get("url") or page.final_url or page.url)
                )
                try:
                    validate_url(source_url)
                except AccessError:
                    continue
                if self.domain and not host_matches(urlsplit(source_url).hostname or "", self.domain):
                    continue
                if not self.is_offer_url(source_url):
                    continue
                seller = raw.get("seller")
                seller = (
                    seller.get("name")
                    if isinstance(seller, dict)
                    else seller
                    if isinstance(seller, str)
                    else None
                )
                price = money(raw.get("price"))
                currency = raw.get("priceCurrency")
                currency = (
                    currency.upper()
                    if isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3}", currency)
                    else None
                )
                if not currency:
                    price = None
                # If the page advertises card/subscription/coupon pricing, conservatively
                # mark all unlabeled structured prices conditional rather than assume public.
                conditions = []
                price_context = str(raw.get("_price_context", page.text)).casefold()
                for pattern, label in (
                    (
                        r"(?:по|с|оплат\w*)\s+(?:банковск\w*\s+)?карт\w*|ozon\s*карт|карт\w*\s*(?:ozon|озон)|с\s+ozon",
                        "bank_card",
                    ),
                    (r"(?:с\s+подписк|для\s+подписчик|яндекс\s*плюс|yandex\s*plus)", "subscription"),
                    (r"промокод|купон|cashback|кэшбэк", "coupon_or_cashback"),
                ):
                    if re.search(pattern, price_context):
                        conditions.append(label)
                price_spec = raw.get("priceSpecification", {})
                if isinstance(price_spec, dict) and any(
                    price_spec.get(k)
                    for k in ("eligibleCustomerType", "membershipPointsEarned", "validForMemberTier")
                ):
                    conditions.append("membership")
                condition = str(raw.get("itemCondition") or node.get("itemCondition") or "").rsplit("/", 1)[
                    -1
                ]
                availability = str(raw.get("availability") or "").rsplit("/", 1)[-1]
                oe = evidence.model_copy(deep=True)
                # Preserve the actually fetched URL; the offer link can carry a seller ID.
                oe.excerpts.append(
                    f"Offer.url={source_url}; seller={seller}; price={price}; currency={currency}; availability={availability}; condition={condition}"
                )
                offers.append(
                    Offer(
                        url=source_url,
                        product_id=pid,
                        title=name,
                        marketplace=self.name,
                        seller=seller,
                        price=price,
                        currency=currency,
                        price_conditions=conditions,
                        price_scope="conditional"
                        if conditions
                        else "public"
                        if price is not None
                        else "unknown",
                        condition={
                            "NewCondition": "new",
                            "UsedCondition": "used",
                            "RefurbishedCondition": "refurbished",
                        }.get(condition, "unknown"),
                        availability={
                            "InStock": "in_stock",
                            "LimitedAvailability": "in_stock",
                            "OutOfStock": "out_of_stock",
                            "SoldOut": "out_of_stock",
                            "PreOrder": "preorder",
                        }.get(availability, "unknown"),
                        delivery=raw.get("shippingDetails")
                        if isinstance(raw.get("shippingDetails"), dict)
                        else {},
                        evidence=oe,
                        variants={
                            k: str(node[k])
                            for k in ("color", "size", "material")
                            if isinstance(node.get(k), (str, int))
                        },
                    )
                )
                parameters = parse_qs(urlsplit(source_url).query)
                variant_id = parameters.get("variant", [None])[0]
                variant_label = page.variant_labels.get(variant_id, "") if variant_id else ""
                capacity_text = (
                    name
                    + " "
                    + str(raw.get("name", ""))
                    + " "
                    + str(raw.get("sku", ""))
                    + " "
                    + variant_label
                )
                capacities = re.findall(r"\b\d+(?:[.,]\d+)?\s*(?:gb|tb|гб|тб)\b", capacity_text.casefold())
                if capacities:
                    labels = [
                        normalize(capacity).replace("гб", "gb").replace("тб", "tb") for capacity in capacities
                    ]
                    offers[-1].variants["capacity_labels"] = " | ".join(dict.fromkeys(labels))
                elif variant_id:
                    offers[-1].variants["source_variant"] = (
                        (urlsplit(source_url).hostname or "") + ":" + variant_id
                    )
        return products, offers


class OzonAdapter(MarketplaceAdapter):
    name, domain = "ozon", "ozon.ru"

    def is_offer_url(self, url):
        return bool(re.search(r"/product/[^/]*\d+/?(?:\?|$)", url))


class AvitoAdapter(MarketplaceAdapter):
    name, domain = "avito", "avito.ru"

    def search_url(self, query):
        return "https://www.avito.ru/all?" + urlencode({"q": query})

    def is_product_page(self, url):
        return urlsplit(url).path.startswith("/all") or self.is_offer_url(url)

    def public_products(self, soup, url):
        products = []
        for card in soup.select('[itemscope][itemtype$="/Product"]')[:80]:
            title = card.select_one('[itemprop="name"]')
            anchor = card.select_one('a[data-marker="item-title"], a[itemprop="url"]')
            price = card.select_one('meta[itemprop="price"]')
            currency = card.select_one('meta[itemprop="priceCurrency"]')
            availability = card.select_one('meta[itemprop="availability"]')
            condition = card.select_one('meta[itemprop="itemCondition"]')
            if not title or not anchor or not price or not currency:
                continue
            target = urlsplit(urljoin(url, anchor.get("href", "")))
            offer_url = urlunsplit((target.scheme, target.netloc, target.path, "", ""))
            if not host_matches(target.hostname or "", self.domain) or not self.is_offer_url(offer_url):
                continue
            # Price and state come from this item's public microdata, never its neighbours.
            offer = {
                "@type": "Offer",
                "url": offer_url,
                "price": price.get("content"),
                "priceCurrency": currency.get("content"),
                "_price_context": card.get_text(" ", strip=True),
            }
            if availability:
                offer["availability"] = availability.get("content")
            if condition:
                offer["itemCondition"] = condition.get("content")
            shop = card.select_one('a[href^="/brands/"]')
            if shop:
                # Seller review counters belong to the seller, never the product.
                shop_copy = BeautifulSoup(str(shop), "html.parser")
                for rating in shop_copy.select(
                    '[data-marker*="rating"], [data-marker*="score"], [data-marker*="summary"]'
                ):
                    rating.decompose()
                label = shop_copy.get_text(" ", strip=True).strip(" ·")
                if label:
                    offer["seller"] = {"name": label[:200]}
            products.append(
                {
                    "@type": "Product",
                    "name": title.get_text(" ", strip=True),
                    "offers": offer,
                    "_extraction": "avito_public_microdata",
                }
            )
            color = title_color(title.get_text(" ", strip=True))
            if color:
                products[-1]["color"] = color
        return products

    def is_offer_url(self, url):
        return bool(re.search(r"/[^/?]+_\d{5,}/?(?:\?|$)", url))


class YandexMarketAdapter(MarketplaceAdapter):
    name, domain = "yandex_market", "market.yandex.ru"

    def search_url(self, query):
        return "https://market.yandex.ru/search?" + urlencode({"text": query})

    def is_product_page(self, url):
        return urlsplit(url).path == "/search" or self.is_offer_url(url)

    def public_products(self, soup, url):
        if urlsplit(url).path != "/search":
            heading = soup.find("h1")
            if heading and self.is_offer_url(url):
                return [
                    {
                        "@type": "Product",
                        "name": heading.get_text(" ", strip=True),
                        "_extraction": "yandex_public_heading",
                    }
                ]
            return []
        products = []
        for card in soup.select('[data-auto="searchOrganic"]')[:60]:
            title = card.select_one('[data-auto="snippet-title"]')
            price = card.select_one('[data-auto="snippet-price-current"]')
            anchor = title if title and title.name == "a" else title.find_parent("a") if title else None
            if not anchor and title:
                anchor = title.find("a")
            if not title or not price or not anchor or not anchor.get("href"):
                continue
            price_text = price.get_text(" ", strip=True)
            if "₽" not in price_text:
                continue
            match = re.search(r"\d[\d\s]*(?:[.,]\d{1,2})?", price_text)
            amount = money(re.sub(r"\s+", "", match[0])) if match else None
            if amount is None:
                continue
            target = urlsplit(urljoin(url, anchor["href"]))
            if target.hostname != self.domain:
                continue
            query = [
                (key, value)
                for key, value in parse_qsl(target.query)
                if key not in {"cpc", "sponsored", "ogV"}
            ]
            offer_url = urlunsplit((target.scheme, target.netloc, target.path, urlencode(query), ""))
            if not self.is_offer_url(offer_url):
                continue
            shop = card.select_one('[data-zone-name="shopInShop"]')
            cart = card.select_one('[data-auto="cartButton"]')
            available = bool(
                cart
                and not cart.has_attr("disabled")
                and cart.get("aria-disabled") != "true"
                and "в корзину" in normalize(cart.get("aria-label") or cart.get_text(" ", strip=True))
            )
            for hidden in card.select("script, style, noframes, template"):
                hidden.decompose()
            visible_text = card.get_text(" ", strip=True)
            offer = {
                "@type": "Offer",
                "url": offer_url,
                "price": str(amount),
                "priceCurrency": "RUB",
                "_price_context": visible_text,
                "_extraction": "yandex_public_search_dom",
            }
            if available:
                offer["availability"] = "https://schema.org/InStock"
            if shop:
                offer["seller"] = {"name": shop.get_text(" ", strip=True)[:200]}
            products.append(
                {
                    "@type": "Product",
                    "name": title.get_text(" ", strip=True),
                    "offers": offer,
                    "_extraction": "yandex_public_search_dom",
                }
            )
            color = title_color(title.get_text(" ", strip=True))
            if color:
                products[-1]["color"] = color
        return products

    def is_offer_url(self, url):
        return bool(re.search(r"/(?:product--[^/]+/\d+|card/[^/]+/\d+|offer/[^/?]+)", url))


ADAPTERS = [OzonAdapter(), AvitoAdapter(), YandexMarketAdapter()]


def adapter_for(url):
    host = (urlsplit(url).hostname or "").lower()
    adapter = next((a for a in ADAPTERS if host_matches(host, a.domain)), None)
    if adapter:
        return adapter
    adapter = MarketplaceAdapter()
    adapter.domain = host.removeprefix("www.")
    adapter.name = adapter.domain
    return adapter


def review_excerpts(page, model):
    if page.status != "verified":
        return []
    result = []
    for product in objects(page.structured_data):
        if not has_type(product, "Product") or not model_matches(model, str(product.get("name", ""))):
            continue
        reviews = product.get("review", [])
        if isinstance(reviews, dict):
            reviews = [reviews]
        for review in reviews if isinstance(reviews, list) else []:
            if not isinstance(review, dict) or not review.get("reviewBody"):
                continue
            result.append(
                {
                    "text": str(review["reviewBody"])[:1200],
                    "published_at": review.get("datePublished"),
                    "rating": review.get("reviewRating"),
                    "url": page.final_url or page.url,
                    "checked_at": page.checked_at,
                    "status": "verified",
                    "model": model,
                    "attribution": "review nested in matching schema.org Product",
                }
            )
    if not result:
        # General forums discuss several models. Return only model-local paragraphs,
        # explicitly as discussion fragments rather than attributed product reviews.
        soup = BeautifulSoup(page.html or "", "html.parser")
        blocks = page.paragraphs or [x.get_text(" ", strip=True) for x in soup.select("p, blockquote")]
        for block in blocks:
            if model_matches(model, block) and len(block) > len(model) + 30:
                result.append(
                    {
                        "text": block[:1200],
                        "url": page.final_url or page.url,
                        "checked_at": page.checked_at,
                        "status": "verified",
                        "model": model,
                        "attribution": "model-local discussion fragment; author/ownership unverified",
                    }
                )
    return result[:8]
