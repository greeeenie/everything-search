"""Conservative public structured-data extraction, never guessed snippet prices."""

import hashlib
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .models import Evidence, Offer, Product
from .network import AccessError, host_matches, validate_url


def normalize(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])", " ", value)
    value = re.sub(r"(?<=\d),(?=\d)", ".", value)
    return " ".join(re.findall(r"[\w]+(?:\.[\d]+)?", value))


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


class MarketplaceAdapter:
    name = "public_web"
    domain = ""

    def is_offer_url(self, url):
        return True

    def parse(self, page, requested_model=None):
        if page.status != "verified" or not self.is_offer_url(page.final_url or page.url):
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
                for pattern, label in (
                    (
                        r"(?:по|с|оплат\w*)\s+(?:банковск\w*\s+)?карт\w*|ozon\s*карт|карт\w*\s*(?:ozon|озон)|с\s+ozon",
                        "bank_card",
                    ),
                    (r"(?:с\s+подписк|для\s+подписчик|яндекс\s*плюс|yandex\s*plus)", "subscription"),
                    (r"промокод|купон|cashback|кэшбэк", "coupon_or_cashback"),
                ):
                    if re.search(pattern, page.text.casefold()):
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
                variant_id = parse_qs(urlsplit(source_url).query).get("variant", [None])[0]
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

    def is_offer_url(self, url):
        return bool(re.search(r"/[^/?]+_\d{5,}/?(?:\?|$)", url))


class YandexMarketAdapter(MarketplaceAdapter):
    name, domain = "yandex_market", "market.yandex.ru"

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
