import json

import httpx
import pytest

from research_v2.config import Settings
from research_v2.fetching import Fetcher
from research_v2.models import Page
from research_v2.network import SafeHTTP
from research_v2.store import Store


@pytest.fixture
def settings():
    return Settings(
        domains={"ozon.ru": "RU", "avito.ru": "RU", "market.yandex.ru": "RU", "reddit.com": "EU"},
        marketplaces={"ozon": "ozon.ru", "avito": "avito.ru", "yandex_market": "market.yandex.ru"},
        enabled_marketplaces=["ozon", "avito", "yandex_market"],
        verify_egress=False,
        eu_proxy="",
        database=":memory:",
        per_host_interval=0,
    )


@pytest.fixture
def store():
    value = Store(":memory:")
    yield value
    value.close()


async def public_resolver(host, port):
    return "93.184.216.34"


def fetcher_for(settings, store, handler):
    return Fetcher(
        settings, store, SafeHTTP(settings, resolver=public_resolver, transport=httpx.MockTransport(handler))
    )


def product_page(
    name="Wanbo Cube 2 Pro",
    price="12990",
    currency="RUB",
    condition="NewCondition",
    host="ozon.ru",
    extra_text="",
    availability="InStock",
    extra_offer=None,
):
    path = {
        "ozon.ru": "/product/wanbo-cube-2-pro-123456/",
        "avito.ru": "/omsk/elektronika/wanbo_12345678",
        "market.yandex.ru": "/product--wanbo/123456",
    }.get(host, "/product/123456")
    offer = {
        "@type": "Offer",
        "price": price,
        "priceCurrency": currency,
        "availability": "https://schema.org/" + availability,
        "itemCondition": "https://schema.org/" + condition,
        "seller": {"name": "Shop A"},
    }
    offer.update(extra_offer or {})
    data = {
        "@type": "Product",
        "name": name,
        "brand": {"name": "Wanbo"},
        "offers": offer,
        "additionalProperty": [{"name": "ANSI", "value": "500"}],
    }
    html = (
        "<title>" + name + "</title><p>" + name + " owner report with model-specific detail</p>" + extra_text
    )
    html += '<script type="application/ld+json">' + json.dumps(data) + "</script>"
    return Page(
        url="https://" + host + path,
        final_url="https://" + host + path,
        source="public_page",
        status="verified",
        region="RU",
        title=name,
        html=html,
        text=name + "\n" + extra_text,
        structured_data=[data],
    )
