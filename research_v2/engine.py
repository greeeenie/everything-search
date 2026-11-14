import asyncio
from collections import defaultdict
from decimal import Decimal
import json

from .config import Settings
from .discovery import Discovery, canonical_url
from .fetching import Fetcher
from .models import now
from .shopping import adapter_for, model_matches, product_matches, review_excerpts
from .store import Store


class Engine:
    def __init__(self, settings=None, store=None, fetcher=None, discovery=None):
        self.settings = settings or Settings.load()
        self.store = store or Store(self.settings.database)
        self.fetcher = fetcher or Fetcher(self.settings, self.store)
        self.discovery = discovery or Discovery(self.settings, self.store)

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
            else:
                row["fetch_error"] = page.error
            result["results"].append(row)
            result["pages_read"] += int(row["status"] == "verified")

        await asyncio.gather(*(read(h) for h in hits))
        return result

    def shop_domains(self, marketplaces):
        names = marketplaces or list(self.settings.marketplaces)
        if len(names) > 8:
            raise ValueError("At most eight marketplaces")
        return [self.settings.marketplaces.get(name, name) for name in names]

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
        hits, errors = await self.discovery.search(
            search_query,
            self.shop_domains(marketplaces),
            max_results,
            exact=bool(model and not specifications),
            region=region,
        )
        if model:
            from urllib.parse import unquote, urlsplit

            hits = [
                hit
                for hit in hits
                if product_matches(model, hit.title)
                or product_matches(model, unquote(urlsplit(hit.url).path).replace("-", " ").replace("_", " "))
            ]
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
                "Prices and stock describe retrieved public pages at checked_at.",
                "Delivery to the requested city must be stated by the source to be confirmed.",
                "Missing structured product/offer data is reported, never filled from index prices.",
            ],
        }
        products, offers = {}, {}
        filter_currency = (
            currency.upper()
            if currency
            else ("RUB" if min_price is not None or max_price is not None else None)
        )

        async def read(hit):
            page = await self.fetcher.read(hit.url, region, dynamic=dynamic)
            return hit, page, adapter_for(page.final_url or hit.url).parse(page, model)

        rows = await asyncio.gather(*(read(h) for h in hits), return_exceptions=True)
        for hit, row in zip(hits, rows):
            if isinstance(row, Exception):
                output["errors"].append({"url": hit.url, "error": "parsing_error"})
                continue
            _, page, (parsed_products, parsed_offers) = row
            candidate = hit.model_dump(mode="json")
            candidate.update(
                fetch_status=page.status, fetch_error=page.error, fetch_checked_at=page.checked_at
            )
            if not parsed_products:
                candidate["extraction_error"] = (
                    "model_mismatch_or_no_product_data" if page.status == "verified" else page.error
                )
            output["candidates"].append(candidate)
            for product in parsed_products:
                if product.id in products:
                    products[product.id].sources.extend(product.sources)
                    # Specifications remain source-scoped when sources disagree.
                    for key, value in product.specifications.items():
                        current = products[product.id]
                        if key in current.specification_conflicts:
                            if value not in current.specification_conflicts[key]:
                                current.specification_conflicts[key].append(value)
                        elif key not in current.specifications:
                            current.specifications[key] = value
                        elif current.specifications[key] != value:
                            current.specification_conflicts[key] = [current.specifications.pop(key), value]
                else:
                    products[product.id] = product
            for offer in parsed_offers:
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
                key = (
                    canonical_url(offer.url),
                    offer.seller,
                    offer.currency,
                    offer.price,
                    offer.condition,
                    json.dumps(offer.variants, sort_keys=True),
                )
                offers[key] = offer
                evidence = offer.evidence.model_dump(mode="json")
                evidence["normalized_offer"] = offer.model_dump(mode="json")
                self.store.record(evidence)
        output["products"] = [x.model_dump(mode="json") for x in products.values()]
        output["offers"] = [x.model_dump(mode="json") for x in offers.values()]
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
            comparisons.append(
                {
                    "product_id": product_id,
                    "currency": currency,
                    "condition": state,
                    "region": region,
                    "variants": json.loads(variant),
                    "offers": offers,
                    "best_offer": eligible[0] if eligible else None,
                    "ranking": "public in-stock total price including confirmed city delivery",
                    "limitation": None
                    if eligible
                    else "No offer has all fields needed for a confirmed total-price winner.",
                }
            )
        result["comparisons"] = comparisons
        return result

    async def get_product_details(self, url=None, model=None, region=None):
        if not url and not model:
            raise ValueError("Provide url or model")
        if url:
            page = await self.fetcher.read(url, region, dynamic=True)
            products, offers = adapter_for(page.final_url or url).parse(page, model)
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
            return {
                "checked_at": now(),
                "products": [p.model_dump(mode="json") for p in products],
                "offers": [o.model_dump(mode="json") for o in offers],
                "page": page.model_dump(mode="json"),
                "extraction_error": None if products else "no_matching_structured_product",
            }
        return await self.search_products(model, model=model, region=region)

    async def get_product_reviews(self, model, domains=None, max_results=8, region=None):
        hits, errors = await self.discovery.search(
            model + " отзывы опыт review", domains or self.settings.review_domains, max_results, region=region
        )
        pages = await asyncio.gather(*(self.fetcher.read(h.url, region) for h in hits))
        reviews, seen = [], set()
        for page in pages:
            for review in review_excerpts(page, model):
                key = (review["url"], review["text"])
                if key not in seen:
                    seen.add(key)
                    reviews.append(review)
        return {
            "model": model,
            "checked_at": now(),
            "reviews": reviews,
            "errors": errors,
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
        budget = 30 if mode == "quick" else 180
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
            jobs["shopping"] = self.compare_prices(
                model, marketplaces=domains, city=city, max_results=6 if mode == "quick" else 12
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
        rows = await asyncio.gather(*(self.fetcher.http.diagnose(region) for region in ("RU", "EU")))
        return {
            "checked_at": now(),
            "egress": rows,
            "distinct_ips": bool(rows[0].get("ip") and rows[1].get("ip") and rows[0]["ip"] != rows[1]["ip"]),
        }


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine
