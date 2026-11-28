import asyncio
from collections import defaultdict
from decimal import Decimal
import json

from .config import Settings
from .connectors import NativeMarketplaceHub
from .discovery import Discovery, canonical_url
from .fetching import Fetcher
from .models import Page, SearchHit, now
from .network import EgressRouter
from .shopping import (
    ADAPTERS, MARKETPLACE_DOMAINS, adapter_for, available_priced_offer, dedupe_reviews, merge_product,
    model_matches, normalize_conditional_price, offer_key, product_matches, review_excerpts,
)
from .store import Store


MARKETPLACE_ALIASES = {"wb": "wildberries", "yandex": "yandex_market", "detmir": "detsky_mir"}
FALLBACK_SOURCES = {"avito", "ozon", "yandex_market"}
FALLBACK_STATUSES = {"blocked", "timeout", "error", "unhealthy"}


class Engine:
    def __init__(
        self, settings=None, store=None, fetcher=None, discovery=None,
        marketplace_hub: NativeMarketplaceHub | None = None,
    ):
        self.settings = settings or Settings.load()
        self.store = store or Store(self.settings.database)
        self.fetcher = fetcher or Fetcher(self.settings, self.store)
        self.discovery = discovery or Discovery(self.settings, self.store)
        self.marketplace_hub = marketplace_hub
        if marketplace_hub is None and self.settings.native_connectors and fetcher is None and discovery is None:
            self.marketplace_hub = NativeMarketplaceHub(self.settings, self.store)

    async def read_page(self, url, region=None, dynamic=False, refresh=False):
        return (await self.fetcher.read(url, region, dynamic=dynamic, refresh=refresh)).model_dump(
            mode="json"
        )

    async def search_web(
        self, query, site=None, max_results=4, region=None, domains=None, dynamic=False, _progress=None
    ):
        result = _progress if _progress is not None else {}
        result.update(
            {
                "query": query,
                "site": site,
                "checked_at": now(),
                "provider": self.settings.discovery_backend,
                "pages_read": 0,
                "excluded_product_pages": 0,
                "results": [],
                "errors": [],
                "scope": "Search excerpts are indexed_only. A verified page confirms retrieval at checked_at.",
            }
        )
        hits, errors = await self.discovery.search(
            query, domains or ([site] if site else []), max_results, region=region
        )
        result["errors"] = errors

        async def read(hit):
            page = await self.fetcher.read(hit.url, region, dynamic=dynamic)
            adapter = adapter_for(page.final_url or hit.url)
            row = hit.model_dump(mode="json")
            row["fetch_status"] = page.status
            row["checked_at"] = page.checked_at
            row["region"] = page.region
            if page.status == "verified":
                row.update(
                    status="verified",
                    source="public_page",
                    discovery_source=hit.source,
                    http_status=page.http_status,
                    page_excerpts=page.excerpts,
                    final_url=page.final_url,
                )
                try:
                    products, offers = adapter.parse(page)
                    if products:
                        offers = [offer for offer in offers if available_priced_offer(offer)]
                        product_ids = {offer.product_id for offer in offers}
                        row["products"] = [
                            product.model_dump(mode="json") for product in products if product.id in product_ids
                        ]
                        row["offers"] = [offer.model_dump(mode="json") for offer in offers]
                except (ValueError, TypeError, KeyError):
                    row["product_extraction_error"] = "parsing_error"
            else:
                row["fetch_error"] = page.error
            result["pages_read"] += int(page.status == "verified")
            product_listing = adapter in ADAPTERS and adapter.is_offer_url(hit.url)
            if (product_listing or "products" in row) and not row.get("offers"):
                result["excluded_product_pages"] += 1
                error = row.get("fetch_error") or row.get("product_extraction_error")
                if error:
                    result["errors"].append({"url": hit.url, "error": error})
                return
            result["results"].append(row)

        await asyncio.gather(*(read(h) for h in hits))
        return result

    def _shop_targets(self, marketplaces):
        names = marketplaces or self.settings.enabled_marketplaces or list(self.settings.marketplaces)
        if not names and self.marketplace_hub is not None:
            names = sorted(self.marketplace_hub.known_marketplaces)
        if len(names) > 12:
            raise ValueError("At most twelve marketplaces")
        targets = []
        for name in names:
            source = MARKETPLACE_ALIASES.get(name, name)
            domain = self.settings.marketplaces.get(name, MARKETPLACE_DOMAINS.get(source, name))
            targets.append((source, domain))
        return targets

    async def search_products(
        self,
        query,
        model=None,
        marketplaces=None,
        min_price=None,
        max_price=None,
        currency=None,
        condition=None,
        city=None,
        max_results=9,
        region=None,
        dynamic=False,
        specifications=None,
    ):
        if (
            min_price is not None
            and max_price is not None
            and Decimal(str(min_price)) > Decimal(str(max_price))
        ):
            raise ValueError("min_price must be <= max_price")
        search_query = model or query
        if specifications:
            search_query += " " + " ".join(f"{k} {v}" for k, v in specifications.items())
        if not search_query.strip() or len(search_query.strip()) > 500 or not 1 <= max_results <= 20:
            raise ValueError("query must contain 1..500 characters; limit 1..20")
        targets = self._shop_targets(marketplaces)
        domains = [domain for _, domain in targets]
        hub_products, hub_offers, outcomes, errors = [], [], [], []
        fallback_sources = set()
        if self.marketplace_hub is not None:
            requested_domains = {}
            local_domains = set()
            known = self.marketplace_hub.known_marketplaces
            for source, domain in targets:
                if source in known:
                    requested_domains.setdefault(source, []).append(domain)
                elif connector := self.marketplace_hub.connector_for_url("https://" + domain):
                    requested_domains.setdefault(connector.name, []).append(domain)
                else:
                    local_domains.add(domain)
            if requested_domains:
                hub_products, hub_offers, outcomes, errors = await self.marketplace_hub.compare(
                    search_query.strip(), list(requested_domains), model=model, limit=max_results, city=city,
                )
            for outcome in outcomes:
                source = outcome["source"]
                code = outcome.get("error_code")
                # A hub deadline can interrupt the built-in Camoufox step before it reports its outcome.
                if (
                    source in FALLBACK_SOURCES and outcome["status"] in FALLBACK_STATUSES
                    and not outcome.get("fallback_used") and code != "circuit_open"
                    and not (code == "connector_timeout" and source in {"avito", "ozon"})
                ):
                    fallback_sources.add(source)
                    local_domains.update(requested_domains[source])
                    outcome["fallback_used"] = "research_v2"
            domains = [domain for domain in domains if domain in local_domains]

        hits = []
        if domains or self.marketplace_hub is None:
            hits, discovery_errors = await self.discovery.search(
                search_query, domains, max_results, exact=bool(model and not specifications), region=region,
            )
            errors.extend(discovery_errors)
        if model:
            from urllib.parse import unquote, urlsplit

            hits = [
                hit
                for hit in hits
                if product_matches(model, hit.title)
                or product_matches(model, unquote(urlsplit(hit.url).path).replace("-", " ").replace("_", " "))
            ]
        native_hits = []
        for domain in domains:
            adapter = adapter_for("https://" + domain)
            native_url = adapter.search_url(search_query)
            if adapter.name in self.settings.native_search_marketplaces and native_url:
                native_hits.append(
                    SearchHit(
                        url=native_url,
                        title=search_query,
                        source="marketplace_search",
                        status="unavailable",
                        error="not_fetched",
                        region=EgressRouter(self.settings).choose(native_url, region),
                    )
                )
        hits = native_hits + hits
        output = {
            "query": query,
            "model": model,
            "city": city,
            "checked_at": now(),
            "products": [],
            "offers": [],
            "candidates": [],
            "errors": errors,
            "limitations": [
                "Offers require verified in-stock availability, a positive price and currency.",
                "Prices and stock describe retrieved public pages at checked_at.",
                "Delivery to the requested city must be stated by the source to be confirmed.",
                "Missing structured product/offer data is reported, never filled from index prices.",
            ],
        }
        if outcomes:
            output["source_outcomes"] = outcomes
            output["limitations"][1] = "Prices and stock describe native marketplace or public page data at checked_at."
        products, offers = {}, {}
        condition_details_checked = 0
        filter_currency = (
            currency.upper()
            if currency
            else ("RUB" if min_price is not None or max_price is not None else None)
        )

        async def read(hit):
            adapter = adapter_for(hit.url)
            if hit.source != "marketplace_search" and not adapter.is_offer_url(hit.url):
                page = Page(
                    url=hit.url,
                    source="url_classification",
                    status="unavailable",
                    region=EgressRouter(self.settings).choose(hit.url, region),
                    error="not_product_offer_url",
                )
                return hit, page, ([], [])
            page = await self.fetcher.read(hit.url, region, dynamic=dynamic or adapter.name in fallback_sources)
            return hit, page, adapter_for(page.final_url or hit.url).parse(page, model)

        rows = await asyncio.gather(*(read(h) for h in hits), return_exceptions=True)
        hub_row = (None, None, (hub_products, hub_offers))
        for hit, row in [(None, hub_row), *zip(hits, rows)]:
            if isinstance(row, Exception):
                output["errors"].append({"url": hit.url, "error": "parsing_error"})
                continue
            _, page, (parsed_products, parsed_offers) = row
            if hit is not None:
                candidate = hit.model_dump(mode="json")
                candidate.update(
                    fetch_status=page.status, fetch_error=page.error, fetch_checked_at=page.checked_at
                )
                if hit.source == "marketplace_search":
                    candidate.update(status=page.status, error=page.error)
                if not parsed_products:
                    candidate["extraction_error"] = (
                        "model_mismatch_or_no_product_data" if page.status == "verified" else page.error
                    )
                output["candidates"].append(candidate)
            for product in parsed_products:
                merge_product(products, product)
            for offer in parsed_offers:
                if (
                    condition
                    and offer.condition == "unknown"
                    and offer.marketplace == "avito"
                    and condition_details_checked < self.settings.condition_detail_limit
                ):
                    condition_details_checked += 1
                    detail = await self.fetcher.read(offer.url, region, dynamic=True)
                    _, detail_offers = adapter_for(detail.final_url or offer.url).parse(
                        detail, model or offer.title
                    )
                    # An Avito ad's canonical path identifies the individual offer.
                    matching = next(
                        (
                            item
                            for item in detail_offers
                            if canonical_url(item.url) == canonical_url(offer.url)
                        ),
                        None,
                    )
                    if matching:
                        matching.product_id = offer.product_id
                        matching.seller = matching.seller or offer.seller
                        matching.variants = {**offer.variants, **matching.variants}
                        offer = matching
                    elif detail.status != "verified":
                        output["errors"].append({"url": offer.url, "error": detail.error})
                offer = normalize_conditional_price(offer)
                if not available_priced_offer(offer):
                    continue
                offer.delivery_city = city
                self.delivery_for_city(offer, city)
                if condition and offer.condition != condition:
                    continue
                if filter_currency and offer.currency != filter_currency:
                    continue
                if min_price is not None and (offer.price is None or offer.price < Decimal(str(min_price))):
                    continue
                if max_price is not None and (offer.price is None or offer.price > Decimal(str(max_price))):
                    continue
                if specifications and not all(
                    model_matches(str(value), str(products[offer.product_id].specifications.get(key, "")))
                    for key, value in specifications.items()
                ):
                    continue
                offers[offer_key(offer)] = offer
                evidence = offer.evidence.model_dump(mode="json")
                evidence["normalized_offer"] = offer.model_dump(mode="json")
                self.store.record(evidence)
        product_ids = {offer.product_id for offer in offers.values()}
        output["products"] = [
            product.model_dump(mode="json") for product in products.values() if product.id in product_ids
        ]
        output["offers"] = [x.model_dump(mode="json") for x in offers.values()]
        if condition:
            output["condition_details_checked"] = condition_details_checked
            output["condition_detail_limit"] = self.settings.condition_detail_limit
            output["limitations"].append(
                "Unknown Avito condition is checked on a bounded number of individual ads. "
                "Unconfirmed condition is excluded from condition-filtered results."
            )
        for product in output["products"]:
            if product["sources"]:
                self.store.record({**product["sources"][0], "normalized_product": product})
        return output

    @staticmethod
    def delivery_for_city(offer, city):
        if not city or not offer.delivery:
            return
        destination = offer.delivery.get("shippingDestination", {})
        region = destination.get("addressLocality") if isinstance(destination, dict) else None
        if not region or str(region).strip().casefold() != city.strip().casefold():
            return
        rate = offer.delivery.get("shippingRate", {})
        if not isinstance(rate, dict) or rate.get("currency") != offer.currency:
            return
        try:
            amount = Decimal(str(rate["value"]))
        except (ValueError, KeyError, ArithmeticError):
            return
        if amount.is_finite() and amount >= 0:
            offer.delivery_status = "confirmed"
            offer.shipping_cost = amount
            if offer.price is not None:
                offer.total_price = offer.price + amount

    async def compare_prices(
        self, model, marketplaces=None, city=None, condition=None, max_results=12, region=None
    ):
        result = await self.search_products(
            model,
            model=model,
            marketplaces=marketplaces,
            city=city,
            condition=condition,
            max_results=max_results,
            region=region,
        )
        groups = defaultdict(list)
        for offer in result["offers"]:
            key = (
                offer["product_id"],
                offer["currency"],
                offer["condition"],
                offer["evidence"]["region"],
                json.dumps(offer["variants"], ensure_ascii=False, sort_keys=True),
            )
            groups[key].append(offer)
        comparisons = []
        for (product_id, currency, state, region, variant), offers in groups.items():
            eligible = [
                o
                for o in offers
                if o["price_scope"] == "public"
                and o["availability"] == "in_stock"
                and o["total_price"] is not None
                and o["delivery_status"] == "confirmed"
                and state != "unknown"
            ]
            eligible.sort(key=lambda o: Decimal(o["total_price"]))
            public = self._public_prices(offers)
            conditional = self._conditional_prices(offers)
            best_price = public[0] if public else None
            best_conditional = conditional[0] if conditional else None
            if best_price and best_conditional and Decimal(best_conditional["effective_price"]) >= Decimal(
                best_price["price"]
            ):
                best_conditional = None
            comparisons.append(
                {
                    "product_id": product_id,
                    "currency": currency,
                    "condition": state,
                    "region": region,
                    "variants": json.loads(variant),
                    "offers": offers,
                    "best_offer": eligible[0] if eligible else None,
                    "best_price_offer": best_price,
                    "best_conditional_offer": best_conditional,
                    "ranking": "public in-stock total price including confirmed city delivery",
                    "limitation": None
                    if eligible
                    else "No offer has all fields needed for a confirmed total-price winner.",
                }
            )
        result["comparisons"] = comparisons
        verified = [
            offer for offer in result["offers"]
            if offer["evidence"]["status"] == "verified" and offer["availability"] == "in_stock"
        ]
        public = self._public_prices(verified)
        new = [
            offer for offer in verified
            if offer["condition"] == "new" or (
                offer["condition"] == "unknown" and offer["marketplace"] in MARKETPLACE_DOMAINS.keys() - {"avito"}
            )
        ]
        conditional = self._conditional_prices(offer for offer in new if offer["condition"] == "new")
        fields = (
            "marketplace", "title", "price", "conditional_price", "currency", "condition", "price_scope",
            "price_conditions", "seller", "rating", "reviews_count", "url",
        )
        best_used = min(
            (offer for offer in verified if offer["condition"] == "used" and offer["price"] is not None),
            key=lambda offer: Decimal(offer["price"]),
            default=None,
        )
        result["recommendations"] = {
            "best_new_public": next(iter(self._public_prices(new)), None),
            "best_new_conditional": conditional[0] if conditional else None,
            "best_used": best_used,
            "cheapest_overall_public": public[0] if public else None,
            "ranked_top_5": [{field: offer.get(field) for field in fields} for offer in public[:5]],
        }
        if len({offer["currency"] for offer in [*public, *conditional]}) > 1:
            result["limitations"].append(
                "Top-level recommendations sort the reported amounts without currency conversion. "
                "Use currency-specific comparison groups when comparing different currencies."
            )
        return result

    @staticmethod
    def _public_prices(offers):
        return sorted(
            (offer for offer in offers if offer["availability"] == "in_stock"
             and offer["price_scope"] == "public" and offer["price"] is not None),
            key=lambda offer: Decimal(offer["price"]),
        )

    @staticmethod
    def _conditional_prices(offers):
        priced = []
        for offer in offers:
            if offer["availability"] != "in_stock":
                continue
            amount = offer.get("conditional_price")
            if amount is None and offer["price_scope"] == "conditional":
                amount = offer["price"]
            if amount is not None:
                priced.append({**offer, "effective_price": amount})
        return sorted(priced, key=lambda offer: Decimal(offer["effective_price"]))

    @staticmethod
    def _card_matches(model, product):
        return product_matches(model, product.name) or bool(
            product.brand and product_matches(model, product.brand + " " + product.name)
        )

    async def get_product_details(self, url=None, model=None, region=None):
        if not url and not model:
            raise ValueError("Provide url or model")
        if url:
            products, offers, reviews, error = [], [], [], None
            if self.marketplace_hub is not None and self.marketplace_hub.connector_for_url(url):
                products, offers, reviews, error = await self.marketplace_hub.get_card(url, model=model)
                if model:
                    products = [p for p in products if self._card_matches(model, p)]
                    ids = {p.id for p in products}
                    offers = [offer for offer in offers if offer.product_id in ids]
            if products or offers:
                evidence = next((source for product in products for source in product.sources), None)
                if evidence is None and offers:
                    evidence = offers[0].evidence
                page = Page(**evidence.model_dump(), extractor="marketplace_hub") if evidence else Page(
                    url=url, status="unavailable", source="marketplace_hub", region="RU",
                    extractor="marketplace_hub", error="marketplace_evidence_unavailable",
                )
            else:
                reviews = []
                page = await self.fetcher.read(url, region, dynamic=True)
                products, offers = adapter_for(page.final_url or url).parse(page, model)
            offers = [normalize_conditional_price(offer) for offer in offers]
            offers = [offer for offer in offers if available_priced_offer(offer)]
            for offer in offers:
                self.store.record(
                    {
                        **offer.evidence.model_dump(mode="json"),
                        "normalized_offer": offer.model_dump(mode="json"),
                    }
                )
            for product in products:
                if product.sources:
                    self.store.record(
                        {
                            **product.sources[0].model_dump(mode="json"),
                            "normalized_product": product.model_dump(mode="json"),
                        }
                    )
            result = {
                "checked_at": now(),
                "products": [p.model_dump(mode="json") for p in products],
                "offers": [o.model_dump(mode="json") for o in offers],
                "page": page.model_dump(mode="json"),
                "extraction_error": None if products else "no_matching_structured_product",
            }
            if reviews:
                result["reviews"] = reviews
            if error is not None:
                result["marketplace_hub_error"] = error
            return result
        return await self.search_products(model, model=model, region=region)

    async def get_product_reviews(self, model, domains=None, max_results=8, region=None):
        async def web_reviews():
            hits, errors = await self.discovery.search(
                model + " отзывы опыт review", domains or self.settings.review_domains, max_results, region=region
            )
            pages = await asyncio.gather(*(self.fetcher.read(h.url, region) for h in hits))
            return hits, errors, pages

        marketplace_reviews, marketplace_errors = [], []
        if self.marketplace_hub is not None:
            (hits, errors, pages), (marketplace_reviews, marketplace_errors) = await asyncio.gather(
                web_reviews(), self.marketplace_hub.get_reviews(model, limit=max_results),
            )
        else:
            hits, errors, pages = await web_reviews()
        reviews = dedupe_reviews(
            marketplace_reviews + [review for page in pages for review in review_excerpts(page, model)]
        )
        return {
            "model": model,
            "checked_at": now(),
            "reviews": reviews,
            "errors": errors + marketplace_errors,
            "sources": [
                {**h.model_dump(mode="json"), "fetch_status": p.status, "fetch_error": p.error}
                for h, p in zip(hits, pages)
            ],
            "limitations": [
                "Observed sample only. Review ownership and authenticity are unverified.",
                "Mixed-model discussions are returned only when a fragment names the requested model.",
            ],
        }

    async def research(self, query, mode="quick", domains=None, model=None, city=None):
        budget = 45 if (mode == "quick" and self.marketplace_hub is not None) else (30 if mode == "quick" else 180)
        output = {
            "query": query,
            "mode": mode,
            "checked_at": now(),
            "budget_seconds": budget,
            "timed_out": False,
            "results": {},
            "errors": [],
        }
        web_progress = {}
        output["results"]["web"] = web_progress
        jobs = {
            "web": self.search_web(
                query,
                domains=domains,
                max_results=3 if mode == "quick" else 8,
                dynamic=mode == "deep",
                _progress=web_progress,
            )
        }
        if model:
            shopping_markets = domains
            if shopping_markets is None and mode == "quick" and self.marketplace_hub is not None:
                quick_core = ["yandex_market", "wildberries", "aliexpress", "avito", "ozon"]
                enabled = set(self.settings.enabled_marketplaces or self.settings.marketplaces) or (
                    self.marketplace_hub.known_marketplaces
                )
                shopping_markets = [name for name in quick_core if name in enabled] or None
            jobs["shopping"] = self.compare_prices(
                model, marketplaces=shopping_markets, city=city, max_results=6 if mode == "quick" else 12
            )
            if mode == "deep":
                jobs["reviews"] = self.get_product_reviews(model, domains=domains)
        elif mode == "deep":
            jobs["discussions"] = self.search_web(
                query, domains=domains or self.settings.review_domains, max_results=8
            )

        async def collect(name, job):
            try:
                output["results"][name] = await job
            except asyncio.CancelledError:
                output["errors"].append({"component": name, "error": "time_budget_exceeded"})
                raise
            except Exception:
                output["errors"].append({"component": name, "error": "component_unavailable"})

        try:
            async with asyncio.timeout(budget):
                await asyncio.gather(*(collect(name, job) for name, job in jobs.items()))
        except TimeoutError:
            output["timed_out"] = True
        return output

    async def diagnose_egress(self):
        jobs = [self.fetcher.http.diagnose(region) for region in ("RU", "EU")]
        if self.marketplace_hub is not None:
            jobs.append(self.marketplace_hub.health())
        rows = await asyncio.gather(*jobs)
        result = {
            "checked_at": now(),
            "egress": rows[:2],
            "distinct_ips": bool(rows[0].get("ip") and rows[1].get("ip") and rows[0]["ip"] != rows[1]["ip"]),
        }
        if self.marketplace_hub is not None:
            result["marketplace_hub"] = rows[2]
        return result


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine
