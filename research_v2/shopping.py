"""Conservative public structured-data extraction, never guessed snippet prices."""

import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .discovery import canonical_url
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


_ALTERNATIVE = r"/|\b(?:or|или)\b"
_PAIRED_MODELS = re.compile(
    rf"(?<!\w)([^\W\d_]*\d+)\s*(?:{_ALTERNATIVE})\s*([^\W\d_]*\d+)(?![\w.])"
)
_ACCESSORIES = {
    "case", "cover", "remote", "mount", "чехол", "пульт", "кабель", "крепление", "адаптер",
    "пленка", "стекло", "ремешок", "накладка", "бампер", "зарядка", "блок питания", "фильтр",
    "насадка", "аккумулятор", "батарея", "сумка", "подставка", "кронштейн",
    "charger", "filter", "strap", "protector", "bag", "stand",
    "запасная", "запасной", "запасные", "запчасть", "запчасти", "сменные", "сменный", "сменная",
    "амбушюры", "амбушюр", "дуга", "оголовье", "шарнир", "деталь", "ремонт", "держатель", "переходник",
    "earpads", "earpad", "headband", "hinge", "cushion", "cushions", "replacement", "spare", "part", "parts",
}


def product_matches(model, name):
    if not model_matches(model, name):
        return False
    needle, text = normalize(model), normalize(name)
    prefix, suffix = re.split(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", text, maxsplit=1)
    if {"для", "for"}.intersection(prefix.split()[-3:]) - set(needle.split()):
        return False
    if not re.search(_ALTERNATIVE, model.casefold()):
        raw = unicodedata.normalize("NFKC", name).casefold()
        start = len(prefix.split())
        end = start + len(needle.split())
        for pair in _PAIRED_MODELS.finditer(raw):
            left = len(normalize(raw[:pair.end(1)]).split()) - 1
            right = len(normalize(raw[:pair.end(2)]).split()) - 1
            if start <= left < end or start <= right < end:
                return False
    bundles = {"kit", "bundle", "комплект", "набор"}
    clean_name = re.sub(r"\[[^\]]*\]", " ", name) if "[" not in model else name
    clean_parts = re.split(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", normalize(clean_name), maxsplit=1)
    check_prefix, check_suffix = (clean_parts[0], clean_parts[1]) if len(clean_parts) == 2 else (prefix, suffix)
    for extra, excluded in ((check_prefix, _ACCESSORIES), (check_suffix, _ACCESSORIES | bundles)):
        extra = " " + extra.replace("ё", "е").strip() + " "
        requested = " " + needle.replace("ё", "е") + " "
        if any(f" {word} " in extra and f" {word} " not in requested for word in excluded):
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


def available_priced_offer(offer):
    return (
        offer.evidence.status == "verified"
        and offer.availability == "in_stock"
        and offer.price is not None
        and offer.price.is_finite()
        and offer.price > 0
        and bool(offer.currency)
    )


def normalize_conditional_price(offer: Offer) -> Offer:
    amount = offer.conditional_price
    if amount is not None and amount.is_finite() and amount > 0 and (
        offer.price_scope == "conditional" or not offer.price or offer.price == amount
    ):
        return offer.model_copy(update={
            "price": offer.price or amount, "price_scope": "conditional", "currency": offer.currency or "RUB",
        })
    return offer


def offer_matches_model(model: str, product: Product, offer: Offer) -> bool:
    name = offer.title
    if product.brand and not normalize(name).startswith(normalize(product.brand)):
        name = f"{product.brand} {name}"
    return product_matches(model, name)


def offer_key(offer: Offer) -> tuple:
    return (
        canonical_url(offer.url), offer.seller, offer.currency, offer.price, offer.conditional_price,
        offer.price_scope, offer.condition, json.dumps(offer.variants, sort_keys=True),
    )


def merge_product(products: dict[str, Product], product: Product) -> None:
    if product.id not in products:
        products[product.id] = product.model_copy(deep=True)
        return
    current = products[product.id]
    current.sources.extend(source for source in product.sources if source not in current.sources)
    current.brand = current.brand or product.brand
    for key, value in product.specifications.items():
        if key in current.specification_conflicts:
            if value not in current.specification_conflicts[key]:
                current.specification_conflicts[key].append(value)
        elif key not in current.specifications:
            current.specifications[key] = value
        elif current.specifications[key] != value:
            current.specification_conflicts[key] = [current.specifications.pop(key), value]
    for key, values in product.specification_conflicts.items():
        conflicts = current.specification_conflicts.setdefault(key, [])
        if key in current.specifications:
            conflicts.append(current.specifications.pop(key))
        conflicts.extend(value for value in values if value not in conflicts)


def dedupe_reviews(reviews):
    unique = {}
    for review in reviews:
        key = (review.get("url"), review.get("text"))
        if not review.get("text"):
            key += (review.get("pros"), review.get("cons"))
        unique.setdefault(key, review)
    return list(unique.values())


def title_color(title):
    text = title.casefold().replace("ё", "е")
    match = re.search(r"(?<!\w)(?:бежево[-\s]зеленый|темно[-\s]синий|белый|черный|синий|зеленый)(?!\w)", text)
    return match[0].replace(" ", "-") if match else None


class MarketplaceAdapter:
    name = "public_web"
    domain = ""
    offer_path = ""

    def search_url(self, query):
        return None

    def public_products(self, soup, url):
        return []

    def is_offer_url(self, url):
        return not self.offer_path or bool(re.search(self.offer_path, urlsplit(url).path))

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
        return bool(re.search(r"/(?:product(?:--[^/]+)?/\d+|card/[^/]+/\d+|offer/[^/?]+)", url))


class WildberriesAdapter(MarketplaceAdapter):
    name, domain = "wildberries", "wildberries.ru"
    offer_path = r"/catalog/\d+/detail\.aspx/?$"


class AliExpressAdapter(MarketplaceAdapter):
    name, domain = "aliexpress", "aliexpress.ru"
    offer_path = r"/item/\d+"


class DnsAdapter(MarketplaceAdapter):
    name, domain = "dns", "dns-shop.ru"
    offer_path = r"/product/[a-zA-Z0-9_-]+"


class CitilinkAdapter(MarketplaceAdapter):
    name, domain = "citilink", "citilink.ru"
    offer_path = r"/product/[^/]+"


class MegamarketAdapter(MarketplaceAdapter):
    name, domain = "megamarket", "megamarket.ru"
    offer_path = r"/catalog/details/[^/]+"


class LamodaAdapter(MarketplaceAdapter):
    name, domain = "lamoda", "lamoda.ru"
    offer_path = r"/p/[a-zA-Z0-9_-]+"


class DetmirAdapter(MarketplaceAdapter):
    name, domain = "detsky_mir", "detmir.ru"
    offer_path = r"/product/index/id/\d+(?:/|$)"


ADAPTERS = [
    OzonAdapter(), AvitoAdapter(), YandexMarketAdapter(), WildberriesAdapter(), AliExpressAdapter(),
    DnsAdapter(), CitilinkAdapter(), MegamarketAdapter(), LamodaAdapter(), DetmirAdapter(),
]
MARKETPLACE_DOMAINS = {adapter.name: adapter.domain for adapter in ADAPTERS}


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
